# Optional extensions

The core local workflow is `make bootstrap` followed by `make first-run`. The
extensions below are useful when the local history should be backed up or refreshed
without an interactive command.

## Signed backups

Signed backup commands are available through the optional backup dependency:

```bash
python -m pip install ".[backup]"
perp-trade-history --config config/config.toml backup \
  --output /path/to/history.tar.gz
perp-trade-history verify-backup /path/to/history.tar.gz
```

Restore is staging-first so retained data can be inspected before an operator
chooses whether to replace an active tree. See [operations](operations.md) for the
full backup and restore workflow.

## Supervised refresh and dashboard service

`runtime-contract.yaml` describes the optional scheduled collector and loopback
dashboard service for a local supervisor. The scheduled workload advances ordinary
API history; historical archive workflows remain operator-initiated.

See [operations](operations.md) for scheduling, health checks, and process behavior.
