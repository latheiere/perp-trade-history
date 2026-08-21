# Using the dashboard

The dashboard turns collected account history into one scrollable report. Use it to
compare periods, find recurring patterns, and inspect the executions and cashflows
behind a result.

## Open the dashboard

From a source checkout, run:

```bash
make dashboard
```

This opens the dashboard using history already stored in the configured data
directory. It does not need account credentials and does not collect new records.

To collect history first and then open the page, run:

```bash
make first-run
```

For a demonstration without account data:

```bash
.venv/bin/perp-trade-history-dashboard --synthetic
```

## Start with the top filters

The controls at the top apply to the whole dashboard:

- **Profile** selects an account source.
- **Market class** selects the available instrument category.
- **Date range** limits episodes by their opening date.
- **Aggregation interval** groups performance by month, quarter, or year.
- **Timezone** changes displayed timestamps, weekday grouping, and time-of-day
  grouping.

Available choices and date boundaries come from collected history. If a selection
contains no matching episodes, clear or widen the filters.

## Read the executive overview

The first row summarizes the current selection:

- **Attributed cashflow** is the comparable trading cashflow assigned to closed
  episodes in the configured reporting currency.
- **Closed episodes** counts reconstructed positions with an observed end.
- **Win rate** is the share of closed episodes with positive attributed cashflow.
- **Median duration** is the median observed holding time of closed episodes.

The performance chart offers four views:

| View | What it shows |
| --- | --- |
| **Cumulative** | Running attributed cashflow from zero inside the selected horizon |
| **Period** | Cashflow earned or lost in each displayed period |
| **Rolling** | Sum of the latest three displayed periods |
| **Drawdown** | Decline from the highest cumulative cashflow reached inside the horizon |

Use **1Y**, **2Y**, **3Y**, or **All** to change the visible horizon. Shortened
horizons start from zero and do not include earlier periods in cumulative or rolling
values.

**Notable changes** compares sufficiently populated recent periods. Each item shows
the direction and number of episodes used. When the current selection does not
contain enough comparable periods, the dashboard reports that directly.

## Explore patterns

### Weekday and hour

The heatmap shows average comparable cashflow for episodes opened in each weekday
and two-hour interval. Green cells are positive, red cells are negative, and color
strength reflects the size of the average result.

Select a cell to filter the episode ledger to that exact weekday and time interval.
The active pattern appears above the ledger. Use **Clear pattern focus** to return to
the broader selection.

### Direction and exposure duration

This section compares episode count and average result across direction and holding
time. Colors describe outcome, not direction: green is positive and red is negative.

Select any duration row under Long, Short, or Net to drill into the matching
episodes. A Long or Short row also sets the direction filter.

## Compare years

Each annual card summarizes closed episodes for one calendar year in the selected
timezone:

- episode count;
- attributed net cashflow;
- win rate;
- cashflow distribution; and
- average observed holding time.

Use the top filters to compare the same profile or market class across years.

## Inspect episodes

The episode ledger is the bridge between summary analytics and source evidence.
You can:

- filter by direction, duration, or outcome;
- search by instrument, status, or quality tag;
- sort and filter individual columns; and
- select a row to open its details below the ledger.

An episode is a reconstructed period of continuous exposure. It can contain many
executions and several attributed cashflows.

The detail tabs provide:

- **Overview** — result, entry, exit, duration, direction, status, and tags;
- **Executions** — execution-price timeline and the complete linked execution list;
- **Cashflows** — attributed cashflow chart and the complete linked cashflow list;
- **Quality** — boundary status, source coverage, execution count, and order count.

Large execution and cashflow lists are virtualized, so scrolling remains responsive
without omitting records.

## Understand episode tags

| Tag | What it means | What to keep in mind |
| --- | --- | --- |
| **Ambiguous Tie Order** | Opposing executions share the same timestamp and the source does not provide their sequence. | Episode boundaries and entry or exit allocation may depend on a stable fallback order. |
| **Mixed Quantity Units** | Linked executions use different quantity units. | Quantity and exposure totals may not be directly comparable. |
| **Left Censored** | A close was observed without the preceding opening inventory. | The true entry, opening time, and full duration are unavailable. |
| **Right Censored** | The episode remains open at the latest analytical boundary. | Exit and final outcome are not yet available. |
| **No Flags** | No episode-level reconstruction warning was detected. | Source coverage is still reported separately. |

An episode can be both left- and right-censored when its opening is outside available
history and it remains open at the latest boundary.

## Check data quality

Data quality appears at the bottom of the page so it does not obscure performance
analysis.

- **Source interval coverage** shows how many selected episode intervals are covered
  by complete stored collection intervals.
- **Reconstructed boundaries** shows how many episodes have an observed beginning
  and end.
- **Open episodes** and **left-censored episodes** explain incomplete boundaries.
- **Latest build** shows when the dashboard last recognized a source-data change.

Open the **Collection build report** to see exact uncovered intervals, their profile,
market class, acquisition method, and recorded reason. It reports stored facts and
does not guess why an unrecorded interval is missing.

## Refresh the data

Run the following command whenever you want to revisit all history currently
available through the configured APIs:

```bash
make history
```

An open dashboard detects changed data files and refreshes automatically. Your
filters remain interactive while unchanged analytical data is reused in memory.

For older history outside ordinary API retention, follow the
[historical archive guide](binance-archive-backfill.md).

## What the calculations use

The dashboard uses normalized executions and attributable trading cashflows. It does
not predict performance or fit a machine-learning model. A metric appears only when
the collected records provide the required inputs; unsupported estimates are not
substituted.

All dashboard activity is read-only. Opening the page, filtering, and drilling down
cannot place or modify orders.
