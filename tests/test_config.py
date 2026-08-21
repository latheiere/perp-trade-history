from decimal import Decimal
from pathlib import Path

import pytest

from perp_trade_history.config import load_config
from perp_trade_history.errors import ConfigurationError


def _write_config(path: Path, *, enabled: str = "mexc") -> None:
    path.write_text(
        f"""
schema_version = 1
[storage]
data_dir = "history"
[reporting]
target_currency = "USDT"
conversion_method = "previous_day.close"
[reporting.fixed_rates]
WRAPPED = "1"
[collection]
initial_start = "2020-01-01T00:00:00Z"
[venues.{enabled}]
enabled = true
account_id = "primary"
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_config_resolves_relative_storage_and_loads_credentials(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    secrets_path = tmp_path / "secrets.env"
    _write_config(config_path)
    secrets_path.write_text(
        "PERP_TRADE_HISTORY_MEXC_API_KEY=key\n"
        "PERP_TRADE_HISTORY_MEXC_API_SECRET=secret\n",
        encoding="utf-8",
    )
    config = load_config(config_path, secrets_path=secrets_path)
    assert config.data_dir == (tmp_path / "history").resolve()
    assert [venue.name for venue in config.enabled_venues] == ["mexc"]
    assert config.venues["mexc"].api_key == "key"
    assert config.reporting.target_currency == "USDT"
    assert config.reporting.conversion_method == "previous_day.close"
    assert config.reporting.fixed_rates == {"WRAPPED": Decimal("1")}


def test_config_requires_credentials_only_for_enabled_venues(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path)
    with pytest.raises(ConfigurationError, match="requires"):
        load_config(config_path)
    assert load_config(config_path, require_credentials=False).venues["mexc"].enabled


def test_read_only_config_does_not_open_private_credential_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path)
    missing_secrets = tmp_path / "credentials-not-mounted.env"

    config = load_config(
        config_path,
        secrets_path=missing_secrets,
        require_credentials=False,
    )

    assert config.secrets_path == missing_secrets.resolve()
    assert config.venues["mexc"].api_key == ""


def test_config_accepts_migrated_venue_credential_names(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    secrets_path = tmp_path / "secrets.env"
    _write_config(config_path)
    secrets_path.write_text(
        "MEXC_API_KEY=migrated-key\nMEXC_API_SECRET=migrated-secret\n",
        encoding="utf-8",
    )

    config = load_config(config_path, secrets_path=secrets_path)

    assert config.venues["mexc"].api_key == "migrated-key"


def test_prefixed_credential_names_override_migrated_names(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    secrets_path = tmp_path / "secrets.env"
    _write_config(config_path)
    secrets_path.write_text(
        "MEXC_API_KEY=migrated-key\n"
        "MEXC_API_SECRET=migrated-secret\n"
        "PERP_TRADE_HISTORY_MEXC_API_KEY=preferred-key\n"
        "PERP_TRADE_HISTORY_MEXC_API_SECRET=preferred-secret\n",
        encoding="utf-8",
    )

    config = load_config(config_path, secrets_path=secrets_path)

    assert config.venues["mexc"].api_key == "preferred-key"


def test_config_rejects_filesystem_root_as_data_directory(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "schema_version=1\n[storage]\ndata_dir='/'\n[venues.mexc]\nenabled=true\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="filesystem root"):
        load_config(config_path, require_credentials=False)


def test_config_rejects_non_https_spot_market_origin(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "schema_version=1\n"
        "[storage]\n"
        "data_dir='history'\n"
        "[venues.mexc]\n"
        "enabled=true\n"
        "spot_base_url='http://market.invalid'\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="spot_base_url must be an HTTPS origin"):
        load_config(config_path, require_credentials=False)


def test_config_rejects_unknown_conversion_method(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "schema_version=1\n"
        "[storage]\n"
        "data_dir='history'\n"
        "[reporting]\n"
        "target_currency='USDT'\n"
        "conversion_method='live.ticker'\n"
        "[venues.mexc]\n"
        "enabled=true\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="conversion_method"):
        load_config(config_path, require_credentials=False)
