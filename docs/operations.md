# Operating Perp Trade History

This guide covers the commands used after download: configuration checks, history
refreshes, diagnostics, dashboard startup, backup, and restore.

## Check the configuration

After editing `config/config.toml` or `config/credentials.env`, validate both files
without making a network request:

```bash
make check
```

Each enabled venue needs a read-only API key and secret in
`config/credentials.env`. Credentials must allow account and trade-history access.
Do not grant order, transfer, or withdrawal permissions.

## Collect history

Load all history currently available from enabled APIs:

```bash
make history
```

Use an ordinary incremental refresh when you only need newly available records:

```bash
.venv/bin/perp-trade-history \
  --config config/config.toml \
  --secrets config/credentials.env \
  collect
```

Repeated collection updates existing records by their source identity and retains
previously downloaded history.

## Open the dashboard

```bash
make dashboard
```

The dashboard reads local data and opens in the default browser. It can remain open
while collection runs; changed source files are detected automatically.

See [using the dashboard](dashboard.md) for filters, drill-downs, metrics, and data
quality.

## Print the PnL report

Read retained history without collecting new data:

```bash
.venv/bin/perp-trade-history-pnl --config config/config.toml
```

`TRADES`, between `TOTAL` and `EVENTS`, counts observed position cycles. Adding to
a position or partially reducing it stays within one trade until the position is
fully closed. Open positions and positions with missing history boundaries also
count once. Counts depend on the retained evidence; they do not certify complete
history. With period grouping, closed trades belong to their closing period, and
open trades belong to their first observed activity, clipped to `--start` when set.

All amount columns are shown by default. Add `--no-extra-columns` to hide `REWARD`
through `OTHER`, or `--extra-columns` to show them. Hidden amounts remain included
in `TOTAL`.

## Check collection status

Show record counts and source coverage:

```bash
.venv/bin/perp-trade-history --config config/config.toml status
```

Use JSON output when another command or script will read the result:

```bash
.venv/bin/perp-trade-history --config config/config.toml status --json
```

Run the diagnostic check when collection, coverage, archive progress, or scheduling
does not look right:

```bash
.venv/bin/perp-trade-history --config config/config.toml doctor
```

For detailed collection output:

```bash
.venv/bin/perp-trade-history --verbose \
  --config config/config.toml \
  --secrets config/credentials.env \
  collect
```

Logs exclude credentials, signatures, and signed query strings. When sharing an
error, still remove account identifiers and local private paths.

## Extend history with archives

Ordinary APIs may expose only a limited historical window. Where asynchronous
account exports are supported, start or resume the historical backfill with:

```bash
.venv/bin/perp-trade-history \
  --config config/config.toml \
  --secrets config/credentials.env \
  archives backfill
```

Check progress at any time:

```bash
.venv/bin/perp-trade-history --config config/config.toml archives status
```

Archive work is resumable and separate from ordinary refreshes. Follow the
[historical archive guide](binance-archive-backfill.md) for initial boundaries,
quota waits, one-shot reruns, and manual imports.

## Change the reporting currency

Edit the `[reporting]` section in `config/config.toml`, then rebuild stored
conversions:

```bash
.venv/bin/perp-trade-history \
  --config config/config.toml \
  recalculate-conversions
```

See [currency conversion](conversion.md) before choosing a price selector or fixed
fallback.

## Back up collected data

Install backup support once:

```bash
.venv/bin/python -m pip install ".[backup]"
```

Create and verify a signed backup:

```bash
.venv/bin/perp-trade-history --config config/config.toml backup \
  --output /path/to/history.tar.gz
```

Verify an existing backup without restoring it:

```bash
.venv/bin/perp-trade-history verify-backup /path/to/history.tar.gz
```

## Restore into a safe staging directory

Restore never needs to overwrite the active data directory. Choose a new, empty
staging directory:

```bash
.venv/bin/perp-trade-history restore-backup /path/to/history.tar.gz \
  --target /path/to/empty/staging-directory
```

Inspect the restored data before deciding whether to use it as the configured data
directory.

## Find the stored data

The `[storage]` section of `config/config.toml` sets the data directory. It contains:

- retained source responses and imported archives;
- normalized tables used by reports and the dashboard;
- coverage, conversion, archive-progress, and scheduling state.

Collection does not delete retained source records. Run only one collection writer
against a data directory at a time; dashboards, reports, and status commands are
read-only.

## Run without an interactive terminal

Manual collection and dashboard commands are sufficient for normal local use. If
you want unattended refreshes, a persistent local dashboard, or managed releases,
continue with [optional extensions](optional-extensions.md).
