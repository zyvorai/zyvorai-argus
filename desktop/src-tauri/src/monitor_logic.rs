// Copyright 2026 Zyvor AI Labs · https://zyvor.dev
// SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

//! Pure decision logic for the menu-bar monitor: which job changes deserve a
//! notification, and what the tray label says. No I/O and no Tauri types, so
//! it can be unit-tested (even with plain `rustc --test`) without building the
//! app.

use std::collections::HashMap;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum JobState {
    Running,
    Finished,
    Failed,
    /// Stopped by the user -- never worth a notification.
    Cancelled,
}

/// `GET /api/dashboard/jobs` rows carry `running`, `error` and `finished_at`
/// but no status field, so the state is derived.
pub fn derive_state(running: bool, error: Option<&str>, finished_at: Option<&str>) -> Option<JobState> {
    if running {
        return Some(JobState::Running);
    }
    match error.map(str::trim).filter(|e| !e.is_empty()) {
        Some(e) if e == "cancelled by user" => Some(JobState::Cancelled),
        Some(_) => Some(JobState::Failed),
        None if finished_at.map(str::trim).is_some_and(|t| !t.is_empty()) => Some(JobState::Finished),
        None => None, // queued / not started: nothing to report
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JobView {
    pub id: String,
    pub kind: String,
    pub state: JobState,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Notice {
    Finished { kind: String },
    Failed { kind: String },
}

impl Notice {
    pub fn title(&self) -> String {
        match self {
            Notice::Finished { .. } => "Argus job finished".to_string(),
            Notice::Failed { .. } => "Argus job failed".to_string(),
        }
    }

    pub fn body(&self) -> String {
        match self {
            Notice::Finished { kind } => format!("{kind} completed."),
            Notice::Failed { kind } => format!("{kind} failed. Open Mission Control for details."),
        }
    }
}

/// Remembers the last state of each job so only *transitions* notify.
#[derive(Default)]
pub struct Tracker {
    seen: HashMap<String, JobState>,
    primed: bool,
}

impl Tracker {
    /// Feed one poll's job list; returns the notices to show.
    ///
    /// The first poll only records state: the jobs list keeps finished jobs
    /// around, so notifying on it would replay old history at every launch.
    /// After that, a job notifies when it moves from running to finished or
    /// failed -- or when it first appears already finished (it started and
    /// ended between two polls).
    pub fn observe(&mut self, jobs: &[JobView]) -> Vec<Notice> {
        let mut notices = Vec::new();
        if self.primed {
            for job in jobs {
                let before = self.seen.get(&job.id).copied();
                let was_done = matches!(
                    before,
                    Some(JobState::Finished | JobState::Failed | JobState::Cancelled)
                );
                if was_done {
                    continue;
                }
                match job.state {
                    JobState::Finished => notices.push(Notice::Finished { kind: job.kind.clone() }),
                    JobState::Failed => notices.push(Notice::Failed { kind: job.kind.clone() }),
                    JobState::Running | JobState::Cancelled => {}
                }
            }
        }
        // Rebuild so jobs that dropped out of the list are forgotten.
        self.seen = jobs.iter().map(|j| (j.id.clone(), j.state)).collect();
        self.primed = true;
        notices
    }

    pub fn running(&self) -> usize {
        self.seen.values().filter(|s| **s == JobState::Running).count()
    }
}

/// Text next to the menu-bar icon; empty when there is nothing to say.
pub fn tray_label(running: usize, open_findings: Option<u64>) -> String {
    let mut parts = Vec::new();
    if running > 0 {
        parts.push(format!("{running} running"));
    }
    if let Some(n) = open_findings.filter(|n| *n > 0) {
        parts.push(format!("{n} open"));
    }
    parts.join(" · ")
}

/// `http://127.0.0.1:8080/dashboard` (or a remote URL, possibly with a path or
/// query) -> `http://127.0.0.1:8080`. `None` if it is not an http(s) URL.
pub fn origin_of(dashboard_url: &str) -> Option<String> {
    let url = dashboard_url.trim();
    let scheme_end = url.find("://")?;
    let scheme = &url[..scheme_end];
    if scheme != "http" && scheme != "https" {
        return None;
    }
    let rest = &url[scheme_end + 3..];
    let host_end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let host = &rest[..host_end];
    if host.is_empty() {
        return None;
    }
    Some(format!("{scheme}://{host}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn job(id: &str, kind: &str, state: JobState) -> JobView {
        JobView { id: id.into(), kind: kind.into(), state }
    }

    #[test]
    fn derives_state_from_the_fields_the_api_returns() {
        assert_eq!(derive_state(true, None, None), Some(JobState::Running));
        assert_eq!(derive_state(true, Some("boom"), None), Some(JobState::Running));
        assert_eq!(derive_state(false, Some("boom"), Some("t")), Some(JobState::Failed));
        assert_eq!(derive_state(false, Some("cancelled by user"), Some("t")), Some(JobState::Cancelled));
        assert_eq!(derive_state(false, None, Some("2026-10-24T01:00:00Z")), Some(JobState::Finished));
        assert_eq!(derive_state(false, None, None), None);
        assert_eq!(derive_state(false, Some("  "), Some("t")), Some(JobState::Finished));
    }

    #[test]
    fn first_poll_never_notifies_even_for_old_failures() {
        let mut t = Tracker::default();
        let old = vec![job("a", "smoke", JobState::Failed), job("b", "crawl", JobState::Finished)];
        assert!(t.observe(&old).is_empty());
        assert!(t.observe(&old).is_empty(), "unchanged history stays quiet");
    }

    #[test]
    fn notifies_once_when_a_running_job_finishes() {
        let mut t = Tracker::default();
        t.observe(&[job("a", "smoke", JobState::Running)]);
        assert_eq!(t.running(), 1);
        let n = t.observe(&[job("a", "smoke", JobState::Finished)]);
        assert_eq!(n, vec![Notice::Finished { kind: "smoke".into() }]);
        assert_eq!(t.running(), 0);
        assert!(t.observe(&[job("a", "smoke", JobState::Finished)]).is_empty());
    }

    #[test]
    fn notifies_once_when_a_running_job_fails() {
        let mut t = Tracker::default();
        t.observe(&[]);
        t.observe(&[job("a", "dast", JobState::Running)]);
        let n = t.observe(&[job("a", "dast", JobState::Failed)]);
        assert_eq!(n, vec![Notice::Failed { kind: "dast".into() }]);
        assert!(t.observe(&[job("a", "dast", JobState::Failed)]).is_empty());
    }

    #[test]
    fn a_job_that_appears_already_done_after_priming_still_notifies() {
        let mut t = Tracker::default();
        t.observe(&[]);
        let n = t.observe(&[job("fast", "smoke", JobState::Finished)]);
        assert_eq!(n, vec![Notice::Finished { kind: "smoke".into() }]);
    }

    #[test]
    fn cancelled_jobs_are_silent() {
        let mut t = Tracker::default();
        t.observe(&[job("a", "crawl", JobState::Running)]);
        assert!(t.observe(&[job("a", "crawl", JobState::Cancelled)]).is_empty());
        t.observe(&[]);
        assert!(t.observe(&[job("b", "crawl", JobState::Cancelled)]).is_empty());
    }

    #[test]
    fn several_jobs_in_one_poll_each_notify() {
        let mut t = Tracker::default();
        t.observe(&[job("a", "smoke", JobState::Running), job("b", "crawl", JobState::Running)]);
        let n = t.observe(&[job("a", "smoke", JobState::Finished), job("b", "crawl", JobState::Failed)]);
        assert_eq!(n.len(), 2);
        assert!(n.contains(&Notice::Finished { kind: "smoke".into() }));
        assert!(n.contains(&Notice::Failed { kind: "crawl".into() }));
    }

    #[test]
    fn a_job_that_leaves_the_list_is_forgotten_not_renotified() {
        let mut t = Tracker::default();
        t.observe(&[job("a", "smoke", JobState::Running)]);
        t.observe(&[]); // vanished while running (e.g. server restarted)
        assert_eq!(t.running(), 0);
        assert!(t.observe(&[]).is_empty());
    }

    #[test]
    fn notice_text_never_includes_error_details() {
        let n = Notice::Failed { kind: "dast".into() };
        assert_eq!(n.title(), "Argus job failed");
        assert!(n.body().starts_with("dast failed"));
    }

    #[test]
    fn tray_label_shows_only_what_is_nonzero() {
        assert_eq!(tray_label(0, None), "");
        assert_eq!(tray_label(0, Some(0)), "");
        assert_eq!(tray_label(2, None), "2 running");
        assert_eq!(tray_label(0, Some(5)), "5 open");
        assert_eq!(tray_label(1, Some(5)), "1 running · 5 open");
    }

    #[test]
    fn origin_is_scheme_and_authority_only() {
        assert_eq!(origin_of("http://127.0.0.1:8080/dashboard").as_deref(), Some("http://127.0.0.1:8080"));
        assert_eq!(origin_of("https://argus.lab:30080/dashboard?x=1#y").as_deref(), Some("https://argus.lab:30080"));
        assert_eq!(origin_of("http://host").as_deref(), Some("http://host"));
        assert_eq!(origin_of("ftp://host/x"), None);
        assert_eq!(origin_of("not a url"), None);
        assert_eq!(origin_of("http:///dashboard"), None);
    }
}
