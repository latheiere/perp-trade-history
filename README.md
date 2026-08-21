# Perp Trade History

A private trading analytics cockpit for long-term perpetual-account history.
Headline PnL rarely explains what happened: strategies, exposure, and results change
across years. Perp Trade History makes those changes explorable while keeping every
conclusion connected to retained evidence—and never needs permission to trade.

- One polished, scrollable report for performance, patterns, risk, and trades
- Interactive comparisons across periods, market classes, directions, and exposure
- Automatic updates as new history arrives
- Trustworthy drill-downs, visible data quality, and auditable retained records
- Local ownership with read-only account access

## Quick start

Python 3.11 or newer is required.

1. Download and bootstrap.

   ```bash
   git clone https://github.com/latheiere/perp-trade-history.git
   cd perp-trade-history
   make bootstrap
   ```

2. Enable the required adapters in `config/config.toml` and add read-only credentials
   to `config/credentials.env`.

3. Load available history and open the dashboard.

   ```bash
   make first-run
   ```

Credentials need account and trade-history access only. Do not grant order,
transfer, or withdrawal permissions.

## Reporting

Use the interactive dashboard for visual investigation and compact text or JSON
reports for automation. Results remain comparable in the configured reporting
currency, with incomplete source coverage visible rather than silently ignored.

See [currency conversion](docs/conversion.md) for reporting-currency behavior.

## Interactive analytics

The dashboard highlights performance, changing patterns, reconstructed episodes,
and data quality across materially different periods. It adapts as retained history
grows and uses only analysis supported directly by retained trade evidence.

See [dashboard and analytics](docs/dashboard.md) for the detailed analytical scope,
metric boundaries, and service usage.

## Configuration and data

Portable configuration templates are provided in [`config`](config). Collected
history remains local, auditable, and independent of the dashboard process.

See [operations](docs/operations.md) for configuration, collection, diagnostics,
storage guarantees, and deployment. See
[historical archive coverage](docs/binance-archive-backfill.md) for extending the
available source history.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
ruff check .
```

When reporting a problem, include the command, sanitized error, Python version,
affected adapter category, and source-history method. Never include credentials,
signatures, signed URLs, or account identifiers.

## License

Licensed under the [Apache License 2.0](LICENSE).
