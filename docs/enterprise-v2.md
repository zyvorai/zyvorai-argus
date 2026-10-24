# Zyvor Argus Enterprise v2

Integration guide for the enterprise security and durable control-plane overlay.

## What the installer changes

The installer copies the overlay into the existing repository and patches three current files:

1. `orchestrator/webhook.py`
   - installs the `/api/v2` enterprise router and durable workers
   - requires webhook signatures unless explicitly allowed for development
   - requires `X-GitHub-Delivery`
   - rejects replayed delivery IDs
2. `orchestrator/dashboard/jobs.py`
   - uses recursive secret redaction
   - validates every URL and hostname through the target policy before execution
3. `agents/aiflow/engine.py`
   - passes every LLM/heuristic action through the deterministic safety gateway

It replaces the old in-memory `scheduler.py` and `findings.py` with compatible SQLite-backed implementations. Backups are written beside changed files with a `.v1-backup` suffix.

## New API

### Queue a durable job

```bash
curl -X POST https://qa.example.com/api/v2/jobs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: deployment-7f0a1' \
  -d '{
    "kind": "vitals",
    "params": {"url": "https://app.example.com", "device": "Desktop Chrome"}
  }'
```

### Queue a job with a secret reference

```json
{
  "kind": "realtime",
  "params": {
    "url": "https://app.example.com",
    "ws": "/ws/events",
    "token": {"$secret": "env:QA_REALTIME_TOKEN"}
  }
}
```

Raw tokens and passwords are rejected for persisted jobs and schedules.

### Service tokens

Create a random token, hash it with SHA-256, and store only its hash:

```bash
python tools/hash_api_token.py
```

Set `ZYVOR_API_TOKENS_FILE=/run/secrets/zyvor/api-tokens.json`.

Example file:

```json
{
  "<sha256>": {
    "subject": "github-actions",
    "role": "operator"
  }
}
```

Roles:

- `viewer`: read jobs, findings, schedules, audit and engagements
- `operator`: run/cancel jobs, manage schedules/findings, read engagements
- `admin`: all scopes, including creating/revoking engagements (`engagements:write`)

Only `admin` can create or revoke a security engagement — see
[Security engagements](#security-engagements) below.

## Target policy

```bash
export ZYVOR_TARGET_ALLOWLIST='zyvor.dev,*.zyvor.dev,user.example.com'
export ZYVOR_TARGET_ALLOWED_PORTS='80,443,24631'
export ZYVOR_TARGET_ALLOWED_CIDRS='10.20.0.0/16'
export ZYVOR_ALLOW_PRIVATE_TARGETS=false
export ZYVOR_ALLOW_HTTP_TARGETS=false
```

Even when a hostname is allowlisted, metadata IPs remain blocked. Every resolved address and redirect destination must pass policy.

The live-crawl agent (`playwright/scripts/crawl-site.mjs`) has its own, narrower guard (`playwright/scripts/lib/target-policy.mjs`) for the same purpose — it validates every page navigated to during the BFS, not just the initial URL, since the crawler follows arbitrary in-site links. It blocks the same private/loopback/link-local/metadata ranges by default; set `CRAWL_ALLOW_PRIVATE_TARGETS=true` only for local dev targets.

## Security engagements

Elevated job kinds do deeper, potentially-invasive security testing —
`misconfig_scan`, `cve_lookup`, `sca_scan`, `llm_redteam`, `port_scan`,
`tls_cipher_scan`, `db_assert`, `db_migration_check`, `exploit_poc`, `attack_chain`,
`host_pentest`, `cloud_pentest`, `chaos_inject`, `chaos_webhook`, plus the
DAST family (`dast_scan`, `injection_scan`, `csrf_probe`, `ssrf_probe`,
`auth_attack_scan`, `idor_scan`) — and are refused (`400`) unless
the request cites a live, sufficiently-scoped **security engagement**: an
`admin`-issued attestation
that the target is actually authorized for testing. This is separate from
(and in addition to) the SSRF-focused [target policy](#target-policy)
above — target policy blocks *unsafe* destinations (private ranges, cloud
metadata); engagements answer *is this specific test run actually
authorized*.

Create one (requires `engagements:write`, i.e. `admin`):

```bash
curl -X POST https://qa.example.com/api/v2/engagements \
  -H 'Content-Type: application/json' \
  -d '{
    "target_pattern": "*.example.com",
    "scope_statement": "authorized pentest, staging + prod, 2026-Q3 engagement",
    "tier": "active_recon",
    "expires_at": "2026-10-01T00:00:00+00:00"
  }'
```

Then pass its `id` as `engagement_id` in the job's params:

```bash
curl -X POST https://qa.example.com/api/v2/jobs \
  -H 'Content-Type: application/json' \
  -d '{"kind": "misconfig_scan", "params": {"url": "https://app.example.com", "engagement_id": "<id>"}}'
```

`target_pattern` matches the job's hostname via the same `fnmatch` glob style
as `ZYVOR_TARGET_ALLOWLIST` (e.g. `*.example.com`, or `*` for "any host" in a
trusted dev environment). `tier` is `active_recon` for `misconfig_scan`/
`cve_lookup`/`llm_redteam`; `exploit_poc` requires `tier: "exploit"` — an
`active_recon`-tier engagement is rejected for it. Revoke with
`DELETE /api/v2/engagements/{id}`; list with `GET /api/v2/engagements`
(`engagements:read`, available to `viewer` too).

`ZYVOR_ENGAGEMENT_ENFORCEMENT=disabled` turns this gate off entirely (e.g.
for local dev) — refused at startup when `ZYVOR_ENV=production`, same
fail-closed pattern as the unrestricted-agent-mode check below.

### exploit_poc: the sandboxed PoC-execution tier

`exploit_poc` needs an `exploit`-tier engagement **and** a second,
independent opt-in — `ZYVOR_EXPLOIT_EXECUTION_ENABLED=true` — mirroring how
`ZYVOR_AGENT_ALLOW_DESTRUCTIVE` gates destructive browser actions below. It
also needs a real sandbox backend configured:

```bash
kubectl apply -f kubernetes/sandbox.yaml   # dedicated namespace + RBAC + default-deny egress
export ZYVOR_SANDBOX_NAMESPACE=argus-sandbox
export ZYVOR_EXPLOIT_EXECUTION_ENABLED=true
```

Without `ZYVOR_SANDBOX_NAMESPACE` pointing at a reachable cluster, `exploit_poc`
refuses to run rather than falling back to unsandboxed execution — see
`orchestrator/security/sandbox.py::available()`. PoC code is generated by an
LLM constrained to non-destructive, read-only verification (a single
`VERIFIED: true/false` output line, no writes, no floods) and executed in a
short-lived Job with dropped capabilities, non-root, read-only rootfs, no
ServiceAccount token, and a hard timeout. The generated source is written to
`reports/pocs/<run>/poc.py` with its SHA-256 logged to the audit log.
`kubernetes/sandbox.yaml` also attempts a per-Job egress-restricting
NetworkPolicy, but that's best-effort — it has no effect on CNIs that don't
enforce NetworkPolicy (notably k3s's default Flannel).

### DAST / network-attack jobs

`dast_scan`, `injection_scan`, `csrf_probe`, `ssrf_probe`, `auth_attack_scan`,
and `idor_scan` need an `exploit`-tier engagement **and**
`ZYVOR_DAST_SCAN_ENABLED=true`. They run in-process (bounded HTTP probes),
not in the exploit sandbox. Optionally point `ZYVOR_DAST_NUCLEI_BIN` at a
`nuclei` binary (or put `nuclei` on `PATH`) so `dast_scan` can merge external
template hits. `port_scan` and `tls_cipher_scan` only need an
`active_recon`-tier engagement (no DAST opt-in).

```bash
export ZYVOR_DAST_SCAN_ENABLED=true
# optional:
# export ZYVOR_DAST_NUCLEI_BIN=/usr/local/bin/nuclei
```

`attack_chain` needs exactly the same two gates and sandbox backend — it's
`exploit_poc` run in a loop, where an LLM proposes one next step at a time
given every step confirmed so far, stopping the moment a step fails to
verify or the planner has nothing safe left to propose (capped at 5 steps):

```bash
curl -X POST https://qa.example.com/api/v2/jobs \
  -H 'Content-Type: application/json' \
  -d '{"kind": "attack_chain", "params": {"url": "https://app.example.com", "objective": "escalate SQLi to RCE", "engagement_id": "<exploit-tier id>"}}'
```

### host_pentest / cloud_pentest: credentialed pentesting

Both need everything `exploit_poc` needs, **plus** a third, independent
opt-in — `ZYVOR_CREDENTIALED_PENTEST_ENABLED=true` — and a specially-imaged
sandbox backend, since the default `python:3.12-slim` image has neither
`paramiko` (for `host_pentest`) nor the `aws`/`gcloud`/`az` CLIs (for
`cloud_pentest`):

```bash
export ZYVOR_CREDENTIALED_PENTEST_ENABLED=true
export ZYVOR_SANDBOX_HOST_IMAGE=your-registry/zyvor-sandbox-host:latest    # python:3.12-slim + paramiko
export ZYVOR_SANDBOX_CLOUD_IMAGE=your-registry/zyvor-sandbox-cloud:latest  # + aws-cli/gcloud/az
```

Without the relevant image env var set, both job kinds refuse to run — same
"fail closed rather than silently downgrade" posture as everywhere else in
this feature set. **Credentials are never accepted as raw values** — every
field in `creds` that looks secret-shaped (`password`, `private_key`,
`secret_access_key`, etc.) must be a `{"$secret": "env:NAME"}` reference
(`orchestrator/security/secrets.py`), resolved only at execution time and
injected straight into that one ephemeral sandbox Job's environment — never
logged, never embedded in the LLM-generated verification script, never
present in the job result:

```bash
curl -X POST https://qa.example.com/api/v2/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "kind": "host_pentest",
    "params": {
      "host": "internal-host.example.com",
      "finding_description": "SSH allows password auth with a weak default credential",
      "creds": {"username": "admin", "password": {"$secret": "env:TARGET_SSH_PASSWORD"}},
      "engagement_id": "<exploit-tier id>"
    }
  }'
```

## Autonomous-agent modes

- `read_only`: assertions, waits, navigation and non-submitting UI exploration
- `supervised`: write actions require an approved risk class
- `unrestricted`: writes allowed, but destructive actions remain disabled by default

```bash
export ZYVOR_AGENT_MODE=supervised
export ZYVOR_AGENT_APPROVED_RISKS=write
export ZYVOR_AGENT_ALLOW_DESTRUCTIVE=false
export ZYVOR_AGENT_ALLOWED_ORIGINS='login.example.com,sso.example.com'
```

The agent blocks unknown actions, invalid indices, disabled controls, unapproved cross-origin navigation, suspicious page instructions and destructive actions.

## Persistence

Default database:

```text
reports/mission-control.db
```

Override:

```bash
export ZYVOR_STATE_DB=/var/lib/argus/mission-control.db
```

SQLite is appropriate for one control-plane replica. Use PostgreSQL and an external queue before scaling control-plane replicas horizontally.

## Secure Kubernetes deployment

```bash
kubectl apply -f kubernetes/enterprise/secure-deployment.yaml
kubectl apply -f kubernetes/enterprise/network-policy.yaml
```

Pod restart is deliberately excluded from default RBAC. Apply `optional-pod-restart-rbac.yaml` only when required.

## SSO / OIDC (Argus Enterprise Watchfloor)

This overlay document covers the community Enterprise v2 security/durability
layer (service tokens, engagements, target policy). Interactive SSO for the
Argus Enterprise Watchfloor product — Keycloak, any OIDC IdP, demo accounts
(`demo`/`demo`, `ssouser`/`Sso@321`), and local username/password — is in
**[enterprise-sso-oidc.md](enterprise-sso-oidc.md)** and the user manual
**[user/enterprise-sso.md](user/enterprise-sso.md)**.
