# Telemetry validation

```json
{
  "consistency": {
    "excursion_count_lte_trade_count": true,
    "exit_event_count_lte_closed_trade_count": true
  },
  "coverage": {
    "account_snapshots": 8,
    "closed_trades": 1,
    "decision_events": 365,
    "health_events": 108,
    "open_trades": 1,
    "position_snapshots": 14,
    "protection_events": 4,
    "rejection_events": 306,
    "trade_excursions": 1,
    "trade_exit_events": 1,
    "trades": 2
  },
  "cross_run": {
    "exit_owner_mismatches": 0,
    "inherited_exit_events_processed": 1,
    "inherited_position_snapshots_processed": 15,
    "inherited_protection_events_processed": 5,
    "position_owner_mismatches": 0
  },
  "duplicates": {
    "account_snapshot_bucket": 0,
    "decision_event_key": 0,
    "position_trade_bucket": 0,
    "protection_event_key": 0
  },
  "missing_data": {
    "closed_trades_without_exit_event": [],
    "trades_without_excursion": [
      "decision_e-c32999083a3b441b"
    ]
  },
  "protection": {
    "non_protected_position_snapshots": 0
  },
  "run_metadata": {
    "config_hash": "5975454bfb28e7678bfbbd1fb8d115a959e3fa58ad1687eefa5c9a210cb79052",
    "dirty_worktree": true,
    "git_branch": "main",
    "git_commit_sha": "582b4c186fba89ba7609e3612e0857f0be0e3809",
    "migration_version": "2026-08-01-cross-run-attribution-v2",
    "policy_epochs": 1,
    "run_id": "testnet-20260801T124936Z-582b4c1-490021",
    "schema_version": "telemetry-v2",
    "started_at": "2026-08-01T12:49:36.344845+00:00",
    "stopped_at": "2026-08-01T13:01:13.522141+00:00",
    "strategy_version": "frozen-current",
    "testnet": true
  },
  "stale_data": {
    "account_snapshots": 0,
    "health_event_types": {
      "inherited_live_protected": 1,
      "inherited_pending_reconciliation": 1,
      "restart_recovery": 3,
      "stale_orderbook": 43,
      "stale_trade_flow": 56,
      "websocket_connected": 2,
      "websocket_reconnect_attempt": 2
    },
    "position_snapshots": 0
  },
  "status": "ok"
}
```
