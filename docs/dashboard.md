# Dashboard and analytics

## Intended outcome

The dashboard turns retained account history into a current, explorable analytical
view. It is designed for histories where behavior and performance vary materially
between periods, accounts, and market classes. The result is a scrollable report
that supports both portfolio-level review and episode-level investigation.

The page provides:

1. Executive cashflow performance, episode outcomes, holding time, and notable changes
2. Period, market-class, direction, weekday, time-of-day, and exposure analysis
3. Comparable annual summaries
4. A searchable episode ledger with execution and cashflow drill-down detail
5. Source-coverage, episode-boundary, collection-gap, and refresh indicators

## How the view adapts

Available filter values, date bounds, year cards, comparison periods, episode rows,
and quality states come from the current retained history. Newly collected records
become visible automatically. A full rebuild is deterministic, so the same source
boundary produces the same analytical snapshot.

Interactive filters reuse the current in-memory analytical snapshot instead of
re-reading and reconstructing unchanged history. Any canonical data-file revision
invalidates that snapshot automatically, so newly collected records appear without
manual cache management.

Performance can be viewed as cumulative cashflow, period cashflow, a rolling
three-period total, or absolute cashflow drawdown. Monthly, quarterly, and yearly
aggregation rebuilds the period series rather than sampling a monthly curve. Date,
profile, market-class, direction, duration, outcome, and timezone controls apply to
the retained episodes. Timezone selection also changes weekday and time-of-day
grouping.

Pattern cells and duration rows are coordinated filters. Selecting one focuses the
episode ledger on the matching episodes; selecting an episode exposes its normalized
executions and attributed cashflows without leaving the page.

Notable changes compare sufficiently populated calendar periods and show their
sample sizes and direction. When there is not enough comparable evidence, the page
shows an unavailable or low-confidence state instead of making a claim.

The dashboard does not fit a machine-learning model. Deterministic reconstruction
and transparent statistical comparisons remain explainable across materially
different histories without training data, retraining, or model monitoring.

## Analytical boundaries

Position episodes are reconstructed from normalized execution transitions. Cashflow
attribution prefers exact identifiers and uses temporal attribution only when one
episode is a defensible match. Ambiguous or unassigned events remain visible in
quality summaries.

The analytical scope is deliberately limited to normalized trades and their
attributable cashflows. The product does not display placeholders for measures that
need a market-price path, portfolio-capital history, trade-plan risk, latency, or
market depth. A future metric begins with a source-data extension; no unrelated
value is substituted.

Execution notional is shown only when the normalized trade provides notional or a
defensible base-quantity and price basis. Missing contract economics remain
unavailable for that execution rather than being estimated.

## Coverage and the collection build report

Source-history coverage and reconstructed episode boundaries are separate:

- Source coverage asks whether the complete recorded collection intervals contain
  the full observed episode interval.
- Boundary completeness asks whether the retained executions establish both the
  beginning and end of an episode.

Neither is presented as trading performance. Both appear in Data quality. The
collapsed collection build report lists merged uncovered trade intervals with the
source profile, market class, inclusive start and end, recorded status, acquisition
method, and the narrowest recorded limitation. When the collector did not record a
reason, the report says so instead of inferring one.

Gap discovery stays within reconstructed episode-domain intervals. It does not
invent an expected-history start before observed activity. Adjacent gaps merge only
when their profile, scope, status, reason, source, and acquisition method are
equivalent.

## Local use

The shortest source-checkout flow is:

```bash
make bootstrap
```

Enable the required adapters in `config/config.toml` and add read-only credentials
to `config/credentials.env`. Then load all currently available history and open the
dashboard:

```bash
make first-run
```

Run `make history` to refresh the full available history without starting the page,
or `make dashboard` to serve the current retained data without credentials.

For visual evaluation without account data:

```bash
.venv/bin/perp-trade-history-dashboard --synthetic
```

## Supervised use

A supervisor should bind the dashboard to a loopback address and disable automatic
browser opening:

```bash
perp-trade-history-dashboard \
  --config config/config.toml \
  --host 127.0.0.1 \
  --port 8050 \
  --no-browser
```

Readiness is exposed at `/readyz`. The dashboard is read-only and can run
independently of collection. See [operations](operations.md) for collection,
scheduling, backup, diagnostics, and durable-data guarantees.
