# Optional extensions

The normal local workflow needs only `make bootstrap`, `make history`, and
`make dashboard`. Add the options below when you want backups or unattended local
operation.

## Keep signed backups

Install backup support:

```bash
.venv/bin/python -m pip install ".[backup]"
```

Create a backup:

```bash
.venv/bin/perp-trade-history --config config/config.toml backup \
  --output /path/to/history.tar.gz
```

Verify it later with:

```bash
.venv/bin/perp-trade-history verify-backup /path/to/history.tar.gz
```

See [operations](operations.md#back-up-collected-data) for restore instructions.

## Refresh data without opening a terminal

The `weekly-collect` command runs one ordinary API refresh for each eligible weekly
schedule window. A local scheduler or service manager can invoke it repeatedly; it
does not start historical archive exports.

```bash
.venv/bin/perp-trade-history \
  --config config/config.toml \
  --secrets config/credentials.env \
  weekly-collect
```

If another collection process already owns the data directory, the overlapping run
exits successfully without starting a second writer.

## Keep the dashboard available locally

Run the dashboard without opening a new browser window:

```bash
.venv/bin/perp-trade-history-dashboard \
  --config config/config.toml \
  --host 127.0.0.1 \
  --port 8050 \
  --no-browser
```

The local readiness endpoint is `/readyz`. The dashboard is read-only and can run
independently of collection.

## Use a managed local installation

`runtime-contract.yaml` describes the collector and dashboard processes for a local
installation manager such as `tradier-dev`. A managed upgrade can replace the
application release while preserving configuration, credentials, collected data,
and backups.

Use the installation manager's bootstrap, profile, build, and apply commands rather
than mixing managed and source-checkout processes against the same data directory.
