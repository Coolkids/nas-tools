"""自动发现并管理 providers 包中的媒体名称识别器。"""

import importlib
import pkgutil
import re
from inspect import isabstract
from threading import RLock

from app.media.recognition.contracts import MediaNameRecognizer, RecognizerDescriptor


class RecognizerRegistry:
    """负责发现、创建和释放识别器，同时收集加载诊断信息。"""

    def __init__(self, package="app.media.recognition.providers"):
        self._package = package
        self._lock = RLock()
        self._classes = None
        self._errors = []

    @staticmethod
    def _validate_descriptor(candidate):
        descriptor = getattr(candidate, "descriptor", None)
        if not isinstance(descriptor, RecognizerDescriptor):
            raise ValueError(f"{candidate.__name__} descriptor 必须是 RecognizerDescriptor")
        if not isinstance(descriptor.provider_id, str) or not re.fullmatch(
                r"[a-z][a-z0-9_-]{0,63}", descriptor.provider_id):
            raise ValueError(f"{candidate.__name__} provider_id 格式无效")
        if not isinstance(descriptor.display_name, str) or not descriptor.display_name.strip():
            raise ValueError(f"{candidate.__name__} display_name 不能为空")
        if not isinstance(descriptor.version, str) or not descriptor.version.strip():
            raise ValueError(f"{candidate.__name__} version 不能为空")
        if not isinstance(descriptor.evidence_family, str) or not descriptor.evidence_family.strip():
            raise ValueError(f"{candidate.__name__} evidence_family 不能为空")
        if not isinstance(descriptor.config_schema, dict):
            raise ValueError(f"{candidate.__name__} config_schema 必须是对象")
        for field_name, field_schema in descriptor.config_schema.items():
            if not isinstance(field_name, str) or not field_name.strip():
                raise ValueError(f"{candidate.__name__} 配置字段名不能为空")
            if not isinstance(field_schema, dict) or not isinstance(field_schema.get("type"), str):
                raise ValueError(f"{candidate.__name__} 配置字段 {field_name} 缺少有效 type")
        return descriptor

    def discover(self, refresh=False):
        with self._lock:
            if self._classes is not None and not refresh:
                return dict(self._classes)
            classes = {}
            conflicted_ids = set()
            errors = []
            try:
                package = importlib.import_module(self._package)
                for module_info in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
                    try:
                        module = importlib.import_module(module_info.name)
                        for candidate in vars(module).values():
                            if not isinstance(candidate, type) or candidate is MediaNameRecognizer:
                                continue
                            if not issubclass(candidate, MediaNameRecognizer) or isabstract(candidate):
                                continue
                            if candidate.__module__ != module.__name__:
                                continue
                            descriptor = self._validate_descriptor(candidate)
                            provider_id = descriptor.provider_id
                            if provider_id in conflicted_ids:
                                errors.append({"module": module_info.name,
                                               "provider": candidate.__name__,
                                               "error": f"识别方式 ID 已存在冲突：{provider_id}"})
                                continue
                            if provider_id in classes:
                                classes.pop(provider_id, None)
                                conflicted_ids.add(provider_id)
                                errors.append({"module": module_info.name,
                                               "provider": candidate.__name__,
                                               "error": f"重复的识别方式 ID，已隔离所有冲突实现：{provider_id}"})
                                continue
                            classes[provider_id] = candidate
                    except Exception as error:
                        errors.append({"module": module_info.name, "error": str(error)})
            except Exception as error:
                errors.append({"module": self._package, "error": str(error)})
            self._classes = classes
            self._errors = errors
            return dict(classes)

    def create(self, provider_id, **kwargs):
        recognizer = self.discover().get(provider_id)
        return recognizer(**kwargs) if recognizer else None

    def release(self, recognizer):
        """释放单次请求使用的识别器，并记录资源清理失败。"""
        if recognizer is None:
            return
        try:
            recognizer.close()
        except Exception as error:
            with self._lock:
                self._errors.append({
                    "provider": getattr(getattr(recognizer, "descriptor", None), "provider_id", None),
                    "error": f"关闭识别方式失败：{error}",
                })

    def diagnostics(self):
        self.discover()
        return list(self._errors)


registry = RecognizerRegistry()
