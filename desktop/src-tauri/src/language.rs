use crate::service::Status;
use crate::settings::{save_json, Role};
use serde::{Deserialize, Serialize};
use std::fs::File;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

#[derive(Clone, Copy, Default, Deserialize, PartialEq, Serialize)]
pub enum LanguagePreference {
    #[default]
    #[serde(rename = "auto")]
    Auto,
    #[serde(rename = "en")]
    English,
    #[serde(rename = "zh-CN")]
    Chinese,
}

#[derive(Clone, Copy)]
pub enum Language {
    English,
    Chinese,
}

impl Language {
    pub fn choose(self, english: &'static str, chinese: &'static str) -> &'static str {
        match self {
            Self::English => english,
            Self::Chinese => chinese,
        }
    }

    pub fn tag(self) -> &'static str {
        self.choose("en", "zh-CN")
    }

    pub fn status(self, role: &Role, status: Status) -> &'static str {
        if matches!(role, Role::Remote(_)) {
            return match status {
                Status::Running if cfg!(target_os = "macos") => self.choose(
                    "Remote mode: Mac agent running",
                    "远程模式：Mac agent 运行中",
                ),
                Status::Running => self.choose("Remote mode: enabled", "远程模式：已启用"),
                Status::Starting => self.choose("Remote mode: starting", "远程模式：正在启动"),
                Status::Failed => self.choose(
                    "Remote mode: Mac agent failed or exited",
                    "远程模式：Mac agent 启动失败或已退出",
                ),
                _ => self.choose("Remote mode: stopped", "远程模式：已停止"),
            };
        }

        match status {
            Status::Starting => self.choose("Hub mode: starting", "主机模式：正在启动"),
            Status::Running => self.choose("Hub mode: running", "主机模式：运行中"),
            Status::External => self.choose(
                "Hub mode: running externally",
                "主机模式：运行中（外部托管）",
            ),
            Status::Stopped => self.choose("Hub mode: stopped", "主机模式：已停止"),
            Status::Occupied => self.choose("Hub mode: port in use", "主机模式：端口已被占用"),
            Status::Failed => self.choose("Hub mode: failed to start", "主机模式：启动失败"),
            Status::Unconfigured => {
                self.choose("Desktop mode: check settings", "桌面模式：请检查设置")
            }
        }
    }
}

pub struct LanguageStore {
    path: PathBuf,
    preference: Mutex<LanguagePreference>,
    system: Language,
}

impl LanguageStore {
    pub fn load(home: &Path) -> Self {
        let path = home.join("desktop-language.json");
        let preference = File::open(&path)
            .ok()
            .and_then(|file| serde_json::from_reader(file).ok())
            .unwrap_or_default();
        Self {
            path,
            preference: Mutex::new(preference),
            system: system_language(),
        }
    }

    pub fn language(&self) -> Language {
        match *self.preference.lock().unwrap() {
            LanguagePreference::Auto => self.system,
            LanguagePreference::English => Language::English,
            LanguagePreference::Chinese => Language::Chinese,
        }
    }

    pub fn set(&self, preference: LanguagePreference) -> io::Result<()> {
        let mut current = self.preference.lock().unwrap();
        if *current != preference {
            save_json(&self.path, &preference)?;
            *current = preference;
        }
        Ok(())
    }
}

#[cfg(windows)]
fn system_language() -> Language {
    use windows_sys::Win32::Globalization::GetUserDefaultUILanguage;

    // The primary language ID is stored in the low 10 bits of the Windows LANGID.
    if unsafe { GetUserDefaultUILanguage() } & 0x03ff == 0x04 {
        Language::Chinese
    } else {
        Language::English
    }
}

#[cfg(target_os = "macos")]
fn system_language() -> Language {
    std::process::Command::new("/usr/bin/defaults")
        .args(["read", "-g", "AppleLanguages"])
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .and_then(|languages| languages.lines().nth(1).map(language_from_locale))
        .unwrap_or(Language::English)
}

#[cfg(not(any(windows, target_os = "macos")))]
fn system_language() -> Language {
    ["LC_ALL", "LC_MESSAGES", "LANG"]
        .into_iter()
        .filter_map(|key| std::env::var(key).ok())
        .find(|value| !value.is_empty())
        .map_or(Language::English, |locale| language_from_locale(&locale))
}

#[cfg(not(windows))]
fn language_from_locale(locale: &str) -> Language {
    let locale = locale.trim().trim_matches(['"', ',']);
    let primary = locale.split(['-', '_', '.']).next();
    if primary.is_some_and(|value| value.eq_ignore_ascii_case("zh")) {
        Language::Chinese
    } else {
        Language::English
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn explicit_language_survives_relaunch_without_touching_pairing() {
        let home = tempfile::tempdir().unwrap();
        let path = home.path().join("desktop-settings.json");
        std::fs::write(&path, "existing settings").unwrap();
        let store = LanguageStore::load(home.path());
        store.set(LanguagePreference::Chinese).unwrap();
        let reopened = LanguageStore::load(home.path());
        assert_eq!(reopened.language().tag(), "zh-CN");
        reopened.set(LanguagePreference::English).unwrap();
        assert_eq!(LanguageStore::load(home.path()).language().tag(), "en");
        assert_eq!(std::fs::read_to_string(path).unwrap(), "existing settings");
    }
}
