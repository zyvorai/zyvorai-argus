# Roadmap

Known gaps and deliberate deferrals, consolidated in one place instead of
scattered across runbooks, docstrings, and CI config comments. This is
inventory, not a promise sheet — no dates, just what's open and where the
detail already lives.

## Test intelligence: flake quarantine, change-based selection, failure studio

First slice — **done.** File-backed on purpose (`reports/quarantine.json`), so this
does not grow `MissionControlStore` / `PostgresStore`'s public surface.

- Deterministic failure taxonomy (`orchestrator/intelligence/classify.py`):
  healthy / failing / flaky / selector / assertion / infra / data. Quarantine
  is recommended only after ≥3 runs with a mixed pass/fail history.
- Quarantine with TTL + owner + release/expiry. Never heals assertions —
  quarantine is an explicit operator action.
- Change-based selection from `git diff --name-only` + `requirement_test_links`
  + smoke fallback on product-path changes. Infra-only diffs do not enqueue
  a product suite.
- Failure studio: `GET /api/v2/intel/studio/{job_id}` joins the job's cases
  with on-disk video/trace presence and a classification.
- CLI: `argus intel health|select|quarantine-*`. Job kind `select_tests`.

**Heal-PRs — done** (`ENABLE_HEAL_PR`: a confirmed autofix repair opens a draft PR).

**Still named rather than silently skipped:** in-run locator healing,
Playwright Healer/Planner adapters, session-replay coverage clustering,
concurrent dashboard jobs, Watchfloor-lite multi-target switcher.

## New test-capability families: contract testing, chaos testing, database testing, compliance/SCA scanning

Shipped as four phases, all done. Each is an independently-shippable, real
slice, not a stub — see each phase's own section below for what it built,
what it deliberately left out, and exactly what was and wasn't live-verified
(the two credentialed/sandboxed phases, 3 and 4, are honest about the one
thing no Kubernetes cluster in this development environment could verify:
actual pod execution).

### ~~Phase 1: compliance signals + API contract diffing~~ — done

- **Compliance signal checks**, folded into the existing `misconfig_scan` job
  (no new job kind) — `agents/probes/misconfig_scan.py` gained
  `check_security_txt()` (RFC 9116 `/.well-known/security.txt` presence +
  required-field check), `check_consent_signals()` (heuristic scan for known
  consent-management-platform markers — explicitly not a legal/compliance
  determination), and `scan_pii_patterns()` (SSN-shaped and Luhn-valid
  credit-card-shaped strings in the response body — deliberately *not* a bare
  email-address scan, which would flag a normal contact address on almost
  every real site and be pure noise). New finding categories:
  `missing-security-txt` (low), `no-consent-mechanism` (low), `pii-exposure`
  (high). Live-verified against a real local HTTP server serving a page with
  a planted Luhn-valid card number and no security.txt/consent marker — all
  three correctly detected, with no duplicate findings (an early version
  double-counted the "security.txt not found" case; fixed and covered by a
  regression test).
- **`api_contract_diff`** — pure-Python OpenAPI breaking-change diff between
  two spec references (inline object, `http(s)://` URL, or
  `git:<ref>:<path>` resolved against this repo's own checkout via `git
  show`). New `agents/contract_diff/` package (`loader.py` + `engine.py`).
  Not gated by a security engagement (`ELEVATED_RISK_KINDS`) — pure static
  analysis, no live target interaction, same class as `import_codegen`.
  Classifies changes as `breaking` (removed endpoint/response-code/field,
  new required request param, request param type change, response field
  type change, enum value removed) or `non_breaking` (added
  endpoint/response-code/field, new optional param, removed param, enum
  value added). **Deliberately out of scope for this first slice, named
  rather than silently skipped:** no `oneOf`/`allOf`/`discriminator`
  semantic diffing, no vendor extensions, no `$ref`s that point outside the
  document being diffed (external file/URL refs resolve to an empty schema
  rather than erroring). Live-verified end to end through the real Mission
  Control UI (a new "API contract diff" card in the API panel) against a
  running `argus serve`: pasted two inline specs with a real breaking change
  (new required param + removed response field), clicked "Diff specs",
  confirmed both changes were correctly classified and landed as `high`
  severity findings in the real Findings panel — not just unit-tested.
  Separately verified the `git:` mode against a real file in this repo
  (`git show HEAD:package.json`).

### ~~Phase 2: consumer contract verification (`contract_verify`) + SCA scanning (`sca_scan`)~~ — done

- **`contract_verify`** — derives expectations from a recorded HAR (a new
  `agents/contract_verify/engine.py`, rule-based, not LLM: no ambiguity to
  resolve) and replays each against a live provider, diffing status,
  content-type, and top-level JSON key/type shape. Only HAR entries whose
  recorded response is `application/json` are considered — the rest of a
  full-page recording (HTML/CSS/JS/images) is noise for this purpose, not
  signal. Explicitly not Pact — no broker, no publish/subscribe, no contract
  versioning, no cross-team matrix, no "can-i-deploy" gating; the honest
  on-ramp, not the destination. Gated at `active_recon` tier (read-only
  against the target, same tier as `misconfig_scan`/`cve_lookup`). New
  `action-contract-verify` card in the API panel.
- **`sca_scan`** — two independent modes. Black-box: reuses `cve_lookup`'s
  `fingerprint_tech()` client-side library/version fingerprinting,
  cross-referenced against a small bundled `agents/probes/data/license_map.json`
  (dozens of well-known libraries, not a real SBOM) to flag copyleft/
  restricted licenses. Local-checkout: subprocess-wraps `pip-audit`
  (new optional `sca` extra in `pyproject.toml`) / `npm audit` against an
  operator-local `checkout_path` — never fetched over the network, so this
  mode alone needs no SSRF/engagement gating (a special-cased branch in
  `_validate()`'s elevated-risk enforcement: `sca_scan` without a `url` skips
  the engagement requirement entirely, unlike every other kind in
  `ELEVATED_RISK_KINDS`). Tool absence degrades to `skipped: True` with a
  reason, never a fabricated result.
  **Real bug caught in live verification and fixed**: the first
  implementation ran bare `pip-audit --format json` with no `-r`/project-path
  argument, which silently audits the *running* Python environment instead
  of the checkout being scanned — a deliberately-vulnerable
  `requests==2.19.1` fixture produced zero findings until this was caught
  and fixed (`-r requirements.txt` when present, the checkout path itself
  otherwise). New `action-sca` card in the Security panel.
  Live-verified end to end through the real Mission Control UI against a
  running `argus serve` for every mode: `contract_verify` against a real
  target server missing a HAR-recorded field (caught correctly, real
  `high`-severity finding); `sca_scan` local-checkout mode both without
  `pip-audit` on `PATH` (graceful `skipped` degradation) and with it (a real
  audit surfacing real transitive-dependency CVEs, e.g. `urllib3`); `sca_scan`
  black-box mode against a real server serving a WordPress generator tag
  (correctly flagged `GPL-2.0-or-later` as `copyleft-or-restricted`) and a
  real jQuery/MIT asset (correctly not flagged). New
  `tests/unit/test_contract_verify.py`, `test_sca_scan.py`; extended
  `test_jobs_validate.py`, `test_findings.py`.

### ~~Phase 3: `db_assert` (database/data-integrity testing)~~ — done

First real database access Argus has ever had. One job kind, read-only
`SELECT`-only assertion (`row_count` / `cell_equals` / `column_values`,
each with a comparison op) against `postgres`/`mysql`/`sqlite`.

- **`orchestrator/security/sql_guard.py`** — `validate_select_only()`,
  mirrors `target_policy.py`'s validate-before-execute shape: strips
  comments, rejects multi-statement input, requires a `SELECT`/`WITH ...
  SELECT` prefix, denylists mutating keywords anywhere in the body. A
  keyword/shape check, not a real SQL parser — documented explicitly as
  defense-in-depth, not a claim of being unbypassable. The real backstop is
  that the DB role behind the `db_secret` credential must itself be
  read-only, an IAM control outside Argus's ability to verify (same posture
  `host_pentest` takes toward SSH account scoping). A naming-convention
  safety net (e.g. requiring `-staging` in the DSN) was considered and
  rejected as security theater — trivially spoofable, false confidence.
- **`agents/db_assert/runner_script.py`** — deliberately **not**
  LLM-generated (unlike `exploit_poc`/`host_pentest`/`cloud_pentest`'s
  `poc_generator.py`): a single fixed script checked into the repo. The
  query and assertion are declarative data passed in via env vars, never
  embedded in generated code, so there's no per-run code to hash-and-audit
  — the auditable artifact is the query + assertion text themselves
  (`orchestrator/dashboard/jobs.py::_job_db_assert`'s audit call records
  them in full, unlike credential values).
- **Gating**: one explicit fail-closed opt-in
  (`ZYVOR_DB_TESTING_ENABLED=true`), not `exploit_poc`'s three-gate stack —
  this is read-only and makes no destructive claim, but does touch live
  data with real credentials, warranting more than the probe kinds get.
  `active_recon`-tier engagement (not `exploit`). New `sandbox.db_image()`
  (`ZYVOR_SANDBOX_DB_IMAGE`), same fail-closed pattern as
  `host_pentest_image()`/`cloud_pentest_image()`.
- **Deliberately deferred**: migration testing and seed/teardown fixtures,
  same "smaller honest slice, don't half-build the risky one" posture as
  `cloud_pentest` deferring AD/Kerberos tooling.

**Live-verified, with one honest gap.** No real Kubernetes cluster is
reachable in this development environment (same situation `cloud_pentest`
was built under — no cloud credentials were available for that pass
either), so the actual K8s sandbox execution of `db_assert` is
**unit-tested only** (mocked `sandbox.run_python`), not live-run inside a
real pod. Everything else *is* live-verified, for real:
- `sql_guard.validate_select_only()` — run directly against real
  accept/reject query strings.
- `agents/db_assert/runner_script.py` — the actual script that would run
  inside the sandbox, invoked exactly as the sandbox invokes it
  (`python3 runner_script.py` as a subprocess with env vars), against both
  a real local SQLite database and a real local Postgres 14 instance: row
  count / cell / column-values assertions passing and failing correctly, a
  connection failure degrading to `VERIFIED: false` instead of crashing,
  and confirmed a planted secret value never appears in the script's
  stdout even on a connection error.
- The full `_validate()` → engagement-check → `_job_db_assert` dispatch
  chain, through a real browser driving the real Mission Control UI against
  a running `argus serve`: created a real engagement via the API, filled
  the new "DB assert" card, clicked "Run assertion", and confirmed the
  request reaches exactly the expected `RuntimeError` at the one boundary
  that genuinely needs a cluster — not a validation error, not a crash.
  This incidentally also confirmed something the unit tests didn't cover
  directly: `db_secret` shows as `"***"` in the persisted job's params via
  the real `/api/dashboard/jobs/status` endpoint, i.e. redaction holds at
  the store/API level, not just inside the job function.

New: `orchestrator/security/sql_guard.py`, `agents/db_assert/`
(`runner_script.py`, `engine.py`), new "DB assert" card in the Security
panel. Tests: `tests/unit/test_sql_guard.py`, `test_db_assert_runner.py`
(includes real-subprocess-against-real-SQLite cases), `test_db_assert_job.py`
(mirrors `test_pentest_jobs.py`'s secret-non-leak shape); extended
`test_jobs_validate.py`.

**Also noticed during this phase, fixed in a follow-up pass:**
`cloud_pentest`'s Mission Control card didn't actually exist —
`jobParams("cloud_pentest")` in `templates/dashboard.html.j2` referenced
DOM elements (`cp-eng`, `cp-creds`, `cp-provider`, `cp-target`, `cp-finding`,
`cp-timeout`) that weren't present anywhere in the template, only reachable
via the command palette entry which hit the same missing elements.
Added the `action-cloud-pentest` card to the Security panel (same shape as
`action-host-pentest`, right above it). Live-verified against a real
`argus serve` instance with `ZYVOR_CREDENTIALED_PENTEST_ENABLED=true`: opened
the dashboard in a real browser, confirmed the card renders and scrolls into
view, filled all six fields (including a `$secret` env-ref for
`secret_access_key`) and selected a real `exploit`-tier engagement, confirmed
`jobParams("cloud_pentest")` builds the exact expected params object with the
secret ref intact, then clicked through `startJob("cloud_pentest")` and
confirmed via `/api/dashboard/jobs/status` it reaches the same
"exploit sandbox unavailable" `RuntimeError` boundary as `host_pentest`/
`db_assert`/`chaos_inject` in this cluster-less dev environment — and that the
persisted job params show `secret_access_key: "***"`, i.e. redaction holds
end-to-end through the real UI path, not just in unit tests.

### ~~Phase 4: `chaos_inject` / `chaos_webhook` (fault-injection testing)~~ — done

The highest-risk phase, as planned: `chaos_inject` needed a new, narrow
`sandbox.run_chaos()` entry point granting `CAP_NET_ADMIN` (for `tc`/
`iptables` fault shaping) — a deliberate, callout-worthy exception to the
sandbox's normal "drop ALL capabilities" invariant. `_run_job()` (the
shared implementation `run_python()`/`run_chaos()` both now call) takes an
`extra_capabilities` parameter that only `run_chaos()` ever passes a
non-`None` value for — `run_python()` still drops everything, unchanged,
confirmed by a dedicated test that inspects the actual `V1SecurityContext`
each function builds. Real target-cluster pod-kill/infra chaos is
explicitly out of scope — Argus never holds privileged access to a
user's own cluster; `chaos_webhook` covers that case by triggering the
*user's own* chaos tooling (Chaos Mesh/Litmus) instead, needing zero
new sandbox capability.

- **`agents/chaos/verdict.py::assess_resilience()`** — deterministic
  rubric, no LLM call (there's no ambiguity to resolve): graceful
  degradation means no raw stack trace leaked, error rate under an
  operator-set threshold, and latency recovered to baseline within an
  operator-set SLA. Every violated criterion is named in the result, not
  just the first one found.
- **`agents/chaos/probe.py`** — plain HTTP latency/recovery-time
  measurement, shared by both job kinds. No privileged operations, so
  unlike the fault-injection mechanism itself this is fully live-testable.
- **`agents/chaos/inject_script.py`** — mechanical (not LLM-generated)
  `tc netem`/`iptables` script builder for `chaos_inject`'s four fault
  types. **Two real bugs were caught and fixed during development**, both
  found by actually running the generated script's control flow (with the
  real `tc`/`iptables` commands swapped for a harmless no-op — the real
  commands were never executed anywhere, on principle: this development
  machine's own network is not an authorized target regardless of cluster
  availability): (1) a bare foreground `sleep N` is **not** interruptible
  by a trap in `sh` — the trap only runs once the foreground command
  completes, so an early kill signal would have silently waited out the
  *entire* fault duration before tearing anything down, exactly the
  opposite of the "guaranteed teardown" the script's own comment claimed.
  Fixed by backgrounding the sleep and `wait`-ing on it, which makes the
  wait interruptible. (2) The trap firing doesn't by itself stop an
  already-backgrounded sibling process — the sleep child was left running
  as an orphan even after teardown "completed". Fixed by having the trap
  explicitly `kill` it. Both are locked in by a regression test that sends
  a real `SIGTERM` mid-run and asserts teardown happens within seconds, not
  after the full fault duration.
- **Gating**: `exploit`-tier engagement (both kinds), `ZYVOR_CHAOS_INJECTION_ENABLED`
  opt-in, and a third, per-run `target_accepts_fault_injection` attestation
  — distinct from the other two because it's a confirmation *this specific
  target* consented, not operator-level policy. `control_kind` is
  allowlisted to `{flow, smoke}` and its params are validated by
  recursively calling `_validate()` for that kind — passing e.g.
  `exploit_poc` as the "control" is rejected outright, not silently
  accepted. `latency_ms`/`packet_loss_pct`/`duration_s` are hard-capped
  server-side, not relaxable via params.

**Live-verified, with one honest gap** (same shape as Phase 3's): no real
Kubernetes cluster is reachable in this development environment, so
`chaos_inject`'s actual `run_chaos()` execution against a real pod is
unit-tested only (a mocked K8s client that inspects the real `V1Job`/
`V1SecurityContext` objects built, confirming the `CAP_NET_ADMIN` grant is
correct). Everything else *is* live-verified, for real:
- `assess_resilience()` and `looks_like_stack_trace()` against synthetic
  data covering every branch.
- `probe.py`'s latency/recovery measurement against a real local HTTP
  server with a genuine, timed slow-then-fast fault window (not just the
  trivial already-fast case).
- `inject_script.py`'s generated shell — syntax-checked for all four fault
  types, and its control-flow (trap/background/wait/teardown) actually
  executed as a real subprocess, including sending it a real `SIGTERM`
  mid-run (the regression test for the two bugs above).
- `chaos_webhook` end to end, with **zero mocking of the actual mechanics**:
  a real local mock chaos-experiment webhook server (confirmed it received
  real `/start` and `/stop` POSTs), a real target HTTP server, and a real
  Playwright browser running the `flow` control test — both the graceful
  (2/2 steps passed, no finding) and resilience-gap (steps failed, `high`-
  severity finding raised) outcomes, run directly and again through the
  real Mission Control UI against a running `argus serve`.
- `chaos_inject`'s full `_validate()` → engagement-check → dispatch chain,
  including all three gates, the `control_kind` allowlist rejecting
  `exploit_poc`, and every param hard-cap — through both a direct call and
  the real UI — confirmed it fails at exactly the expected
  sandbox-unavailable boundary, the one point that genuinely needs a
  cluster.

New: `agents/chaos/` (`verdict.py`, `probe.py`, `inject_script.py`), new
`sandbox.run_chaos()`/`chaos_image()`, new "Chaos inject"/"Chaos webhook"
cards in the Security panel. Tests: `tests/unit/test_chaos_verdict.py`,
`test_chaos_probe.py`, `test_chaos_inject_script.py`, `test_chaos_jobs.py`;
extended `test_sandbox.py`, `test_jobs_validate.py`.

**With this, all four phases of the "make Argus one-stop" plan are
done** — see the top of this section for what each phase covers and what
each deliberately left out.

## Test coverage — in progress

Overall unit-test coverage (as measured by the CI gate's own command,
`--cov=orchestrator --cov=agents`) moved from ~33% to ~45.2% (510 tests)
across several passes: the validation/state-layer pass below, the
security-testing feature pass (`jobs.py`'s new `exploit_poc`/
`attack_chain`/`host_pentest`/`cloud_pentest`/`misconfig_scan`/
`cve_lookup`/`llm_redteam` validation and state paths), then a dedicated
pass closing out every remaining gap in `orchestrator/security/` — the
whole package is now **100% covered**, all 11 modules. Most of these had
zero direct tests going in, only incidental coverage from routes/jobs
that happened to call through them:

- `rbac.py` — token/session identification and scope-enforcement gating
  every `/api/v2` route: 58% → 100% (`tests/unit/test_security_rbac.py`).
- `secrets.py` — the secret-reference guard/resolver used for durable
  schedules, queued jobs, and the `host_pentest`/`cloud_pentest`
  credential path (`{"$secret": "env:NAME"}` / `{"$secret": "file:/path"}`):
  67% → 100% (`tests/unit/test_security_secrets.py`).
- `agent_policy.py` — the deterministic enforcement point for autonomous
  browser actions (prompt-injection detection, risk classification,
  cross-origin navigation gating): 76% → 100%
  (`tests/unit/test_agent_policy.py`).
- `target_policy.py` — the SSRF-resistant target-URL/host validator used
  by every browser/API/network job: 82% → 100%
  (`tests/unit/test_target_policy.py`).
- `sandbox.py` — the Kubernetes-Job sandbox that runs LLM-generated
  exploit/PoC code (`exploit_poc`/`attack_chain`/`host_pentest`/
  `cloud_pentest`): 77% → 100%, including the egress-NetworkPolicy
  apply/cleanup paths and every cleanup-failure-is-swallowed branch
  (`tests/unit/test_sandbox.py`).
- `config.py`, `webhook.py`, `redaction.py`, `rate_limit.py`, `slack.py` —
  each had a small handful of untested branches (a missing/malformed
  timestamp, a max-recursion-depth guard, an inline stale-entry trim
  racing the periodic prune sweep, ...); all closed to 100%
  (`tests/unit/test_security_config_webhook_metrics.py`,
  `tests/unit/test_security_redaction.py`, `tests/unit/test_rate_limit.py`,
  `tests/unit/test_slack_security.py`).

Each of these modules is individually small, so the whole-suite movement
from any one going to 100% is modest — it's the previously-open gaps in
security-critical code that mattered (SSRF validation, sandboxed exploit
execution, secret handling, RBAC), not the aggregate percentage.

- `orchestrator/dashboard/jobs.py` — 19% → 46%. `_validate()` (all job
  kinds, not just a handful) plus the state/dispatch layer (`log_progress`,
  `_stream_line*`, `cancel`/`status`, `trigger`/`_run`, `_brief`, `_slug`,
  `_explain_failure`, `_cases_payload`, `_env_overrides`,
  `_safe_local_spec` — including a real path-traversal-rejection test) are
  now covered (`tests/unit/test_jobs_validate.py`,
  `tests/unit/test_jobs_state.py`, plus the new security-job test files).
- `orchestrator/cli.py` — 0% → 17%. `_initial_state`, `_load_env`,
  `_ensure_tls_cert` covered (`tests/unit/test_cli_helpers.py`).

**Deliberately still uncovered** in both files: most of the `_job_*` /
`@app.command()` functions themselves. They're thin wrappers that
immediately delegate to real subprocess/network calls (Playwright, crawl
scripts, TLS probes, HTTP probes) — meaningfully unit-testing them means
mocking `subprocess.run`/network I/O per job kind for a large effort-to-value
ratio, versus the validation/state layer above where bugs actually bite
(input validation, path safety, dispatch correctness) and where coverage is
now real.

After `orchestrator/security/` was fully closed out, the durable job
service got the same treatment since it's real orchestration logic (state
transitions, cancellation, retry/dead-letter handling), not a thin
subprocess wrapper:

- `orchestrator/dashboard/durable_jobs.py` — 17% → 100%
  (`tests/unit/test_durable_jobs.py`). The worker/scheduler loops are
  driven directly and synchronously in the test thread (a small
  `_FakeStopEvent` stand-in for `threading.Event` avoids any real
  wall-clock waiting between iterations) with a mocked store and a
  mocked `orchestrator.dashboard.jobs` module — covering the still-running
  vs. already-finished cancellation race, the busy-requeue path, and the
  exception-to-dead-job path, not just the happy path.
- `orchestrator/dashboard/scheduler.py` — 0% → 100%
  (`tests/unit/test_scheduler.py`) — the small backward-compatible
  wrapper the dashboard's legacy schedule API still calls through.

A few more small, previously-untested modules were closed to 100% in the
same pass: `orchestrator/dashboard/auth.py` (93% → 100%,
`tests/unit/test_auth.py`) — the login-rate-limiter's inline stale-entry
trims and expired-lockout cleanup, the explicit-`DASHBOARD_SECRET` path,
and the auth-disabled short-circuits in `is_authenticated`/`requires_auth`;
`orchestrator/enterprise.py` (91% → 100%, `tests/unit/test_enterprise.py`)
— `install_enterprise`'s startup/shutdown hooks, which no existing test
actually triggered since none of them built the app inside a `with
TestClient(app):` block; `orchestrator/slack_gateway.py` (93% → 100%,
extended `tests/unit/test_slack_gateway.py`) — the enqueue-failure reply
path; and `orchestrator/dashboard/activity.py` (67% → 100%, new
`tests/unit/test_activity.py`) — `record_webhook`/`recent`/`last_webhook`
had no direct test at all, only `record_job` was incidentally exercised
elsewhere.

`orchestrator/persistence/store.py` (83% → 100%, extended
`tests/unit/test_persistence_store.py`) — the SQLite `MissionControlStore`
backing everything above got the same direct treatment: `cancel_job`'s
three branches (queued → immediately cancelled, running → flagged but
left running, already-finished → `False`) had *no* direct test at all
despite being real, user-triggerable behavior; `heartbeat`,
`cancellation_requested`, `mark_cancelled`, `remove_schedule`,
`due_schedules`, `advance_schedule` (both the `ran=True`/`ran=False`
branches and the unknown-schedule no-op), `audit`/`list_audit`, and
`record_webhook_delivery`'s empty-delivery-id rejection were all the
same story. Also covers two real `enqueue_job` `IntegrityError` edge
cases (a forced `uuid4` collision with and without a distinguishing
idempotency key) and `claim_job`'s belt-and-suspenders guard against the
claiming `UPDATE` affecting zero rows, exercised via a thin connection
proxy that spoofs `rowcount=0` on that one statement rather than trying
to force a genuine SQLite-level race.

**Correction to the note above**: `orchestrator/nodes/*.py` turned out not
to belong in the same "low marginal value" bucket as `jobs.py`'s
`_job_*`/`cli.py`'s `@app.command()` wrappers. Those really are thin
wrappers around real subprocess/Playwright/network calls, expensive to
mock meaningfully. The 17 LangGraph pipeline nodes are the opposite: small,
pure `PipelineState -> PipelineState` transforms with real branching logic
(source routing, coverage-gap merging, quality-score persistence,
autofix-retry bookkeeping) sitting behind only one or two already-mockable
agent calls each. All 17 were brought from 17-47% to 93-100% coverage in a
single pass (`generate.py`, `parse.py`, `fetch.py`'s `github`/`local`
branches, `report.py`, `discover.py`, `gap_analyze.py`, `notify.py`,
`apply_autofix.py`, `autofix.py`, `analyze.py`, `execute.py` — `evaluate_quality.py`
was already 100% from the pass that added it), 65 new tests, moving the
whole-suite coverage metric from 47% to 50%. `jobs.py`'s `_job_*`/
`@app.command()` wrappers, `cli.py`, and `dashboard/k8s.py` remain the
genuinely low-marginal-value remainder — real subprocess/Playwright/network
calls, not logic worth unit-testing in isolation.

**Update**: the four test-capability phases above (`api_contract_diff`/
`contract_verify`/`sca_scan`/`db_assert`/`chaos_inject`/`chaos_webhook`) added
several heavily-tested new packages, moving measured whole-suite coverage to
**53.47%**. CI's `--cov-fail-under` raised 47 → 50 to match, keeping the same
~3-point cushion below the real floor as every prior raise (36 → 40 → 47 → 50).

The CI gate (`.github/workflows/security.yml`) enforces
`--cov-fail-under=40` (raised from 36, itself raised from an original,
never-actually-met 70% target) — still a deliberate few points below the
measured ~50% floor to leave headroom for minor cross-Python-version
coverage variance, with an inline `TODO` to keep raising it as coverage
grows rather than a plan to close the whole gap at once.

## Multi-source requirements: ticket/email/transcript connectors, deeper impact analysis

The requirements pipeline now supports two real sources — `github` (labeled
issues, PR bodies, spec files) and `document` (local files, incl. PDF, via
`knowledge/documents.py`) — plus durable versioned storage
(`orchestrator/persistence/store.py`'s `requirements`/`requirement_versions`
tables), LLM-driven quality/gap scoring (`agents/requirement_quality/`), and
first-cut traceability (`requirement_test_links`): a changed requirement
surfaces which previously-generated tests trace to its old version.

Deliberately scoped earlier slices are now complete:

- ~~**Ticket-system (Jira-like), email, and meeting-transcript sources**~~ —
  **done.** `agents/requirements_sources/`: `email` (`.eml` + IMAP),
  `transcript`, `jira` (JSON / REST / OAuth), `diarize` (speaker VTT +
  optional live audio). Wired through `fetch_requirements`, CLI `--source`,
  and Mission Control job validation.
- ~~**Business-flow/data-model impact analysis**~~ — **done.**
  The original change-based impact check ("which generated tests trace to a
  requirement that changed") is joined by shared data models/flows,
  co-occurrence `model_edges`, and typed `model_dependencies` (schema v6,
  Order → Payment) with an SVG canvas in Mission Control
  (`GET /api/v2/requirements/impact-graph`).
- ~~**A dashboard/UI surface for requirement history and quality scores**~~
  — **done, for Mission Control (OSS).** A new **Requirements** panel
  (`templates/dashboard.html.j2`, `data-panel="requirements"`) lists every
  requirement (title, source, latest version, color-coded quality score,
  last-updated), and a click-through detail view pulls `GET
  /api/v2/requirements/{id}` + `/{id}/history` for the description, named
  quality issues, and full version history — each version now also carries
  `linked_tests` (the route enriches `requirement_history()`'s response with
  `MissionControlStore.linked_tests()`, previously computed but never
  exposed over HTTP), so a version's generated tests show inline instead of
  needing a second lookup. Polls every 30s alongside the panel's siblings
  (findings, engagements). Live-verified in a real browser against a
  running `argus serve` (not just `TestClient`): seeded two requirements
  (one two-version history with a per-version linked test, one single-version
  with a quality issue) via a real `MissionControlStore`, confirmed the list
  renders, the detail drawer opens with the right content for both rows,
  quality-issue text and linked-test paths appear correctly, and the browser
  console is clean. New `tests/unit/test_requirements_route.py` case for the
  `linked_tests` enrichment. **Still open:** an Argus Enterprise/Watchfloor
  proxy route + panel (separate commercial product, not this repo).

## ~~Observability: tracing~~ — done (within-process and cross-replica)

`orchestrator/observability/metrics.py`'s Prometheus counters/gauges are now
joined by `orchestrator/observability/tracing.py`, opt-in via
`ZYVOR_OTEL_ENABLED=true` (off by default — zero cost, and the `otel` extra
isn't even imported unless enabled). Exports to `OTEL_EXPORTER_OTLP_ENDPOINT`
if set, otherwise a `ConsoleSpanExporter` for local debugging without a
collector. Two instrumentation points, wired without touching the ~19
individual node/job modules themselves:

- `orchestrator/graph.py`'s `_traced()` wraps every LangGraph pipeline node
  at registration time (`build_graph()`) in a `pipeline.<name>` span —
  `fetch`, `parse`, `evaluate_quality`, `generate`, `execute`, and every
  other node get real per-node timing and error status without any of them
  importing tracing themselves.
- `orchestrator/dashboard/durable_jobs.py`'s `_worker_loop` wraps each
  claimed job's execution in a `job.execute` span (`job_id`, `job_kind`,
  `job.status`, `job.duration_s` attributes; marked `ERROR` on failure).

**Live-verified**, not just unit-tested with a mocked SDK: ran a real
document-source pipeline slice (`fetch → parse → evaluate_quality`) with
tracing enabled and confirmed real `pipeline.fetch`/`pipeline.parse`/
`pipeline.evaluate_quality` spans on stdout; separately ran a real job
through `DurableJobService` end to end (enqueue → worker claims → executes
→ finishes) and confirmed a real `job.execute` span with correct attributes
(`job.status: succeeded`, real duration) came out the other side. New
`tests/unit/test_tracing.py` (no-op-when-disabled path, plus real span
emission/error-status assertions against an `InMemorySpanExporter`).

**Cross-replica propagation — done.** `jobs.trace_context` (schema v4, both
`MissionControlStore` and `PostgresStore`, `ALTER TABLE`-on-migrate for
existing databases) persists a serialized W3C `traceparent`.
`orchestrator/observability/tracing.py` gained two pieces:
`current_traceparent()` serializes the active span's context, and
`start_span(..., trace_context=...)` extracts a `traceparent` string back
into a parent context — `None`/omitted behaves exactly as before (parented
to whatever's locally active, or a new root span). `DurableJobService.enqueue()`
now wraps the call in a `job.enqueue` span, captures its `current_traceparent()`,
and passes it to `store.enqueue_job(trace_context=...)`; `_worker_loop` reads
it back off the claimed row and passes it as `job.execute`'s `trace_context`
— so a job enqueued on one replica and claimed on another links into one
trace instead of two independent ones.

**Live-verified**, not just unit-tested with a mocked SDK: ran the real
enqueue → persist → claim → execute sequence against both a real SQLite
`MissionControlStore` and a real local Postgres 14 instance, confirming
`job.execute`'s span shares `job.enqueue`'s `trace_id` and has its
`span_id` as `parent_id` in both cases — and separately ran the full
`DurableJobService.enqueue()` → worker-thread-claims → executes path
end to end (not the store methods in isolation) with the same assertion.
New tests: `tests/unit/test_tracing.py` (context-propagation cases),
`tests/unit/test_persistence_store.py`/`test_postgres_store.py`
(`trace_context` round-trips through enqueue/claim/get).

## ~~Horizontal scale: Postgres-backed store~~ — done

`MissionControlStore` (SQLite, `orchestrator/persistence/store.py`) is still
the default — single-writer, matching the current single-replica K8s
deployment. `PostgresStore` (`orchestrator/persistence/postgres_store.py`,
new `postgres` extra) implements the identical public method surface, so no
caller anywhere in the codebase needed to change — `get_store()` picks the
backend automatically from `ZYVOR_STATE_DB`'s scheme (`postgresql://...` →
Postgres, anything else → the existing SQLite path).

Two deliberate per-backend improvements rather than a literal line-by-line
port: `claim_job()` uses `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP
LOCKED) RETURNING *` — a single atomic statement, and unlike SQLite's
`BEGIN IMMEDIATE` (which serializes *all* claims globally), it lets
concurrent workers claim *different* jobs without blocking each other.
`upsert_requirement()` uses `INSERT ... ON CONFLICT DO NOTHING` for the
brand-new-id race (the loser falls through to `SELECT ... FOR UPDATE`,
which blocks until the winner commits, then reads its committed state) —
same external behavior as SQLite's version, serialized per-id rather than
table-wide.

**Live-verified** against a real local Postgres 14 instance (no Docker in
this environment, but Homebrew's `postgres`/`initdb`/`pg_ctl` binaries were
available): every method category (jobs, schedules, findings, engagements,
audit, webhook replay, requirements) end to end, plus two concurrency
scenarios specifically — 10 threads claiming 10 queued jobs (each job
claimed exactly once, no drops, no duplicates) and 20 threads racing
`upsert_requirement()` on the same brand-new id (0 errors, exactly 1 version
persisted). New `tests/unit/test_postgres_store.py`, skipped by default
(needs a real Postgres + the `postgres` extra) but wired into CI for real:
`.github/workflows/security.yml` gained a `postgres-quality` job running
against a genuine `postgres:16` service container, so this doesn't quietly
bit-rot untested between the rare times someone exercises it locally.

## Scheduler: single-flight, catch-up opt-in

Already documented operationally in
[`docs/devops/04-mission-control-ops.md`](docs/devops/04-mission-control-ops.md#3-scheduled-checks-optional):
by default schedules advance from wall-clock *now* (missed ticks are dropped),
and are for human alerting, not a substitute for the CI gate.

Opt-in behaviour (env):

- `ZYVOR_SCHEDULE_CATCHUP=true` — advance from previous `next_at + interval`
  (capped) so overdue schedules catch up instead of jumping forward from now.
- `ZYVOR_SCHEDULE_SKIP_IF_BUSY=true` — leave a due schedule pending while the
  in-process runner is busy (true skip-while-busy; default still enqueues and
  requeues via the durable worker).

## Desktop app v2: single-binary freeze

`desktop/` (Tauri 2 shell) ships a native macOS window around
`argus serve`, but v1 deliberately wraps an *existing* local install
(the repo's own `.venv`, or `argus` on `PATH`) rather than bundling a
self-contained runtime — see `desktop/README.md` and the plan that shipped
it. Two real blockers stand between that and a true single-binary/`.pkg`
distribution someone could install without a dev checkout:

- ~~**`_repo_root()` isn't frozen-binary-aware.**~~ **Done.** New
  `orchestrator/paths.py::repo_root()` checks `sys.frozen`/`sys._MEIPASS`
  first (PyInstaller's own frozen-app markers — the approach
  `hypercluster/cli/hypercluster.spec` uses for its own Python CLI),
  falling back to the real `Path(__file__).resolve().parents[1]` when not
  frozen. Every one of the 29 files that used to hand-roll
  `Path(__file__).resolve().parents[N]` locally (`webhook.py`, `cli.py`
  ×5, `routes.py`, `jobs.py`, `v2_routes.py` via `jobs.py`'s own
  `_repo_root()`, every `nodes/*.py`, and 14 files under `agents/*`) now
  delegates to it instead — the repo-wide audit this item asked for, not a
  packaging afterthought. Live-verified, not just unit-tested: simulated
  `sys.frozen=True`/`sys._MEIPASS=<fake bundle>` and confirmed both the
  shared helper and a real downstream consumer (`agents/reporter/agent.py`'s
  `_repo_root()`) resolve to the same fake bundle path instead of the real
  checkout. New `tests/unit/test_paths.py` (100% coverage). Doesn't by
  itself make the desktop app freezable — the other blocker below is
  untouched — but it removes the one blocker that was actually a code gap
  rather than a tooling/credential one. `scripts/validate_k8s_manifests.py`
  was deliberately left as-is — a CI-only hygiene script, never part of any
  shipped binary, frozen or otherwise.
- **Playwright's browser binaries aren't freezable.** They're downloaded
  separately via `npx playwright install` (hundreds of MB per browser), so
  even a fully frozen Python side would still need Node + Playwright
  bundled alongside it — closer in size/complexity to the existing
  `docker/Dockerfile` multi-stage build than to a lightweight desktop
  installer. **Practical path:** keep the desktop shell thin and point jobs
  at a remote Mission Control via Settings → **Remote URL** (lab/team host
  that already has Chromium), or use `docker/Dockerfile`. The desktop app
  does not need to freeze Playwright to be useful — see `desktop/README.md`.

Code signing + notarization config is wired up (`make desktop-build-signed`,
`desktop/README.md`'s "Code signing & notarization" section) but not
actually usable without an Apple Developer account's credentials, which
this session doesn't have — so it's configured, not done. Still fully
open: a Windows/NSIS build (hypercluster's `desktop-pkg-windows` target is
the template if this becomes worth doing).

## ~~Active exploitation~~ — done (PoC generation/execution, attack chaining, host/cloud pentesting)

Built in four stages during a security-testing feature pass rather than all
at once. Foundation: a general-purpose security-engagement authorization
primitive (`orchestrator/security/engagement_policy.py`,
`orchestrator/persistence/store.py`'s `engagements` table,
`POST/GET/DELETE /api/v2/engagements`) that gates elevated-risk job kinds
behind an admin-issued, target-scoped, tier-ranked attestation — mirroring
`orchestrator/security/agent_policy.py`'s mode/approved-risks/fail-closed-
in-production shape. Seven job kinds sit behind it: `misconfig_scan`,
`cve_lookup`, `llm_redteam` at the `active_recon` tier, and `exploit_poc`,
`attack_chain`, `host_pentest`, `cloud_pentest` at the `exploit` tier.

### ~~PoC generation/execution~~ — done, verification-only

`exploit_poc` (`orchestrator/dashboard/jobs.py`, `agents/exploit/
poc_generator.py`, `orchestrator/security/sandbox.py`) generates a
non-destructive verification script via LLM for a described finding, then
runs it — never in the job-runner process — as a short-lived Kubernetes Job
in a dedicated namespace (`kubernetes/sandbox.yaml`): dropped capabilities,
non-root, read-only rootfs, no ServiceAccount token, resource limits, a hard
wall-clock timeout. Gated by two independent things, not one: the citing
engagement must be `tier=exploit` (`active_recon` is rejected), *and*
`ZYVOR_EXPLOIT_EXECUTION_ENABLED=true` must be set — mirroring
`AgentPolicy`'s `allow_destructive` pattern, so an admin creating an
`exploit`-tier engagement alone can't turn this on by accident. If no
sandbox namespace is configured or the cluster is unreachable,
`sandbox.available()` returns false and the job refuses to run rather than
falling back to unsandboxed execution. The generated script's system prompt
constrains it to read-only requests, no floods/DoS, and a single
`VERIFIED: true/false - reason` output line grounded in a timing/response/
status-code difference — not a destructive payload. PoC source is written to
`reports/pocs/<run>/poc.py` with its SHA-256 logged to `audit_events`.

Network-egress restriction is attempted (a per-Job NetworkPolicy scoped to
the target's resolved IPs) but is explicitly best-effort: it only has real
effect on NetworkPolicy-enforcing CNIs (Calico, Cilium, EKS/GKE/AKS default
addons) — k3s's default Flannel CNI does not enforce NetworkPolicy at all,
so on a plain k3s cluster this specific layer is a no-op and the pod
security hardening above is what's actually holding. See
`kubernetes/sandbox.yaml`'s comments for the full caveat.

**Live-verified** against a real k3s cluster (not just the mocked-client
unit tests in `tests/unit/test_sandbox.py`/`test_exploit_poc_job.py`):
`sandbox.run_python()` genuinely creates a Job, runs code under the
hardened `securityContext`, retrieves its output, and tears everything down
— confirmed zero leftover Jobs/Pods/ConfigMaps across repeated runs. This
live pass also caught and fixed a real bug: the Kubernetes client
occasionally returns a pod's log as the `str()` of a `bytes` object rather
than a decoded string (the exact quirk `orchestrator/dashboard/k8s.py`
already works around for the dashboard's own log viewer —
`_normalize_log_text`, now reused by `sandbox.py` too). The LLM-generation
side (`poc_generator.py`) is unit-tested with a mocked model only; it
wasn't live-exercised against a real LLM provider in this pass.

### ~~Attack chaining~~ — done

`attack_chain` (`orchestrator/dashboard/jobs.py::_job_attack_chain`,
`agents/exploit/poc_generator.py::plan_next_chain_step`) repeatedly
plan-and-verifies one escalation step at a time — an LLM planner proposes
the next step given every step already confirmed, `poc_generator.py`
generates its verification script exactly as `exploit_poc` does, and it
runs through the identical sandboxed executor. The chain stops the moment a
step fails to verify or the planner has nothing safe left to propose
(capped at 5 steps either way) — it does not blindly retry or brute-force
past a failed step. Same two-gate authorization as `exploit_poc`
(`exploit`-tier engagement + `ZYVOR_EXPLOIT_EXECUTION_ENABLED`). A confirmed
multi-step chain raises an additional `critical`-severity finding
summarizing the full escalation path, on top of one `high`-severity finding
per individual confirmed step.

The sequential-execution mechanic (multiple `sandbox.run_python()` calls in
a row, each with its own Job/ConfigMap lifecycle) was live-verified against
the same k3s cluster — three consecutive runs, zero leftover resources, no
naming collisions. The LLM planning loop itself
(`plan_next_chain_step`/`generate_verification_poc` deciding what to
verify next) is unit-tested with a mocked model only, same caveat as above.

### ~~Credentialed host/cloud pentesting~~ — done (host SSH; cloud AD/WinRM not included)

`host_pentest` (SSH, via `paramiko`) and `cloud_pentest` (`aws`/`gcloud`/`az`
CLIs) close out the full NeuroSploit-inspired scope. Credentials are never
accepted as raw job params — `orchestrator/security/secrets.py`'s
`{"$secret": "env:..."}` reference pattern is required (enforced via
`assert_persistable()` in `_validate()`), resolved only at execution time,
and injected directly into the one ephemeral sandbox Job's environment —
never logged, never embedded in LLM-generated code, never present in the
job result (verified by dedicated unit tests, `tests/unit/
test_pentest_jobs.py`, that plant a real-looking secret value and assert it
never appears in the returned result or any audit-log call).

The default sandbox image (`python:3.12-slim`) has neither `paramiko` nor
the cloud CLIs, so `sandbox.py` gained an `image` override
(`ZYVOR_SANDBOX_HOST_IMAGE`/`ZYVOR_SANDBOX_CLOUD_IMAGE`) — both job kinds
fail closed with a clear error if the relevant image env var isn't set,
same "refuse rather than silently downgrade" posture as everywhere else in
this feature set. A **third**, independent opt-in —
`ZYVOR_CREDENTIALED_PENTEST_ENABLED=true` — gates these on top of
`exploit_poc`'s existing two gates (exploit-tier engagement +
`ZYVOR_EXPLOIT_EXECUTION_ENABLED`), since using real credentials against
real infrastructure is a materially bigger step than generating/running a
verification script against a URL.

**Live-verified** the custom-image mechanic against the real k3s cluster:
built a minimal `python:3.12-slim` + `paramiko` image, imported it into the
cluster, and ran it as a real sandboxed Job — which caught and fixed a real
bug (Kubernetes defaults `:latest`-tagged images to `imagePullPolicy:
Always`, so it tried to pull the locally-built image from a registry
instead of using what was already on the node; `sandbox.py` now sets
`IfNotPresent` explicitly). Did **not** live-test an actual SSH connection
end-to-end — that would have required adding a new key to the test host's
`authorized_keys`, a standing access-control change judged out of scope for
a one-off verification pass. `cloud_pentest` is code-complete and
unit-tested only; no cloud credentials were available in this pass to
verify it live.

Not included: Active Directory-specific tooling (Kerberos/LDAP enumeration,
WinRM) beyond generic SSH, and any lateral-movement/persistence logic — the
scope here is read-only enumeration and non-destructive verification, same
as every other job kind in this feature set.

## ~~CSRF~~ — done

Was flagged, then reconsidered as low-value (`SameSite=Lax` + `HttpOnly`
already covers most of it), then built anyway: double-submit-cookie CSRF
protection (`orchestrator/dashboard/auth.py`'s `csrf_token_for`/`csrf_valid`,
enforced in `orchestrator/webhook.py`'s `auth_middleware` for mutating
`/api/*` requests authenticated via the session cookie). The frontend side
is a single `window.fetch` wrapper in `templates/dashboard.html.j2` that
attaches `X-CSRF-Token` automatically — none of the ~20 existing `fetch()`
call sites needed touching individually. Covered by
`tests/unit/test_csrf_route.py` (real login → protected-route round trip)
and `tests/unit/test_auth.py`.

## ~~Network-attack / DAST coverage~~ — done (bounded; floods/MITM/AD still deferred)

Shipped eight engagement-gated job kinds that close the inventory in
[`docs/security-network-attack-gaps.md`](docs/security-network-attack-gaps.md):

- **Recon:** `port_scan` (bounded TCP connect), `tls_cipher_scan` (protocol +
  weak-cipher grading) at `active_recon`.
- **DAST / web-attack:** `dast_scan`, `injection_scan`, `csrf_probe`,
  `ssrf_probe`, `auth_attack_scan`, `idor_scan` at `exploit` **plus**
  `ZYVOR_DAST_SCAN_ENABLED=true`. `dast_scan` aggregates built-in templates
  and optionally shells out to nuclei when `ZYVOR_DAST_NUCLEI_BIN` (or
  `PATH`) provides a binary.

Still deliberately deferred (do not sneak into these jobs):

- Full-range port sweeps, packet capture, MITM, TLS stripping, ARP
- DDoS / unbounded load
- AD / Kerberos / LDAP / WinRM, lateral movement / persistence
- Security job kinds on MCP / Slack allowlists
- Credential stuffing / password spraying
