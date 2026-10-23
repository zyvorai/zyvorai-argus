# Zyvor Argus — Feature Guide

> **AI-first autonomous testing, security, and requirements-quality platform that turns requirements into tests and validates every deploy.**

Zyvor Argus reads requirements from GitHub issues/PRs or from local documents (including PDFs), scores each one for gaps and ambiguity, generates Playwright tests, runs them after every deployment, detects regressions, and files plain-English reports — all coordinated by a LangGraph state machine. Every requirement is persisted with a full version history and traced to the tests generated from it, so a changed requirement surfaces exactly which tests may need review — plus an impact view by shared data models and flows. Beyond the pipeline it ships Mission Control, a live web console that runs 25+ capabilities on demand: end-to-end journey tests, API-contract / contract-diff / HAR verify, real-time stream assertions, Core Web Vitals, visual sweeps, security probes and authorized security testing (SCA, DAST / network-attack probes, DB assert, chaos), and recurring monitors. It is LLM-provider agnostic and degrades gracefully to rule-based fallbacks when no key is configured.

**20+** QA actions in Mission Control · **5** LLM providers supported · **3** browser engines (Chromium/Firefox/WebKit) · **10** network & security probes

This is the user-facing onboarding guide — how to access the product, your first workflows, and how to use every feature. A print-ready PDF of the same content sits alongside this file.

## Contents

0. [Getting started — access & first workflows](#getting-started)
1. [Autonomous Pipeline](#1-autonomous-pipeline)
2. [Browser & Journey Testing](#2-browser-journey-testing)
3. [API, Auth & Real-Time](#3-api,-auth-real-time)
4. [Performance & Web Quality](#4-performance-web-quality)
5. [Network & Security Probes](#5-network-security-probes)
6. [AI Analysis & Reporting](#6-ai-analysis-reporting)
7. [Mission Control Dashboard](#7-mission-control-dashboard)
8. [Integrations & Delivery](#8-integrations-delivery)

## Getting started

**How to access it**

- **Web:** Mission Control dashboard, served by `argus serve` (e.g. `argus serve --port 8080`, then open `http://localhost:8080/dashboard`). A self-refreshing console with side-rail navigation, dark theme, status hero, category action panels, ⌘K command palette, schedules, QA-run history, and optional Kubernetes pod health. Serve over HTTPS with `argus serve --tls` (self-signed cert under `~/.zyvor-argus/tls`).
- **CLI:** Primary interface is the `argus` CLI (installed by `make install`). Verbs: `run`, `test`, `generate`, `discover`, `create`, `regression`, `serve`, plus the Mission Control actions `flow`, `route-sweep`, `api-test`, `auth-test`, `realtime`, `vitals`, `ai-test`. Examples: `argus test exec` (smoke, no LLM), `argus test run --source github --spec docs/specs/my-feature.md`, `argus test generate --spec my-specs/products-page.md`, `argus test create "Verify the homepage Schedule Demo button is visible" --execute`.
- **API:** The dashboard is a thin client over documented JSON endpoints you can script directly: `POST /api/dashboard/jobs` `{kind, params}` to run any action, `GET /api/dashboard/jobs/status` and `POST /jobs/cancel` to watch/stop, `GET /api/dashboard/overview` · `/pods` · `/events` · `/tests` · `/runs`, `GET/POST/DELETE /api/dashboard/schedules[/{id}]`, `GET /api/dashboard/jobs/report.{csv,html,pdf}`, and the HMAC-verified `POST /webhook/github`. `/health` and `/webhook/github` stay open even when login is on.
- **Login:** No auth by default (local-dev mode). Set `DASHBOARD_PASSWORD` (and optionally `DASHBOARD_USER`, default `admin`) to gate `/dashboard`, its API, and all artifacts behind the login screen; sessions are signed cookies (12 h, or 30 days with "remember me") and login is rate-limited (8 failures / 5 min per IP → 5-min lockout). `deploy-remote.sh` generates a per-host password automatically (skip with `--no-auth`). GitHub webhook payloads are authenticated separately by `GITHUB_WEBHOOK_SECRET` (HMAC). **Argus Enterprise (Watchfloor)** uses Keycloak / OIDC instead — seeded demo accounts after a default install are `demo`/`demo` and `ssouser`/`Sso@321`; see [user/enterprise-sso.md](user/enterprise-sso.md) or [enterprise-sso-oidc.md](enterprise-sso-oidc.md).
- **Needs:** Python 3.9+ (3.11 recommended) and Node.js 20+; copy `.env.example` to `.env` (at minimum set `ZYVOR_BASE_URL`); for GitHub sources run `gh auth login` (or set `GITHUB_TOKEN`) and `ZYVOR_PRODUCT_REPO=owner/repo`; an LLM key (`LLM_PROVIDER` + matching key) is optional except for `argus test create`.

**Your first workflows**

- **First run — smoke a site with no LLM or GitHub**
  1. Install: `cp .env.example .env` then `make install` (installs the `argus` CLI, Playwright, and Chromium).
  1. Set the target in `.env`: `ZYVOR_BASE_URL=https://zyvor.dev` (leave LLM and GitHub sections empty).
  1. Run the hand-written smoke suite: `argus test exec` — expect `Results: 11 passed, 0 failed`.
  1. Run the full pipeline against the example spec: `argus test run --source local` (parse → generate → execute → report).
  1. Read the report: `open reports/qa-summary.html` (or `npm run report` for the interactive Playwright report).
- **From a markdown spec to a running test**
  1. Write a user-story spec with an `## Acceptance Criteria` section, e.g. `my-specs/products-page.md`.
  1. Generate without running: `argus test generate --spec my-specs/products-page.md`.
  1. Inspect what the parser understood: `cat tests/fixtures/requirements.json`; rephrase any criterion that didn't become a step.
  1. Run the full pipeline: `argus test run --source local --spec my-specs/products-page.md` (exit code is non-zero on failure, so it's CI-safe).
  1. Optional: set `LLM_PROVIDER` + key in `.env` and regenerate for free-form prose and richer TypeScript (a quality gate falls back to the template if the LLM output is bad).
- **Add a one-off test from plain English**
  1. Configure an LLM in `.env` (this is the one feature with no non-LLM fallback): `LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY=…` (or `ollama` for a local, free model).
  1. Create and run in one step: `argus test create "Check that /vm page loads and mentions KubeVirt migration" --execute`.
  1. Name the route and exact visible text in the description — they become `page.goto` and `getByText` assertions.
  1. To keep the check permanently, promote the generated file from `tests/generated/` into `tests/manual/`, or turn the description into a versioned markdown spec.
- **Wire GitHub as the requirement source and comment on PRs**
  1. Authenticate: `gh auth login` (or set `GITHUB_TOKEN` in `.env`); token needs `Contents: Read` (+ `Pull requests: Write` for comments).
  1. Point at the product repo in `.env`: `ZYVOR_PRODUCT_REPO=owner/repo` (owner/repo, not a URL) and `ZYVOR_BASE_URL`.
  1. Run from a single spec and post results: `argus test run --source github --spec docs/specs/my-feature.md --pr-number 42`.
  1. Or run from everything (`qa`-labeled issues + `docs/specs/` + README/CHANGELOG): `argus test run --source github`.
  1. For automatic runs, set `GITHUB_WEBHOOK_SECRET`, start `argus serve --port 8080`, and add a repo webhook to `https://:8080/webhook/github` for `push`, `pull_request`, and `repository_dispatch`.
- **Turn on self-healing autofix for a nightly run**
  1. In `.env` enable diagnosis and repair: `ENABLE_AUTOFIX=true`, `ENABLE_AUTOFIX_APPLY=true`, `AUTOFIX_MAX_RETRIES=2` (keep `ENABLE_LLM_ANALYSIS=true` for useful suggestions).
  1. Run the pipeline with a failing test: `argus test run --source local` — the fail branch runs analyze → autofix → apply_autofix → re-execute until green or retries are exhausted.
  1. Review the patch like any diff: `git diff tests/` — self-healing fixes selectors, not intent, so confirm it isn't masking a real regression.
  1. In CI prefer suggestions-only mode (`ENABLE_AUTOFIX=true`, `ENABLE_AUTOFIX_APPLY=false`) with a human-reviewed commit.
- **Operate live from Mission Control**
  1. Start the console: `argus serve --port 8080` and open `http://localhost:8080/dashboard` (add `--tls` for HTTPS beyond localhost).
  1. Expect the **side rail**, **dark theme** (light toggle in header), and **status hero**; ⌘K or **Search** opens the palette; category panels hold action cards.
  1. Run any capability from a card; watch per-test ✓/✗ chips stream live, with a ⏹ Stop button and HTML/PDF/Markdown/CSV download row.
  1. Practice on the public site: watch the [journey .webm](assets/zyvor-dev-mission-control-demo.webm), then re-run — [Test zyvor.dev](user/test-zyvor-dev.md) / [Tutorial 13](tutorials/13-test-zyvor-dev-recording.md).
  1. Point at your CRM sandbox (HubSpot, Pipedrive, Zoho, Salesforce): password login → flow video → route sweep — [Tutorial 19](tutorials/19-crm-golden-paths.md) / [CRM packs](crm/README.md). Zoho and Salesforce need no API token.
  1. Generate run history for the trends sparkline: `argus test run --source local` (each run appends to `reports/history/`).
  1. Turn any job into a recurring monitor from the Schedules panel (5 min – 6 h) — e.g. smoke every 15 min, TLS check daily.
  1. Point at a cluster (in-cluster SA or local kubeconfig) to activate the Pods/Workloads panels; set `DASHBOARD_PASSWORD` before exposing it since it reads pod logs.

## 1. Autonomous Pipeline

_A LangGraph state machine turns requirements into running tests and back into reports — no human in the loop._

- **AI Test Generation** — Reads product requirements and generates ready-to-run Playwright test specs that assert the described behavior. — _Turns written requirements into working coverage without hand-authoring every test._
  - **How:** CLI: `argus test generate --spec ` (parse → generate into `tests/generated/`), or Mission Control's ⚙ Generate card. Rule-based without a key; set `LLM_PROVIDER` + an API key in `.env` for free-form prose and richer TypeScript.
- **Spec-to-Test from GitHub or Documents** — Pulls a markdown spec (or all specs and issues) straight from your product repo, or ingests a local document — markdown, text, or PDF — and produces tests for it either way. — _Requirements and their tests stay in sync whether they live in your repo or in a spec doc someone wrote elsewhere._
  - **How:** CLI: `argus test run --source github --spec docs/specs/my-feature.md` (needs `ZYVOR_PRODUCT_REPO` in `.env` + `gh auth login`); omit `--spec` to fetch all `qa`-labeled issues, `docs/specs/`, and README/CHANGELOG. For a document, use `--source document` with one or more file paths — PDF text is extracted automatically, no separate conversion step. Dashboard: the ▶ Full pipeline card with `--source github`.
- **Requirement Quality Scoring & History** — Every ingested requirement is scored for gaps and ambiguity (missing acceptance criteria, unmeasurable language, contradictions), persisted with a full version history, and traced to the tests generated from it. — _When a requirement changes, you see exactly which existing tests may need review — and every low score comes with a named reason, never a bare number._
  - **How:** Runs automatically as part of `parse → evaluate_quality → generate` on every pipeline run — no flag needed; falls back to a rule-based scorer without an LLM key. Query the results via `GET /api/v2/requirements`, `/{id}`, and `/{id}/history`.
- **Natural-Language Test Creation** — Describe a check in plain English with `argus test create` and get a generated Playwright test, optionally run on the spot. — _Anyone can add a test without knowing Playwright or selectors._
  - **How:** CLI: `argus test create "" [--execute]`, or Mission Control's ✨ Create from English card. Requires an LLM (`LLM_PROVIDER` + key, or a local `ollama` model) — this is the one feature with no rule-based fallback.
- **Autonomous AI Browser Agent** — Give a goal like 'create an Ubuntu VM with 2GB RAM' and the agent drives the real browser step by step to accomplish it. — _Tests intent, not scripts — the agent figures out the clicks itself._
  - **How:** CLI: `argus api ai-test  --goal "" [--session .json --insecure]`, or the 🤖 AI test card (URL, goal, session, max-steps). LLM decider by default (`LLM_PROVIDER` + key); a heuristic decider runs standard forms without a key.
- **Self-Healing Autofix** — After a failure the LLM proposes selector repairs, and can optionally patch the spec and re-run until it passes. — _Brittle selectors heal themselves instead of paging an engineer._
  - **How:** Config in `.env`: `ENABLE_AUTOFIX=true` for suggestions, add `ENABLE_AUTOFIX_APPLY=true` + `AUTOFIX_MAX_RETRIES=2` to patch files and re-run. Runs on the fail branch of `argus test run`; review with `git diff tests/`.
- **Coverage Discovery & Expansion** — Scans repo code and docs for untested routes and pages, reports the gaps, and generates tests to close them. — _Surfaces the pages nobody remembered to test and fills them in._
  - **How:** CLI: `argus test discover --source github` reports gaps; `argus test run --source github --expand-coverage` (or `argus test generate … --expand-coverage`) generates the missing `coverage-*.spec.ts`. Or set `ENABLE_COVERAGE_EXPANSION=true` in `.env` for webhook/default GitHub runs.

> No API key? Parsing, generation, analysis, and summaries all fall back to rule-based implementations — only natural-language creation strictly requires an LLM.

## 2. Browser & Journey Testing

_Drive real user journeys across browsers and devices, and catch visual regressions frame by frame._

- **E2E Flow Tests** — Drives a multi-step journey — log in, navigate, fill a wizard, assert the outcome — as one continuous session recorded to a single video and Playwright trace. — _Watch the whole user journey succeed or fail, then time-travel debug it._
  - **How:** CLI: `argus flow run  --steps  | --describe "" [--video --username --password --insecure --session  --no-trace]`, or Mission Control's 🎬 Flow test card. Steps stream live; a `journey.webm` video and `trace.zip` land in `reports/jobs/-flow/`.
- **CRM golden-path packs** — Ready-made auth + `.steps` + route lists for HubSpot, Pipedrive, Zoho CRM, and Salesforce Sales Cloud (Classic). Zoho and Salesforce are UI-first (username/password only; API optional). — _Smoke your CRM sandbox the same way you smoke zyvor.dev._
  - **How:** [Tutorial 19](tutorials/19-crm-golden-paths.md) and [docs/crm/](crm/README.md). Auth with `argus api auth-test --login-url …`, then `argus flow run --steps docs/assets/crm/<pack>-demo.steps --session <host> --video`.
- **HAR Record / Replay** — Captures network traffic as a HAR file, then drives the UI against that recording (offline-friendly contract for the page). — _Prove the UI still works when the backend is mocked from a real capture._
  - **How:** CLI: `argus api har-replay <url> --mode record|replay --har path [--routes /, /pricing]`, or the 📼 HAR card in Mission Control.
- **Import Playwright Codegen** — Paste codegen JS/TS (or record locally with `node playwright/scripts/record-flow.mjs`) and convert it into runnable flow steps. — _Turn an interactive recording into a permanent journey without rewriting selectors by hand._
  - **How:** CLI: `argus test import-codegen <file|-> [--run --url …]`, or the 📥 Import codegen card.
- **Smoke Tests** — Runs the hand-written smoke suite against any target with a single command and no LLM required. — _A fast, dependency-free health check for every deploy._
  - **How:** CLI: `argus test exec` (runs everything in `tests/manual/` with Chromium), or the ▶ Smoke card in Mission Control. Targets `ZYVOR_BASE_URL`; no LLM key needed.
- **Route Sweep** — Screenshots every route at desktop and mobile, then pixel-diffs each shot against a saved baseline, masking dynamic content. — _Catches unintended visual changes across the whole site at once._
  - **How:** CLI: `argus vision route-sweep  --routes "/,/pricing" [--mobile --update-baselines --auto --insecure]`, or the 🗺 Route sweep card. Baselines persist under `reports/artifacts/route-baselines/`; tune the pass threshold with `VISUAL_MAX_DIFF_RATIO`. To promote one changed screenshot to the new baseline without overwriting every route (what `--update-baselines` does), use `POST /api/v2/jobs/{job_id}/route-sweep/approve` with `{route, viewport}` — it records who approved what, when, in the audit log. This is what Argus Enterprise's Quality → Visual baselines panel drives.
- **Visual Regression** — Compares captured screenshots against committed baselines with a configurable pixel-diff threshold. — _Fails the build when the UI shifts unexpectedly, not when you meant it to._
  - **How:** Config in `.env`: `ENABLE_REGRESSION=true` (+ `REGRESSION_THRESHOLD`) compares against `screenshots/baselines/`. CLI: `argus vision regression [--update-baselines]`, or the 👁 Visual regression card.
- **Test Any Site** — Crawls every page of any URL, generates a check per page, and runs them all — login and self-signed TLS supported. — _Point it at a URL and get instant coverage with zero setup._
  - **How:** Mission Control's 🌐 Test any site card (URL, optional login, self-signed TLS toggle). For a standalone crawl into the coverage inventory, set `ENABLE_LIVE_CRAWL=true` (+ `CRAWL_MAX_PAGES`/`CRAWL_MAX_DEPTH`) or run `npm run crawl`.
- **Visual Compare** — Pixel-diffs two live URLs side by side, such as staging against production, and produces a diff image and percentage. — _Prove a deploy changed nothing it wasn't supposed to._
  - **How:** Mission Control's 🔀 Compare card — enter the two URLs; entirely UI/API-driven (`POST /api/dashboard/jobs {kind: "compare"}`) with no extra environment variables. Produces a side-by-side + diff image + % in the result panel.
- **Flaky Detection** — Runs a suite N times and ranks each test by its flake rate. — _Finds the unreliable tests before they erode trust in the whole suite._
  - **How:** Mission Control's 🎲 Flaky check card — pick the run count N; UI/API-driven with no extra environment variables. Results feed the Test health panel's flaky badges.
- **Test intelligence** — Classifies suite health (healthy / failing / flaky / selector / assertion / infra / data), quarantines flakes with TTL + owner, selects which tests a git change should run, and opens a failure studio for a red job. — _Answers which tests are flaky vs broken, what this PR should run, and where the video/trace lives — without growing the store schema._
  - **How:** CLI `argus intel health|select|quarantine-add|quarantine-list|quarantine-release`; APIs `GET/POST/DELETE /api/v2/intel/*` (health, quarantine, select, studio); job kind `select_tests` (static, no engagement). Quarantine is file-backed at `reports/quarantine.json`.
- **Cross-Browser & Device** — Runs on Chromium, Firefox, and WebKit with device profiles and 3G/offline network throttling. — _Confirms the experience holds up beyond your own laptop's browser._
  - **How:** Per-journey CLI flags: `argus flow run  --browser firefox|webkit --device "Pixel 7" --throttle 3g|offline` (set `ZYVOR_BROWSER`/`ZYVOR_DEVICE`/`ZYVOR_THROTTLE`), or the 🎬 Flow card's dropdowns. For the full pipeline, set `ENABLE_MULTI_BROWSER=true` in `.env` (install browsers with `npx playwright install --with-deps`). Filter suites with tags (`@smoke`, `@visual`, `@a11y`) via `argus test exec --grep @smoke` or `ZYVOR_GREP`; shard CI with `--shard 1/2`. Optional: `ENABLE_AUTH_SETUP=true` + dashboard credentials for a Playwright setup project that writes `storageState`; `ENABLE_EMULATION_PROJECTS=true` for dark / reduced-motion / locale projects.

## 3. API, Auth & Real-Time

_Validation that goes past the page — into your REST contracts, sessions, and live streams._

- **API Contract Tests** — Validates REST endpoints against their OpenAPI schema and runs ordered multi-step API workflows with bearer or API-key auth. — _Catches contract drift and broken endpoints before the UI ever sees them._
  - **How:** CLI: `argus api test  --spec  [--token "$JWT" --include-writes]` for schema validation, or `--workflow .json` for ordered create→poll→delete steps with `{{variable}}` interpolation. Dashboard: the 🔌 API contract card (base URL, spec URL or inline JSON, optional bearer token).
- **API Contract Diff** — Pure-Python OpenAPI breaking-change diff between two specs (URL, `git:<ref>:<path>`, or inline JSON). — _Flags removed fields and new required params before merge._
  - **How:** Mission Control → **API** → 🆚 API contract diff (no engagement; static analysis only).
- **Contract Verify** — HAR-derived consumer expectations checked against a live provider. — _Honest on-ramp to consumer contracts without a Pact broker._
  - **How:** Mission Control → **API** → 🤝 Contract verify (HAR path + provider URL; needs an engagement).
- **Auth & Session Tests** — Logs in, saves a reusable session, and asserts logout, expiry, and negative-auth behavior. — _Verifies access control works — and hands other tests a ready session to reuse._
  - **How:** CLI: `argus api auth-test  --api-login /api/v1/auth/login | --login-url /login --username  --password  [--protected /dashboard --logout-url …]`, or the 🔐 Auth & session card. A passed run saves the session to `reports/artifacts/auth/.json` for reuse via flow/realtime `--session`.
- **Live-Data Assertions** — Confirms WebSocket and SSE streams are actually delivering messages, covering reconnect, bearer/subprotocol/ticket auth, and live-region updates. — _Proves real-time dashboards are live, not just loading._
  - **How:** CLI: `argus flow realtime  --ws /path | --sse /path [--expect-messages 3 --token "$JWT" --subprotocol-jwt --ticket-url /path --live-selector  --session ]`, or the 📡 Live data card. Flags a hung page `slow` via the `ZYVOR_SLOW_MS` latency budget instead of a false pass.

> A saved auth session can be reused by flow and real-time tests — and any target-site password is redacted from job status, history, and the live panel.

## 4. Performance & Web Quality

_Grade speed, accessibility, SEO, and code coverage on a single pass._

- **Core Web Vitals** — Measures and grades LCP, CLS, INP, FCP, and TTFB with device and network-throttle profiles. — _Know how fast real users perceive the page, with a letter grade per metric._
  - **How:** CLI: `argus watch vitals  [--throttle 3g --device "iPhone 14"]`, or Mission Control's 📊 Web Vitals card with device and throttle dropdowns. Each metric is graded good / needs-improvement / poor against Google's thresholds.
- **Site Audit with A–F Grade** — Runs per-page accessibility (axe-core), links, SEO, console errors, performance, and security-header checks into a pass/warn/fail matrix and overall grade. — _One report card for the whole quality picture of any page._
  - **How:** Mission Control's 🔬 Site audit card (enter the page URL) — UI/API-driven (`POST /api/dashboard/jobs {kind: "audit"}`) with no extra environment variables. Returns a pass/warn/fail matrix ending in an A–F health grade.
- **Load Test** — Fires N requests at a chosen concurrency and reports p50/p95/p99 latency and requests per second. — _A quick throughput and latency read without a separate load tool._
  - **How:** Mission Control's ⏱ Load test card — set N requests and concurrency C; UI/API-driven with no extra environment variables (in-pod runs are capped to avoid resource exhaustion). Reports p50/p95/p99 latency and req/s.
- **V8 JS Coverage** — Collects Chromium V8 JavaScript coverage per test run and aggregates it into the report as a percentage. — _Shows how much of your shipped JavaScript your tests actually exercise._
  - **How:** Config in `.env`: `ENABLE_V8_COVERAGE=true`, then run any suite (`argus test exec` / `argus test run`). Artifacts land in `reports/v8-coverage/` and the percentage is summarized in the HTML/PR report.

## 5. Network & Security Probes

_One-shot checks that inspect the wire, the headers, and the certificate._

- **TLS Certificate Check** — Resolves DNS and reports the certificate issuer, expiry, protocol, and SANs. — _Catches expiring or misconfigured certificates before browsers do._
  - **How:** Mission Control's 🔒 TLS check card (enter the host/URL) — UI/API-driven with no extra environment variables, and schedulable from the Schedules panel (e.g. TLS check daily).
- **Exposed-Path Probe** — Probes for sensitive paths such as `/.env` and `/.git/config`, aware of SPA fallbacks. — _Flags accidental secret exposure that a functional test would miss._
  - **How:** Mission Control's 🧰 Probes card → Exposed paths (enter the base URL) — one-shot, UI/API-driven with no extra environment variables. SPA-fallback aware so a catch-all route isn't a false positive.
- **Cookie & Header Inspection** — Reports Secure/HttpOnly/SameSite cookie flags, full response headers, CORS policy, and compression. — _Verifies the security and caching posture of every response._
  - **How:** Mission Control's 🧰 Probes card → Headers / Cookies / CORS / Compression sub-probes — each renders as a table with flagged issues; UI/API-driven with no extra environment variables.
- **Redirect & Routing Probes** — Traces the full redirect chain, robots/sitemap presence, sitemap URLs, DNS records, and API status with JSON-path assertions. — _Confirms crawlers, DNS, and routing behave exactly as intended._
  - **How:** Mission Control's 🧰 Probes card → Redirects / Robots-sitemap / Sitemap URLs / DNS / API check sub-probes (the API check supports a JSON-path assertion) — UI/API-driven with no extra environment variables and schedulable.

> Ten probes ship in total — redirects, headers, cookies, robots/sitemap, exposed paths, API check, sitemap URLs, DNS, CORS, and compression.

## 6. AI Analysis & Reporting

_Every run ends in an explanation a human can act on and a bundle they can share._

- **LLM Failure Analysis** — Reads traces and screenshots after a failure to propose a root cause and a fix. — _Turns a red X into a starting point instead of a mystery._
  - **How:** Config in `.env`: `ENABLE_LLM_ANALYSIS=true` (default). Runs automatically on the fail branch of `argus test run`, building root cause, affected area, and flake assessment from error messages, console/network logs, and artifact paths; a stub fallback runs without a key.
- **Plain-English Summaries** — Generates a human-readable run summary suitable for posting as a PR comment. — _Reviewers see what changed and what broke without reading raw logs._
  - **How:** Config in `.env`: `ENABLE_LLM_REPORT=true` (default). The summary appears in the report and, when you add `--pr-number 42` to a `argus test run --source github`, is posted as the PR comment; a stub fallback runs without a key.
- **CSV / HTML / Markdown / PDF Reports** — Writes a downloadable report bundle for every executed job, with per-failure likely-cause hints. — _Share results in the format each audience actually wants._
  - **How:** Every executed job writes a bundle to `reports/jobs/-/`; download it from the dashboard result panel's Download HTML · PDF · Markdown · CSV row (plus a one-click **⧉ Copy MD** to clipboard) or via `GET /api/dashboard/jobs/report.{csv,html,md,pdf}`. PDF rendering is toggled by `ENABLE_PDF_REPORT` in `.env`; Markdown needs no external tool and is always available.
- **Video, Trace & Screenshot Artifacts** — Captures journey videos, Playwright traces, and per-step screenshots, persisted under `reports/` and downloadable in bulk. — _See exactly what the agent saw when something went wrong._
  - **How:** On by flow `--video` (or `ZYVOR_VIDEO=on` in `.env`); the Playwright trace is on by default (`--no-trace` to skip). Browse the 🎬 videos panel and `⬇ all videos (zip)` in Mission Control; open a `trace.zip` at trace.playwright.dev. All persist under `reports/` (PVC-backed on Kubernetes).
- **Test Health & Trends** — Ranks worst-offender tests by fail count and flake rate and tracks a pass-rate sparkline over the last 30 runs. — _Spot the tests and trends that need attention over time._
  - **How:** Mission Control's Test health panel (worst-offender ranking) and QA Runs pass-rate sparkline (last 30 runs); scriptable via `GET /api/dashboard/tests` and `GET /api/dashboard/runs?limit=`. Classified overlay + quarantine status: `GET /api/v2/intel/health` / `argus intel health`. Per-job failure studio (video/trace/error/category): `GET /api/v2/intel/studio/{job_id}`. Each pipeline run appends to `reports/history/`.

## 7. Mission Control Dashboard

_A live console that runs every capability on demand and watches your cluster while it does._

- **Live Status Console** — A self-refreshing dashboard with a glanceable verdict, stat tiles, and streamed per-test pass/fail output for the running job. — _One screen tells you whether everything is green right now._
  - **How:** Run `argus serve` and open `/dashboard` — side rail, dark theme, status hero (ALL SYSTEMS GO / DEGRADED / SYSTEMS DOWN / CLUSTER OFFLINE). Auto-refreshes every 5 s (press `r` to refresh now). Scriptable via `GET /api/dashboard/overview`.
- **Console UX** — Grouped side rail (Console / Testing / Security / Operations), collapsible rail, theme toggle, macOS Terminal live logs, ⌘K / Search palette — reduced-motion aware. — _Clean Apple-style ops console, not a generic admin form._
  - **How:** Navigate via the rail or `#pipeline` / `#ask` / `#requirements` hash URLs; ⌘K (Ctrl-K) or header **Search** for the palette; moon/sun for light mode. Details: [Using the Dashboard](user/using-the-dashboard.md).
- **On-Demand Actions Panel** — Launch any of 25+ QA capabilities from a category panel or the ⌘K command palette, with Copy / Save / Stop and a live log. — _Run any check without touching a terminal._
  - **How:** Open a category (Pipeline, Visual, Security testing, …), click an action card, or press ⌘K; one job runs at a time with a live terminal log. Scriptable via `POST /api/dashboard/jobs {kind, params}`, `GET /jobs/status`, `POST /jobs/cancel`. Try Flow + HAR against zyvor.dev: [Test zyvor.dev](user/test-zyvor-dev.md).
- **Requirements panel** — Versioned requirements with quality scores, linked tests, shared data models & flows, co-occurrence edges, and typed Order → Payment dependencies (SVG canvas). — _See what the pipeline ingested and what a change might touch._
  - **How:** Side rail **Requirements**; APIs `GET /api/v2/requirements` and `GET /api/v2/requirements/impact-graph` (`data_models`, `flows`, `model_edges`, `model_dependencies`). Sources include `github`, `document`, `email` (`.eml` or IMAP), `transcript`, `jira` (JSON / REST / OAuth), and `diarize`.
- **Recurring Schedules** — Turns any job into a recurring monitor from 5 minutes to 6 hours, re-triggered by a background scheduler. — _Smoke every 15 minutes, audit hourly, TLS daily — set and forget._
  - **How:** Add/remove schedules from **Runs & schedules → Schedules** or the ⌘K palette (interval 5 min – 6 h; a background thread re-triggers due jobs, single-flight). Scriptable via `GET/POST/DELETE /api/dashboard/schedules[/{id}]`.
- **Kubernetes Pod Health** — Shows per-pod phase, restarts, events, CPU/memory, and live log tails, with a restart button and namespace events. — _Watch the platform under test and the agent's own pods in one place._
  - **How:** Activates automatically when a cluster is reachable (in-cluster service account, else local kubeconfig). Scope it with `DASHBOARD_NAMESPACE` and `DASHBOARD_POD_SELECTOR` in `.env`. Scriptable via `GET /api/dashboard/pods`, `…/pods/{name}/logs`, `DELETE …/pods/{name}` (restart).
- **Authenticated & TLS-Ready** — Optional password login (rate-limited, signed-cookie sessions) gates the dashboard, API, and artifacts; serve over HTTPS with a self-signed cert. — _Safe to expose a console that can read pod logs._
  - **How:** Config in `.env`: set `DASHBOARD_PASSWORD` (+ optional `DASHBOARD_USER`) to enable login; `/health` and `/webhook/github` stay open. Serve HTTPS with `argus serve --tls` (self-signed under `~/.zyvor-argus/tls`) or `--tls-cert`/`--tls-key`.
- **Ask Zyra (knowledge RAG)** — Optional citation-first Q&A over product docs (Qdrant hybrid retrieval) inside Mission Control, with streaming answers and optional read-only live cluster tools. — _Ask “why is egress failing?” without leaving the console._
  - **How:** Install `pip install -e ".[knowledge,fastembed]"`, start Qdrant, ingest docs, set `LLM_API_KEY` — see [Tutorial 14](tutorials/14-ask-zyra-knowledge.md). For labs without OpenAI-compatible embeddings, set `EMBEDDING_BACKEND=fastembed`. Side rail **Ask Zyra**; API `POST /v1/qa` (+ `/v1/qa/stream`). Primary agent stays read-only; remediation HITL is a separate gated agent.
- **Scriptable JSON API** — The whole console is a thin client over documented JSON endpoints for jobs, schedules, runs, pods, and reports. — _Automate the same actions the UI performs from your own tooling._
  - **How:** Call the JSON endpoints directly, e.g. `POST /api/dashboard/jobs {kind, params}` to run an action, `GET /api/dashboard/runs` for history, `GET /api/dashboard/jobs/report.pdf` for the bundle; read endpoints degrade to `"available": false` when no cluster is reachable.

## 8. Integrations & Delivery

_Wires into GitHub, your chat tools, your LLM of choice, and your cluster._

- **GitHub Integration** — Reads specs and issues, runs on deploy events via an HMAC-verified webhook, and posts result summaries back as PR comments. — _QA rides along with your existing GitHub workflow._
  - **How:** Set `ZYVOR_PRODUCT_REPO` in `.env` + `gh auth login` (or `GITHUB_TOKEN`), then `argus test run --source github [--pr-number 42]`. For automatic runs, set `GITHUB_WEBHOOK_SECRET`, run `argus serve`, and add a repo webhook to `/webhook/github` for `push`/`pull_request`/`repository_dispatch`.
- **Slack, Teams & Email Alerts** — Sends block-formatted Slack messages, Teams adaptive cards, and HTML email with the PDF report attached. — _The right people hear about failures where they already work._
  - **How:** Config in `.env`: set `SLACK_WEBHOOK_URL`, `TEAMS_WEBHOOK_URL`, and/or `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD` + `NOTIFY_EMAIL_TO` (PDF attached when available). Alerts fire on pipeline runs.
- **Provider-Agnostic LLM** — Works with OpenAI, Anthropic, Azure OpenAI, Google, or a local Ollama model through a single configuration switch. — _Use the model and vendor you already trust — or none at all._
  - **How:** Config in `.env`: `LLM_PROVIDER=openai|anthropic|azure|google|ollama`, `LLM_MODEL=`, and the matching key (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `AZURE_OPENAI_API_KEY`+`AZURE_OPENAI_ENDPOINT` / `GOOGLE_API_KEY` / `OLLAMA_BASE_URL`). Omit entirely to use rule-based fallbacks.
- **Kubernetes & Docker Deploy** — Ships manifests for Deployment, Service, Ingress, CronJob, RBAC, and PVC, plus a container image and a one-command remote deploy script. — _Run it as a scheduled in-cluster job or a standing service._
  - **How:** Apply the manifests under `kubernetes/` with `make k8s-validate` then `make k8s-apply`; port-forward the dashboard (`kubectl port-forward svc/argus-webhook 8080:80`). Or deploy to a host with `scripts/deploy-remote.sh   [--service --tls]`.
- **CI/CD Workflows** — Includes GitHub Actions for lint and unit tests, nightly and PR smoke runs, and post-deploy validation via repository dispatch. — _Coverage runs automatically on push, PR, schedule, and deploy._
  - **How:** Ships GitHub Actions in `.github/workflows/` (lint, unit, nightly/PR smoke, post-deploy). Trigger the post-deploy pipeline from your own pipeline with `gh api repos/$ZYVOR_PRODUCT_REPO/dispatches -f event_type=staging-deployed -F 'client_payload[pr_number]=42'`.
- **Rust Diff Accelerator** — An optional `zyvor-diff` Rust binary replaces Pillow for faster screenshot comparison. — _Speeds up visual diffing on large baseline sets._
  - **How:** Config in `.env`: `ENABLE_RUST_PROCESSOR=true`; build the binary with `make rust` (or point `ZYVOR_DIFF_BINARY` at an existing build). Used by visual regression and route-sweep diffing instead of Pillow.

## Getting started

1. **Install** — Copy `.env.example` to `.env` and run `make install` (needs Python 3.9+ and Node.js 20+).
2. **Run a smoke test** — `argus test exec --grep @smoke` against your target (try `ZYVOR_BASE_URL=https://zyvor.dev`) — no LLM key required.
3. **Watch / re-record a journey** — open the [Mission Control demo .webm](assets/zyvor-dev-mission-control-demo.webm), then `argus flow run https://zyvor.dev --steps docs/assets/zyvor-dev-demo.steps --video` — see [Test zyvor.dev](user/test-zyvor-dev.md).
4. **Open Mission Control** — `argus serve` then browse to `/dashboard` (grouped side rail + ⌘K) to run any of the 25+ actions live. Optional: enable [Ask Zyra](tutorials/14-ask-zyra-knowledge.md).
5. **Wire up GitHub** — Set `ZYVOR_PRODUCT_REPO`, authenticate `gh`, then `argus test run --source github --spec docs/specs/my-feature.md`.
6. **Add an LLM (optional)** — Set `LLM_PROVIDER` and the matching API key to unlock AI generation, analysis, and natural-language tests.
7. **Pull the container** — `docker pull ghcr.io/zyvorai/zyvor-argus:v0.9.2` — see [Releases](releases.md).

> **Good to know:** Many features are opt-in behind flags (regression, autofix, coverage expansion, V8 coverage, multi-browser, Rust diff) and are off by default. Without an LLM key the agent still runs but uses rule-based fallbacks for parsing, generation, analysis, and summaries; only `argus test create` strictly requires an LLM. API validation checks HTTP statuses and OpenAPI schemas rather than full business logic. The Mission Control dashboard reads pod logs, so it is intentionally not exposed through the ingress and should be protected with a password. Kubernetes panels require a reachable cluster; without one they show an offline state while everything else keeps working. Multi-browser and load testing are best-effort in-pod and capped to avoid resource exhaustion.

---
_Zyvor Argus is developed by ZyvorAI Labs. Contact **info@zyvor.dev** · Proprietary & Confidential._
