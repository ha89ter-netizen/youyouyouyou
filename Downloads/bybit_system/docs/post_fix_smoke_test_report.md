# Post-fix Testnet telemetry smoke test

## Executive result

The live Testnet exit path was exercised successfully under RUN_ID `testnet-20260801T124936Z-582b4c1-490021`. Two new DOGEUSDT positions opened naturally and trade 259 closed naturally through its exchange-native stop loss. The close produced exactly one durable exit event, one closure row, one final protection trigger and a complete deterministic order/execution chain. Manual weighted-price, fee, P&L, R, MFE and MAE calculations match PostgreSQL and Bybit with zero numerical difference.

The validator reports `ok` for both the new and previous smoke runs. However, this smoke test also exposed two reliability defects that were fixed after the immutable run stopped: missing retry of normalized exchange-order evidence after a transient order-history gap, and cross-run mutation of an inherited position's SL/TP. The first defect was backfilled from read-only Bybit history; the second is preserved in telemetry and was not manually reversed. Because the corrected source fingerprint itself has not run in a live process, this report does **not** approve the 24–72 hour run yet. A short final smoke of the corrected tree is required.

No signal, indicator, threshold, sizing, symbol, SL/TP distance, risk limit, trading frequency or normal same-run Exit Manager rule was changed.

## Preflight

| Check | Result |
|---|---|
| No local bot process before launch | Pass |
| Previous smoke process stopped | Pass |
| Mainnet disabled / `BYBIT_TESTNET=true` | Pass |
| PostgreSQL reachable and migrations idempotently applied | Pass |
| API credentials present and accepted by Bybit Testnet | Pass; secrets were neither printed nor persisted |
| Git branch / SHA | `main` / `582b4c186fba89ba7609e3612e0857f0be0e3809` |
| Worktree | Dirty; recorded immutably as `dirty_worktree=true` with source hash `0e31c344a5d82a1e54733f85097e472fae0c5e5127bb0940fd6d35f47d1e6ec6` |
| Schema / migration | `telemetry-v2` / `2026-08-01-cross-run-attribution-v2` |
| Full tests before launch | 248 passed |
| Existing positions | ETHUSDT short trade 257 live and protected; ADAUSDT trade 258 no longer live and pending deterministic reconciliation |

The immutable record contains one policy epoch, config hash `5975454bfb28e7678bfbbd1fb8d115a959e3fa58ad1687eefa5c9a210cb79052`, local deployment identity `MacBook-Air-Alan.local`, Python 3.9.6 and Testnet trading mode.

## Previous-run position handling

The intended owner/processor model was implemented before launch:

- the RUN_ID that opened a trade remains its immutable owner;
- a later process can observe and reconcile an inherited position;
- `processing_run_id` records which process observed or finalized it;
- snapshots, protection events, excursions and exit events remain scoped to the owner RUN_ID;
- ambiguous symbol/direction matches remain unresolved rather than being attached heuristically.

At startup, ETH trade 257 was classified `inherited_live_protected`; ADA trade 258 was classified `inherited_pending_reconciliation`. ADA's natural Testnet TP close was then recorded exactly once under the previous RUN_ID with the new RUN_ID only as processor. No old trade was reassigned and no new trade was attached to the old run.

One ownership defect was observed: at 12:57:27 UTC the current processor applied the frozen time-range tightening rule to inherited ETH trade 257, moving SL/TP from 1893.82/1809.87 to 1878.67/1836.70. This was not hidden, deleted or manually rolled back. The fix now makes inherited positions monitor/reconcile-only for a later RUN_ID: native protection remains active, while cross-run trailing, time tightening and Exit Manager mutation are suppressed. Same-run behavior is unchanged.

## Runtime and restart

- Start: 2026-08-01 12:49:36.344845 UTC.
- Stop: 2026-08-01 13:01:13.522141 UTC.
- Wall-clock duration: 11 minutes 37 seconds.
- New trades: 2 DOGEUSDT longs (trade IDs 259 and 260).
- Natural closes: 1 (trade 259, native SL).
- Graceful restart: pass. The same RUN_ID and policy epoch were resumed, RiskManager durable state was restored, and no snapshot, closure, protection or decision duplicate was introduced.
- Operational health: WebSocket reconnect/recovery, stale order-book/trade-flow marking and restart recovery were persisted. There were no stale account or position snapshots.

The previous controlled-wallet failure test was not repeated because this post-fix task required exit/cross-run coverage; the already-confirmed fault path was unchanged. No unsafe network fault was injected.

## Table coverage

| Table | Rows | First UTC | Last UTC | Logical duplicates | Critical nulls |
|---|---:|---|---|---:|---:|
| trading_runs | 1 | 12:49:36.344845 | 12:49:36.344845 | 0 | 0 |
| run_policy_epochs | 1 | 12:49:36.344845 | 12:49:36.344845 | 0 | 0 |
| account_snapshots | 8 | 12:49:36.344845 | 13:00:39.660479 | 0 | 0 |
| position_snapshots | 14 | 12:50:52.440716 | 13:00:04.803107 | 0 | 0 |
| trade_excursions | 1 | 13:00:38.988137 | 13:00:38.988137 | 0 | 0 |
| trade_protection_events | 4 | 12:50:18.305086 | 13:00:42.361441 | 0 | 0 |
| trade_exit_events | 1 | 13:00:38.988137 | 13:00:38.988137 | 0 | 0 |
| decision_events | 365 | 12:50:15.524136 | 13:00:42.388430 | 0 | 0 |
| rejection_events | 306 | 12:50:15.524136 | 13:00:40.394324 | 0 | 0 |
| operational_health_events | 108 | 12:49:36.344845 | 13:01:50.228620 | 0 | 0 |

Trade 260 opened only 31 seconds before shutdown. Entry and both native protective IDs are durable, but the first periodic position/excursion sample was not due before cutoff. It is correctly reported as missing rather than reconstructed from future data. The health-event timestamp after stop is a disclosed post-stop idempotency diagnostic, not a live bot cycle.

## Natural exit reconstruction: DOGEUSDT trade 259

### Identity and protection

- Owner and processor RUN_ID: `testnet-20260801T124936Z-582b4c1-490021`.
- Internal trade ID: 259, long, quantity 1401 DOGE.
- Entry order: `c351cf25-9ea8-4e8a-9558-7ddfeb6be0b4`.
- Entry orderLinkId: `decision_e-8843699ac2d649aa`.
- Entry execution: `d7221e18-a007-567e-8947-410b3bbdac97`.
- Weighted entry: 0.07139.
- Original SL/TP: 0.07030 / 0.07351.
- SL order: `b7e70ac4-fbe3-439f-8d24-f1305372257d` (Filled).
- TP order: `fbb6b5cf-6e55-4a95-999f-a4e1c99d7113` (Deactivated after SL).
- Both protective orders have parentOrderLinkId `decision_e-8843699ac2d649aa`.
- Initial creation, exchange acknowledgement and final trigger are structured and durable.

### Exit and accounting

- Exchange mechanism / structured reason: `StopLoss` / `SL`.
- Requested reason: NULL, correctly, because no internal Exit Manager close was requested.
- Closing execution: `feb0a30d-0b21-5090-9522-7589d654b7f2`.
- Fill: 1401 at 0.07030, taker (`isMaker=false`).
- Entry fee: 0.05500957 USDT.
- Exit fee: 0.05416967 USDT.
- Funding: unavailable at trade scope in the Bybit response and stored NULL.
- Closed: 2026-08-01 13:00:11.063 UTC.

Manual calculations:

```text
weighted entry = (0.07139 × 1401) / 1401 = 0.07139
weighted exit  = (0.07030 × 1401) / 1401 = 0.07030
initial risk   = (0.07139 − 0.07030) × 1401 = 1.52709 USDT
gross P&L      = (0.07030 − 0.07139) × 1401 = −1.52709 USDT
fees           = 0.05500957 + 0.05416967 = 0.10917924 USDT
net P&L        = −1.52709 − 0.10917924 = −1.63626924 USDT
realized R     = −1.63626924 / 1.52709 = −1.0714949610042692 R
```

All differences against PostgreSQL and Bybit are exactly 0. Exactly one `trade_closures` row, one `trade_exit_events` row and one `final_trigger` event exist. Two repeated reconciliations left all three counts and P&L unchanged.

## Manual MFE/MAE verification

For the long position, favorable distance is `max(sample_price − entry, 0)` and adverse distance is `max(entry − sample_price, 0)`.

- Best sampled price: 0.07137 at 12:50:19.434 UTC. It did not exceed entry, so MFE distance, percentage, USDT and R are all 0.
- Worst sampled price: 0.07030 at 13:00:11.063 UTC.
- MAE distance: 0.00109.
- MAE percentage: `0.00109 / 0.07139 × 100 = 1.5268244852220112%`.
- MAE USDT: `0.00109 × 1401 = 1.52709`.
- MAE R: `1.52709 / 1.52709 = 1.0R`.
- Time to MFE/MAE: 1 second / 592 seconds.

Every stored/manual difference is 0. Values are finalized and unchanged after repeated reconciliation. The known limitation remains polling plus received public trades: a WebSocket gap can understate an intratrade extreme; future candles are never used.

## Defects and fixes

1. **Normalized order-evidence retry missing.** If Bybit order history was empty in the closure cycle, `trade_exchange_orders` was never retried after status became `closed`. A recent/current-run idempotent backfill now combines historical orders with active protective orders and keys every row by immutable exchange order ID. Read-only backfill restored entry/SL/TP linkage for trades 257–260 without duplicates.
2. **Cross-run mutation guard missing.** The later processor tightened inherited ETH protection. Ownership guard now suppresses cross-run trailing, time tightening and Exit Manager closure while continuing snapshots, health checks and deterministic reconciliation. No exchange rollback was attempted.
3. **Datetime JSON serialization in finalized MFE/MAE** was found before this run and fixed before launch; the natural DOGE exit proved the fix live.

Regression coverage includes transient order-history recovery, active protective-order backfill, duplicate backfill calls, inherited time-tightening/trailing suppression and inherited Exit Manager suppression.

## Tests and validator

- Before launch: 248 tests passed.
- After all fixes: `python3 -m unittest discover -s tests` → **252 tests in 8.283s, OK**.
- New RUN_ID validator: `ok`.
- Previous RUN_ID validator: `ok`.
- Owner mismatches: 0; critical duplicates: 0; closed trades without exit event: 0.

## Current exchange/process state

The bot is stopped. No collector or trader process is alive. Two protected Testnet positions remain on the exchange and were not manually closed:

- ETHUSDT short trade 257, previous RUN_ID, qty 0.05, native reduceOnly SL/TP active at 1878.67/1836.70.
- DOGEUSDT long trade 260, current RUN_ID, qty 1423, native reduceOnly SL/TP active at 0.06918/0.07233.

## Recommendation

**Not yet approved for an unattended 24–72 hour run.** The exit telemetry itself passes, but the final source tree includes reliability changes made after the immutable smoke run. Run one short corrected-source smoke with no threshold changes, specifically verifying that inherited SL/TP remain byte-for-byte unchanged across several management cycles and that normalized order evidence is written without a post-stop backfill. If that passes, the evidence supports proceeding to the 24–72 hour Testnet run.

No Mainnet interaction occurred, and no strategy optimization was performed.
