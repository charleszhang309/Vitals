use serde::{Deserialize, Serialize};
use std::fs::{self, File};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use tauri::Url;
use uuid::Uuid;

const SETTINGS_FILE: &str = "desktop-settings.json";
const INVALID_URL: &str = "主机地址必须是 HTTPS 服务地址，例如 https://vitals.example.com。";
const INVALID_TOKEN: &str = "请输入主机的配对令牌。令牌不能包含空白或非 ASCII 字符。";

#[derive(Clone, Copy, Default, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Selection {
    #[default]
    Hub,
    Remote,
}

#[derive(Clone, PartialEq)]
pub enum Role {
    Hub,
    Remote(Remote),
    Unconfigured,
}

#[derive(Clone, Deserialize, PartialEq, Serialize)]
pub struct Remote {
    pub url: String,
    pub token: String,
    pub device_id: String,
}

impl Remote {
    pub fn address(&self) -> Url {
        self.url.parse().expect("validated primary URL")
    }
}

#[derive(Default, Deserialize, Serialize)]
struct Settings {
    role: Selection,
    remote: Option<Remote>,
}

impl Settings {
    fn role(&self) -> Role {
        match (&self.role, &self.remote) {
            (Selection::Hub, _) => Role::Hub,
            (Selection::Remote, Some(remote)) => Role::Remote(remote.clone()),
            (Selection::Remote, None) => Role::Unconfigured,
        }
    }
}

#[derive(Serialize)]
pub struct SettingsView {
    role: Option<Selection>,
    url: String,
    has_token: bool,
}

#[derive(Serialize)]
pub struct DesktopModeView {
    mode: Selection,
    remote_url: String,
    has_token: bool,
}

pub struct SettingsStore {
    path: PathBuf,
    settings: Mutex<Option<Settings>>,
}

impl SettingsStore {
    pub fn load(home: &Path) -> (Self, Role) {
        let path = home.join(SETTINGS_FILE);
        let settings = match File::open(&path) {
            Ok(file) => match read_settings(file) {
                Ok(settings) => Some(settings),
                Err(error) => {
                    eprintln!("Could not read Vitals desktop settings: {error}");
                    None
                }
            },
            Err(error) if error.kind() == io::ErrorKind::NotFound => Some(Settings::default()),
            Err(error) => {
                eprintln!("Could not open Vitals desktop settings: {error}");
                None
            }
        };
        let role = settings.as_ref().map_or(Role::Unconfigured, Settings::role);
        (
            Self {
                path,
                settings: Mutex::new(settings),
            },
            role,
        )
    }

    pub fn view(&self) -> SettingsView {
        let settings = self.settings.lock().unwrap();
        let remote = settings
            .as_ref()
            .and_then(|settings| settings.remote.as_ref());
        SettingsView {
            role: settings.as_ref().map(|settings| settings.role),
            url: remote.map_or_else(String::new, |remote| remote.url.clone()),
            has_token: remote.is_some(),
        }
    }

    pub fn mode_view(&self) -> DesktopModeView {
        let view = self.view();
        DesktopModeView {
            mode: view.role.unwrap_or_default(),
            remote_url: view.url,
            has_token: view.has_token,
        }
    }

    pub fn apply(
        &self,
        role: Selection,
        url: &str,
        token: &str,
        activate: impl FnOnce(Role) -> Result<(), &'static str>,
    ) -> Result<(), &'static str> {
        let mut current = self.settings.lock().unwrap();
        let previous = current
            .as_ref()
            .and_then(|settings| settings.remote.as_ref());
        let remote = match role {
            Selection::Hub => previous.cloned(),
            Selection::Remote => {
                let url = primary_url(url)?;
                let token = if token.is_empty() {
                    previous
                        .filter(|remote| remote.url == url)
                        .map(|remote| remote.token.clone())
                        .ok_or(INVALID_TOKEN)?
                } else if valid_token(token) {
                    token.to_owned()
                } else {
                    return Err(INVALID_TOKEN);
                };
                let device_id = previous.map_or_else(
                    || format!("mac-{}", Uuid::new_v4().simple()),
                    |remote| remote.device_id.clone(),
                );
                Some(Remote {
                    url,
                    token,
                    device_id,
                })
            }
        };
        let settings = Settings { role, remote };
        save_settings(&self.path, &settings).map_err(|error| {
            eprintln!("Could not save Vitals desktop settings: {error}");
            "无法保存 Vitals 桌面设置。"
        })?;
        let active = settings.role();
        *current = Some(settings);
        activate(active)
    }
}

fn primary_url(value: &str) -> Result<String, &'static str> {
    let value = value.trim();
    if value.len() > 2048
        || value.contains(['@', '\\', '?', '#'])
        || value.chars().any(|c| c.is_whitespace() || c.is_control())
    {
        return Err(INVALID_URL);
    }
    let (scheme, authority) = value.split_once("://").ok_or(INVALID_URL)?;
    let authority = authority.strip_suffix('/').unwrap_or(authority);
    if !scheme.eq_ignore_ascii_case("https") || authority.is_empty() || authority.contains('/') {
        return Err(INVALID_URL);
    }
    let url = Url::parse(value).map_err(|_| INVALID_URL)?;
    if !url.host_str().is_some_and(|host| {
        host.bytes()
            .all(|ch| ch.is_ascii_alphanumeric() || b".-_:[]".contains(&ch))
    }) || url.port() == Some(0)
    {
        return Err(INVALID_URL);
    }
    Ok(url.to_string())
}

fn valid_token(token: &str) -> bool {
    !token.is_empty()
        && token.len() <= 4096
        && token.bytes().all(|byte| (b'!'..=b'~').contains(&byte))
        && token != "replace-with-windows-agents-token"
}

fn read_settings(file: File) -> Result<Settings, &'static str> {
    let mut settings: Settings = serde_json::from_reader(file).map_err(|_| "invalid JSON")?;
    if let Some(remote) = &mut settings.remote {
        remote.url = primary_url(&remote.url)?;
        if !valid_token(&remote.token)
            || !remote
                .device_id
                .strip_prefix("mac-")
                .is_some_and(|id| Uuid::parse_str(id).is_ok())
        {
            return Err("invalid pairing settings");
        }
    }
    Ok(settings)
}

fn save_settings(path: &Path, settings: &Settings) -> io::Result<()> {
    let directory = path.parent().expect("settings directory");
    fs::create_dir_all(directory)?;
    // NamedTempFile is created owner-only on Unix and inherits the user's profile ACL on Windows.
    let mut pending = tempfile::NamedTempFile::new_in(directory)?;
    serde_json::to_writer(&mut pending, settings)?;
    pending.write_all(b"\n")?;
    pending.as_file().sync_all()?;
    pending.persist(path)?;
    Ok(())
}
