mod runtime_status;
mod service;
mod windows_job;

use std::sync::Arc;

use tauri::{image::Image, Manager, RunEvent};

const APP_ICON: Image<'_> = tauri::include_image!("./icons/icon.ico");

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(
            |app, _arguments, _cwd| {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.unminimize();
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            },
        ))
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .setup(|app| {
            if let Some(window) = app.get_webview_window("main") {
                window.set_icon(APP_ICON.clone())?;
            }
            let supervisor = Arc::new(service::ServiceSupervisor::default());
            app.manage(supervisor.clone());
            let app_handle = app.handle().clone();
            let monitor = supervisor.clone();
            tauri::async_runtime::spawn(async move {
                let _ = supervisor.ensure_running(&app_handle).await;
                monitor.start_monitor(app_handle);
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            runtime_status::desktop_runtime_status,
            service::desktop_service_status,
            service::desktop_restart_service
        ])
        .build(tauri::generate_context!())
        .expect("failed to build Codex Cockpit desktop application");

    app.run(|app_handle, event| {
        if let RunEvent::ExitRequested { api, .. } = event {
            let supervisor = app_handle
                .state::<Arc<service::ServiceSupervisor>>()
                .inner()
                .clone();
            if !supervisor.is_shutting_down() {
                api.prevent_exit();
                let handle = app_handle.clone();
                tauri::async_runtime::spawn(async move {
                    supervisor.shutdown(&handle).await;
                    handle.exit(0);
                });
            }
        }
    });
}
