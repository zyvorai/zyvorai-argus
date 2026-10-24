# Zyvor Argus Desktop

A native macOS window around `argus serve`'s Mission Control dashboard —
no browser tab needed. This is a thin shell, not a reimplementation: every
dashboard action (trigger a run, view reports, manage schedules) runs
through the exact same FastAPI server and job queue as `argus serve`
does normally. See [the desktop plan](../ROADMAP.md) for the v1/v2 scope
boundary (v1 wraps an existing local install; it does not bundle a frozen
Python+Node+Playwright runtime).

## Requirements

- Node.js 20+
- Rust (for Tauri 2) — `rustup` or Homebrew
- macOS: Xcode Command Line Tools
- A working `argus` install: the repo root's `.venv` (`make install` from
  the repo root) is auto-detected in dev; a `pip`-installed `argus` on
  `PATH` also works
- Node/Playwright (`npm install && npx playwright install --with-deps
  chromium` from the repo root) — only needed to actually *run* jobs from
  the dashboard, not to view it

## Packaging reality (v2)

A true single-binary `.app` / `.pkg` that embeds Python + Node + Playwright
Chromium is not practical for v1/v2 size (~hundreds of MB of browser
binaries). Recommended setups:

1. **Dev shell (default):** desktop wraps a local `.venv` / `argus` on `PATH`
2. **Lab / team:** set **Settings → Remote URL** to an existing Mission Control
   (`http://host:30080`) — the shell skips spawning local `argus serve` and
   opens that dashboard (Chromium/Playwright stay on the remote)
3. **Container:** use `docker/Dockerfile` when you need a reproducible runner

Code signing / notarization stays optional and needs Apple Developer
credentials (`make desktop-build-signed`).

## Run (npm CLI — like `hypercluster-desktop`)

From the **repo root**:

```bash
npm install
npm run desktop
# or
npx zyvor-argus-desktop
```

Install globally:

```bash
npm install -g ./desktop
zyvor-argus-desktop
```

### Commands

| Command | Description |
|---------|--------------|
| `zyvor-argus-desktop` | Start Tauri dev (default) |
| `zyvor-argus-desktop dev` | Same as above |
| `zyvor-argus-desktop build` | Build the native `.app`/`.dmg` bundle |
| `zyvor-argus-desktop run` | Launch the built `.app` |
| `zyvor-argus-desktop help` | Show usage |

Legacy (still works):

```bash
cd desktop && npm install && npm run tauri dev
```

## How it works

1. On launch, the Rust shell (`src-tauri/src/server.rs`) resolves the
   `argus` binary (`src-tauri/src/paths.rs`: explicit settings override →
   the repo's own `.venv` → `PATH`) and spawns
   `argus serve --host 127.0.0.1 --port <free-port>` — **bound to
   localhost only**, not `serve`'s own CLI default of `0.0.0.0`, so opening
   the app never makes the dashboard reachable from the LAN.
2. `public/index.html` — the entire "frontend," no build step, no
   React/Vite — polls a `dashboard_url` Tauri command until the server is
   ready, then navigates the window straight to
   `http://127.0.0.1:<port>/dashboard`.
3. **Quitting** the app (menu-bar item, **Zyvor Argus → Quit**, or ⌘Q) kills the
   spawned `argus serve` process, so no orphaned server is left running.
   Closing the window only hides it (see below); clicking the Dock icon brings
   it back.

## Menu-bar status and notifications

A menu-bar item shows what Argus is doing while the window is closed:

- the label beside the icon reads e.g. `1 running · 5 open` (blank when idle and
  there are no open findings);
- its menu lists the running-job count and open-findings count, and has
  **Open Mission Control**, **Settings…** and **Quit**;
- a macOS notification appears when a job finishes or fails. Cancelled jobs are
  silent, and the notice names the job kind only, never error text.

How it works (`src-tauri/src/monitor.rs`): a Rust thread polls
`/api/dashboard/jobs` and `/api/dashboard/findings` every 4 s (every 15 s after
repeated failures) on the local server, or on the **Remote URL** if one is set.
The first poll only records state, so launching the app never replays old jobs.
The decisions (what counts as a transition, what the label says) are in
`src-tauri/src/monitor_logic.rs`, which has no I/O and its own unit tests.

Limits: a remote Mission Control that needs a login returns 401 to the poller, so
the label and notifications stay empty there. macOS only shows notifications for a
signed `.app` with a stable bundle id; an unbundled `cargo run` binary may not
display them (the notice is still logged to stderr). The menu-bar icon is the
colour app icon, not a monochrome template image.

## Settings

`⌘,` (or **Zyvor Argus → Settings…** in the menu bar) opens a small
window to override the resolved `argus` binary path — useful if
auto-detection picks the wrong one (e.g. multiple Python environments).
Saved to `~/Library/Application Support/ZyvorArgus/settings.json`; takes
effect on the next app restart, not live.

## Code signing & notarization

Not done automatically — `make desktop-build` produces an **unsigned**
`.app`/`.dmg`. Tauri's bundler signs and notarizes automatically during
`tauri build` when these are present in the environment (no extra script
or config needed beyond that):

| Variable | What it is |
|----------|------------|
| `APPLE_SIGNING_IDENTITY` | Your "Developer ID Application: …" identity, as it appears in `security find-identity -v -p codesigning` |
| `APPLE_CERTIFICATE` | Base64-encoded `.p12` export of that identity (alternative to having it in the local keychain already) |
| `APPLE_CERTIFICATE_PASSWORD` | Password for the `.p12` above |
| `APPLE_ID` | Apple ID email for notarization |
| `APPLE_PASSWORD` | App-specific password for that Apple ID (not your Apple ID password) |
| `APPLE_TEAM_ID` | Your Apple Developer Team ID |

```bash
export APPLE_SIGNING_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export APPLE_ID="you@example.com"
export APPLE_PASSWORD="app-specific-password"
export APPLE_TEAM_ID="TEAMID"
make desktop-build-signed   # fails fast with a clear message if any of these are unset
```

## Icons

The app icon is the Argus "Panoptes" mark (a ring of eight eyes around a
pupil with a green status light). The source of truth is `../brand/`:
`argus-icon.svg` (full detail), `argus-icon-small.svg` (favicon and sizes of
32 px and below) and `argus-mark-template.svg` (black on transparent, for the
macOS menu-bar template icon). `../brand/panoptes.py` generates those SVGs.

To rebuild everything shipped from them (`src-tauri/icons/*`, including
`icon.icns` and `tray-template*.png`, plus the site favicon, apple-touch icon
and share card):

```bash
brew install librsvg        # rsvg-convert
python3 brand/panoptes.py   # only if you changed the mark itself
brand/build-icons.sh
```

## Building

```bash
zyvor-argus-desktop build
# or
make desktop-build   # from the repo root
```

Produces an **unsigned** `.app`/`.dmg` under
`desktop/src-tauri/target/release/bundle/macos/` — fine for local testing.
Code signing/notarization needs an Apple Developer account and isn't wired
up yet (see `ROADMAP.md`).

## Tech stack

- Tauri 2 (Rust) — process spawn/lifecycle only, no custom IPC surface
  beyond `dashboard_url`/`get_settings`/`set_settings`
- A single static HTML file as the frontend — the dashboard itself
  (`templates/dashboard.html.j2`) is server-rendered by the Python backend,
  so there's nothing here to build with React/Vite/Tailwind
