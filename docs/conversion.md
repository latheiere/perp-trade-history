# Reporting-currency conversion

The dashboard compares cashflows only after converting them into one reporting
currency. Collection performs this conversion automatically and stores the result
alongside the original cashflow.

## Choose the reporting currency

Open `config/config.toml` and set:

```toml
[reporting]
target_currency = "USDT"
conversion_method = "previous_day.close"
```

`target_currency` is the unit used for dashboard performance and comparable text or
JSON reports. Original cashflow amounts and currencies remain retained.

## Choose the daily price

| Setting | Daily price used for an event |
| --- | --- |
| `previous_day.open` | Previous UTC day's opening price |
| `previous_day.close` | Previous UTC day's closing price |
| `current_day.open` | Event UTC day's opening price |
| `current_day.close` | Event UTC day's closing price |

`previous_day.close` is the default and is the simplest choice for routine
collection because the preceding daily candle is already complete. A current-day
close cannot be used until that UTC day has finished.

## Add a fixed fallback

Some settlement currencies do not have a usable public spot pair. If you know the
auditable fixed relationship that should be used, add it explicitly:

```toml
[reporting.fixed_rates]
INTERNAL_SETTLEMENT_ASSET = "1"
```

Use fixed rates only when that relationship is intentional. The configured value is
applied to each relevant UTC day and becomes part of the stored conversion basis.

## Apply a configuration change to existing history

Changing the target currency, daily price selector, or fixed fallback does not
silently rewrite previous results. Recalculate after saving the new configuration:

```bash
.venv/bin/perp-trade-history \
  --config config/config.toml \
  recalculate-conversions
```

This uses public spot-price endpoints and does not require account credentials. Add
`--venue` when you want to rebuild only one configured source.

## Resolve unavailable comparable results

If the dashboard can show an episode but cannot show a comparable result:

1. Run `make history` to retry collection and conversion.
2. Run `.venv/bin/perp-trade-history --config config/config.toml doctor`.
3. Confirm that the event currency has a public conversion pair or an intentional
   fixed fallback.
4. Recalculate conversions if reporting settings changed after collection.

The dashboard leaves a result unavailable when no defensible conversion exists. It
does not replace the missing rate with an unrelated market price.
