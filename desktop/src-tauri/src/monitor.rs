// Copyright 2026 Zyvor AI Labs · https://zyvor.dev
// SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

//! Menu-bar status and notifications.
//!
//! A background thread polls Mission Control (`/api/dashboard/jobs` and
//! `/api/dashboard/findings`) and keeps the tray label and menu rows current,
//! and posts a macOS notification when a job ends. It polls from Rust rather
//! than from the page because the window's CSP only allows `127.0.0.1` and
//! the dashboard JS cannot reach Tauri APIs.
//!
//! The decisions (what counts as a transition, what the label says) live in
//! `monitor_logic.rs`, which has no I/O and is unit-tested.

use crate::monitor_logic::{derive_state, tray_label, JobView, Tracker};
use crate::server::{self, ServerState};
use serde_json::Value;
use std::thread;
use std::time::Duration;
use tauri::menu::MenuItem;
use tauri::tray::TrayIcon;
use tauri::{AppHandle, Manager, Wry};
use tauri_plugin_notification::NotificationExt;

const POLL: Duration = Duration::from_secs(4);
const BACKOFF: Duration = Duration::from_secs(15);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(3);
/// After this many failed polls in a row, poll slowly until it recovers.
const FAILURES_BEFORE_BACKOFF: u32 = 3;

pub struct Handles {
    pub tray: TrayIcon<Wry>,
    pub running: MenuItem<Wry>,
    pub findings: MenuItem<Wry>,
}

fn get_json(agent: &ureq::Agent, url: &str) -> Result<Value, String> {
    let mut response = agent.get(url).call().map_err(|e| e.to_string())?;
    let text = response.body_mut().read_to_string().map_err(|e| e.to_string())?;
    serde_json::from_str(&text).map_err(|e| e.to_string())
}

fn parse_jobs(body: &Value) -> Vec<JobView> {
    body.get("jobs")
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .filter_map(|row| {
                    let id = row.get("job_id")?.as_str()?.to_string();
                    let kind = row.get("kind").and_then(Value::as_str).unwrap_or("job").to_string();
                    let state = derive_state(
                        row.get("running").and_then(Value::as_bool).unwrap_or(false),
                        row.get("error").and_then(Value::as_str),
                        row.get("finished_at").and_then(Value::as_str),
                    )?;
                    Some(JobView { id, kind, state })
                })
                .collect()
        })
        .unwrap_or_default()
}

fn notify(app: &AppHandle, title: &str, body: &str) {
    eprintln!("[argus-desktop] notify: {title}: {body}");
    if let Err(e) = app.notification().builder().title(title).body(body).show() {
        eprintln!("[argus-desktop] notification failed: {e}");
    }
}

pub fn start(app: AppHandle, handles: Handles) {
    thread::spawn(move || {
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(REQUEST_TIMEOUT))
            .http_status_as_error(true)
            .build()
            .into();
        let mut tracker = Tracker::default();
        let mut failures: u32 = 0;

        loop {
            // Wait for the server (local child or remote URL) to be ready.
            let origin = match server::origin(&app.state::<ServerState>()) {
                Ok(Some(origin)) => origin,
                Ok(None) => {
                    thread::sleep(Duration::from_millis(500));
                    continue;
                }
                Err(_) => return, // the server never came up: nothing to monitor
            };

            match get_json(&agent, &format!("{origin}/api/dashboard/jobs")) {
                Ok(body) => {
                    failures = 0;
                    for notice in tracker.observe(&parse_jobs(&body)) {
                        notify(&app, &notice.title(), &notice.body());
                    }
                    let findings = get_json(&agent, &format!("{origin}/api/dashboard/findings?limit=1"))
                        .ok()
                        .and_then(|v| v.get("total").and_then(Value::as_u64));
                    let running = tracker.running();

                    let _ = handles.running.set_text(match running {
                        0 => "No jobs running".to_string(),
                        n => format!("{n} job{} running", if n == 1 { "" } else { "s" }),
                    });
                    let _ = handles.findings.set_text(match findings {
                        Some(n) => format!("Open findings: {n}"),
                        None => "Open findings: unavailable".to_string(),
                    });
                    let label = tray_label(running, findings);
                    let _ = handles.tray.set_title(if label.is_empty() { None } else { Some(label) });
                }
                Err(_) => {
                    // Server restarting, remote down, or auth required: keep the
                    // last good state and retry; never notify off a failed poll.
                    failures = failures.saturating_add(1);
                }
            }

            thread::sleep(if failures >= FAILURES_BEFORE_BACKOFF { BACKOFF } else { POLL });
        }
    });
}
