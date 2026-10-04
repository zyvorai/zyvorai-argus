<!-- Copyright (c) 2026 ZyvorAI Labs Private Limited. -->
<!-- SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0 -->
# Argus

[![Release](https://img.shields.io/github/v/release/zyvorai/zyvorai-argus?label=release&color=2997ff)](https://github.com/zyvorai/zyvorai-argus/releases/latest)
[![CI](https://github.com/zyvorai/zyvorai-argus/actions/workflows/ci.yml/badge.svg)](https://github.com/zyvorai/zyvorai-argus/actions/workflows/ci.yml)
[![Security](https://github.com/zyvorai/zyvorai-argus/actions/workflows/security.yml/badge.svg)](https://github.com/zyvorai/zyvorai-argus/actions/workflows/security.yml)
[![License: Zyvor Production v1.0](https://img.shields.io/badge/License-Zyvor%20Production%20v1.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776ab?logo=python&logoColor=white)](pyproject.toml)
[![Node 20+](https://img.shields.io/badge/node-20%2B-339933?logo=node.js&logoColor=white)](package.json)
[![TypeScript](https://img.shields.io/badge/typescript-Playwright-3178c6?logo=typescript&logoColor=white)](playwright/)

![Argus — Autonomous QA for the real world.](docs/social/argus-hero-dark.jpg)

[![Book a demo](https://img.shields.io/badge/Book_a_demo-0071e3?style=for-the-badge)](https://zyvor.dev/schedule?utm_source=github&utm_medium=argus&utm_campaign=readme_hero)
[![30-day PoC](https://img.shields.io/badge/30--day_PoC-1d1d1f?style=for-the-badge)](https://zyvor.dev/poc?utm_source=github&utm_medium=argus&utm_campaign=readme_hero)

**Autonomous QA for the real world.** Argus reads your requirements, scores them, generates Playwright tests, runs them on every deploy, and shows what broke — in Mission Control, a live ops console.

No LLM key required for smoke tests, rule-based parsing, and most dashboard actions. Add a provider when you want richer generation and analysis.

![Mission Control — dark theme, side rail, live terminal job panel](docs/assets/zyvor-dev-mission-control-demo.gif)

*Grouped side rail · dark theme · Ask Zyra · macOS Terminal live job · Search / ⌘K · 25+ actions*

## Contents

- [Quickstart](#-quickstart)
- [Mission Control](#-mission-control)
- [Architecture at a glance](#-architecture-at-a-glance)
- [Capabilities](#-capabilities)
- [Why Argus](#-why-argus)
- [Important boundaries](#-important-boundaries)
- [License](#-license)

## 🚀 Quickstart

Requires Python 3.10+, Node 20+, and Docker (only for the container path). `make install` handles the rest, including Playwright's Chromium download.

```bash
git clone https://github.com/zyvorai/zyvorai-argus.git && cd argus
cp .env.example .env          # set ZYVOR_BASE_URL
make install                    # Python venv + Playwright Chromium
argus test exec --grep @smoke   # first green run — no API key
argus serve --port 8080         # → http://localhost:8080/dashboard
```

**Remote deploy in one command:**

```bash
./scripts/deploy-remote.sh YOUR_HOST YOUR_USER --service --key
# Mission Control on port 30080 — credentials printed in deploy summary
```

Container:

```bash
docker pull ghcr.io/zyvorai/zyvor-argus:v0.9.2
docker run --rm -p 8080:8080 --env-file .env ghcr.io/zyvorai/zyvor-argus:v0.9.2 serve --port 8080 --host 0.0.0.0
```

| Track | Where |
| --- | --- |
| **Non-production use** (free under the Zyvor Production License) | This repo |
| **Production / commercial license** | [https://zyvor.dev](https://zyvor.dev/?utm_source=github&utm_medium=argus&utm_campaign=readme_edition) |
| **Docs** | [Tutorials](docs/tutorials/README.md) · [zyvor.dev/docs](https://zyvor.dev/docs?utm_source=github&utm_medium=argus&utm_campaign=readme_suite) |

More: [user manual](docs/user/README.md) · [feature guide](docs/zyvor-argus-user-feature-guide.md) · [configuration](docs/configuration.md) · [remote deploy](docs/remote-deploy.md) · [enterprise overlay](docs/enterprise-v2.md).

## 🖥 Mission Control

`argus serve` exposes **Mission Control** at `/dashboard`.

- **Grouped side rail** — Console (Overview, Ask Zyra) · Testing (Pipeline, Visual, Quality, Journeys, API, Probes, Requirements) · Security · Operations (Runs & schedules)
- **Header** — knowledge lamp, dark/light theme, Search (also **⌘K**)
- **Overview** — hero status, pass rate, next smoke, and a live macOS Terminal job panel (Copy / Save / Stop)
- **Hash routes** — `#pipeline`, `#ask`, `#requirements`, `#operations`

Full action list: [dashboard tutorial](docs/tutorials/10-mission-control-dashboard.md).

## 🗺 Architecture at a glance

```mermaid
flowchart LR
  subgraph Sources["Specs"]
    GH["GitHub"]
    PDF["PDF"]
    Email["Email"]
    Jira["Jira"]
    Transcript["Transcript"]
  end
  Sources --> Pipeline["LangGraph pipeline"]
  Pipeline --> Playwright["Playwright"]
  Playwright --> Console["Mission Control"]
  Pipeline -->|"fail"| Autofix["autofix"]
  Autofix --> Playwright
```

```bash
argus test run --source github --spec docs/specs/feature.md
argus test run --source document --spec requirements/checkout.pdf
argus flow run https://zyvor.dev --steps docs/assets/zyvor-dev-demo.steps --video
```

Command reference: [docs/test-authoring.md](docs/test-authoring.md).

## 🧰 Capabilities

- **Requirements** — versioned, scored, and traced to every generated test, plus impact by shared data models and flows
- **One pipeline** — the same LangGraph path from the CLI or one dashboard click
- **Self-healing autofix** — suggests and applies repairs, then re-runs
- **Contracts** — OpenAPI contract test, breaking-change diff, and HAR consumer verify
- **Authorized security** — misconfig, CVE, SCA, DAST, LLM red-team, and chaos jobs with an audit trail and sandboxed PoC
- **Mission Control** — Console, Testing, Security, and Operations in one rail

Ask Zyra (optional knowledge extra): [docs/tutorials/14-ask-zyra-knowledge.md](docs/tutorials/14-ask-zyra-knowledge.md). What DAST covers, and what it deliberately defers: [docs/security-network-attack-gaps.md](docs/security-network-attack-gaps.md).

## ⚖ Why Argus

| | Without Argus | With Argus |
|---|---|---|
| Specs and tests | Drift apart | Requirements are versioned, scored, and traced |
| Smoke runs | A tribal ritual | One command, or one dashboard click |
| Flaky selectors | Waste the afternoon | Autofix suggests a repair and re-runs |
| API drift | Tribal knowledge | Contract test, OpenAPI diff, HAR verify |
| Security checks | Spreadsheets | Authorized jobs with an audit trail |
| Tooling | Five products | One Mission Control |

## 🔍 Important boundaries

What's free under the Zyvor Production License vs. what needs a commercial license
([full guide](docs/LICENSING.md)):

| Use case | Allowed without a paid license? |
| --- | --- |
| Development, testing, evaluation, research, education | Yes |
| Non-production laboratory and proof-of-concept use | Yes |
| Production environments and customer workloads | No — needs a commercial license |
| SaaS, managed services, OEM, appliances | No — needs a commercial license |
| Redistribution or resale | No — needs written permission and a commercial license |

## 📈 Star History

[![Star History Chart](https://api.star-history.com/svg?repos=zyvorai/argus&type=Date)](https://star-history.com/#zyvorai/argus&Date)

## 📄 License

Licensed under the **[Zyvor Production License v1.0](LICENSE)**.

- **Free** for development, testing, evaluation, research, education, and non-production labs
- **Paid commercial license required** for production, customer workloads, SaaS, managed services, OEM, redistribution, and other revenue-generating use

Commercial terms are issued separately: [https://zyvor.dev](https://zyvor.dev/?utm_source=github&utm_medium=argus&utm_campaign=readme_footer).

Evaluate with the team: [Book a demo](https://zyvor.dev/schedule?utm_source=github&utm_medium=argus&utm_campaign=readme_footer) · [30-day PoC](https://zyvor.dev/poc?utm_source=github&utm_medium=argus&utm_campaign=readme_footer).

See [docs/LICENSING.md](docs/LICENSING.md). Contributions: [CLA.md](CLA.md) + [DCO.md](DCO.md) (`git commit -s`),
governed by our [Code of Conduct](CODE_OF_CONDUCT.md).
