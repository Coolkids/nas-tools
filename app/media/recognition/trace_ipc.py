"""用于临时轨迹的私有有界本地 IPC。磁盘上只保存控制信息。"""

import atexit
from collections import OrderedDict
from copy import deepcopy
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
from threading import RLock, Thread
import time


class TraceHub:
    def __init__(self, root, control=None, max_traces=128):
        self.control = control
        self.max_traces = max_traces
        self.owner_pid = os.getpid()
        directory = Path(root) / ".maintenance" / "recognition-ipc"
        if directory.is_symlink():
            raise ValueError("invalid IPC directory")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory, self.key = directory, secrets.token_hex(32)
        self.path = directory / f"{os.getpid()}-{secrets.token_hex(4)}.sock"
        self.manifest = self.path.with_suffix(".json")
        self._traces, self._lock, self._closed = OrderedDict(), RLock(), False
        # 项目内的测试路径可能超过 Unix sockaddr 约 108 字节的限制。
        # 使用回环 socket 和权限为 0600 的控制文件保存 256 位密钥，不受
        # 路径长度影响，也不会持久化轨迹正文。
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(8)
        self.socket.settimeout(0.2)
        descriptor = os.open(self.manifest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            from app.media.recognition.admission import process_start
            json.dump({"pid": os.getpid(), "process_start": process_start(os.getpid()),
                       "host": "127.0.0.1", "port": self.socket.getsockname()[1], "key": self.key}, stream)
        self.thread = Thread(target=self._serve, daemon=True, name="recognition-trace-ipc")
        self.thread.start()
        atexit.register(self.close)

    def register(self, trace):
        with self._lock:
            for key, (value, expires) in list(self._traces.items()):
                if expires and expires <= time.monotonic():
                    del self._traces[key]
            while len(self._traces) >= self.max_traces:
                completed = next((key for key, (_trace, expires) in self._traces.items() if expires is not None), None)
                if completed is None:
                    break
                del self._traces[completed]
            if len(self._traces) >= self.max_traces:
                raise RuntimeError("volatile trace quota reached")
            self._traces[trace.request_id] = (trace, None)

    def release(self, trace):
        with self._lock:
            if trace.request_id in self._traces:
                self._traces[trace.request_id] = (trace, time.monotonic()+60)

    def _serve(self):
        while not self._closed:
            try:
                connection, _address = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(0.3)
                try:
                    data = b""
                    while b"\n" not in data and len(data) <= 2048:
                        chunk = connection.recv(2048)
                        if not chunk:
                            break
                        data += chunk
                    request = json.loads(data)
                    if not isinstance(request, dict):
                        continue
                    if not hmac.compare_digest(str(request.get("key", "")), self.key):
                        continue
                    action = request.get("action", "snapshot")
                    if action != "snapshot":
                        result = self.control(action, request.get("request_id")) if self.control is not None else None
                        connection.sendall(json.dumps(result).encode())
                        continue
                    with self._lock:
                        item = self._traces.get(request.get("request_id"))
                        trace = item[0] if item and (item[1] is None or item[1] > time.monotonic()) else None
                    if trace is None:
                        result = None
                    else:
                        with trace._mutex:
                            result = {"request_id": trace.request_id, "status": trace.status,
                                "durability": "volatile" if trace.mode == "none" else "persistent" if trace.persisted else "temporary",
                                "retention": trace.mode, "input": trace.input, "last_seq": trace._seq,
                                "nodes": deepcopy(trace.node_states),
                                "started_at": trace.started_at, "finished_at": trace.finished_at,
                                "source": trace.source, "candidates": deepcopy(trace.candidates[:200]),
                                "candidates_truncated": len(trace.candidates) > 200,
                                "outcome": trace.outcome_snapshot(), "events": deepcopy(trace.events)}
                    connection.sendall(json.dumps(result, ensure_ascii=False).encode())
                except (OSError, ValueError, TypeError):
                    continue

    def close(self):
        if self.owner_pid != os.getpid():
            return
        if self._closed:
            return
        self._closed = True
        self.socket.close()
        self.thread.join(1)
        self.manifest.unlink(missing_ok=True)
        self.path.unlink(missing_ok=True)


_hub, _lock = None, RLock()


def _after_fork():
    global _hub, _lock
    if _hub is not None:
        _hub.socket.close()
    _hub, _lock = None, RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def get_hub():
    global _hub
    with _lock:
        if _hub is None:
            from config import Config
            _hub = TraceHub(Config().get_config_path())
        return _hub


def get_active_trace(request_id, root=None):
    return owner_command("snapshot", request_id, root=root)


def owner_command(action, request_id, root=None):
    """仅供内部使用；HTTP 调用方必须先检查请求归属。"""
    if root is None:
        from config import Config
        root = Config().get_config_path()
    directory = Path(root) / ".maintenance" / "recognition-ipc"
    probed = 0
    for path in directory.glob("*.json"):
        try:
            if path.is_symlink() or path.stat().st_size > 2048:
                continue
            metadata = json.loads(path.read_text())
            if not isinstance(metadata, dict):
                continue
            from app.media.recognition.admission import process_alive
            if not process_alive(metadata.get("pid"), metadata.get("process_start")):
                path.unlink(missing_ok=True)
                continue
            if metadata.get("host") != "127.0.0.1" or type(metadata.get("port")) is not int or not 0 < metadata["port"] < 65536:
                continue
            probed += 1
            if probed > 64:
                break
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.settimeout(0.5)
                client.connect(("127.0.0.1", metadata["port"]))
                client.sendall(json.dumps({"key": metadata["key"], "action": action, "request_id": request_id}).encode()+b"\n")
                chunks, size = [], 0
                while True:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 2*1024*1024:
                        raise ValueError("IPC response too large")
                    chunks.append(chunk)
            result = json.loads(b"".join(chunks))
            if result is not None:
                return result
        except (OSError, KeyError, ValueError):
            continue
    return None
