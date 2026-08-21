# Maintainer dashboard design QA

This is an internal acceptance checklist for dashboard changes. User instructions
belong in `README.md` and `docs/`.

## Experience target

- Preserve an ordinary scrollable page instead of compressing every section into one
  viewport.
- Keep global filters compact and available before the analytical content.
- Present executive performance, patterns, annual comparison, episode evidence, and
  data quality in that reading order.
- Use green and red for positive and negative outcomes; do not encode direction as
  outcome.
- Keep blue for interaction and selection.
- Prefer readable spacing, restrained borders, and stable alignment over maximum
  information density.
- Keep the page usable at the supported desktop breakpoint and allow natural
  stacking on narrower screens.

## Interaction checks

- Global filters update all affected sections and retain a clear selected state.
- Performance horizons rebase cumulative, rolling, and drawdown views to the visible
  boundary.
- Heatmap cells and exposure-duration rows drill into matching episodes.
- Clearing a pattern restores the broader episode selection.
- Episode search, column filters, sorting, pagination, and row selection remain
  responsive.
- High-activity episodes do not mount unbounded evidence rows or inactive charts.
- Execution and cashflow tabs expose every linked record through virtualized grids.
- Empty, unavailable, low-confidence, open, and censored states use explicit wording.
- Price axes and hover values retain precision appropriate to magnitude.
- Hover labels remain legible against the dark background.

## Data checks

- Synthetic data renders every designed section without relying on a particular
  retained history.
- Retained local history renders multi-period performance, patterns, annual cards,
  episodes, details, and quality states.
- Filters and drill-downs preserve the exact selected episode set.
- Only metrics supported by retained inputs appear.
- Source coverage and episode-boundary completeness remain separate from performance.
- The collection build report identifies exact stored coverage gaps without inferred
  causes.

## Release checks

- Run `python -m pytest` and `ruff check .`.
- Confirm `/readyz` returns HTTP 200.
- Check the dashboard at the supported desktop viewport and one narrower viewport.
- Exercise a high-activity episode and each detail tab.
- Confirm a fresh browser session has no warnings or errors.
- Store new visual evidence under `artifacts/` only when it is needed for a review.
