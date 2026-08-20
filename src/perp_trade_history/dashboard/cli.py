from __future__ import annotations

import argparse
import threading
import webbrowser

from waitress import serve

from perp_trade_history.config import load_config
from perp_trade_history.conversion import StoredCashflowConversion
from perp_trade_history.dashboard.app import create_app
from perp_trade_history.dashboard.providers import (
    AnalyticsSnapshotProvider,
    SyntheticSnapshotProvider,
)
from perp_trade_history.errors import ConfigurationError
from perp_trade_history.storage import DataStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the read-only local analytics dashboard.")
    parser.add_argument("--config", help="TOML configuration path")
    parser.add_argument("--host", default="127.0.0.1", help="Listen address")
    parser.add_argument("--port", type=_port, default=8050, help="Listen port")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the dashboard in the default browser",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Use deterministic demonstration data instead of local storage",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.synthetic:
        provider = SyntheticSnapshotProvider()
    else:
        try:
            config = load_config(args.config, require_credentials=False)
        except ConfigurationError as exc:
            parser.error(str(exc))
        store = DataStore(config.data_dir)
        provider = AnalyticsSnapshotProvider(
            store,
            conversion=StoredCashflowConversion(
                store,
                target_currency=config.reporting.target_currency,
                method=config.reporting.conversion_method,
                fixed_rates=config.reporting.fixed_rates,
            ),
            reporting_currency=config.reporting.target_currency,
        )
    app = create_app(provider)
    if not args.no_browser:
        _open_browser_later(_browser_url(args.host, args.port))
    serve(app.server, host=args.host, port=args.port, threads=8)


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _browser_url(host: str, port: int) -> str:
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}"


def _open_browser_later(url: str, *, delay_seconds: float = 0.8) -> None:
    timer = threading.Timer(delay_seconds, webbrowser.open, args=(url,))
    timer.daemon = True
    timer.start()


if __name__ == "__main__":
    main()
