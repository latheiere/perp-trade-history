from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from perp_trade_history.errors import ConfigurationError
from perp_trade_history.models import parse_datetime

CONFIG_ENV = "PERP_TRADE_HISTORY_CONFIG"
SECRETS_ENV = "PERP_TRADE_HISTORY_ENV_FILE"
DATA_ENV = "PERP_TRADE_HISTORY_DATA_DIR"

CREDENTIAL_VARIABLES: dict[str, tuple[str, str]] = {
    "binance": (
        "PERP_TRADE_HISTORY_BINANCE_API_KEY",
        "PERP_TRADE_HISTORY_BINANCE_API_SECRET",
    ),
    "gate": (
        "PERP_TRADE_HISTORY_GATE_API_KEY",
        "PERP_TRADE_HISTORY_GATE_API_SECRET",
    ),
    "mexc": (
        "PERP_TRADE_HISTORY_MEXC_API_KEY",
        "PERP_TRADE_HISTORY_MEXC_API_SECRET",
    ),
}

CREDENTIAL_ALIASES: dict[str, tuple[str, str]] = {
    "binance": ("BINANCE_API_KEY", "BINANCE_API_SECRET"),
    "gate": ("GATE_API_KEY", "GATE_API_SECRET"),
    "mexc": ("MEXC_API_KEY", "MEXC_API_SECRET"),
}

SUPPORTED_VENUES = frozenset(CREDENTIAL_VARIABLES)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class CollectionSettings:
    initial_start_ms: int
    overlap_ms: int
    request_timeout_seconds: int
    request_retries: int
    page_limit: int
    max_pages_per_query: int


@dataclass(frozen=True, slots=True)
class VenueSettings:
    name: str
    enabled: bool
    account_id: str
    base_url: str
    options: dict[str, Any]
    api_key: str
    api_secret: str


@dataclass(frozen=True, slots=True)
class AppConfig:
    path: Path
    secrets_path: Path | None
    data_dir: Path
    collection: CollectionSettings
    venues: dict[str, VenueSettings]

    @property
    def enabled_venues(self) -> list[VenueSettings]:
        return [settings for settings in self.venues.values() if settings.enabled]


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        raise ConfigurationError(f"credential file does not exist: {path}")
    values: dict[str, str] = {}
    for line_number, original in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(f"invalid credential line {path}:{line_number}")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not _ENV_NAME.fullmatch(name):
            raise ConfigurationError(f"invalid credential name {path}:{line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[name] = value
    return values


def load_config(
    config_path: str | Path | None = None,
    *,
    secrets_path: str | Path | None = None,
    require_credentials: bool = True,
) -> AppConfig:
    raw_config_path = config_path or os.environ.get(CONFIG_ENV)
    if not raw_config_path:
        raise ConfigurationError(
            f"configuration path is required via --config or {CONFIG_ENV}"
        )
    path = Path(raw_config_path).expanduser().resolve()
    if not path.is_file():
        raise ConfigurationError(f"configuration file does not exist: {path}")

    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"unable to read configuration {path}: {exc}") from exc
    if payload.get("schema_version") != 1:
        raise ConfigurationError("configuration schema_version must be 1")

    resolved_secrets = _resolve_optional_path(secrets_path or os.environ.get(SECRETS_ENV), path)
    file_environment = load_env_file(resolved_secrets) if resolved_secrets else {}

    storage = _mapping(payload, "storage")
    configured_data = os.environ.get(DATA_ENV) or storage.get("data_dir")
    if not configured_data:
        raise ConfigurationError(f"storage.data_dir or {DATA_ENV} is required")
    data_dir = _resolve_path(configured_data, path)
    _validate_data_path(data_dir)

    collection_payload = _mapping(payload, "collection")
    initial_start_text = str(
        collection_payload.get("initial_start", "2017-01-01T00:00:00Z")
    )
    try:
        initial_start_ms = int(parse_datetime(initial_start_text).timestamp() * 1000)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid collection.initial_start: {initial_start_text}") from exc
    overlap_hours = _bounded_int(collection_payload, "overlap_hours", 72, 1, 24 * 31)
    collection = CollectionSettings(
        initial_start_ms=initial_start_ms,
        overlap_ms=overlap_hours * 60 * 60 * 1000,
        request_timeout_seconds=_bounded_int(
            collection_payload, "request_timeout_seconds", 30, 1, 300
        ),
        request_retries=_bounded_int(collection_payload, "request_retries", 4, 0, 10),
        page_limit=_bounded_int(collection_payload, "page_limit", 100, 1, 1000),
        max_pages_per_query=_bounded_int(
            collection_payload, "max_pages_per_query", 50000, 1, 100000
        ),
    )

    venue_payload = _mapping(payload, "venues")
    unknown = set(venue_payload) - SUPPORTED_VENUES
    if unknown:
        raise ConfigurationError(f"unsupported venues: {sorted(unknown)}")
    venues: dict[str, VenueSettings] = {}
    for name in sorted(SUPPORTED_VENUES):
        raw = venue_payload.get(name, {})
        if not isinstance(raw, dict):
            raise ConfigurationError(f"venues.{name} must be a table")
        enabled = bool(raw.get("enabled", False))
        account_id = str(raw.get("account_id", "primary")).strip()
        if not account_id:
            raise ConfigurationError(f"venues.{name}.account_id cannot be empty")
        base_url = str(raw.get("base_url", _default_base_url(name))).rstrip("/")
        _validate_base_url(name, base_url)
        key_variable, secret_variable = CREDENTIAL_VARIABLES[name]
        alias_key, alias_secret = CREDENTIAL_ALIASES[name]
        api_key = (
            os.environ.get(key_variable)
            or file_environment.get(key_variable)
            or os.environ.get(alias_key)
            or file_environment.get(alias_key, "")
        )
        api_secret = (
            os.environ.get(secret_variable)
            or file_environment.get(secret_variable)
            or os.environ.get(alias_secret)
            or file_environment.get(alias_secret, "")
        )
        if enabled and require_credentials and (not api_key or not api_secret):
            raise ConfigurationError(
                f"enabled venue {name} requires {key_variable} and {secret_variable}"
            )
        options = {key: value for key, value in raw.items() if key not in {
            "enabled", "account_id", "base_url"
        }}
        _validate_options(name, options)
        venues[name] = VenueSettings(
            name=name,
            enabled=enabled,
            account_id=account_id,
            base_url=base_url,
            options=options,
            api_key=api_key,
            api_secret=api_secret,
        )

    if not any(settings.enabled for settings in venues.values()):
        raise ConfigurationError("at least one venue must be enabled")
    return AppConfig(
        path=path,
        secrets_path=resolved_secrets,
        data_dir=data_dir,
        collection=collection,
        venues=venues,
    )


def _mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"{key} must be a table")
    return value


def _resolve_optional_path(value: str | Path | None, config_path: Path) -> Path | None:
    if value in (None, ""):
        return None
    return _resolve_path(value, config_path)


def _resolve_path(value: str | Path, config_path: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def _validate_data_path(path: Path) -> None:
    if path == Path(path.anchor):
        raise ConfigurationError("data directory cannot be a filesystem root")
    if path.exists() and not path.is_dir():
        raise ConfigurationError(f"data path is not a directory: {path}")


def _bounded_int(
    payload: dict[str, Any], key: str, default: int, minimum: int, maximum: int
) -> int:
    try:
        value = int(payload.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"collection.{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(
            f"collection.{key} must be between {minimum} and {maximum}"
        )
    return value


def _default_base_url(venue: str) -> str:
    return {
        "binance": "https://fapi.binance.com",
        "gate": "https://api.gateio.ws",
        "mexc": "https://contract.mexc.com",
    }[venue]


def _validate_base_url(venue: str, base_url: str) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
        raise ConfigurationError(f"venues.{venue}.base_url must be an HTTPS origin")


def _validate_options(venue: str, options: dict[str, Any]) -> None:
    if venue in {"binance", "mexc"}:
        symbols = options.get("symbols", [])
        if not isinstance(symbols, list) or not all(
            isinstance(symbol, str) and symbol.strip() for symbol in symbols
        ):
            raise ConfigurationError(f"venues.{venue}.symbols must be a string array")
    if venue == "binance" and not isinstance(options.get("discover_active_symbols", True), bool):
        raise ConfigurationError("venues.binance.discover_active_symbols must be boolean")
    if venue == "binance":
        discovery_limit = options.get("max_public_discovery_symbols", 100)
        if (
            isinstance(discovery_limit, bool)
            or not isinstance(discovery_limit, int)
            or discovery_limit < 0
        ):
            raise ConfigurationError(
                "venues.binance.max_public_discovery_symbols must be a non-negative integer"
            )
    if venue == "gate":
        settlements = options.get("settlements", ["usdt", "btc"])
        if not isinstance(settlements, list) or not settlements:
            raise ConfigurationError("venues.gate.settlements must be a non-empty array")
        invalid = {str(value).lower() for value in settlements} - {"usdt", "btc"}
        if invalid:
            raise ConfigurationError(
                f"venues.gate.settlements contains unsupported values: {sorted(invalid)}"
            )
