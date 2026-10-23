# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Dashboard job runner — every CLI capability, triggerable online.

Kinds mirror the CLI:
  smoke       argus test exec
  full        argus test run [--source --spec --pr-number --expand-coverage]
  generate    argus test generate [--source --spec --expand-coverage]
  discover    argus test discover
  create      argus test create "description" [--execute]
  regression  argus vision regression [--update-baselines]

One job at a time; runs on a daemon thread; kind-specific `result` payloads.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

PROBE_KINDS = {
    "redirects", "headers", "cookies", "robots", "security_paths",
    "api_check", "sitemap_test", "dns_records", "cors", "transport",
}
VALID_KINDS = {
    "smoke", "full", "generate", "discover", "create", "regression",
    "crawl_test", "audit", "flaky", "screenshot", "compare", "ping",
    "loadtest", "tls", "flow", "route_sweep",
    "api_contract", "api_contract_diff", "contract_verify", "auth_test", "realtime", "vitals", "ai_flow",
    "har_replay", "import_codegen",
    "misconfig_scan", "cve_lookup", "sca_scan", "llm_redteam", "exploit_poc", "attack_chain",
    "host_pentest", "cloud_pentest", "db_assert", "chaos_inject", "chaos_webhook",
    "port_scan", "tls_cipher_scan",
    "dast_scan", "injection_scan", "csrf_probe", "ssrf_probe",
    "auth_attack_scan", "idor_scan",
    "select_tests",
} | PROBE_KINDS

# Job kinds gated behind an authorized security engagement
# (orchestrator/security/engagement_policy.py) — see the enforcement call
# near the end of _validate(). Values are the minimum engagement tier
# required.
ELEVATED_RISK_KINDS: dict[str, str] = {
    "misconfig_scan": "active_recon",
    "cve_lookup": "active_recon",
    "sca_scan": "active_recon",
    "contract_verify": "active_recon",
    "db_assert": "active_recon",
    "llm_redteam": "active_recon",
    "port_scan": "active_recon",
    "tls_cipher_scan": "active_recon",
    "exploit_poc": "exploit",
    "attack_chain": "exploit",
    "host_pentest": "exploit",
    "cloud_pentest": "exploit",
    "chaos_inject": "exploit",
    "chaos_webhook": "exploit",
    "dast_scan": "exploit",
    "injection_scan": "exploit",
    "csrf_probe": "exploit",
    "ssrf_probe": "exploit",
    "auth_attack_scan": "exploit",
    "idor_scan": "exploit",
}

# control_kind allowlist for chaos_inject/chaos_webhook -- the "observe
# behavior under fault" test. Deliberately narrow: running e.g. exploit_poc
# as the "control" makes no sense and must be rejected, not silently
# accepted.
_CHAOS_CONTROL_KINDS = {"flow", "smoke"}

# Active web-attack / DAST kinds — require ZYVOR_DAST_SCAN_ENABLED in
# addition to an exploit-tier engagement (same fail-closed posture as
# ZYVOR_EXPLOIT_EXECUTION_ENABLED for exploit_poc).
DAST_KINDS = frozenset({
    "dast_scan", "injection_scan", "csrf_probe", "ssrf_probe",
    "auth_attack_scan", "idor_scan",
})

_lock = threading.Lock()
_cancel = threading.Event()
_progress: list[str] = []
_live_cases: list[dict[str, Any]] = []
_state: dict[str, Any] = {
    "running": False,
    "kind": None,
    "params": {},
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
}


class JobCancelled(Exception):
    pass


def _repo_root() -> Path:
    from orchestrator.paths import repo_root

    return repo_root()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_progress(message: str) -> None:
    """Append a redacted stage line visible live in the dashboard's job panel."""
    from orchestrator.security.redaction import redact_text
    message = redact_text(message)
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    with _lock:
        _progress.append(f"[{stamp}] {message}")
        del _progress[:-200]


def _check_cancel() -> None:
    if _cancel.is_set():
        raise JobCancelled("cancelled by user")


def _stream_line(line: str) -> None:
    """Feed a Playwright stdout line into the live log and per-test tally."""
    import re

    log_progress(line)
    m = re.match(r"^\s*(✓|✗|✘|×)\s+\d+\s+(?:\[([^\]]+)\]\s+)?›?\s*(.+?)(?:\s+\(([\d.]+m?s)\))?\s*$", line)
    if not m:
        return
    mark, browser, title = m.group(1), m.group(2), m.group(3)
    status = "passed" if mark == "✓" else "failed"
    with _lock:
        _live_cases.append({"title": title.strip()[:120], "status": status, "browser": browser})


def _stream_line_flow(line: str) -> None:
    """Flow step line (▶/✓/✗ step N: desc) → live log + per-step tally chip."""
    import re

    log_progress(line)
    m = re.match(r"^([✓✗])\s+step\s+\d+:\s+(.+?)(?:\s+—.*)?$", line)
    if not m:
        return
    with _lock:
        _live_cases.append({"title": m.group(2)[:80], "status": "passed" if m.group(1) == "✓" else "failed", "browser": None})


def _stream_line_audit(line: str) -> None:
    """Audit progress line → live log + one live-case per audited page."""
    import re

    log_progress(line)
    m = re.match(r"^audit:\s+(\S+)\s+\((?:HTTP\s+)?(\d+)\)", line)
    if not m:
        return
    path_, status = m.group(1), int(m.group(2))
    with _lock:
        _live_cases.append(
            {"title": path_, "status": "passed" if 200 <= status < 400 else "failed", "browser": None}
        )


def cancel() -> dict[str, Any]:
    """Request cancellation of the running job (kills an in-flight Playwright run)."""
    with _lock:
        running = _state["running"]
    if running:
        _cancel.set()
        log_progress("⏹ cancellation requested…")
        try:
            from agents.execution.runner import terminate_current

            if terminate_current():
                log_progress("terminated in-flight Playwright process")
        except Exception:
            pass
    return status()


def _redact_params(params: Any) -> Any:
    """Deeply redact credentials in status, history and schedule responses."""
    from orchestrator.security.redaction import redact
    return redact(params)


def status() -> dict[str, Any]:
    with _lock:
        state = dict(_state)
        state["params"] = _redact_params(state.get("params"))
        state["progress"] = _progress[-80:]
        state["live_cases"] = list(_live_cases)
        state["live_tally"] = {
            "passed": sum(1 for c in _live_cases if c["status"] == "passed"),
            "failed": sum(1 for c in _live_cases if c["status"] != "passed"),
        }
    return state


def _safe_local_spec(spec: str) -> str:
    """Resolve a local spec path strictly inside the repo — the trigger is
    network-reachable and must not read arbitrary host files."""
    root = os.path.realpath(str(_repo_root()))
    fullpath = os.path.realpath(os.path.join(root, spec))
    if not fullpath.startswith(root + os.sep):
        raise ValueError("spec path must be inside the repository")
    if not os.path.isfile(fullpath):
        raise ValueError(f"spec not found: {spec}")
    return fullpath


def _validate(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """Normalize and validate params up-front (raises ValueError → HTTP 400)."""
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown job kind: {kind}")
    clean: dict[str, Any] = {}
    if kind in {"full", "generate", "discover"}:
        source = str(params.get("source") or ("github" if kind == "discover" else "local"))
        if source not in {"local", "github", "document", "email", "transcript", "jira", "diarize"}:
            raise ValueError(
                "source must be local, github, document, email, transcript, jira, or diarize"
            )
        clean["source"] = source
        spec = (params.get("spec") or "").strip()
        if spec and source in {"local", "document", "email", "transcript", "jira", "diarize"}:
            # jira JSON exports / .eml / transcripts are local paths when not live keys
            if source != "jira" or spec.lower().endswith(".json"):
                spec = _safe_local_spec(spec)
        clean["spec"] = spec or None
        clean["expand_coverage"] = bool(params.get("expand_coverage"))
        if source == "jira":
            keys = params.get("jira_issue_keys") or params.get("issue_keys") or []
            if isinstance(keys, str):
                keys = [k.strip() for k in keys.replace(",", " ").split() if k.strip()]
            clean["jira_issue_keys"] = [str(k)[:64] for k in keys][:50]
    if kind == "full":
        pr = params.get("pr_number")
        clean["pr_number"] = int(pr) if pr not in (None, "", 0) else None
    if kind == "create":
        description = (params.get("description") or "").strip()
        if not description:
            raise ValueError("description is required")
        clean["description"] = description[:500]
        clean["execute"] = bool(params.get("execute"))
    if kind == "regression":
        clean["update_baselines"] = bool(params.get("update_baselines"))
    if kind == "crawl_test":
        url = (params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = url[:500]
        clean["username"] = (params.get("username") or "").strip()[:200]
        clean["password"] = (params.get("password") or "")[:200]
        clean["insecure"] = bool(params.get("insecure"))
        max_pages = int(params.get("max_pages") or 30)
        clean["max_pages"] = max(1, min(max_pages, 200))
    if kind == "audit":
        url = (params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = url[:500]
        clean["max_pages"] = max(1, min(int(params.get("max_pages") or 10), 100))
        selected = params.get("checks") or ["a11y", "links", "seo", "console", "perf", "headers"]
        from agents.audit.engine import VALID_CHECKS

        clean["checks"] = [c for c in selected if c in VALID_CHECKS] or ["a11y", "seo", "console"]
        clean["username"] = (params.get("username") or "").strip()[:200]
        clean["password"] = (params.get("password") or "")[:200]
        clean["insecure"] = bool(params.get("insecure"))
    if kind == "flaky":
        clean["runs"] = max(2, min(int(params.get("runs") or 3), 10))
        target = (params.get("target") or "manual").strip()
        if target != "manual":
            target = _safe_local_spec(target)
        clean["target"] = target
    if kind == "screenshot":
        url = (params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = url[:500]
        vps = params.get("viewports") or ["desktop"]
        clean["viewports"] = [v for v in vps if v in ("desktop", "tablet", "mobile")] or ["desktop"]
        clean["full_page"] = bool(params.get("full_page"))
        clean["insecure"] = bool(params.get("insecure"))
    if kind == "compare":
        for key in ("url_a", "url_b"):
            u = (params.get(key) or "").strip()
            if not u.startswith(("http://", "https://")):
                raise ValueError(f"{key} must start with http:// or https://")
            clean[key] = u[:500]
        clean["insecure"] = bool(params.get("insecure"))
    if kind == "ping":
        raw = params.get("urls") or ""
        if isinstance(raw, str):
            raw = raw.replace(",", "\n").split("\n")
        urls = [u.strip() for u in raw if u.strip().startswith(("http://", "https://"))]
        if not urls:
            raise ValueError("provide at least one http(s) URL")
        clean["urls"] = urls[:30]
    if kind == "loadtest":
        url = (params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = url[:500]
        clean["requests"] = max(10, min(int(params.get("requests") or 100), 1000))
        clean["concurrency"] = max(1, min(int(params.get("concurrency") or 10), 50))
    if kind == "tls":
        host = (params.get("host") or "").strip()
        if host.startswith(("http://", "https://")):
            from urllib.parse import urlparse

            host = urlparse(host).hostname or ""
        if not host or "/" in host or " " in host:
            raise ValueError("provide a hostname, e.g. zyvor.dev")
        clean["host"] = host[:255]
        clean["port"] = max(1, min(int(params.get("port") or 443), 65535))
    if kind == "flow":
        url = (params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = url[:500]
        clean["description"] = (params.get("description") or "").strip()[:2000]
        clean["steps_mode"] = bool(params.get("steps_mode"))
        if not clean["description"]:
            raise ValueError("describe the journey or provide steps")
        clean["username"] = (params.get("username") or "").strip()[:200]
        clean["password"] = (params.get("password") or "")[:200]
        clean["insecure"] = bool(params.get("insecure"))
        clean["record"] = params.get("record", True) is not False
        clean["session"] = (params.get("session") or "").strip()[:200]
        clean["browser"] = params.get("browser") if params.get("browser") in ("chromium", "firefox", "webkit") else ""
        clean["device"] = (params.get("device") or "").strip()[:60]
        clean["throttle"] = params.get("throttle") if params.get("throttle") in ("3g", "offline") else ""
    if kind == "route_sweep":
        url = (params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = url[:500]
        raw = params.get("routes") or ""
        if isinstance(raw, str):
            raw = raw.replace(",", "\n").split("\n")
        clean["routes"] = [r.strip() for r in raw if r.strip().startswith("/")][:40] or ["/"]
        vps = params.get("viewports") or ["desktop"]
        clean["viewports"] = [v for v in vps if v in ("desktop", "mobile")] or ["desktop"]
        clean["update_baselines"] = bool(params.get("update_baselines"))
        clean["insecure"] = bool(params.get("insecure"))
        clean["auto"] = bool(params.get("auto"))
        clean["max_pages"] = max(1, min(int(params.get("max_pages") or 20), 40))
        clean["username"] = (params.get("username") or "").strip()[:200]
        clean["password"] = (params.get("password") or "")[:200]
    if kind == "api_contract":
        base = (params.get("url") or params.get("base") or "").strip()
        if not base.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = base[:500]
        clean["mode"] = "workflow" if params.get("mode") == "workflow" else "spec"
        clean["insecure"] = bool(params.get("insecure"))
        clean["include_writes"] = bool(params.get("include_writes"))
        clean["max_endpoints"] = max(1, min(int(params.get("max_endpoints") or 60), 200))
        spec = params.get("spec")
        workflow = params.get("workflow")
        if clean["mode"] == "spec" and not spec:
            raise ValueError("provide an OpenAPI spec (URL or JSON)")
        if clean["mode"] == "workflow" and not workflow:
            raise ValueError("provide a workflow (list of steps)")
        clean["spec"] = spec
        clean["workflow"] = workflow if isinstance(workflow, list) else None
        clean["auth"] = params.get("auth") if isinstance(params.get("auth"), dict) else None
        clean["path_params"] = params.get("path_params") if isinstance(params.get("path_params"), dict) else None
    if kind == "api_contract_diff":
        def _clean_spec_ref(value: Any, label: str) -> Any:
            if isinstance(value, dict):
                return value
            if isinstance(value, str) and value.strip():
                v = value.strip()
                if v.startswith(("http://", "https://")) or v.startswith("git:"):
                    return v[:1000]
            raise ValueError(f"{label} must be an inline object, an http(s) URL, or 'git:<ref>:<path>'")

        clean["spec_a"] = _clean_spec_ref(params.get("spec_a"), "spec_a")
        clean["spec_b"] = _clean_spec_ref(params.get("spec_b"), "spec_b")
        clean["insecure"] = bool(params.get("insecure"))
        clean["fail_on"] = "any" if params.get("fail_on") == "any" else "breaking"
    if kind == "contract_verify":
        url = (params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = url[:500]
        har = (params.get("har") or "").strip()
        if not har and not os.environ.get("ZYVOR_HAR_PATH"):
            raise ValueError("provide a HAR path (or set ZYVOR_HAR_PATH)")
        clean["har"] = har[:500]
        clean["insecure"] = bool(params.get("insecure"))
        clean["max_endpoints"] = max(1, min(int(params.get("max_endpoints") or 60), 200))
    if kind == "sca_scan":
        url = (params.get("url") or "").strip()
        checkout_path = (params.get("checkout_path") or "").strip()
        if not url and not checkout_path:
            raise ValueError("provide a url (black-box mode) and/or checkout_path (local-checkout mode)")
        clean["url"] = url[:500] if url else ""
        if clean["url"] and not clean["url"].startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["checkout_path"] = checkout_path[:1000]
        clean["insecure"] = bool(params.get("insecure"))
    if kind == "vitals":
        url = (params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = url[:500]
        clean["insecure"] = bool(params.get("insecure"))
        clean["device"] = (params.get("device") or "").strip()[:60]
        clean["throttle"] = (params.get("throttle") or "").strip().lower() if params.get("throttle") in ("3g", "offline") else ""
    if kind == "ai_flow":
        url = (params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = url[:500]
        clean["goal"] = (params.get("goal") or "").strip()[:600]
        if not clean["goal"]:
            raise ValueError("describe the goal for the AI agent")
        clean["session"] = (params.get("session") or "").strip()[:200]
        clean["max_steps"] = max(1, min(int(params.get("max_steps") or 20), 40))
        clean["insecure"] = bool(params.get("insecure"))
    if kind == "auth_test":
        base = (params.get("url") or "").strip()
        if not base.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = base[:500]
        clean["login_url"] = (params.get("login_url") or "").strip()[:500]
        clean["api_login"] = (params.get("api_login") or "").strip()[:500]
        clean["protected"] = (params.get("protected") or "").strip()[:500]
        clean["logout_url"] = (params.get("logout_url") or "").strip()[:500]
        clean["username"] = (params.get("username") or "").strip()[:200]
        clean["password"] = (params.get("password") or "")[:200]
        clean["token_path"] = (params.get("token_path") or "token").strip()[:100]
        clean["save_session"] = params.get("save_session", True) is not False
        clean["insecure"] = bool(params.get("insecure"))
        if not (clean["login_url"] or clean["api_login"]):
            raise ValueError("provide a login page URL or an API login endpoint")
    if kind == "realtime":
        base = (params.get("url") or "").strip()
        if not base.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = base[:500]
        clean["ws"] = (params.get("ws") or "").strip()[:500]
        clean["sse"] = (params.get("sse") or "").strip()[:500]
        clean["ticket_url"] = (params.get("ticket_url") or "").strip()[:500]
        clean["ticket_query"] = (params.get("ticket_query") or "ticket").strip()[:40]
        clean["token"] = (params.get("token") or "")[:2000]
        clean["token_query"] = (params.get("token_query") or "token").strip()[:40]
        clean["ws_subprotocol"] = (params.get("ws_subprotocol") or "access_token").strip()[:60]
        clean["subprotocol_jwt"] = bool(params.get("subprotocol_jwt"))
        clean["expect_messages"] = max(1, min(int(params.get("expect_messages") or 1), 100))
        clean["window_ms"] = max(1000, min(int(params.get("window_ms") or 15000), 120000))
        clean["live_selector"] = (params.get("live_selector") or "").strip()[:200]
        clean["session"] = (params.get("session") or "").strip()[:200]
        clean["insecure"] = bool(params.get("insecure"))
        if not (clean["ws"] or clean["sse"] or clean["live_selector"]):
            raise ValueError("provide a ws path, sse path, or a live-view selector")
    if kind == "har_replay":
        url = (params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = url[:500]
        mode = (params.get("mode") or "replay").strip().lower()
        if mode not in {"record", "replay"}:
            raise ValueError("mode must be record or replay")
        clean["mode"] = mode
        raw = params.get("routes") or "/"
        if isinstance(raw, str):
            raw = raw.replace(",", "\n").split("\n")
        clean["routes"] = [r.strip() for r in raw if r.strip().startswith("/")][:40] or ["/"]
        clean["har"] = (params.get("har") or "").strip()[:500]
        clean["expect_text"] = (params.get("expect_text") or "").strip()[:200]
        clean["not_found_ok"] = bool(params.get("not_found_ok"))
        clean["insecure"] = bool(params.get("insecure"))
        if mode == "replay" and not clean["har"] and not os.environ.get("ZYVOR_HAR_PATH"):
            raise ValueError("provide a HAR path for replay (or set ZYVOR_HAR_PATH)")
    if kind == "import_codegen":
        script = (params.get("script") or "").strip()
        if not script:
            raise ValueError("paste a Playwright codegen script")
        clean["script"] = script[:50000]
        url = (params.get("url") or "").strip()
        if url and not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = url[:500] if url else ""
        clean["run"] = bool(params.get("run"))
        clean["insecure"] = bool(params.get("insecure"))
        if clean["run"] and not clean["url"]:
            raise ValueError("url is required when run is enabled")
    if kind == "select_tests":
        clean["base"] = (params.get("base") or "HEAD~1").strip()[:80] or "HEAD~1"
        clean["head"] = (params.get("head") or "HEAD").strip()[:80] or "HEAD"
        clean["include_quarantined"] = bool(params.get("include_quarantined"))
    if kind == "smoke":
        clean["grep"] = (params.get("grep") or "").strip()[:200]
        clean["shard"] = (params.get("shard") or "").strip()[:20]
        if clean["shard"] and not re.match(r"^\d+/\d+$", clean["shard"]):
            raise ValueError("shard must look like 1/2")
    if kind == "misconfig_scan":
        url = (params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = url[:500]
        clean["max_paths"] = max(1, min(int(params.get("max_paths") or 60), 300))
        clean["insecure"] = bool(params.get("insecure"))
    if kind == "cve_lookup":
        url = (params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = url[:500]
        clean["insecure"] = bool(params.get("insecure"))
    if kind == "llm_redteam":
        from agents.redteam.battery import VALID_CATEGORIES

        target = params.get("target") or "dashboard_ask"
        if target not in ("dashboard_ask", "v1_qa"):
            raise ValueError("target must be 'dashboard_ask' or 'v1_qa'")
        clean["target"] = target
        if target == "v1_qa":
            base_url = (params.get("base_url") or "").strip()
            if not base_url.startswith(("http://", "https://")):
                raise ValueError("base_url must start with http:// or https:// for target=v1_qa")
            clean["url"] = base_url[:500]
            clean["api_key"] = (params.get("api_key") or "").strip()[:500]
            if not clean["api_key"]:
                raise ValueError("api_key is required for target=v1_qa")
        else:
            clean["url"] = "dashboard_ask"
        selected = params.get("categories") or sorted(VALID_CATEGORIES)
        clean["categories"] = [c for c in selected if c in VALID_CATEGORIES] or sorted(VALID_CATEGORIES)
        clean["max_prompts"] = max(1, min(int(params.get("max_prompts") or 40), 40))
    if kind == "exploit_poc":
        # Fail closed: this kind requires an explicit, separate opt-in even
        # beyond the exploit-tier engagement check below — mirrors
        # ZYVOR_AGENT_ALLOW_DESTRUCTIVE's pattern in agent_policy.py.
        if os.environ.get("ZYVOR_EXPLOIT_EXECUTION_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"}:
            raise ValueError("exploit_poc is disabled — set ZYVOR_EXPLOIT_EXECUTION_ENABLED=true to enable it")
        url = (params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = url[:500]
        description = (params.get("finding_description") or "").strip()
        if not description:
            raise ValueError("finding_description is required — describe what to verify")
        clean["finding_description"] = description[:2000]
        clean["timeout_s"] = max(5, min(int(params.get("timeout_s") or 60), 300))
    if kind == "attack_chain":
        # Same fail-closed opt-in as exploit_poc — chaining is at least as
        # sensitive (it's just exploit_poc run in a loop with an LLM planner).
        if os.environ.get("ZYVOR_EXPLOIT_EXECUTION_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"}:
            raise ValueError("attack_chain is disabled — set ZYVOR_EXPLOIT_EXECUTION_ENABLED=true to enable it")
        url = (params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = url[:500]
        objective = (params.get("objective") or "").strip()
        if not objective:
            raise ValueError("objective is required — describe the escalation goal")
        clean["objective"] = objective[:1000]
        clean["max_steps"] = max(1, min(int(params.get("max_steps") or 5), 5))
        clean["timeout_s"] = max(5, min(int(params.get("timeout_s") or 60), 300))
    if kind in ("host_pentest", "cloud_pentest"):
        from orchestrator.security.secrets import SecretReferenceError, assert_persistable

        # Two independent fail-closed opt-ins, on top of the exploit-tier
        # engagement check below: using real credentials against real
        # infrastructure is a materially bigger step than generating/running
        # a verification script against a URL (exploit_poc/attack_chain).
        if os.environ.get("ZYVOR_EXPLOIT_EXECUTION_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"}:
            raise ValueError(f"{kind} is disabled — set ZYVOR_EXPLOIT_EXECUTION_ENABLED=true to enable it")
        if os.environ.get("ZYVOR_CREDENTIALED_PENTEST_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"}:
            raise ValueError(f"{kind} is disabled — set ZYVOR_CREDENTIALED_PENTEST_ENABLED=true to enable it")
        description = (params.get("finding_description") or "").strip()
        if not description:
            raise ValueError("finding_description is required — describe what to verify")
        clean["finding_description"] = description[:2000]
        clean["timeout_s"] = max(5, min(int(params.get("timeout_s") or 60), 300))
        creds = params.get("creds")
        if not isinstance(creds, dict) or not creds:
            raise ValueError("creds is required — a dict of credential fields (secrets must use {'$secret': 'env:NAME'})")
        try:
            assert_persistable(creds, parent_key="creds")
        except SecretReferenceError as exc:
            raise ValueError(str(exc)) from exc
        clean["creds"] = creds
    if kind == "host_pentest":
        host = (params.get("host") or "").strip()
        if not host or any(ch in host for ch in "/?#@ "):
            raise ValueError("provide a bare hostname or IP, e.g. host.example.com")
        clean["host"] = host[:255]
        clean["port"] = max(1, min(int(params.get("port") or 22), 65535))
        clean["url"] = clean["host"]  # engagement target-pattern matches on this
        host_creds = clean["creds"]
        if "username" not in host_creds or not str(host_creds.get("username") or "").strip():
            raise ValueError("creds.username is required")
        if "password" not in host_creds and "private_key" not in host_creds:
            raise ValueError("creds must include either 'password' or 'private_key'")
    if kind == "cloud_pentest":
        provider = (params.get("provider") or "").strip().lower()
        if provider not in ("aws", "gcp", "azure"):
            raise ValueError("provider must be 'aws', 'gcp', or 'azure'")
        clean["provider"] = provider
        target = (params.get("target") or "").strip()
        if not target:
            raise ValueError("target is required — an account/project identifier, e.g. 'aws-prod-123456789012'")
        clean["target"] = target[:200]
        clean["url"] = clean["target"]  # engagement target-pattern matches on this
    if kind == "db_assert":
        # One explicit fail-closed opt-in, not exploit_poc's three-gate
        # stack -- this is read-only and makes no destructive claim, but it
        # does touch live data with real credentials, which warrants more
        # than the probe kinds get.
        if os.environ.get("ZYVOR_DB_TESTING_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"}:
            raise ValueError("db_assert is disabled — set ZYVOR_DB_TESTING_ENABLED=true to enable it")

        engine = (params.get("engine") or "").strip().lower()
        if engine not in ("postgres", "mysql", "sqlite"):
            raise ValueError("engine must be 'postgres', 'mysql', or 'sqlite'")
        clean["engine"] = engine

        from orchestrator.security.secrets import SecretReferenceError, assert_persistable, is_secret_ref

        db_secret = params.get("db_secret")
        if not is_secret_ref(db_secret):
            raise ValueError("db_secret is required and must be a {'$secret': 'env:NAME'} reference")
        try:
            assert_persistable(db_secret, parent_key="db_secret")
        except SecretReferenceError as exc:
            raise ValueError(str(exc)) from exc
        clean["db_secret"] = db_secret

        # target is a declarative label for engagement-scope matching/audit
        # only -- the real connection endpoint is inside db_secret's
        # resolved DSN, which isn't available at validation time (secrets
        # are resolved only at execution time, inside the sandbox). Same
        # shape as cloud_pentest's `target` (an opaque account/project
        # identifier, not a literal validated hostname).
        target = (params.get("target") or "").strip()
        if not target:
            raise ValueError("target is required — a label identifying the database, e.g. 'staging-orders-db'")
        clean["target"] = target[:200]
        clean["url"] = clean["target"]

        from orchestrator.security.sql_guard import SqlGuardError, validate_select_only

        query = (params.get("query") or "").strip()
        try:
            clean["query"] = validate_select_only(query)
        except SqlGuardError as exc:
            raise ValueError(str(exc)) from exc

        raw_query_params = params.get("query_params")
        clean["query_params"] = raw_query_params[:50] if isinstance(raw_query_params, list) else []

        assertion = params.get("assertion")
        if not isinstance(assertion, dict) or assertion.get("mode") not in {"row_count", "cell_equals", "column_values"}:
            raise ValueError("assertion must be a dict with mode in row_count|cell_equals|column_values")
        clean["assertion"] = assertion
        clean["timeout_s"] = max(5, min(int(params.get("timeout_s") or 30), 120))
    if kind in ("chaos_inject", "chaos_webhook"):
        # Two gates shared by both kinds, on top of the exploit-tier
        # engagement check at the end of _validate(): an independent
        # fail-closed opt-in (mirrors ZYVOR_EXPLOIT_EXECUTION_ENABLED's
        # pattern), and a per-run attestation -- distinct from the other
        # two because it's a confirmation THIS target consented to being
        # broken, not operator-level policy. Even client-side-only fault
        # injection against a target with no resilience has real impact.
        if os.environ.get("ZYVOR_CHAOS_INJECTION_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"}:
            raise ValueError(f"{kind} is disabled — set ZYVOR_CHAOS_INJECTION_ENABLED=true to enable it")
        if params.get("target_accepts_fault_injection") is not True:
            raise ValueError(
                "target_accepts_fault_injection must be true — an explicit, per-run "
                "confirmation that this specific target has consented to fault injection"
            )
        clean["target_accepts_fault_injection"] = True

        url = (params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = url[:500]
        clean["insecure"] = bool(params.get("insecure"))

    if kind == "port_scan":
        target = (params.get("url") or params.get("host") or "").strip()
        if not target:
            raise ValueError("provide a URL or hostname")
        # Accept bare host or URL — normalize to something engagement/policy can match.
        if "://" not in target:
            clean["url"] = f"https://{target[:240]}"
            clean["host"] = target[:240]
        else:
            if not target.startswith(("http://", "https://")):
                raise ValueError("url must start with http:// or https://")
            clean["url"] = target[:500]
        clean["timeout_s"] = max(0.2, min(float(params.get("timeout_s") or 1.0), 3.0))
        raw_ports = params.get("ports")
        if raw_ports:
            if isinstance(raw_ports, str):
                parts = [p.strip() for p in raw_ports.split(",") if p.strip()]
            elif isinstance(raw_ports, list):
                parts = raw_ports
            else:
                raise ValueError("ports must be a comma-separated string or list of ints")
            clean["ports"] = [max(1, min(int(p), 65535)) for p in parts][:64]
    if kind == "tls_cipher_scan":
        target = (params.get("url") or params.get("host") or "").strip()
        if not target:
            raise ValueError("provide a URL or hostname")
        if "://" not in target:
            clean["url"] = f"https://{target[:240]}"
        else:
            if not target.startswith(("http://", "https://")):
                raise ValueError("url must start with http:// or https://")
            clean["url"] = target[:500]
        if params.get("port") is not None:
            clean["port"] = max(1, min(int(params.get("port") or 443), 65535))
    if kind in DAST_KINDS:
        if os.environ.get("ZYVOR_DAST_SCAN_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"}:
            raise ValueError(f"{kind} is disabled — set ZYVOR_DAST_SCAN_ENABLED=true to enable it")
        url = (params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        clean["url"] = url[:500]
        clean["insecure"] = bool(params.get("insecure"))
    if kind in ("chaos_inject", "chaos_webhook"):
        control_kind = (params.get("control_kind") or "").strip()
        if control_kind not in _CHAOS_CONTROL_KINDS:
            raise ValueError(f"control_kind must be one of {sorted(_CHAOS_CONTROL_KINDS)}")
        clean["control_kind"] = control_kind
        control_params = dict(params.get("control_params") or {})
        control_params["url"] = url
        try:
            clean["control_params"] = _validate(control_kind, control_params)
        except ValueError as exc:
            raise ValueError(f"control_params invalid for control_kind {control_kind!r}: {exc}") from exc

        clean["error_rate_threshold_pct"] = max(0.0, min(float(params.get("error_rate_threshold_pct") or 10.0), 100.0))
        clean["recovery_sla_s"] = max(1.0, min(float(params.get("recovery_sla_s") or 30.0), 300.0))
    if kind == "chaos_inject":
        from agents.chaos.inject_script import FAULT_TYPES

        fault_type = (params.get("fault_type") or "").strip()
        if fault_type not in FAULT_TYPES:
            raise ValueError(f"fault_type must be one of {FAULT_TYPES}")
        clean["fault_type"] = fault_type
        # Hard-capped server-side, not relaxable via params -- blast radius
        # is real even for client-side-only fault injection.
        clean["latency_ms"] = max(0, min(int(params.get("latency_ms") or 200), 5000))
        clean["packet_loss_pct"] = max(0, min(int(params.get("packet_loss_pct") or 10), 100))
        clean["duration_s"] = max(5, min(int(params.get("duration_s") or 30), 120))
    if kind == "chaos_webhook":
        experiment_url = (params.get("experiment_webhook_url") or "").strip()
        if not experiment_url.startswith(("http://", "https://")):
            raise ValueError("experiment_webhook_url must start with http:// or https://")
        clean["experiment_webhook_url"] = experiment_url[:500]
        stop_url = (params.get("experiment_stop_webhook_url") or "").strip()
        clean["experiment_stop_webhook_url"] = stop_url[:500] if stop_url.startswith(("http://", "https://")) else ""
        clean["settle_s"] = max(1, min(int(params.get("settle_s") or 5), 60))
    if kind == "dast_scan":
        clean["max_requests"] = max(5, min(int(params.get("max_requests") or 40), 80))
        clean["timeout_s"] = max(30, min(int(params.get("timeout_s") or 120), 300))
        modules = params.get("modules")
        if modules:
            if isinstance(modules, str):
                modules = [m.strip() for m in modules.split(",") if m.strip()]
            allowed = {"headers", "injection", "csrf", "open_redirect", "nuclei"}
            clean["modules"] = [m for m in modules if m in allowed] or None
    if kind == "injection_scan":
        clean["max_requests"] = max(5, min(int(params.get("max_requests") or 40), 80))
    if kind == "ssrf_probe":
        param = (params.get("param") or "").strip()
        if param:
            clean["param"] = param[:64]
    if kind == "auth_attack_scan":
        login_url = (params.get("login_url") or "").strip()
        if login_url:
            if not login_url.startswith(("http://", "https://")):
                raise ValueError("login_url must start with http:// or https://")
            clean["login_url"] = login_url[:500]
    if kind == "idor_scan":
        clean["delta"] = max(1, min(int(params.get("delta") or 1), 5))
        cookie = (params.get("cookie") or "").strip()
        if cookie:
            clean["cookie"] = cookie[:4000]
        authorization = (params.get("authorization") or "").strip()
        if authorization:
            clean["authorization"] = authorization[:2000]
    if kind in PROBE_KINDS:
        target = (params.get("url") or params.get("host") or "").strip()
        if kind == "dns_records":
            if not target:
                raise ValueError("provide a hostname or URL")
            clean["host"] = target[:500]
        else:
            if not target.startswith(("http://", "https://")):
                raise ValueError("url must start with http:// or https://")
            clean["url"] = target[:500]
        if kind == "api_check":
            clean["expect_status"] = int(params.get("expect_status") or 200)
            clean["json_path"] = (params.get("json_path") or "").strip()[:100]
            clean["contains"] = (params.get("contains") or "").strip()[:100]
    # Enforce one target policy for every network-capable job. This final
    # pass also catches URL fields added by future job kinds.
    from orchestrator.security.target_policy import TargetPolicy
    policy = TargetPolicy.from_env()
    for key in ("url", "url_a", "url_b", "login_url", "protected", "logout_url", "ticket_url",
                "experiment_webhook_url", "experiment_stop_webhook_url"):
        value = clean.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            clean[key] = policy.validate_url(value)
    if isinstance(clean.get("urls"), list):
        clean["urls"] = [policy.validate_url(value) for value in clean["urls"]]
    spec_value = clean.get("spec")
    if isinstance(spec_value, str) and spec_value.startswith(("http://", "https://")):
        clean["spec"] = policy.validate_url(spec_value)
    for key in ("spec_a", "spec_b"):
        value = clean.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            clean[key] = policy.validate_url(value)
    if kind == "tls" and clean.get("host"):
        clean["host"] = policy.validate_host(clean["host"], int(clean.get("port") or 443))
    if kind == "host_pentest" and clean.get("host"):
        # SSRF-style guard: same private/loopback/link-local/metadata-IP
        # check every other network-capable job gets — credentials don't
        # exempt a target from the allowlist/private-range policy. Port
        # restriction is deliberately lifted (policy.allowed_ports defaults
        # to 80/443, which is meaningless for SSH — a bare validate_host()
        # call would wrongly reject the default port 22).
        import dataclasses

        ssh_policy = dataclasses.replace(policy, allowed_ports=())
        clean["host"] = ssh_policy.validate_host(clean["host"], int(clean.get("port") or 22))
        clean["url"] = clean["host"]
    if kind == "tls_cipher_scan" and clean.get("port") and clean.get("port") not in (80, 443):
        import dataclasses
        from urllib.parse import urlparse as _urlparse

        host = _urlparse(clean["url"]).hostname or ""
        if host:
            tls_policy = dataclasses.replace(policy, allowed_ports=())
            tls_policy.validate_host(host, int(clean["port"]))

    if kind in ELEVATED_RISK_KINDS:
        # sca_scan's local-checkout mode reads an operator-local filesystem
        # path, not a remote target -- no engagement makes sense when there's
        # nothing being attacked. Black-box mode (a `url` present) still
        # requires one, same as every other kind in this dict.
        if kind == "sca_scan" and not clean.get("url"):
            clean["engagement_id"] = None
        else:
            from orchestrator.security.engagement_policy import EngagementPolicy

            clean["engagement_id"] = params.get("engagement_id")
            # _validate() is a pure param-normalization function shared by every
            # trigger path (CLI, dashboard, /api/v2/jobs, scheduled jobs) and has
            # no requester identity in scope — the engagement-use audit row is
            # logged with an empty actor; who *authorized* the engagement is
            # already recorded on the engagement record itself.
            EngagementPolicy.from_env().require(
                target_url=clean.get("url", ""),
                min_tier=ELEVATED_RISK_KINDS[kind],  # type: ignore[arg-type]
                engagement_id=clean["engagement_id"],
            )

    return clean


def trigger(kind: str, params: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any]]:
    """Start a job unless one is already running. Returns (started, status)."""
    clean = _validate(kind, params or {})
    with _lock:
        if _state["running"]:
            return False, dict(_state)
        _cancel.clear()
        _progress.clear()
        _live_cases.clear()
        _state.update(
            running=True,
            kind=kind,
            params=clean,
            started_at=_now(),
            finished_at=None,
            result=None,
            error=None,
        )
    log_progress(f"▶ {kind} started")
    threading.Thread(target=_run, args=(kind, clean), daemon=True).start()
    return True, status()


def _brief(kind: str, result: Optional[dict[str, Any]], error: Optional[str]) -> str:
    if error:
        return error
    r = result or {}
    if "total" in r and r.get("total") is not None:
        return f"{r.get('passed', 0)}/{r.get('total', 0)} passed"
    if kind == "discover":
        return f"{r.get('inventory', 0)} candidates, {r.get('gaps_total', 0)} gaps"
    if "generated" in r:
        return f"{len(r['generated'])} test file(s) generated"
    if kind == "regression":
        return f"{len(r.get('diffs', []))} screenshot(s) compared"
    return "done"


def _run(kind: str, params: dict[str, Any]) -> None:
    import time as _time

    from orchestrator.dashboard import activity

    t0 = _time.time()
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    try:
        result = _JOBS[kind](params)
        log_progress(f"✅ {kind} finished: {_brief(kind, result, None)}")
    except JobCancelled:
        error = "cancelled by user"
        log_progress("⏹ job cancelled")
    except Exception as exc:  # surfaced in status, never crashes the server
        error = str(exc)
        log_progress(f"❌ {kind} failed: {error}")
    duration = _time.time() - t0
    with _lock:
        _state.update(running=False, finished_at=_now(), result=result, error=error)
    activity.record_job(kind, error is None, _brief(kind, result, error), duration)


# ── job implementations ──────────────────────────────────────────────


def _require_llm() -> None:
    from agents.parser.agent import _llm_available

    if not _llm_available():
        raise RuntimeError(
            "LLM not configured — set LLM_PROVIDER and the matching API key "
            "(e.g. OPENAI_API_KEY) in the environment/secret"
        )


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "test"


def _persist_artifacts(results: Any, kind: str) -> tuple[dict[str, str], dict[str, str]]:
    """Copy every test video + trace into the PVC-backed reports tree.
    Returns (title→video href, title→trace href)."""
    import shutil

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    videos: dict[str, str] = {}
    traces: dict[str, str] = {}
    for case in results.cases:
        for kind_name, path_attr, ext, sink in (
            ("videos", "video_path", "webm", videos),
            ("traces", "trace_path", "zip", traces),
        ):
            src_val = getattr(case, path_attr, None)
            if not src_val:
                continue
            src = Path(src_val)
            if not src.exists():
                continue
            rel_dir = f"artifacts/{kind_name}/{stamp}-{kind}"
            dest = _repo_root() / "reports" / rel_dir
            dest.mkdir(parents=True, exist_ok=True)
            name = f"{_slug(case.title)}.{ext}"
            try:
                shutil.copy2(src, dest / name)
            except OSError:
                continue
            sink[case.title] = f"/reports/{rel_dir}/{name}"
    if videos:
        log_progress(f"saved {len(videos)} test video(s)")
    if traces:
        log_progress(f"saved {len(traces)} trace(s)")
    # keep the libraries bounded: newest 20 run-directories each
    for kind_name in ("videos", "traces"):
        root = _repo_root() / "reports" / "artifacts" / kind_name
        if root.exists():
            for stale in sorted([d for d in root.iterdir() if d.is_dir()])[:-20]:
                shutil.rmtree(stale, ignore_errors=True)
    return videos, traces


# back-compat: some callers still expect the video-only helper
def _persist_videos(results: Any, kind: str) -> dict[str, str]:
    return _persist_artifacts(results, kind)[0]


def _explain_failure(error: str) -> str:
    """Heuristic likely-cause hint for a Playwright failure (no LLM needed)."""
    e = (error or "").lower()
    if not e:
        return ""
    if "timeout" in e and ("locator" in e or "waiting for" in e or "getby" in e):
        return "Element never appeared — selector may have changed, or the page loaded too slowly."
    if "timeout" in e and ("goto" in e or "navigation" in e):
        return "Navigation timed out — the target may be down, slow, or blocking the request."
    if "strict mode violation" in e or "resolved to" in e:
        return "Selector matched multiple elements — make it more specific (add a role or exact text)."
    if "tobevisible" in e or "not visible" in e:
        return "Element exists but isn't visible — it may be hidden, off-screen, or behind an overlay."
    if "expected" in e and "received" in e:
        return "Assertion mismatch — the actual text/value differs from what the test expects."
    if "net::" in e or "err_" in e or "econnrefused" in e:
        return "Network error reaching the target — check the URL, TLS, or that the service is up."
    if "no valid playwright spec" in e:
        return "The generated spec had a syntax error and was skipped — regenerate it."
    return "Review the trace or video to see the exact step that failed."


def _cases_payload(
    results: Any,
    limit: int = 60,
    videos: dict[str, str] | None = None,
    traces: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Full per-test detail for the result tables and downloadable reports."""
    payload = []
    for c in results.cases[:limit]:
        error = (c.error_message or "")[:1200] if c.status != "passed" else ""
        payload.append(
            {
                "title": c.title[:120],
                "status": c.status,
                "browser": c.browser,
                "duration_ms": round(c.duration_ms or 0),
                "error": error,
                "hint": _explain_failure(error) if error else "",
                "console_logs": [line for line in (c.console_logs or []) if line.startswith("[error]")][:8],
                "network_errors": (c.network_errors or [])[:8],
                "video": (videos or {}).get(c.title),
                "trace": (traces or {}).get(c.title),
            }
        )
    return payload


def _finalize(kind: str, results: Any, videos, traces, duration_s: float | None = None) -> dict[str, Any]:
    """Build the case list + a downloadable CSV/HTML/PDF report bundle."""
    cases = _cases_payload(results, videos=videos, traces=traces)
    try:
        from orchestrator.dashboard import history

        history.record_test_results(cases)
    except Exception:
        pass
    report: dict[str, str] = {}
    try:
        from agents.reporter.exports import build_report_bundle

        log_progress("building CSV / HTML / PDF report…")
        report = build_report_bundle(
            kind,
            cases,
            {
                "passed": results.passed,
                "failed": results.failed,
                "total": results.total,
                "duration_s": round(duration_s, 1) if duration_s is not None else None,
                "target": os.environ.get("ZYVOR_BASE_URL"),
            },
        )
    except Exception as exc:
        log_progress(f"report bundle failed: {str(exc)[:80]}")
    return {"cases": cases, "report": report}


def _report_href() -> Optional[str]:
    return "/reports/qa-summary.html" if (_repo_root() / "reports" / "qa-summary.html").is_file() else None


def _job_smoke(params: dict[str, Any]) -> dict[str, Any]:
    import time as _time

    from agents.common.models import PipelineReport
    from agents.execution.runner import run_playwright
    from agents.reporter.agent import generate_summary_stub
    from orchestrator.dashboard import history

    t0 = _time.time()
    base_url = os.environ.get("ZYVOR_BASE_URL", "https://zyvor.dev")
    grep = params.get("grep") or ""
    shard = params.get("shard") or ""
    extra = []
    if grep:
        extra.append(f"grep={grep}")
    if shard:
        extra.append(f"shard={shard}")
    log_progress(
        f"running tests/manual against {base_url} (Playwright + Chromium, video on"
        + (f", {', '.join(extra)}" if extra else "")
        + ")…"
    )
    with _env_overrides({"ZYVOR_VIDEO": "on"}):
        results = run_playwright(
            test_dirs=[str(_repo_root() / "tests" / "manual")],
            base_url=base_url,
            grep=grep or None,
            shard=shard or None,
            on_line=_stream_line,
        )
    _check_cancel()
    log_progress(f"execution done: {results.passed}/{results.total} passed")
    videos, traces = _persist_artifacts(results, "smoke")
    final = _finalize("smoke", results, videos, traces, duration_s=_time.time() - t0)
    report = PipelineReport(
        summary=generate_summary_stub(results),
        passed=results.passed,
        failed=results.failed,
        total=results.total,
    )
    history.append_run(report, source="dashboard-smoke", duration_s=_time.time() - t0)
    return {
        "passed": results.passed,
        "failed": results.failed,
        "total": results.total,
        **final,
    }


def _job_full(params: dict[str, Any]) -> dict[str, Any]:
    from orchestrator.cli import _initial_state
    from orchestrator.graph import get_compiled_graph

    state = _initial_state(
        source=params["source"],
        spec=params.get("spec"),
        pr_number=params.get("pr_number"),
        expand_coverage=params.get("expand_coverage", False),
        jira_issue_keys=params.get("jira_issue_keys"),
    )
    state["metadata"]["event"] = "dashboard-trigger"
    log_progress(f"full pipeline: fetch → parse → generate → execute → report (source={params['source']})")
    result = get_compiled_graph().invoke(state)  # report node appends history
    _check_cancel()
    tr = result.get("test_results")
    log_progress("pipeline graph finished")
    if result.get("error") and not tr:
        raise RuntimeError(result["error"])
    return {
        "passed": tr.passed if tr else 0,
        "failed": tr.failed if tr else 0,
        "total": tr.total if tr else 0,
        "generated": [Path(p).name for p in result.get("generated_tests", [])],
        **(_finalize("full", tr, {}, {}) if tr else {"cases": [], "report": {}}),
    }


def _generate_states(params: dict[str, Any]):
    """fetch → discover → gap_analyze → parse pipeline prefix, like the CLI."""
    from orchestrator.cli import _initial_state
    from orchestrator.nodes.discover import discover_coverage
    from orchestrator.nodes.fetch import fetch_requirements
    from orchestrator.nodes.gap_analyze import gap_analyze

    state = _initial_state(
        source=params["source"],
        spec=params.get("spec"),
        expand_coverage=params.get("expand_coverage", False),
        jira_issue_keys=params.get("jira_issue_keys"),
    )
    log_progress(f"fetching specs (source={params['source']})…")
    state = fetch_requirements(state)
    if state.get("error"):
        raise RuntimeError(state["error"])
    _check_cancel()
    log_progress("discovering coverage candidates…")
    state = discover_coverage(state)
    return gap_analyze(state)


def _job_generate(params: dict[str, Any]) -> dict[str, Any]:
    from orchestrator.nodes.generate import generate_tests
    from orchestrator.nodes.parse import parse_requirements

    state = _generate_states(params)
    _check_cancel()
    log_progress("parsing requirements…")
    state = parse_requirements(state)
    if state.get("error"):
        raise RuntimeError(state["error"])
    _check_cancel()
    log_progress(f"generating tests for {len(state.get('requirements', []))} requirement(s)…")
    state = generate_tests(state)
    metadata = state.get("metadata", {})
    return {
        "generated": [Path(p).name for p in state.get("generated_tests", [])],
        "requirements": len(state.get("requirements", [])),
        "coverage_candidates": metadata.get("coverage_inventory_size"),
        "coverage_gaps": metadata.get("coverage_gaps_remaining"),
        "quality_passed": metadata.get("quality_passed"),
        "quality_regenerated": metadata.get("quality_regenerated"),
    }


def _job_discover(params: dict[str, Any]) -> dict[str, Any]:
    params = {**params, "expand_coverage": True}
    state = _generate_states(params)
    gaps = state.get("coverage_gaps", [])
    return {
        "inventory": len(state.get("coverage_inventory", [])),
        "files_scanned": len(state.get("metadata", {}).get("discovered_paths", [])),
        "gaps": [
            {
                "kind": g.candidate.kind,
                "path": g.candidate.path,
                "title": g.candidate.title,
                "priority": g.candidate.priority,
            }
            for g in gaps[:50]
        ],
        "gaps_total": len(gaps),
    }


def _job_create(params: dict[str, Any]) -> dict[str, Any]:
    from agents.generator.agent import generate_tests_from_requirements
    from agents.nl_create.agent import (
        create_from_natural_language,
        create_from_natural_language_heuristic,
    )
    from agents.parser.agent import _llm_available, save_requirements

    root = _repo_root()
    mode = "llm"
    if _llm_available():
        log_progress("asking the LLM to turn the description into requirements…")
        try:
            parsed = create_from_natural_language(params["description"])
        except Exception as exc:
            log_progress(f"LLM failed ({str(exc)[:80]}) — falling back to heuristic parsing")
            parsed = create_from_natural_language_heuristic(params["description"])
            mode = "heuristic"
    else:
        log_progress("no LLM key configured — using heuristic parsing")
        parsed = create_from_natural_language_heuristic(params["description"])
        mode = "heuristic"

    save_requirements(parsed, root / "tests" / "fixtures" / "requirements.json")
    log_progress(f"generating Playwright test(s) from {len(parsed.requirements)} requirement(s)…")
    generated, _stats = generate_tests_from_requirements(
        parsed.requirements, root / "tests" / "generated"
    )
    result: dict[str, Any] = {"generated": [Path(p).name for p in generated], "mode": mode}

    if params.get("execute"):
        import time as _time

        from agents.common.models import PipelineReport
        from agents.execution.runner import run_playwright
        from agents.reporter.agent import generate_summary_stub
        from orchestrator.dashboard import history

        t0 = _time.time()
        log_progress(f"executing {len(generated)} generated test(s) (video on)…")
        with _env_overrides({"ZYVOR_VIDEO": "on"}):
            results = run_playwright(test_dirs=generated, on_line=_stream_line)
        _check_cancel()
        videos, traces = _persist_artifacts(results, "create")
        report = PipelineReport(
            summary=generate_summary_stub(results),
            passed=results.passed,
            failed=results.failed,
            total=results.total,
        )
        history.append_run(report, source="dashboard-create", duration_s=_time.time() - t0)
        result.update(
            passed=results.passed,
            failed=results.failed,
            total=results.total,
            **_finalize("create", results, videos, traces, duration_s=_time.time() - t0),
        )
    return result


def _job_regression(params: dict[str, Any]) -> dict[str, Any]:
    from agents.execution.runner import run_playwright
    from orchestrator.nodes.regression import regression_check

    saved = {k: os.environ.get(k) for k in ("ENABLE_REGRESSION", "UPDATE_BASELINES")}
    os.environ["ENABLE_REGRESSION"] = "true"
    os.environ["UPDATE_BASELINES"] = "true" if params.get("update_baselines") else "false"
    try:
        log_progress("running manual suite with screenshot capture…")
        results = run_playwright(
            test_dirs=[str(_repo_root() / "tests" / "manual")],
            base_url=os.environ.get("ZYVOR_BASE_URL", "https://zyvor.dev"),
            on_line=_stream_line,
        )
        _check_cancel()
        log_progress("comparing screenshots against baselines…")
        state = regression_check({"test_results": results})
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    diffs = [d.model_dump() for d in state.get("regression_diffs", [])]
    screenshots_root = _repo_root() / "screenshots"
    for d in diffs:
        path = d.get("diff_image_path")
        d["diff_href"] = None
        if path:
            try:
                d["diff_href"] = "/screenshots/" + str(Path(path).relative_to(screenshots_root))
            except ValueError:
                pass
    return {
        "passed": results.passed,
        "failed": results.failed,
        "diffs": diffs,
        "baselines_updated": params.get("update_baselines", False),
    }


def _env_overrides(overrides: dict[str, Optional[str]]):
    """Context manager: apply env overrides for a job, restore afterwards."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        saved = {k: os.environ.get(k) for k in overrides}
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    return _ctx()


def _job_crawl_test(params: dict[str, Any]) -> dict[str, Any]:
    """Point the agent at ANY site: crawl every reachable page, generate a test
    per page, run them all. Self-signed TLS and target login supported."""
    from agents.common.models import CoverageGap, PipelineReport
    from agents.coverage.gap import gaps_to_requirements
    from agents.discover.crawl import crawl_live_site
    from agents.execution.runner import run_playwright
    from agents.generator.agent import generate_tests_from_requirements
    from agents.reporter.agent import generate_summary_stub
    from orchestrator.dashboard import history

    url = params["url"]
    overrides: dict[str, Optional[str]] = {
        "ZYVOR_BASE_URL": url,
        "ENABLE_LIVE_CRAWL": "true",
        "CRAWL_MAX_PAGES": str(params["max_pages"]),
        "ZYVOR_IGNORE_HTTPS_ERRORS": "true" if params.get("insecure") else None,
        "ZYVOR_TEST_USER": params.get("username") or None,
        "ZYVOR_TEST_PASSWORD": params.get("password") or None,
        # target is arbitrary — don't treat it as the zyvor.dev marketing site
        "ENABLE_DASHBOARD_TESTS": "true" if params.get("username") else None,
    }

    import time as _time

    t0 = _time.time()
    with _env_overrides(overrides):
        log_progress(f"crawling {url} (max {params['max_pages']} pages, BFS)…")
        candidates = crawl_live_site(url)
        if not candidates:
            raise RuntimeError(f"crawl found no reachable pages at {url}")
        _check_cancel()
        log_progress(f"found {len(candidates)} page(s): " + ", ".join(c.path for c in candidates[:8]) + ("…" if len(candidates) > 8 else ""))

        requirements = gaps_to_requirements([CoverageGap(candidate=c) for c in candidates])
        output_dir = _repo_root() / "tests" / "generated"
        output_dir.mkdir(parents=True, exist_ok=True)
        log_progress(f"generating {len(requirements)} validation test(s)…")
        generated, _stats = generate_tests_from_requirements(
            requirements, output_dir, coverage_mode=True
        )
        _check_cancel()
        log_progress(f"executing {len(generated)} test(s) with Playwright (video on)…")
        with _env_overrides({"ZYVOR_VIDEO": "on"}):
            results = run_playwright(test_dirs=generated, base_url=url, on_line=_stream_line)
        _check_cancel()
        log_progress(f"execution done: {results.passed}/{results.total} passed")
        videos, traces = _persist_artifacts(results, "crawl")

    report = PipelineReport(
        summary=f"Crawl of {url}: {results.passed}/{results.total} pages passed. "
        + generate_summary_stub(results),
        passed=results.passed,
        failed=results.failed,
        total=results.total,
    )
    history.append_run(report, source="dashboard-crawl", duration_s=_time.time() - t0)
    return {
        "url": url,
        "pages_found": len(candidates),
        "generated": [Path(p).name for p in generated],
        "passed": results.passed,
        "failed": results.failed,
        "total": results.total,
        **_finalize("crawl", results, videos, traces),
    }


def _job_audit(params: dict[str, Any]) -> dict[str, Any]:
    """Crawl a site and run per-page QA checks (a11y / links / SEO / perf / …)."""
    import time as _time

    from agents.audit.engine import run_audit
    from agents.common.models import PipelineReport
    from agents.reporter.exports import build_audit_bundle
    from orchestrator.dashboard import history

    t0 = _time.time()
    url = params["url"]
    checks = params["checks"]
    log_progress(f"auditing {url} — checks: {', '.join(checks)} (max {params['max_pages']} pages)")
    data = run_audit(
        url,
        checks,
        max_pages=params["max_pages"],
        insecure=params.get("insecure", False),
        username=params.get("username", ""),
        password=params.get("password", ""),
        on_line=_stream_line_audit,
    )
    _check_cancel()
    pages = data.get("pages", [])
    summary = data.get("summary", {})
    by_check = summary.get("byCheck", {})
    total_fail = sum(v.get("fail", 0) for v in by_check.values())
    total_warn = sum(v.get("warn", 0) for v in by_check.values())
    log_progress(f"audit done: {len(pages)} pages, {total_fail} failing checks, {total_warn} warnings")

    # aggregate a health grade (A–F) — weighted pass ratio across all checks
    weights = {"a11y": 3, "console": 2, "links": 2, "seo": 1, "perf": 1, "headers": 1, "responsive": 1}
    earned = possible = 0.0
    for check, counts in by_check.items():
        w = weights.get(check, 1)
        n = sum(counts.values())
        if not n:
            continue
        possible += w * n
        earned += w * (counts.get("ok", 0) + 0.5 * counts.get("warn", 0))
    score = round(100 * earned / possible) if possible else 100
    grade = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 60 else "F"
    log_progress(f"health grade: {grade} ({score}/100)")

    raised = _auto_findings("audit", url, {"pages": pages})
    report = build_audit_bundle(url, checks, pages, summary, findings=raised)

    hist = PipelineReport(
        summary=f"Audit of {url}: {len(pages)} pages, {total_fail} failing / {total_warn} warning checks",
        passed=sum(v.get("ok", 0) for v in by_check.values()),
        failed=total_fail,
        total=sum(sum(v.values()) for v in by_check.values()),
    )
    history.append_run(hist, source="dashboard-audit", duration_s=_time.time() - t0)
    return {
        "url": url,
        "checks": checks,
        "pages_audited": len(pages),
        "by_check": by_check,
        "fail_count": total_fail,
        "warn_count": total_warn,
        "grade": grade,
        "score": score,
        "findings": raised,
        "audit_pages": pages,
        "report": report,
    }


def _job_screenshot(params: dict[str, Any]) -> dict[str, Any]:
    """Capture on-demand full-page screenshots of any URL at chosen viewports."""
    import json as _json
    import subprocess

    url = params["url"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rel_dir = f"artifacts/shots/{stamp}"
    out_dir = _repo_root() / "reports" / rel_dir
    env = {**os.environ, "ZYVOR_BASE_URL": url}
    if params.get("insecure"):
        env["ZYVOR_IGNORE_HTTPS_ERRORS"] = "true"

    log_progress(f"capturing {url} at {', '.join(params['viewports'])}…")
    script = _repo_root() / "playwright" / "scripts" / "shot-url.mjs"
    cmd = [
        "node", str(script), url, str(out_dir),
        ",".join(params["viewports"]),
        "full" if params.get("full_page") else "",
    ]
    proc = subprocess.run(cmd, cwd=_repo_root(), env=env, capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"screenshot failed: {(proc.stderr or '')[:200]}")
    data = _json.loads(proc.stdout)

    # prune older shot dirs
    root = _repo_root() / "reports" / "artifacts" / "shots"
    if root.exists():
        import shutil

        for stale in sorted([d for d in root.iterdir() if d.is_dir()])[:-20]:
            shutil.rmtree(stale, ignore_errors=True)

    shots = [
        {**s, "href": f"/reports/{rel_dir}/{s['file']}"}
        for s in data.get("shots", [])
    ]
    log_progress(f"captured {len(shots)} screenshot(s)")
    return {"url": url, "status": data.get("status"), "title": data.get("title"), "shots": shots}


def _job_flaky(params: dict[str, Any]) -> dict[str, Any]:
    """Run a suite N times and report which tests flake (mixed pass/fail)."""
    import time as _time

    from agents.common.models import PipelineReport
    from agents.execution.runner import run_playwright
    from orchestrator.dashboard import history

    t0 = _time.time()
    runs = params["runs"]
    target = params["target"]
    test_dirs = [str(_repo_root() / "tests" / "manual")] if target == "manual" else [target]
    base_url = os.environ.get("ZYVOR_BASE_URL", "https://zyvor.dev")

    tally: dict[str, dict[str, int]] = {}
    for i in range(runs):
        _check_cancel()
        log_progress(f"flaky run {i + 1}/{runs}…")
        results = run_playwright(test_dirs=test_dirs, base_url=base_url, on_line=_stream_line)
        for c in results.cases:
            rec = tally.setdefault(c.title[:120], {"pass": 0, "fail": 0})
            rec["pass" if c.status == "passed" else "fail"] += 1
        try:
            history.record_test_results(
                [{"title": c.title[:120], "status": c.status} for c in results.cases]
            )
        except Exception:
            pass

    cases = []
    flaky_count = 0
    for title, rec in sorted(tally.items(), key=lambda kv: -(kv[1]["fail"])):
        total = rec["pass"] + rec["fail"]
        is_flaky = rec["pass"] > 0 and rec["fail"] > 0
        if is_flaky:
            flaky_count += 1
        cases.append(
            {
                "title": title,
                "status": "flaky" if is_flaky else ("passed" if rec["fail"] == 0 else "failed"),
                "passes": rec["pass"],
                "runs": total,
                "flake_pct": round(100 * rec["fail"] / total) if total else 0,
            }
        )

    log_progress(f"flaky check done: {flaky_count} flaky test(s) over {runs} runs")
    hist = PipelineReport(
        summary=f"Flaky check ({runs} runs): {flaky_count} flaky of {len(cases)} tests",
        passed=sum(1 for c in cases if c["status"] == "passed"),
        failed=flaky_count,
        total=len(cases),
    )
    history.append_run(hist, source="dashboard-flaky", duration_s=_time.time() - t0)
    return {"runs": runs, "flaky_count": flaky_count, "flaky_cases": cases}


def _capture_one(url: str, out_dir: Path, tag: str, insecure: bool) -> Optional[Path]:
    import json as _json
    import subprocess

    env = {**os.environ, "ZYVOR_BASE_URL": url}
    if insecure:
        env["ZYVOR_IGNORE_HTTPS_ERRORS"] = "true"
    script = _repo_root() / "playwright" / "scripts" / "shot-url.mjs"
    proc = subprocess.run(
        ["node", str(script), url, str(out_dir), "desktop", "full"],
        cwd=_repo_root(), env=env, capture_output=True, text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    data = _json.loads(proc.stdout)
    shots = data.get("shots", [])
    return (out_dir / shots[0]["file"]) if shots else None


def _job_compare(params: dict[str, Any]) -> dict[str, Any]:
    """Visual-diff two URLs (e.g. staging vs prod) at desktop, full page."""
    import shutil

    from agents.regression.compare_screenshots import _diff_percent

    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("Pillow required for compare")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rel_dir = f"artifacts/compare/{stamp}"
    out_dir = _repo_root() / "reports" / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    log_progress(f"capturing A: {params['url_a']}")
    a = _capture_one(params["url_a"], out_dir, "a", params.get("insecure", False))
    _check_cancel()
    log_progress(f"capturing B: {params['url_b']}")
    b = _capture_one(params["url_b"], out_dir, "b", params.get("insecure", False))
    if not a or not b:
        raise RuntimeError("failed to capture one or both URLs")

    img_a = Image.open(a).convert("RGB")
    img_b = Image.open(b).convert("RGB")
    if img_a.size != img_b.size:
        img_b = img_b.resize(img_a.size)
    from PIL import ImageChops

    diff_img = ImageChops.difference(img_a, img_b)
    diff_path = out_dir / "diff.png"
    diff_img.save(diff_path)
    pct = round(_diff_percent(img_a, img_b), 3)
    log_progress(f"visual difference: {pct}%")

    # prune
    root = _repo_root() / "reports" / "artifacts" / "compare"
    if root.exists():
        for stale in sorted([d for d in root.iterdir() if d.is_dir()])[:-20]:
            shutil.rmtree(stale, ignore_errors=True)

    base = f"/reports/{rel_dir}"
    return {
        "url_a": params["url_a"], "url_b": params["url_b"],
        "diff_percent": pct,
        "identical": pct < 0.1,
        "a_href": f"{base}/{a.name}", "b_href": f"{base}/{b.name}", "diff_href": f"{base}/diff.png",
    }


def _job_ping(params: dict[str, Any]) -> dict[str, Any]:
    """HTTP status + latency check across a list of URLs."""
    import time as _time

    import httpx

    results = []
    up = 0
    with httpx.Client(timeout=15, follow_redirects=True, verify=not params.get("insecure", False)) as client:
        for url in params["urls"]:
            _check_cancel()
            t0 = _time.time()
            try:
                resp = client.get(url)
                ms = round((_time.time() - t0) * 1000)
                ok = resp.status_code < 400
                up += 1 if ok else 0
                results.append({"url": url, "status": resp.status_code, "ms": ms, "ok": ok})
                log_progress(f"{resp.status_code} {url} ({ms}ms)")
            except Exception as exc:
                results.append({"url": url, "status": 0, "ms": None, "ok": False, "error": str(exc)[:120]})
                log_progress(f"ERR {url}: {str(exc)[:80]}")
    return {"total": len(results), "up": up, "down": len(results) - up, "results": results}


def _job_loadtest(params: dict[str, Any]) -> dict[str, Any]:
    """Fire N requests at a URL with C workers; report latency percentiles."""
    import concurrent.futures
    import time as _time

    import httpx

    url = params["url"]
    n, conc = params["requests"], params["concurrency"]
    log_progress(f"load test: {n} requests, {conc} concurrent → {url}")
    latencies: list[float] = []
    ok = 0
    codes: dict[int, int] = {}
    lock = threading.Lock()
    t_start = _time.time()

    insecure = bool(params.get("insecure", False))

    def _one(_i: int) -> None:
        nonlocal ok
        with httpx.Client(timeout=20, verify=not insecure, follow_redirects=True) as client:
            t0 = _time.time()
            try:
                resp = client.get(url)
                ms = (_time.time() - t0) * 1000
                with lock:
                    latencies.append(ms)
                    codes[resp.status_code] = codes.get(resp.status_code, 0) + 1
                    if resp.status_code < 400:
                        ok += 1
            except Exception:
                with lock:
                    codes[0] = codes.get(0, 0) + 1

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=conc) as pool:
        futures = [pool.submit(_one, i) for i in range(n)]
        for _ in concurrent.futures.as_completed(futures):
            _check_cancel()
            done += 1
            if done % max(1, n // 5) == 0:
                log_progress(f"  {done}/{n} done")
    elapsed = _time.time() - t_start

    def _pct(p: float) -> float:
        if not latencies:
            return 0.0
        s = sorted(latencies)
        return round(s[min(len(s) - 1, int(p / 100 * len(s)))], 1)

    return {
        "url": url, "requests": n, "concurrency": conc,
        "success": ok, "success_pct": round(100 * ok / n) if n else 0,
        "rps": round(n / elapsed, 1) if elapsed else 0,
        "p50": _pct(50), "p95": _pct(95), "p99": _pct(99),
        "min": round(min(latencies), 1) if latencies else 0,
        "max": round(max(latencies), 1) if latencies else 0,
        "mean": round(sum(latencies) / len(latencies), 1) if latencies else 0,
        "codes": codes,
    }


def _job_tls(params: dict[str, Any]) -> dict[str, Any]:
    """DNS + TLS certificate inspection: issuer, expiry, protocol, SANs."""
    import socket
    import ssl
    from datetime import datetime as _dt

    host, port = params["host"], params["port"]
    log_progress(f"resolving {host}…")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        ips = sorted({str(i[4][0]) for i in infos})
    except Exception as exc:
        raise RuntimeError(f"DNS resolution failed: {exc}")
    log_progress(f"{host} → {', '.join(ips)}")

    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    log_progress(f"TLS handshake with {host}:{port}…")
    with socket.create_connection((host, port), timeout=15) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            cert = ssock.getpeercert()
            proto = ssock.version()
            cipher = ssock.cipher()

    assert cert is not None, "getpeercert() only returns None before the handshake completes"

    def _name(field: Any) -> str:
        return ", ".join("=".join(x) for rdn in (field or ()) for x in rdn)

    not_after_raw = cert.get("notAfter")
    not_after = not_after_raw if isinstance(not_after_raw, str) else None
    expiry = _dt.strptime(not_after, "%b %d %H:%M:%S %Y %Z") if not_after else None
    days_left = (expiry - _dt.utcnow()).days if expiry else None
    san_raw = cert.get("subjectAltName")
    sans = [v for k, v in san_raw if k == "DNS"] if isinstance(san_raw, tuple) else []

    status = "ok"
    if days_left is not None:
        if days_left < 0:
            status = "fail"
        elif days_left < 30:
            status = "warn"
    log_progress(f"cert valid {days_left} more day(s) · {proto}")

    return {
        "host": host, "port": port, "ips": ips,
        "issuer": _name(cert.get("issuer")), "subject": _name(cert.get("subject")),
        "expiry": not_after, "days_left": days_left,
        "protocol": proto, "cipher": cipher[0] if cipher else "",
        "sans": sans[:20], "status": status,
    }


def _job_flow(params: dict[str, Any]) -> dict[str, Any]:
    """Drive a multi-step user journey and record the whole thing as one video."""
    import time as _time

    from agents.common.models import PipelineReport
    from agents.flow.engine import run_flow
    from agents.flow.parse import parse_flow
    from orchestrator.dashboard import history

    t0 = _time.time()
    url = params["url"]
    log_progress("planning the journey…")
    steps, mode = parse_flow(params["description"], steps_mode=params.get("steps_mode", False))
    log_progress(f"{len(steps)} step(s) parsed ({mode})")
    _check_cancel()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rel_dir = f"artifacts/flows/{stamp}"
    out_dir = _repo_root() / "reports" / rel_dir
    session_path = ""
    if params.get("session"):
        sp = _repo_root() / "reports" / "artifacts" / "auth" / params["session"]
        if sp.exists():
            session_path = str(sp)
            log_progress(f"reusing saved session {params['session']}")
    log_progress(f"running journey against {url} (video on)…")
    flow_env: dict[str, Optional[str]] = {"ZYVOR_NO_SANDBOX": os.environ.get("ZYVOR_NO_SANDBOX", "false")}
    if params.get("browser"):
        flow_env["ZYVOR_BROWSER"] = params["browser"]
    if params.get("device"):
        flow_env["ZYVOR_DEVICE"] = params["device"]
    if params.get("throttle"):
        flow_env["ZYVOR_THROTTLE"] = params["throttle"]
    with _env_overrides(flow_env):
        data = run_flow(
            url, steps, out_dir,
            insecure=params.get("insecure", False),
            record=params.get("record", True),
            username=params.get("username", ""),
            password=params.get("password", ""),
            session=session_path,
            on_line=_stream_line_flow,
            stop_on_fail=False,
        )
    _check_cancel()

    base = f"/reports/{rel_dir}"
    result_steps = []
    for s in data.get("steps", []):
        result_steps.append({
            "n": s["n"], "action": s["action"], "desc": s["desc"],
            "status": s["status"], "error": s.get("error", ""),
            "hint": _explain_failure(s.get("error", "")) if s["status"] != "passed" else "",
            "shot": f"{base}/{s['shot']}" if s.get("shot") else None,
        })
    video = f"{base}/{data['video']}" if data.get("video") else None
    trace = f"{base}/{data['trace']}" if data.get("trace") else None
    passed, failed, total = data.get("passed", 0), data.get("failed", 0), data.get("total", 0)
    log_progress(
        f"journey done: {passed}/{total} steps passed"
        + (" · video saved" if video else "")
        + (" · trace saved" if trace else "")
    )

    # prune old flow dirs
    root = _repo_root() / "reports" / "artifacts" / "flows"
    if root.exists():
        import shutil
        for stale in sorted([d for d in root.iterdir() if d.is_dir()])[:-20]:
            shutil.rmtree(stale, ignore_errors=True)

    report = _flow_report_bundle(url, result_steps, {"passed": passed, "failed": failed, "total": total, "video": video})
    hist = PipelineReport(
        summary=f"Flow of {url}: {passed}/{total} steps passed",
        passed=passed, failed=failed, total=total,
    )
    history.append_run(hist, source="dashboard-flow", duration_s=_time.time() - t0)
    return {
        "url": url, "mode": mode, "passed": passed, "failed": failed, "total": total,
        "flow_steps": result_steps, "video": video, "trace": trace, "report": report,
        "browser": data.get("browser"), "device": data.get("device"), "throttle": data.get("throttle"),
    }


def _flow_report_bundle(url: str, steps: list, summary: dict) -> dict[str, str]:
    try:
        from agents.reporter.exports import build_flow_bundle

        return build_flow_bundle(url, steps, summary)
    except Exception as exc:
        log_progress(f"report bundle failed: {str(exc)[:80]}")
        return {}


def _job_ai_flow(params: dict[str, Any]) -> dict[str, Any]:
    """Autonomous AI tester — drive the browser toward a plain-English goal."""
    import time as _time

    from agents.aiflow.engine import run_ai_flow
    from agents.common.models import PipelineReport
    from orchestrator.dashboard import history

    t0 = _time.time()
    url, goal = params["url"], params["goal"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rel_dir = f"artifacts/ai/{stamp}"
    out_dir = _repo_root() / "reports" / rel_dir

    session_path = ""
    if params.get("session"):
        sp = _repo_root() / "reports" / "artifacts" / "auth" / params["session"]
        if sp.exists():
            session_path = str(sp)
            log_progress(f"reusing saved session {params['session']}")
    log_progress(f"AI agent goal: {goal}")
    with _env_overrides({"ZYVOR_NO_SANDBOX": os.environ.get("ZYVOR_NO_SANDBOX", "false")}):
        data = run_ai_flow(
            url, goal, out_dir,
            insecure=params.get("insecure", False),
            session=session_path,
            max_steps=params.get("max_steps", 20),
            on_line=_stream_line_generic,
        )
    _check_cancel()

    base = f"/reports/{rel_dir}"
    steps = []
    for s in data.get("steps", []):
        steps.append({
            "n": s.get("n"), "action": s.get("action"), "desc": s.get("desc") or s.get("action"),
            "status": "passed" if s.get("status") == "ok" else "failed",
            "error": s.get("error", ""), "reason": s.get("reason", ""),
            "shot": f"{base}/{s['shot']}" if s.get("shot") else None,
        })
    video = f"{base}/{data['video']}" if data.get("video") else None
    trace = f"{base}/{data['trace']}" if data.get("trace") else None
    passed_flag = bool(data.get("passed"))
    ok = sum(1 for s in steps if s["status"] == "passed")
    total = len(steps)
    log_progress(f"AI agent {'succeeded' if passed_flag else 'stopped'}: {data.get('done_summary', '')}")

    root = _repo_root() / "reports" / "artifacts" / "ai"
    if root.exists():
        import shutil
        for stale in sorted([d for d in root.iterdir() if d.is_dir()])[:-20]:
            shutil.rmtree(stale, ignore_errors=True)

    report = _flow_report_bundle(url, steps, {"passed": ok, "failed": total - ok, "total": total, "video": video})
    if not passed_flag:
        from orchestrator.dashboard import findings
        findings.add("ai_flow", "high", f"AI goal not achieved: {goal}",
                     data.get("done_summary", ""), url, "goal")
    hist = PipelineReport(summary=f"AI flow {url}: {data.get('done_summary', '')[:80]}",
                          passed=ok, failed=total - ok, total=total)
    history.append_run(hist, source="dashboard-ai", duration_s=_time.time() - t0)
    return {
        "url": url, "goal": goal, "mode": data.get("mode"), "success": passed_flag,
        "summary": data.get("done_summary", ""), "ai_steps": steps,
        "video": video, "trace": trace, "report": report,
    }


def _job_api_contract(params: dict[str, Any]) -> dict[str, Any]:
    """Validate REST endpoints against their OpenAPI schema, or run an API workflow."""
    import time as _time

    from agents.apitest.engine import run_api_contract
    from agents.common.models import PipelineReport
    from orchestrator.dashboard import history

    t0 = _time.time()
    url = params["url"]
    mode = params["mode"]
    log_progress(f"api contract ({mode}) against {url}…")
    data = run_api_contract(
        url,
        spec=params.get("spec"),
        mode=mode,
        workflow=params.get("workflow"),
        auth=params.get("auth"),
        include_writes=params.get("include_writes", False),
        insecure=params.get("insecure", False),
        path_params=params.get("path_params"),
        max_endpoints=params.get("max_endpoints", 60),
        on_line=_stream_line_generic,
    )
    _check_cancel()
    passed, failed, total = data.get("passed", 0), data.get("failed", 0), data.get("total", 0)
    rows = data.get("endpoints") if mode == "spec" else data.get("steps")
    log_progress(f"api contract done: {passed}/{total} checks passed")

    report = _api_contract_report_bundle(url, mode, rows or [], {"passed": passed, "failed": failed, "total": total})
    hist = PipelineReport(summary=f"API contract {url}: {passed}/{total}", passed=passed, failed=failed, total=total)
    history.append_run(hist, source="dashboard-api", duration_s=_time.time() - t0)
    result = {
        "url": url, "mode": mode, "passed": passed, "failed": failed, "total": total,
        "endpoints": data.get("endpoints"), "steps": data.get("steps"), "report": report,
    }
    _auto_findings("api_contract", url, result)
    return result


def _spec_ref_label(ref: Any) -> str:
    if isinstance(ref, dict):
        return "inline"
    text = str(ref)
    return text if len(text) <= 60 else text[:57] + "..."


def _job_api_contract_diff(params: dict[str, Any]) -> dict[str, Any]:
    """Static OpenAPI breaking-change diff between two spec references
    (inline object, http(s) URL, or 'git:<ref>:<path>'). No live target
    interaction, so this kind is not gated by a security engagement --
    same class as import_codegen."""
    from agents.contract_diff.engine import BREAKING, diff_specs
    from agents.contract_diff.loader import load_spec

    spec_a_ref, spec_b_ref = params["spec_a"], params["spec_b"]
    log_progress(f"api_contract_diff: loading spec_a ({_spec_ref_label(spec_a_ref)})")
    spec_a = load_spec(spec_a_ref, insecure=params.get("insecure", False))
    log_progress(f"api_contract_diff: loading spec_b ({_spec_ref_label(spec_b_ref)})")
    spec_b = load_spec(spec_b_ref, insecure=params.get("insecure", False))
    _check_cancel()

    changes = diff_specs(spec_a, spec_b)
    breaking = [c for c in changes if c["classification"] == BREAKING]
    log_progress(f"api_contract_diff: {len(changes)} change(s), {len(breaking)} breaking")

    fail_on = params.get("fail_on", "breaking")
    passed = not breaking if fail_on == "breaking" else not changes

    label = f"{_spec_ref_label(spec_a_ref)} vs {_spec_ref_label(spec_b_ref)}"
    result = {
        "spec_a": _spec_ref_label(spec_a_ref), "spec_b": _spec_ref_label(spec_b_ref),
        "changes": changes, "breaking_count": len(breaking), "total_count": len(changes), "passed": passed,
    }
    _auto_findings("api_contract_diff", label, result)
    return result


def _job_contract_verify(params: dict[str, Any]) -> dict[str, Any]:
    """Consumer-driven contract verification, HAR-derived -- not Pact (see
    agents/contract_verify/engine.py's module docstring and ROADMAP.md).
    Derives per-endpoint expectations from a recorded HAR, replays each
    against the live provider, diffs status/content-type/top-level JSON
    key shape. Read-only against the target -- gated at active_recon tier."""
    import json as _json

    from agents.contract_verify.engine import derive_expectations, verify_expectations

    url = params["url"]
    har_path = params["har"] or os.environ.get("ZYVOR_HAR_PATH") or ""
    if har_path and not Path(har_path).is_absolute():
        har_path = str(_repo_root() / har_path)
    log_progress(f"contract_verify: reading {har_path}")
    try:
        with open(har_path, encoding="utf-8") as fh:
            har = _json.load(fh)
    except (OSError, _json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read HAR {har_path}: {exc}") from exc

    expectations = derive_expectations(har, max_endpoints=params.get("max_endpoints", 60))
    log_progress(f"contract_verify: derived {len(expectations)} expectation(s) from the HAR, "
                 f"replaying against {url}")
    _check_cancel()
    checks = verify_expectations(url, expectations, insecure=params.get("insecure", False))
    passed = sum(1 for c in checks if c["ok"])
    failed = len(checks) - passed
    log_progress(f"contract_verify done: {passed}/{len(checks)} checks passed")

    result = {"url": url, "har": har_path, "checks": checks, "passed": passed, "failed": failed, "total": len(checks)}
    _auto_findings("contract_verify", url, result)
    return result


def _api_contract_report_bundle(url: str, mode: str, rows: list, summary: dict) -> dict[str, str]:
    try:
        from agents.reporter.exports import build_api_contract_bundle

        return build_api_contract_bundle(url, mode, rows, summary)
    except Exception as exc:
        log_progress(f"report bundle failed: {str(exc)[:80]}")
        return {}


def _job_vitals(params: dict[str, Any]) -> dict[str, Any]:
    """Measure Core Web Vitals (LCP/CLS/INP/FCP/TTFB) and grade them."""
    import json as _json
    import subprocess
    import time as _time

    from agents.common.models import PipelineReport
    from orchestrator.dashboard import history

    t0 = _time.time()
    url = params["url"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rel_dir = f"artifacts/vitals/{stamp}"
    out_dir = _repo_root() / "reports" / rel_dir
    script = _repo_root() / "playwright" / "scripts" / "vitals.mjs"

    env = {**os.environ, "ZYVOR_NO_SANDBOX": os.environ.get("ZYVOR_NO_SANDBOX", "false")}
    if params.get("insecure"):
        env["ZYVOR_IGNORE_HTTPS_ERRORS"] = "true"
    if params.get("device"):
        env["ZYVOR_DEVICE"] = params["device"]
    if params.get("throttle"):
        env["ZYVOR_THROTTLE"] = params["throttle"]
    log_progress(f"measuring Core Web Vitals for {url}…")
    proc = subprocess.run(
        ["node", str(script), url, str(out_dir)],
        cwd=_repo_root(), env=env, capture_output=True, text=True,
    )
    for line in (proc.stderr or "").splitlines():
        if line.strip():
            log_progress(line.strip())
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"vitals failed: {(proc.stderr or '')[:200]}")
    data = _json.loads(proc.stdout)
    _check_cancel()

    metrics = data.get("metrics", {})
    if data.get("shot"):
        data["shot_url"] = f"/reports/{rel_dir}/{data['shot']}"
    passed = sum(1 for m in metrics.values() if m.get("grade") == "good")
    total = len(metrics)
    log_progress(f"vitals: {data.get('overall', '?')} ({passed}/{total} good)")

    # prune
    root = _repo_root() / "reports" / "artifacts" / "vitals"
    if root.exists():
        import shutil
        for stale in sorted([d for d in root.iterdir() if d.is_dir()])[:-20]:
            shutil.rmtree(stale, ignore_errors=True)

    report = _vitals_report_bundle(url, data)
    hist = PipelineReport(summary=f"Web Vitals {url}: {data.get('overall')}", passed=passed, failed=total - passed, total=total)
    history.append_run(hist, source="dashboard-vitals", duration_s=_time.time() - t0)
    data["report"] = report
    _auto_findings("vitals", url, data)
    return data


def _job_auth_test(params: dict[str, Any]) -> dict[str, Any]:
    """Log in, capture a reusable session, and assert auth/session behaviour."""
    import json as _json
    import re as _re
    import subprocess
    import tempfile
    import time as _time
    from urllib.parse import urlparse

    from agents.common.models import PipelineReport
    from orchestrator.dashboard import history

    t0 = _time.time()
    url = params["url"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rel_dir = f"artifacts/auth/{stamp}"
    out_dir = _repo_root() / "reports" / rel_dir
    script = _repo_root() / "playwright" / "scripts" / "auth-probe.mjs"

    slug = _re.sub(r"[^a-z0-9]+", "-", urlparse(url).netloc.lower()).strip("-") or "session"
    session_out = _repo_root() / "reports" / "artifacts" / "auth" / f"{slug}.json"

    cfg = {
        "base": url,
        "login_url": params.get("login_url", ""),
        "api_login": params.get("api_login", ""),
        "protected": params.get("protected", "/"),
        "logout_url": params.get("logout_url", ""),
        "username": params.get("username", ""),
        "password": params.get("password", ""),
        "token_path": params.get("token_path", "token"),
        "save_session": params.get("save_session", True),
        "session_out": str(session_out),
        "insecure": params.get("insecure", False),
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        _json.dump(cfg, fh)
        cfg_file = fh.name

    env = {**os.environ, "ZYVOR_NO_SANDBOX": os.environ.get("ZYVOR_NO_SANDBOX", "false")}
    if params.get("insecure"):
        env["ZYVOR_IGNORE_HTTPS_ERRORS"] = "true"
    log_progress(f"auth & session test on {url}…")
    proc = subprocess.run(["node", str(script), cfg_file, str(out_dir)], cwd=_repo_root(), env=env, capture_output=True, text=True)
    try:
        os.unlink(cfg_file)
    except OSError:
        pass
    for line in (proc.stderr or "").splitlines():
        if line.strip():
            _stream_line_generic(line.strip())
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"auth probe failed: {(proc.stderr or '')[:200]}")
    data = _json.loads(proc.stdout)
    _check_cancel()

    if data.get("shot"):
        data["shot_url"] = f"/reports/{rel_dir}/{data['shot']}"
    if data.get("session_file"):
        data["session_name"] = Path(data["session_file"]).name  # reuse via `session` on flow/realtime
    passed, failed, total = data.get("passed", 0), data.get("failed", 0), data.get("total", 0)
    log_progress(f"auth test done: {passed}/{total} checks passed"
                 + (f" · session saved as {data.get('session_name')}" if data.get("session_name") else ""))

    report = _auth_report_bundle(url, data)
    hist = PipelineReport(summary=f"Auth {url}: {passed}/{total} checks", passed=passed, failed=failed, total=total)
    history.append_run(hist, source="dashboard-auth", duration_s=_time.time() - t0)
    data["report"] = report
    _auto_findings("auth_test", url, data)
    return data


def _auth_report_bundle(url: str, data: dict) -> dict[str, str]:
    try:
        from agents.reporter.exports import build_checks_bundle

        return build_checks_bundle(url, data, kind="auth", title="Auth & session")
    except Exception as exc:
        log_progress(f"report bundle failed: {str(exc)[:80]}")
        return {}


def _job_har_replay(params: dict[str, Any]) -> dict[str, Any]:
    """Record or replay a HAR against a live URL."""
    import json as _json
    import subprocess
    import tempfile
    import time as _time

    from agents.common.models import PipelineReport
    from orchestrator.dashboard import history

    t0 = _time.time()
    url = params["url"]
    mode = params["mode"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rel_dir = f"artifacts/har/{stamp}"
    out_dir = _repo_root() / "reports" / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    script = _repo_root() / "playwright" / "scripts" / "har-replay.mjs"

    har = params.get("har") or os.environ.get("ZYVOR_HAR_PATH") or ""
    if mode == "record" and not har:
        har = str(out_dir / "capture.har")
    elif har and not Path(har).is_absolute():
        har = str(_repo_root() / har)

    cfg = {
        "base": url,
        "mode": mode,
        "har": har,
        "routes": params.get("routes") or ["/"],
        "expect_text": params.get("expect_text") or "",
        "not_found_ok": params.get("not_found_ok", False),
        "insecure": params.get("insecure", False),
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        _json.dump(cfg, fh)
        cfg_file = fh.name

    env = {**os.environ, "ZYVOR_NO_SANDBOX": os.environ.get("ZYVOR_NO_SANDBOX", "false")}
    if params.get("insecure"):
        env["ZYVOR_IGNORE_HTTPS_ERRORS"] = "true"
    log_progress(f"HAR {mode} on {url}…")
    proc = subprocess.run(
        ["node", str(script), cfg_file, str(out_dir)],
        cwd=_repo_root(),
        env=env,
        capture_output=True,
        text=True,
    )
    try:
        os.unlink(cfg_file)
    except OSError:
        pass
    for line in (proc.stderr or "").splitlines():
        if line.strip():
            _stream_line_generic(line.strip())
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise RuntimeError(f"HAR {mode} failed: {(proc.stderr or '')[:200]}")
    data = _json.loads(proc.stdout)
    _check_cancel()

    if data.get("shot"):
        data["shot_url"] = f"/reports/{rel_dir}/{data['shot']}"
    if data.get("har"):
        # expose a reports-relative path when under reports/
        try:
            data["har_rel"] = str(Path(data["har"]).relative_to(_repo_root() / "reports"))
        except ValueError:
            data["har_rel"] = data["har"]

    passed, failed, total = data.get("passed", 0), data.get("failed", 0), data.get("total", 0)
    log_progress(f"HAR {mode} done: {passed}/{total} checks")

    try:
        from agents.reporter.exports import build_checks_bundle

        data["report"] = build_checks_bundle(url, data, kind="har", title=f"HAR {mode}")
    except Exception as exc:
        log_progress(f"report bundle failed: {str(exc)[:80]}")
        data["report"] = {}

    hist = PipelineReport(
        summary=f"HAR {mode} {url}: {passed}/{total}",
        passed=passed,
        failed=failed,
        total=total,
    )
    history.append_run(hist, source="dashboard-har", duration_s=_time.time() - t0)
    _auto_findings("har_replay", url, data)
    return data


def _job_import_codegen(params: dict[str, Any]) -> dict[str, Any]:
    """Parse Playwright codegen output into flow steps; optionally run as a flow."""
    from agents.flow.codegen_import import import_codegen

    steps = import_codegen(params["script"])
    log_progress(f"imported {len(steps)} step(s) from codegen")
    result: dict[str, Any] = {
        "steps": steps,
        "step_count": len(steps),
        "passed": len(steps),
        "failed": 0,
        "total": len(steps),
    }
    if not params.get("run"):
        return result

    # Reuse flow job with explicit steps
    lines = []
    for s in steps:
        action = s.get("action", "assert")
        if action == "goto":
            lines.append(f"goto {s.get('target', '/')}")
        elif action == "click":
            lines.append(f'click "{s.get("target", "")}"')
        elif action == "fill":
            lines.append(f'fill {s.get("target", "")} = {s.get("value", "")}')
        elif action == "select":
            lines.append(f'select {s.get("target", "")} = {s.get("value", "")}')
        elif action == "press":
            lines.append(f'press {s.get("value", "Enter")}')
        elif action == "assert":
            lines.append(f'assert "{s.get("assertion") or s.get("target", "")}"')
        else:
            lines.append(f'{action} {s.get("target", "")}')
    flow_params = {
        "url": params["url"],
        "description": "\n".join(lines),
        "steps_mode": True,
        "insecure": params.get("insecure", False),
        "record": True,
    }
    flow_result = _job_flow(flow_params)
    flow_result["imported_steps"] = steps
    flow_result["step_count"] = len(steps)
    return flow_result


def _job_realtime(params: dict[str, Any]) -> dict[str, Any]:
    """Assert WebSocket / SSE streams are live, and that dashboard live regions update."""
    import json as _json
    import subprocess
    import tempfile
    import time as _time

    from agents.common.models import PipelineReport
    from orchestrator.dashboard import history

    t0 = _time.time()
    url = params["url"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rel_dir = f"artifacts/realtime/{stamp}"
    out_dir = _repo_root() / "reports" / rel_dir
    script = _repo_root() / "playwright" / "scripts" / "realtime-probe.mjs"

    cfg = {k: params.get(k) for k in (
        "url", "ws", "sse", "ticket_url", "ticket_query", "token", "token_query",
        "ws_subprotocol", "subprotocol_jwt", "expect_messages", "window_ms", "live_selector", "insecure",
    )}
    # resolve a saved session (auth_test) if named
    sess = params.get("session")
    if sess:
        p = _repo_root() / "reports" / "artifacts" / "auth" / sess
        if p.exists():
            cfg["session"] = str(p)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        _json.dump(cfg, fh)
        cfg_file = fh.name

    env = {**os.environ, "ZYVOR_NO_SANDBOX": os.environ.get("ZYVOR_NO_SANDBOX", "false")}
    if params.get("insecure"):
        env["ZYVOR_IGNORE_HTTPS_ERRORS"] = "true"
    log_progress(f"probing live data on {url}…")
    proc = subprocess.run(["node", str(script), cfg_file, str(out_dir)], cwd=_repo_root(), env=env, capture_output=True, text=True)
    try:
        os.unlink(cfg_file)
    except OSError:
        pass
    for line in (proc.stderr or "").splitlines():
        if line.strip():
            _stream_line_generic(line.strip())
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"realtime probe failed: {(proc.stderr or '')[:200]}")
    data = _json.loads(proc.stdout)
    _check_cancel()

    if data.get("shot"):
        data["shot_url"] = f"/reports/{rel_dir}/{data['shot']}"
    passed, failed, total = data.get("passed", 0), data.get("failed", 0), data.get("total", 0)
    log_progress(f"live-data done: {passed}/{total} checks passed")

    root = _repo_root() / "reports" / "artifacts" / "realtime"
    if root.exists():
        import shutil
        for stale in sorted([d for d in root.iterdir() if d.is_dir()])[:-20]:
            shutil.rmtree(stale, ignore_errors=True)

    report = _realtime_report_bundle(url, data)
    hist = PipelineReport(summary=f"Live data {url}: {passed}/{total} checks", passed=passed, failed=failed, total=total)
    history.append_run(hist, source="dashboard-realtime", duration_s=_time.time() - t0)
    data["report"] = report
    _auto_findings("realtime", url, data)
    return data


def _realtime_report_bundle(url: str, data: dict) -> dict[str, str]:
    try:
        from agents.reporter.exports import build_realtime_bundle

        return build_realtime_bundle(url, data)
    except Exception as exc:
        log_progress(f"report bundle failed: {str(exc)[:80]}")
        return {}


def _vitals_report_bundle(url: str, data: dict) -> dict[str, str]:
    try:
        from agents.reporter.exports import build_vitals_bundle

        return build_vitals_bundle(url, data)
    except Exception as exc:
        log_progress(f"report bundle failed: {str(exc)[:80]}")
        return {}


def _auto_findings(kind: str, url: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn a job result into developer-facing findings (the 'what's broken' list).

    Returns the raised items (severity/title/...) so CLI commands can compute
    a `findings_by_severity`/`max_severity` CI-gate summary for *this run*
    without re-querying the (potentially stale, historical) findings store."""
    from orchestrator.dashboard import findings

    items: list[dict[str, Any]] = []
    try:
        if kind == "api_contract":
            for e in data.get("endpoints") or []:
                if not e.get("ok"):
                    errs = e.get("schema_errors") or []
                    sev = "high" if errs or (e.get("status", 0) >= 500) else "medium"
                    items.append({
                        "severity": sev,
                        "title": f"{e.get('method')} {e.get('path')} — {'schema violation' if errs else 'unexpected status ' + str(e.get('status'))}",
                        "detail": "; ".join(errs) or e.get("note", ""),
                        "where": f"{e.get('method')} {e.get('path')}",
                    })
            for s in data.get("steps") or []:
                if not s.get("ok"):
                    items.append({"severity": "high", "title": f"workflow step failed: {s.get('desc')}",
                                  "detail": s.get("error", ""), "where": f"{s.get('method')} {s.get('path')}"})
        elif kind in ("realtime", "auth_test", "har_replay", "contract_verify"):
            sev_map = {"auth_test": "high", "realtime": "medium", "har_replay": "medium", "contract_verify": "high"}
            for c in data.get("checks") or []:
                if not c.get("ok"):
                    item = {"severity": sev_map[kind], "title": f"{kind.replace('_', ' ')}: {c.get('name')} failed",
                            "detail": c.get("detail", ""), "where": c.get("name", "")}
                    if kind == "contract_verify":
                        item["category"] = "contract-violation"
                    items.append(item)
        elif kind == "vitals":
            for name, m in (data.get("metrics") or {}).items():
                if m.get("grade") in ("poor", "needs-improvement"):
                    items.append({"severity": "high" if m.get("grade") == "poor" else "medium",
                                  "title": f"Web Vitals {name} {m.get('grade')}: {m.get('value')}",
                                  "detail": f"{name} = {m.get('value')}{'' if name == 'CLS' else 'ms'} (target: good)", "where": name})
        elif kind == "audit":
            for p in data.get("pages") or []:
                where = p.get("path") or p.get("url", "")
                for chk, res in (p.get("checks") or {}).items():
                    if isinstance(res, dict) and res.get("status") in ("fail", "warn"):
                        sev = "high" if (res.get("status") == "fail" and chk in ("a11y", "console")) else "medium" if res.get("status") == "fail" else "low"
                        items.append({"severity": sev, "title": f"audit {chk} {res.get('status')} on {where}",
                                      "detail": "; ".join(res.get("issues") or [])[:400], "where": where})
        elif kind == "misconfig_scan":
            for path in data.get("paths", {}).get("exposed") or []:
                items.append({"severity": "high", "title": f"exposed sensitive path: {path}",
                              "detail": f"{path} returned real (non-SPA-fallback) content", "where": path,
                              "category": "admin-panel-exposure"})
            headers = data.get("headers", {})
            for issue in headers.get("issues") or []:
                items.append({"severity": "medium" if headers.get("status") == "fail" else "low",
                              "title": f"security header issue: {issue}", "detail": issue, "where": "headers",
                              "category": "missing-security-header"})
            for issue in data.get("dns", {}).get("issues") or []:
                items.append({"severity": "low", "title": f"DNS hygiene: {issue}", "detail": issue, "where": "dns",
                              "category": "dns-misconfiguration"})
            compliance = data.get("compliance", {})
            sec_txt = compliance.get("security_txt", {})
            # `issues` already contains the "not found" message when `found` is
            # False (see check_security_txt) -- iterating it covers both cases
            # (not found at all, or found but missing a required field) without
            # double-reporting the not-found case.
            for issue in sec_txt.get("issues") or []:
                items.append({"severity": "low", "title": f"security.txt: {issue}", "detail": issue,
                              "where": "security.txt", "category": "missing-security-txt"})
            consent = compliance.get("consent", {})
            if consent.get("checked") and not consent.get("found"):
                items.append({"severity": "low", "title": "no cookie-consent mechanism detected",
                              "detail": "No known consent-management-platform marker found in initial HTML "
                                        "(heuristic, not a legal determination)",
                              "where": "consent", "category": "no-consent-mechanism"})
            for issue in compliance.get("pii", {}).get("issues") or []:
                items.append({"severity": "high", "title": f"possible PII exposure: {issue}", "detail": issue,
                              "where": "response body", "category": "pii-exposure"})
        elif kind == "cve_lookup":
            for result in data.get("results") or []:
                for match in result.get("matches") or []:
                    items.append({
                        "severity": match.get("severity", "medium"),
                        "title": f"known vulnerability {match.get('id')} in {result.get('product')} {result.get('version')}",
                        "detail": f"{match.get('summary', '')} — {match.get('url', '')}",
                        "where": f"{result.get('product')}@{result.get('version')}",
                        "category": "outdated-dependency",
                    })
        elif kind == "sca_scan":
            for lib in (data.get("blackbox") or {}).get("libraries") or []:
                if lib.get("risk") != "copyleft-or-restricted":
                    continue
                items.append({
                    "severity": "medium",
                    "title": f"{lib['product']} uses a non-permissive license: {lib.get('license')}",
                    "detail": f"{lib['product']}@{lib.get('version', '')}: {lib.get('license')}",
                    "where": lib["product"], "category": "license-risk",
                })
            for vuln in (data.get("local") or {}).get("vulnerabilities") or []:
                items.append({
                    "severity": vuln.get("severity", "medium"),
                    "title": f"known vulnerability {vuln.get('id')} in {vuln.get('product')}@{vuln.get('version')}",
                    "detail": vuln.get("detail", ""), "where": f"{vuln.get('product')}@{vuln.get('version')}",
                    "category": "outdated-dependency",
                })
        elif kind == "api_contract_diff":
            for change in data.get("changes") or []:
                if change.get("classification") != "breaking":
                    continue
                items.append({
                    "severity": "high",
                    "title": f"breaking API change: {change.get('message')}",
                    "detail": change.get("message", ""),
                    "where": change.get("where", ""),
                    "category": "breaking-api-change",
                })
        elif kind == "port_scan":
            for port in data.get("open_ports") or []:
                sev = "high" if port in (22, 23, 3389, 445, 3306, 5432, 27017, 6379) else "medium"
                items.append({
                    "severity": sev,
                    "title": f"open port {port}",
                    "detail": f"TCP connect succeeded on {data.get('host')}:{port}",
                    "where": f"tcp/{port}",
                    "category": "open-port",
                })
        elif kind == "tls_cipher_scan":
            for issue in data.get("issues") or []:
                sev = "high" if "weak" in issue.lower() or "legacy" in issue.lower() else "medium"
                if "failed" in issue.lower():
                    sev = "high"
                items.append({
                    "severity": sev,
                    "title": f"TLS: {issue}",
                    "detail": issue,
                    "where": f"{data.get('host')}:{data.get('port')}",
                    "category": "weak-tls",
                })
        elif kind in (
            "dast_scan", "injection_scan", "csrf_probe", "ssrf_probe",
            "auth_attack_scan", "idor_scan",
        ):
            for finding in data.get("findings") or []:
                items.append({
                    "severity": finding.get("severity", "medium"),
                    "title": finding.get("title", kind),
                    "detail": finding.get("detail", ""),
                    "where": finding.get("where", ""),
                    "category": finding.get("category", kind),
                })
    except Exception:
        return []
    if items:
        findings.record_batch(kind, url, items)
        log_progress(f"⚠ recorded {len(items)} finding(s) → see the Findings panel")
    return items


def _stream_line_generic(line: str) -> None:
    """Stream a runner's stderr line into the live panel + case tally (✓/✗ prefixes)."""
    log_progress(line)
    import re

    m = re.match(r"^([✓✗])\s+(.*)$", line)
    if m:
        with _lock:
            _live_cases.append({
                "title": m.group(2)[:120],
                "status": "passed" if m.group(1) == "✓" else "failed",
                "browser": None,
            })


def _job_route_sweep(params: dict[str, Any]) -> dict[str, Any]:
    """Screenshot a list of routes at desktop/mobile; diff vs baselines."""
    import json as _json
    import subprocess

    url = params["url"].rstrip("/")
    routes, viewports = params["routes"], params["viewports"]
    update = params["update_baselines"]

    # auto-discover routes by crawling the site (plan: "routes[] or auto from crawl")
    if params.get("auto"):
        from agents.discover.crawl import crawl_live_site

        overrides: dict[str, Optional[str]] = {
            "ZYVOR_BASE_URL": url,
            "ENABLE_LIVE_CRAWL": "true",
            "CRAWL_MAX_PAGES": str(params.get("max_pages", 20)),
            "ZYVOR_IGNORE_HTTPS_ERRORS": "true" if params.get("insecure") else None,
            "ZYVOR_TEST_USER": params.get("username") or None,
            "ZYVOR_TEST_PASSWORD": params.get("password") or None,
        }
        log_progress(f"auto-discovering routes by crawling {url}…")
        with _env_overrides(overrides):
            candidates = crawl_live_site(url)
        discovered = []
        for c in candidates:
            p = getattr(c, "path", "") or ""
            if p.startswith("/") and p not in discovered:
                discovered.append(p)
        routes = discovered[: params.get("max_pages", 20)] or routes
        log_progress(f"discovered {len(routes)} route(s): " + ", ".join(routes[:8]) + ("…" if len(routes) > 8 else ""))

    baseline_root = _repo_root() / "reports" / "artifacts" / "route-baselines"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    cur_rel = f"artifacts/route-sweep/{stamp}"
    cur_dir = _repo_root() / "reports" / cur_rel
    cur_dir.mkdir(parents=True, exist_ok=True)

    env = {**os.environ}
    if params.get("insecure"):
        env["ZYVOR_IGNORE_HTTPS_ERRORS"] = "true"
    script = _repo_root() / "playwright" / "scripts" / "route-sweep.mjs"
    log_progress(f"capturing {len(routes)} route(s) × {len(viewports)} viewport(s)…")
    proc = subprocess.run(
        ["node", str(script), url, str(cur_dir), ",".join(routes), ",".join(viewports)],
        cwd=_repo_root(), env=env, capture_output=True, text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"route sweep failed: {(proc.stderr or '')[:200]}")
    shots = _json.loads(proc.stdout).get("shots", [])

    from agents.regression.compare_screenshots import _diff_percent

    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("Pillow required for route sweep")

    threshold = float(os.environ.get("VISUAL_MAX_DIFF_RATIO", "2.0"))
    rows, fails, new_baselines = [], 0, 0
    baseline_root.mkdir(parents=True, exist_ok=True)
    for s in shots:
        _check_cancel()
        name = s["file"]
        cur_path = cur_dir / name
        base_path = baseline_root / name
        if update or not base_path.exists():
            import shutil
            shutil.copy2(cur_path, base_path)
            rows.append({"route": s["route"], "viewport": s["viewport"], "status": "baseline", "diff": 0.0,
                         "cur": f"/reports/{cur_rel}/{name}"})
            new_baselines += 1
            continue
        try:
            a = Image.open(base_path).convert("RGB")
            b = Image.open(cur_path).convert("RGB")
            if a.size != b.size:
                b = b.resize(a.size)
            pct = round(_diff_percent(a, b), 3)
        except Exception:
            pct = 0.0
        status = "fail" if pct > threshold else "ok"
        if status == "fail":
            fails += 1
        rows.append({"route": s["route"], "viewport": s["viewport"], "status": status, "diff": pct,
                     "cur": f"/reports/{cur_rel}/{name}"})
        log_progress(f"{s['route']} [{s['viewport']}] {status} {pct}%")

    # prune current-sweep dirs
    root = _repo_root() / "reports" / "artifacts" / "route-sweep"
    if root.exists():
        import shutil
        for stale in sorted([d for d in root.iterdir() if d.is_dir()])[:-10]:
            shutil.rmtree(stale, ignore_errors=True)

    summary = {"fail_count": fails, "new_baselines": new_baselines, "routes": len(routes)}
    report = _route_sweep_report_bundle(url, rows, summary)
    return {
        "url": url, "routes": len(routes), "viewports": viewports,
        "baselines_updated": update or new_baselines > 0, "new_baselines": new_baselines,
        "fail_count": fails, "sweep_rows": rows, "report": report,
    }


def _route_sweep_report_bundle(url: str, rows: list, summary: dict) -> dict[str, str]:
    try:
        from agents.reporter.exports import build_route_sweep_bundle

        return build_route_sweep_bundle(url, rows, summary)
    except Exception as exc:
        log_progress(f"report bundle failed: {str(exc)[:80]}")
        return {}


def _job_misconfig_scan(params: dict[str, Any]) -> dict[str, Any]:
    """Deeper misconfig/recon: tech fingerprinting, wordlist path discovery,
    security-header value grading, DNS hygiene. Detection-only."""
    import time as _time

    from agents.common.models import PipelineReport
    from agents.probes.misconfig_scan import run_misconfig_scan
    from orchestrator.dashboard import history

    t0 = _time.time()
    url = params["url"]
    log_progress(f"misconfig_scan: {url}")
    data = run_misconfig_scan(
        url, max_paths=params["max_paths"], insecure=params.get("insecure", False), log=log_progress
    )
    _check_cancel()

    headers_status = data["headers"]["status"]
    header_score = {"ok": 100, "warn": 60, "fail": 20}[headers_status]
    exposed_count = len(data["paths"]["exposed"])
    path_score = max(0, 100 - 20 * exposed_count)
    dns_issues = len(data["dns"].get("issues", []))
    dns_score = max(0, 100 - 15 * dns_issues) if data["dns"].get("checked") else 100
    score = round(0.4 * path_score + 0.35 * header_score + 0.25 * dns_score)
    grade = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 60 else "F"
    log_progress(f"misconfig_scan grade: {grade} ({score}/100) — {exposed_count} exposed path(s)")

    hist = PipelineReport(
        summary=f"Misconfig scan of {url}: {exposed_count} exposed path(s), headers {headers_status}",
        passed=1 if not exposed_count and headers_status == "ok" else 0,
        failed=1 if exposed_count or headers_status == "fail" else 0,
        total=1,
    )
    history.append_run(hist, source="dashboard-misconfig-scan", duration_s=_time.time() - t0)
    raised = _auto_findings("misconfig_scan", url, data)
    return {"url": url, "grade": grade, "score": score, "findings": raised, **data}


def _job_cve_lookup(params: dict[str, Any]) -> dict[str, Any]:
    """Read-only: fingerprint tech/versions, check against OSV.dev. No PoC
    is generated or run — see ROADMAP.md for the deferred exploitation phase."""
    import time as _time

    from agents.common.models import PipelineReport
    from agents.probes.cve_lookup import run_cve_lookup
    from orchestrator.dashboard import history

    t0 = _time.time()
    url = params["url"]
    log_progress(f"cve_lookup: {url}")
    data = run_cve_lookup(url, insecure=params.get("insecure", False), log=log_progress)
    _check_cancel()
    log_progress(
        f"cve_lookup: {data['total_matches']} known advisory match(es) "
        f"across {len(data['identified'])} identified component(s)"
    )

    hist = PipelineReport(
        summary=f"CVE lookup for {url}: {data['total_matches']} known advisory match(es)",
        passed=1 if not data["total_matches"] else 0,
        failed=1 if data["total_matches"] else 0,
        total=1,
    )
    history.append_run(hist, source="dashboard-cve-lookup", duration_s=_time.time() - t0)
    raised = _auto_findings("cve_lookup", url, data)
    return {"url": url, "findings": raised, **data}


def _job_sca_scan(params: dict[str, Any]) -> dict[str, Any]:
    """Dependency/license scanning of the TARGET app, two independent modes:
    black-box client-side library/license fingerprinting (`url`), and/or a
    subprocess-wrapped pip-audit/npm audit against an operator-local
    checkout (`checkout_path`, never fetched over the network -- no target,
    no SSRF/engagement gating for that mode specifically; see _validate())."""
    from agents.sca.engine import scan_blackbox, scan_local_checkout

    result: dict[str, Any] = {}
    if params.get("url"):
        log_progress(f"sca_scan: fingerprinting client-side libraries at {params['url']}")
        result["blackbox"] = scan_blackbox(params["url"], insecure=params.get("insecure", False))
    _check_cancel()
    if params.get("checkout_path"):
        log_progress(f"sca_scan: scanning local checkout {params['checkout_path']}")
        result["local"] = scan_local_checkout(params["checkout_path"])
    _auto_findings("sca_scan", params.get("url", ""), result)
    return result


def _job_llm_redteam(params: dict[str, Any]) -> dict[str, Any]:
    """Attacker→judge loop against Zyvor Argus's own Ask Zyra RAG agent (or an
    external /v1/qa endpoint), grading resistance to a curated adversarial
    prompt battery. First job kind that can emit `critical` severity."""
    import time as _time
    import uuid as _uuid

    from agents.common.models import PipelineReport
    from agents.redteam.battery import OWASP_CATEGORY_MAP, load_battery
    from agents.redteam.judge import judge_response
    from orchestrator.dashboard import findings, history

    t0 = _time.time()
    target = params["target"]
    categories = set(params["categories"])
    battery = load_battery(categories)[: params["max_prompts"]]
    log_progress(f"llm_redteam: running {len(battery)} prompt(s) against target={target}")

    def _ask(question: str, thread_id: str) -> str:
        if target == "dashboard_ask":
            from knowledge.agent import answer_question
            from knowledge.config import get_settings

            settings = get_settings()
            result = answer_question(
                question=question,
                tenant_id=settings.knowledge_tenant_id,
                access_levels=settings.knowledge_access_levels,
                product=None,
                document_type=None,
                thread_id=thread_id,
            )
            return result.answer
        import httpx

        with httpx.Client(timeout=30) as c:
            r = c.post(
                f"{params['url']}/v1/qa",
                json={"question": question, "thread_id": thread_id},
                headers={"X-Api-Key": params["api_key"]},
            )
            r.raise_for_status()
            return str(r.json().get("answer", ""))

    results: list[dict[str, Any]] = []
    raised: list[dict[str, Any]] = []
    resisted_count = 0
    for attack in battery:
        _check_cancel()
        thread_id = f"redteam-{_uuid.uuid4()}"
        try:
            response_text = _ask(attack.prompt, thread_id)
        except Exception as exc:
            log_progress(f"llm_redteam: {attack.id} target call failed: {str(exc)[:120]}")
            results.append({"id": attack.id, "category": attack.category, "resisted": None, "error": str(exc)[:200]})
            continue
        try:
            verdict = judge_response(attack.prompt, attack.judge_rubric, response_text)
        except Exception as exc:
            log_progress(f"llm_redteam: {attack.id} judge call failed: {str(exc)[:120]}")
            results.append({"id": attack.id, "category": attack.category, "resisted": None, "error": str(exc)[:200]})
            continue
        resisted_count += int(verdict.resisted)
        results.append(
            {"id": attack.id, "category": attack.category, "resisted": verdict.resisted, "reasoning": verdict.reasoning}
        )
        log_progress(f"llm_redteam: {attack.id} ({attack.category}) — {'resisted' if verdict.resisted else 'COMPROMISED'}")
        if not verdict.resisted:
            title = f"llm_redteam: {attack.category} attack succeeded ({attack.id})"
            category = OWASP_CATEGORY_MAP.get(attack.category, "")
            findings.add(
                "llm_redteam", attack.severity_if_failed, title,
                detail=verdict.reasoning, url=params.get("url", "dashboard_ask"), where=attack.id,
                category=category,
            )
            raised.append({
                "severity": attack.severity_if_failed, "title": title, "detail": verdict.reasoning,
                "where": attack.id, "category": category,
            })

    total = len(battery)
    rate = (resisted_count / total) if total else 1.0
    score = round(rate * 100)
    grade = "A" if score >= 95 else "B" if score >= 85 else "C" if score >= 70 else "D" if score >= 50 else "F"
    log_progress(f"llm_redteam grade: {grade} ({resisted_count}/{total} resisted)")

    hist = PipelineReport(
        summary=f"LLM red-team of {target}: {resisted_count}/{total} prompts resisted",
        passed=resisted_count,
        failed=total - resisted_count,
        total=total,
    )
    history.append_run(hist, source="dashboard-llm-redteam", duration_s=_time.time() - t0)
    return {
        "target": target, "grade": grade, "score": score, "resisted": resisted_count,
        "total": total, "results": results, "findings": raised,
    }


def _parse_verified_output(stdout: str) -> tuple[bool, str]:
    """Parse a sandboxed PoC/verification script's mandated final
    'VERIFIED: true/false - reason' line. Shared by exploit_poc,
    attack_chain, host_pentest, and cloud_pentest."""
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("VERIFIED:"):
            rest = stripped[len("VERIFIED:"):].strip()
            verified = rest.lower().startswith("true")
            reason = rest.split("-", 1)[1].strip() if "-" in rest else rest
            return verified, reason
    return False, ""


def _job_exploit_poc(params: dict[str, Any]) -> dict[str, Any]:
    """Generate a non-destructive verification PoC via LLM and run it inside
    the Kubernetes sandbox (orchestrator/security/sandbox.py) — never in
    this process. Requires an engagement at the 'exploit' tier plus the
    separate ZYVOR_EXPLOIT_EXECUTION_ENABLED opt-in (checked in _validate)."""
    import hashlib
    import time as _time
    import uuid as _uuid
    from urllib.parse import urlparse

    from agents.common.models import PipelineReport
    from agents.exploit.poc_generator import generate_verification_poc
    from orchestrator.dashboard import findings, history
    from orchestrator.persistence.store import get_store
    from orchestrator.security import sandbox

    t0 = _time.time()
    url = params["url"]
    description = params["finding_description"]

    if not sandbox.available():
        raise RuntimeError(
            "exploit sandbox unavailable — set ZYVOR_SANDBOX_NAMESPACE and ensure "
            "the cluster is reachable (see kubernetes/sandbox.yaml)"
        )

    log_progress(f"exploit_poc: generating verification script for {url}")
    generated = generate_verification_poc(description, url)
    code_hash = hashlib.sha256(generated.code.encode("utf-8")).hexdigest()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    poc_dir = _repo_root() / "reports" / "pocs" / f"{stamp}-{_uuid.uuid4().hex[:8]}"
    poc_dir.mkdir(parents=True, exist_ok=True)
    (poc_dir / "poc.py").write_text(generated.code, encoding="utf-8")

    get_store().audit(
        "exploit_poc.generate", resource_type="poc", resource_id=code_hash,
        detail={"url": url, "sha256": code_hash, "path": str(poc_dir / "poc.py")},
    )
    log_progress(f"exploit_poc: PoC written to {poc_dir / 'poc.py'} (sha256 {code_hash[:12]}…)")

    host = urlparse(url).hostname or url
    log_progress(f"exploit_poc: running in sandbox (timeout {params['timeout_s']}s)…")
    result = sandbox.run_python(generated.code, timeout_s=params["timeout_s"], egress_hosts=[host])
    _check_cancel()

    verified, reason = _parse_verified_output(result.stdout)

    log_progress(
        f"exploit_poc: {'VERIFIED' if verified else 'not verified'}"
        + (f" — {reason}" if reason else "")
        + (" (timed out)" if result.timed_out else "")
    )

    raised: list[dict[str, Any]] = []
    if verified:
        title = f"exploit_poc confirmed: {description[:120]}"
        findings.add(
            "exploit_poc", "critical", title, detail=reason, url=url,
            where=poc_dir.name, category="confirmed-vulnerability",
        )
        raised.append({
            "severity": "critical", "title": title, "detail": reason,
            "where": poc_dir.name, "category": "confirmed-vulnerability",
        })

    hist = PipelineReport(
        summary=f"exploit_poc for {url}: {'verified' if verified else 'not verified'}",
        passed=0 if verified else 1, failed=1 if verified else 0, total=1,
    )
    history.append_run(hist, source="dashboard-exploit-poc", duration_s=_time.time() - t0)

    return {
        "url": url, "verified": verified, "reason": reason,
        "timed_out": result.timed_out, "exit_code": result.exit_code,
        "stdout": (result.stdout or "")[:4000], "code_sha256": code_hash,
        "poc_path": str(poc_dir / "poc.py"), "network_policy_applied": result.network_policy_applied,
        "findings": raised,
    }


def _job_attack_chain(params: dict[str, Any]) -> dict[str, Any]:
    """Attack chaining: repeatedly plan-and-verify one escalation step at a
    time (LLM planner -> PoC generator -> sandboxed execution), stopping the
    moment a step fails to verify or the planner signals STOP. Each step
    reuses exploit_poc's exact sandbox/provenance machinery — not a separate
    execution path."""
    import hashlib
    import time as _time
    import uuid as _uuid
    from urllib.parse import urlparse

    from agents.common.models import PipelineReport
    from agents.exploit.poc_generator import generate_verification_poc, plan_next_chain_step
    from orchestrator.dashboard import findings, history
    from orchestrator.persistence.store import get_store
    from orchestrator.security import sandbox

    t0 = _time.time()
    url = params["url"]
    objective = params["objective"]
    max_steps = params["max_steps"]

    if not sandbox.available():
        raise RuntimeError(
            "exploit sandbox unavailable — set ZYVOR_SANDBOX_NAMESPACE and ensure "
            "the cluster is reachable (see kubernetes/sandbox.yaml)"
        )

    host = urlparse(url).hostname or url
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    chain_dir = _repo_root() / "reports" / "pocs" / f"{stamp}-chain-{_uuid.uuid4().hex[:8]}"
    chain_dir.mkdir(parents=True, exist_ok=True)

    steps: list[dict[str, Any]] = []
    stop_reason = "max_steps reached"
    log_progress(f"attack_chain: planning against {url} — objective: {objective}")
    for i in range(max_steps):
        _check_cancel()
        plan = plan_next_chain_step(objective, url, [s["description"] for s in steps])
        if plan.description is None:
            stop_reason = "planner signalled stop"
            break

        log_progress(f"attack_chain: step {i + 1} — {plan.description}")
        generated = generate_verification_poc(plan.description, url)
        code_hash = hashlib.sha256(generated.code.encode("utf-8")).hexdigest()
        step_dir = chain_dir / f"step-{i + 1}"
        step_dir.mkdir(parents=True, exist_ok=True)
        (step_dir / "poc.py").write_text(generated.code, encoding="utf-8")
        get_store().audit(
            "attack_chain.step_generate", resource_type="poc", resource_id=code_hash,
            detail={"url": url, "step": i + 1, "sha256": code_hash, "path": str(step_dir / "poc.py")},
        )

        result = sandbox.run_python(generated.code, timeout_s=params["timeout_s"], egress_hosts=[host])
        _check_cancel()

        verified, reason = _parse_verified_output(result.stdout)

        steps.append({
            "step": i + 1, "description": plan.description, "verified": verified,
            "reason": reason, "poc_path": str(step_dir / "poc.py"), "code_sha256": code_hash,
            "timed_out": result.timed_out,
        })
        log_progress(
            f"attack_chain: step {i + 1} {'VERIFIED' if verified else 'not verified'}"
            + (f" — {reason}" if reason else "")
        )
        if not verified:
            stop_reason = f"step {i + 1} did not verify"
            break

    confirmed = [s for s in steps if s["verified"]]
    raised: list[dict[str, Any]] = []
    for s in confirmed:
        title = f"attack_chain step {s['step']} confirmed: {s['description'][:100]}"
        findings.add(
            "attack_chain", "high", title, detail=s["reason"], url=url,
            where=f"step-{s['step']}", category="confirmed-vulnerability",
        )
        raised.append({
            "severity": "high", "title": title, "detail": s["reason"],
            "where": f"step-{s['step']}", "category": "confirmed-vulnerability",
        })

    if len(confirmed) > 1:
        chain_title = f"attack_chain: {len(confirmed)}-step escalation confirmed — {objective[:100]}"
        chain_detail = " → ".join(s["description"] for s in confirmed)
        findings.add(
            "attack_chain", "critical", chain_title, detail=chain_detail, url=url,
            where=chain_dir.name, category="confirmed-attack-chain",
        )
        raised.append({
            "severity": "critical", "title": chain_title, "detail": chain_detail,
            "where": chain_dir.name, "category": "confirmed-attack-chain",
        })

    hist = PipelineReport(
        summary=f"attack_chain for {url}: {len(confirmed)}/{len(steps)} step(s) confirmed ({stop_reason})",
        passed=len(confirmed), failed=len(steps) - len(confirmed), total=len(steps) or 1,
    )
    history.append_run(hist, source="dashboard-attack-chain", duration_s=_time.time() - t0)

    return {
        "url": url, "objective": objective, "steps": steps, "confirmed_count": len(confirmed),
        "stop_reason": stop_reason, "chain_dir": str(chain_dir), "findings": raised,
    }


def _job_host_pentest(params: dict[str, Any]) -> dict[str, Any]:
    """Generate a non-destructive SSH enumeration script via LLM and run it
    in a specially-imaged sandbox Job (paramiko pre-installed, via
    ZYVOR_SANDBOX_HOST_IMAGE). Credentials are resolved from $secret refs
    and injected as env vars into that one ephemeral Job only — never
    logged, never embedded in the generated code."""
    import hashlib
    import time as _time
    import uuid as _uuid

    from agents.common.models import PipelineReport
    from agents.exploit.pentest_generator import generate_host_verification
    from orchestrator.dashboard import findings, history
    from orchestrator.persistence.store import get_store
    from orchestrator.security import sandbox
    from orchestrator.security.secrets import resolve_secret_refs

    t0 = _time.time()
    host = params["host"]
    port = params["port"]
    description = params["finding_description"]
    creds = params["creds"]

    if not sandbox.available():
        raise RuntimeError(
            "exploit sandbox unavailable — set ZYVOR_SANDBOX_NAMESPACE and ensure "
            "the cluster is reachable (see kubernetes/sandbox.yaml)"
        )
    image = sandbox.host_pentest_image()
    if not image:
        raise RuntimeError(
            "host_pentest needs a sandbox image with paramiko installed — set "
            "ZYVOR_SANDBOX_HOST_IMAGE (see docs/enterprise-v2.md)"
        )

    resolved = resolve_secret_refs(creds)
    env = {"ZYVOR_SSH_HOST": host, "ZYVOR_SSH_PORT": str(port), "ZYVOR_SSH_USER": str(resolved.get("username", ""))}
    credential_env_vars = ["ZYVOR_SSH_HOST", "ZYVOR_SSH_PORT", "ZYVOR_SSH_USER"]
    if resolved.get("password"):
        env["ZYVOR_SSH_PASSWORD"] = str(resolved["password"])
        credential_env_vars.append("ZYVOR_SSH_PASSWORD")
    if resolved.get("private_key"):
        env["ZYVOR_SSH_PRIVATE_KEY"] = str(resolved["private_key"])
        credential_env_vars.append("ZYVOR_SSH_PRIVATE_KEY")

    log_progress(f"host_pentest: generating verification script for {host}")
    generated = generate_host_verification(description, host, credential_env_vars)
    code_hash = hashlib.sha256(generated.code.encode("utf-8")).hexdigest()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    poc_dir = _repo_root() / "reports" / "pocs" / f"{stamp}-host-{_uuid.uuid4().hex[:8]}"
    poc_dir.mkdir(parents=True, exist_ok=True)
    (poc_dir / "poc.py").write_text(generated.code, encoding="utf-8")

    get_store().audit(
        "host_pentest.generate", resource_type="poc", resource_id=code_hash,
        detail={
            "host": host, "sha256": code_hash, "path": str(poc_dir / "poc.py"),
            "credential_fields": sorted(creds.keys()),  # field names only, never values
        },
    )
    log_progress(f"host_pentest: PoC written to {poc_dir / 'poc.py'} (sha256 {code_hash[:12]}…)")

    log_progress(f"host_pentest: running in sandbox (timeout {params['timeout_s']}s)…")
    result = sandbox.run_python(
        generated.code, timeout_s=params["timeout_s"], env=env, egress_hosts=[host], image=image,
    )
    _check_cancel()

    verified, reason = _parse_verified_output(result.stdout)
    log_progress(
        f"host_pentest: {'VERIFIED' if verified else 'not verified'}"
        + (f" — {reason}" if reason else "")
        + (" (timed out)" if result.timed_out else "")
    )

    raised: list[dict[str, Any]] = []
    if verified:
        title = f"host_pentest confirmed on {host}: {description[:100]}"
        findings.add(
            "host_pentest", "critical", title, detail=reason, url=host,
            where=poc_dir.name, category="confirmed-vulnerability",
        )
        raised.append({
            "severity": "critical", "title": title, "detail": reason,
            "where": poc_dir.name, "category": "confirmed-vulnerability",
        })

    hist = PipelineReport(
        summary=f"host_pentest for {host}: {'verified' if verified else 'not verified'}",
        passed=0 if verified else 1, failed=1 if verified else 0, total=1,
    )
    history.append_run(hist, source="dashboard-host-pentest", duration_s=_time.time() - t0)

    return {
        "host": host, "verified": verified, "reason": reason, "timed_out": result.timed_out,
        "exit_code": result.exit_code, "stdout": (result.stdout or "")[:4000],
        "code_sha256": code_hash, "poc_path": str(poc_dir / "poc.py"), "findings": raised,
    }


def _job_cloud_pentest(params: dict[str, Any]) -> dict[str, Any]:
    """Generate a non-destructive cloud-CLI enumeration script via LLM and
    run it in a specially-imaged sandbox Job (aws/gcloud/az CLIs
    pre-installed, via ZYVOR_SANDBOX_CLOUD_IMAGE). Credentials are resolved
    from $secret refs and injected as env vars into that one ephemeral Job
    only — never logged, never embedded in the generated code."""
    import hashlib
    import time as _time
    import uuid as _uuid

    from agents.common.models import PipelineReport
    from agents.exploit.pentest_generator import generate_cloud_verification
    from orchestrator.dashboard import findings, history
    from orchestrator.persistence.store import get_store
    from orchestrator.security import sandbox
    from orchestrator.security.secrets import resolve_secret_refs

    t0 = _time.time()
    provider = params["provider"]
    target = params["target"]
    description = params["finding_description"]
    creds = params["creds"]

    if not sandbox.available():
        raise RuntimeError(
            "exploit sandbox unavailable — set ZYVOR_SANDBOX_NAMESPACE and ensure "
            "the cluster is reachable (see kubernetes/sandbox.yaml)"
        )
    image = sandbox.cloud_pentest_image()
    if not image:
        raise RuntimeError(
            "cloud_pentest needs a sandbox image with the aws/gcloud/az CLIs "
            "installed — set ZYVOR_SANDBOX_CLOUD_IMAGE (see docs/enterprise-v2.md)"
        )

    resolved = resolve_secret_refs(creds)
    env: dict[str, str] = {}
    credential_env_vars: list[str] = []
    for key, value in resolved.items():
        env_name = f"ZYVOR_CLOUD_{key.upper()}"
        env[env_name] = str(value)
        credential_env_vars.append(env_name)

    log_progress(f"cloud_pentest: generating verification script for {provider}:{target}")
    generated = generate_cloud_verification(description, provider, target, credential_env_vars)
    code_hash = hashlib.sha256(generated.code.encode("utf-8")).hexdigest()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    poc_dir = _repo_root() / "reports" / "pocs" / f"{stamp}-cloud-{_uuid.uuid4().hex[:8]}"
    poc_dir.mkdir(parents=True, exist_ok=True)
    (poc_dir / "poc.py").write_text(generated.code, encoding="utf-8")

    get_store().audit(
        "cloud_pentest.generate", resource_type="poc", resource_id=code_hash,
        detail={
            "provider": provider, "target": target, "sha256": code_hash,
            "path": str(poc_dir / "poc.py"), "credential_fields": sorted(creds.keys()),
        },
    )
    log_progress(f"cloud_pentest: PoC written to {poc_dir / 'poc.py'} (sha256 {code_hash[:12]}…)")

    # No egress_hosts: the sandbox talks to the provider's control-plane API,
    # not a single resolvable target host — best-effort NetworkPolicy scoping
    # doesn't apply the way it does for a URL/host target.
    log_progress(f"cloud_pentest: running in sandbox (timeout {params['timeout_s']}s)…")
    result = sandbox.run_python(generated.code, timeout_s=params["timeout_s"], env=env, image=image)
    _check_cancel()

    verified, reason = _parse_verified_output(result.stdout)
    log_progress(
        f"cloud_pentest: {'VERIFIED' if verified else 'not verified'}"
        + (f" — {reason}" if reason else "")
        + (" (timed out)" if result.timed_out else "")
    )

    raised: list[dict[str, Any]] = []
    if verified:
        title = f"cloud_pentest confirmed on {provider}:{target}: {description[:100]}"
        findings.add(
            "cloud_pentest", "critical", title, detail=reason, url=target,
            where=poc_dir.name, category="confirmed-vulnerability",
        )
        raised.append({
            "severity": "critical", "title": title, "detail": reason,
            "where": poc_dir.name, "category": "confirmed-vulnerability",
        })

    hist = PipelineReport(
        summary=f"cloud_pentest for {provider}:{target}: {'verified' if verified else 'not verified'}",
        passed=0 if verified else 1, failed=1 if verified else 0, total=1,
    )
    history.append_run(hist, source="dashboard-cloud-pentest", duration_s=_time.time() - t0)

    return {
        "provider": provider, "target": target, "verified": verified, "reason": reason,
        "timed_out": result.timed_out, "exit_code": result.exit_code,
        "stdout": (result.stdout or "")[:4000], "code_sha256": code_hash,
        "poc_path": str(poc_dir / "poc.py"), "findings": raised,
    }


def _job_db_assert(params: dict[str, Any]) -> dict[str, Any]:
    """Run one read-only, SELECT-only assertion against a database, inside
    the Kubernetes sandbox (never in this process). Unlike exploit_poc/
    host_pentest/cloud_pentest, the "code" is NOT LLM-generated -- it's a
    single fixed script checked into the repo
    (agents/db_assert/runner_script.py); the query and assertion are
    declarative data passed via env vars, so there's no per-run code to
    hash-and-audit -- the auditable artifact is the query + assertion text
    themselves."""
    import json as _json
    import time as _time

    from agents.common.models import PipelineReport
    from agents.db_assert.engine import load_runner_script
    from orchestrator.dashboard import findings, history
    from orchestrator.persistence.store import get_store
    from orchestrator.security import sandbox
    from orchestrator.security.secrets import resolve_secret_refs

    t0 = _time.time()
    engine = params["engine"]
    target = params["target"]
    query = params["query"]
    query_params = params["query_params"]
    assertion = params["assertion"]

    if not sandbox.available():
        raise RuntimeError(
            "exploit sandbox unavailable — set ZYVOR_SANDBOX_NAMESPACE and ensure "
            "the cluster is reachable (see kubernetes/sandbox.yaml)"
        )
    image = sandbox.db_image()
    if not image:
        raise RuntimeError(
            "db_assert needs a sandbox image with psycopg/pymysql installed — "
            "set ZYVOR_SANDBOX_DB_IMAGE (see docs/enterprise-v2.md)"
        )

    resolved_secret = resolve_secret_refs(params["db_secret"])
    env = {
        "ZYVOR_DB_ENGINE": engine,
        "ZYVOR_DB_SECRET": str(resolved_secret),
        "ZYVOR_DB_QUERY": query,
        "ZYVOR_DB_QUERY_PARAMS": _json.dumps(query_params),
        "ZYVOR_DB_ASSERTION": _json.dumps(assertion),
        "ZYVOR_DB_TIMEOUT_S": str(params["timeout_s"]),
    }

    get_store().audit(
        "db_assert.run", resource_type="db_assert", resource_id=target,
        detail={"engine": engine, "target": target, "query": query, "assertion": assertion},
    )
    log_progress(f"db_assert: running against {target} ({engine}, timeout {params['timeout_s']}s)…")
    result = sandbox.run_python(
        load_runner_script(), timeout_s=params["timeout_s"], env=env, image=image,
    )
    _check_cancel()

    verified, reason = _parse_verified_output(result.stdout)
    log_progress(
        f"db_assert: {'PASSED' if verified else 'FAILED'}"
        + (f" — {reason}" if reason else "")
        + (" (timed out)" if result.timed_out else "")
    )

    raised: list[dict[str, Any]] = []
    if not verified:
        # A failed assertion is a *test* failure, not a vulnerability
        # confirmation -- severity never defaults to critical/high the way
        # the pentest kinds do.
        title = f"db_assert failed on {target}: {reason or 'assertion not satisfied'}"
        findings.add("db_assert", "medium", title, detail=reason, url=target, category="db-assertion-failed")
        raised.append({"severity": "medium", "title": title, "detail": reason, "category": "db-assertion-failed"})

    hist = PipelineReport(
        summary=f"db_assert against {target}: {'passed' if verified else 'failed'}",
        passed=1 if verified else 0, failed=0 if verified else 1, total=1,
    )
    history.append_run(hist, source="dashboard-db-assert", duration_s=_time.time() - t0)

    return {
        "target": target, "engine": engine, "query": query, "assertion": assertion,
        "passed": verified, "reason": reason, "timed_out": result.timed_out,
        "exit_code": result.exit_code, "stdout": (result.stdout or "")[:2000], "findings": raised,
    }


def _extract_error_bodies(control_kind: str, control_result: dict[str, Any]) -> list[str]:
    """Best-effort per-case error text for verdict.py's stack-trace check.
    Only 'flow' exposes this at the level _job_flow returns it
    (flow_steps[].error) -- 'smoke' results don't carry per-case error text
    at this layer, so this returns [] for it rather than guessing.
    error_rate (the primary resilience criterion) is computed correctly for
    both kinds from passed/failed/total regardless."""
    if control_kind == "flow":
        return [
            step.get("error", "") for step in control_result.get("flow_steps", [])
            if step.get("status") != "passed" and step.get("error")
        ]
    return []


def _job_chaos_inject(params: dict[str, Any]) -> dict[str, Any]:
    """Client-side fault injection: shapes the sandbox pod's OWN egress
    toward the target (tc netem for latency/packet_loss, iptables for
    connection_reset/dependency_timeout) while a concurrently-run control
    test (flow/smoke) observes behavior. Never touches the target's own
    infrastructure -- real target-cluster pod-kill/infra chaos is
    explicitly out of scope (see _job_chaos_webhook for that case, and
    ROADMAP.md for the full reasoning). Three gates, checked in
    _validate(): exploit-tier engagement, ZYVOR_CHAOS_INJECTION_ENABLED,
    and a per-run target_accepts_fault_injection attestation."""
    import threading
    import time as _time
    from urllib.parse import urlsplit

    from agents.chaos.inject_script import build_injection_script
    from agents.chaos.probe import measure_latency_s, measure_recovery_s
    from agents.chaos.verdict import assess_resilience
    from agents.common.models import PipelineReport
    from orchestrator.dashboard import findings, history
    from orchestrator.security import sandbox

    t0 = _time.time()
    url = params["url"]
    fault_type = params["fault_type"]
    duration_s = params["duration_s"]
    control_kind = params["control_kind"]
    control_params = params["control_params"]
    insecure = params.get("insecure", False)

    if not sandbox.available():
        raise RuntimeError(
            "exploit sandbox unavailable — set ZYVOR_SANDBOX_NAMESPACE and ensure "
            "the cluster is reachable (see kubernetes/sandbox.yaml)"
        )
    image = sandbox.chaos_image()
    if not image:
        raise RuntimeError(
            "chaos_inject needs a sandbox image with iproute2/iptables installed — "
            "set ZYVOR_SANDBOX_CHAOS_IMAGE (see docs/enterprise-v2.md)"
        )

    parsed = urlsplit(url)
    target_host = parsed.hostname or url
    target_port = parsed.port or (443 if parsed.scheme == "https" else 80)

    log_progress(f"chaos_inject: measuring baseline latency for {url}")
    baseline_s = measure_latency_s(url, insecure=insecure) or 1.0

    script = build_injection_script(
        fault_type=fault_type, target_host=target_host, target_port=target_port,
        latency_ms=params["latency_ms"], packet_loss_pct=params["packet_loss_pct"], duration_s=duration_s,
    )

    sandbox_holder: dict[str, Any] = {}

    def _run_fault() -> None:
        sandbox_holder["result"] = sandbox.run_chaos(
            script, timeout_s=duration_s + 30, egress_hosts=[target_host], image=image,
        )

    fault_thread = threading.Thread(target=_run_fault)
    fault_thread.start()
    _time.sleep(min(3, duration_s / 4))  # let the fault actually apply before observing
    log_progress(f"chaos_inject: fault active ({fault_type}), running control test {control_kind}…")
    control_result = _JOBS[control_kind](control_params)
    _check_cancel()
    fault_thread.join(timeout=duration_s + 60)
    sandbox_result = sandbox_holder.get("result")

    log_progress("chaos_inject: measuring recovery…")
    recovery_s = measure_recovery_s(url, baseline_s, max_wait_s=params["recovery_sla_s"] * 2, insecure=insecure)

    total = control_result.get("total", 0) or 0
    failed = control_result.get("failed", 0) or 0
    error_rate_pct = (100.0 * failed / total) if total else 0.0
    error_bodies = _extract_error_bodies(control_kind, control_result)

    graceful, reason = assess_resilience(
        error_rate_pct=error_rate_pct, error_rate_threshold_pct=params["error_rate_threshold_pct"],
        recovery_s=recovery_s, recovery_sla_s=params["recovery_sla_s"], error_bodies=error_bodies,
    )
    log_progress(f"chaos_inject: {'graceful degradation' if graceful else 'resilience gap'} — {reason}")

    raised: list[dict[str, Any]] = []
    if not graceful:
        title = f"resilience gap under {fault_type} fault on {url}: {reason}"
        severity = "high" if recovery_s is None else "medium"
        findings.add("chaos_inject", severity, title, detail=reason, url=url, category="resilience-gap")
        raised.append({"severity": severity, "title": title, "detail": reason, "category": "resilience-gap"})

    hist = PipelineReport(
        summary=f"chaos_inject ({fault_type}) on {url}: {'graceful' if graceful else 'resilience gap'}",
        passed=1 if graceful else 0, failed=0 if graceful else 1, total=1,
    )
    history.append_run(hist, source="dashboard-chaos-inject", duration_s=_time.time() - t0)

    return {
        "url": url, "fault_type": fault_type, "graceful": graceful, "reason": reason,
        "error_rate_pct": round(error_rate_pct, 1), "recovery_s": recovery_s, "baseline_s": round(baseline_s, 3),
        "control_kind": control_kind, "control_result": {"passed": control_result.get("passed"), "failed": failed, "total": total},
        "sandbox_stdout": (sandbox_result.stdout if sandbox_result else "")[:2000],
        "network_policy_applied": sandbox_result.network_policy_applied if sandbox_result else False,
        "findings": raised,
    }


def _job_chaos_webhook(params: dict[str, Any]) -> dict[str, Any]:
    """User-triggered chaos: POSTs to the user's OWN chaos-
    experiment webhook (Chaos Mesh/Litmus/etc.), waits `settle_s`, runs the
    control test, optionally POSTs a stop-webhook. Zero new sandbox
    capability -- reuses the same job-composition pattern
    _job_import_codegen already uses calling _job_flow internally. Same
    three gates as chaos_inject, checked in _validate()."""
    import time as _time

    import httpx

    from agents.chaos.probe import measure_latency_s, measure_recovery_s
    from agents.chaos.verdict import assess_resilience
    from agents.common.models import PipelineReport
    from orchestrator.dashboard import findings, history

    t0 = _time.time()
    url = params["url"]
    experiment_url = params["experiment_webhook_url"]
    stop_url = params.get("experiment_stop_webhook_url") or ""
    control_kind = params["control_kind"]
    control_params = params["control_params"]
    insecure = params.get("insecure", False)

    log_progress(f"chaos_webhook: measuring baseline latency for {url}")
    baseline_s = measure_latency_s(url, insecure=insecure) or 1.0

    log_progress(f"chaos_webhook: triggering experiment at {experiment_url}")
    with httpx.Client(verify=not insecure, timeout=15) as client:
        response = client.post(experiment_url)
        response.raise_for_status()
    _check_cancel()

    log_progress(f"chaos_webhook: settling {params['settle_s']}s before observing…")
    _time.sleep(params["settle_s"])
    _check_cancel()

    log_progress(f"chaos_webhook: running control test {control_kind}…")
    control_result = _JOBS[control_kind](control_params)
    _check_cancel()

    if stop_url:
        log_progress(f"chaos_webhook: stopping experiment at {stop_url}")
        try:
            with httpx.Client(verify=not insecure, timeout=15) as client:
                client.post(stop_url)
        except Exception as exc:
            log_progress(f"chaos_webhook: stop-webhook call failed (non-fatal): {str(exc)[:200]}")

    log_progress("chaos_webhook: measuring recovery…")
    recovery_s = measure_recovery_s(url, baseline_s, max_wait_s=params["recovery_sla_s"] * 2, insecure=insecure)

    total = control_result.get("total", 0) or 0
    failed = control_result.get("failed", 0) or 0
    error_rate_pct = (100.0 * failed / total) if total else 0.0
    error_bodies = _extract_error_bodies(control_kind, control_result)

    graceful, reason = assess_resilience(
        error_rate_pct=error_rate_pct, error_rate_threshold_pct=params["error_rate_threshold_pct"],
        recovery_s=recovery_s, recovery_sla_s=params["recovery_sla_s"], error_bodies=error_bodies,
    )
    log_progress(f"chaos_webhook: {'graceful degradation' if graceful else 'resilience gap'} — {reason}")

    raised: list[dict[str, Any]] = []
    if not graceful:
        title = f"resilience gap under user-triggered chaos on {url}: {reason}"
        severity = "high" if recovery_s is None else "medium"
        findings.add("chaos_webhook", severity, title, detail=reason, url=url, category="resilience-gap")
        raised.append({"severity": severity, "title": title, "detail": reason, "category": "resilience-gap"})

    hist = PipelineReport(
        summary=f"chaos_webhook on {url}: {'graceful' if graceful else 'resilience gap'}",
        passed=1 if graceful else 0, failed=0 if graceful else 1, total=1,
    )
    history.append_run(hist, source="dashboard-chaos-webhook", duration_s=_time.time() - t0)

    return {
        "url": url, "graceful": graceful, "reason": reason,
        "error_rate_pct": round(error_rate_pct, 1), "recovery_s": recovery_s, "baseline_s": round(baseline_s, 3),
        "control_kind": control_kind, "control_result": {"passed": control_result.get("passed"), "failed": failed, "total": total},
        "findings": raised,
    }


def _job_port_scan(params: dict[str, Any]) -> dict[str, Any]:
    import time as _time

    from agents.common.models import PipelineReport
    from agents.probes.port_scan import run_port_scan
    from orchestrator.dashboard import history

    t0 = _time.time()
    url = params["url"]
    log_progress(f"port_scan: {url}")
    data = run_port_scan(
        url,
        ports=params.get("ports"),
        timeout_s=float(params.get("timeout_s") or 1.0),
        log=log_progress,
    )
    _check_cancel()
    hist = PipelineReport(
        summary=f"port_scan {data.get('host')}: {len(data.get('open_ports') or [])} open",
        passed=0 if data.get("open_ports") else 1,
        failed=1 if data.get("open_ports") else 0,
        total=1,
    )
    history.append_run(hist, source="dashboard-port-scan", duration_s=_time.time() - t0)
    raised = _auto_findings("port_scan", url, data)
    return {"url": url, "findings": raised, **data}


def _job_tls_cipher_scan(params: dict[str, Any]) -> dict[str, Any]:
    import time as _time

    from agents.common.models import PipelineReport
    from agents.probes.tls_cipher_scan import run_tls_cipher_scan
    from orchestrator.dashboard import history

    t0 = _time.time()
    url = params["url"]
    log_progress(f"tls_cipher_scan: {url}")
    data = run_tls_cipher_scan(url, port=params.get("port"), log=log_progress)
    _check_cancel()
    hist = PipelineReport(
        summary=f"tls_cipher_scan grade={data.get('grade')} issues={len(data.get('issues') or [])}",
        passed=0 if data.get("issues") else 1,
        failed=1 if data.get("issues") else 0,
        total=1,
    )
    history.append_run(hist, source="dashboard-tls-cipher-scan", duration_s=_time.time() - t0)
    raised = _auto_findings("tls_cipher_scan", url, data)
    return {"url": url, "findings": raised, **data}


def _job_dast_scan(params: dict[str, Any]) -> dict[str, Any]:
    import time as _time

    from agents.common.models import PipelineReport
    from agents.probes.dast_scan import run_dast_scan
    from orchestrator.dashboard import history

    t0 = _time.time()
    url = params["url"]
    log_progress(f"dast_scan: {url}")
    data = run_dast_scan(
        url,
        insecure=params.get("insecure", False),
        max_requests=params.get("max_requests", 40),
        timeout_s=params.get("timeout_s", 120),
        modules=params.get("modules"),
        log=log_progress,
    )
    _check_cancel()
    total = int(data.get("total") or 0)
    hist = PipelineReport(
        summary=f"dast_scan: {total} finding(s)",
        passed=0 if total else 1, failed=1 if total else 0, total=1,
    )
    history.append_run(hist, source="dashboard-dast-scan", duration_s=_time.time() - t0)
    raised = _auto_findings("dast_scan", url, data)
    return {"url": url, "findings": raised, **data}


def _job_injection_scan(params: dict[str, Any]) -> dict[str, Any]:
    import time as _time

    from agents.common.models import PipelineReport
    from agents.probes.injection_scan import run_injection_scan
    from orchestrator.dashboard import history

    t0 = _time.time()
    url = params["url"]
    data = run_injection_scan(
        url, insecure=params.get("insecure", False),
        max_requests=params.get("max_requests", 40), log=log_progress,
    )
    _check_cancel()
    n = len(data.get("findings") or [])
    history.append_run(
        PipelineReport(summary=f"injection_scan: {n} finding(s)", passed=0 if n else 1, failed=1 if n else 0, total=1),
        source="dashboard-injection-scan", duration_s=_time.time() - t0,
    )
    raised = _auto_findings("injection_scan", url, data)
    return {"url": url, "findings": raised, **data}


def _job_csrf_probe(params: dict[str, Any]) -> dict[str, Any]:
    import time as _time

    from agents.common.models import PipelineReport
    from agents.probes.csrf_probe import run_csrf_probe
    from orchestrator.dashboard import history

    t0 = _time.time()
    url = params["url"]
    data = run_csrf_probe(url, insecure=params.get("insecure", False), log=log_progress)
    _check_cancel()
    n = len(data.get("findings") or [])
    history.append_run(
        PipelineReport(summary=f"csrf_probe: {n} finding(s)", passed=0 if n else 1, failed=1 if n else 0, total=1),
        source="dashboard-csrf-probe", duration_s=_time.time() - t0,
    )
    raised = _auto_findings("csrf_probe", url, data)
    return {"url": url, "findings": raised, **data}


def _job_ssrf_probe(params: dict[str, Any]) -> dict[str, Any]:
    import time as _time

    from agents.common.models import PipelineReport
    from agents.probes.ssrf_probe import run_ssrf_probe
    from orchestrator.dashboard import history

    t0 = _time.time()
    url = params["url"]
    data = run_ssrf_probe(
        url, insecure=params.get("insecure", False),
        param=params.get("param"), log=log_progress,
    )
    _check_cancel()
    n = len(data.get("findings") or [])
    history.append_run(
        PipelineReport(summary=f"ssrf_probe: {n} finding(s)", passed=0 if n else 1, failed=1 if n else 0, total=1),
        source="dashboard-ssrf-probe", duration_s=_time.time() - t0,
    )
    raised = _auto_findings("ssrf_probe", url, data)
    return {"url": url, "findings": raised, **data}


def _job_auth_attack_scan(params: dict[str, Any]) -> dict[str, Any]:
    import time as _time

    from agents.common.models import PipelineReport
    from agents.probes.auth_attack_scan import run_auth_attack_scan
    from orchestrator.dashboard import history

    t0 = _time.time()
    url = params["url"]
    data = run_auth_attack_scan(
        url, insecure=params.get("insecure", False),
        login_url=params.get("login_url"), log=log_progress,
    )
    _check_cancel()
    n = len(data.get("findings") or [])
    history.append_run(
        PipelineReport(summary=f"auth_attack_scan: {n} finding(s)", passed=0 if n else 1, failed=1 if n else 0, total=1),
        source="dashboard-auth-attack-scan", duration_s=_time.time() - t0,
    )
    raised = _auto_findings("auth_attack_scan", url, data)
    return {"url": url, "findings": raised, **data}


def _job_idor_scan(params: dict[str, Any]) -> dict[str, Any]:
    import time as _time

    from agents.common.models import PipelineReport
    from agents.probes.idor_scan import run_idor_scan
    from orchestrator.dashboard import history

    t0 = _time.time()
    url = params["url"]
    data = run_idor_scan(
        url, insecure=params.get("insecure", False),
        cookie=params.get("cookie") or "",
        authorization=params.get("authorization") or "",
        delta=params.get("delta", 1),
        log=log_progress,
    )
    _check_cancel()
    n = len(data.get("findings") or [])
    history.append_run(
        PipelineReport(summary=f"idor_scan: {n} finding(s)", passed=0 if n else 1, failed=1 if n else 0, total=1),
        source="dashboard-idor-scan", duration_s=_time.time() - t0,
    )
    raised = _auto_findings("idor_scan", url, data)
    return {"url": url, "findings": raised, **data}


def _job_select_tests(params: dict[str, Any]) -> dict[str, Any]:
    """Change-based test selection — static, no live target."""
    from orchestrator.intelligence.select import select_from_git

    log_progress(f"select_tests: {params.get('base')}...{params.get('head')}")
    result = select_from_git(
        base=params.get("base") or "HEAD~1",
        head=params.get("head") or "HEAD",
        include_quarantined=bool(params.get("include_quarantined")),
    )
    log_progress(
        f"select_tests: {len(result['changed'])} changed, "
        f"{len(result['selected_files'])} selected, grep={result['grep'] or '-'}"
    )
    return result


_JOBS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "select_tests": _job_select_tests,
    "smoke": _job_smoke,
    "flow": _job_flow,
    "route_sweep": _job_route_sweep,
    "api_contract": _job_api_contract,
    "api_contract_diff": _job_api_contract_diff,
    "contract_verify": _job_contract_verify,
    "vitals": _job_vitals,
    "realtime": _job_realtime,
    "auth_test": _job_auth_test,
    "ai_flow": _job_ai_flow,
    "har_replay": _job_har_replay,
    "import_codegen": _job_import_codegen,
    "full": _job_full,
    "generate": _job_generate,
    "discover": _job_discover,
    "create": _job_create,
    "regression": _job_regression,
    "crawl_test": _job_crawl_test,
    "audit": _job_audit,
    "flaky": _job_flaky,
    "screenshot": _job_screenshot,
    "compare": _job_compare,
    "ping": _job_ping,
    "loadtest": _job_loadtest,
    "tls": _job_tls,
    "misconfig_scan": _job_misconfig_scan,
    "cve_lookup": _job_cve_lookup,
    "sca_scan": _job_sca_scan,
    "llm_redteam": _job_llm_redteam,
    "exploit_poc": _job_exploit_poc,
    "attack_chain": _job_attack_chain,
    "host_pentest": _job_host_pentest,
    "cloud_pentest": _job_cloud_pentest,
    "db_assert": _job_db_assert,
    "chaos_inject": _job_chaos_inject,
    "chaos_webhook": _job_chaos_webhook,
    "port_scan": _job_port_scan,
    "tls_cipher_scan": _job_tls_cipher_scan,
    "dast_scan": _job_dast_scan,
    "injection_scan": _job_injection_scan,
    "csrf_probe": _job_csrf_probe,
    "ssrf_probe": _job_ssrf_probe,
    "auth_attack_scan": _job_auth_attack_scan,
    "idor_scan": _job_idor_scan,
}


def _make_probe_job(name: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def _job(params: dict[str, Any]) -> dict[str, Any]:
        from agents.probes.http_probes import PROBES

        kwargs = {k: v for k, v in params.items()}
        kwargs["log"] = log_progress
        return PROBES[name](**kwargs)

    return _job


for _pk in PROBE_KINDS:
    _JOBS[_pk] = _make_probe_job(_pk)
