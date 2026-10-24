# Configuration Reference

Every environment variable the agent reads, with defaults and the code that consumes it. Copy `.env.example` to `.env` and adjust; the CLI loads `.env` from the repo root automatically.

Boolean flags accept `true`/`false` (case-insensitive).

---

## LLM provider

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `openai` | One of `openai`, `anthropic`, `azure`, `google`, `ollama` |
| `LLM_MODEL` | `gpt-4o` | Model name passed to the provider |
| `OPENAI_API_KEY` | — | Required when `LLM_PROVIDER=openai` |
| `ANTHROPIC_API_KEY` | — | Required when `LLM_PROVIDER=anthropic` |
| `AZURE_OPENAI_API_KEY` | — | Required when `LLM_PROVIDER=azure` |
| `AZURE_OPENAI_ENDPOINT` | — | Required when `LLM_PROVIDER=azure` |
| `AZURE_OPENAI_DEPLOYMENT` | `LLM_MODEL` | Azure deployment name |
| `GOOGLE_API_KEY` | — | Required when `LLM_PROVIDER=google` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server (needs `pip install langchain-community`) |

No key set? Everything still runs — parsing, generation, analysis, and summaries fall back to rule-based/template implementations. Only `argus test create` (natural-language tests) hard-requires an LLM.

Consumed by: `agents/common/llm.py`, `agents/parser/agent.py`.

---

## GitHub integration

| Variable | Default | Description |
|----------|---------|-------------|
| `ZYVOR_PRODUCT_REPO` | — | Product repo as `owner/repo` (not a URL). Required for `--source github`. |
| `GITHUB_TOKEN` | — | PAT with `Contents: Read` (+ `Pull requests: Write` for PR comments). Optional if `gh auth login` is configured — resolution order is `GITHUB_TOKEN` → `gh auth token`. |
| `GITHUB_WEBHOOK_SECRET` | — | HMAC secret for `argus serve`. If empty, signature verification is skipped (do not leave empty in production). |

Consumed by: `github_integration/client.py`, `orchestrator/webhook.py`, `orchestrator/nodes/fetch.py`.

---

## Requirements connectors (email / Jira / diarize)

| Variable | Default | Description |
|----------|---------|-------------|
| `JIRA_BASE_URL` / `JIRA_URL` | — | Jira site base for live `--source jira` |
| `JIRA_OAUTH_ACCESS_TOKEN` / `JIRA_ACCESS_TOKEN` | — | Preferred Bearer token (Atlassian 3LO) |
| `JIRA_OAUTH_REFRESH_TOKEN` | — | Optional; refresh with client id/secret |
| `JIRA_OAUTH_CLIENT_ID` / `JIRA_OAUTH_CLIENT_SECRET` | — | OAuth refresh credentials |
| `JIRA_OAUTH_TOKEN_URL` | `https://auth.atlassian.com/oauth/token` | Token endpoint |
| `JIRA_API_TOKEN` / `JIRA_TOKEN` | — | Fallback API token (Basic with `JIRA_USER`/`JIRA_EMAIL`, else Bearer) |
| `IMAP_HOST` | `imap.gmail.com` | Mailbox for `--source email` without `.eml` paths |
| `IMAP_USER` / `GMAIL_USER` | — | IMAP username |
| `IMAP_PASSWORD` / `GMAIL_APP_PASSWORD` | — | IMAP password (Gmail app password) |
| `IMAP_FOLDER` | `INBOX` | Folder to search |
| `ZYVOR_DIARIZE_CMD` | — | Shell template with `{input}` / `{output}` for audio diarization |
| `ZYVOR_DIARIZE_API_URL` | — | Alternative: POST multipart audio → text/VTT |
| `ZYVOR_DIARIZE_API_TOKEN` | — | Optional Bearer for the diarize API |

Consumed by: `agents/requirements_sources/{email,jira,diarize}.py`, `orchestrator/nodes/fetch.py`.

---

## Target environment

| Variable | Default | Description |
|----------|---------|-------------|
| `ZYVOR_BASE_URL` | `https://zyvor.dev` | Base URL all tests run against |
| `ZYVOR_STAGING_URL` | — | Overrides the target for dashboard flows (takes precedence over `ZYVOR_BASE_URL` in `playwright/utils/target.ts`) |
| `ZYVOR_TEST_USER` / `ZYVOR_TEST_PASSWORD` | — | Dashboard login credentials |
| `ENABLE_DASHBOARD_TESTS` | `false` | Allow auth/login flows. When false and the target is zyvor.dev, login-tagged tests are skipped — the marketing site has no login. |

Consumed by: `playwright/utils/target.ts`, `playwright/utils/auth.ts`, `agents/generator/agent.py`.

---

## Visual regression (Phase 2)

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_REGRESSION` | `false` | Compare screenshots against `screenshots/baselines/`. Also switches Playwright to `screenshot: 'on'`. |
| `REGRESSION_THRESHOLD` | `1.0` | Max allowed pixel diff, percent |
| `UPDATE_BASELINES` | `false` | Copy current screenshots as new baselines instead of failing on missing ones (set by `argus vision regression --update-baselines`) |
| `ENABLE_RUST_PROCESSOR` | `false` | Use the Rust `zyvor-diff` binary instead of Pillow (build first: `make rust`) |
| `ZYVOR_DIFF_BINARY` | — | Explicit path to `zyvor-diff` if not in `rust/target/{release,debug}/` |

Consumed by: `orchestrator/nodes/regression.py`, `agents/regression/*`, `playwright/playwright.config.ts`.

---

## API validation & logs (Phase 2)

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_API_VALIDATION` | `false` | Validate HTTP statuses captured by fixtures and HAR files in `traces/` |

Browser log analysis (console errors, network failures) is **always on** and needs no flag. Noise (favicon, analytics, CSP, Cloudflare) is filtered in `agents/logs/analyzer.py`.

---

## LLM analysis, reports, notifications (Phase 3)

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_LLM_ANALYSIS` | `true` | LLM root-cause analysis of failures (stub fallback on error) |
| `ENABLE_LLM_REPORT` | `true` | LLM plain-English report summary (stub fallback) |
| `ENABLE_PDF_REPORT` | `true` | Render `reports/qa-summary.pdf` via headless Chromium |
| `SLACK_WEBHOOK_URL` | — | Slack incoming webhook (block-formatted message) |
| `SLACK_SIGNING_SECRET` | — | Signs/verifies inbound Slack slash-command requests at `POST /webhook/slack/command` (`/zyvor run <kind>` / `/zyvor status <job_id>`). Requests are rejected outright without it — no unsigned fallback. See [Tutorial 16](tutorials/16-slack-gateway.md). |
| `TEAMS_WEBHOOK_URL` | — | Microsoft Teams webhook (MessageCard) |
| `SMTP_HOST` | — | SMTP server; enables email when set together with `NOTIFY_EMAIL_TO` |
| `SMTP_PORT` | `587` | SMTP port (STARTTLS is used when user+password are set) |
| `SMTP_USER` / `SMTP_PASSWORD` | — | SMTP credentials; also used as From address |
| `NOTIFY_EMAIL_TO` | — | Recipient address (PDF report attached when available) |

Consumed by: `orchestrator/nodes/analyze.py`, `orchestrator/nodes/report.py`, `agents/reporter/*`, `orchestrator/security/slack.py`.

---

## Autofix / self-healing (Phase 4)

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_AUTOFIX` | `false` | Suggest selector repairs after failures |
| `ENABLE_AUTOFIX_APPLY` | `false` | Actually patch spec files and re-run failed tests |
| `AUTOFIX_MAX_RETRIES` | `2` | Max apply→re-execute loops per run |
| `SKILLS_PATH` | `.zyvor-argus/skills.json` | Skill store — confirmed autofix repairs are recorded here and reused on future runs before falling back to an LLM suggestion |

Consumed by: `orchestrator/graph.py`, `orchestrator/nodes/autofix.py`, `orchestrator/nodes/apply_autofix.py`, `orchestrator/nodes/learn_skills.py`, `agents/skills/store.py`.

---

## Multi-browser (Phase 4)

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_MULTI_BROWSER` | `false` | Add firefox and webkit projects alongside chromium |
| `ENABLE_AUTH_SETUP` | `false` | Login once via `playwright/auth.setup.ts` and reuse `storageState` (`playwright/.auth/user.json`). Requires `ENABLE_DASHBOARD_TESTS=true` + credentials. |
| `ENABLE_EMULATION_PROJECTS` | `false` | Add chromium-dark / reduced-motion / locale projects |
| `ZYVOR_LOCALE` | `en-US` | Locale for the `chromium-locale` emulation project |
| `ZYVOR_GREP` | *(none)* | Default Playwright `--grep` filter (e.g. `@smoke`) for `argus test exec` |
| `ZYVOR_SHARD` | *(none)* | Default Playwright `--shard=i/n` for CI |
| `ZYVOR_HAR_PATH` | *(none)* | Default HAR file for `argus api har-replay --mode replay` |

Consumed by: `playwright/playwright.config.ts`, `agents/execution/runner.py`. Install browsers first: `npx playwright install --with-deps`.

---

## Coverage expansion

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_COVERAGE_EXPANSION` | `false` | Discover routes/pages/docs from the product repo and generate missing tests on GitHub runs (equivalent to `--expand-coverage`) |
| `COVERAGE_MAX_NEW_TESTS` | `10` | Cap on new `coverage-*.spec.ts` files per run |
| `COVERAGE_DISCOVERY_PATHS` | `docs/, docs/specs/, CHANGELOG.md, README.md, src/pages/, src/routes/, app/` | Comma-separated repo roots to scan |
| `COVERAGE_MAX_DISCOVERY_FILES` | `200` | Max files downloaded per discovery run |
| `COVERAGE_MAX_DISCOVERY_BYTES` | `2000000` | Max total bytes downloaded |

Consumed by: `orchestrator/coverage_config.py`, `github_integration/client.py`, `orchestrator/nodes/{fetch,discover,gap_analyze}.py`.

Note: an explicit `--spec` disables coverage expansion unless you also pass `--expand-coverage`.

---

## Live site crawl

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_LIVE_CRAWL` | `false` | BFS-crawl the deployed site and merge routes into the coverage inventory |
| `CRAWL_MAX_PAGES` | `50` | Page budget |
| `CRAWL_MAX_DEPTH` | `2` | Link depth from `/` |
| `CRAWL_MAX_CONTENT_CHARS` | `200000` | Cap on captured page text per page, so one large page can't blow up memory/output size or downstream embedding cost |
| `CRAWL_TIMEOUT_SECONDS` | `120` | Subprocess timeout for the crawl script |
| `CRAWL_ALLOW_PRIVATE_TARGETS` | `false` | Allow crawling private/loopback/link-local/metadata-adjacent addresses — for local dev targets only |

Every crawled URL — including per-page navigation during the BFS, not just the initial target — is validated as http(s)-only, credential-free, and resolving outside private/loopback/link-local/reserved ranges before navigating (`playwright/scripts/lib/target-policy.mjs`), so a redirect or DNS rebinding can't steer the crawler at an SSRF/internal target mid-crawl.

Consumed by: `agents/discover/crawl.py`, `playwright/scripts/crawl-site.mjs`, `playwright/scripts/lib/target-policy.mjs`. Standalone run: `npm run crawl`.

---

## Job completion callbacks

`POST /api/v2/jobs` accepts an optional `"callback_url"`. When that job finishes (succeeded,
failed or cancelled) Argus POSTs a small notice to it, so a chat agent or CI system is pushed the
result instead of polling. Off by default.

| Variable | Default | Meaning |
|---|---|---|
| `ZYVOR_CALLBACKS_ENABLED` | `false` | Master switch. While off, a request with `callback_url` is rejected with 400. |
| `ZYVOR_CALLBACK_ALLOWLIST` | _(empty)_ | Comma-separated hosts a callback may target (`fnmatch` patterns). `hooks.slack.com` is always allowed. This is separate from `ZYVOR_TARGET_ALLOWLIST`. |
| `ZYVOR_CALLBACK_SECRET` | _(unset)_ | HMAC key. Required for every non-Slack destination. |
| `ZYVOR_CALLBACK_ALLOWED_PORTS` | `443` | Allowed destination ports. |
| `ZYVOR_CALLBACK_ALLOW_HTTP` | `false` | Allow plain `http://` (ignored when `ZYVOR_ENV=production`). |
| `ZYVOR_CALLBACK_ALLOW_PRIVATE` | `false` | Allow private/loopback addresses (lab use only). |
| `ZYVOR_PUBLIC_URL` | _(unset)_ | If set, the notice includes `link` to `/api/v2/jobs/{id}`. |

Body (JSON): `{"event": "job.finished", "job_id", "kind", "status", "duration_s", "link"}`. It never
contains the job's params, result or error text. Slack webhooks get a `{"text": ...}` message instead.

Verify a notice by recomputing `sha256=` + HMAC-SHA256(`ZYVOR_CALLBACK_SECRET`, `"<X-Zyvor-Timestamp>." + raw body`)
and comparing it, in constant time, with `X-Zyvor-Signature`; reject timestamps that are too old.

Behaviour worth knowing: the URL is checked when the job is enqueued and again just before each
send; redirects are not followed; 5xx/429/network errors are retried (3 attempts, 1s then 4s);
a 4xx is not. The connection is not pinned to the validated IP, so a DNS-rebinding race in that
short window is narrowed, not eliminated: keep the allowlist to hosts you control. The URL is stored
in its own column and is never returned by the API or written to the audit log (it can hold a token);
`job.callback` audit rows record only the host and the result. Callbacks cover jobs enqueued through
`/api/v2` (the durable queue), not jobs started directly from the dashboard buttons.

## Mission Control dashboard

| Variable | Default | Description |
|----------|---------|-------------|
| `DASHBOARD_NAMESPACE` | in-cluster namespace, else `default` | Kubernetes namespace the dashboard inspects |
| `DASHBOARD_POD_SELECTOR` | *(empty — all pods in namespace)* | Label selector filter, e.g. `app=zyvor-argus` |
| `DASHBOARD_USER` | `admin` | Login username (Zyvor premium login screen) |
| `DASHBOARD_PASSWORD` | *(empty — auth disabled)* | Setting this **enables login** for `/dashboard`, the API, and artifacts; `/health` and `/webhook/github` stay open. `deploy-remote.sh` generates one per host automatically (skip with `--no-auth`). Login is rate-limited (8 failures / 5 min per IP → 5-min lockout). |
| `DASHBOARD_SECRET` | derived from credentials | Optional explicit session-signing secret |
| `ZYVOR_DESKTOP_MODE` | `false` | Set automatically by `desktop/` (the Tauri app) — hides the pods/workloads panel, which is always "cluster unavailable" for a locally-wrapped app. Not meant to be set by hand. |
| `ZYVOR_IGNORE_HTTPS_ERRORS` | `false` | Accept self-signed/invalid TLS on the target under test (the Test-any-site / audit / probe actions set this per job) |
| `ZYVOR_VIDEO` | *(off)* | `on` records a video for every test (dashboard job runs set this) |
| `ZYVOR_NO_SANDBOX` | *(off)* | `true` runs Chromium with `--no-sandbox` (required in containers/as root; set in the image) |
| `ZYVOR_PW_WORKERS` | *(CPU count)* | Cap Playwright parallelism (set to `2` in the image so in-pod runs don't OOM) |
| `ZYVOR_GREP` | *(none)* | Filter smoke/suite by Playwright tag (e.g. `@smoke`) |
| `ZYVOR_SHARD` | *(none)* | Playwright shard `i/n` for CI matrix runs |
| `ZYVOR_HAR_PATH` | *(none)* | Default HAR for Mission Control / CLI HAR replay |
| `VISUAL_MAX_DIFF_RATIO` | `2.0` | Route-sweep pixel-diff pass threshold (percent); routes above it are flagged |
| `VISUAL_SETTLE_MS` | `1500` | Extra settle time per route before the sweep screenshots it |
| `ZYVOR_BROWSER` | `chromium` | Flow engine: `chromium` / `firefox` / `webkit` (set by `flow --browser`) |
| `ZYVOR_DEVICE` | *(desktop)* | Playwright device profile for flow/vitals (set by `--device`) |
| `ZYVOR_THROTTLE` | *(none)* | `3g` / `offline` network emulation via CDP (set by `--throttle`) |
| `ZYVOR_SLOW_MS` | `12000` | Live-data per-request latency budget before a page is flagged `slow` |

### Knowledge RAG (optional Ask Zyra)

Requires Python **3.11 or 3.12**, `pip install -e ".[knowledge,fastembed]"` (the `fastembed` extra supplies BM25 sparse vectors for hybrid retrieval and local embeddings, and pins Pillow below 13), and a running Qdrant. Mission Control shows **Ask Zyra**; API clients use `POST /v1/qa`.

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_API_KEY` | *(empty)* | API key for the chat model (OpenAI or compatible). Required to answer. |
| `LLM_MODEL` | `gpt-5.5` | Chat model name |
| `LLM_BASE_URL` | *(none)* | OpenAI-compatible base URL (e.g. vLLM) |
| `LLM_TEMPERATURE` | `0` | Generation temperature |
| `LLM_TIMEOUT_SECONDS` | `90` | Model call timeout |
| `LLM_FALLBACK_MODEL` | *(none)* | Optional backup chat model used by `ModelFallbackMiddleware` |
| `LLM_FALLBACK_API_KEY` | `LLM_API_KEY` | API key for the fallback model |
| `LLM_FALLBACK_BASE_URL` | `LLM_BASE_URL` | Base URL for the fallback model |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model (`BAAI/bge-small-en-v1.5` with FastEmbed) |
| `EMBEDDING_BACKEND` | *(empty → OpenAI-compatible)* | Set `fastembed` / `local` for on-box FastEmbed (no remote embeddings API) |
| `EMBEDDING_API_KEY` | `LLM_API_KEY` | Embedding API key (unused when `EMBEDDING_BACKEND=fastembed`) |
| `EMBEDDING_BASE_URL` | *(none)* | OpenAI-compatible embeddings URL |
| `EMBEDDING_DIMENSIONS` | *(none)* | Optional fixed embedding dimensions |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant HTTP endpoint |
| `QDRANT_API_KEY` | *(none)* | Qdrant API key when enabled |
| `QDRANT_COLLECTION` | `zyvor_knowledge` | Collection name |
| `APP_API_KEY` | *(none)* | Shared key for `POST /v1/qa` (dev). Prefer `AUTH_TOKENS_JSON` in production. |
| `AUTH_TOKENS_JSON` | *(empty)* | JSON map of API token → `{tenant_id, access_levels}` |
| `TRUST_CLIENT_TENANT_HEADER` | `false` | If `false` (default) and no `AUTH_TOKENS_JSON` mapping is configured, requests are refused rather than trusting a client-supplied `X-Tenant-ID` header. Only enable for trusted, network-isolated internal deployments — never for tenant-facing/external clients. |
| `DEFAULT_ACCESS_LEVELS` | `public,user` | Levels a non-mapped key may request |
| `KNOWLEDGE_TENANT_ID` | `public` | Tenant used by Mission Control ask proxy (never from the browser) |
| `KNOWLEDGE_ACCESS_LEVELS` | `public,user` | Access levels for Mission Control ask proxy |
| `KNOWLEDGE_CHECKPOINT_PATH` | `reports/knowledge-checkpoints.sqlite` | SQLite path for conversation checkpoints (`:memory:` for ephemeral) |
| `ENABLE_LIVE_CLUSTER_TOOLS` | `false` | Opt-in read-only live K8s/KubeVirt/Cilium/Hubble/Ceph/node tools in Ask Zyra |
| `ENABLE_REMEDIATION_AGENT` | `false` | Separate HITL remediation planner (`POST /v1/remediation`); not part of Ask Zyra |
| `ENABLE_REMEDIATION_EXECUTOR` | `false` | After HITL approve, allowlisted pod restarts may execute |
| `REMEDIATION_RESTART_NAMESPACES` | *(empty — deny all)* | Namespaces where approved restarts may run (`*` for any) |
| `REMEDIATION_RESTART_NAME_PREFIXES` | *(empty — any name in allowlisted NS)* | Optional pod name prefixes required for executor restarts |
| `ENABLE_QUERY_UNDERSTANDING` | `true` | Deterministic intent/product/multi-query rewrite before retrieval |
| `ENABLE_ASK_STREAMING` | `true` | Mission Control uses `/api/dashboard/ask/stream` SSE progress when available |
| `KNOWLEDGE_LIVE_NAMESPACES` | *(Mission Control namespace)* | Comma allowlist of namespaces live tools may query; `*` allows any valid name |
| `ENABLE_PUBLIC_DOCUMENTS` | `true` | Also retrieve `tenant_id=public` docs for other tenants |
| `RETRIEVAL_K` | `8` | Passages returned to the model after rerank |
| `RETRIEVAL_FETCH_K` | `16` | Candidates fetched per expanded query |
| `MAX_TOOL_CALLS` | `8` | Tool-call budget per answer (specialised tools may combine) |
| `MAX_QUERY_LENGTH` | `4000` | Max question characters |

Eval / observability: set `LANGSMITH_API_KEY` and run `argus ask evaluate --langsmith` to send traces to LangSmith (`LANGCHAIN_PROJECT`, default `zyvor-knowledge-eval`).

Start Qdrant: `docker compose -f docker/docker-compose.yml up -d qdrant`. Ingest samples: `argus ask ingest knowledge_docs/sample --tenant-id public --access-level public`. See [Tutorial 14](tutorials/14-ask-zyra-knowledge.md).

The **🎬 Flow test** action (`flow` job / `argus flow run`) drives a multi-step journey recorded as one video, with a Playwright `trace.zip` (open at trace.playwright.dev) and richer assertions (`assert_not` / `assert_count` / `assert_value` / `assert_url` / `assert_api` / `assert_aria` / `upload` / `download` / `dialog` / `iframe` / `clock` / `wait_until`); the **🗺 Route sweep** action (`route_sweep` / `argus vision route-sweep`) screenshots routes at desktop/mobile and diffs them against baselines under `reports/artifacts/route-baselines/`, and can `--auto`-discover routes by crawling. **📼 HAR record/replay** (`har_replay` / `argus api har-replay`) captures network as HAR then drives the UI against it. **📥 Import codegen** (`import_codegen` / `argus test import-codegen`) turns pasted Playwright codegen into flow steps (optionally runs them). Both honour `ZYVOR_IGNORE_HTTPS_ERRORS`, `ZYVOR_NO_SANDBOX`, and (flow) `ZYVOR_VIDEO`, and both are schedulable. Serve the dashboard over HTTPS with `argus serve --tls` (self-signed cert under `~/.zyvor-argus/tls`) or the deploy script's `--tls`. A target-site login password passed to a flow/crawl is redacted (`***`) from the job-status API, history, and live panel — it is never echoed back to a dashboard reader. See [Tutorial 11](tutorials/11-flow-tests.md).

Four **product-testing** actions go beyond the page (see [Tutorial 12](tutorials/12-api-auth-realtime.md)): **🔌 API contract** (`api_contract` / `argus api test`) validates REST endpoints against their OpenAPI schema and runs multi-step API workflows; **🔐 Auth & session** (`auth_test` / `argus api auth-test`) logs in, saves a reusable session under `reports/artifacts/auth/`, and asserts logout/expiry/negative-auth — the saved session can be reused by `flow`/`realtime` via their `session` param; **📡 Live data** (`realtime` / `argus flow realtime`) asserts WebSocket/SSE streams are live (Bearer / `Sec-WebSocket-Protocol` / one-time ticket auth); **📊 Web Vitals** (`vitals` / `argus watch vitals`) grades LCP/CLS/INP with device + network throttle.

The dashboard's audit, probe, screenshot, compare, ping, load-test, TLS, flaky, and schedule actions are entirely UI/API-driven — no extra environment variables. They persist artifacts (videos, screenshots, diff images, and HTML/PDF/Markdown/CSV report bundles) under `reports/` (PVC-backed on Kubernetes).

**Test intelligence** (`argus intel …`, `GET/POST/DELETE /api/v2/intel/*`, job kind `select_tests`) is also UI/API/CLI-driven with no extra env vars. Quarantine entries live in `reports/quarantine.json` (TTL + owner). `select_tests` and `POST /api/v2/intel/select` are static analysis — no engagement required.

The dashboard is served by `argus serve` at `/dashboard`. Cluster access resolves in-cluster config first, then local kubeconfig; with neither, the pod panels show an offline state and QA run history still works. See [Tutorial 10](tutorials/10-mission-control-dashboard.md).

Consumed by: `orchestrator/dashboard/k8s.py`.

---

## V8 JS coverage

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_V8_COVERAGE` | `false` | Collect Chromium V8 JS coverage per test; aggregated into the report (`reports/v8-coverage/`) |

Consumed by: `playwright/fixtures/base.ts`, `agents/coverage/v8_report.py`.

---

## Recommended profiles

**Minimal smoke (no LLM, no GitHub):**

```bash
ZYVOR_BASE_URL=https://zyvor.dev
```

**PR bot (webhook or Actions):**

```bash
ZYVOR_PRODUCT_REPO=owner/repo
GITHUB_TOKEN=ghp_...
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=claude-sonnet-5
ENABLE_API_VALIDATION=true
ENABLE_LLM_ANALYSIS=true
ENABLE_LLM_REPORT=true
```

**Full self-healing nightly:**

```bash
ENABLE_REGRESSION=true
ENABLE_API_VALIDATION=true
ENABLE_AUTOFIX=true
ENABLE_AUTOFIX_APPLY=true
ENABLE_COVERAGE_EXPANSION=true
ENABLE_V8_COVERAGE=true
SLACK_WEBHOOK_URL=https://hooks.slack.com/...
```
