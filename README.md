# Perp Trade History

Collect perpetual-account history through read-only APIs, retain auditable source
records, and produce fast cross-venue PnL reports in one configured currency.

- Portable Python package with one required runtime dependency.
- Idempotent collection and normalization with explicit coverage status.
- Collection-time currency conversion backed by daily spot-rate sidecars.
- Compact reports grouped by venue and base symbol, with verbose contract detail
  available on demand.
- Optional archive backfill, recurring scheduling, and signed backups.

Python 3.11 or newer is required.

## Quick start

```bash
git clone https://github.com/latheiere/perp-trade-history.git
cd perp-trade-history
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .

cp config/config.example.toml config/config.toml
cp config/credentials.env.example config/credentials.env
chmod 600 config/credentials.env
```

Choose a reporting currency and enable the required venue adapters in
`config/config.toml`:

```toml
[storage]
data_dir = "data"

[reporting]
target_currency = "USDT"
conversion_method = "previous_day.close"

[venues.binance]
enabled = true
```

Add read-only credentials to `config/credentials.env`, then validate, collect, and
report:

```bash
perp-trade-history \
  --config config/config.toml \
  --secrets config/credentials.env \
  check-config

perp-trade-history \
  --config config/config.toml \
  --secrets config/credentials.env \
  collect

perp-trade-history-pnl --config config/config.toml
```

The collector accepts only HTTP `GET` operations. Credentials should have account
and trade-history read permissions without order, transfer, or withdrawal access.

## Reporting

Collection is followed by a shared conversion pass. Daily rates are stored once in
`normalized/conversion_rates.csv`; converted cashflow values are stored separately
in `normalized/cashflow_conversions.csv`. Reports use those persisted values and
never request market data.

Compact output is the default. It removes contract quote and delivery suffixes,
groups records by base symbol and venue, and prints venue totals in the configured
reporting currency. Use `--verbose` for full contract symbols and currency detail.

```bash
perp-trade-history-pnl --config config/config.toml --period month
perp-trade-history-pnl --config config/config.toml --verbose
perp-trade-history --config config/config.toml report --period month --json
```

Changing the target currency, price selector, or fixed-rate configuration marks
stored conversions as stale. Rebuild them explicitly:

```bash
perp-trade-history --config config/config.toml recalculate-conversions
```

See [currency conversion](docs/conversion.md) for rate selection, fixed fallbacks,
storage, and recalculation behavior.

## Configuration and data

The portable templates are
[`config/config.example.toml`](config/config.example.toml) and
[`config/credentials.env.example`](config/credentials.env.example). Configuration,
credential, and data locations can also be supplied through:

```text
PERP_TRADE_HISTORY_CONFIG
PERP_TRADE_HISTORY_ENV_FILE
PERP_TRADE_HISTORY_DATA_DIR
```

Durable state remains human-readable:

```text
normalized/   canonical CSV tables and conversion sidecars
raw/          retained API JSON Lines and source archives
state/        checkpoints, failures, schedules, and coverage state
```

See [operations](docs/operations.md) for archive ingestion, scheduling, diagnostics,
optional backups, and storage guarantees.

## Optional features

The base installation requires only Requests. Signed backup commands are isolated
behind an extra; local control-plane packages are not dependencies.

```bash
python -m pip install ".[backup]"
```

Recurring services are also optional. Interactive collection and reporting require
neither a service manager nor a private local module.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
ruff check .
```

Open an issue with the command, sanitized error, Python version, affected adapter
category, and whether the history came from REST collection or archive import.
Never include credentials, signatures, signed URLs, or account identifiers.

## License

Licensed under the [Apache License 2.0](LICENSE).
