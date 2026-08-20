# Currency conversion

Currency conversion is a post-collection pass, not report work. Normal collection,
archive ingestion, and manual historical recalculation invoke the same conversion
flow.

## Flow

For each unconverted cashflow, the pass:

1. resolves the configured reporting currency and price selector;
2. loads the required venue-native daily spot candle;
3. stores the unique rate by venue, base currency, reporting currency, and UTC date;
4. stores the converted amount by canonical cashflow record ID and conversion
   specification.

The canonical cashflow schema does not contain rates or converted values. This keeps
collection records stable while allowing conversion data to be rebuilt independently.

## Price selectors

| Selector | Price used for an event |
| --- | --- |
| `previous_day.open` | Previous UTC candle open |
| `previous_day.close` | Previous UTC candle close |
| `current_day.open` | Event-date UTC candle open |
| `current_day.close` | Event-date UTC candle close |

`previous_day.close` is the bootstrap default because the candle is complete for
activity collected during the current UTC day. A current-day close is unavailable
until its candle completes, so same-day conversion can fail when that selector is
used.

## Stored sidecars

`normalized/conversion_rates.csv` stores one daily rate for each required conversion
key. `normalized/cashflow_conversions.csv` stores converted amounts without expanding
the canonical cashflow table.

Reports validate the stored conversion specification and cashflow fingerprint before
aggregation. They do not read the rate table directly and never call a market-data
endpoint.

## Fixed fallbacks

A settlement asset without a usable public spot pair can use an explicit fixed rate:

```toml
[reporting.fixed_rates]
INTERNAL_SETTLEMENT_ASSET = "1"
```

The pass materializes the fixed value for each relevant UTC day in the rate table.
Fixed fallbacks are part of the conversion specification, so adding or changing one
requires recalculation.

## Historical recalculation

Changing `target_currency`, `conversion_method`, or a fixed fallback does not
silently reinterpret stored values. Collection and reporting surface the stale
specification. After reviewing the new configuration, rebuild conversions explicitly:

```bash
perp-trade-history --config config/config.toml recalculate-conversions
```

The command uses public spot endpoints and does not require account credentials. Use
`--venue` to bound the rebuild to one adapter.
