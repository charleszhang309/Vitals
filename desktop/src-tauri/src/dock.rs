use crate::settings::save_json;
use std::fs::File;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

pub struct DockPreference {
    path: PathBuf,
    hidden: Mutex<bool>,
}

impl DockPreference {
    pub fn load(home: &Path) -> Self {
        let path = home.join("desktop-hide-dock.json");
        let hidden = File::open(&path)
            .ok()
            .and_then(|file| serde_json::from_reader(file).ok())
            .unwrap_or(false);
        Self {
            path,
            hidden: Mutex::new(hidden),
        }
    }

    pub fn hidden(&self) -> bool {
        *self.hidden.lock().unwrap()
    }

    pub fn set_hidden(&self, hidden: bool) -> io::Result<()> {
        let mut current = self.hidden.lock().unwrap();
        if *current != hidden {
            save_json(&self.path, &hidden)?;
            *current = hidden;
        }
        Ok(())
    }

    pub fn shows_icon(&self, has_window: bool) -> bool {
        has_window && !self.hidden()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn desktop_chrome_dock_preference_stays_hidden_across_relaunch() {
        let home = tempfile::tempdir().unwrap();
        let preference = DockPreference::load(home.path());
        assert!(!preference.hidden());
        assert!(preference.shows_icon(true));
        assert!(!preference.shows_icon(false));

        preference.set_hidden(true).unwrap();
        let reloaded = DockPreference::load(home.path());
        assert!(reloaded.hidden());
        assert!(!reloaded.shows_icon(true));

        reloaded.set_hidden(false).unwrap();
        assert!(!DockPreference::load(home.path()).hidden());
    }

    #[test]
    fn desktop_chrome_failed_dock_preference_write_keeps_previous_value() {
        let home = tempfile::tempdir().unwrap();
        let blocked = home.path().join("blocked");
        File::create(&blocked).unwrap();
        let preference = DockPreference::load(&blocked);

        assert!(preference.set_hidden(true).is_err());
        assert!(!preference.hidden());
    }
}
