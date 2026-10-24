// Copyright 2026 Zyvor AI Labs · https://zyvor.dev
// SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0

mod monitor;
mod monitor_logic;
mod paths;
mod server;

use paths::AppSettings;
use server::ServerState;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::image::Image;
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};

#[tauri::command]
fn dashboard_url(state: tauri::State<ServerState>) -> Result<Option<String>, String> {
    server::dashboard_url(&state)
}

#[tauri::command]
fn get_settings() -> AppSettings {
    paths::load_settings()
}

#[tauri::command]
fn set_settings(settings: AppSettings) -> Result<(), String> {
    paths::save_settings(&settings)
}

fn open_settings_window(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("settings") {
        let _ = w.show();
        let _ = w.set_focus();
        return;
    }
    let _ = WebviewWindowBuilder::new(app, "settings", WebviewUrl::App("settings.html".into()))
        .title("Zyvor Argus Settings")
        .inner_size(440.0, 380.0)
        .resizable(false)
        .build();
}

fn show_main_window(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

/// Menu-bar item: shows running-job and open-finding counts (kept current by
/// `monitor`), opens the window or Settings, and is the way to quit now that
/// closing the window only hides it.
fn setup_tray(app: &AppHandle) -> tauri::Result<monitor::Handles> {
    let running = MenuItem::with_id(app, "tray_running", "No jobs running", false, None::<&str>)?;
    let findings = MenuItem::with_id(app, "tray_findings", "Open findings: …", false, None::<&str>)?;
    let open = MenuItem::with_id(app, "tray_open", "Open Mission Control", true, None::<&str>)?;
    let settings = MenuItem::with_id(app, "tray_settings", "Settings…", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "tray_quit", "Quit Zyvor Argus", true, Some("CmdOrCtrl+Q"))?;
    let menu = Menu::with_items(
        app,
        &[
            &running,
            &findings,
            &PredefinedMenuItem::separator(app)?,
            &open,
            &settings,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )?;

    let mut builder = TrayIconBuilder::with_id("argus")
        .tooltip("Zyvor Argus")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "tray_open" => show_main_window(app),
            "tray_settings" => open_settings_window(app),
            "tray_quit" => app.exit(0),
            _ => {}
        });
    // A monochrome template image: macOS tints it for light and dark menu bars. Fall
    // back to the colour app icon if the embedded PNG ever fails to decode.
    match Image::from_bytes(include_bytes!("../icons/tray-template@2x.png")) {
        Ok(icon) => builder = builder.icon(icon).icon_as_template(true),
        Err(_) => {
            if let Some(icon) = app.default_window_icon() {
                builder = builder.icon(icon.clone());
            }
        }
    }
    let tray = builder.build(app)?;
    Ok(monitor::Handles { tray, running, findings })
}

/// Native app menu — mainly so ⌘, opens Settings and the standard
/// edit/window commands (⌘C/⌘V/⌘Q/…) exist at all; Tauri doesn't provide a
/// default macOS menu bar for free the way a plain WKWebView-in-Xcode app
/// would.
fn setup_menu(app: &AppHandle) -> tauri::Result<()> {
    let app_menu = Submenu::with_items(
        app,
        "Zyvor Argus",
        true,
        &[
            &PredefinedMenuItem::about(app, None, None)?,
            &PredefinedMenuItem::separator(app)?,
            &MenuItem::with_id(app, "settings", "Settings…", true, Some("CmdOrCtrl+,"))?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::hide(app, None)?,
            &PredefinedMenuItem::hide_others(app, None)?,
            &PredefinedMenuItem::show_all(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::quit(app, None)?,
        ],
    )?;

    let edit_menu = Submenu::with_items(
        app,
        "Edit",
        true,
        &[
            &PredefinedMenuItem::undo(app, None)?,
            &PredefinedMenuItem::redo(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::cut(app, None)?,
            &PredefinedMenuItem::copy(app, None)?,
            &PredefinedMenuItem::paste(app, None)?,
            &PredefinedMenuItem::select_all(app, None)?,
        ],
    )?;

    let window_menu = Submenu::with_items(
        app,
        "Window",
        true,
        &[
            &PredefinedMenuItem::minimize(app, None)?,
            &PredefinedMenuItem::maximize(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::close_window(app, None)?,
        ],
    )?;

    let menu = Menu::with_items(app, &[&app_menu, &edit_menu, &window_menu])?;
    app.set_menu(menu)?;

    app.on_menu_event(|app, event| {
        if event.id().as_ref() == "settings" {
            open_settings_window(app);
        }
    });

    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .manage(ServerState::default())
        .invoke_handler(tauri::generate_handler![
            dashboard_url,
            get_settings,
            set_settings,
        ])
        .setup(|app| {
            setup_menu(app.handle())?;
            let tray = setup_tray(app.handle())?;
            let settings = paths::load_settings();
            server::start_in_background(
                app.handle().clone(),
                settings.argus_bin,
                settings.remote_url,
            );
            monitor::start(app.handle().clone(), tray);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Zyvor Argus desktop app");

    // Killing the spawned `argus serve` child needs to survive every way
    // this single-window app can end, not just one of them — verified live
    // that only handling the window's own CloseRequested event (via
    // `on_window_event`) misses Cmd+Q / Dock "Quit" / the app-menu Quit
    // item, which macOS delivers as an app-level RunEvent::ExitRequested
    // instead, leaving the server orphaned with its port still bound.
    app.run(|app_handle, event| match event {
        RunEvent::WindowEvent {
            label,
            event: WindowEvent::CloseRequested { api, .. },
            ..
        } if label == "main" => {
            // Closing the main window only hides it: `argus serve` keeps
            // running so jobs finish and the menu-bar item keeps reporting.
            // Quit (menu-bar item, app menu or ⌘Q) is what ends the app and
            // kills the child. The settings window just closes normally.
            api.prevent_close();
            if let Some(w) = app_handle.get_webview_window("main") {
                let _ = w.hide();
            }
        }
        // Clicking the Dock icon with every window hidden brings it back.
        #[cfg(target_os = "macos")]
        RunEvent::Reopen { .. } => show_main_window(app_handle),
        RunEvent::ExitRequested { .. } | RunEvent::Exit => {
            server::shutdown(&app_handle.state::<ServerState>());
        }
        _ => {}
    });
}
