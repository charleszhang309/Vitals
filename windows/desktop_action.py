"""在 Task Scheduler 提供的交互会话执行一项固定的桌面操作。"""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import sys
from time import time


def perform(action):
    session = wintypes.DWORD()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)) or session.value == 0:
        return False
    if action == "probe":
        return True
    user32 = ctypes.windll.user32
    if action == "lock":
        return bool(user32.LockWorkStation())

    # WM_SYSCOMMAND / SC_MONITORPOWER，lParam=2 只关闭显示器。
    send = user32.SendMessageTimeoutW
    send.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t))
    send.restype = wintypes.LPARAM
    result = ctypes.c_size_t()
    return bool(send(0xFFFF, 0x0112, 0xF170, 2, 0x0002, 200, ctypes.byref(result)))


def main():
    if len(sys.argv) != 3:
        return 2
    action, token = sys.argv[1:]
    if action not in {"lock", "display-off", "probe"} or not re.fullmatch(r"[0-9]{10}-[0-9a-f]{32}", token):
        return 2
    deadline = int(token[:10])
    if not 0 < deadline - time() <= 11:
        return 2
    accepted = perform(action)
    if time() < deadline:
        directory = Path(__file__).resolve().parents[1] / "data" / ".desktop-actions"
        temporary = directory / (token + ".tmp")
        temporary.write_text("accepted" if accepted else "failed", encoding="ascii")
        temporary.replace(directory / (token + ".txt"))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
