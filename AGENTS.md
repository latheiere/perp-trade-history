# Maintainer instructions

## Documentation audience

Treat `README.md` and every file under `docs/` as product documentation for someone
who may know Python and trading but does not know this repository.

- Lead with the outcome a command or screen provides.
- Give the shortest safe command path before optional details.
- Explain interface labels in the language displayed by the product.
- Prefer task headings such as “Open the dashboard” and “Restore a backup.”
- Keep commands copyable from a source checkout unless the section explicitly covers
  a managed installation.
- State required permissions and destructive boundaries next to the relevant action.
- Do not put implementation rationale, proposed extensions, internal component
  choices, or contributor workflow in public user guides.
- Keep maintainer architecture and quality guidance in this file or an explicitly
  maintainer-only document such as `design-qa.md`.
- Preserve the existing documentation structure unless a new user task cannot fit an
  existing guide.

## Product boundaries

- Collection uses read-only account access and must not require order, transfer, or
  withdrawal permissions.
- Collected source records and canonical normalized records remain durable across
  report, dashboard, and application-release changes.
- Repeated collection and archive import remain idempotent by stable source identity.
- The dashboard remains read-only and derives analytics from retained normalized
  records.
- Source coverage, episode-boundary completeness, and trading performance remain
  distinct concepts.
- Metrics require retained inputs that support their definition. Add source data
  before adding a dependent metric.
- Episode reconstruction and attribution remain deterministic and auditable.
- Ambiguous events remain visible through quality flags rather than being hidden by
  an unsupported estimate.

## Dashboard architecture

- Keep the dashboard as a scrollable Dash, Mantine, and Plotly application.
- Reuse the in-memory analytical snapshot for interactive filters and invalidate it
  when canonical data files change.
- Use coordinated filters for summary-to-episode drill-downs.
- Virtualize high-cardinality evidence grids and avoid mounting inactive heavy
  panels.
- Format values for display without discarding machine-readable numeric values.
- Keep outcome colors independent from position direction.

## Development checks

Install development dependencies and run the standard checks with:

```bash
python -m pip install -e ".[dev]"
python -m pytest
ruff check .
```

Dashboard changes must also follow `design-qa.md`. Before publishing documentation,
check Markdown links, confirm every command against the current CLI, and remove stale
implementation or future-development narrative from public guides.

When reporting a defect, include the affected system area, triggering conditions,
observed behavior, sanitized error, and relevant environment version. Never include
credentials, signatures, signed URLs, account identifiers, or concrete retained
trading identifiers in development prose.
