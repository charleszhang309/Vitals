"""Dispatch Resolve's default keyboard shortcuts through Vitals desktop input."""

import sys

from mac_agent.desktop_input import dispatch_input


PLATFORMS = {"win32": ("windows", "ctrl"), "darwin": ("macos", "cmd")}
SHORTCUTS = {
    "play-pause": "space",
    "shuttle-reverse": "j",
    "shuttle-stop": "k",
    "shuttle-forward": "l",
    "razor": "b",
    "split-clip": "{primary}+b",
    "ripple-delete": "shift+backspace",
    "add-marker": "m",
    "undo": "{primary}+z",
    "redo": "{primary}+shift+z",
    "page-edit": "shift+4",
    "page-color": "shift+6",
    "page-fairlight": "shift+7",
    "page-deliver": "shift+8",
}


def execute_action(action_id, params):
    if sys.platform not in PLATFORMS:
        return {"ok": False, "message": "仅支持 Windows 和 macOS。 / Supported on Windows and macOS."}

    platform, primary = PLATFORMS[sys.platform]
    try:
        dispatch_input(platform, shortcut=SHORTCUTS[action_id].format(primary=primary))
    except (OSError, RuntimeError):
        return {"ok": False, "message": (
            "无法发送快捷键，请检查桌面会话和辅助功能权限。 / "
            "Unable to send the shortcut. Check the desktop session and Accessibility permission."
        )}
    return {"ok": True}
