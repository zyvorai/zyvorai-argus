# Tutorial 13 — Test zyvor.dev with recording

Hands-on recipe that points Zyvor Argus at **[https://zyvor.dev](https://zyvor.dev)**. Start by **watching the committed journey video**, then re-run the same steps yourself (smoke, flow with video, HAR, Mission Control UX).

**Prerequisites:** [Tutorial 1](01-getting-started.md) (`make install`, Playwright Chromium). No LLM key required for these steps.

---

## Watch the recording

Mission Control driving a GuestKit journey (login → Flow URL → live steps):

Also a raw Playwright capture of zyvor.dev (home → assert HyperSDK → click Products):

<video src="../assets/zyvor-dev-mission-control-demo.webm" controls width="720" title="zyvor.dev journey recording"></video>

- **Video file:** [`docs/assets/zyvor-dev-mission-control-demo.webm`](../assets/zyvor-dev-mission-control-demo.webm)
- **Steps that produced it:** [`docs/assets/zyvor-dev-demo.steps`](../assets/zyvor-dev-demo.steps)
- **Raw GitHub URL:** https://github.com/zyvorai/argus/raw/main/docs/assets/zyvor-dev-mission-control-demo.webm

---

## 0. Point at zyvor.dev

```bash
# .env (minimum)
ZYVOR_BASE_URL=https://zyvor.dev
```

---

## 1. Smoke (no recording)

```bash
argus test exec --grep @smoke
```

Mission Control: ▶ **Smoke** (optional `@smoke` grep).

---

## 2. Re-record the same journey

```bash
argus flow run https://zyvor.dev \
  --steps docs/assets/zyvor-dev-demo.steps \
  --video
# → reports/artifacts/flows/cli/journey.webm
# optional: refresh the committed demo
# cp reports/artifacts/flows/cli/journey.webm docs/assets/zyvor-dev-mission-control-demo.webm
```

**Mission Control:** Actions → 🎬 **Flow test** → URL `https://zyvor.dev` → paste the steps → **record video** on → Run. Download `journey.webm` from the result panel / 🎬 Videos.

---

## 3. HAR record / replay

```bash
argus api har-replay https://zyvor.dev --mode record --routes / --har /tmp/zyvor-home.har
argus api har-replay https://zyvor.dev --mode replay --har /tmp/zyvor-home.har \
  --expect-text Zyvor --not-found-ok
```

---

## 4. Mission Control UX (what to look for)

```bash
argus serve --port 8080
open http://localhost:8080/dashboard
```

| Cue | What it means |
|-----|----------------|
| **Grouped side rail** | Console · Testing · Security · Operations (Overview, Ask Zyra, Pipeline, Requirements, Security testing, Runs & schedules, …) |
| **Dark theme** | Charcoal default; moon/sun toggle in header |
| **Overview hero** | Status lamp + stat tiles |
| **⌘K / Search** | Command palette — jump to Flow, HAR, Ask Zyra, … |
| **Live Terminal panel** | macOS-style log (Copy / Save / Stop) while a job runs |
| **Collapse rail** | Icons-only mode at bottom of side rail |

---

## Related

- [Tutorial 10 — Mission Control](10-mission-control-dashboard.md)
- [Tutorial 11 — Flow tests](11-flow-tests.md)
- [User: test zyvor.dev](../user/test-zyvor-dev.md)
- [User: using the dashboard](../user/using-the-dashboard.md)
