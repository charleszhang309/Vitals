#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod service;

use service::{Hub, Operation, Status};
use std::sync::mpsc::{self, Sender};
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{
    AppHandle, Manager, RunEvent, Runtime, Url, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_autostart::ManagerExt;

fn open_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

struct TrayMenu<R: Runtime> {
    status: MenuItem<R>,
    start: MenuItem<R>,
    stop: MenuItem<R>,
    restart: MenuItem<R>,
}

impl<R: Runtime> TrayMenu<R> {
    fn update(&self, state: Status) {
        let _ = self.status.set_text(state.label());
        let _ = self.start.set_enabled(matches!(
            state,
            Status::Stopped | Status::Failed | Status::Occupied
        ));
        let _ = self.stop.set_enabled(state == Status::Running);
        let _ = self.restart.set_enabled(state == Status::Running);
    }
}

fn build_app(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let hub = Hub::new(app.handle())?;
    let origin: Url = hub.url().parse()?;
    let allowed_origin = origin.origin();
    let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("Vitals")
        .inner_size(1280.0, 820.0)
        .min_inner_size(900.0, 640.0)
        .visible(false)
        .on_navigation(move |url| {
            url.origin() == allowed_origin
                || url.scheme() == "tauri"
                || (url.scheme() == "http" && url.host_str() == Some("tauri.localhost"))
        })
        .build()?;
    let startup = window.url()?;
    let closing = window.clone();
    window.on_window_event(move |event| {
        if let WindowEvent::CloseRequested { api, .. } = event {
            api.prevent_close();
            let _ = closing.hide();
        }
    });

    let open = MenuItem::with_id(app, "open", "打开 Vitals", true, None::<&str>)?;
    let status = MenuItem::with_id(app, "status", "服务状态：正在启动", false, None::<&str>)?;
    let start = MenuItem::with_id(app, "start", "启动服务", false, None::<&str>)?;
    let stop = MenuItem::with_id(app, "stop", "停止服务", false, None::<&str>)?;
    let restart = MenuItem::with_id(app, "restart", "重启服务", false, None::<&str>)?;
    let autostart = CheckMenuItem::with_id(
        app,
        "autostart",
        "开机自启",
        true,
        app.autolaunch().is_enabled().unwrap_or(false),
        None::<&str>,
    )?;
    let quit = MenuItem::with_id(app, "quit", "退出 Vitals", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let actions = Menu::with_items(
        app,
        &[
            &open, &status, &sep, &start, &stop, &restart, &autostart, &quit,
        ],
    )?;
    let menu = TrayMenu {
        status,
        start,
        stop,
        restart,
    };
    let (send, receive) = mpsc::channel();
    app.manage(send.clone());
    TrayIconBuilder::with_id("vitals")
        .icon(
            app.default_window_icon()
                .expect("bundled Vitals icon")
                .clone(),
        )
        .tooltip("Vitals")
        .menu(&actions)
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            if matches!(
                event,
                TrayIconEvent::Click {
                    button: MouseButton::Left,
                    button_state: MouseButtonState::Up,
                    ..
                }
            ) {
                open_window(tray.app_handle());
            }
        })
        .on_menu_event(move |app, event| {
            let operation = match event.id().as_ref() {
                "open" => {
                    open_window(app);
                    return;
                }
                "start" => Operation::Start,
                "stop" => Operation::Stop,
                "restart" => Operation::Restart,
                "quit" => Operation::Quit,
                "autostart" => {
                    let manager = app.autolaunch();
                    let enabled = manager.is_enabled().unwrap_or(false);
                    let result = if enabled {
                        manager.disable()
                    } else {
                        manager.enable()
                    };
                    if let Err(error) = result {
                        eprintln!("Vitals auto-start: {error}");
                    }
                    let _ = autostart.set_checked(manager.is_enabled().unwrap_or(false));
                    return;
                }
                _ => return,
            };
            let _ = send.send(operation);
        })
        .build(app)?;

    let app = app.handle().clone();
    let background = std::env::args_os().any(|arg| arg == "--background");
    std::thread::spawn(move || {
        let mut initial = true;
        hub.run(receive, |state| {
            menu.update(state);
            if matches!(state, Status::Running | Status::External) {
                let _ = window.navigate(origin.clone());
            } else if !matches!(state, Status::Starting) {
                let _ = window.navigate(startup.clone());
            }
            if initial && !matches!(state, Status::Starting) {
                initial = false;
                if !background {
                    open_window(&app);
                }
            }
        });
        app.exit(0);
    });
    Ok(())
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            open_window(app)
        }))
        .plugin(
            tauri_plugin_autostart::Builder::new()
                .arg("--background")
                .build(),
        )
        .setup(build_app)
        .build(tauri::generate_context!())
        .expect("could not initialize Vitals");
    app.run(|app, event| {
        if let RunEvent::ExitRequested {
            api, code: None, ..
        } = event
        {
            if let Some(send) = app.try_state::<Sender<Operation>>() {
                api.prevent_exit();
                let _ = send.send(Operation::Quit);
            }
        }
    });
}
