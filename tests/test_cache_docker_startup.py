"""在临时目录执行容器入口，验证首次启动和密码配置。"""

import os
import socket
import subprocess
from pathlib import Path

import pytest
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry


@pytest.mark.parametrize("password", ["", 'spaces \\ quotes " semicolon; $literal'])
def test_entrypoint_creates_missing_cache_directory_and_valid_valkey_configuration(tmp_path, password):
    root = Path(__file__).resolve().parents[1]
    directory = tmp_path / "config"
    configuration = tmp_path / "valkey.conf"
    command_dir = tmp_path / "bin"
    command_dir.mkdir()
    supervisor = command_dir / "supervisord"
    supervisor.write_text("#!/bin/sh\nexit 0\n")
    supervisor.chmod(0o755)
    # 替换固定容器路径，使测试不接触真实 /config 和 supervisor。
    script = (root / "docker/entrypoint.sh").read_text().replace("/config", str(directory))
    script = script.replace("/tmp/nastool-valkey.conf", str(configuration))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    script = script.replace("port 6379", f"port {port}")
    environment = {**os.environ, "WORKDIR": str(tmp_path), "VALKEY_PASSWORD": password,
                   "PATH": str(command_dir) + os.pathsep + os.environ["PATH"]}
    assert not directory.exists()
    result = subprocess.run(["/bin/sh"], input=script, text=True, env=environment, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert (directory / "cache/valkey").is_dir()
    assert configuration.stat().st_mode & 0o777 == 0o600
    # 同样的入口第二次执行不破坏持久化文件。
    existing = directory / "cache/valkey/keep"
    existing.write_text("existing-data")
    subprocess.run(["/bin/sh"], input=script, text=True, env=environment, check=True, timeout=10)
    assert existing.read_text() == "existing-data"
    binary = os.environ.get("NASTOOL_TEST_VALKEY_SERVER")
    if binary:
        process = subprocess.Popen([binary, str(configuration)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        client = redis.Redis(host="127.0.0.1", port=port, password=password or None,
                             socket_timeout=0.2, socket_connect_timeout=0.2, retry=Retry(NoBackoff(), 0))
        try:
            import time
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    assert client.ping()
                    client.set("startup-check", "ok")
                    assert client.get("startup-check") == b"ok"
                    break
                except redis.ConnectionError:
                    if process.poll() is not None:
                        pytest.fail(process.stderr.read().decode())
                    time.sleep(0.02)
            else:
                pytest.fail("Valkey 启动超时")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            process.stderr.close()
            client.connection_pool.disconnect()


def test_cache_configuration_validation_does_not_save_invalid_values():
    from unittest.mock import Mock, patch
    from copy import deepcopy
    from config import Config
    from web.action import WebAction

    configuration = Mock()
    configuration.get_config.return_value = deepcopy(Config().get_config())
    action = object.__new__(WebAction)
    with patch("web.action.Config", return_value=configuration):
        for settings in ({"cache.backend": "unknown"}, {"cache.port": 1.5}, {"cache.port": 65536},
                         {"cache.db": -1}, {"cache.auth_mode": "unknown"},
                         {"cache.connect_timeout": float("nan")}, {"cache.retry_interval": 0}):
            result = action._WebAction__update_config(settings)
            assert result["code"] == 1
        configuration.save_config.assert_not_called()
        result = action._WebAction__update_config({"cache.backend": "valkey", "cache.port": "6380",
                                                   "cache.db": "2", "cache.auth_mode": "password",
                                                   "cache.password": "test-password", "cache.connect_timeout": "0.5"})
        assert result["code"] == 0
        saved = configuration.save_config.call_args.args[0]["cache"]
        assert saved["port"] == 6380
        assert saved["db"] == 2
        assert saved["connect_timeout"] == 0.5
        assert saved["password"] == "test-password"
