// Copyright 2026 Zyvor AI Labs · https://zyvor.dev
// SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

//! Resolve the `argus` binary and the app's data directory.
//!
//! v1 deliberately wraps an *existing* local install rather than bundling a
//! frozen Python+Node+Playwright runtime (see the desktop plan's Context
//! section) — this cascade is the whole story for "where does the wrapped
//! tool live," mirroring hypercluster's `resolve_hypercluster_bin()`.

use serde::{Deserialize, Serialize};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

#[cfg(target_os = "windows")]
const PATH_SEP: char = ';';
#[cfg(not(target_os = "windows"))]
const PATH_SEP: char = ':';

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct AppSettings {
    /// Explicit override for the `argus` binary path. Empty/absent means
    /// "use the resolve_argus_bin() cascade."
    #[serde(default, alias = "zyvor_qa_bin")]
    pub argus_bin: Option<String>,
    /// When set, open Mission Control at this URL instead of spawning a local
    /// `argus serve` (lab / team packaging path — Chromium lives on the remote).
    #[serde(default)]
    pub remote_url: Option<String>,
}

pub fn app_data_dir() -> PathBuf {
    if cfg!(target_os = "macos") {
        dirs::home_dir()
            .map(|h| h.join("Library/Application Support/ZyvorArgus"))
            .unwrap_or_else(|| PathBuf::from(".zyvor-argus-desktop"))
    } else {
        dirs::data_dir()
            .unwrap_or_else(|| PathBuf::from("."))
            .join("ZyvorArgus")
    }
}

pub fn settings_path() -> PathBuf {
    app_data_dir().join("settings.json")
}

pub fn load_settings() -> AppSettings {
    fs::read_to_string(settings_path())
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

pub fn save_settings(settings: &AppSettings) -> Result<(), String> {
    let dir = app_data_dir();
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let json = serde_json::to_string_pretty(settings).map_err(|e| e.to_string())?;
    fs::write(settings_path(), json).map_err(|e| e.to_string())
}

/// The Zyvor Argus repo root, resolved from *this crate's own source
/// location* at compile time (`CARGO_MANIFEST_DIR`) rather than the
/// process's runtime working directory or executable path — both of those
/// vary depending on how `cargo`/`tauri dev` was invoked, while
/// `CARGO_MANIFEST_DIR` is a fixed, reliable absolute path baked in at
/// build time for whoever compiled this checkout. Returns `None` if this
/// binary wasn't built from within a real Zyvor Argus checkout (`.venv`
/// missing) — e.g. a release build copied elsewhere.
pub fn dev_checkout_root() -> Option<PathBuf> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")); // .../zyvor-argus/desktop/src-tauri
    let repo_root = manifest_dir.parent()?.parent()?.to_path_buf(); // .../zyvor-argus
    repo_root.join(".venv").is_dir().then_some(repo_root)
}

fn dev_checkout_bin() -> Option<PathBuf> {
    let repo_root = dev_checkout_root()?;
    let venv_name = if cfg!(target_os = "windows") {
        "Scripts/argus.exe"
    } else {
        "bin/argus"
    };
    let candidate = repo_root.join(".venv").join(venv_name);
    candidate.is_file().then_some(candidate)
}

/// The PyInstaller onedir sidecar shipped inside the .app
/// (`<Resources>/argus/argus`).
pub fn bundled_bin(resource_dir: Option<&Path>) -> Option<PathBuf> {
    let candidate = resource_dir?.join("argus").join("argus");
    candidate.is_file().then_some(candidate)
}

/// Writable workspace for a bundled app (see orchestrator/paths.py
/// `ensure_workspace`): reports, history and generated tests land here.
pub fn bundled_workspace() -> PathBuf {
    app_data_dir().join("workspace")
}

/// Playwright browsers are downloaded here on first run, not shipped.
pub fn browsers_dir() -> PathBuf {
    app_data_dir().join("browsers")
}

fn path_lookup_bin() -> Option<PathBuf> {
    let name = if cfg!(target_os = "windows") {
        "argus.exe"
    } else {
        "argus"
    };
    for seg in env::var("PATH").unwrap_or_default().split(PATH_SEP) {
        if seg.is_empty() {
            continue;
        }
        let candidate = Path::new(seg).join(name);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

/// Working directory to launch `argus serve` from. `MissionControlStore`
/// (`orchestrator/persistence/store.py`) defaults `ZYVOR_STATE_DB` to the
/// *relative* path `reports/mission-control.db` — unlike the rest of
/// Zyvor Argus's path handling (`_repo_root()`-based, CWD-independent),
/// that one default resolves relative to the process's working directory.
/// Left unset, the spawned child inherits whatever CWD `cargo`/the app
/// bundle happened to launch with (confirmed while testing this: it landed
/// state in `desktop/src-tauri/reports/` instead of the repo root). Pin it
/// explicitly: the real repo root in dev (matching a normal `argus
/// serve` invocation exactly, so state lands in the same place), or the
/// app's own data dir otherwise (stable and app-owned, rather than
/// whatever ambient CWD Finder/launchd happened to provide).
pub fn working_dir() -> PathBuf {
    dev_checkout_root().unwrap_or_else(app_data_dir)
}

/// Resolution order: explicit settings override -> dev checkout's `.venv`
/// -> `argus` on PATH -> bare `"argus"` (last resort; spawning it will
/// fail with a clear "not found" error the UI can surface).
pub fn resolve_argus_bin(settings_override: Option<&str>, resource_dir: Option<&Path>) -> PathBuf {
    if let Some(b) = settings_override {
        if !b.is_empty() && Path::new(b).is_file() {
            return PathBuf::from(b);
        }
    }
    if let Some(b) = bundled_bin(resource_dir) {
        return b;
    }
    if let Some(b) = dev_checkout_bin() {
        return b;
    }
    if let Some(b) = path_lookup_bin() {
        return b;
    }
    PathBuf::from(if cfg!(target_os = "windows") {
        "argus.exe"
    } else {
        "argus"
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dev_checkout_bin_finds_the_real_venv_when_present() {
        // This test runs from within the actual Zyvor Argus checkout, so if
        // `.venv` was set up (`make install`), the cascade's second tier
        // should find it without needing PATH or a settings override.
        if let Some(bin) = dev_checkout_bin() {
            assert!(bin.ends_with("argus") || bin.ends_with("argus.exe"));
        }
    }

    #[test]
    fn resolve_prefers_explicit_override_when_it_exists() {
        let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let this_file = manifest_dir.join("Cargo.toml");
        let resolved = resolve_argus_bin(Some(this_file.to_str().unwrap()), None);
        assert_eq!(resolved, this_file);
    }

    #[test]
    fn resolve_ignores_a_nonexistent_override() {
        let resolved = resolve_argus_bin(Some("/definitely/not/a/real/path/argus"), None);
        // Falls through to the next tier rather than returning the bogus path.
        assert_ne!(resolved, PathBuf::from("/definitely/not/a/real/path/argus"));
    }

    #[test]
    fn app_settings_round_trips_through_json() {
        let settings = AppSettings {
            argus_bin: Some("/tmp/argus".to_string()),
            remote_url: Some("http://lab:30080".to_string()),
        };
        let json = serde_json::to_string(&settings).unwrap();
        let parsed: AppSettings = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.argus_bin, settings.argus_bin);
        assert_eq!(parsed.remote_url, settings.remote_url);
    }

    #[test]
    fn app_settings_default_has_no_override() {
        assert!(AppSettings::default().argus_bin.is_none());
        assert!(AppSettings::default().remote_url.is_none());
    }

    #[test]
    fn app_settings_deserializes_the_shape_settings_html_sends() {
        // Matches exactly what desktop/public/settings.html's Save button
        // sends: invoke("set_settings", { settings: { argus_bin: value
        // || null } }) — a null clears the override, a string sets it.
        let cleared: AppSettings = serde_json::from_str(r#"{"argus_bin": null, "remote_url": null}"#).unwrap();
        assert!(cleared.argus_bin.is_none());
        assert!(cleared.remote_url.is_none());

        let set: AppSettings = serde_json::from_str(
            r#"{"argus_bin": "/x/y/argus", "remote_url": "http://host:30080"}"#,
        )
        .unwrap();
        assert_eq!(set.argus_bin.as_deref(), Some("/x/y/argus"));
        assert_eq!(set.remote_url.as_deref(), Some("http://host:30080"));
    }

    #[test]
    fn app_settings_accepts_legacy_zyvor_qa_bin_key() {
        // Backward compat: existing users' saved settings.json still has the
        // old field name — the serde alias must still load it.
        let legacy: AppSettings = serde_json::from_str(r#"{"zyvor_qa_bin": "/x/y/zyvor-qa"}"#).unwrap();
        assert_eq!(legacy.argus_bin.as_deref(), Some("/x/y/zyvor-qa"));
    }
}
