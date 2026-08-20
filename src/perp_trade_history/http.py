from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse

import requests

from perp_trade_history.errors import (
    ApiError,
    AuthenticationError,
    CircuitOpenError,
    RateLimitError,
    TransientApiError,
    TransportError,
)

Query = Mapping[str, Any] | Sequence[tuple[str, Any]]


class ReadOnlyHttp:
    """HTTP client whose public surface cannot issue a mutating request."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: int,
        retries: int,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        minimum_interval_seconds: float = 0.0,
        circuit_failure_threshold: int = 1,
        circuit_cooldown_seconds: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.session = session or requests.Session()
        self.sleep = sleep
        self.clock = clock
        self.minimum_interval_seconds = max(minimum_interval_seconds, 0.0)
        self._last_request_at: float | None = None
        self.circuit_failure_threshold = max(circuit_failure_threshold, 1)
        self.circuit_cooldown_seconds = max(circuit_cooldown_seconds, 0.0)
        self._transport_failures = 0
        self._circuit_opened_at: float | None = None

    @property
    def circuit_open(self) -> bool:
        if self._circuit_opened_at is None:
            return False
        if self.clock() - self._circuit_opened_at >= self.circuit_cooldown_seconds:
            self._circuit_opened_at = None
            self._transport_failures = 0
            return False
        return True

    def get_json(
        self,
        path: str,
        *,
        params: Query | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if self.circuit_open:
            raise CircuitOpenError(
                f"GET {path} skipped because the venue transport circuit is open"
            )
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                response = self.session.request(
                    method="GET",
                    url=url,
                    params=params,
                    headers=dict(headers or {}),
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:
                if attempt < self.retries:
                    self.sleep(self._backoff(attempt, None))
                    continue
                self._record_transport_failure()
                raise TransportError(
                    f"GET {path} failed after {attempt + 1} attempts: "
                    f"{_request_failure_description(exc)}"
                ) from exc

            if 200 <= response.status_code < 300:
                self._record_success()
                try:
                    return response.json()
                except ValueError as exc:
                    raise ApiError(
                        f"GET {path} returned invalid JSON",
                        category="malformed_response",
                    ) from exc

            if (
                response.status_code == 429 or response.status_code >= 500
            ) and attempt < self.retries:
                self.sleep(self._backoff(attempt, response.headers.get("Retry-After")))
                continue
            body = response.text[:500].replace("\n", " ")
            response_code: str | int | None = None
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = None
            if isinstance(error_payload, dict):
                response_code = error_payload.get("code")
            error_type: type[ApiError]
            if response.status_code in {401, 403} or str(response_code) in {
                "-2014",
                "-2015",
                "10072",
                "10073",
            }:
                error_type = AuthenticationError
            elif response.status_code == 429 or str(response_code) in {
                "-1003",
                "429",
            }:
                error_type = RateLimitError
            elif response.status_code >= 500:
                error_type = TransientApiError
            else:
                error_type = ApiError
            raise error_type(
                f"GET {path} returned HTTP {response.status_code}: {body}",
                status_code=response.status_code,
                response_code=response_code,
            )
        raise AssertionError("unreachable")

    def get_bytes_url(self, url: str) -> bytes:
        """Download an exchange-issued HTTPS artifact through GET only."""
        self._validate_https_download_url(url)
        if self.circuit_open:
            raise CircuitOpenError(
                "GET archive skipped because the venue transport circuit is open"
            )
        for attempt in range(self.retries + 1):
            try:
                response = self._get_download_response(url)
            except requests.RequestException as exc:
                if attempt < self.retries:
                    self.sleep(self._backoff(attempt, None))
                    continue
                self._record_transport_failure()
                raise TransportError(
                    f"GET archive failed after {attempt + 1} attempts: "
                    f"{_request_failure_description(exc)}"
                ) from exc

            if 200 <= response.status_code < 300:
                self._record_success()
                return bytes(response.content)
            if (
                response.status_code == 429 or response.status_code >= 500
            ) and attempt < self.retries:
                self.sleep(self._backoff(attempt, response.headers.get("Retry-After")))
                continue
            if response.status_code == 429:
                raise RateLimitError(
                    f"GET archive returned HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            if response.status_code >= 500:
                raise TransientApiError(
                    f"GET archive returned HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            raise ApiError(
                f"GET archive returned HTTP {response.status_code}",
                status_code=response.status_code,
            )
        raise AssertionError("unreachable")

    def _get_download_response(self, url: str) -> requests.Response:
        current_url = url
        for _ in range(6):
            self._validate_https_download_url(current_url)
            self._throttle()
            response = self.session.request(
                method="GET",
                url=current_url,
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("Location")
            if not location:
                raise ApiError("archive download redirect omitted its destination")
            current_url = urljoin(current_url, location)
        raise ApiError("archive download exceeded the redirect limit")

    @staticmethod
    def _validate_https_download_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ApiError("download URL must be an absolute HTTPS URL")

    def _throttle(self) -> None:
        now = self.clock()
        if self._last_request_at is not None:
            remaining = self.minimum_interval_seconds - (now - self._last_request_at)
            if remaining > 0:
                self.sleep(remaining)
                now = self.clock()
        self._last_request_at = now

    def _record_transport_failure(self) -> None:
        self._transport_failures += 1
        if self._transport_failures >= self.circuit_failure_threshold:
            self._circuit_opened_at = self.clock()

    def _record_success(self) -> None:
        self._transport_failures = 0
        self._circuit_opened_at = None

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), 60.0)
            except ValueError:
                pass
        return min(0.5 * (2**attempt), 30.0)


def binance_signed_query(
    params: Mapping[str, Any], *, api_secret: str, timestamp_ms: int
) -> list[tuple[str, str]]:
    normalized = [
        (str(key), _stringify(value))
        for key, value in params.items()
        if value is not None
    ]
    normalized.extend([("recvWindow", "10000"), ("timestamp", str(timestamp_ms))])
    query = urlencode(normalized, doseq=True)
    signature = hmac.new(
        api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    normalized.append(("signature", signature))
    return normalized


def gate_headers(
    *,
    method: str,
    api_path: str,
    query: Sequence[tuple[str, Any]],
    api_key: str,
    api_secret: str,
    timestamp_seconds: int,
) -> dict[str, str]:
    if method.upper() != "GET":
        raise ValueError("trade history clients only sign GET requests")
    query_text = urlencode([(key, _stringify(value)) for key, value in query], doseq=True)
    body_hash = hashlib.sha512(b"").hexdigest()
    sign_text = f"GET\n{api_path}\n{query_text}\n{body_hash}\n{timestamp_seconds}"
    signature = hmac.new(
        api_secret.encode("utf-8"), sign_text.encode("utf-8"), hashlib.sha512
    ).hexdigest()
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "KEY": api_key,
        "Timestamp": str(timestamp_seconds),
        "SIGN": signature,
    }


def mexc_headers(
    *,
    params: Mapping[str, Any],
    api_key: str,
    api_secret: str,
    timestamp_ms: int,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    normalized = sorted(
        (
            str(key),
            quote(_stringify(value), safe=""),
        )
        for key, value in params.items()
        if value is not None
    )
    query = "&".join(f"{key}={value}" for key, value in normalized)
    target = f"{api_key}{timestamp_ms}{query}"
    signature = hmac.new(
        api_secret.encode("utf-8"), target.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    headers = {
        "ApiKey": api_key,
        "Request-Time": str(timestamp_ms),
        "Signature": signature,
        "Recv-Window": "10000",
    }
    # Values are already percent encoded for signing. Passing the original values to
    # requests produces the same RFC 3986 query representation for supported inputs.
    request_params = [
        (str(key), _stringify(value))
        for key, value in sorted(params.items())
        if value is not None
    ]
    return headers, request_params


def ensure_success_payload(payload: Any, *, venue: str, source: str) -> Any:
    if not isinstance(payload, dict):
        return payload
    success = payload.get("success")
    code = payload.get("code")
    message = payload.get("message", payload.get("msg", ""))
    if success is False or (code not in (None, 0, "0", 200, "200") and message):
        safe_message = str(message)[:300].replace("\n", " ")
        code_text = str(code)
        if (venue == "mexc" and code_text == "513") or code_text == "-1021":
            from perp_trade_history.errors import ClockSkewError

            raise ClockSkewError(
                f"{venue} {source} returned code {code}: {safe_message}",
                response_code=code,
            )
        if code_text in {"401", "403", "10072", "10073"}:
            raise AuthenticationError(
                f"{venue} {source} returned code {code}: {safe_message}",
                response_code=code,
            )
        if code_text in {"429", "510"}:
            raise RateLimitError(
                f"{venue} {source} returned code {code}: {safe_message}",
                response_code=code,
            )
        raise ApiError(
            f"{venue} {source} returned code {code}: {safe_message}",
            response_code=code,
        )
    return payload


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    return str(value)


def _request_failure_description(exc: requests.RequestException) -> str:
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS handshake or certificate validation failed"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "connection timed out"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "response timed out"
    if isinstance(exc, requests.exceptions.Timeout):
        return "request timed out"
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return "redirect limit was exceeded"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection failed"
    return "network request failed"
