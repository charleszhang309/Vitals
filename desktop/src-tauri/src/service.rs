use serde_json::Value;
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::net::{Ipv4Addr, SocketAddrV4, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager};

#[derive(Clone, Copy, PartialEq)]
pub enum Status {
    Starting,
    Running,
    External,
    Stopped,
    Occupied,
    Failed,
}

impl Status {
    pub fn label(self) -> &'static str {
        match self {
            Self::Starting => "服务状态：正在启动",
            Self::Running => "服务状态：运行中",
            Self::External => "服务状态：运行中（外部托管）",
            Self::Stopped => "服务状态：已停止",
            Self::Occupied => "服务状态：端口已被占用",
            Self::Failed => "服务状态：启动失败",
        }
    }
}

pub enum Operation {
    Start,
    Stop,
    Restart,
    Quit,
}

enum Probe {
    Free,
    Busy,
    Vitals(Option<String>),
}

pub struct Hub {
    home: PathBuf,
    sidecar: PathBuf,
    dev_project: Option<PathBuf>,
    port: u16,
    child: Option<Child>,
}

impl Hub {
    pub fn new(app: &AppHandle) -> Result<Self, Box<dyn std::error::Error>> {
        let parent = std::env::current_exe()?.parent().unwrap().to_owned();
        let sidecar = parent.join(if cfg!(windows) {
            "vitals-hub.exe"
        } else {
            "vitals-hub"
        });
        let (home, dev_project) = (app.path().app_local_data_dir()?, None);
        #[cfg(debug_assertions)]
        let (home, dev_project) = if !sidecar.is_file() {
            let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("../..")
                .canonicalize()?;
            (root.clone(), Some(root))
        } else {
            (home, dev_project)
        };
        let home = match std::env::var_os("VITALS_DESKTOP_HOME") {
            Some(path) => std::path::absolute(path)?,
            None => home,
        };
        let port = match std::env::var("VITALS_DESKTOP_PORT") {
            Ok(port) => port.parse::<std::num::NonZeroU16>()?.get(),
            Err(std::env::VarError::NotPresent) => 3993,
            Err(error) => return Err(error.into()),
        };
        Ok(Self {
            home,
            sidecar,
            dev_project,
            port,
            child: None,
        })
    }

    pub fn url(&self) -> String {
        format!("http://127.0.0.1:{}/", self.port)
    }

    fn probe(&self) -> Probe {
        let address = SocketAddrV4::new(Ipv4Addr::LOCALHOST, self.port);
        let timeout = Duration::from_millis(350);
        let mut stream = match TcpStream::connect_timeout(&address.into(), timeout) {
            Ok(stream) => stream,
            // Windows may time out before it reports a closed local port. The hub
            // acquires exclusive listeners and the instance ID verifies ownership.
            Err(_) => return Probe::Free,
        };
        let _ = stream.set_read_timeout(Some(timeout));
        let _ = stream.set_write_timeout(Some(timeout));
        if write!(
            stream,
            "GET /api/setup/status HTTP/1.0\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n",
            self.port
        )
        .is_err()
        {
            return Probe::Busy;
        }
        let mut reply = String::new();
        if stream.take(4096).read_to_string(&mut reply).is_ok() {
            if let Some((headers, body)) = reply.split_once("\r\n\r\n") {
                if headers.starts_with("HTTP/1.1 200 ") || headers.starts_with("HTTP/1.0 200 ") {
                    if let Ok(payload) = serde_json::from_str::<Value>(body) {
                        if payload["service"] == "Vitals" {
                            return Probe::Vitals(payload["instance"].as_str().map(str::to_owned));
                        }
                    }
                }
            }
        }
        Probe::Busy
    }

    fn spawn(&self, instance: &str) -> io::Result<Child> {
        fs::create_dir_all(&self.home)?;
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.home.join("desktop-hub.log"))?;
        let mut command = if self.sidecar.is_file() {
            let mut command = Command::new(&self.sidecar);
            command.current_dir(&self.home);
            command
        } else if let Some(root) = &self.dev_project {
            let python = root.join(if cfg!(windows) {
                ".venv/Scripts/python.exe"
            } else {
                ".venv/bin/python"
            });
            let mut command = Command::new(python);
            command.current_dir(root).args(["-B", "-m", "backend.main"]);
            command
        } else {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "Vitals hub sidecar missing",
            ));
        };
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        #[cfg(target_os = "macos")]
        {
            use std::os::unix::process::CommandExt;
            // PyInstaller runs the frozen Python interpreter in a child of its bootloader.
            command.process_group(0);
        }
        command
            .arg("--config")
            .arg(self.home.join("config.json"))
            .arg("--data-dir")
            .arg(self.home.join("data"))
            .arg("--port")
            .arg(self.port.to_string())
            .arg("--desktop-stdio")
            .stdin(Stdio::piped())
            .stdout(Stdio::from(log.try_clone()?))
            .stderr(Stdio::from(log))
            .env("PYTHONUTF8", "1")
            .env("VITALS_DESKTOP_INSTANCE", instance)
            .spawn()
    }

    fn start(&mut self, report: &mut impl FnMut(Status)) -> Status {
        match self.probe() {
            Probe::Vitals(_) => return Status::External,
            Probe::Busy => return Status::Occupied,
            Probe::Free => {}
        }
        report(Status::Starting);
        let instance = uuid::Uuid::new_v4().to_string();
        match self.spawn(&instance) {
            Ok(child) => self.child = Some(child),
            Err(error) => {
                eprintln!("Could not start Vitals hub: {error}");
                return Status::Failed;
            }
        }
        let deadline = Instant::now() + Duration::from_secs(30);
        while Instant::now() < deadline {
            let child = self.child.as_mut().expect("hub child");
            if !matches!(child.try_wait(), Ok(None)) {
                self.stop();
                return if matches!(self.probe(), Probe::Vitals(_)) {
                    Status::External
                } else {
                    Status::Failed
                };
            }
            if matches!(self.probe(), Probe::Vitals(Some(found)) if found == instance) {
                return Status::Running;
            }
            std::thread::sleep(Duration::from_millis(180));
        }
        self.stop();
        Status::Failed
    }

    fn stop(&mut self) {
        if let Some(mut child) = self.child.take() {
            drop(child.stdin.take());
            let deadline = Instant::now() + Duration::from_secs(8);
            while Instant::now() < deadline {
                match child.try_wait() {
                    Ok(Some(_)) => return,
                    Ok(None) => std::thread::sleep(Duration::from_millis(100)),
                    Err(_) => break,
                }
            }
            #[cfg(target_os = "macos")]
            // The unreaped bootloader PID is also the process group ID.
            unsafe {
                libc::kill(-(child.id() as libc::pid_t), libc::SIGKILL);
            }
            let _ = child.kill();
            let _ = child.wait();
        }
    }

    pub fn run(mut self, operations: Receiver<Operation>, mut report: impl FnMut(Status)) {
        let mut status = self.start(&mut report);
        report(status);
        loop {
            let next = match operations.recv_timeout(Duration::from_secs(2)) {
                Ok(Operation::Quit) | Err(RecvTimeoutError::Disconnected) => {
                    self.stop();
                    return;
                }
                Ok(Operation::Start) if status != Status::Running => self.start(&mut report),
                Ok(Operation::Stop) if status == Status::Running => {
                    self.stop();
                    Status::Stopped
                }
                Ok(Operation::Restart) if status == Status::Running => {
                    self.stop();
                    status = Status::Starting;
                    self.start(&mut report)
                }
                Err(RecvTimeoutError::Timeout) if status == Status::Running => {
                    if self
                        .child
                        .as_mut()
                        .is_some_and(|child| matches!(child.try_wait(), Ok(None)))
                    {
                        status
                    } else {
                        self.stop();
                        Status::Failed
                    }
                }
                Err(RecvTimeoutError::Timeout)
                    if matches!(status, Status::External | Status::Occupied) =>
                {
                    match self.probe() {
                        Probe::Vitals(_) => Status::External,
                        Probe::Busy => Status::Occupied,
                        Probe::Free => Status::Stopped,
                    }
                }
                _ => status,
            };
            if next != status {
                report(next);
                status = next;
            }
        }
    }
}
