"""Durable private state for a single agent instance."""

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile


def state_directory(config):
    scope = sha256(f"{config.server_url}\n{config.device_id}".encode()).hexdigest()[:20]
    path = Path.home() / "Library" / "Application Support" / "Vitals Mac Agent" / scope
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def load_state(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_state(path, state):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        try:
            json.dump(state, temporary, ensure_ascii=False)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.replace(temporary.name, path)
        finally:
            Path(temporary.name).unlink(missing_ok=True)


@contextmanager
def single_instance(directory):
    import fcntl

    with (directory / "agent.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("同一服务器和设备 ID 的 Vitals agent 已在运行") from None
        yield
