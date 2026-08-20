import hashlib
import hmac
from urllib.parse import urlencode

import pytest
import requests

from perp_trade_history.errors import ApiError
from perp_trade_history.http import (
    ReadOnlyHttp,
    binance_signed_query,
    gate_headers,
    mexc_headers,
)


def test_binance_signature_covers_exact_query_parameters() -> None:
    signed = binance_signed_query(
        {"symbol": "ASSETQUOTE", "limit": 100},
        api_secret="secret",
        timestamp_ms=1_700_000_000_000,
    )
    unsigned = signed[:-1]
    expected = hmac.new(
        b"secret", urlencode(unsigned).encode(), hashlib.sha256
    ).hexdigest()
    assert signed[-1] == ("signature", expected)


def test_gate_signature_uses_read_only_method_and_canonical_body_hash() -> None:
    headers = gate_headers(
        method="GET",
        api_path="/api/v4/futures/usdt/account_book",
        query=[("limit", 100)],
        api_key="key",
        api_secret="secret",
        timestamp_seconds=1_700_000_000,
    )
    body_hash = hashlib.sha512(b"").hexdigest()
    text = f"GET\n/api/v4/futures/usdt/account_book\nlimit=100\n{body_hash}\n1700000000"
    expected = hmac.new(b"secret", text.encode(), hashlib.sha512).hexdigest()
    assert headers["SIGN"] == expected


def test_mexc_signature_matches_contract_api_scheme() -> None:
    headers, params = mexc_headers(
        params={"page_size": 100, "symbol": "ASSET_QUOTE"},
        api_key="key",
        api_secret="secret",
        timestamp_ms=1_700_000_000_000,
    )
    target = "key1700000000000page_size=100&symbol=ASSET_QUOTE"
    expected = hmac.new(b"secret", target.encode(), hashlib.sha256).hexdigest()
    assert headers["Signature"] == expected
    assert params == [("page_size", "100"), ("symbol", "ASSET_QUOTE")]


class _Response:
    status_code = 200
    headers: dict[str, str] = {}
    text = ""
    content = b"archive"

    @staticmethod
    def json() -> dict[str, bool]:
        return {"ok": True}


class _Session:
    def __init__(self) -> None:
        self.methods: list[str] = []

    def request(self, *, method: str, **kwargs):
        self.methods.append(method)
        return _Response()


def test_read_only_http_throttles_consecutive_get_requests() -> None:
    now = [0.0]
    sleeps: list[float] = []
    session = _Session()

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    client = ReadOnlyHttp(
        "https://api.example",
        timeout_seconds=1,
        retries=0,
        session=session,  # type: ignore[arg-type]
        sleep=sleep,
        clock=lambda: now[0],
        minimum_interval_seconds=0.5,
    )
    client.get_json("/one")
    client.get_json("/two")
    assert sleeps == [0.5]
    assert session.methods == ["GET", "GET"]


def test_archive_download_rejects_non_https_urls_before_request() -> None:
    session = _Session()
    client = ReadOnlyHttp(
        "https://api.example",
        timeout_seconds=1,
        retries=0,
        session=session,  # type: ignore[arg-type]
    )
    with pytest.raises(ApiError, match="HTTPS"):
        client.get_bytes_url("http://download.example/archive")
    assert not session.methods


class _FailingArchiveSession:
    @staticmethod
    def request(*, method: str, **kwargs):
        raise requests.ConnectionError(
            "request failed for https://download.example/archive?signature=sensitive"
        )


def test_archive_download_errors_do_not_retain_signed_urls() -> None:
    client = ReadOnlyHttp(
        "https://api.example",
        timeout_seconds=1,
        retries=0,
        session=_FailingArchiveSession(),  # type: ignore[arg-type]
    )
    with pytest.raises(ApiError) as caught:
        client.get_bytes_url(
            "https://download.example/archive?signature=sensitive"
        )
    assert "download.example" not in str(caught.value)
    assert "sensitive" not in str(caught.value)


class _FailingApiSession:
    @staticmethod
    def request(*, method: str, **kwargs):
        raise requests.ConnectionError(
            "request failed for https://api.example/account?signature=sensitive"
        )


def test_api_request_errors_do_not_retain_signed_query_values() -> None:
    client = ReadOnlyHttp(
        "https://api.example",
        timeout_seconds=1,
        retries=0,
        session=_FailingApiSession(),  # type: ignore[arg-type]
    )
    with pytest.raises(ApiError) as caught:
        client.get_json("/account", params={"signature": "sensitive"})
    assert "sensitive" not in str(caught.value)
    assert str(caught.value).endswith("connection failed")


class _FailingTlsSession:
    @staticmethod
    def request(*, method: str, **kwargs):
        raise requests.exceptions.SSLError(
            "certificate failure at https://api.example/account?signature=sensitive"
        )


def test_transport_errors_use_actionable_categories_without_request_details() -> None:
    client = ReadOnlyHttp(
        "https://api.example",
        timeout_seconds=1,
        retries=0,
        session=_FailingTlsSession(),  # type: ignore[arg-type]
    )

    with pytest.raises(ApiError) as caught:
        client.get_json("/account", params={"signature": "sensitive"})

    assert str(caught.value) == (
        "GET /account failed after 1 attempts: "
        "TLS handshake or certificate validation failed"
    )
    assert "sensitive" not in str(caught.value)


class _RedirectResponse:
    status_code = 302
    headers = {"Location": "http://download.example/archive"}
    text = ""
    content = b""


class _RedirectSession:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def request(self, *, method: str, url: str, **kwargs):
        self.urls.append(url)
        return _RedirectResponse()


def test_archive_download_rejects_non_https_redirects_before_following() -> None:
    session = _RedirectSession()
    client = ReadOnlyHttp(
        "https://api.example",
        timeout_seconds=1,
        retries=0,
        session=session,  # type: ignore[arg-type]
    )
    with pytest.raises(ApiError, match="HTTPS"):
        client.get_bytes_url("https://download.example/archive")
    assert session.urls == ["https://download.example/archive"]
