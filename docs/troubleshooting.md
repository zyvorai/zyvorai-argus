# Troubleshooting

Common failures, what they mean, and how to fix them.

---

## Install & environment

### `argus: command not found`

The package isn't installed in the active environment.

```bash
pip install -e ".[dev]"
# or the full setup:
make install
```

If you use a virtualenv, activate it first. Verify with `which argus`.

### `npx playwright test` fails with "browserType.launch: Executable doesn't exist"

Browsers aren't installed:

```bash
npx playwright install --with-deps chromium
# multi-browser runs also need firefox + webkit:
npx playwright install --with-deps
```

### `Package 'zyvor-argus' requires a different Python: 3.9.6 not in '>=3.11'`

macOS ships Python 3.9.6 (Xcode CLT). Bare `pip` often points at that interpreter even when `python3 --version` is 3.11+. `make install` creates a repo-root `.venv` with the first working Python ≥ 3.11 it finds (or `uv` if present).

```bash
make install
# or pin an interpreter:
make install PYTHON=python3.12
```

If Homebrew `python3` itself fails (`ensurepip` / `Symbol not found: _XML_SetAllocTrackerActivationThreshold`), it is linked against a newer `libexpat` than `/usr/lib/libexpat.1.dylib`. Use a standalone CPython instead:

```bash
uv python install 3.12
make install PYTHON=python3.12
```

Requires Python ≥ 3.11. Check with `python3 --version`.

---

## GitHub integration

### `GitHub token required. Set GITHUB_TOKEN or run 'gh auth login'.`

Token resolution order is `GITHUB_TOKEN` env var → `gh auth token`. Either:

```bash
gh auth login
# or in .env:
GITHUB_TOKEN=ghp_...
```

Expired CLI token: `gh auth refresh -h github.com`.

### `ZYVOR_PRODUCT_REPO is not set`

Add to `.env` in `owner/repo` form (not a full URL):

```bash
ZYVOR_PRODUCT_REPO=ssahani/hypersdk-web
```

### `Failed to fetch spec from GitHub: 404`

- The path doesn't exist in the repo — check spelling and branch (files are fetched from the default branch).
- The token lacks `Contents: Read` on that repo.
- Verify access:

```bash
curl -s -H "Authorization: Bearer $(gh auth token)" \
  https://api.github.com/repos/<owner>/<repo> | grep full_name
```

### Webhook returns 401 Invalid signature

`GITHUB_WEBHOOK_SECRET` in your environment doesn't match the secret configured on the GitHub webhook. They must be identical; the server verifies `X-Hub-Signature-256` with HMAC-SHA256.

### Webhook receives events but does nothing

Only `push`, `pull_request`, and `repository_dispatch` (with action `staging-deployed`) are handled; everything else returns `{"status": "ignored"}`. Check the event types configured on the GitHub webhook.

---

## Parsing & generation

### `No requirements extracted from specs`

The markdown spec has no parseable content. The rule-based parser (used when no LLM key is set) needs:

- a `# Title` heading
- a `## Acceptance Criteria` section with numbered/bulleted steps
- steps phrased like `` Homepage loads at `https://…` ``, `Click "Button"`, `` X shows `text` ``

See [`prompts/examples/vm-create.md`](../prompts/examples/vm-create.md) for the canonical format, or set an LLM key for free-form specs.

### Generated tests all navigate to `/` or look identical

You're on the template fallback (no LLM key, or LLM output kept failing the quality gate) with requirements that carry no path information. Fix the spec to include concrete paths in acceptance criteria, or configure `LLM_PROVIDER` + API key. Stale stubs can be regenerated:

```bash
rm tests/generated/coverage-*.spec.ts
argus test generate --source github --expand-coverage
```

### `NL parsing failed` from `argus test create`

Natural-language creation is the one feature with **no non-LLM fallback**. Set `LLM_PROVIDER` and the matching API key.

---

## Execution

### `No valid Playwright spec files found to execute`

Every candidate spec failed the pre-execution syntax check (`agents/generator/quality.py`). Inspect the files in `tests/generated/` — usually a truncated LLM response. Delete the broken files and regenerate.

### Tests time out on `networkidle`

`waitForPageReady()` already swallows `networkidle` timeouts (long-polling sites never go idle). If you see hard navigation timeouts instead, the target may be down or slow — check `ZYVOR_BASE_URL` and raise `navigationTimeout` in `playwright/playwright.config.ts` if needed.

### Tests pass locally but fail in CI

- CI runs with `workers: 1` and `retries: 2` (see config) — flakiness surfaces differently.
- CI targets the public site; local `.env` may point elsewhere.
- Check the uploaded `playwright-report` artifact — traces and videos for failures are retained.

---

## Regression

### Every screenshot fails with "No baseline for …"

Baselines haven't been captured yet:

```bash
make regression-update      # capture
make regression             # compare
```

### Regression diffs are noisy / always failing

- Animated or dynamic content (carousels, timestamps) will diff every run — raise `REGRESSION_THRESHOLD` or exclude those pages from baseline capture.
- Baseline and current screenshots taken at different viewport sizes are resized before comparison, which inflates the diff. Recapture baselines after viewport changes.

### `Pillow is required for screenshot regression`

`pip install Pillow` (it's in the project dependencies; you may be in the wrong environment).

### Rust diff not used despite `ENABLE_RUST_PROCESSOR=true`

Build the binary first: `make rust` (needs a Rust toolchain). Or point `ZYVOR_DIFF_BINARY` at an existing build. Without a binary the Pillow path is used for missing-baseline cases and the Rust path reports an error diff.

---

## Reports & notifications

### No PDF generated

PDF rendering shells out to Node + Playwright Chromium (`playwright/scripts/html-to-pdf.mjs`). Ensure `node` is on PATH and Chromium is installed. Disable with `ENABLE_PDF_REPORT=false`. Failures are logged as warnings, not fatal.

### PR comment never appears

- `--pr-number` (or a webhook PR event) must be present — comments are only posted when both repo and PR number are known.
- Token needs `Pull requests: Write`.
- Notification failures are swallowed per-channel (`notify_all` returns per-channel booleans) — run with the webhook server logs visible to see which channel failed.

### Slack/Teams/email silent

Each channel activates only when its variable is set (`SLACK_WEBHOOK_URL`, `TEAMS_WEBHOOK_URL`, `SMTP_HOST`+`NOTIFY_EMAIL_TO`). Exceptions are caught per channel; test the webhook URL with `curl` directly if nothing arrives.

---

## Coverage expansion

### `argus test discover` finds 0 candidates

- `ZYVOR_PRODUCT_REPO` repo has none of the default discovery roots (`docs/`, `src/pages/`, …). Set `COVERAGE_DISCOVERY_PATHS` to match your repo layout.
- Discovery is capped (`COVERAGE_MAX_DISCOVERY_FILES`, `COVERAGE_MAX_DISCOVERY_BYTES`) — huge repos may need higher limits.

### Gaps reported for pages that clearly have tests

Gap matching is heuristic: it looks for the candidate's path/slug in `goto()` calls, `toHaveURL` patterns, and test titles of existing specs. A test that reaches a page indirectly (clicking through) won't register. Add the path as a `goto()` or mention it in the test title.

### Discovery ran but no new tests appeared

New coverage tests are capped by `COVERAGE_MAX_NEW_TESTS` (default 10) and deduplicated against existing test body hashes. Check `Coverage: N candidates, M gaps, K new tests` in the CLI output.

---

## Kubernetes / Docker

### `make k8s-apply` fails immediately

`make k8s-check` requires a reachable cluster. Start one (`minikube start`, `kind create cluster`) or validate offline instead: `make k8s-validate`.

### Container exits instantly

Default CMD is `run --source local`. For a long-running webhook use the Deployment command (`argus serve`). Check `kubectl logs deploy/argus-webhook`.

---

## Still stuck?

- Re-run with the raw Playwright output: `npx playwright test --config=playwright/playwright.config.ts tests/manual --reporter=list`
- Inspect intermediate state: `tests/fixtures/requirements.json` (what was parsed), `tests/generated/` (what was written), `reports/results.json` (what Playwright saw).
- Open the HTML report: `npm run report`.
