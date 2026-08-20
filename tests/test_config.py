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


def test_config_requires_credentials_only_for_enabled_venues(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    _write_config(config_path)
    with pytest.raises(ConfigurationError, match="requires"):
        load_config(config_path)
    assert load_config(config_path, require_credentials=False).venues["mexc"].enabled


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
