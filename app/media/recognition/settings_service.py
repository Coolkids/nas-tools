"""通过跨进程配置文件锁更新带版本的设置。"""

from contextlib import contextmanager
from copy import deepcopy
import os

from ruamel.yaml import YAML

from config import Config, CONFIG_LOCK
from app.media.recognition.configuration import (
    migrate_recognition_v3, prepare_recognition_settings_update,
    recognition_settings_version, recognition_cache_capabilities,
    recognition_v3_defaults,
)
from app.utils.cache_backend import STORE


@contextmanager
def _configuration_lock(config):
    try:
        import fcntl
    except ImportError:  # Windows 使用单字节排他文件锁。
        fcntl = None
        import msvcrt
    with CONFIG_LOCK:
        descriptor = os.open(config._config_path + ".recognition.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            else:
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            else:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            os.close(descriptor)


def _read(config):
    with open(config._config_path, encoding="utf-8") as stream:
        value = YAML(typ="safe").load(stream)
    value = migrate_recognition_v3(value)
    from app.media.recognition.secrets import SecretManager
    value["recognition"]["providers"]["openai"].update(SecretManager().metadata())
    return value


def _public(value):
    from app.media.recognition.trace import redact
    from app.media.recognition.refresh_worker import RefreshWorker
    recognition = redact(deepcopy(value["recognition"]))
    for provider, options in recognition.get("providers", {}).items():
        if isinstance(options, dict):
            raw = value["recognition"]["providers"][provider]
            options["api_key_set"] = bool(raw.get("api_key_set"))
            for field in ("api_key", "password", "authorization", "token", "secret"):
                options.pop(field, None)
    # 不返回凭据明文、连接信息或迁移设置。
    return {"recognition": recognition, "cache": {"ttl_seconds": deepcopy(value["cache"]["ttl_seconds"])},
            "defaults": recognition_v3_defaults(),
            "version": recognition_settings_version(value),
            "capabilities": RefreshWorker(value, store=STORE).capabilities()}


def get_settings():
    config = Config()
    with _configuration_lock(config):
        return _public(_read(config))


def save_settings(patch, expected_version):
    config = Config()
    if not isinstance(patch, dict):
        raise ValueError("settings patch must be an object")
    patch = deepcopy(patch)
    recognition_patch = patch.get("recognition", {})
    if not isinstance(recognition_patch, dict) or not isinstance(recognition_patch.get("providers", {}), dict):
        raise ValueError("invalid recognition provider settings")
    for options in recognition_patch.get("providers", {}).values():
        if isinstance(options, dict) and set(options) & {"secret_revision", "api_key_set", "clear_api_key",
                                                      "credential_status", "credential_message"}:
            raise ValueError("credential control fields require credential endpoint")
        if isinstance(options, dict) and options.get("api_key") in ("", None):
            options.pop("api_key", None)
    with _configuration_lock(config):
        value = prepare_recognition_settings_update(_read(config), patch, expected_version)
        config.save_config(value)
        from app.media.recognition.refresh_worker import cancel_refreshes
        cancel_refreshes()
        if "cleanup" in (recognition_patch.get("records") or {}) or "cache_v3" in recognition_patch:
            from app.scheduler import Scheduler, restart_scheduler
            scheduler = Scheduler().SCHEDULER
            if scheduler is not None and scheduler.running:
                restart_scheduler()
        return _public(value)


def save_openai_secret(value, clear, expected_version):
    from app.media.recognition.secrets import SecretManager
    from app.media.recognition.configuration import recognition_settings_version, RecognitionConfigurationConflict
    if type(clear) is not bool or value is not None and not isinstance(value, str):
        raise ValueError("invalid_credential_update")
    if clear and value and value.strip():
        raise ValueError("conflicting_credential_update")
    config = Config()
    with _configuration_lock(config):
        current = _read(config)
        if expected_version != recognition_settings_version(current):
            raise RecognitionConfigurationConflict("识别配置已变化，请重新读取后保存")
        manager = SecretManager()
        if clear:
            manager.clear()
        elif value and value.strip():
            manager.set(value)
        return _public(_read(config))


def test_openai_connection(options=None, api_key=None, expected_version=None):
    import time
    from app.media.recognition.runtime import WorkItem, get_runtime
    from app.media.recognition.secrets import SecretManager
    from app.media.recognition.configuration import RecognitionConfigurationError
    with _configuration_lock(Config()):
        settings = _read(Config())
        version = recognition_settings_version(settings)
        allowed = {"service_type", "extraction_mode", "base_url", "model", "temperature", "max_output_tokens", "response_format_mode", "token_parameter"}
        if options is not None and (not isinstance(options, dict) or set(options)-allowed):
            raise ValueError("invalid_openai_probe_options")
        if api_key is not None and (not isinstance(api_key, str) or len(api_key) > 8192 or any(c in api_key for c in ("\r", "\n"))):
            raise ValueError("invalid_probe_credential")
        tested = prepare_recognition_settings_update(settings, {"recognition": {"providers": {
            "openai": {**(options or {}), "enabled": True}}}}, expected_version if expected_version is not None else version)
        configured = tested["recognition"]["providers"]["openai"]
        provider_options = {key: configured[key] for key in allowed}
        provider_options.update(timeout=15, api_key=(api_key.strip() if api_key and api_key.strip() else SecretManager().get()))
    # 不会暴露解析缓存、轨迹、历史记录或已保存凭据；工作边界
    # 可在识别没有时限时终止无响应的探测。
    work = get_runtime().submit("parser", WorkItem("parse", {"provider_id": "openai", "title": "Example (2020).mkv",
        "options": provider_options, "context": {"remaining_timeout_seconds": 15}}), deadline=time.monotonic()+15).result()
    parsed = work.value if work.status == "success" else None
    raw = parsed.raw_result if parsed is not None and isinstance(parsed.raw_result, dict) else {}
    return {"status": parsed.status if parsed is not None else work.status,
            "error": parsed.error if parsed is not None else work.reason,
            "duration_ms": parsed.elapsed_ms if parsed is not None else None,
            "capabilities": {key: raw[key] for key in ("service_type", "extraction_mode", "prompt_version", "format", "token_parameter", "downgrades") if key in raw},
            "usage": raw.get("usage"), "attempts": raw.get("attempts", [])}
