# Operations

The core package supports interactive collection and reporting without a service
manager, backup library, or private local module. The workflows below are optional.

## Collection and coverage

The first successful collection backfills to the configured start or the oldest
history exposed by each endpoint. Later runs overlap prior coverage and upsert changed
records. `collect --full` revisits all currently reachable REST history without
deleting stored data.

Coverage is explicit. Missing, failed, symbol-scoped, and retention-limited intervals
are not treated as complete. Primary signed cashflows are included in PnL by default;
non-PnL transfers and supplemental summaries require explicit report flags.

## Archive ingestion

Supported asynchronous export datasets can extend history beyond REST retention:

```bash
perp-trade-history --config config/config.toml archives backfill-step
perp-trade-history --config config/config.toml archives status --json
perp-trade-history --config config/config.toml archives import \
  --kind trades --file /path/to/export.zip
```

Archive polling and import invoke the same conversion pass as normal collection.
Original files are retained with content-addressed names; signed download URLs are
not persisted.

## Scheduling

`weekly-collect` exposes a persistent UTC schedule gate suitable for an external
supervisor. `runtime-contract.yaml` describes the optional integration surface.
Neither is required for manual commands.

An overlapping singleton collection is reported as a successful skip because the
active writer retains exclusive ownership of the data directory.

## Backup and restore

Signed backup commands require the optional backup dependency:

```bash
python -m pip install ".[backup]"

perp-trade-history --config config/config.toml backup \
  --output /path/to/history.tar.gz
perp-trade-history verify-backup /path/to/history.tar.gz
```

Restore is staging-first. Inspect the restored tree before replacing active data:

```bash
perp-trade-history restore-backup /path/to/history.tar.gz \
  --target /path/to/empty/staging-directory
```

The package imports and all collection/report commands work when the backup extra is
not installed.

## Diagnostics

```bash
perp-trade-history --config config/config.toml status --json
perp-trade-history --config config/config.toml doctor
perp-trade-history --verbose --config config/config.toml collect
```

Routine logs go to standard output; warnings and errors go to standard error.
Credentials, signatures, and signed query strings are excluded from logs and archive
metadata.

## Storage guarantees

Normalized tables use stable source identities so repeated collection and archive
import are idempotent. Raw API and import records remain available for audit and
renormalization. Collection does not delete canonical or retained source records.

Only one process should write a data directory at a time. Reports and status commands
are read-only.
