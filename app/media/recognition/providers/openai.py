"""使用 Chat Completions 提取信息，并进行明确且有界的兼容性协商。"""

from collections import OrderedDict
import hashlib
import json
from threading import RLock
import time
from urllib.parse import urlsplit, urlunsplit
import requests

from app.media.recognition.contracts import MediaNameRecognizer, ParseResult, RecognizerDescriptor
from app.media.recognition.provider_schema import strict_json_loads
from app.media.recognition.llm_protocol import PROMPT_REVISION, extraction_schema, messages, prompt_version, validate_llm_extraction
from app.media.recognition.llm_usage import attempt_usage, extract_usage


_capabilities, _capability_lock = OrderedDict(), RLock()

# 默认值只用于自动协商；显式设置优先。
SERVICE_DEFAULTS = {
    "compatible": ("json_schema", "max_completion_tokens"),
    "ollama": ("json_schema", "max_tokens"),
    "llama_cpp": ("json_schema", "max_tokens"),
    "deepseek": ("json_object", "max_tokens"),
}


def completion_url(base_url):
    try:
        url = urlsplit(base_url.strip().rstrip("/"))
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment):
            raise ValueError
        url.port
    except (ValueError, AttributeError):
        raise ValueError("invalid_openai_base_url") from None
    path = url.path.rstrip("/")
    if not path:
        path = "/v1"
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urlunsplit((url.scheme, url.netloc, path, "", ""))


class OpenAIRecognizer(MediaNameRecognizer):
    descriptor = RecognizerDescriptor("openai", "OpenAI Compatible", "chat-v4.0-" + PROMPT_REVISION, "llm_model",
        config_schema={name: {"type": kind} for name, kind in (
            ("service_type", "string"), ("extraction_mode", "string"), ("base_url", "string"), ("model", "string"), ("temperature", "number"),
            ("max_output_tokens", "integer"), ("response_format_mode", "string"), ("token_parameter", "string"))},
        network_required=True)

    def __init__(self, base_url="", model="", temperature=0, max_output_tokens=256,
                 response_format_mode="auto", token_parameter="auto", timeout=None,
                 secret_revision="unset", api_key=None, service_type="compatible", extraction_mode="core"):
        self.base_url, self.model, self.timeout = base_url, model, timeout
        self.temperature, self.max_output_tokens = temperature, max_output_tokens
        self.format_mode, self.token_parameter = response_format_mode, token_parameter
        self.secret_revision, self._api_key = secret_revision, api_key
        self.service_type = service_type
        self.extraction_mode = extraction_mode

    @staticmethod
    def _unsupported(response, parameter):
        if response.status_code not in {400, 422}:
            return False
        try:
            error = response.json().get("error", {})
            if isinstance(error, str):
                error = {"message": error}
            if not isinstance(error, dict):
                return False
            text = str(error.get("message", "")).casefold()
            param = error.get("param", "")
            explicit = error.get("code") in {"unsupported_parameter", "unsupported_value"} or any(
                word in text for word in ("not supported", "unsupported", "unrecognized request argument", "unknown parameter"))
            return explicit and (parameter in text or param == parameter or
                parameter == "json_schema" and param == "response_format")
        except (ValueError, AttributeError):
            return False

    def parse(self, request):
        started = time.monotonic()
        attempts, downgrades = [], []
        fmt, token = None, None
        def diagnostics():
            return {"service_type": self.service_type, "model": self.model, "extraction_mode": self.extraction_mode,
                "prompt_version": prompt_version(self.extraction_mode) if self.extraction_mode in {"core", "full"} else None,
                "format": fmt, "token_parameter": token, "downgrades": list(downgrades),
                "usage": attempt_usage(attempts), "attempts": attempts}
        def failure(code, status="invalid"):
            if attempts:
                attempts[-1]["latency_ms"] = int((time.monotonic()-attempt_started)*1000)
            return ParseResult("openai", status, error=code, raw_result=diagnostics(), elapsed_ms=int((time.monotonic()-started)*1000))
        try:
            url = completion_url(self.base_url)
            if not self.model.strip():
                return failure("openai_model_missing")
            if (self.service_type not in SERVICE_DEFAULTS or self.format_mode not in {"auto", "json_schema", "json_object", "prompt"}
                    or self.extraction_mode not in {"core", "full"}
                    or self.token_parameter not in {"auto", "max_tokens", "max_completion_tokens"}):
                return failure("invalid_openai_options")
            if len(request.title) > 16384 or request.subtitle and len(request.subtitle) > 16384:
                return failure("openai_input_too_large")
            key = self._api_key
            if key is None:
                from app.media.recognition.secrets import SecretManager, SecretUnavailable
                try:
                    key = SecretManager().get()
                except SecretUnavailable as error:
                    # 本地凭据故障发生在请求前，直接保留原因，避免误报接口故障。
                    return failure(str(error), "error")
            # 本地兼容端点可以有意不配置凭据。
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = "Bearer " + key
            capability_key = hashlib.sha256(json.dumps([url, self.model, self.service_type, self.format_mode,
                self.token_parameter, self.extraction_mode, prompt_version(self.extraction_mode), self.descriptor.version,
                self.secret_revision, hashlib.sha256((key or "").encode()).hexdigest()]).encode()).hexdigest()
            with _capability_lock:
                learned = _capabilities.get(capability_key, SERVICE_DEFAULTS[self.service_type])
            fmt = learned[0] if self.format_mode == "auto" else self.format_mode
            token = learned[1] if self.token_parameter == "auto" else self.token_parameter
            attempted, token_changed = set(), False
            schema = extraction_schema(self.extraction_mode)
            remaining = request.context.get("remaining_timeout_seconds")
            limits = [x for x in (self.timeout, remaining) if x is not None]
            deadline = started + min(limits) if limits else None
            with requests.Session() as session:
                # 两次格式降级和一次令牌切换后成功。
                for _ in range(4):
                    if (fmt, token) in attempted:
                        return failure("openai_compatibility_exhausted")
                    attempted.add((fmt, token))
                    timeout = None if deadline is None else deadline-time.monotonic()
                    if timeout is not None and timeout <= 0:
                        return failure("openai_deadline_exceeded", "timeout")
                    payload = {"model": self.model, "temperature": self.temperature, token: self.max_output_tokens,
                        "stream": False, "messages": messages(self.extraction_mode, request.title, request.subtitle)}
                    if fmt != "prompt":
                        payload["response_format"] = {"type": fmt}
                        if fmt == "json_schema":
                            if self.service_type == "llama_cpp":
                                payload["response_format"]["schema"] = schema
                            else:
                                payload["response_format"]["json_schema"] = {"name": "media_extraction", "strict": True, "schema": schema}
                    if self.service_type == "deepseek":
                        # 文件名提取要求最终 JSON 控制在较小的
                        # 输出预算内，而不是把预算消耗在思考令牌上。
                        payload["thinking"] = {"type": "disabled"}
                    attempt_started = time.monotonic()
                    attempt = {"attempt_index": len(attempts)+1, "http_status": None, "finish_reason": None,
                        "latency_ms": 0, "usage": extract_usage(None)}
                    attempts.append(attempt)
                    with session.post(url, headers=headers, json=payload, timeout=timeout, stream=True,
                                      allow_redirects=False, verify=True) as response:
                        attempt["http_status"] = response.status_code
                        body = bytearray()
                        for chunk in response.iter_content(65536):
                            if deadline is not None and time.monotonic() >= deadline:
                                return failure("openai_deadline_exceeded", "timeout")
                            body.extend(chunk)
                            if len(body) > 1024*1024:
                                return failure("openai_response_too_large")
                        response._content = bytes(body)
                        attempt["latency_ms"] = int((time.monotonic()-attempt_started)*1000)
                        try:
                            data = response.json()
                        except ValueError:
                            data = None
                        attempt["usage"] = extract_usage(data)
                        choices = data.get("choices") if isinstance(data, dict) else None
                        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                            reason = choices[0].get("finish_reason")
                            if isinstance(reason, str) and reason in {"stop", "length", "content_filter", "tool_calls", "insufficient_system_resource"}:
                                attempt["finish_reason"] = reason
                        if self.format_mode == "auto" and fmt == "json_schema" and self._unsupported(response, "json_schema"):
                            fmt = "json_object"
                            downgrades.append("json_schema_unsupported")
                            continue
                        if (self.format_mode == "auto" and fmt in {"json_schema", "json_object"}
                                and (self._unsupported(response, "response_format") or self._unsupported(response, fmt))):
                            downgrades.append(fmt + "_unsupported")
                            fmt = "json_object" if fmt == "json_schema" else "prompt"
                            continue
                        if self.token_parameter == "auto" and not token_changed and self._unsupported(response, token):
                            token = "max_tokens" if token == "max_completion_tokens" else "max_completion_tokens"
                            token_changed = True
                            downgrades.append("token_parameter_unsupported")
                            continue
                        if response.status_code != 200:
                            return failure(f"openai_http_{response.status_code}", "error")
                        if data is None:
                            return failure("openai_invalid_json")
                        break
                else:
                    return failure("openai_compatibility_exhausted")
            choices = data.get("choices") if isinstance(data, dict) else None
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                return failure("openai_missing_choices")
            choice = choices[0]
            message = choice.get("message")
            if not isinstance(message, dict):
                return failure("openai_missing_message")
            if message.get("refusal") or choice.get("finish_reason") == "content_filter":
                return failure("openai_refusal")
            if choice.get("finish_reason") == "length":
                return failure("openai_truncated")
            if choice.get("finish_reason") != "stop":
                return failure("openai_incomplete")
            content = message.get("content")
            if not isinstance(content, str) or len(content) > 32768:
                return failure("openai_invalid_content")
            try:
                extracted, release, warnings = validate_llm_extraction(strict_json_loads(content), self.extraction_mode)
            except (ValueError, TypeError):
                return failure("openai_invalid_extraction")
            with _capability_lock:
                _capabilities[capability_key] = (fmt, token)
                _capabilities.move_to_end(capability_key)
                while len(_capabilities) > 128:
                    _capabilities.popitem(last=False)
            # 不要返回远端文本、请求头或原始聊天封装。
            return ParseResult("openai", "success", parsed=extracted,
                raw_result={**diagnostics(), "release_metadata": release, "metadata_warnings": warnings},
                elapsed_ms=int((time.monotonic()-started)*1000))
        except requests.Timeout:
            return failure("openai_request_timeout", "timeout")
        except Exception:
            return failure("openai_request_failed", "error")
