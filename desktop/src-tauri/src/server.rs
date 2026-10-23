// Copyright 2026 Zyvor AI Labs · https://zyvor.dev
// SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

//! Spawn `argus serve` as a child process and track its readiness.
//!
//! Unlike hypercluster's `hypercluster.rs` (which spawns a fresh subprocess
//! per action and streams its output into custom UI panels), there's only
//! ever one long-lived child here: `argus serve` runs once for the
//! app's lifetime, and the dashboard it serves handles every subsequent
//! action itself over HTTP — the Rust side's job is just "start it, know
//! when it's ready, kill it on quit."

use crate::paths;
use std::io::{BufRead, BufReader};
use std::net::{TcpListener, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

const READY_TIMEOUT: Duration = Duration::from_secs(60);
const POLL_INTERVAL: Duration = Duration::from_millis(150);

pub enum ServerStatus {
    Ready { url: String },
    Failed(String),
}

struct ServerHandle {
    child: Child,
}

impl Drop for ServerHandle {
    fn drop(&mut self) {
        // Best-effort: the app is quitting either way. A `argus serve`
        // left running after the window closes would silently keep a port
        // bound and a stale dashboard reachable, so this is not optional.
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[derive(Default)]
pub struct ServerState {
    status: Mutex<Option<ServerStatus>>,
    handle: Mutex<Option<ServerHandle>>,
}

impl ServerState {
    fn set_status(&self, status: ServerStatus) {
        if let Ok(mut guard) = self.status.lock() {
            *guard = Some(status);
        }
    }
}

fn free_port() -> Result<u16, String> {
    let listener = TcpListener::bind(("127.0.0.1", 0)).map_err(|e| e.to_string())?;
    let port = listener
        .local_addr()
        .map_err(|e| e.to_string())?
        .port();
    drop(listener); // release it immediately so `argus serve` can bind it
    Ok(port)
}

fn wait_for_port(port: u16, timeout: Duration) -> bool {
    let start = std::time::Instant::now();
    while start.elapsed() < timeout {
        if TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return true;
        }
        thread::sleep(POLL_INTERVAL);
    }
    false
}

fn spawn_serve(bin_override: Option<&str>, resource_dir: Option<std::path::PathBuf>) -> Result<(Child, u16), String> {
    let bin = paths::resolve_argus_bin(bin_override, resource_dir.as_deref());
    let bundled = paths::bundled_bin(resource_dir.as_deref()).is_some_and(|b| b == bin);
    let port = free_port()?;
    let working_dir = if bundled { paths::bundled_workspace() } else { paths::working_dir() };
    std::fs::create_dir_all(&working_dir).ok();

    // 127.0.0.1, not `serve`'s own CLI default of 0.0.0.0 — a desktop app
    // being opened shouldn't silently make the dashboard reachable from the
    // LAN. This is a spawn-argument choice here, not a change to `serve`'s
    // own default (other callers, e.g. Docker/K8s, still want 0.0.0.0).
    let mut cmd = Command::new(&bin);
    cmd.args(["serve", "--host", "127.0.0.1", "--port", &port.to_string()])
        // See paths::working_dir()'s doc comment — without this, relative
        // state paths (MissionControlStore's ZYVOR_STATE_DB default among
        // them) resolve against whatever CWD the app happened to launch
        // with, not the repo the wrapped `argus` actually belongs to.
        .current_dir(&working_dir)
        // Tells the dashboard template it's running inside this desktop
        // shell, not a normal browser tab — it hides the Kubernetes
        // pods/workloads panel, which is always "cluster unavailable" here
        // (see orchestrator/dashboard/routes.py, templates/dashboard.html.j2).
        .env("ZYVOR_DESKTOP_MODE", "true")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if bundled {
        // Read-only .app: give the backend a writable workspace and a
        // browsers dir for the first-run Chromium download.
        // Bundled Node (for Playwright) goes first on PATH.
        if let Some(dir) = resource_dir.as_deref() {
            let node_bin = dir.join("node");
            if node_bin.is_dir() {
                let old = std::env::var("PATH").unwrap_or_default();
                cmd.env("PATH", format!("{}:{old}", node_bin.display()));
            }
        }
        cmd.env("ZYVOR_HOME", &working_dir)
            .env("PLAYWRIGHT_BROWSERS_PATH", paths::browsers_dir());
    }

    let mut child = cmd.spawn().map_err(|e| {
        format!(
            "failed to launch {} ({e}) — is argus installed? \
             Set a custom path in Settings if it's not on PATH.",
            bin.display()
        )
    })?;

    // Drain stdout/stderr on background threads so a full pipe buffer never
    // blocks the child; forwarded to this process's own stdout/stderr,
    // visible in `tauri dev`'s terminal.
    if let Some(stdout) = child.stdout.take() {
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                println!("[argus serve] {line}");
            }
        });
    }
    if let Some(stderr) = child.stderr.take() {
        thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                eprintln!("[argus serve] {line}");
            }
        });
    }

    if bundled {
        install_browsers_if_missing(&working_dir, resource_dir.as_deref());
    }
    Ok((child, port))
}

/// Chromium is not shipped (hundreds of MB); fetch it once, in the
/// background, into the app-data browsers dir. Jobs that need a browser
/// work as soon as it lands; the dashboard itself does not wait for it.
fn install_browsers_if_missing(workspace: &std::path::Path, resource_dir: Option<&std::path::Path>) {
    let dir = paths::browsers_dir();
    let has_chromium = std::fs::read_dir(&dir)
        .map(|d| d.flatten().any(|e| e.file_name().to_string_lossy().starts_with("chromium")))
        .unwrap_or(false);
    let cli = workspace.join("node_modules/playwright/cli.js");
    let node = resource_dir.map(|r| r.join("node/bin/node"));
    if has_chromium || !cli.is_file() {
        return;
    }
    let Some(node) = node.filter(|n| n.is_file()) else { return };
    let _ = std::fs::create_dir_all(&dir);
    let _ = Command::new(node)
        .args([cli.to_string_lossy().as_ref(), "install", "chromium"])
        .env("PLAYWRIGHT_BROWSERS_PATH", &dir)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn();
}

/// Start the server on a background thread; `status()` reports progress.
/// Called once from the app's `setup()` hook.
///
/// Takes an owned `AppHandle` (Send + Clone + 'static) rather than a
/// borrowed `State<ServerState>` — a `State` reference can't cross a
/// `thread::spawn` boundary, so the handle is captured instead and
/// `.state::<ServerState>()` is re-derived fresh once inside the thread.
///
/// When `remote_url` is set, skips spawning `argus serve` and reports that
/// URL as ready (desktop shell against a lab/team Mission Control).
pub fn start_in_background(
    app_handle: tauri::AppHandle,
    bin_override: Option<String>,
    remote_url: Option<String>,
) {
    thread::spawn(move || {
        use tauri::Manager;
        let state = app_handle.state::<ServerState>();
        let remote = remote_url
            .as_deref()
            .map(str::trim)
            .filter(|u| !u.is_empty())
            .map(|u| u.to_string());
        if let Some(url) = remote {
            let normalized = if url.contains("/dashboard") {
                url
            } else {
                format!("{}/dashboard", url.trim_end_matches('/'))
            };
            state.set_status(ServerStatus::Ready { url: normalized });
            return;
        }
        match spawn_serve(bin_override.as_deref(), app_handle.path().resource_dir().ok()) {
            Ok((child, port)) => {
                if wait_for_port(port, READY_TIMEOUT) {
                    if let Ok(mut guard) = state.handle.lock() {
                        *guard = Some(ServerHandle { child });
                    }
                    state.set_status(ServerStatus::Ready {
                        url: format!("http://127.0.0.1:{port}/dashboard"),
                    });
                } else {
                    state.set_status(ServerStatus::Failed(format!(
                        "argus serve did not become ready on port {port} within {}s",
                        READY_TIMEOUT.as_secs()
                    )));
                }
            }
            Err(e) => state.set_status(ServerStatus::Failed(e)),
        }
    });
}

/// Non-blocking status check for the frontend's poll loop:
/// `Ok(None)` = still starting, `Ok(Some(url))` = ready, `Err` = failed.
pub fn dashboard_url(state: &ServerState) -> Result<Option<String>, String> {
    match state.status.lock().map_err(|_| "server state poisoned")?.as_ref() {
        None => Ok(None),
        Some(ServerStatus::Ready { url }) => Ok(Some(url.clone())),
        Some(ServerStatus::Failed(e)) => Err(e.clone()),
    }
}

/// Kill the child, if running — called on window close.
pub fn shutdown(state: &ServerState) {
    if let Ok(mut guard) = state.handle.lock() {
        *guard = None; // drops ServerHandle -> kills the child
    }
}
