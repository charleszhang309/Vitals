use std::io;

pub const BACKGROUND_ARG: &str = "--background";

#[cfg(any(windows, target_os = "macos"))]
fn optional<T>(result: io::Result<T>) -> io::Result<Option<T>> {
    match result {
        Ok(value) => Ok(Some(value)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

#[cfg(windows)]
mod platform {
    use super::{optional, BACKGROUND_ARG};
    use std::ffi::{OsStr, OsString};
    use std::io;
    use winreg::enums::{HKEY_CURRENT_USER, KEY_SET_VALUE, REG_BINARY};
    use winreg::{RegKey, RegValue};

    const RUN: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
    const APPROVED: &str =
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run";
    const NAME: &str = "Vitals";

    pub fn is_enabled() -> io::Result<bool> {
        let user = RegKey::predef(HKEY_CURRENT_USER);
        let entry = optional(
            user.open_subkey(RUN)
                .and_then(|key| key.get_value::<OsString, _>(NAME)),
        )?;
        if entry.is_none() {
            return Ok(false);
        }
        let approval = optional(
            user.open_subkey(APPROVED)
                .and_then(|key| key.get_raw_value(NAME)),
        )?;
        Ok(!approval.as_ref().is_some_and(is_disabled))
    }

    fn is_disabled(value: &RegValue<'_>) -> bool {
        value.vtype == REG_BINARY
            && value.bytes.len() == 12
            && value.bytes[4..].iter().any(|&byte| byte != 0)
    }

    fn command(executable: &OsStr) -> OsString {
        let mut command = OsString::from("\"");
        command.push(executable);
        command.push("\" ");
        command.push(BACKGROUND_ARG);
        command
    }

    pub fn set_enabled(enabled: bool) -> io::Result<()> {
        let user = RegKey::predef(HKEY_CURRENT_USER);
        if !enabled {
            optional(
                user.open_subkey_with_flags(RUN, KEY_SET_VALUE)
                    .and_then(|key| key.delete_value(NAME)),
            )?;
            return Ok(());
        }

        let executable = std::env::current_exe()?;
        let command = command(executable.as_os_str());
        user.create_subkey(RUN)?.0.set_value(NAME, &command)?;
        // Windows can keep a separate disabled flag after the Run entry is recreated.
        if let Some(key) = optional(user.open_subkey_with_flags(APPROVED, KEY_SET_VALUE))? {
            optional(key.delete_value(NAME))?;
        }
        Ok(())
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn windows_login_command_quotes_paths_with_spaces() {
            let path = OsStr::new(r"C:\Program Files\Vitals\Vitals.exe");
            assert_eq!(
                command(path),
                r#""C:\Program Files\Vitals\Vitals.exe" --background"#
            );
        }
    }
}

#[cfg(target_os = "macos")]
mod platform {
    use super::{optional, BACKGROUND_ARG};
    use serde::Serialize;
    use std::fs;
    use std::io;
    use std::path::{Path, PathBuf};

    const LABEL: &str = "org.vitals.desktop";

    #[derive(Serialize)]
    #[serde(rename_all = "PascalCase")]
    struct LaunchAgent<'a> {
        label: &'a str,
        program_arguments: [&'a str; 2],
        run_at_load: bool,
        associated_bundle_identifiers: [&'a str; 1],
    }

    fn directory() -> io::Result<PathBuf> {
        let home = std::env::home_dir()
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "missing home directory"))?;
        Ok(home.join("Library/LaunchAgents"))
    }

    fn legacy_agent(directory: &Path) -> Option<PathBuf> {
        let path = directory.join("Vitals.plist");
        let agent = plist::Value::from_file(&path).ok()?;
        let label = agent.as_dictionary()?.get("Label")?.as_string()?;
        (label == "Vitals").then_some(path)
    }

    pub fn is_enabled() -> io::Result<bool> {
        let directory = directory()?;
        Ok(directory.join("org.vitals.desktop.plist").try_exists()?
            || legacy_agent(&directory).is_some())
    }

    pub fn set_enabled(enabled: bool) -> io::Result<()> {
        let directory = directory()?;
        let path = directory.join("org.vitals.desktop.plist");
        if enabled {
            let executable = std::env::current_exe()?.canonicalize()?;
            let executable = executable.to_str().ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidData, "invalid Vitals executable path")
            })?;
            let agent = LaunchAgent {
                label: LABEL,
                program_arguments: [executable, BACKGROUND_ARG],
                run_at_load: true,
                associated_bundle_identifiers: [LABEL],
            };
            fs::create_dir_all(&directory)?;
            let mut pending = tempfile::NamedTempFile::new_in(&directory)?;
            plist::to_writer_xml(&mut pending, &agent).map_err(io::Error::other)?;
            pending.as_file().sync_all()?;
            pending.persist(path)?;
        } else {
            optional(fs::remove_file(path))?;
        }
        if let Some(legacy) = legacy_agent(&directory) {
            optional(fs::remove_file(legacy))?;
        }
        Ok(())
    }
}

#[cfg(not(any(windows, target_os = "macos")))]
mod platform {
    use std::io;

    pub fn is_enabled() -> io::Result<bool> {
        Ok(false)
    }

    pub fn set_enabled(_enabled: bool) -> io::Result<()> {
        Err(io::Error::from(io::ErrorKind::Unsupported))
    }
}

pub use platform::{is_enabled, set_enabled};
