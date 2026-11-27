#![cfg(windows)]

// Exercise the same supervisor that receives native tray menu operations.
#[path = "../src/settings.rs"]
mod settings;
include!("../src/service.rs");
use settings::{Selection, SettingsStore};

fn remote_role(home: &Path) -> Role {
    let (store, default) = SettingsStore::load(home);
    assert!(default == Role::Hub);
    let (send, receive) = std::sync::mpsc::channel();
    store
        .apply(
            Selection::Remote,
            "https://vitals.example.test/",
            "test-only-token",
            |role| send.send(role).map_err(|_| "Supervisor stopped"),
        )
        .unwrap();
    assert!(!serde_json::to_string(&store.view())
        .unwrap()
        .contains("test-only-token"));
    let active = receive.recv().unwrap();
    let Role::Remote(remote) = &active else {
        panic!("configured remote role")
    };
    assert_eq!(remote.address().host_str(), Some("vitals.example.test"));
    active
}

#[test]
#[ignore = "requires the packaged sidecar from build_windows.ps1"]
fn tray_owned_hub_lifecycle() {
    let desktop = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..");
    let sidecar = desktop.join(".build/frozen/vitals-hub/vitals-hub.exe");
    assert!(sidecar.is_file(), "build the Windows sidecar first");
    let home = desktop
        .join(".build/smoke")
        .join(uuid::Uuid::new_v4().to_string());
    fs::create_dir_all(&home).unwrap();
    let config = home.join("config.json");
    let valid_config = br#"{"host":"127.0.0.1"}"#;
    fs::write(&config, valid_config).unwrap();
    let socket = std::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let port = socket.local_addr().unwrap().port();
    drop(socket);

    let hub = Hub {
        home: home.clone(),
        sidecar,
        dev_project: None,
        port,
        child: None,
        starting: None,
    };
    let (send, receive) = std::sync::mpsc::channel();
    let (report, statuses) = std::sync::mpsc::channel();
    let worker = std::thread::spawn(move || {
        Supervisor::new(hub, Role::Hub, PathBuf::new()).run(receive, |_, status| {
            let _ = report.send(status);
        })
    });
    let wait = |expected: Status| {
        let actual = statuses.recv_timeout(Duration::from_secs(35)).unwrap();
        assert_eq!(actual, expected);
    };
    let probe = || {
        let mut stream = TcpStream::connect((Ipv4Addr::LOCALHOST, port)).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(4)))
            .unwrap();
        write!(
            stream,
            "GET /api/setup/status HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
        )
        .unwrap();
        let mut reply = String::new();
        stream.read_to_string(&mut reply).unwrap();
        let (_, body) = reply.split_once("\r\n\r\n").unwrap();
        serde_json::from_str::<Value>(body).unwrap()["instance"]
            .as_str()
            .unwrap()
            .to_owned()
    };
    wait(Status::Starting);
    wait(Status::Running);
    let first = probe();
    send.send(Operation::Restart).unwrap();
    wait(Status::Starting);
    wait(Status::Running);
    assert_ne!(probe(), first);
    send.send(Operation::Stop).unwrap();
    wait(Status::Stopped);
    assert!(TcpStream::connect((Ipv4Addr::LOCALHOST, port)).is_err());
    fs::write(&config, b"{invalid").unwrap();
    for _ in 0..2 {
        send.send(Operation::Start).unwrap();
        wait(Status::Starting);
        wait(Status::Failed);
    }
    fs::write(&config, valid_config).unwrap();
    send.send(Operation::Start).unwrap();
    wait(Status::Starting);
    wait(Status::Running);
    let remote = remote_role(&home);
    send.send(Operation::Configure(remote)).unwrap();
    wait(Status::Starting);
    wait(Status::Running);
    assert!(TcpStream::connect((Ipv4Addr::LOCALHOST, port)).is_err());
    send.send(Operation::Restart).unwrap();
    wait(Status::Starting);
    wait(Status::Running);
    assert!(TcpStream::connect((Ipv4Addr::LOCALHOST, port)).is_err());
    send.send(Operation::Configure(Role::Hub)).unwrap();
    wait(Status::Starting);
    wait(Status::Running);
    probe();
    send.send(Operation::Quit).unwrap();
    worker.join().unwrap();
    assert!(TcpStream::connect((Ipv4Addr::LOCALHOST, port)).is_err());
}

#[test]
fn remote_start_never_creates_hub_home() {
    let directory = tempfile::tempdir().unwrap();
    let home = directory.path().join("hub");
    let hub = Hub {
        home: home.clone(),
        sidecar: directory.path().join("missing.exe"),
        dev_project: None,
        port: 3993,
        child: None,
        starting: None,
    };
    let remote = remote_role(directory.path());
    let (send, receive) = std::sync::mpsc::channel();
    send.send(Operation::Quit).unwrap();
    let mut running = false;
    Supervisor::new(hub, remote.clone(), directory.path().to_owned()).run(
        receive,
        |role, status| {
            assert!(role == &remote);
            running |= status == Status::Running;
        },
    );
    assert!(running);
    assert!(!home.exists());
}
