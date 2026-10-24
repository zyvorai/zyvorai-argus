# Architecture

How Zyvor Argus is put together: the pipeline, the agents, the state, and the design decisions behind them.

---

## High-level view

```
GitHub (specs, issues, PRs, deploy events)          Natural language (CLI)
        │                                                   │
        ▼                                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                LangGraph Orchestrator (Python)                       │
│                                                                      │
│  fetch → discover → gap_analyze → parse → generate → execute        │
│                                                                       │
│  execute fans out to 4 parallel analysis nodes, then joins:          │
│      ┌─ regression ──┐                                               │
│      ├─ api_validate ┼─► merge_results                               │
│      ├─ log_analyze ─┤                                               │
│      └─ v8_coverage ─┘                                               │
│                                                                       │
│         pass → learn_skills → heal_pr → report → notify              │
│         fail → analyze → autofix → apply_autofix ─┐                  │
│                       ▲                            │                 │
│                       └── re-execute (≤ retries) ◄──┘                │
└─────────────────────────────────────────────────────────────────────┘
        │                                   │
        ▼                                   ▼
  Playwright (Node.js/TS)             Reports + Notifications
  chromium/firefox/webkit             HTML, PDF, PR comment,
  + optional Rust zyvor-diff          Slack, Teams, Email
```

Three languages, three roles:

| Layer | Language | Location | Role |
|-------|----------|----------|------|
| Orchestrator + agents | Python | `orchestrator/`, `agents/`, `github_integration/` | Pipeline control, LLM calls, parsing, reporting |
| Test execution | TypeScript | `playwright/`, `tests/` | Browser automation, fixtures, artifact capture |
| Screenshot diff (optional) | Rust | `rust/` | Fast pixel diffing (`zyvor-diff` binary) |

---

## The pipeline graph

Defined in [`orchestrator/graph.py`](../orchestrator/graph.py) as a LangGraph `StateGraph`. Every node is a pure-ish function `PipelineState -> PipelineState` living in [`orchestrator/nodes/`](../orchestrator/nodes/).

### Nodes

| Node | Module | What it does |
|------|--------|--------------|
| `fetch` | `nodes/fetch.py` | Resolves spec sources. Local: reads markdown files (default example spec if none given). GitHub: downloads specs, labeled issues, PR bodies, and — when coverage expansion is on — discovery files into `tests/fixtures/fetched/`. |
| `discover` | `nodes/discover.py` | Builds a coverage inventory (`CoverageCandidate` list) from downloaded code/docs; optionally merges a live site crawl. |
| `gap_analyze` | `nodes/gap_analyze.py` | Compares the inventory against signals extracted from existing specs (`goto()` paths, `toHaveURL`, test titles) and produces `CoverageGap` items. |
| `parse` | `nodes/parse.py` | Turns spec markdown into structured `Requirement` objects (LLM or rule-based) and converts coverage gaps into extra requirements. Persists `tests/fixtures/requirements.json`. |
| `generate` | `nodes/generate.py` | Writes one `.spec.ts` per requirement into `tests/generated/`. LLM output goes through a quality gate; failures fall back to a Jinja2 template. |
| `execute` | `nodes/execute.py` | Runs Playwright over `tests/manual/` (always) plus `tests/generated/` (when present) via `agents/execution/runner.py`. |
| `regression` | `nodes/regression.py` | When `ENABLE_REGRESSION=true`, pixel-compares screenshots against `screenshots/baselines/` (Pillow, or Rust when enabled). Runs in parallel with the three nodes below. |
| `api_validate` | `nodes/api_validate.py` | When `ENABLE_API_VALIDATION=true`, validates captured HTTP statuses from fixtures and HAR files in `traces/`. Runs in parallel. |
| `log_analyze` | `nodes/log_analyze.py` | Flags console errors and network failures from test sidecar logs (always on; noise like favicon/analytics is filtered). Runs in parallel. |
| `v8_coverage` | `nodes/v8_coverage.py` | When `ENABLE_V8_COVERAGE=true`, aggregates V8 JS coverage JSON written by the Playwright fixture. Runs in parallel. |
| `merge_results` | `nodes/merge_results.py` | Join point for the four parallel nodes above. Copies their outputs onto the shared `test_results` object once, sequentially, so no two parallel nodes ever write the same state key in one step. |
| `analyze` | `nodes/analyze.py` | On failure: LLM root-cause analysis with bounded artifact context (screenshots, traces, videos) — capped failed-case count, per-case log/error truncation, filtered to failing entries only (`agents/analyzer/agent.py`'s `MAX_*` constants); stub summary as fallback. |
| `autofix` | `nodes/autofix.py` | When `ENABLE_AUTOFIX=true`: checks the [skill store](../agents/autofix/README.md) for a previously-confirmed fix per failed case first, then falls back to an LLM suggestion (`AutofixSuggestion`) for unmatched cases. |
| `apply_autofix` | `nodes/apply_autofix.py` | When `ENABLE_AUTOFIX_APPLY=true`: patches spec files in place and loops back to `execute` (bounded by `AUTOFIX_MAX_RETRIES`). |
| `learn_skills` | `nodes/learn_skills.py` | If a patched retry passed, records the applied fix(es) into the skill store (`agents/skills/store.py`) for reuse in future runs. No-op otherwise. |
| `heal_pr` | `nodes/heal_pr.py` | When `ENABLE_HEAL_PR=true` and a patched retry passed: opens a draft PR with the confirmed repair via the GitHub API. Never fails the run. |
| `report` | `nodes/report.py` | Renders `reports/qa-summary.html` (Jinja2), optional PDF via headless Chromium, optional LLM plain-English summary. |
| `notify` | `nodes/notify.py` | Delivers to every configured channel: GitHub PR comment, Slack, Teams, email. |

### Routing

Three conditional edges (all in `graph.py`):

1. **`route_on_results`** (after `merge_results`): "fail" if any of — test failures, regression diff over threshold, failed API validation, or error-severity log issue. Otherwise on to `learn_skills` → `report`.
2. **`route_after_analyze`**: goes to `autofix` only when `ENABLE_AUTOFIX=true` and retry budget remains.
3. **`route_after_apply_autofix`**: loops back to `execute` only when `ENABLE_AUTOFIX_APPLY=true`, patches were actually applied, and retries remain; otherwise on to `learn_skills`.

### A note on the parallel fan-out

`regression`/`api_validate`/`log_analyze`/`v8_coverage` all read `test_results` but never each other's output, so they run concurrently off of `execute` instead of chaining. LangGraph raises `InvalidUpdateError` if two nodes in the same superstep both write the same state key without a reducer — so, unlike the rest of the pipeline, **these four nodes return only the specific key(s) they change**, never a `{**state, ...}` spread, and none of them mutate `test_results` in place. `merge_results` is the single node that copies their outputs onto `test_results` afterwards. Keep this in mind if you add a fifth parallel branch or turn `discover`/`parse` into parallel branches later (see [Extending the pipeline](#extending-the-pipeline)).

---

## Pipeline state

[`orchestrator/state.py`](../orchestrator/state.py) defines `PipelineState`, a `TypedDict` shared by all nodes. Key fields:

| Field | Type | Set by |
|-------|------|--------|
| `source` | `"local" \| "github" \| "document" \| "email" \| "transcript" \| "jira" \| "diarize"` | CLI / webhook / jobs |
| `spec_paths`, `spec_contents` | `list[str]` | `fetch` |
| `document_paths` | `list[str]` | `fetch` (document/email/transcript/diarize/jira export) |
| `jira_issue_keys` | `list[str]` | `fetch` (jira) |
| `requirements` | `list[Requirement]` | `parse` |
| `generated_tests` | `list[str]` (file paths) | `generate` |
| `test_results` | `TestResult` | `execute` (enriched by `merge_results` from the parallel regression/api/log nodes) |
| `coverage_inventory`, `coverage_gaps` | candidates / gaps | `discover`, `gap_analyze` |
| `failure_analysis` | `str` | `analyze` |
| `autofix_suggestions` | `list[AutofixSuggestion]` | `autofix` |
| `report_path`, `pdf_report_path`, `report_summary` | `str` | `report` |
| `metadata` | `dict` | everyone (counters, retry bookkeeping, changed files) |

All data models are Pydantic (`agents/common/models.py`): `Requirement`, `RequirementStep`, `TestResult`, `TestCaseResult`, `RegressionDiff`, `ApiValidationResult`, `LogIssue`, `CoverageCandidate`, `CoverageGap`, `AutofixSuggestion`, `V8CoverageSummary`, `PipelineReport`, `RequirementEntities`, `ModelDependency`.

---

## Design principle: LLM with deterministic fallback

Every AI-powered stage degrades gracefully so the pipeline works with **no API key at all**:

| Stage | With LLM | Without LLM (or on LLM error) |
|-------|----------|-------------------------------|
| Parse | `prompts/parser.md` → JSON requirements | Regex rule parser over `## Acceptance Criteria` sections |
| Requirement entities | `prompts/requirement_entities.md` → models/flows/typed deps | Capitalized-word + “depends on” heuristic |
| Generate | `prompts/generator.md` → full TypeScript | Jinja2 template `templates/test.spec.ts.j2` |
| Analyze | `prompts/analyzer.md` + artifact context | Stub summary echoing Playwright errors |
| Autofix | JSON selector suggestions | Generic role-based suggestion stub |
| Report summary | 2–4 sentence PR-ready summary | Markdown stats block |
| NL create | `prompts/nl_create.md` | *(requires LLM — the one exception)* |

The provider is selected once via `LLM_PROVIDER` in [`agents/common/llm.py`](../agents/common/llm.py) (OpenAI, Anthropic, Azure OpenAI, Google Gemini, Ollama), cached with `lru_cache`, always `temperature=0`.

### Generation quality gate

LLM-generated specs pass through [`agents/generator/quality.py`](../agents/generator/quality.py) before being accepted:

- rejects navigation to `/` when the requirement targets another path
- rejects brittle `toBeAttached()` assertions
- rejects duplicate test bodies (SHA-256 of content vs existing files)
- coarse syntax checks (balanced braces/parens, `node --check` on a stripped version)
- coverage tests must import `playwright/fixtures/base` and call `waitForPageReady`

Anything that fails the gate is regenerated from the deterministic template instead.

---

## Test execution bridge (Python ⇄ Node)

[`agents/execution/runner.py`](../agents/execution/runner.py) spawns `npx playwright test --config=playwright/playwright.config.ts <targets>`:

1. Expands test dirs to individual spec files, **skipping syntactically broken ones** so a single bad generated file can't sink the run.
2. Playwright writes `reports/results.json` (JSON reporter) plus HTML report and `test-results/` artifacts.
3. The JSON is parsed back into `TestResult`/`TestCaseResult`, including attachments: screenshots, traces, videos, and the sidecar `console.log` / `network-errors.log` produced by the custom fixtures.
4. Failure artifacts are copied to `reports/artifacts/<test-slug>/` and mirrored into `videos/`, `screenshots/`, `traces/` for CI upload conventions (`agents/execution/artifacts.py`).

### Custom fixtures (`playwright/fixtures/base.ts`)

- `consoleLogs` — every console message, attached as `console.log`
- `networkErrors` — every response ≥ 400, attached as `network-errors.log`
- `apiCalls` — full request log for `validateApiCalls()` assertions
- `page` override — starts/stops V8 JS coverage when `ENABLE_V8_COVERAGE=true`, writing per-test JSON to `reports/v8-coverage/`

---

## Coverage expansion subsystem

The most distinctive feature: the agent figures out *what your tests are missing* by reading the product repo.

```
GitHub repo ──► download discovery files ──► extract candidates ──► gap match ──► new requirements
 docs/, src/pages/,      tests/fixtures/        routes, pages,       vs signals     coverage-*.spec.ts
 sidebars, openapi        fetched/code/         docs, APIs           in existing     (≤ COVERAGE_MAX_NEW_TESTS)
                                                                     specs
```

- **Extractors** (`agents/discover/agent.py`): markdown headings → page/doc candidates; `src/pages/`, `src/routes/`, `app/` file paths → route candidates; Docusaurus sidebar ids; OpenAPI `paths`.
- **Live crawl** (`agents/discover/crawl.py` + `playwright/scripts/crawl-site.mjs`): BFS over same-origin links of the deployed site, merged into the inventory when `ENABLE_LIVE_CRAWL=true`.
- **Gap matching** (`agents/coverage/gap.py`): extracts signals from every existing spec (goto paths, URLs, titles) and checks each candidate against them; uncovered, routable candidates become `navigate → wait → assert` requirements tagged `coverage`.
- **Scoping**: webhook `push`/`pull_request` events restrict discovery to changed files.

---

## Test intelligence subsystem

Suite brain for Mission Control and CI: classify flakes, quarantine them, select
which tests a change should run, and open a failure studio for a red job. File-
backed on purpose (`reports/quarantine.json`) — no `MissionControlStore` /
`PostgresStore` schema change.

- **Taxonomy** (`orchestrator/intelligence/classify.py`): deterministic
  (no LLM) — `healthy` / `failing` / `flaky` / `selector` / `assertion` /
  `infra` / `data` / `unknown`. Quarantine is recommended only after ≥3 runs
  with a mixed pass/fail history.
- **Quarantine** (`orchestrator/intelligence/quarantine.py`): TTL + owner +
  release/expiry prune. Never heals assertions — quarantine is an explicit
  operator action.
- **Change-based select** (`orchestrator/intelligence/select.py`):
  `git diff --name-only` + requirement-linked tests + quarantine drop; smoke
  fallback on product-path changes; infra-only diffs do not enqueue a product
  suite.
- **Health / studio** (`health.py`, `studio.py`): overlay quarantine on
  `history.test_health()`; join a job's cases with on-disk video/trace
  presence and a classification.
- **Surfaces**: `GET/POST/DELETE /api/v2/intel/*`, CLI `argus intel …`, job
  kind `select_tests` (static, no engagement).

---

## Entry points

| Entry | File | Trigger |
|-------|------|---------|
| CLI `argus` | `orchestrator/cli.py` (Typer) | `test run`, `test exec`, `flow run`, `vision regression`, `guard misconfig-scan`, `intel health|select|quarantine-*`, `serve` (grouped subcommands; legacy flat `zyvor-qa` alias still works) |
| Webhook server | `orchestrator/webhook.py` (FastAPI) | GitHub `push`, `pull_request`, `repository_dispatch: staging-deployed`; HMAC-verified via `GITHUB_WEBHOOK_SECRET`; `/health` for probes |
| Slack slash command | `orchestrator/webhook.py` (`POST /webhook/slack/command`) | `/zyvor run <smoke\|full\|regression\|audit>` / `/zyvor status <job_id>` from chat, enqueued onto the same job queue as `POST /api/v2/jobs`. HMAC-verified via `SLACK_SIGNING_SECRET` (`orchestrator/security/slack.py`); dispatch logic in `orchestrator/slack_gateway.py`. One-way only — completion is still reported via the existing `SLACK_WEBHOOK_URL` notify channel, not a reply to the command. See [Tutorial 16](tutorials/16-slack-gateway.md). |
| MCP server | `integrations/mcp/` (`argus-mcp`, optional `[mcp]` extra) | Exposes an allowlisted subset of `/api/v2` jobs as MCP tools (`run_job`, `run_smoke_test`, `run_site_audit`, `run_crawl_test`, `get_job_status`, `cancel_job`) for MCP-capable chat agents (e.g. Hermes Agent) to trigger and poll QA jobs from Telegram/Discord/Slack/CLI. Thin HTTP client of `/api/v2`, no `orchestrator.*` imports — deployable independently. Bearer-token auth via the same `orchestrator/security/rbac.py` scopes. See [`docs/mcp-server.md`](mcp-server.md). |
| GitHub Actions | `.github/workflows/qa-smoke.yml`, `qa-post-deploy.yml` | push/PR/nightly smoke; full pipeline on staging deploy |
| Kubernetes | `kubernetes/` | webhook Deployment + nightly smoke CronJob |
| Docker | `docker/Dockerfile` | `argus test run --source local` by default |

---

## Security testing

Beyond the 10 read-only network/security probes and the `audit` site grade,
deeper security and resilience job kinds are gated behind an authorized
**security engagement** (and, for some kinds, independent opt-in flags):

| Job kind | Tier | What it does |
|----------|------|--------------|
| `misconfig_scan` | `active_recon` | Tech/version fingerprinting, wordlist-driven path discovery, security-header *value* grading, DNS hygiene, plus compliance signals (`security.txt`, consent markers, PII patterns) — `agents/probes/misconfig_scan.py` |
| `cve_lookup` | `active_recon` | Read-only: fingerprints tech/versions, checks them against OSV.dev — `agents/probes/cve_lookup.py`. No PoC is generated or run |
| `sca_scan` | `active_recon` (URL mode) | Client-side library/license fingerprinting and/or local-checkout `pip-audit`/`npm audit` — checkout mode needs no engagement |
| `contract_verify` | `active_recon` | HAR-derived consumer contract verification against a live provider |
| `llm_redteam` | `active_recon` | Attacker→judge loop against Ask Zyra (curated battery, `agents/redteam/`) — prompt injection, system-prompt exfiltration, excessive agency, jailbreaks, PII/secret exfiltration |
| `db_assert` | `active_recon` | Read-only SELECT-only assertions against Postgres/MySQL/SQLite — also requires `ZYVOR_DB_TESTING_ENABLED`; DSN via `$secret` |
| `db_migration_check` | `active_recon` | Read-only suite of SELECT checks around a database migration: run `phase: pre`, apply the migration with your own tooling, run `phase: post` with `baseline_job_id`. Fails on a broken invariant, a changed column set, or changed data in a check marked `stable`; keeps only row counts, columns and result digests. Same `ZYVOR_DB_TESTING_ENABLED` gate and `$secret` DSN as `db_assert` |
| `exploit_poc` | `exploit` | Generates a non-destructive verification script via LLM for a described finding and runs it in a sandboxed Kubernetes Job (`orchestrator/security/sandbox.py`, `kubernetes/sandbox.yaml`) — never in-process. Also requires `ZYVOR_EXPLOIT_EXECUTION_ENABLED=true` |
| `attack_chain` | `exploit` | Repeatedly plan-and-verifies one escalation step at a time (LLM planner + `exploit_poc`'s exact PoC-generation/sandbox machinery), stopping the moment a step fails or the planner has nothing safe left to propose (max 5 steps). Same gates as `exploit_poc` |
| `host_pentest` | `exploit` | Non-destructive SSH enumeration (`paramiko`) via a specially-imaged sandbox (`ZYVOR_SANDBOX_HOST_IMAGE`). Also requires `ZYVOR_CREDENTIALED_PENTEST_ENABLED=true`; creds must be `$secret` refs |
| `cloud_pentest` | `exploit` | Non-destructive `aws`/`gcloud`/`az` CLI enumeration via a specially-imaged sandbox (`ZYVOR_SANDBOX_CLOUD_IMAGE`). Same additional credentialed-pentest gate as `host_pentest` |
| `chaos_inject` | `exploit` | Client-side egress fault injection while a flow/smoke control observes — also requires `ZYVOR_CHAOS_INJECTION_ENABLED` and per-run consent |
| `chaos_webhook` | `exploit` | Trigger a user-owned chaos experiment webhook, then observe with the same resilience rubric |
| `port_scan` | `active_recon` | Bounded TCP connect scan (≤64 common ports) — `agents/probes/port_scan.py` |
| `tls_cipher_scan` | `active_recon` | TLS protocol + weak-cipher grading — `agents/probes/tls_cipher_scan.py` |
| `dast_scan` / `injection_scan` / `csrf_probe` / `ssrf_probe` / `auth_attack_scan` / `idor_scan` | `exploit` | Bounded DAST / web-attack probes — also require `ZYVOR_DAST_SCAN_ENABLED=true`. Optional nuclei via `ZYVOR_DAST_NUCLEI_BIN`. See `docs/security-network-attack-gaps.md` |

`api_contract_diff` (API panel) is pure static OpenAPI analysis and needs **no** engagement.

**Engagement gating** (`orchestrator/security/engagement_policy.py`): an
admin creates a target-scoped, tier-ranked attestation via
`POST /api/v2/engagements` (`orchestrator/persistence/store.py`'s
`engagements` table); every elevated job kind must cite its id and is
rejected by `orchestrator/dashboard/jobs.py`'s `_validate()` — the one
choke-point every trigger path (dashboard, CLI, `/api/v2/jobs`, schedules)
already funnels through — if the engagement is missing, revoked, expired, an
insufficient tier, or the target falls outside its `target_pattern`. This
mirrors `orchestrator/security/agent_policy.py`'s mode/fail-closed-in-
production shape rather than inventing a new one; `ZYVOR_ENGAGEMENT_ENFORCEMENT
=disabled` is refused at startup when `ZYVOR_ENV=production`
(`orchestrator/security/config.py`).

**`exploit_poc`'s sandbox** (`orchestrator/security/sandbox.py`) is a real
containment boundary, not a convenience wrapper: PoC code runs as a
short-lived Job in a dedicated namespace with dropped capabilities, non-root,
read-only rootfs, no ServiceAccount token, resource limits, and a hard
timeout — `sandbox.available()` must be true (a configured
`ZYVOR_SANDBOX_NAMESPACE` + a reachable cluster) or the job refuses to run
rather than falling back to unsandboxed execution. Per-Job network-egress
restriction is attempted but best-effort — only enforced on
NetworkPolicy-capable CNIs; see `kubernetes/sandbox.yaml`'s CNI caveat.

Full design rationale for the shipped engagement/sandbox model lives in
`ROADMAP.md`. Remaining **network-attack / DAST** gaps (what is still
deliberately deferred) are inventoried in
[`docs/security-network-attack-gaps.md`](security-network-attack-gaps.md).

---

## Filesystem contract

Directories the pipeline reads and writes (all relative to repo root):

| Path | Contents | Producer |
|------|----------|----------|
| `tests/manual/` | Hand-written specs — **always executed** | humans |
| `tests/generated/` | Generated specs (`req-*`, `coverage-*`) | `generate` |
| `tests/fixtures/requirements.json` | Last parsed requirements | `parse` |
| `tests/fixtures/fetched/` | Downloaded GitHub specs/issues (`code/` for discovery files) | `fetch` |
| `reports/qa-summary.html` / `.pdf` | Final report | `report` |
| `reports/results.json` | Playwright JSON output | `execute` |
| `reports/artifacts/` | Per-failure video/screenshot/trace | `execute` |
| `reports/test-index.jsonl` | Per-test pass/fail index for Test health | `history.record_test_results` |
| `reports/history/` | Run history for sparklines / trends | dashboard history |
| `reports/quarantine.json` | Flake quarantine (TTL, owner, status) | `orchestrator/intelligence/quarantine` |
| `reports/v8-coverage/` | Per-test V8 coverage JSON | Playwright fixture |
| `reports/crawl-inventory.json` | Live crawl results | crawl script |
| `screenshots/baselines/`, `current/`, `diffs/` | Visual regression images | `regression` |
| `test-results/` | Raw Playwright output dir | Playwright |
| `videos/`, `traces/` | CI-convention mirrors of failure artifacts | `execute` |

---

## Extending the pipeline

To add a new stage:

1. Add any data models to `agents/common/models.py` and state fields to `orchestrator/state.py`.
2. Implement the logic as an agent package under `agents/<name>/` (keep LLM and non-LLM paths separate, like the existing agents).
3. Add a thin node wrapper in `orchestrator/nodes/<name>.py` that reads/writes `PipelineState` and honors a feature flag.
4. Register the node and its edges in `orchestrator/graph.py`. If the new stage runs sequentially, returning `{**state, "your_key": value}` is fine (existing convention). If it runs **in parallel** with sibling nodes (see [the fan-out note above](#a-note-on-the-parallel-fan-out)), return only the key(s) you actually change — no `{**state, ...}` spread, and don't mutate shared mutable objects like `test_results` in place; do that in a downstream join node instead.
5. Surface results in the report template (`templates/report.html.j2`) and/or `PipelineReport`.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for conventions.
