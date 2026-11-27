import ctypes as C
import unittest
from unittest.mock import Mock, call, patch

from mac_agent.clipboard import latest_text
from mac_agent.core_audio import AudioPropertyError, CoreAudio, PropertyAddress, fourcc
from mac_agent.macos import execute_command, parse_battery, parse_input_volume, parse_volume, volume
from mac_agent.reminders import snapshot_changes


class AgentTests(unittest.TestCase):
    def test_battery_distinguishes_ac_without_charging(self):
        self.assertEqual(parse_battery("78%; AC attached; not charging present: true"),
                         {"percent": 78, "charging": False, "state": "not_charging",
                          "minutes_left": None})
        self.assertEqual(parse_battery("46%; discharging; 2:05 remaining present: true")["minutes_left"], 125)
        self.assertIsNone(parse_battery("No batteries."))

    def test_volume(self):
        settings = "output volume:70, input volume:40, alert volume:100, output muted:true"
        self.assertEqual(parse_volume(settings), {"percent": 70, "muted": True})
        self.assertEqual(parse_input_volume(settings), 40)
        self.assertIsNone(parse_input_volume("input volume:missing value, output volume:70"))

    def test_commands_reject_out_of_range_and_unknown_parameters(self):
        for action, args in (("volume_set", {"percent": 101}), ("volume_mute", {"muted": "false"}),
                             ("input_volume_set", {"percent": True}), ("input_volume_mute", {"muted": 0}),
                             ("input_audio_device_set", {"device_id": "", "command": "echo hi"}),
                             ("sleep", {"script": "echo hi"}), ("shell", {})):
            with self.subTest(action=action), self.assertRaises(ValueError):
                execute_command(action, args)

    def test_microphone_command_and_snapshot_keep_gain_and_mute_independent(self):
        settings = "output volume:70, input volume:0, output muted:true"
        audio = Mock(input_mute=Mock(return_value=False))
        with (patch("mac_agent.macos.apple", return_value=settings) as apple,
              patch("mac_agent.macos.core_audio", return_value=audio),
              patch("mac_agent.macos.audio_tool", return_value=None)):
            self.assertEqual(execute_command("input_volume_set", {"percent": 0}), {"volume": {"input_percent": 0}})
            self.assertEqual(apple.call_args_list, [call("set volume input volume 0"), call("get volume settings")])
            self.assertEqual(execute_command("input_volume_mute", {"muted": False}), {"volume": {"input_muted": False}})
            audio.input_mute.assert_called_once_with(muted=False)
            self.assertEqual(apple.call_count, 2)
            self.assertEqual(volume(), {"percent": 70, "muted": True, "devices": [], "device_id": None,
                                        "input_percent": 0, "input_muted": False, "input_devices": [], "input_device_id": None})
            audio.input_mute.side_effect = AudioPropertyError("unsupported")
            self.assertIsNone(volume()["input_muted"])
            with self.assertRaises(AudioPropertyError):
                execute_command("input_volume_mute", {"muted": True})

    def test_input_device_uses_only_capture_inventory_and_refreshes_input_state(self):
        usb = '{"id": "42", "name": "USB microphone", "uid": "usb-microphone"}'
        tool = "/opt/homebrew/bin/SwitchAudioSource"
        with (patch("mac_agent.macos.audio_tool", return_value=tool),
              patch("mac_agent.macos.native", side_effect=[usb, "switched", usb]) as native,
              patch("mac_agent.macos.apple", return_value="input volume:55"),
              patch("mac_agent.macos.core_audio", return_value=Mock(input_mute=Mock(return_value=True)))):
            self.assertEqual(execute_command("input_audio_device_set", {"device_id": "usb-microphone"}),
                             {"volume": {"input_device_id": "usb-microphone", "input_percent": 55, "input_muted": True}})
            self.assertEqual(native.call_args_list, [call(tool, "-a", "-t", "input", "-f", "json"),
                                                     call(tool, "-t", "input", "-i", "42"),
                                                     call(tool, "-c", "-t", "input", "-f", "json")])
        with (patch("mac_agent.macos.audio_tool", return_value=tool),
              patch("mac_agent.macos.native", return_value=usb) as native):
            with self.assertRaises(ValueError):
                execute_command("input_audio_device_set", {"device_id": "speaker"})
            native.assert_called_once_with(tool, "-a", "-t", "input", "-f", "json")

    def test_core_audio_uses_writable_default_input_mute_property(self):
        observed = []
        audio = CoreAudio.__new__(CoreAudio)

        def address_value(pointer):
            address = C.cast(pointer, C.POINTER(PropertyAddress)).contents
            return address.selector, address.scope, address.element

        def get(device, address, qualifiers, data, size, value):
            selector, scope, element = address_value(address)
            observed.append(("read", device, selector, scope, element))
            C.cast(value, C.POINTER(C.c_uint32)).contents.value = 42 if device == 1 else 1
            return 0

        def settable(device, address, value):
            observed.append(("settable", device, *address_value(address)))
            C.cast(value, C.POINTER(C.c_ubyte)).contents.value = 1
            return 0

        def set_property(device, address, qualifiers, data, size, value):
            observed.append(("write", device, *address_value(address), C.cast(value, C.POINTER(C.c_uint32)).contents.value))
            return 0

        audio.get, audio.set, audio.is_settable = get, set_property, settable
        self.assertIs(audio.input_mute(muted=True), True)
        capture_mute = (42, fourcc("mute"), fourcc("inpt"), 0)
        self.assertEqual(observed, [("read", 1, fourcc("dIn "), fourcc("glob"), 0),
                                    ("settable", *capture_mute), ("write", *capture_mute, 1), ("read", *capture_mute)])
        audio.is_settable = Mock(return_value=-1)
        with self.assertRaises(AudioPropertyError):
            audio.input_mute(muted=False)

    def test_newest_text_skips_files_and_older_pins(self):
        self.assertEqual(latest_text([
            {"id": 1, "kind": "text", "text": "old pin", "created_at": "2026-10-01"},
            {"id": 4, "kind": "file", "created_at": "2026-10-03"},
            {"id": 2, "kind": "text", "text": "older", "created_at": "2026-10-02"},
            {"id": 3, "kind": "text", "text": "  新\n", "created_at": "2026-10-02"},
        ]), "  新\n")

    def test_reminder_delta_preserves_completion_and_explicit_deletion(self):
        stamp = "2026-01-01T00:00:00+00:00"
        previous = {"a": {"title": "one", "done": False, "updated_at": stamp},
                    "b": {"title": "two", "done": True, "updated_at": stamp}}
        current = {"a": {"title": "one", "done": True, "updated_at": stamp},
                   "c": {"title": "three", "done": False, "updated_at": stamp}}
        changes = snapshot_changes(previous, current)
        self.assertTrue(changes["b"]["deleted"])
        self.assertTrue(changes["a"]["done"])
        self.assertEqual(changes["a"]["updated_at"], "2026-01-01T00:00:00.000001+00:00")
        self.assertEqual(changes["c"]["updated_at"], stamp)
