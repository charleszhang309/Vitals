"""Bounded calls to the native macOS tools."""

import os
import subprocess

OSASCRIPT = "/usr/bin/osascript"


def native(*args: str, input_text: str | None = None, timeout: float = 3) -> str:
    result = subprocess.run(args, input=input_text, capture_output=True, text=True, encoding="utf-8",
                            timeout=timeout, check=False, env={**os.environ, "LC_ALL": "en_US.UTF-8"})
    if result.returncode:
        raise RuntimeError(f"{args[0].rsplit('/', 1)[-1]}: {result.stderr.strip() or result.returncode}")
    return result.stdout


def apple(script: str, *args: str) -> str:
    return native(OSASCRIPT, "-e", script, *args)


def javascript(script: str, *args: str, timeout: float = 3) -> str:
    return native(OSASCRIPT, "-l", "JavaScript", "-e", script, *args, timeout=timeout)
