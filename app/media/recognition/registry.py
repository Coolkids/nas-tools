"""Discover media-name recognizers placed in the providers package."""

import importlib
import pkgutil
from inspect import isabstract
from threading import RLock

from app.media.recognition.contracts import MediaNameRecognizer


class RecognizerRegistry:
    def __init__(self, package="app.media.recognition.providers"):
        self._package = package
        self._lock = RLock()
        self._classes = None
        self._errors = []

    def discover(self, refresh=False):
        with self._lock:
            if self._classes is not None and not refresh:
                return dict(self._classes)
            classes = {}
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
                            descriptor = getattr(candidate, "descriptor", None)
                            provider_id = getattr(descriptor, "provider_id", None)
                            if not provider_id:
                                raise ValueError(f"{candidate.__name__} 缺少 provider_id")
                            if provider_id in classes:
                                raise ValueError(f"重复的识别方式 ID：{provider_id}")
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

    def diagnostics(self):
        self.discover()
        return list(self._errors)


registry = RecognizerRegistry()

