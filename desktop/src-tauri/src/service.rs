#[cfg(target_os = "macos")]
use crate::settings::Remote;
use crate::settings::Role;
use serde_json::Value;
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::net::{Ipv4Addr, SocketAddrV4, TcpStream};
use std::path::{Path, PathBuf};
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
    Unconfigured,
}

impl Status {
    pub fn label(self, role: &Role) -> &'static str {
        if matches!(role, Role::Remote(_)) {
            return match self {
                Self::Running if cfg!(target_os = "macos") => "远程模式：Mac agent 运行中",
                Self::Running => "远程模式：已启用",
                Self::Starting => "远程模式：正在启动",
                Self::Failed => "远程模式：Mac agent 启动失败或已退出",
                _ => "远程模式：已停止",
            };
        }
        match self {
            Self::Starting => "主机模式：正在启动",
            Self::Running => "主机模式：运行中",
            Self::External => "主机模式：运行中（外部托管）",
            Self::Stopped => "主机模式：已停止",
            Self::Occupied => "主机模式：端口已被占用",
            Self::Failed => "主机模式：启动失败",
            Self::Unconfigured => "桌面模式：请检查设置",
        }
    }
}

pub enum Operation {
    Start,
    Stop,
    Restart,
    Configure(Role),
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
    starting: Option<(String, Instant)>,
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
        let home = desktop_home(home)?;
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
            starting: None,
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

    fn command(
        &self,
        home: &Path,
        log_name: &str,
        module: &str,
        sidecar_args: &[&str],
    ) -> io::Result<Command> {
        fs::create_dir_all(home)?;
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(home.join(log_name))?;
        let mut command = if self.sidecar.is_file() {
            let mut command = Command::new(&self.sidecar);
            command.current_dir(home).args(sidecar_args);
            command
        } else if let Some(root) = &self.dev_project {
            let python = root.join(if cfg!(windows) {
                ".venv/Scripts/python.exe"
            } else {
                ".venv/bin/python"
            });
            let mut command = Command::new(python);
            command.current_dir(root).args(["-B", "-m", module]);
            command
        } else {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "Vitals sidecar missing",
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
            .stdin(Stdio::piped())
            .stdout(Stdio::from(log.try_clone()?))
            .stderr(Stdio::from(log))
            .env("PYTHONUTF8", "1");
        Ok(command)
    }

    fn spawn(&self, instance: &str) -> io::Result<Child> {
        self.command(&self.home, "desktop-hub.log", "backend.main", &[])?
            .arg("--config")
            .arg(self.home.join("config.json"))
            .arg("--data-dir")
            .arg(self.home.join("data"))
            .arg("--port")
            .arg(self.port.to_string())
            .arg("--desktop-stdio")
            .env("VITALS_DESKTOP_INSTANCE", instance)
            .spawn()
    }

    fn start(&mut self) -> Status {
        match self.probe() {
            Probe::Vitals(_) => return Status::External,
            Probe::Busy => return Status::Occupied,
            Probe::Free => {}
        }
        let instance = uuid::Uuid::new_v4().to_string();
        match self.spawn(&instance) {
            Ok(child) => self.child = Some(child),
            Err(error) => {
                eprintln!("Could not start Vitals hub: {error}");
                return Status::Failed;
            }
        }
        self.starting = Some((instance, Instant::now() + Duration::from_secs(30)));
        Status::Starting
    }

    fn poll(&mut self, status: Status) -> Status {
        match status {
            Status::Starting => {
                if !child_running(&mut self.child) {
                    self.stop();
                    return if matches!(self.probe(), Probe::Vitals(_)) {
                        Status::External
                    } else {
                        Status::Failed
                    };
                }
                let (instance, deadline) = self.starting.as_ref().expect("hub startup");
                if matches!(self.probe(), Probe::Vitals(Some(found)) if &found == instance) {
                    self.starting = None;
                    Status::Running
                } else if Instant::now() >= *deadline {
                    self.stop();
                    Status::Failed
                } else {
                    Status::Starting
                }
            }
            Status::Running if !child_running(&mut self.child) => {
                self.stop();
                Status::Failed
            }
            Status::External | Status::Occupied => match self.probe() {
                Probe::Vitals(_) => Status::External,
                Probe::Busy => Status::Occupied,
                Probe::Free => Status::Stopped,
            },
            _ => status,
        }
    }

    fn stop(&mut self) {
        self.starting = None;
        stop_child(&mut self.child);
    }
}

pub fn desktop_home(default: PathBuf) -> io::Result<PathBuf> {
    match std::env::var_os("VITALS_DESKTOP_HOME") {
        Some(path) => std::path::absolute(path),
        None => Ok(default),
    }
}

fn child_running(child: &mut Option<Child>) -> bool {
    child
        .as_mut()
        .is_some_and(|child| matches!(child.try_wait(), Ok(None)))
}

fn stop_child(child: &mut Option<Child>) {
    let Some(mut child) = child.take() else {
        return;
    };
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

pub struct Supervisor {
    hub: Hub,
    role: Role,
    #[cfg(target_os = "macos")]
    home: PathBuf,
    #[cfg(target_os = "macos")]
    agent: Option<Child>,
}

impl Supervisor {
    pub fn new(hub: Hub, role: Role, _home: PathBuf) -> Self {
        Self {
            hub,
            role,
            #[cfg(target_os = "macos")]
            home: _home,
            #[cfg(target_os = "macos")]
            agent: None,
        }
    }

    #[cfg(target_os = "macos")]
    fn spawn_agent(&self, remote: &Remote) -> io::Result<Child> {
        let input = serde_json::to_vec(&serde_json::json!({
            "server_url": remote.url,
            "token": remote.token,
            "device_id": remote.device_id,
        }))?;
        let mut child = Some(
            self.hub
                .command(
                    &self.home,
                    "desktop-agent.log",
                    "mac_agent.agent",
                    &["--mac-agent"],
                )?
                .arg("--desktop-stdio")
                .spawn()?,
        );
        let stdin = child
            .as_mut()
            .unwrap()
            .stdin
            .as_mut()
            .expect("agent input pipe");
        if let Err(error) = stdin
            .write_all(&input)
            .and_then(|()| stdin.write_all(b"\n"))
        {
            stop_child(&mut child);
            return Err(error);
        }
        Ok(child.unwrap())
    }

    fn start(&mut self) -> Status {
        match &self.role {
            Role::Hub => self.hub.start(),
            #[cfg(target_os = "macos")]
            Role::Remote(remote) => match self.spawn_agent(remote) {
                Ok(child) => {
                    self.agent = Some(child);
                    Status::Running
                }
                Err(error) => {
                    eprintln!("Could not start Vitals Mac agent: {error}");
                    Status::Failed
                }
            },
            #[cfg(not(target_os = "macos"))]
            Role::Remote(_) => Status::Running,
            Role::Unconfigured => Status::Unconfigured,
        }
    }

    fn stop(&mut self) {
        self.hub.stop();
        #[cfg(target_os = "macos")]
        stop_child(&mut self.agent);
    }

    fn poll(&mut self, status: Status) -> Status {
        match self.role {
            Role::Hub => self.hub.poll(status),
            #[cfg(target_os = "macos")]
            Role::Remote(_) if status == Status::Running && !child_running(&mut self.agent) => {
                stop_child(&mut self.agent);
                Status::Failed
            }
            _ => status,
        }
    }

    pub fn run(mut self, operations: Receiver<Operation>, mut report: impl FnMut(&Role, Status)) {
        if self.role != Role::Unconfigured {
            report(&self.role, Status::Starting);
        }
        let mut status = self.start();
        if status != Status::Starting {
            report(&self.role, status);
        }
        loop {
            let refresh = if status == Status::Starting {
                Duration::from_millis(180)
            } else {
                Duration::from_secs(2)
            };
            let next = match operations.recv_timeout(refresh) {
                Ok(Operation::Quit) | Err(RecvTimeoutError::Disconnected) => {
                    self.stop();
                    return;
                }
                Ok(Operation::Start)
                    if matches!(status, Status::Stopped | Status::Failed | Status::Occupied) =>
                {
                    report(&self.role, Status::Starting);
                    status = Status::Starting;
                    self.start()
                }
                Ok(Operation::Stop)
                    if matches!(status, Status::Starting | Status::Running | Status::Failed) =>
                {
                    self.stop();
                    Status::Stopped
                }
                Ok(Operation::Restart) if status == Status::Running => {
                    report(&self.role, Status::Starting);
                    self.stop();
                    status = Status::Starting;
                    self.start()
                }
                Ok(Operation::Configure(role)) if self.role != role => {
                    report(&role, Status::Starting);
                    self.stop();
                    self.role = role;
                    status = Status::Starting;
                    self.start()
                }
                Err(RecvTimeoutError::Timeout) => self.poll(status),
                _ => status,
            };
            if next != status {
                report(&self.role, next);
                status = next;
            }
        }
    }
}
