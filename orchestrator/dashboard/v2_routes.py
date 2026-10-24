# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
"""Versioned enterprise API for durable jobs, schedules, findings and audit."""

from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from orchestrator.dashboard.durable_jobs import get_service
from orchestrator.observability.metrics import render
from orchestrator.persistence.store import get_store
from orchestrator.security.rbac import require_scope
from orchestrator.security.target_policy import TargetPolicyError

router = APIRouter(prefix="/api/v2", tags=["mission-control-v2"])


def _request_context(request: Request) -> tuple[str, str, str]:
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    forwarded = request.headers.get("x-forwarded-for", "")
    client_ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "")
    return request_id, client_ip, request.url.path


@router.post("/jobs", status_code=202)
async def enqueue_job(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    identity = require_scope(request, "jobs:write")
    kind = str(payload.get("kind", "")).strip()
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise HTTPException(status_code=400, detail="params must be an object")
    try:
        return get_service().enqueue(
            kind,
            params,
            requested_by=identity.subject,
            idempotency_key=request.headers.get("idempotency-key") or payload.get("idempotency_key"),
            priority=max(1, min(int(payload.get("priority") or 100), 1000)),
            callback_url=str(payload["callback_url"]) if payload.get("callback_url") else None,
        )
    except (ValueError, TargetPolicyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/jobs")
async def list_jobs(request: Request, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    require_scope(request, "jobs:read")
    return {"jobs": get_store().list_jobs(limit)}


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: str) -> dict[str, Any]:
    require_scope(request, "jobs:read")
    job = get_store().get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.post("/jobs/{job_id}/cancel", status_code=202)
async def cancel_job(request: Request, job_id: str) -> dict[str, Any]:
    identity = require_scope(request, "jobs:write")
    changed = get_store().cancel_job(job_id)
    get_store().audit(
        "job.cancel", actor=identity.subject, resource_type="job", resource_id=job_id,
        outcome="success" if changed else "ignored",
    )
    if not changed:
        raise HTTPException(status_code=409, detail="job is absent or already complete")
    return {"cancel_requested": True, "job_id": job_id}


@router.get("/jobs/{job_id}/artifact")
async def get_job_artifact(request: Request, job_id: str, href: str = Query(...)) -> FileResponse:
    """Streams a test video/trace through the authenticated v2 API instead
    of the /reports static mount, whose own auth (dashboard.auth, gated on
    the unrelated legacy DASHBOARD_PASSWORD env var) has nothing to do with
    the RBAC/API-token model the rest of this API uses -- a caller
    authenticated via require_scope() here shouldn't depend on whether an
    operator happened to also set that separate password.

    href must be exactly one of the video/trace hrefs already present in
    this job's own persisted result (see jobs.py's _cases_payload), or --
    for a route_sweep job -- one of its sweep_rows' screenshot hrefs (see
    jobs.py's _job_route_sweep) -- never a client-supplied path, which is
    what actually prevents traversal here; resolving under reports_root is
    defense in depth on top of that, not the primary control."""
    require_scope(request, "jobs:read")
    job = get_store().get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    result = job.get("result") or {}
    known_hrefs = {
        case.get(key)
        for case in (result.get("cases") or [])
        for key in ("video", "trace")
        if case.get(key)
    }
    known_hrefs |= {row.get("cur") for row in (result.get("sweep_rows") or []) if row.get("cur")}
    if href not in known_hrefs:
        raise HTTPException(status_code=404, detail="artifact not recorded for this job")

    from orchestrator.dashboard.jobs import _repo_root

    reports_root = os.path.realpath(str(_repo_root() / "reports"))
    # href is allow-listed against the job result above; still contain the
    # joined path under reports/ (normalise, then prefix-check).
    rel = href.lstrip("/")
    if rel.startswith("reports/"):
        rel = rel[len("reports/") :]
    file_path = os.path.realpath(os.path.join(reports_root, rel))
    if not file_path.startswith(reports_root + os.sep):
        raise HTTPException(status_code=400, detail="invalid artifact path")
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="artifact file no longer on disk")
    return FileResponse(file_path)


@router.post("/jobs/{job_id}/route-sweep/approve")
async def approve_route_sweep_baseline(
    request: Request, job_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Promote one screenshot from a completed route_sweep job's own result
    to the saved baseline it's diffed against. The only existing way to
    update a baseline is re-running the whole sweep with
    update_baselines=true, which overwrites every route unconditionally
    with no author or timestamp recorded -- this is the reviewed,
    per-screenshot alternative (an audit entry records who approved what,
    when), matching what a "baseline approval" actually needs to mean.

    route/viewport must match a row already present in this job's own
    sweep_rows -- same "known-hrefs, not a client-supplied path" pattern
    get_job_artifact() uses above, applied to which screenshot is eligible
    to become a baseline rather than which file can be read."""
    identity = require_scope(request, "jobs:write")
    route = str(payload.get("route", "")).strip()
    viewport = str(payload.get("viewport", "")).strip()
    if not route or not viewport:
        raise HTTPException(status_code=400, detail="route and viewport are required")

    job = get_store().get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.get("kind") != "route_sweep":
        raise HTTPException(status_code=400, detail="job is not a route_sweep job")

    rows = (job.get("result") or {}).get("sweep_rows") or []
    match = next((r for r in rows if r.get("route") == route and r.get("viewport") == viewport), None)
    if match is None:
        raise HTTPException(status_code=404, detail="no screenshot recorded for this route/viewport in this job")
    href = match.get("cur")
    if not href:
        raise HTTPException(status_code=404, detail="screenshot has no recorded file")

    from orchestrator.dashboard.jobs import _repo_root

    reports_root = (_repo_root() / "reports").resolve()
    cur_path = (_repo_root() / href.lstrip("/")).resolve()
    if reports_root not in cur_path.parents:
        raise HTTPException(status_code=400, detail="invalid artifact path")
    if not cur_path.is_file():
        raise HTTPException(status_code=404, detail="screenshot file no longer on disk")

    baseline_root = _repo_root() / "reports" / "artifacts" / "route-baselines"
    baseline_root.mkdir(parents=True, exist_ok=True)

    import shutil

    shutil.copy2(cur_path, baseline_root / cur_path.name)

    get_store().audit(
        "route_sweep.baseline_approved",
        actor=identity.subject,
        resource_type="job",
        resource_id=job_id,
        detail={"route": route, "viewport": viewport, "baseline_file": cur_path.name},
    )
    return {"approved": True, "route": route, "viewport": viewport, "job_id": job_id}


@router.get("/schedules")
async def list_schedules(request: Request) -> dict[str, Any]:
    require_scope(request, "schedules:read")
    return {"schedules": get_store().list_schedules()}


@router.post("/schedules", status_code=201)
async def add_schedule(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    identity = require_scope(request, "schedules:write")
    kind = str(payload.get("kind", "")).strip()
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise HTTPException(status_code=400, detail="params must be an object")
    try:
        # Validate through the same durable enqueue service without actually enqueueing.
        from orchestrator.dashboard import jobs
        from orchestrator.dashboard.durable_jobs import _validation_view

        jobs._validate(kind, _validation_view(params))
        schedule = get_store().add_schedule(
            kind, params, int(payload.get("interval_s") or 300), requested_by=identity.subject
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    get_store().audit(
        "schedule.create", actor=identity.subject, resource_type="schedule",
        resource_id=schedule["id"], detail={"kind": kind, "interval_s": schedule["interval_s"]},
    )
    return schedule


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(request: Request, schedule_id: str) -> dict[str, Any]:
    identity = require_scope(request, "schedules:write")
    removed = get_store().remove_schedule(schedule_id)
    get_store().audit(
        "schedule.delete", actor=identity.subject, resource_type="schedule",
        resource_id=schedule_id, outcome="success" if removed else "ignored",
    )
    if not removed:
        raise HTTPException(status_code=404, detail="schedule not found")
    return {"removed": True}


@router.get("/engagements")
async def list_engagements(request: Request) -> dict[str, Any]:
    require_scope(request, "engagements:read")
    return {"engagements": get_store().list_engagements()}


@router.post("/engagements", status_code=201)
async def create_engagement(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    identity = require_scope(request, "engagements:write")
    target_pattern = str(payload.get("target_pattern", "")).strip()
    scope_statement = str(payload.get("scope_statement", "")).strip()
    tier = str(payload.get("tier", "")).strip()
    if not target_pattern:
        raise HTTPException(status_code=400, detail="target_pattern is required")
    if not scope_statement:
        raise HTTPException(status_code=400, detail="scope_statement is required")
    if tier not in ("active_recon", "exploit"):
        raise HTTPException(status_code=400, detail="tier must be 'active_recon' or 'exploit'")
    engagement = get_store().create_engagement(
        target_pattern,
        scope_statement,
        tier,
        authorized_by=identity.subject,
        expires_at=payload.get("expires_at"),
    )
    get_store().audit(
        "engagement.create", actor=identity.subject, resource_type="engagement",
        resource_id=engagement["id"], detail={"target_pattern": target_pattern, "tier": tier},
    )
    return engagement


@router.delete("/engagements/{engagement_id}")
async def revoke_engagement(request: Request, engagement_id: str) -> dict[str, Any]:
    identity = require_scope(request, "engagements:write")
    revoked = get_store().revoke_engagement(engagement_id, revoked_by=identity.subject)
    get_store().audit(
        "engagement.revoke", actor=identity.subject, resource_type="engagement",
        resource_id=engagement_id, outcome="success" if revoked else "ignored",
    )
    if not revoked:
        raise HTTPException(status_code=404, detail="engagement not found or already revoked")
    return {"revoked": True}


@router.get("/engagement-policy")
async def engagement_policy(request: Request) -> dict[str, Any]:
    """Read-only status for the engagement gate itself -- enforcement is set
    once, from ZYVOR_ENGAGEMENT_ENFORCEMENT at process start
    (EngagementPolicy.from_env()), so there is nothing to write here; an
    admin who wants it changed has to change the env var and restart."""
    require_scope(request, "engagements:read")
    from orchestrator.dashboard.jobs import ELEVATED_RISK_KINDS
    from orchestrator.security.engagement_policy import EngagementPolicy

    return {
        "enforcement": EngagementPolicy.from_env().enforcement,
        "elevated_risk_kinds": ELEVATED_RISK_KINDS,
    }


@router.get("/findings")
async def list_findings(
    request: Request,
    limit: int = Query(200, ge=1, le=500),
    severity: str = Query(""),
) -> dict[str, Any]:
    require_scope(request, "findings:read")
    return get_store().list_findings(limit, severity or None)


@router.delete("/findings")
async def close_findings(request: Request) -> dict[str, Any]:
    identity = require_scope(request, "findings:write")
    count = get_store().clear_findings()
    get_store().audit("findings.close_all", actor=identity.subject, detail={"count": count})
    return {"closed": count}


@router.get("/audit")
async def audit_events(request: Request, limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    require_scope(request, "audit:read")
    return {"events": get_store().list_audit(limit)}


@router.get("/requirements")
async def list_requirements(request: Request, limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    """Every requirement the pipeline has ever ingested, at its latest known
    version -- source-agnostic (github/document/local), with its current
    quality score. Read-only: requirements are written by the pipeline
    (fetch -> parse -> evaluate_quality), never directly through this API."""
    require_scope(request, "requirements:read")
    return {"requirements": get_store().list_requirements(limit)}


@router.get("/requirements/impact-graph")
async def requirement_impact_graph(request: Request) -> dict[str, Any]:
    """Groups every requirement's latest version by the data models and
    business flows named in agents/requirement_entities/ output: which
    requirements share a data model, and which generated tests trace to
    which flow -- the bounded first slice of impact analysis named as an
    open gap in ROADMAP.md. Registered before `/{requirement_id}` so
    "impact-graph" isn't swallowed as a path parameter."""
    require_scope(request, "requirements:read")
    return get_store().requirement_impact_graph()


@router.get("/requirements/{requirement_id}")
async def get_requirement(request: Request, requirement_id: str) -> dict[str, Any]:
    """Latest version's full content, quality score, and named quality issues."""
    require_scope(request, "requirements:read")
    requirement = get_store().get_requirement(requirement_id)
    if requirement is None:
        raise HTTPException(status_code=404, detail="requirement not found")
    return requirement


@router.get("/requirements/{requirement_id}/history")
async def requirement_history(request: Request, requirement_id: str) -> dict[str, Any]:
    """Every version on record -- the raw material for a real diff view, and
    for answering "what changed" when a requirement's quality score moves.
    Each version is enriched with `linked_tests` -- the generated tests that
    trace to it (`link_requirement_test`) -- so a caller can see, per
    version, what was actually generated from it without a second round
    trip per version."""
    require_scope(request, "requirements:read")
    store = get_store()
    versions = store.requirement_history(requirement_id)
    for version in versions:
        version["linked_tests"] = store.linked_tests(requirement_id, version["version"])
    return {"versions": versions}


@router.get("/intel/health")
async def intel_health(request: Request, limit: int = Query(40, ge=1, le=200)) -> dict[str, Any]:
    """Suite health + quarantine overlay. Read-only."""
    require_scope(request, "jobs:read")
    from orchestrator.intelligence.health import summarize

    return summarize(limit=limit)


@router.get("/intel/quarantine")
async def intel_quarantine_list(
    request: Request,
    include_inactive: bool = Query(False),
) -> dict[str, Any]:
    require_scope(request, "jobs:read")
    from orchestrator.intelligence import quarantine as q

    return {"quarantine": q.listing(include_inactive=include_inactive)}


@router.post("/intel/quarantine", status_code=201)
async def intel_quarantine_add(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    identity = require_scope(request, "jobs:write")
    from orchestrator.intelligence import quarantine as q

    try:
        entry = q.add(
            str(payload.get("title") or ""),
            file=str(payload.get("file") or ""),
            reason=str(payload.get("reason") or ""),
            owner=str(payload.get("owner") or identity.subject),
            ttl_hours=int(payload.get("ttl_hours") or 72),
            category=str(payload.get("category") or "flaky"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    get_store().audit(
        "intel.quarantine.add", actor=identity.subject, resource_type="quarantine",
        resource_id=entry["key"], detail={"title": entry["title"], "ttl_hours": payload.get("ttl_hours")},
    )
    return entry


@router.delete("/intel/quarantine/{key:path}")
async def intel_quarantine_release(request: Request, key: str) -> dict[str, Any]:
    identity = require_scope(request, "jobs:write")
    from orchestrator.intelligence import quarantine as q

    released = q.release(key)
    get_store().audit(
        "intel.quarantine.release", actor=identity.subject, resource_type="quarantine",
        resource_id=key, outcome="success" if released else "ignored",
    )
    if not released:
        raise HTTPException(status_code=404, detail="quarantine entry not found")
    return {"released": True, "key": key}


@router.post("/intel/select")
async def intel_select(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Change-based test selection from git + optional requirement links."""
    require_scope(request, "jobs:read")
    from orchestrator.intelligence.select import select_from_git

    base = str(payload.get("base") or "HEAD~1")[:80]
    head = str(payload.get("head") or "HEAD")[:80]
    include_quarantined = bool(payload.get("include_quarantined"))
    extra = payload.get("linked_tests") if isinstance(payload.get("linked_tests"), list) else None
    linked = [str(x) for x in extra] if extra else None
    if linked is None:
        try:
            graph = get_store().requirement_impact_graph()
            linked = []
            for bucket in (graph.get("flows") or {}).values():
                linked.extend(bucket.get("tests") or [])
        except Exception:
            linked = []
    return select_from_git(
        base=base, head=head, linked_tests=linked, include_quarantined=include_quarantined,
    )


@router.get("/intel/studio/{job_id}")
async def intel_studio(request: Request, job_id: str) -> dict[str, Any]:
    require_scope(request, "jobs:read")
    from orchestrator.intelligence.studio import build_studio

    job = get_store().get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return build_studio(job)


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    require_scope(request, "audit:read")
    return Response(render(), media_type="text/plain; version=0.0.4")
