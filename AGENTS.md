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

## Development checks

Install development dependencies and run the standard checks with:

```bash
python -m pip install -e ".[dev]"
python -m pytest
ruff check .
```

Before publishing documentation, check Markdown links, confirm every command against
the current CLI, and remove stale implementation or future-development narrative
from public guides.

When reporting a defect, include the affected system area, triggering conditions,
observed behavior, sanitized error, and relevant environment version. Never include
credentials, signatures, signed URLs, account identifiers, or concrete retained
trading identifiers in development prose.
