#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod service;
mod settings;

use service::{desktop_home, Hub, Operation, Status, Supervisor};
use settings::{DesktopModeView, Role, Selection, SettingsStore, SettingsView};
use std::collections::HashSet;
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Mutex};
use tauri::ipc::{CapabilityBuilder, CommandScope};
#[cfg(target_os = "macos")]
use tauri::menu::Submenu;
use tauri::menu::{CheckMenuItem, IsMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
#[cfg(not(target_os = "macos"))]
use tauri::tray::{MouseButton, MouseButtonState, TrayIconEvent};
use tauri::{
    AppHandle, Manager, RunEvent, Runtime, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
    WindowEvent,
};
use tauri_plugin_autostart::ManagerExt;

const SERVICE_UNAVAILABLE: &str = "无法连接 Vitals 服务管理器。";
const SETTINGS_WINDOW: &str = "desktop-settings";
type DashboardOrigin = Arc<Mutex<Option<Url>>>;

fn bundled_url(path: &str) -> Url {
    let origin = if cfg!(windows) {
        "http://tauri.localhost/"
    } else {
        "tauri://localhost/"
    };
    let mut url = Url::parse(origin).expect("bundled origin");
    url.set_path(path);
    url
}

fn is_bundled_page(url: &Url, entry: &str) -> bool {
    let bundled = bundled_url(entry);
    url.scheme() == bundled.scheme()
        && url.host_str() == bundled.host_str()
        && url.port() == bundled.port()
        && url.username().is_empty()
        && url.password().is_none()
        && (url.path() == entry || (entry == "/index.html" && url.path() == "/"))
}

fn require_bundled_page(
    window: &WebviewWindow,
    label: &str,
    entry: &str,
) -> Result<(), &'static str> {
    if window.label() == label && window.url().is_ok_and(|url| is_bundled_page(&url, entry)) {
        Ok(())
    } else {
        Err("请在 Vitals 桌面应用中打开此设置。")
    }
}

#[tauri::command]
fn start_service(
    window: WebviewWindow,
    operations: tauri::State<'_, Sender<Operation>>,
) -> Result<(), &'static str> {
    require_bundled_page(&window, "main", "/index.html")?;
    operations
        .send(Operation::Start)
        .map_err(|_| SERVICE_UNAVAILABLE)
}

#[tauri::command]
fn open_desktop_settings(window: WebviewWindow) -> Result<(), &'static str> {
    require_bundled_page(&window, "main", "/index.html")?;
    open_settings(window.app_handle());
    Ok(())
}

#[tauri::command]
fn read_desktop_settings(
    window: WebviewWindow,
    store: tauri::State<'_, SettingsStore>,
) -> Result<SettingsView, &'static str> {
    require_bundled_page(&window, SETTINGS_WINDOW, "/settings.html")?;
    Ok(store.view())
}

#[tauri::command]
fn save_desktop_settings(
    window: WebviewWindow,
    store: tauri::State<'_, SettingsStore>,
    operations: tauri::State<'_, Sender<Operation>>,
    role: Selection,
    url: String,
    token: String,
) -> Result<(), &'static str> {
    require_bundled_page(&window, SETTINGS_WINDOW, "/settings.html")?;
    store.apply(role, &url, &token, |role| {
        operations
            .send(Operation::Configure(role))
            .map_err(|_| SERVICE_UNAVAILABLE)
    })?;
    let _ = window.hide();
    open_window(window.app_handle());
    Ok(())
}

fn permits_dashboard_page(
    label: &str,
    page: &Url,
    active: &Mutex<Option<Url>>,
    grants: &[Arc<String>],
) -> bool {
    let origin = page.origin().ascii_serialization();
    label == "main"
        && matches!(page.path(), "/" | "/index.html")
        && is_allowed_origin(active, page)
        && grants.iter().any(|grant| grant.as_str() == origin)
}

fn require_dashboard(
    window: &WebviewWindow,
    active: &DashboardOrigin,
    scope: &CommandScope<String>,
) -> Result<(), &'static str> {
    if window
        .url()
        .is_ok_and(|page| permits_dashboard_page(window.label(), &page, active, scope.allows()))
    {
        Ok(())
    } else {
        Err("请在 Vitals 桌面主窗口中打开此设置。")
    }
}

#[tauri::command]
fn get_desktop_mode(
    window: WebviewWindow,
    active: tauri::State<'_, DashboardOrigin>,
    scope: CommandScope<String>,
    store: tauri::State<'_, SettingsStore>,
) -> Result<DesktopModeView, &'static str> {
    require_dashboard(&window, &active, &scope)?;
    Ok(store.mode_view())
}

#[tauri::command(rename_all = "snake_case")]
fn set_desktop_mode(
    window: WebviewWindow,
    active: tauri::State<'_, DashboardOrigin>,
    scope: CommandScope<String>,
    store: tauri::State<'_, SettingsStore>,
    operations: tauri::State<'_, Sender<Operation>>,
    mode: Selection,
    remote_url: String,
    token: String,
) -> Result<DesktopModeView, &'static str> {
    require_dashboard(&window, &active, &scope)?;
    store.apply(mode, &remote_url, &token, |role| {
        operations
            .send(Operation::Configure(role))
            .map_err(|_| SERVICE_UNAVAILABLE)
    })?;
    Ok(store.mode_view())
}

fn show_window(app: &AppHandle, label: &str) {
    if let Some(window) = app.get_webview_window(label) {
        #[cfg(target_os = "macos")]
        if let Err(error) = app.set_activation_policy(tauri::ActivationPolicy::Regular) {
            eprintln!("Could not restore Vitals Dock icon: {error}");
        }
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn open_window(app: &AppHandle) {
    show_window(app, "main");
}

fn open_settings(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(SETTINGS_WINDOW) {
        if !window.is_visible().unwrap_or(false) {
            let _ = window.reload();
        }
        show_window(app, SETTINGS_WINDOW);
    }
}

fn reopen_and_start(app: &AppHandle) {
    open_window(app);
    if let Some(operations) = app.try_state::<Sender<Operation>>() {
        let _ = operations.send(Operation::Start);
    }
}

struct TrayMenu<R: Runtime> {
    open: MenuItem<R>,
    settings: MenuItem<R>,
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
            settings: MenuItem::with_id(
                app,
                "desktop-settings",
                "配置桌面模式…",
                true,
                None::<&str>,
            )?,
            status: MenuItem::with_id(app, "status", "正在启动 Vitals", false, None::<&str>)?,
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

    fn items(&self) -> [&dyn IsMenuItem<R>; 9] {
        [
            &self.open,
            &self.settings,
            &self.status,
            &self.separator,
            &self.start,
            &self.stop,
            &self.restart,
            &self.autostart,
            &self.quit,
        ]
    }

    fn update(&self, role: &Role, state: Status) {
        let _ = self.status.set_text(state.label(role));
        let _ = self.start.set_enabled(matches!(
            state,
            Status::Stopped | Status::Failed | Status::Occupied
        ));
        let _ = self.stop.set_enabled(
            matches!(state, Status::Starting | Status::Running)
                || (state == Status::Failed && matches!(role, Role::Remote(_))),
        );
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

fn hide_on_close(window: &WebviewWindow) {
    let window = window.clone();
    let event_window = window.clone();
    window.on_window_event(move |event| {
        if let WindowEvent::CloseRequested { api, .. } = event {
            api.prevent_close();
            if let Err(error) = event_window.hide() {
                eprintln!("Could not hide Vitals window: {error}");
                return;
            }
            #[cfg(target_os = "macos")]
            {
                let app = event_window.app_handle();
                let any_visible = app
                    .webview_windows()
                    .values()
                    .any(|window| window.is_visible().unwrap_or(false));
                if !any_visible {
                    if let Err(error) =
                        app.set_activation_policy(tauri::ActivationPolicy::Accessory)
                    {
                        eprintln!("Could not hide Vitals Dock icon: {error}");
                    }
                }
            }
        }
    });
}

fn known_external_link(url: &Url) -> bool {
    if url.scheme() != "https"
        || url.port().is_some()
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return false;
    }
    match url.host_str() {
        Some("github.com") => {
            let path = url.path().to_ascii_lowercase();
            path == "/charleszhang309/vitals"
                || path == "/charleszhang309/vitals/releases"
                || path.starts_with("/charleszhang309/vitals/releases/")
        }
        Some("frankfurter.dev" | "open-meteo.com" | "atmosphere.copernicus.eu") => true,
        _ => false,
    }
}

fn navigation_target(role: &Role, status: Status, hub_url: &Url) -> (Url, Option<Url>) {
    let target = match (role, status) {
        (Role::Hub, Status::Running | Status::External) => Some(hub_url.clone()),
        (Role::Remote(remote), Status::Running | Status::Failed) => Some(remote.address()),
        _ => None,
    };
    if let Some(url) = target {
        return (url.clone(), Some(url));
    }
    let mut url = bundled_url("/");
    let mode = match role {
        Role::Hub => "hub",
        Role::Remote(_) => "remote",
        Role::Unconfigured => "unconfigured",
    };
    let status = match status {
        Status::Starting => "starting",
        Status::Stopped => "stopped",
        Status::Occupied => "occupied",
        Status::Unconfigured => "unconfigured",
        _ => "failed",
    };
    url.query_pairs_mut()
        .append_pair("mode", mode)
        .append_pair("status", status);
    (url, None)
}

fn is_allowed_origin(allowed: &Mutex<Option<Url>>, url: &Url) -> bool {
    matches!(url.scheme(), "http" | "https")
        && allowed
            .lock()
            .unwrap()
            .as_ref()
            .is_some_and(|active| active.origin() == url.origin())
}

fn allow_dashboard_ipc(app: &AppHandle, origin: &Url, registered: &mut HashSet<String>) {
    let origin = origin.origin().ascii_serialization();
    if registered.contains(&origin) {
        return;
    }
    let pattern = format!("{origin}/*");
    if pattern
        .parse::<tauri::utils::acl::RemoteUrlPattern>()
        .is_err()
    {
        eprintln!("Could not authorize the Vitals dashboard origin.");
        return;
    }
    let capability = CapabilityBuilder::new(format!("dashboard-{}", registered.len()))
        .local(false)
        .remote(pattern)
        .window("main")
        .permission_scoped(
            "allow-dashboard-mode",
            vec![origin.clone()],
            Vec::<String>::new(),
        );
    if let Err(error) = app.add_capability(capability) {
        eprintln!("Could not add Vitals dashboard permissions: {error}");
    } else {
        registered.insert(origin);
    }
}

fn build_app(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    #[cfg(target_os = "macos")]
    app.set_activation_policy(tauri::ActivationPolicy::Accessory);

    let hub = Hub::new(app.handle())?;
    let hub_url: Url = hub.url().parse()?;
    let home = desktop_home(app.path().app_local_data_dir()?)?;
    let (store, role) = SettingsStore::load(&home);
    app.manage(store);
    let allowed_origin = Arc::new(Mutex::new(None));
    app.manage(allowed_origin.clone());
    let navigation_origin = allowed_origin.clone();
    let popup_origin = allowed_origin.clone();
    let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("Vitals")
        .inner_size(1280.0, 820.0)
        .min_inner_size(900.0, 640.0)
        .visible(false)
        .on_navigation(move |url| {
            is_bundled_page(url, "/index.html") || is_allowed_origin(&navigation_origin, url)
        })
        .on_new_window(move |url, _| {
            let same_origin = is_allowed_origin(&popup_origin, &url);
            if same_origin || known_external_link(&url) {
                if let Err(error) = open::that_detached(url.as_str()) {
                    eprintln!("Could not open Vitals link: {error}");
                }
            }
            tauri::webview::NewWindowResponse::Deny
        })
        .build()?;
    hide_on_close(&window);
    let settings_window = WebviewWindowBuilder::new(
        app,
        SETTINGS_WINDOW,
        WebviewUrl::App("settings.html".into()),
    )
    .title("Vitals 桌面设置")
    .inner_size(540.0, 495.0)
    .min_inner_size(410.0, 430.0)
    .visible(false)
    .on_navigation(|url| is_bundled_page(url, "/settings.html"))
    .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
    .build()?;
    hide_on_close(&settings_window);

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
                "desktop-settings" => {
                    open_settings(app);
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
    if !background {
        open_window(&app);
    }
    std::thread::spawn(move || {
        let mut previous = None;
        let mut registered = HashSet::new();
        Supervisor::new(hub, role, home).run(receive, |role, state| {
            for menu in &menus {
                menu.update(role, state);
            }
            let (target, origin) = navigation_target(role, state, &hub_url);
            if let Some(origin) = &origin {
                allow_dashboard_ipc(&app, origin, &mut registered);
            }
            *allowed_origin.lock().unwrap() = origin;
            if previous.as_ref() != Some(&target) {
                let _ = window.navigate(target.clone());
                previous = Some(target);
            }
        });
        app.exit(0);
    });
    Ok(())
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            reopen_and_start(app)
        }))
        .plugin(
            tauri_plugin_autostart::Builder::new()
                .arg("--background")
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            start_service,
            open_desktop_settings,
            read_desktop_settings,
            save_desktop_settings,
            get_desktop_mode,
            set_desktop_mode
        ])
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
            reopen_and_start(app);
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn desktop_mode_only_accepts_the_active_dashboard_and_its_grant() {
        let hub = Url::parse("http://127.0.0.1:3993/").unwrap();
        let remote = Url::parse("https://vitals.example.test:8443/").unwrap();
        let hub_grants = [Arc::new(hub.origin().ascii_serialization())];
        let remote_grants = [Arc::new(remote.origin().ascii_serialization())];
        let active = Mutex::new(Some(hub.clone()));

        assert!(permits_dashboard_page("main", &hub, &active, &hub_grants));
        assert!(!permits_dashboard_page("main", &hub, &active, &[]));
        assert!(!permits_dashboard_page(
            SETTINGS_WINDOW,
            &hub,
            &active,
            &hub_grants
        ));
        let file = hub.join("api/scratchpad/files/1").unwrap();
        assert!(!permits_dashboard_page("main", &file, &active, &hub_grants));

        *active.lock().unwrap() = Some(remote.clone());
        assert!(permits_dashboard_page(
            "main",
            &remote,
            &active,
            &remote_grants
        ));
        assert!(!permits_dashboard_page(
            "main",
            &remote,
            &active,
            &hub_grants
        ));
        assert!(!permits_dashboard_page("main", &hub, &active, &hub_grants));
    }

    #[test]
    fn remote_navigation_excludes_the_hub_and_other_origins() {
        let remote = Role::Remote(settings::Remote {
            url: "https://vitals.example.test/".into(),
            token: "fixture-token".into(),
            device_id: "fixture-device".into(),
        });
        let hub = Url::parse("http://127.0.0.1:3993/").unwrap();
        let (target, allowed) = navigation_target(&remote, Status::Running, &hub);
        assert_eq!(target.as_str(), "https://vitals.example.test/");
        assert!(!is_bundled_page(&target, "/index.html"));
        assert!(!is_bundled_page(&target, "/settings.html"));
        let allowed = Mutex::new(allowed);
        assert!(is_allowed_origin(&allowed, &target.join("today").unwrap()));
        assert!(!is_allowed_origin(&allowed, &hub));
        for other in [
            "https://vitals.example.test:4430/",
            "http://vitals.example.test/",
            "https://vitals.example.test.other/",
            "blob:https://vitals.example.test/test",
        ] {
            assert!(!is_allowed_origin(&allowed, &Url::parse(other).unwrap()));
        }
        let (waiting, allowed) = navigation_target(&remote, Status::Stopped, &hub);
        assert!(is_bundled_page(&waiting, "/index.html"));
        assert!(allowed.is_none());
    }
}
