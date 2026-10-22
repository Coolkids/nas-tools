"""维护期间用于本地跨进程识别准入，不包含请求正文。"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
import uuid


def root_path(root=None):
    if root is None:
        from config import Config
        root = Config().get_config_path()
    return Path(root)


def process_start(pid):
    try:
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def process_alive(pid, start=None):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return start is None or process_start(pid) == start


@contextmanager
def _lock(root):
    directory = root_path(root)/".maintenance"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(directory/"recognition-admission.lock", "a+b") as stream:
        if os.name == "nt":  # pragma: no cover - Windows 平台
            import msvcrt
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield directory/"recognition-admission.json"
        finally:
            if os.name == "nt":  # pragma: no cover
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def upgrade_gate(root=None):
    upgrade = root_path(root)/".maintenance"/"recognition-upgrade-gate.json"
    try:
        value = json.loads(upgrade.read_text())
        if not isinstance(value, dict) or value.get("reason") != "recognition_upgrade":
            return {"reason": "invalid_upgrade_gate"}
        return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"reason": "invalid_upgrade_gate"}


def gate(root=None):
    upgrade = upgrade_gate(root)
    if upgrade:
        return upgrade
    try:
        value = json.loads((root_path(root)/".maintenance"/"recognition-admission.json").read_text())
        if not isinstance(value, dict) or type(value.get("pid")) is not int or not isinstance(value.get("token"), str):
            return {"reason": "invalid_maintenance_gate"}
        return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"reason": "invalid_maintenance_gate"}  # 失败时拒绝放行


def pause(root=None):
    with _lock(root) as path:
        if upgrade_gate(root):
            raise RuntimeError("recognition schema upgrade must finish before maintenance")
        current = gate(root)
        if current and ("pid" not in current or process_alive(current["pid"], current.get("process_start"))):
            raise RuntimeError("recognition maintenance already active")
        token = uuid.uuid4().hex
        descriptor = os.open(str(path)+".tmp", os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"token": token, "pid": os.getpid(), "process_start": process_start(os.getpid()),
                       "created_at": time.time(), "reason": "storage_maintenance"}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(path)+".tmp", path)
        return token


def resume(token, root=None):
    with _lock(root) as path:
        if (gate(root) or {}).get("token") == token:
            path.unlink(missing_ok=True)


def recover_gate(root=None):
    with _lock(root) as path:
        current = gate(root)
        if current and "pid" in current and not process_alive(current["pid"], current.get("process_start")):
            path.unlink(missing_ok=True)
