"""封装 anitopy-ml 单标题解析接口的 HTTP 调用。"""

import time
import requests
from requests.exceptions import Timeout

from app.media.recognition.contracts import (
    MediaNameRecognizer,
    ParseResult,
    RecognitionRequest,
    RecognizerDescriptor,
)
from app.utils import RequestUtils
from config import Config


class AnitopyMlRecognizer(MediaNameRecognizer):
    descriptor = RecognizerDescriptor(
        provider_id="anitopy_ml",
        display_name="anitopy-ml",
        evidence_family="ml_model",
        config_schema={"endpoint": {"type": "string"}},
        network_required=True,
    )

    def __init__(self, endpoint=None, timeout=10, strict=False, model_revision="unknown"):
        self.endpoint = endpoint if strict else endpoint or Config().get_config("laboratory").get("ai_inference_url")
        self.timeout = timeout
        self.strict, self.model_revision = strict, model_revision

    def parse(self, request: RecognitionRequest) -> ParseResult:
        started = time.monotonic()
        if not request.title or not self.endpoint:
            return ParseResult(self.descriptor.provider_id, "skipped", error="disabled_or_empty_title")
        url = str(self.endpoint).strip().rstrip("/")
        if not url:
            return ParseResult(self.descriptor.provider_id, "skipped", error="empty_endpoint")
        if not url.endswith("/v1/parse"):
            url = f"{url}/v1/parse"
        try:
            timeout = self.timeout
            remaining = request.context.get("remaining_timeout_seconds")
            if remaining is not None:
                timeout = min(float(timeout), float(remaining)) if timeout is not None else float(remaining)
            if timeout is not None and timeout <= 0:
                return ParseResult(self.descriptor.provider_id, "timeout",
                                   error="recognition_deadline_exceeded")
            if self.strict:
                with requests.Session() as session, session.post(url, json={"title": request.title},
                        timeout=timeout, verify=True, stream=True, allow_redirects=False) as response:
                    body = bytearray()
                    for chunk in response.iter_content(65536):
                        body.extend(chunk)
                        if len(body) > 1024*1024:
                            return ParseResult(self.descriptor.provider_id, "invalid", error="ml_response_too_large")
                    response._content = bytes(body)
            else:
                response = RequestUtils(
                    timeout=timeout,
                    allow_unlimited_timeout=timeout is None,
                    headers={"Content-Type": "application/json", "User-Agent": Config().get_ua()},
                ).post_res(url=url, json={"title": request.title})
            if response is None or response.status_code != 200:
                status = response.status_code if response is not None else "no_response"
                return ParseResult(
                    self.descriptor.provider_id, "error", raw_result={"status": status},
                    error=f"http_{status}", elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            payload = response.json()
            extracted = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(extracted, dict):
                return ParseResult(
                    self.descriptor.provider_id, "invalid", raw_result=payload,
                    error="invalid_response_schema",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            if self.strict:
                from app.media.recognition.provider_schema import ml_extraction
                try:
                    extracted = ml_extraction(extracted)
                except (ValueError, TypeError):
                    return ParseResult(self.descriptor.provider_id, "invalid", error="invalid_ml_extraction")
            return ParseResult(
                self.descriptor.provider_id, "success", parsed=extracted,
                raw_result=extracted if self.strict else payload, elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except (Timeout, TimeoutError) as error:
            return ParseResult(
                self.descriptor.provider_id, "timeout", error="request_timeout",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as error:
            return ParseResult(
                self.descriptor.provider_id, "error", error="ml_request_failed",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
