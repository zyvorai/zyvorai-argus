# MCP server: chat-ops for Mission Control

`integrations/mcp/` exposes a subset of Mission Control's `/api/v2` job API as an
[MCP](https://modelcontextprotocol.io) server, so an MCP-capable chat agent (e.g.
[Hermes Agent](https://github.com/NousResearch/hermes-agent)) can trigger QA jobs and read
results conversationally — "run a smoke test", "audit zyvor.dev" — from any platform the agent
is connected to (Telegram, Discord, Slack, CLI, ...).

It is a thin HTTP client of the existing `/api/v2` API — it does not import `orchestrator.*`
and has no access to Playwright/LangGraph internals. It can be run, deployed, and upgraded
independently of the rest of the pipeline.

## What it exposes

Tools (see `integrations/mcp/tools.py` for exact behavior):

- `run_job(kind, params=None, wait_s=None)` — generic job trigger, restricted to a fixed
  allowlist of low-risk kinds (`integrations/mcp/allowlist.py`) even though the underlying
  service token may have broader `jobs:write` access server-side.
- `run_smoke_test(wait_s=None)` — runs the fixed smoke suite against the deployment's
  configured target. **Takes no URL** — see the gotcha below.
- `run_site_audit(url, wait_s=None)` — crawls `url`, grades it A-F.
- `run_crawl_test(url, wait_s=None)` — crawls `url`, generates+runs a test per page.
- `get_job_status(job_id)` — poll a previously started job.
- `cancel_job(job_id)`.
- `list_job_kinds()` — one-line description of every kind `run_job` accepts.
- `get_findings(severity="", limit=50)` — open findings, newest first, with per-severity
  counts (read-only; needs the `findings:read` scope).
- `list_schedules()` — the recurring jobs Mission Control runs (read-only; needs
  `schedules:read`).

Two MCP resources give an agent ambient context without a tool call:
`zyvor://findings/open` and `zyvor://schedules`.

**Not** exposed in this version: creating or deleting schedules, closing findings, the audit log, or any kind outside the
allowlist (notably `full`, `loadtest`, `auth_test`, `create`/`generate`, `flow`/`ai_flow`/
`realtime`/`har_replay`/`import_codegen`). These are deliberate exclusions — see the comment
at the top of `integrations/mcp/allowlist.py` for the reasoning per kind.

### Gotcha: `smoke` has no `url` param

`smoke` (`orchestrator/dashboard/jobs.py`) always runs the repo's fixed Playwright suite
against the deployment's configured `ZYVOR_BASE_URL` — it does not accept an arbitrary target.
If someone asks "smoke test example.com", the right tool is `run_site_audit` or
`run_job(kind="crawl_test", ...)`, not `run_smoke_test`.

### Async handling

`/api/v2/jobs` is enqueue-then-poll; there's no push/webhook/SSE. `run_job` (and its
ergonomic wrappers) enqueue, then poll internally up to a bounded wait budget
(`ZYVOR_MCP_DEFAULT_WAIT_S`, default 20s; hard cap `ZYVOR_MCP_MAX_WAIT_S`, default 90s). If the
job finishes within budget it returns the full result in one call. If not, it returns
`{"status": "running", "job_id": ..., "note": "..."}` — call `get_job_status(job_id)` later.

## Setup

### 1. Mint a service token

```bash
python tools/hash_api_token.py
# TOKEN=<raw token>       -> give this to the MCP server as ZYVOR_API_TOKEN
# SHA256=<hash>           -> store this in ZYVOR_API_TOKENS_FILE, never the raw token
```

Add a record to the JSON file pointed to by `ZYVOR_API_TOKENS_FILE`:

```json
{
  "<sha256>": {"subject": "hermes-agent", "role": "viewer", "scopes": ["jobs:write"]}
}
```

`role: viewer` gives read-only scopes (`jobs:read`, `findings:read`, `schedules:read`,
`audit:read`); adding `jobs:write` on top is enough to enqueue/cancel jobs, without granting
`findings:write`/`schedules:write` (see `orchestrator/security/rbac.py`'s `identify()` — a
token's explicit `scopes` list adds to, not replaces, its role's base scopes).

### 2. Install and run

```bash
pip install -e ".[mcp]"

export ZYVOR_API_BASE_URL=http://127.0.0.1:8080   # or your deployment's URL
export ZYVOR_API_TOKEN=<raw token from step 1>
export ZYVOR_MCP_TRANSPORT=stdio                  # or streamable-http

argus-mcp
# or: python -m integrations.mcp
```

Config knobs (all optional besides the two above): `ZYVOR_MCP_HOST`/`ZYVOR_MCP_PORT`
(streamable-http only, default `127.0.0.1:8090`), `ZYVOR_MCP_DEFAULT_WAIT_S` (20),
`ZYVOR_MCP_MAX_WAIT_S` (90), `ZYVOR_MCP_POLL_INTERVAL_S` (2).

### 3. Try it locally with the MCP Inspector

```bash
mcp dev -m integrations.mcp
```

Call `run_smoke_test`, then `run_site_audit(url="https://zyvor.dev")`, then `get_job_status`
with the returned `job_id`. A URL outside the server's target allowlist should come back as a
clean `{"error": "...", "status_code": 400}` tool result, not a crash.

### 4. Connect Hermes Agent

Hermes's exact MCP client config schema is external to this repo — consult its own
[MCP integration docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp).
Two common shapes:

**stdio** (Hermes spawns the process locally):

```json
{
  "command": "python",
  "args": ["-m", "integrations.mcp"],
  "env": {
    "ZYVOR_API_BASE_URL": "http://127.0.0.1:8080",
    "ZYVOR_API_TOKEN": "<token>"
  }
}
```

**streamable-http** (Hermes's gateway runs elsewhere and needs a network endpoint): start the
server with `ZYVOR_MCP_TRANSPORT=streamable-http`, then point Hermes at
`http://<host>:8090/mcp`. This transport has no auth of its own beyond the baked-in Zyvor
token — keep it on a private network (`ClusterIP` in Kubernetes, not exposed via Ingress)
unless you add your own auth layer in front of it.

## Kubernetes note

`ZYVOR_API_TOKENS_FILE` must point at a real file — `kubernetes/deployment.yaml` and
`kubernetes/enterprise/secure-deployment.yaml` now mount a `argus-secrets` key
(`api-tokens.json`) at `/app/secrets/api-tokens.json` for this. Populate that secret key with
minted token records before relying on Bearer-token auth in a cluster deployment.

## Future work (explicitly deferred)

- Schedule create/delete and finding-close tools (writes), and a `callback_url` argument on
  `run_job`. The completion webhook itself exists (see
  [Job completion callbacks](configuration.md#job-completion-callbacks)); an MCP server cannot
  receive a push, so it is meant for Slack or CI rather than for this server's own tools.
- A standalone Kubernetes Deployment/Service for the `streamable-http` transport with a real
  auth layer for public exposure.
