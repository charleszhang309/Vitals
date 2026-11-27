"""Fixed keyboard vocabulary shared by the Hub and the Mac Agent."""

import ctypes as c
from ctypes import wintypes as w
from threading import Lock


PRESETS = {
    "desktop_left": {"windows": "ctrl+win+left", "macos": "ctrl+left"},
    "desktop_right": {"windows": "ctrl+win+right", "macos": "ctrl+right"},
    "snap_left": {"windows": "win+left"},
    "snap_right": {"windows": "win+right"},
    "new_desktop": {"windows": "ctrl+win+d"},
    "close_desktop": {"windows": "ctrl+win+f4"},
    "task_view": {"windows": "win+tab"},
    "mission_control": {"macos": "ctrl+up"},
    "switch_app": {"macos": "cmd+tab"},
}
WIN_MOD = {"ctrl": 0xA2, "alt": 0xA4, "shift": 0xA0, "win": 0x5B, "cmd": 0x5B}
MAC_MOD = {"ctrl": 1 << 18, "alt": 1 << 19, "shift": 1 << 17, "cmd": 1 << 20, "win": 1 << 20}
WIN_KEYS = {**{letter: ord(letter.upper()) for letter in "abcdefghijklmnopqrstuvwxyz0123456789"},
            **{f"f{i}": 0x6F + i for i in range(1, 13)},
            "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "tab": 9, "enter": 13,
            "escape": 27, "space": 32, "backspace": 8, "delete": 46, "home": 36, "end": 35,
            "pageup": 33, "pagedown": 34}
MAC_KEYS = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40,
}
MAC_MOD_CODES = {"ctrl": 59, "alt": 58, "shift": 56, "cmd": 55, "win": 55}
MAC_KEYS.update({"n": 45, "m": 46, "left": 123, "right": 124, "down": 125, "up": 126,
                 "tab": 48, "enter": 36, "escape": 53, "space": 49, "backspace": 51,
                 "delete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121})
MAC_KEYS.update({f"f{i}": code for i, code in enumerate((122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111), 1)})
INPUT_LOCK = Lock()


def parse_hotkey(value):
    keys = [key.strip().lower() for key in value.split("+")]
    modifiers, key = keys[:-1], keys[-1]
    if (key not in WIN_KEYS or key not in MAC_KEYS or len(modifiers) > 4
            or any(item not in WIN_MOD for item in modifiers)
            or len({WIN_MOD[item] for item in modifiers}) != len(modifiers)):
        raise ValueError("无效的快捷键组合")
    return modifiers, key


def text_units(text):
    if not 1 <= len(text) <= 500 or any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise ValueError("文本片段必须为 1–500 个可输入字符")
    encoded = text.encode("utf-16-le")
    return [int.from_bytes(encoded[i:i + 2], "little") for i in range(0, len(encoded), 2)]


def windows_input(modifiers, key, text):
    class Keyboard(c.Structure):
        _fields_ = [("key", w.WORD), ("scan", w.WORD), ("flags", w.DWORD),
                    ("time", w.DWORD), ("extra", c.c_size_t)]

    class Mouse(c.Structure):
        _fields_ = [("x", w.LONG), ("y", w.LONG), ("data", w.DWORD), ("flags", w.DWORD),
                    ("time", w.DWORD), ("extra", c.c_size_t)]

    class InputData(c.Union):
        _fields_ = [("keyboard", Keyboard), ("mouse", Mouse)]

    class Input(c.Structure):
        _fields_ = [("kind", w.DWORD), ("data", InputData)]

    events = []

    def add(code, up=False, scan=0, unicode=False):
        extended = code in {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2E, 0x5B}
        flags = (2 if up else 0) | (4 if unicode else int(extended))
        events.append(Input(1, InputData(keyboard=Keyboard(code, scan, flags, 0, 0))))

    if text is not None:
        for unit in text_units(text):
            add(0, scan=unit, unicode=True)
            add(0, up=True, scan=unit, unicode=True)
    else:
        codes = [WIN_MOD[modifier] for modifier in modifiers] + [WIN_KEYS[key]]
        for code in codes:
            add(code)
        for code in reversed(codes):
            add(code, up=True)

    user32 = c.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = w.HWND
    if not user32.GetForegroundWindow():
        raise RuntimeError("需要已解锁的交互式桌面")
    send = user32.SendInput
    send.argtypes = [w.UINT, c.POINTER(Input), c.c_int]
    send.restype = w.UINT
    if send(len(events), (Input * len(events))(*events), c.sizeof(Input)) != len(events):
        raise RuntimeError("Windows 未接受桌面输入")


def mac_input(modifiers, key, text):
    quartz = c.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    foundation = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    services = c.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    services.AXIsProcessTrusted.restype = c.c_bool
    if not services.AXIsProcessTrusted():
        raise RuntimeError("请为 Vitals 授予辅助功能权限")
    quartz.CGEventCreateKeyboardEvent.argtypes = [c.c_void_p, c.c_uint16, c.c_bool]
    quartz.CGEventCreateKeyboardEvent.restype = c.c_void_p
    quartz.CGEventSetFlags.argtypes = [c.c_void_p, c.c_uint64]
    quartz.CGEventKeyboardSetUnicodeString.argtypes = [c.c_void_p, c.c_ulong, c.POINTER(c.c_uint16)]
    quartz.CGEventPost.argtypes = [c.c_uint32, c.c_void_p]
    foundation.CFRelease.argtypes = [c.c_void_p]
    units = text_units(text) if text is not None else None
    codes = [units[i:i + 20] for i in range(0, len(units), 20)] if units is not None else [None]
    # Keep non-BMP characters together when dividing long snippets.
    for index, group in enumerate(codes[:-1]):
        if group and 0xD800 <= group[-1] <= 0xDBFF:
            codes[index + 1].insert(0, group.pop())
    def post(code, down, flags, group=None):
        event = quartz.CGEventCreateKeyboardEvent(None, code, down)
        if not event:
            raise RuntimeError("macOS 未接受桌面输入")
        try:
            quartz.CGEventSetFlags(event, flags)
            if group:
                quartz.CGEventKeyboardSetUnicodeString(event, len(group), (c.c_uint16 * len(group))(*group))
            quartz.CGEventPost(0, event)
        finally:
            foundation.CFRelease(event)

    pressed = []
    flags = 0
    try:
        for modifier in modifiers:
            flags |= MAC_MOD[modifier]
            post(MAC_MOD_CODES[modifier], True, flags)
            pressed.append(modifier)
        for group in codes:
            for down in (True, False):
                post(MAC_KEYS[key] if key else 0, down, flags, group)
    finally:
        for modifier in reversed(pressed):
            flags &= ~MAC_MOD[modifier]
            post(MAC_MOD_CODES[modifier], False, flags)


def dispatch_input(platform, *, shortcut=None, preset=None, text=None):
    if sum(item is not None for item in (shortcut, preset, text)) != 1:
        raise ValueError("只能指定一种桌面输入")
    if preset is not None:
        shortcut = PRESETS.get(preset, {}).get(platform)
        if shortcut is None:
            raise ValueError("此平台不支持该桌面预设")
    modifiers, key = parse_hotkey(shortcut) if shortcut is not None else ([], None)
    if text is not None:
        text_units(text)
    send = {"windows": windows_input, "macos": mac_input}.get(platform)
    if send is None:
        raise ValueError("此平台不支持桌面输入")
    with INPUT_LOCK:
        send(modifiers, key, text)
