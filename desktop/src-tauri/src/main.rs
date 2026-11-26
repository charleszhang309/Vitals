#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod service;

use service::{Hub, Operation, Status};
use std::sync::mpsc::{self, Sender};
#[cfg(target_os = "macos")]
use tauri::menu::Submenu;
use tauri::menu::{CheckMenuItem, IsMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
#[cfg(not(target_os = "macos"))]
use tauri::tray::{MouseButton, MouseButtonState, TrayIconEvent};
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
    open: MenuItem<R>,
    status: MenuItem<R>,
    separator: PredefinedMenuItem<R>,
    start: MenuItem<R>,
    stop: MenuItem<R>,
    restart: MenuItem<R>,
    autostart: CheckMenuItem<R>,
    quit: MenuItem<R>,
}

impl<R: Runtime> TrayMenu<R> {
    fn new(app: &AppHandle<R>) -> tauri::Result<Self> {
        Ok(Self {
            open: MenuItem::with_id(app, "open", "打开 Vitals", true, None::<&str>)?,
            status: MenuItem::with_id(app, "status", "服务状态：正在启动", false, None::<&str>)?,
            separator: PredefinedMenuItem::separator(app)?,
            start: MenuItem::with_id(app, "start", "启动服务", false, None::<&str>)?,
            stop: MenuItem::with_id(app, "stop", "停止服务", false, None::<&str>)?,
            restart: MenuItem::with_id(app, "restart", "重启服务", false, None::<&str>)?,
            autostart: CheckMenuItem::with_id(
                app,
                "autostart",
                "开机自启",
                true,
                app.autolaunch().is_enabled().unwrap_or(false),
                None::<&str>,
            )?,
            quit: MenuItem::with_id(app, "quit", "退出 Vitals", true, None::<&str>)?,
        })
    }

    fn items(&self) -> [&dyn IsMenuItem<R>; 8] {
        [
            &self.open,
            &self.status,
            &self.separator,
            &self.start,
            &self.stop,
            &self.restart,
            &self.autostart,
            &self.quit,
        ]
    }

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

#[cfg(target_os = "macos")]
fn install_macos_menu(app: &AppHandle) -> tauri::Result<TrayMenu<tauri::Wry>> {
    let services = TrayMenu::new(app)?;
    let native = Menu::default(app)?;
    // AppKit's built-in Quit bypasses the asynchronous Tauri exit request.
    let quit = MenuItem::with_id(app, "quit", "退出 Vitals", true, Some("Cmd+Q"))?;
    let application = Submenu::with_items(
        app,
        "Vitals",
        true,
        &[
            &PredefinedMenuItem::about(app, None, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::services(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::hide(app, None)?,
            &PredefinedMenuItem::hide_others(app, None)?,
            &PredefinedMenuItem::show_all(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )?;
    native.remove_at(0)?;
    native.insert(&application, 0)?;
    native.insert(
        &Submenu::with_items(app, "Vitals 服务", true, &services.items())?,
        1,
    )?;
    app.set_menu(native)?;
    Ok(services)
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

    let tray_menu = TrayMenu::new(app.handle())?;
    let actions = Menu::with_items(app, &tray_menu.items())?;
    #[cfg(target_os = "macos")]
    let menus = [tray_menu, install_macos_menu(app.handle())?];
    #[cfg(not(target_os = "macos"))]
    let menus = [tray_menu];
    let autostart_items = menus.each_ref().map(|menu| menu.autostart.clone());
    let (send, receive) = mpsc::channel();
    app.manage(send.clone());
    let tray = TrayIconBuilder::with_id("vitals");
    #[cfg(target_os = "macos")]
    let tray = tray
        .icon(tauri::image::Image::from_bytes(include_bytes!(
            "../icons/tray-template.png"
        ))?)
        .icon_as_template(true)
        .show_menu_on_left_click(true);
    #[cfg(not(target_os = "macos"))]
    let tray = tray
        .icon(
            app.default_window_icon()
                .expect("bundled Vitals icon")
                .clone(),
        )
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
        });
    tray.tooltip("Vitals")
        .menu(&actions)
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
                    let enabled = manager.is_enabled().unwrap_or(false);
                    for item in &autostart_items {
                        let _ = item.set_checked(enabled);
                    }
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
            for menu in &menus {
                menu.update(state);
            }
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
        #[cfg(target_os = "macos")]
        if let RunEvent::Reopen {
            has_visible_windows: false,
            ..
        } = &event
        {
            open_window(app);
        }
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
