import tomllib
from pathlib import Path


def test_core_install_uses_only_public_runtime_dependency() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]

    assert project["dependencies"] == ["requests>=2.31.0,<3"]
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE"]
    assert (root / "LICENSE").read_text(encoding="utf-8").startswith(
        "                                 Apache License\n"
    )
    assert project["optional-dependencies"]["backup"] == ["statecrate>=0.1,<0.2"]
    assert "dash>=4.4,<5" in project["optional-dependencies"]["dashboard"]
    assert "tradier-dev" not in str(project).lower()
    assert project["scripts"]["perp-trade-history-dashboard"].endswith(
        "dashboard.cli:main"
    )
    assert project["scripts"]["perp-trade-history-pnl"].endswith("text_report:main")


def test_runtime_contract_keeps_collection_scheduled_and_dashboard_read_only() -> None:
    root = Path(__file__).resolve().parents[1]
    contract = (root / "runtime-contract.yaml").read_text(encoding="utf-8")

    assert "weekly_rest_ingestion" in contract
    assert "weekly-collect" in contract
    assert "lifecycle: daemon" in contract
    assert "perp-trade-history-dashboard" in contract
    assert "scope: loopback" in contract
    assert "readiness_endpoint: /readyz" in contract
    assert "initialize-archives" not in contract
    assert "initial_archive_backfill" not in contract
