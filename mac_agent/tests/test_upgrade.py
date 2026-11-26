import plistlib
from queue import Queue
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from mac_agent import agent, macos, playback


class UpgradeTests(unittest.TestCase):
    @patch("mac_agent.macos.psutil.disk_usage")
    @patch("mac_agent.macos.psutil.disk_partitions")
    @patch("mac_agent.macos.native")
    def test_only_root_and_real_external_partitions(self, native, partitions, usage):
        partitions.return_value = [
            SimpleNamespace(device="/dev/disk3s1", mountpoint="/", fstype="apfs"),
            SimpleNamespace(device="/dev/disk3s5", mountpoint="/System/Volumes/Data", fstype="apfs"),
            SimpleNamespace(device="/dev/disk1s1", mountpoint="/Volumes/Recovery", fstype="apfs"),
            SimpleNamespace(device="/dev/disk4s1", mountpoint="/Volumes/USB", fstype="exfat"),
            SimpleNamespace(device="/dev/disk7s1", mountpoint="/Volumes/Image", fstype="apfs"),
            SimpleNamespace(device="map", mountpoint="/Volumes/Virtual", fstype="autofs"),
        ]
        native.side_effect = [plistlib.dumps({"Internal": False, "BusProtocol": p}).decode()
                              for p in ("USB", "Disk Image")]
        usage.return_value = SimpleNamespace(free=10, total=20)
        self.assertEqual([(d["mountpoint"], d["name"]) for d in macos.Disks().read()],
                         [("/", "Macintosh HD"), ("/Volumes/USB", "USB")])

    def test_spotify_duration_is_converted_and_track_id_ignores_position(self):
        raw = {"source": "spotify", "player": "Spotify", "status": "playing", "identity": "spotify:track:fixture",
               "title": "Title", "album": "Album", "artist": "Artist", "position": 10, "duration": 205000}
        first = playback.normalize_track(raw)
        second = playback.normalize_track({**raw, "position": 40})
        self.assertEqual(first["duration_seconds"], 205)
        self.assertEqual(second["position_seconds"], 40)
        self.assertEqual(first["track_id"], second["track_id"])

    @patch("mac_agent.macos.audio_tool", return_value=None)
    def test_missing_audio_tool_keeps_mute_and_volume(self, _):
        with patch("mac_agent.macos.apple", return_value="output volume:70, output muted:true"):
            self.assertEqual(macos.volume(), {"percent": 70, "muted": True, "devices": [], "device_id": None})

    @patch("mac_agent.macos.native")
    def test_display_lock_uses_pmset(self, native):
        macos.execute_command("display_lock", {})
        native.assert_called_once_with(macos.PMSET, "displaysleepnow")

    def test_sse_listener_parses_command_frames_and_ignores_keepalive(self):
        stopped = Event()
        inbox = Queue()

        def server(request):
            self.assertEqual(request.url.path, "/api/agents/macbook/commands/stream")
            self.assertEqual(request.headers["Accept"], "text/event-stream")
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=(
                ': connected\n\n: keepalive\n\nevent: other\ndata: ignored\n\n'
                'event: command\ndata: {"id":"a",\ndata: "action":"media_play_pause","payload":{}}\n\n'
            ))

        config = SimpleNamespace(device_id="macbook", client=lambda **_: httpx.Client(
            base_url="https://fixture.test", transport=httpx.MockTransport(server)))
        with patch.object(stopped, "wait", side_effect=lambda _: stopped.set()):
            agent.listen_commands(config, stopped, inbox)
        self.assertEqual(inbox.get_nowait(), [{"id": "a", "action": "media_play_pause", "payload": {}}])
        self.assertTrue(inbox.empty())

    @patch("mac_agent.agent.execute_command")
    def test_receipt_worker_executes_once_and_requests_a_fresh_snapshot(self, execute):
        from tempfile import TemporaryDirectory
        from pathlib import Path

        stopped, sample_now, inbox = Event(), Event(), Queue()
        reported, snapshots = [], []

        def server(request):
            reported.append(request.url.path)
            return httpx.Response(204)

        config = SimpleNamespace(device_id="macbook")
        command = {"id": "a", "action": "volume_mute", "payload": {"muted": True}}
        execute.return_value = {"volume": {"muted": True}}

        def publish(update):
            snapshots.append(update)
            sample_now.set()

        publisher = SimpleNamespace(control=publish)
        with httpx.Client(base_url="https://fixture.test", transport=httpx.MockTransport(server)) as client, TemporaryDirectory() as path:
            worker = Thread(target=agent.command_worker,
                            args=(client, config, Path(path), None, stopped, inbox, None, publisher, Event()), daemon=True)
            worker.start()
            try:
                inbox.put([command, command])
                self.assertTrue(sample_now.wait(1))
            finally:
                stopped.set()
                inbox.put([])
                worker.join(1)
        self.assertEqual(snapshots, [{"volume": {"muted": True}}])
        self.assertFalse(worker.is_alive())
        execute.assert_called_once()
        self.assertEqual(reported, ["/api/agents/macbook/commands/a/result"])

    def test_stream_reconnect_delays_and_recovers_after_a_command(self):
        stopped = Event()
        attempts, waits = [], []

        def server(request):
            attempts.append(request)
            if len(attempts) == 4:
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=(
                    'event: command\ndata: malformed\n\n'
                    'event: command\ndata: {"id":"wake","action":"media_next"}\n\n'
                ))
            return httpx.Response(503)

        def wait(delay):
            waits.append(delay)
            if len(waits) == 5:
                stopped.set()

        inbox = Queue()
        config = SimpleNamespace(device_id="macbook", client=lambda **_: httpx.Client(
            base_url="https://fixture.test", headers={"Authorization": "Bearer fixture"}, transport=httpx.MockTransport(server)))
        with patch.object(stopped, "wait", side_effect=wait):
            agent.listen_commands(config, stopped, inbox)
        self.assertEqual(waits, [1, 2, 5, 1, 2])
        self.assertEqual(inbox.get_nowait(), [{"id": "wake", "action": "media_next"}])
        self.assertTrue(all(request.headers["Authorization"] == "Bearer fixture" for request in attempts))

    def test_hardware_publication_does_not_roll_back_a_concurrent_control_update(self):
        sent = []
        config = SimpleNamespace(device_id="macbook", device_name="MacBook")

        def server(request):
            import json
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"commands": []})

        with httpx.Client(base_url="https://fixture.test", transport=httpx.MockTransport(server)) as client:
            publisher = agent.SnapshotPublisher(client, config, None, Queue())
            initial = publisher.revision()
            publisher.hardware({"cpu": {"usage_percent": 2}, "volume": {"percent": 40, "devices": [{"id": "a"}]},
                                "media": {"track_id": "first"}}, initial, None)
            before = publisher.revision()
            publisher.control({"volume": {"percent": 32}, "media": {"track_id": "second"}})
            publisher.hardware({"cpu": {"usage_percent": 4}, "volume": {"percent": 40, "devices": [{"id": "a"}]},
                                "media": {"track_id": "first"}}, before, None)
        self.assertEqual(sent[1]["cpu"]["usage_percent"], 2)
        self.assertEqual(sent[1]["volume"], {"percent": 32, "devices": [{"id": "a"}]})
        self.assertEqual(sent[2]["cpu"]["usage_percent"], 4)
        self.assertEqual(sent[2]["volume"]["percent"], 32)
        self.assertEqual(sent[2]["media"]["track_id"], "second")

    @patch("mac_agent.agent.execute_command", return_value=None)
    def test_media_result_is_reported_before_requesting_player_state(self, execute):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        refresh = Event()
        results = []

        def server(request):
            self.assertFalse(refresh.is_set())
            results.append(request.url.path)
            return httpx.Response(204)

        config = SimpleNamespace(device_id="macbook")
        command = {"id": "next-one", "action": "media_next", "payload": {}}
        with httpx.Client(base_url="https://fixture.test", transport=httpx.MockTransport(server)) as client, TemporaryDirectory() as folder:
            agent.process_commands(client, config, [command], Path(folder), None, None, None, refresh)
        execute.assert_called_once()
        self.assertEqual(results, ["/api/agents/macbook/commands/next-one/result"])
        self.assertTrue(refresh.is_set())
