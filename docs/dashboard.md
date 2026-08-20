# Dashboard and analytics

## Intended outcome

The dashboard turns retained account history into a current, explorable analytical
view. It is designed for histories where behavior and performance vary materially
between periods, accounts, and market classes. The result is a scrollable report
that supports both portfolio-level review and episode-level investigation.

The page provides:

1. Executive performance, cashflow, drawdown, and notable changes
2. Period, market-class, direction, weekday, time-of-day, and exposure analysis
3. Comparable annual summaries
4. A searchable episode ledger with drill-down detail
5. Coverage, capability, unsupported-metric, and refresh indicators

## How the view adapts

Available filter values, year cards, comparison periods, episode rows, and quality
states come from the current retained history. Newly collected records become
visible automatically. A full rebuild is deterministic, so the same source boundary
produces the same analytical snapshot.

Notable changes compare sufficiently populated calendar periods and show their
sample sizes and direction. When there is not enough comparable evidence, the page
shows an unavailable or low-confidence state instead of making a claim.

The MVP does not fit a machine-learning model. Deterministic reconstruction and
transparent statistical comparisons make the first version explainable across
materially different histories without training data, retraining, or model
monitoring. Predictive or clustering models can be added later only when they answer
a validated question that simpler analysis cannot.

## Analytical boundaries

Position episodes are reconstructed from normalized execution transitions. Cashflow
attribution prefers exact identifiers and uses temporal attribution only when one
episode is a defensible match. Ambiguous or unassigned events remain visible in
quality summaries.

Metrics are enabled only when their required source facts are present. In
particular, capital return, mark-to-market excursion, latency, market-depth, and
other execution-quality measures are unavailable unless the retained history
contains their actual inputs. The dashboard does not substitute unrelated values.

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
