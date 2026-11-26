import plistlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mac_agent import macos, playback


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
