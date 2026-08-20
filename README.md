# Perpetual trade history

`perp-trade-history` retrieves private perpetual-account history through read-only
exchange APIs and keeps an auditable local record. Binance USD-margined perpetuals,
Gate perpetual accounts, and MEXC perpetuals are implemented behind one adapter
contract. Additional venue and product adapters can be added without changing the
normalized storage layer.

The collector issues only HTTP `GET` requests. Use API keys that have account and
trade-read permission and no order-placement, transfer, or withdrawal permission.
The HTTP client has no method for issuing a mutating exchange request.

## Durable data model

The data directory contains human-readable normalized CSV tables:

- `normalized/cashflows.csv`: signed realized PnL, funding, commission, rebates,
  transfers, liquidation adjustments, and other balance changes;
- `normalized/executions.csv`: individual fills with order linkage, side, quantity,
  price, notional, fee, realized PnL, and liquidity role;
- `normalized/orders.csv`: latest known state for every retrievable regular,
  conditional, trailing, chase, and stop order;
- `normalized/order_events.csv`: retrievable order lifecycle events such as field-level
  amendments, with before/after values and parent-order linkage;
- `normalized/positions.csv`: current and historical position summaries;
- `normalized/account_snapshots.csv`: imported or collected balance and account
  metrics;
- `normalized/coverage.csv`: successful, partial, point-in-time, and failed coverage
  intervals for every source dataset.

Every economic row carries a schema version, venue, logical account, perpetual
product scope, UTC timestamp, stable source identity, collection timestamp, and
raw-record reference. `reporting_role` distinguishes primary cash-ledger rows from
supplemental summaries so reports do not silently double-count the same outcome.

Original API and import records are retained as upserted JSON Lines under
`raw/<venue>/<source>.jsonl`. Sync checkpoints and source errors are recorded under
`state/`. Existing normalized and raw records are never removed by collection.
A repeated source identity is overwritten only when the source row changes.

## Configuration and credentials

Copy the templates and keep the credential file outside Git:

```bash
cp config/config.example.toml /path/to/config.toml
cp config/credentials.env.example /path/to/secrets.env
chmod 600 /path/to/secrets.env
```

Enable each configured venue in `config.toml`, then export the portable runtime
locations:

```bash
export PERP_TRADE_HISTORY_CONFIG=/path/to/config.toml
export PERP_TRADE_HISTORY_ENV_FILE=/path/to/secrets.env
export PERP_TRADE_HISTORY_DATA_DIR=/path/to/persistent/data
```

Validate without making a network request, then collect:

```bash
perp-trade-history check-config
perp-trade-history collect
```

The prefixed names in the credential template take precedence. Unprefixed
per-venue key and secret names are also accepted so an existing private
environment file can be migrated without rewriting its values.

The first successful REST source run backfills to the configured start or the oldest
history exposed by that endpoint. Later runs overlap previous coverage and upsert
changed records. `collect --full` revisits the full reachable REST range without
deleting prior data.

## Operational logging

Default command output is concise and intended for operators: scheduled collection
commands report aggregate status, per-venue source and record counts, and actionable
warnings or failures. Pass `--json` to retain the complete machine-readable result,
including storage statistics for every source. Pass `--verbose` before the command
to include successful per-source diagnostic details.

Symbol-scoped collection logs report candidate provenance and discovery-limit
decisions. A failed history request includes its venue, source, endpoint, contract
identifier, and UTC request window while excluding credentials, signatures, and raw
signed query strings.

Log timestamps use UTC in ISO 8601 format. `INFO` and `DEBUG` messages are written to
standard output; `WARNING` and `ERROR` messages are written to standard error. This
keeps supervisor error logs limited to conditions that need attention. A scheduled
collection that overlaps another singleton writer is reported as a warning and a
successful skip, because the active writer retains exclusive ownership of the data.

## Binance asynchronous history exports

Binance exposes asynchronous exports for three USD-margined futures account
datasets: orders, trades, and income. The implementation automates the complete
lifecycle for all three:

1. detect an uncovered interval older than the corresponding REST retention bound;
2. request an export identifier;
3. poll the identifier until the export is complete;
4. download the exchange-issued archive over HTTPS;
5. retain the original bytes, SHA-256 digest, and an extracted source CSV;
6. parse and normalize through the same functions used by REST ingestion;
7. deduplicate and upsert into the canonical tables;
8. merge the completed interval into persistent coverage state.

Initial deep backfill is advanced with one idempotent step command:

```bash
perp-trade-history archives backfill-step
```

Each invocation polls pending work first and requests at most one uncovered export
interval. Dataset selection is round-robin. The initial target is fixed when the
backfill starts, so a long-lived polling job becomes a no-op when that target is
complete; it does not repeatedly request full exports. Locally tracked monthly
quotas prevent the worker from knowingly exceeding the documented venue limits.
The venue also shares those quotas with its interactive download interface, which
the local state cannot observe. Every programmatic request attempt is recorded
before the network call, including failed or uncertain attempts, so a lost response
cannot make the local quota ledger undercount usage.

Routine weekly ingestion uses `collect` and therefore REST only. To extend a
completed archive target after a gap has become unreachable through REST, run an
explicit recovery request:

```bash
perp-trade-history archives request-next --recover-gaps
```

When a specific interval is known to be unreachable through REST, request that
bounded archive range manually without changing the recurring policy:

```bash
perp-trade-history archives request-range --kind trades \
  --start "$GAP_START_UTC" --end "$GAP_END_UTC"
```

The explicit request observes the same one-year window, pending-job, deduplication,
and locally tracked monthly-quota safeguards as initial backfill.

Inspect jobs and exact uncovered ranges without making a network request:

```bash
perp-trade-history archives status --json
```

If an exchange-provided file is obtained outside the API lifecycle, import CSV,
ZIP, or gzip through the same normalization path:

```bash
perp-trade-history archives import --kind trades --file /path/to/export.zip
```

An imported file is treated as partial coverage unless its exact export interval is
provided and explicitly asserted complete:

```bash
perp-trade-history archives import --kind income --file /path/to/export.csv \
  --start "$EXPORT_RANGE_START_UTC" --end "$EXPORT_RANGE_END_UTC" --complete-range
```

Downloaded artifacts and extracted source CSV files are retained under
`raw/binance/archive-files/<dataset>/`. Content-addressed filenames preserve every
distinct source archive while making repeated imports of identical bytes
idempotent. Signed download URLs are never persisted.

The supervised `initialize-archives` command advances this initial export
backfill when Binance is enabled and exits successfully without API access when
it is disabled. It is separate from weekly ingestion and becomes a no-op after
the fixed initial target is complete. It never extends that target into newly
unreachable periods without an explicit recovery request.

## Reporting and completeness

Print a terminal-friendly closed-execution PnL report across every enabled venue:

```bash
PYTHONPATH=src .venv/bin/python show_closed_perp_positions_pnl.py \
  --config /path/to/config.toml
```

The text report separates realized PnL, funding, commission, rebates, and other
economic events, then prints totals per venue and settlement currency. It also
shows coverage status and lists enabled venues that have no matching primary
cashflows. Use `--period day`, `--period week`, or `--period month` for time
buckets; `--start`, `--end`, `--venue`, and `--symbol` narrow the report without
changing stored data.

## Diagnostics and recovery

`doctor` reports the last successful weekly run, persisted retry timing,
classified source failures, endpoint-specific terminal contract exclusions,
archive coverage and quota state, and authoritative lock ownership:

```bash
perp-trade-history --config /path/to/config.toml doctor
```

Create a signed Statecrate archive before changing a release, then verify it
through the independent public-key path:

```bash
perp-trade-history --config /path/to/config.toml backup \
  --output /path/to/application-archives/trade-history.tar.gz
perp-trade-history verify-backup \
  /path/to/application-archives/trade-history.tar.gz
```

Statecrate retains its signing and verification keys beside the archive. Restore
is staging-first; inspect the restored tree before replacing any active data:

```bash
perp-trade-history restore-backup \
  /path/to/application-archives/trade-history.tar.gz \
  --target /path/to/empty/staging-directory
```

Create a CSV summary without mixing settlement currencies:

```bash
perp-trade-history report --period month \
  --group-by venue,symbol,event_type,currency
```

Primary cash flows are included by default. `--include-supplemental` explicitly adds
position-summary estimates for coverage analysis. For supported venues, every
report row also includes `coverage_status` and machine-readable `coverage_gaps`.
Transfers and account conversions remain available in the normalized ledger but are
excluded from PnL totals unless `--include-non-pnl` is supplied.
A period is complete only when every dataset required for that venue's PnL is fully
covered. Missing, failed, symbol-scoped, or retention-limited intervals are reported
as incomplete or unknown rather than being silently treated as complete.

## Source coverage boundaries

Binance REST account trades are symbol-scoped, limited to seven-day query windows,
and retained for six months. Income is account-wide and retained for three months.
Regular orders are symbol-scoped, limited to seven-day query windows, and generally
retained for ninety days, with shorter retention for canceled or expired unfilled
orders. Read-only order amendment history is retained for three months and requires
one query per known order. Force-order history has a short rolling bound.
Asynchronous order, trade, and income exports accept at most one year per request.
Order exports are limited to ten requests per month; trade and income exports are
each limited to five. Export requests have high request weight, and completed
download links expire after seven days. Position-margin change history is not
collected because the venue classifies that endpoint as `TRADE`, despite using GET;
collecting it would violate the module's read-only credential boundary. See the
official [Binance account API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account)
and [Binance trade API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade).

Gate time-range endpoints are paged for account changes, fills, orders, position
closes, liquidations, and auto-deleveraging. Current account and position snapshots,
historical position snapshots, and advanced order families are stored separately so
their different coverage guarantees remain visible. See the official
[Gate futures API](https://www.gate.com/docs/developers/apiv4/en/futures/).

MEXC page and bounded time-window endpoints are used for account snapshots,
transfers, funding, positions, fills, regular orders, trigger orders, and position
stop orders. Sources whose history depends on venue retention or discovered symbols
are marked partial. See the official
[MEXC contract API](https://mexcdevelop.github.io/apidocs/contract_v1_en/).

## Deployment contract

The portable package contains no machine-specific installation path, launchd label,
secret location, or local-time calendar assumption. `runtime-contract.yaml` publishes
two independently supervised batch workloads. The weekly workload runs
`weekly-collect`, whose persistent UTC gate permits one REST-only ingestion at or
after Monday 08:00 UTC and retries source failures without advancing the gate. The
local control plane invokes it on a short interval, so daylight-saving changes do
not move the UTC boundary. The second workload advances only the fixed initial
asynchronous archive target. Explicit gap recovery and exchange-provided CSV import
remain manual operations.
