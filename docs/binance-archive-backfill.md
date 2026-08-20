# Binance historical archive backfill

Binance USD-M Futures exposes asynchronous account-history exports for trades,
income, and orders. Perp Trade History automates requesting, polling, downloading,
retaining, and importing those exports, but starts historical archive backfill only
when an operator runs the backfill command. The periodic workload remains ordinary
REST collection and never starts or resumes archive history by itself.

## Choose the historical boundary

Set `collection.initial_start` before the first backfill run. The following
non-normative configurations illustrate older and newer account boundaries; they do
not define or limit supported history:

```toml
[collection]
initial_start = "2017-01-01T00:00:00Z"
```

```toml
[collection]
initial_start = "2022-01-01T00:00:00Z"
```

Choose the earliest UTC instant required for the account. Earlier values are
supported down to the history available from the venue; the portable template uses
the beginning of 2017 as a conservative venue-history boundary. A later account
start reduces export requests and import work.

The first `archives backfill` invocation stores both this configured start and its
current UTC end as the fixed historical target. Later invocations resume that target;
they do not move its end forward. Ongoing scheduled collection uses the REST APIs to
capture new activity.

Changing `collection.initial_start` later changes the required lower boundary on the
next manual invocation. Complete explicitly ranged imports are reused when they cover
one of the planned intervals.

## Run the backfill

```bash
perp-trade-history \
  --config config/config.toml \
  --secrets config/credentials.env \
  archives backfill
```

Each invocation is idempotent and performs the following work:

1. Polls pending exports that have waited at least ten minutes, downloads completed
   files, imports them, and runs currency conversion.
2. Recomputes exact coverage from retained complete export intervals.
3. Selects the newest missing interval and then moves backward in maximum one-year
   windows.
4. Requests the trade and income pair for each window. These datasets establish
   report coverage. It also requests the matching order export when order quota is
   available; orders are useful supplemental history but do not gate PnL coverage.
5. Persists every accepted request, import result, pending poll time, monthly quota
   wait, next missing interval, and completion marker.

The venue documents monthly request limits of five trade exports, five income
exports, and ten order exports, shared with its web download interface. It does not
document an endpoint that reports remaining archive quota. The command therefore
requests report-critical exports as window pairs and stops a pair when Binance
returns a recognized monthly export-limit error. Authentication, transport, HTTP
rate-limit, parsing, and other API failures retain their own error classification and
are not reported as monthly quota exhaustion. Locally accepted requests are also
counted to avoid unnecessary calls once a known limit is reached.

The normal first invocation can request up to five full report years (ten
report-critical files) plus matching order files. If a request fails after its pair
partner was accepted, the accepted job remains durable and the missing dataset is
resumed first on a later invocation.

## Read progress and resume

Human-readable output and `archives status --json` show:

- the configured start and fixed target end;
- how far contiguous trade-and-income coverage reaches backward;
- remaining report years, supplemental order years, and files;
- the next annual interval and its missing datasets;
- pending export count and the next poll instant;
- locally tracked quota use and the next request instant;
- completion and one-shot scheduling state.

If exports are pending, rerun after the printed `next_poll_at`, which is ten minutes
after the latest request or processing poll. If monthly quota is exhausted, rerun at
the printed `next_request_at`. A completed target is a fast no-op on every later
manual invocation.

## Optionally schedule one rerun

Scheduling is always an explicit option on the manual command. It does not install a
recurring historical service, and a scheduled invocation does not schedule another
invocation.

Use the persisted next action chosen by the command:

```bash
perp-trade-history --config config/config.toml \
  --secrets config/credentials.env \
  archives backfill --schedule-next
```

Or request an explicit one-shot delay:

```bash
perp-trade-history --config config/config.toml \
  --secrets config/credentials.env \
  archives backfill --schedule-in 10m

perp-trade-history --config config/config.toml \
  --secrets config/credentials.env \
  archives backfill --schedule-in 31d
```

The ten-minute delay is appropriate for pending exports: Binance states that export
generation can take about five minutes, and the additional time is contingency. The
31-day option is available for operator preference; `--schedule-next` normally uses
the exact stored first day of the next UTC month after quota exhaustion.

On macOS the command uses a replaceable one-shot `launchd` agent. On other Unix
systems it uses `at` when available. If neither mechanism is usable, archive work
still succeeds and the command prints the exact UTC rerun time and copyable manual
command. Scheduled commands contain only interpreter and protected configuration or
credential-file paths, never API keys, signatures, or signed download URLs.

Cancel a pending rerun with:

```bash
perp-trade-history --config config/config.toml archives schedule cancel
```

## Manual file recovery

API-driven archive export is the primary historical path. Manual file import remains
a recovery option for an export obtained outside the command:

```bash
perp-trade-history --config config/config.toml archives import \
  --kind trades --file /path/to/export.zip
```

An import without an explicit complete range stores and normalizes its records but
does not advance the backfill cursor. To satisfy an exact planned interval, provide
both bounds and assert that the export is complete:

```bash
perp-trade-history --config config/config.toml archives import \
  --kind income --file /path/to/export.zip \
  --start START_UTC --end END_UTC --complete-range
```

This deliberately avoids attempting to infer complex intersections between
arbitrary files, REST retention, and asynchronous export availability. Original
archives remain content-addressed in durable storage, and signed download URLs are
never persisted.

## API references

- [Binance USD-M Futures account API catalog](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account#get-download-id-for-futures-order-history)
- [Official connector account export methods](https://github.com/binance/binance-connector-python/blob/master/clients/derivatives_trading_usds_futures/src/binance_sdk_derivatives_trading_usds_futures/rest_api/rest_api.py)
