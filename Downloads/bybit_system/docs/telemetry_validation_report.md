# Telemetry validation

```json
{
  "consistency": {
    "excursion_count_lte_trade_count": true,
    "exit_event_count_lte_closed_trade_count": true
  },
  "coverage": {
    "account_snapshots": 19,
    "closed_trades": 0,
    "decision_events": 794,
    "health_events": 245,
    "open_trades": 2,
    "position_snapshots": 73,
    "protection_events": 2,
    "rejection_events": 664,
    "trade_excursions": 2,
    "trade_exit_events": 0,
    "trades": 2
  },
  "duplicates": {
    "account_snapshot_bucket": 0,
    "decision_event_key": 0,
    "position_trade_bucket": 0,
    "protection_event_key": 0
  },
  "missing_data": {
    "closed_trades_without_exit_event": [],
    "trades_without_excursion": []
  },
  "protection": {
    "non_protected_position_snapshots": 0
  },
  "run_metadata": {
    "config_hash": "d363d4d6fdc3505acc699085579a5fa87d72cbbe978310e7cf4cc646d8a20081",
    "dirty_worktree": true,
    "git_branch": "main",
    "git_commit_sha": "582b4c186fba89ba7609e3612e0857f0be0e3809",
    "migration_version": "2026-08-01-research-telemetry-v1",
    "policy_epochs": 3,
    "run_id": "testnet-20260801T115641Z-582b4c1-66cd62",
    "schema_version": "telemetry-v1",
    "started_at": "2026-08-01T11:56:41.435292+00:00",
    "stopped_at": "2026-08-01T12:28:25.817485+00:00",
    "strategy_version": "frozen-current",
    "testnet": true
  },
  "stale_data": {
    "account_snapshots": 1,
    "health_event_types": {
      "account_fetch_failure": 1,
      "position_fetch_failure": 2,
      "restart_recovery": 3,
      "stale_orderbook": 103,
      "stale_trade_flow": 136
    },
    "position_snapshots": 0
  },
  "status": "ok"
}
```
