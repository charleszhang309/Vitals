#[path = "../src/settings.rs"]
mod settings;

use settings::{Role, Selection, SettingsStore};
use std::sync::mpsc;

#[test]
fn remote_url_requires_an_https_origin_and_keeps_token_private() {
    let home = tempfile::tempdir().unwrap();
    let (store, active) = SettingsStore::load(home.path());
    assert!(matches!(active, Role::Hub));
    let (send, receive) = mpsc::channel();
    let apply = |url: &str, token: &str| {
        store.apply(Selection::Remote, url, token, |role| {
            send.send(role).map_err(|_| "Service stopped")
        })
    };

    for invalid in [
        "http://example.test",
        "https:///example.test",
        "https://example.test:0",
        "https://*.example.test",
        "https://user@example.test",
        "https://@example.test",
        "https://example.test?",
        "https://example.test#",
        "https://example.test/a/..",
        "https://example.test\\",
    ] {
        assert!(apply(invalid, "private-test-token").is_err(), "{invalid}");
    }
    assert!(apply("https://example.test", " ").is_err());
    assert!(receive.try_recv().is_err());
    apply("https://EXAMPLE.test:443/", "private-test-token").unwrap();
    let Role::Remote(remote) = receive.recv().unwrap() else {
        panic!("remote role")
    };
    assert_eq!(remote.address().as_str(), "https://example.test/");
    assert!(!serde_json::to_string(&store.view())
        .unwrap()
        .contains("private-test-token"));
    assert_eq!(
        serde_json::to_value(store.mode_view()).unwrap(),
        serde_json::json!({
            "mode": "remote", "remote_url": "https://example.test/", "has_token": true,
        })
    );
    assert!(apply("https://new.test", "").is_err());
    apply("https://example.test", "").unwrap();
    let (_, reloaded) = SettingsStore::load(home.path());
    assert!(reloaded == Role::Remote(remote));
}

#[test]
fn corrupted_settings_require_reconfiguration() {
    let home = tempfile::tempdir().unwrap();
    std::fs::write(home.path().join("desktop-settings.json"), "invalid").unwrap();
    let (_, active) = SettingsStore::load(home.path());
    assert!(matches!(active, Role::Unconfigured));
}
