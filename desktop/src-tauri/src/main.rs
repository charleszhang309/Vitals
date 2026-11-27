#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod autostart;
#[cfg(any(test, target_os = "macos"))]
mod dock;
mod language;
mod service;
mod settings;

use language::{Language, LanguagePreference, LanguageStore};
use service::{desktop_home, Hub, Operation, Status, Supervisor};
use settings::{DesktopModeView, Role, Selection, SettingsStore, SettingsView};
use std::collections::HashSet;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Mutex};
use tauri::ipc::{CapabilityBuilder, CommandScope};
#[cfg(target_os = "macos")]
use tauri::menu::Submenu;
use tauri::menu::{CheckMenuItem, IsMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
#[cfg(not(target_os = "macos"))]
use tauri::tray::{MouseButton, MouseButtonState, TrayIconEvent};
#[cfg(target_os = "macos")]
use tauri::WindowEvent;
use tauri::{
    AppHandle, Manager, RunEvent, Runtime, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
};
const MAIN_WINDOW: &str = "main";
const SETTINGS_WINDOW: &str = "desktop-settings";
type DashboardOrigin = Arc<Mutex<Option<Url>>>;

#[derive(Default)]
struct ManagedHub(AtomicBool);

fn desktop_language(app: &AppHandle) -> Language {
    app.state::<LanguageStore>().language()
}

fn service_unavailable(app: &AppHandle) -> &'static str {
    desktop_language(app).choose(
        "Could not reach the Vitals service manager.",
        "无法连接 Vitals 服务管理器。",
    )
}

fn settings_error(app: &AppHandle, error: &'static str) -> &'static str {
    desktop_language(app).choose(settings::english_error(error), error)
}

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
        Err(desktop_language(window.app_handle()).choose(
            "Open this setting in the Vitals desktop app.",
            "请在 Vitals 桌面应用中打开此设置。",
        ))
    }
}

#[tauri::command]
fn start_service(
    window: WebviewWindow,
    operations: tauri::State<'_, Sender<Operation>>,
) -> Result<(), &'static str> {
    require_bundled_page(&window, MAIN_WINDOW, "/index.html")?;
    operations
        .send(Operation::Start)
        .map_err(|_| service_unavailable(window.app_handle()))
}

#[tauri::command]
fn open_desktop_settings(window: WebviewWindow) -> Result<(), &'static str> {
    require_bundled_page(&window, MAIN_WINDOW, "/index.html")?;
    open_window(window.app_handle(), SETTINGS_WINDOW);
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
    store
        .apply(role, &url, &token, |role| {
            operations
                .send(Operation::Configure(role))
                .map_err(|_| service_unavailable(window.app_handle()))
        })
        .map_err(|error| settings_error(window.app_handle(), error))?;
    let _ = window.close();
    open_window(window.app_handle(), MAIN_WINDOW);
    Ok(())
}

fn permits_dashboard_page(
    label: &str,
    page: &Url,
    active: &Mutex<Option<Url>>,
    grants: &[Arc<String>],
) -> bool {
    let origin = page.origin().ascii_serialization();
    label == MAIN_WINDOW
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
        Err(desktop_language(window.app_handle()).choose(
            "Open this setting in the main Vitals desktop window.",
            "请在 Vitals 桌面主窗口中打开此设置。",
        ))
    }
}

fn permits_window_controls(
    label: &str,
    page: &Url,
    active: &Mutex<Option<Url>>,
    grants: &[Arc<String>],
) -> bool {
    (label == MAIN_WINDOW && is_bundled_page(page, "/index.html"))
        || permits_dashboard_page(label, page, active, grants)
}

#[derive(serde::Deserialize)]
#[serde(rename_all = "snake_case")]
enum WindowAction {
    Drag,
    ToggleMaximize,
    Minimize,
    Close,
    IsMaximized,
}

#[tauri::command]
fn desktop_window_action(
    window: WebviewWindow,
    active: tauri::State<'_, DashboardOrigin>,
    scope: CommandScope<String>,
    action: WindowAction,
) -> Result<Option<bool>, &'static str> {
    let language = desktop_language(window.app_handle());
    if !window
        .url()
        .is_ok_and(|page| permits_window_controls(window.label(), &page, &active, scope.allows()))
    {
        return Err(language.choose(
            "Open the main Vitals desktop window to use these controls.",
            "请在 Vitals 桌面主窗口中使用这些控件。",
        ));
    }

    let result = match action {
        WindowAction::Drag => window.start_dragging().map(|()| None),
        WindowAction::ToggleMaximize => window.is_maximized().and_then(|maximized| {
            if maximized {
                window.unmaximize()?;
            } else {
                window.maximize()?;
            }
            Ok(Some(!maximized))
        }),
        WindowAction::Minimize => window.minimize().map(|()| None),
        WindowAction::Close => window.close().map(|()| None),
        WindowAction::IsMaximized => window.is_maximized().map(Some),
    };
    result.map_err(|error| {
        eprintln!("Could not control the Vitals window: {error}");
        language.choose(
            "Could not update the Vitals window.",
            "无法更新 Vitals 窗口。",
        )
    })
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
    store
        .apply(mode, &remote_url, &token, |role| {
            operations
                .send(Operation::Configure(role))
                .map_err(|_| service_unavailable(window.app_handle()))
        })
        .map_err(|error| settings_error(window.app_handle(), error))?;
    Ok(store.mode_view())
}

fn is_local_hub_page(page: &Url) -> bool {
    page.scheme() == "http" && page.host_str() == Some("127.0.0.1")
}

#[tauri::command]
fn restart_desktop_hub(
    window: WebviewWindow,
    active: tauri::State<'_, DashboardOrigin>,
    scope: CommandScope<String>,
    managed_hub: tauri::State<'_, ManagedHub>,
    operations: tauri::State<'_, Sender<Operation>>,
) -> Result<(), &'static str> {
    require_dashboard(&window, &active, &scope)?;
    let language = desktop_language(window.app_handle());
    if !window.url().is_ok_and(|page| is_local_hub_page(&page)) {
        return Err(language.choose(
            "Restart the service from the local hub.",
            "请在本机主中心重启服务。",
        ));
    }
    if !managed_hub.inner().0.load(Ordering::Acquire) {
        return Err(language.choose(
            "Restart Vitals the same way you started it.",
            "请使用当前的服务启动方式重启 Vitals。",
        ));
    }
    operations
        .send(Operation::Restart)
        .map_err(|_| service_unavailable(window.app_handle()))
}

fn launch_at_login_error(app: &AppHandle, error: std::io::Error) -> &'static str {
    eprintln!("Could not update Vitals launch at login: {error}");
    desktop_language(app).choose("Could not update Start at login.", "无法更新开机自启设置。")
}

fn apply_launch_at_login(app: &AppHandle, enabled: bool) -> Result<bool, &'static str> {
    let write = autostart::set_enabled(enabled);
    let actual = autostart::is_enabled();
    if let Ok(checked) = &actual {
        for menu in &app.state::<NativeMenus>().menus {
            let _ = menu.autostart.set_checked(*checked);
        }
    }
    write
        .and(actual)
        .map_err(|error| launch_at_login_error(app, error))
}

#[tauri::command]
fn get_launch_at_login(
    window: WebviewWindow,
    active: tauri::State<'_, DashboardOrigin>,
    scope: CommandScope<String>,
) -> Result<bool, &'static str> {
    require_dashboard(&window, &active, &scope)?;
    autostart::is_enabled().map_err(|error| {
        eprintln!("Could not read Vitals launch at login: {error}");
        desktop_language(window.app_handle())
            .choose("Could not read Start at login.", "无法读取开机自启设置。")
    })
}

#[tauri::command]
fn set_launch_at_login(
    window: WebviewWindow,
    active: tauri::State<'_, DashboardOrigin>,
    scope: CommandScope<String>,
    enabled: bool,
) -> Result<bool, &'static str> {
    require_dashboard(&window, &active, &scope)?;
    apply_launch_at_login(window.app_handle(), enabled)
}

#[cfg(target_os = "macos")]
#[tauri::command]
fn get_hide_dock_icon(
    window: WebviewWindow,
    active: tauri::State<'_, DashboardOrigin>,
    scope: CommandScope<String>,
    store: tauri::State<'_, dock::DockPreference>,
) -> Result<bool, &'static str> {
    require_dashboard(&window, &active, &scope)?;
    Ok(store.hidden())
}

#[cfg(target_os = "macos")]
#[tauri::command]
fn set_hide_dock_icon(
    window: WebviewWindow,
    active: tauri::State<'_, DashboardOrigin>,
    scope: CommandScope<String>,
    store: tauri::State<'_, dock::DockPreference>,
    enabled: bool,
) -> Result<bool, &'static str> {
    require_dashboard(&window, &active, &scope)?;
    let app = window.app_handle();
    let message = desktop_language(app).choose(
        "Could not update Hide Dock icon.",
        "无法更新隐藏程序坞图标设置。",
    );
    store.set_hidden(enabled).map_err(|error| {
        eprintln!("Could not save Vitals Dock preference: {error}");
        message
    })?;
    let dock_app = app.clone();
    app.run_on_main_thread(move || {
        let has_window = dock_app
            .webview_windows()
            .values()
            .any(|window| window.is_visible().unwrap_or(false));
        refresh_dock_policy(&dock_app, has_window);
    })
    .map_err(|error| {
        eprintln!("Could not update Vitals Dock policy: {error}");
        message
    })?;
    Ok(store.hidden())
}

#[tauri::command]
fn set_desktop_language(
    window: WebviewWindow,
    active: tauri::State<'_, DashboardOrigin>,
    scope: CommandScope<String>,
    store: tauri::State<'_, LanguageStore>,
    language: LanguagePreference,
) -> Result<(), &'static str> {
    require_dashboard(&window, &active, &scope)?;
    let error_message = store.language().choose(
        "Could not update the desktop language.",
        "无法更新桌面语言。",
    );
    store.set(language).map_err(|error| {
        eprintln!("Could not save Vitals desktop language: {error}");
        error_message
    })?;
    let app = window.app_handle().clone();
    window
        .app_handle()
        .run_on_main_thread(move || {
            let language = desktop_language(&app);
            app.state::<NativeMenus>().set_language(language);
            if let Some(window) = app.get_webview_window(SETTINGS_WINDOW) {
                let _ = window.set_title(settings_title(language));
            }
        })
        .map_err(|error| {
            eprintln!("Could not refresh Vitals desktop language: {error}");
            error_message
        })
}

fn open_window(app: &AppHandle, label: &'static str) {
    let app = app.clone();
    // WebView2 creation must run outside synchronous window and IPC callbacks.
    tauri::async_runtime::spawn_blocking(move || {
        let result = show_or_build_window(&app, label);
        if let Err(error) = result {
            eprintln!("Could not open Vitals window {label}: {error}");
        }
    });
}

fn show_or_build_window(app: &AppHandle, label: &str) -> tauri::Result<()> {
    let navigation = app.state::<Mutex<DashboardNavigation>>();
    let mut navigation = navigation.lock().unwrap();
    let window = match app.get_webview_window(label) {
        Some(window) => window,
        None if label == MAIN_WINDOW => navigation.build_window(app)?,
        None => build_settings_window(app)?,
    };
    #[cfg(target_os = "macos")]
    {
        let dock_app = app.clone();
        if let Err(error) = app.run_on_main_thread(move || refresh_dock_policy(&dock_app, true)) {
            eprintln!("Could not update Vitals Dock policy: {error}");
        }
    }
    window.show()?;
    window.unminimize()?;
    window.set_focus()
}

#[cfg(target_os = "macos")]
fn refresh_dock_policy(app: &AppHandle, has_window: bool) {
    let policy = if app.state::<dock::DockPreference>().shows_icon(has_window) {
        tauri::ActivationPolicy::Regular
    } else {
        tauri::ActivationPolicy::Accessory
    };
    if let Err(error) = app.set_activation_policy(policy) {
        eprintln!("Could not update Vitals Dock policy: {error}");
    }
}

fn reopen_and_start(app: &AppHandle) {
    if let Some(operations) = app.try_state::<Sender<Operation>>() {
        open_window(app, MAIN_WINDOW);
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
    fn new(app: &AppHandle<R>, language: Language, launches_at_login: bool) -> tauri::Result<Self> {
        Ok(Self {
            open: MenuItem::with_id(
                app,
                "open",
                language.choose("Open Vitals", "打开 Vitals"),
                true,
                None::<&str>,
            )?,
            settings: MenuItem::with_id(
                app,
                "desktop-settings",
                language.choose("Configure desktop mode…", "配置桌面模式…"),
                true,
                None::<&str>,
            )?,
            status: MenuItem::with_id(
                app,
                "status",
                language.choose("Starting Vitals", "正在启动 Vitals"),
                false,
                None::<&str>,
            )?,
            separator: PredefinedMenuItem::separator(app)?,
            start: MenuItem::with_id(
                app,
                "start",
                language.choose("Start service", "启动服务"),
                false,
                None::<&str>,
            )?,
            stop: MenuItem::with_id(
                app,
                "stop",
                language.choose("Stop service", "停止服务"),
                false,
                None::<&str>,
            )?,
            restart: MenuItem::with_id(
                app,
                "restart",
                language.choose("Restart service", "重启服务"),
                false,
                None::<&str>,
            )?,
            autostart: CheckMenuItem::with_id(
                app,
                "autostart",
                language.choose("Start at login", "开机自启"),
                cfg!(any(windows, target_os = "macos")),
                launches_at_login,
                None::<&str>,
            )?,
            quit: MenuItem::with_id(
                app,
                "quit",
                language.choose("Quit Vitals", "退出 Vitals"),
                true,
                None::<&str>,
            )?,
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

    fn set_language(&self, language: Language) {
        let _ = self
            .open
            .set_text(language.choose("Open Vitals", "打开 Vitals"));
        let _ = self
            .settings
            .set_text(language.choose("Configure desktop mode…", "配置桌面模式…"));
        let _ = self
            .start
            .set_text(language.choose("Start service", "启动服务"));
        let _ = self
            .stop
            .set_text(language.choose("Stop service", "停止服务"));
        let _ = self
            .restart
            .set_text(language.choose("Restart service", "重启服务"));
        let _ = self
            .autostart
            .set_text(language.choose("Start at login", "开机自启"));
        let _ = self
            .quit
            .set_text(language.choose("Quit Vitals", "退出 Vitals"));
    }

    fn update(&self, language: Language, role: &Role, state: Status) {
        let _ = self.status.set_text(language.status(role, state));
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

struct NativeMenus {
    menus: Vec<TrayMenu<tauri::Wry>>,
    status: Mutex<(Role, Status)>,
    #[cfg(target_os = "macos")]
    service_submenu: Submenu<tauri::Wry>,
    #[cfg(target_os = "macos")]
    application_quit: MenuItem<tauri::Wry>,
}

impl NativeMenus {
    fn set_language(&self, language: Language) {
        let (role, status) = self.status.lock().unwrap().clone();
        for menu in &self.menus {
            menu.set_language(language);
            let _ = menu.status.set_text(language.status(&role, status));
        }
        #[cfg(target_os = "macos")]
        {
            let _ = self
                .service_submenu
                .set_text(language.choose("Vitals Service", "Vitals 服务"));
            let _ = self
                .application_quit
                .set_text(language.choose("Quit Vitals", "退出 Vitals"));
        }
    }

    fn update(&self, language: Language, role: Role, status: Status) {
        for menu in &self.menus {
            menu.update(language, &role, status);
        }
        *self.status.lock().unwrap() = (role, status);
    }
}

#[cfg(target_os = "macos")]
fn install_macos_menu(
    app: &AppHandle,
    language: Language,
    launches_at_login: bool,
) -> tauri::Result<(
    TrayMenu<tauri::Wry>,
    Submenu<tauri::Wry>,
    MenuItem<tauri::Wry>,
)> {
    let services = TrayMenu::new(app, language, launches_at_login)?;
    let native = Menu::default(app)?;
    // AppKit's built-in Quit bypasses the asynchronous Tauri exit request.
    let quit = MenuItem::with_id(
        app,
        "quit",
        language.choose("Quit Vitals", "退出 Vitals"),
        true,
        Some("Cmd+Q"),
    )?;
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
    let submenu = Submenu::with_items(
        app,
        language.choose("Vitals Service", "Vitals 服务"),
        true,
        &services.items(),
    )?;
    native.insert(&submenu, 1)?;
    app.set_menu(native)?;
    Ok((services, submenu, quit))
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

#[derive(Debug, PartialEq, Eq)]
enum MainPage {
    Startup(Url),
    Dashboard(Url),
}

impl MainPage {
    fn url(&self) -> &Url {
        match self {
            Self::Startup(url) | Self::Dashboard(url) => url,
        }
    }

    fn origin(&self) -> Option<&Url> {
        match self {
            Self::Startup(_) => None,
            Self::Dashboard(url) => Some(url),
        }
    }

    fn initial_url(&self) -> WebviewUrl {
        match self {
            Self::Startup(url) => WebviewUrl::App(
                format!("{}?{}", url.path(), url.query().unwrap_or_default()).into(),
            ),
            Self::Dashboard(url) => WebviewUrl::External(url.clone()),
        }
    }
}

fn navigation_target(role: &Role, status: Status, hub_url: &Url, language: Language) -> MainPage {
    let target = match (role, status) {
        (Role::Hub, Status::Running | Status::External) => Some(hub_url.clone()),
        (Role::Remote(remote), Status::Running | Status::Failed) => Some(remote.address()),
        _ => None,
    };
    if let Some(url) = target {
        return MainPage::Dashboard(url);
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
        .append_pair("status", status)
        .append_pair("language", language.tag());
    MainPage::Startup(url)
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
        .window(MAIN_WINDOW)
        .permission_scoped(
            "allow-dashboard-mode",
            vec![origin.clone()],
            Vec::<String>::new(),
        )
        .permission_scoped(
            "allow-desktop-preferences",
            vec![origin.clone()],
            Vec::<String>::new(),
        )
        .permission_scoped(
            "allow-desktop-window-action",
            vec![origin.clone()],
            Vec::<String>::new(),
        );
    if let Err(error) = app.add_capability(capability) {
        eprintln!("Could not add Vitals dashboard permissions: {error}");
    } else {
        registered.insert(origin);
    }
}

struct DashboardNavigation {
    current: MainPage,
    registered_origins: HashSet<String>,
}

impl DashboardNavigation {
    fn authorize(&mut self, app: &AppHandle) {
        let origin = self.current.origin();
        if let Some(origin) = origin {
            allow_dashboard_ipc(app, origin, &mut self.registered_origins);
        }
        *app.state::<DashboardOrigin>().lock().unwrap() = origin.cloned();
    }

    fn build_window(&mut self, app: &AppHandle) -> tauri::Result<WebviewWindow> {
        self.authorize(app);
        build_main_window(app, self.current.initial_url())
    }

    fn report(&mut self, app: &AppHandle, target: MainPage) {
        if self.current == target {
            return;
        }
        self.current = target;
        self.authorize(app);
        if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
            if let Err(error) = window.navigate(self.current.url().clone()) {
                eprintln!("Could not navigate to the Vitals page: {error}");
            }
        }
    }
}

fn build_main_window(app: &AppHandle, url: WebviewUrl) -> tauri::Result<WebviewWindow> {
    let navigation_origin = app.state::<DashboardOrigin>().inner().clone();
    let popup_origin = navigation_origin.clone();
    let builder = WebviewWindowBuilder::new(app, MAIN_WINDOW, url)
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
        });
    #[cfg(windows)]
    let builder = builder
        .decorations(false)
        .shadow(true)
        .resizable(true)
        .initialization_script("window.__VITALS_DESKTOP_PLATFORM__ = 'windows';");
    #[cfg(target_os = "macos")]
    let builder = builder
        .title_bar_style(tauri::TitleBarStyle::Overlay)
        .hidden_title(true)
        .traffic_light_position(tauri::LogicalPosition::new(18.0, 20.0))
        .initialization_script("window.__VITALS_DESKTOP_PLATFORM__ = 'macos';");
    builder.build()
}

fn settings_title(language: Language) -> &'static str {
    language.choose("Vitals Desktop Settings", "Vitals 桌面设置")
}

fn build_settings_window(app: &AppHandle) -> tauri::Result<WebviewWindow> {
    let language = desktop_language(app);
    WebviewWindowBuilder::new(
        app,
        SETTINGS_WINDOW,
        WebviewUrl::App(format!("settings.html?language={}", language.tag()).into()),
    )
    .title(settings_title(language))
    .inner_size(540.0, 495.0)
    .min_inner_size(410.0, 430.0)
    .visible(false)
    .on_navigation(|url| is_bundled_page(url, "/settings.html"))
    .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
    .build()
}

fn build_app(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    #[cfg(target_os = "macos")]
    app.set_activation_policy(tauri::ActivationPolicy::Accessory);

    let hub = Hub::new(app.handle())?;
    let hub_url: Url = hub.url().parse()?;
    let home = desktop_home(app.path().app_local_data_dir()?)?;
    let (store, role) = SettingsStore::load(&home);
    app.manage(store);
    app.manage(LanguageStore::load(&home));
    #[cfg(target_os = "macos")]
    app.manage(dock::DockPreference::load(&home));
    let language = desktop_language(app.handle());
    app.manage::<DashboardOrigin>(Arc::new(Mutex::new(None)));
    app.manage(ManagedHub::default());
    let initial_status = if role == Role::Unconfigured {
        Status::Unconfigured
    } else {
        Status::Starting
    };
    app.manage(Mutex::new(DashboardNavigation {
        current: navigation_target(&role, initial_status, &hub_url, language),
        registered_origins: HashSet::new(),
    }));

    let launches_at_login = autostart::is_enabled().unwrap_or_else(|error| {
        eprintln!("Could not read Vitals launch at login: {error}");
        false
    });
    let tray_menu = TrayMenu::new(app.handle(), language, launches_at_login)?;
    let actions = Menu::with_items(app, &tray_menu.items())?;
    #[cfg(target_os = "macos")]
    let (services, service_submenu, application_quit) =
        install_macos_menu(app.handle(), language, launches_at_login)?;
    #[cfg(target_os = "macos")]
    let menus = vec![tray_menu, services];
    #[cfg(not(target_os = "macos"))]
    let menus = vec![tray_menu];
    let native_menus = NativeMenus {
        menus,
        status: Mutex::new((role.clone(), initial_status)),
        #[cfg(target_os = "macos")]
        service_submenu,
        #[cfg(target_os = "macos")]
        application_quit,
    };
    native_menus.update(language, role.clone(), initial_status);
    app.manage(native_menus);
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
                open_window(tray.app_handle(), MAIN_WINDOW);
            }
        });
    tray.tooltip("Vitals")
        .menu(&actions)
        .on_menu_event(move |app, event| {
            let operation = match event.id().as_ref() {
                "open" => {
                    open_window(app, MAIN_WINDOW);
                    return;
                }
                "desktop-settings" => {
                    open_window(app, SETTINGS_WINDOW);
                    return;
                }
                "start" => Operation::Start,
                "stop" => Operation::Stop,
                "restart" => Operation::Restart,
                "quit" => Operation::Quit,
                "autostart" => {
                    match autostart::is_enabled() {
                        Ok(enabled) => {
                            let _ = apply_launch_at_login(app, !enabled);
                        }
                        Err(error) => eprintln!("Could not read Vitals launch at login: {error}"),
                    }
                    return;
                }
                _ => return,
            };
            let _ = send.send(operation);
        })
        .build(app)?;

    let app = app.handle().clone();
    let background = std::env::args_os().any(|arg| arg == autostart::BACKGROUND_ARG);
    if !background {
        open_window(&app, MAIN_WINDOW);
    }
    std::thread::spawn(move || {
        let navigation = app.state::<Mutex<DashboardNavigation>>();
        let managed_hub = app.state::<ManagedHub>();
        Supervisor::new(hub, role, home).run(receive, |role, state| {
            managed_hub.inner().0.store(
                matches!(role, Role::Hub) && state == Status::Running,
                Ordering::Release,
            );
            let menu_app = app.clone();
            let menu_role = role.clone();
            let _ = app.run_on_main_thread(move || {
                let language = desktop_language(&menu_app);
                menu_app
                    .state::<NativeMenus>()
                    .update(language, menu_role, state);
            });
            navigation.lock().unwrap().report(
                &app,
                navigation_target(role, state, &hub_url, desktop_language(&app)),
            );
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
        .invoke_handler(tauri::generate_handler![
            start_service,
            open_desktop_settings,
            read_desktop_settings,
            save_desktop_settings,
            desktop_window_action,
            get_desktop_mode,
            set_desktop_mode,
            restart_desktop_hub,
            get_launch_at_login,
            set_launch_at_login,
            #[cfg(target_os = "macos")]
            get_hide_dock_icon,
            #[cfg(target_os = "macos")]
            set_hide_dock_icon,
            set_desktop_language
        ])
        .setup(build_app)
        .build(tauri::generate_context!())
        .expect("could not initialize Vitals");
    app.run(|app, event| {
        #[cfg(target_os = "macos")]
        match &event {
            RunEvent::Reopen {
                has_visible_windows: false,
                ..
            } => reopen_and_start(app),
            RunEvent::WindowEvent {
                event: WindowEvent::Destroyed,
                ..
            } if !app
                .webview_windows()
                .values()
                .any(|window| window.is_visible().unwrap_or(false)) =>
            {
                refresh_dock_policy(app, false);
            }
            _ => {}
        }
        #[cfg(not(target_os = "macos"))]
        let _ = app;
        if let RunEvent::ExitRequested {
            api, code: None, ..
        } = event
        {
            // Tauri requests an uncoded exit when the last window is destroyed.
            // Quit from the native menus still stops the services before app.exit(0).
            api.prevent_exit();
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wizard_restart_only_accepts_the_local_hub_address() {
        assert!(is_local_hub_page(
            &Url::parse("http://127.0.0.1:3993/").unwrap()
        ));
        for remote in [
            "https://vitals.example.test/",
            "https://127.0.0.1:3993/",
            "http://127.0.0.2/",
        ] {
            assert!(!is_local_hub_page(&Url::parse(remote).unwrap()));
        }
    }

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
    fn desktop_chrome_only_accepts_the_startup_page_or_granted_dashboard() {
        let startup = bundled_url("/index.html");
        let settings = bundled_url("/settings.html");
        let dashboard = Url::parse("https://vitals.example.test:8443/").unwrap();
        let grants = [Arc::new(dashboard.origin().ascii_serialization())];
        let active = Mutex::new(Some(dashboard.clone()));

        assert!(permits_window_controls(MAIN_WINDOW, &startup, &active, &[]));
        assert!(!permits_window_controls(
            MAIN_WINDOW,
            &settings,
            &active,
            &[]
        ));
        assert!(!permits_window_controls(
            SETTINGS_WINDOW,
            &startup,
            &active,
            &[]
        ));
        assert!(permits_window_controls(
            MAIN_WINDOW,
            &dashboard,
            &active,
            &grants
        ));
        assert!(!permits_window_controls(
            MAIN_WINDOW,
            &dashboard,
            &active,
            &[]
        ));

        *active.lock().unwrap() = None;
        assert!(!permits_window_controls(
            MAIN_WINDOW,
            &dashboard,
            &active,
            &grants
        ));
    }

    #[test]
    fn remote_navigation_excludes_the_hub_and_other_origins() {
        let remote = Role::Remote(settings::Remote {
            url: "https://vitals.example.test/".into(),
            token: "fixture-token".into(),
            device_id: "fixture-device".into(),
        });
        let hub = Url::parse("http://127.0.0.1:3993/").unwrap();
        let page = navigation_target(&remote, Status::Running, &hub, Language::English);
        let target = page.url();
        assert_eq!(target.as_str(), "https://vitals.example.test/");
        assert!(!is_bundled_page(target, "/index.html"));
        assert!(!is_bundled_page(target, "/settings.html"));
        let allowed = Mutex::new(page.origin().cloned());
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
        let waiting = navigation_target(&remote, Status::Stopped, &hub, Language::English);
        assert!(is_bundled_page(waiting.url(), "/index.html"));
        assert!(waiting.origin().is_none());
    }

    #[test]
    fn recreated_window_uses_the_reported_page_and_current_dashboard_origin() {
        let hub = Url::parse("http://127.0.0.1:3993/").unwrap();
        let remote = Role::Remote(settings::Remote {
            url: "https://vitals.example.test:8443/".into(),
            token: "fixture-token".into(),
            device_id: "fixture-device".into(),
        });
        let remote_url = Url::parse("https://vitals.example.test:8443/").unwrap();
        let grants = [
            Arc::new(hub.origin().ascii_serialization()),
            Arc::new(remote_url.origin().ascii_serialization()),
        ];

        for (role, status, expected, previous) in [
            (&Role::Hub, Status::External, &hub, &remote_url),
            (&remote, Status::Running, &remote_url, &hub),
            (&remote, Status::Failed, &remote_url, &hub),
        ] {
            let page = navigation_target(role, status, &hub, Language::English);
            let WebviewUrl::External(initial) = page.initial_url() else {
                panic!("running dashboard must open directly");
            };
            assert_eq!(&initial, expected);
            let active = Mutex::new(page.origin().cloned());
            assert!(permits_dashboard_page(
                MAIN_WINDOW,
                &initial,
                &active,
                &grants
            ));
            assert!(!permits_dashboard_page(
                MAIN_WINDOW,
                previous,
                &active,
                &grants
            ));
        }
    }

    #[test]
    fn recreated_startup_window_passes_the_reported_mode_and_status() {
        let hub = Url::parse("http://127.0.0.1:3993/").unwrap();
        let remote = Role::Remote(settings::Remote {
            url: "https://vitals.example.test/".into(),
            token: "fixture-token".into(),
            device_id: "fixture-device".into(),
        });
        for (role, status, mode, status_name) in [
            (&Role::Hub, Status::Starting, "hub", "starting"),
            (&remote, Status::Stopped, "remote", "stopped"),
            (
                &Role::Unconfigured,
                Status::Unconfigured,
                "unconfigured",
                "unconfigured",
            ),
        ] {
            let page = navigation_target(role, status, &hub, Language::Chinese);
            let WebviewUrl::App(initial) = page.initial_url() else {
                panic!("waiting service status must open the bundled page");
            };
            let url = bundled_url("/").join(initial.to_str().unwrap()).unwrap();
            assert_eq!(&url, page.url());
            assert!(is_bundled_page(&url, "/index.html"));
            assert_eq!(
                url.query(),
                Some(format!("mode={mode}&status={status_name}&language=zh-CN").as_str())
            );
            assert!(page.origin().is_none());
        }
    }
}
