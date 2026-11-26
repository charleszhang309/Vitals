import unittest

from mac_agent.clipboard import latest_text
from mac_agent.macos import parse_battery, parse_volume, execute_command
from mac_agent.reminders import snapshot_changes


class AgentTests(unittest.TestCase):
    def test_battery_distinguishes_ac_without_charging(self):
        self.assertEqual(parse_battery("78%; AC attached; not charging present: true"),
                         {"percent": 78, "charging": False, "state": "not_charging",
                          "minutes_left": None})
        self.assertEqual(parse_battery("46%; discharging; 2:05 remaining present: true")["minutes_left"], 125)
        self.assertIsNone(parse_battery("No batteries."))

    def test_volume(self):
        self.assertEqual(parse_volume("output volume:70, input volume:40, alert volume:100, output muted:true"),
                         {"percent": 70, "muted": True})

    def test_commands_reject_out_of_range_and_unknown_parameters(self):
        for action, args in (("volume_set", {"percent": 101}), ("volume_mute", {"muted": "false"}),
                             ("sleep", {"script": "echo hi"}), ("shell", {})):
            with self.subTest(action=action), self.assertRaises(ValueError):
                execute_command(action, args)

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
