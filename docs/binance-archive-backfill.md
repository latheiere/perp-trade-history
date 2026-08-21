# Load older Binance history

Ordinary account APIs expose a limited historical window. Binance account-history
exports can extend the local dataset with older trades, income, and supporting order
records.

Archive backfill is manual and resumable. Running an ordinary collection never
starts archive exports.

## 1. Set the earliest required date

Before the first archive run, open `config/config.toml` and set the lower history
boundary in UTC:

```toml
[collection]
initial_start = "2022-01-01T00:00:00Z"
```

Choose the earliest date you need. An earlier boundary requests more export windows
and can take more quota cycles to complete.

The first run fixes the current UTC time as the upper boundary for this backfill.
Later runs resume the same historical target while ordinary collection handles new
activity.

## 2. Start or resume the backfill

```bash
.venv/bin/perp-trade-history \
  --config config/config.toml \
  --secrets config/credentials.env \
  archives backfill
```

The command resumes safely when run again. It:

1. checks previously requested exports;
2. downloads and imports completed files;
3. converts imported cashflows into the configured reporting currency;
4. identifies the next missing historical interval; and
5. requests the next available exports.

Original archive files remain retained locally. Signed download links are not saved.

## 3. Read progress

```bash
.venv/bin/perp-trade-history \
  --config config/config.toml \
  archives status
```

Add `--json` for machine-readable output. Status shows:

- the configured start and fixed backfill end;
- completed and remaining history;
- pending exports and the next useful poll time;
- the next interval and datasets to request;
- recorded quota waits; and
- whether the historical target is complete.

When exports are still being prepared, rerun at or after the displayed poll time.
When the monthly archive quota is exhausted, rerun at or after the displayed request
time. A completed backfill becomes a fast no-op.

## Schedule one automatic rerun

Let the command schedule the next useful action:

```bash
.venv/bin/perp-trade-history \
  --config config/config.toml \
  --secrets config/credentials.env \
  archives backfill --schedule-next
```

Or choose a one-shot delay:

```bash
.venv/bin/perp-trade-history \
  --config config/config.toml \
  --secrets config/credentials.env \
  archives backfill --schedule-in 10m
```

A scheduled invocation runs once and does not schedule another invocation. If the
computer cannot install a one-shot schedule, the command prints the next UTC time
and a copyable manual command instead.

Cancel a pending rerun with:

```bash
.venv/bin/perp-trade-history \
  --config config/config.toml \
  archives schedule cancel
```

## Import an archive file manually

Use manual import when you already have an account-history export:

```bash
.venv/bin/perp-trade-history \
  --config config/config.toml \
  archives import --kind trades --file /path/to/export.zip
```

This stores and normalizes the file but does not claim that a planned time interval
is complete.

If the file is known to contain one exact complete interval, provide both UTC bounds:

```bash
.venv/bin/perp-trade-history \
  --config config/config.toml \
  archives import \
  --kind income \
  --file /path/to/export.zip \
  --start START_UTC \
  --end END_UTC \
  --complete-range
```

Use `--complete-range` only when the export is known to cover the entire stated
interval. This allows archive progress and dashboard coverage to advance accurately.

## If progress stops

1. Run `archives status` and follow its next poll or request time.
2. Run `.venv/bin/perp-trade-history --config config/config.toml doctor`.
3. Confirm that the read-only credential still permits account-history exports.
4. Rerun with `--verbose` and review the sanitized error classification.

Authentication, transport, API rate limits, parsing failures, and monthly archive
quota waits are reported separately so the next action remains clear.
