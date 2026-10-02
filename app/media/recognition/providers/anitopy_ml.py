"""封装 anitopy-ml 单标题解析接口的 HTTP 调用。"""

import time
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
    )

    def __init__(self, endpoint=None, timeout=10):
        self.endpoint = endpoint or Config().get_config("laboratory").get("ai_inference_url")
        self.timeout = timeout

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
            response = RequestUtils(
                timeout=self.timeout,
                headers={"Content-Type": "application/json", "User-Agent": Config().get_ua()},
            ).post_res(url=url, json={"title": request.title})
            if not response or response.status_code != 200:
                status = response.status_code if response else "no_response"
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
            return ParseResult(
                self.descriptor.provider_id, "success", parsed=extracted,
                raw_result=payload, elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except (Timeout, TimeoutError) as error:
            return ParseResult(
                self.descriptor.provider_id, "timeout", error=str(error) or "request_timeout",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as error:
            return ParseResult(
                self.descriptor.provider_id, "error", error=str(error),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
