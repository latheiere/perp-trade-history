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
    assert "tradier-dev" not in str(project).lower()
    assert project["scripts"]["perp-trade-history-pnl"].endswith("text_report:main")
