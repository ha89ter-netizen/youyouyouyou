# Testnet telemetry smoke-test report

## Verdict

The live Testnet process produced valid, run-scoped research telemetry and the read-only validator returned `status=ok`. Two natural trades opened, both directions were exercised, MFE/MAE matched manual calculations exactly, restart recovery retained the same run and trade identities, and the controlled account failure blocked new entries while the open-position management path continued.

This build is **not yet approved for a 24–72 hour run**. The smoke test exposed four telemetry/supervision defects. They were fixed with regression tests, but scientific run immutability correctly prevents loading the changed source tree into this already-started run. A new short post-fix smoke test is required. Exit telemetry was also not exercised because neither natural trade closed.

No strategy signal, indicator, threshold, symbol, sizing rule, SL/TP distance, Exit Manager behavior, risk limit, or trading frequency was changed.

## Run identity and timing

| Item | Value |
|---|---|
| RUN_ID | `testnet-20260801T115641Z-582b4c1-66cd62` |
| Commit recorded at startup | `582b4c186fba89ba7609e3612e0857f0be0e3809` |
| Branch | `main` |
| Dirty worktree | `true` (the uncommitted forensic/telemetry implementation was intentionally under test) |
| Environment | `local`, hostname `MacBook-Air-Alan.local`, Python `3.9.6` |
| Trading mode | `live_testnet` |
| Testnet | `true` |
| Immutable start | `2026-08-01T11:56:41.435292+00:00` |
| Processes stopped | `2026-08-01T12:27:03.364000+00:00` |
| Durable stop marker | `2026-08-01T12:28:25.817485+00:00` |
| Active process duration | approximately 30 minutes 22 seconds |
| Durable run span | 31 minutes 44 seconds |
| Initial config hash | `d363d4d6fdc3505acc699085579a5fa87d72cbbe978310e7cf4cc646d8a20081` |
| Schema / migration | `telemetry-v1` / `2026-08-01-research-telemetry-v1` |

The requested foreground entrypoint was invoked with the local interpreter as `python3 -u live_run.py run` (`python` is not the configured command on this host).

## Preflight

| Check | Result | Evidence |
|---|---|---|
| Git identity recorded | Pass | SHA, branch, dirty flag persisted in `trading_runs` |
| Complete preflight suite | Pass | 240 tests passed before launch |
| Explicit Testnet gate | Pass | `BYBIT_TESTNET=true`; persisted `testnet=true`; Testnet REST/WS endpoints observed |
| Mainnet disabled | Pass | Startup and child-process gates force/refuse anything except Testnet; no Mainnet interaction observed |
| PostgreSQL target | Pass | Configured intended local telemetry PostgreSQL; all ten telemetry tables present |
| Migrations | Pass | Safe migrations applied idempotently; schema/migration versions persisted |
| Credentials | Pass with provenance limitation | Authenticated calls succeeded against Bybit Testnet. Secret values were neither printed nor persisted. The API cannot independently attest how a key was named outside Bybit. |
| Duplicate process check | Pass | No collector/trader process alive before launch |
| Exchange state before launch | Pass | Wallet `9882.92739793`, equity `9882.92739793`, available `9873.59791446`; zero positions and zero active orders |
| Active historical orphans | Pass | Zero blocking active orphans |
| Effective settings | Pass | Non-secret resolved settings, all configuration families, dependency/source fingerprints, environment names, config hash and startup account snapshot persisted |

## Natural trades and final exchange state

| Trade | Side | Quantity | Weighted entry | Original SL | Original TP | Status at stop |
|---|---:|---:|---:|---:|---:|---|
| ETHUSDT, trade 257 | short | 0.05 | 1865.83 | 1893.82 | 1809.87 | open |
| ADAUSDT, trade 258 | long | 346 | 0.1733 | 0.1708 | 0.1784 | open |

After stopping all local bot processes, a read-only Bybit Testnet query confirmed:

- ETHUSDT still had an untriggered `reduceOnly` StopLoss and TakeProfit at the recorded prices.
- ADAUSDT still had an untriggered `reduceOnly` StopLoss and TakeProfit at the recorded prices.
- The four native protective order IDs match the IDs stored in the latest position snapshots.
- No local collector, trader, or supervisor process remained alive.

The positions and protective orders were not changed or closed by this investigation.

## Table-by-table coverage

| Table | Rows | First UTC | Last UTC | Duplicate groups | Critical nulls / outcome |
|---|---:|---|---|---:|---|
| `trading_runs` | 1 | 11:56:41 | 11:56:41 | 0 | none |
| `run_policy_epochs` | 3 | 11:56:41 | 12:03:02 | 0 | none; see harness epoch note |
| `account_snapshots` | 19 | 11:56:41 | 12:26:24 | 0 | none; unavailable account values are intentionally NULL in one failed/stale row |
| `position_snapshots` | 73 | 11:57:54 | 12:26:59 | 0 | none; all 73 reported `protected` |
| `trade_excursions` | 2 | 12:26:59 | 12:26:59 | 0 | none; timestamp shown is `last_observed_at` |
| `trade_protection_events` | 2 | 11:57:22 | 11:57:58 | 0 | pre-fix `trade_log_id` missing; unique `order_link_id` present |
| `trade_exit_events` | 0 | — | — | 0 | not exercised: no closure |
| `decision_events` | 794 | 11:57:18 | 12:27:01 | 0 | no critical structured field NULLs |
| `rejection_events` | 664 | 11:57:18 | 12:27:01 | 0 | no critical structured field NULLs |
| `operational_health_events` | 245 | 11:57:17 | 12:27:00 | 0 | no critical structured field NULLs |

All counted rows have the exact expected `run_id`. Detailed machine-readable coverage is in `artifacts/smoke_test_table_counts.json`.

Account snapshots had a median interval of 73.89 seconds. Position snapshots had median per-symbol intervals of 36.60 seconds. The largest gaps (335 seconds account, 235/220 seconds positions) coincide with graceful restart diagnostics and transient Bybit position API failures; they are explained rather than hidden.

## Decision and rejection validation

Ten evaluated/accepted decision rows and twenty rejection rows were inspected individually.

- Every sampled committee decision had market timestamp/age, score, regime, volatility regime, trend, spread, funding, signal array, filter object, final decision/reason, config hash, and policy epoch.
- Proposed entry, SL, TP, quantity, and estimated risk are NULL at the committee phase because sizing has not happened yet. Both sampled `risk_approved` rows contained all five values plus portfolio, funding, spread, volatility, trend, freshness and RiskManager filter results.
- All twenty sampled rejections had structured `stage`, `code`, reason, policy epoch and linkage to a decision event. Committee rejections additionally contained `rejected_actions`; early data-quality/entry-guard rows legitimately had an empty optional context object because the rejection occurred before indicator construction.
- Distribution: 585 committee evaluations, 156 data-quality rejections, 39 entry-guard rejections, 8 RiskManager rejections, 3 risk-approved candidates, 2 accepted order submissions, and 1 spacing rejection.

Thus records are analytically structured, not only human-readable text.

## MFE/MAE manual verification

Definitions used by the implementation and manual check:

```text
initial risk = actual filled quantity × |weighted entry − original stop|
long MFE = max(0, highest observed price − entry)
long MAE = max(0, entry − lowest observed price)
short MFE = max(0, entry − lowest observed price)
short MAE = max(0, highest observed price − entry)
USDT = distance × quantity at the extreme
R = USDT / initial risk
```

### ETHUSDT short

- Entry 1865.83; quantity 0.05; stop 1893.82; initial risk 1.3995 USDT.
- 481 durable samples: 407 public trades plus mark/last values from 37 snapshots.
- Lowest observed price 1865.84: short MFE clamps to 0; stored MFE = 0 USDT = 0R.
- Highest observed price 1867.16 at `12:22:43.683956Z`: MAE distance 1.33; `1.33 × 0.05 = 0.0665 USDT`; `0.0665 / 1.3995 = 0.0475169703R`.
- Stored values match all manual numeric values exactly.

### ADAUSDT long

- Entry 0.1733; quantity 346; stop 0.1708; initial risk 0.865 USDT.
- 230 durable samples: 158 public trades plus mark/last values from 36 snapshots.
- High 0.1735 at `12:00:26.904Z`: MFE distance 0.0002; 0.0692 USDT; 0.08R.
- Low 0.1721 at `12:11:39.053Z`: MAE distance 0.0012; 0.4152 USDT; 0.48R.
- Stored values match all manual numeric values exactly.

The ETH stream contributed 26 samples before restart and 455 after; ADA contributed 15 before and 215 after. Extrema were not reset, and the restart consumed retained public trades from the entry timestamp where an excursion row had not yet been created. Neither excursion is finalized because both positions remained open.

These are sampling-based excursions, not tick-perfect exchange truth. Missing WebSocket messages can understate extrema; no future candle was used.

## Restart test

The initial processes stopped gracefully at `11:58:14Z`. The first same-run supervisor restart exposed a race: `.runtime/collector.json` still named the dead prior collector PID, so `_wait_for_collector` incorrectly treated the new collector as dead before it could publish its PID. Local `_prepare` also consulted PostgreSQL for resumable runs only in Railway mode.

For the live diagnostic, the same unchanged source was preserved and collector/trader children were started directly with the durable RUN_ID. PostgreSQL then proved:

- the same `trading_runs` row was used; no second immutable run was created;
- RiskManager state restored rather than resetting;
- trades 257/258, protective linkage and orderLinkIds remained unchanged;
- position snapshots resumed for both positions;
- MFE/MAE retained pre-restart observations and advanced afterward;
- account/position/event uniqueness constraints produced zero duplicate groups;
- open-position management resumed.

Two extra diagnostic process cycles are visible in the runtime timeline. They are recorded rather than omitted.

The code now always discovers active durable runs from PostgreSQL in both local and Railway modes. Collector readiness ignores a stale same-run PID unless it equals the newly spawned expected PID. Regression tests cover both stale-PID acceptance and real new-process death.

### Policy epoch harness artifact

During restart diagnosis, the wrapper instantiated telemetry once before applying the child process's `TRADING_ENABLED=true` override. That appended epoch 1 (`TRADING_ENABLED=false`) even though the spawned trader used `true`. Epoch 2 append-only records the correction. No immutable row was overwritten.

This revealed a separate A→B→A bug: `ensure_run` compared only with epoch 0, so a return to A could be lost. It now compares with the latest epoch and appends every real transition. A regression test proves epochs `[0,1,2]` while the original run row remains unchanged.

## Controlled failure test

At `12:02:16Z`, a safe in-process stub made only the account/wallet reader raise `RuntimeError("controlled wallet telemetry smoke fault")`. It did not alter networking, exchange orders, positions, or protective orders.

Observed results:

- new-entry execution did not occur;
- management calls continued through pending recovery, SL/TP management, trailing management, closure reconciliation, and ETH Exit Manager evaluation;
- an account snapshot was persisted with `fetch_status=failed`, `is_stale=true`, account values NULL, and the exception type/message;
- an `account_fetch_failure` health event was persisted with `new_entries_allowed=false` and `position_management_continued=true`;
- the first later successful account snapshot at `12:06:45.977099Z` was fresh and reported both positions.

Result: **pass**.

## Protection and exit lifecycle

Protection coverage during the run:

- two `initial_protection_created` events were persisted;
- 73/73 position snapshots were `protected`;
- each latest position snapshot contained the two actual Bybit SL/TP order IDs;
- a post-stop read-only exchange query agreed with snapshot state.

The smoke run exposed two event-level attribution defects: the initial events had `trade_log_id=NULL`, and their singular `exchange_order_id` held the entry order ID rather than the later-observable native SL/TP IDs. Future protection event persistence now resolves the internal trade by unique orderLinkId and emits one state-deduplicated `exchange_protection_acknowledged` event containing all native protection IDs/statuses/trigger prices. Tests prove linkage and no repeated event on later identical snapshots.

No protection modification or final trigger occurred. No position closed, so `trade_exit_events`, final MFE/MAE, fees, funding and closing execution linkage are **not exercised**.

## Operational health and stale data

Durable events recorded by the tested source:

| Type | Count |
|---|---:|
| `restart_recovery` | 3 |
| `account_fetch_failure` | 1 |
| `position_fetch_failure` | 2 |
| `stale_orderbook` | 103 |
| `stale_trade_flow` | 136 |

One account snapshot was stale/failed; no position snapshot was stale. The two position failures were temporary Bybit retryable timestamp/API errors and later cycles recovered.

A real pybit `ping/pong timeout` at `12:16:46Z` reconnected by `12:17:12Z`. Because the disconnect was shorter than the 120-second watchdog, the tested source placed it only in the durable platform/file log, not `operational_health_events`. This was a concrete gap. A post-smoke logging bridge now mirrors pybit disconnect, reconnect-attempt and connected messages into PostgreSQL without changing reconnection behavior. Its regression test passes; it still requires live post-fix verification.

## Validator

Command:

```bash
python3 -u tools/validate_run_telemetry.py \
  --run-id testnet-20260801T115641Z-582b4c1-66cd62 \
  --format markdown \
  --output docs/telemetry_validation_report.md
```

Result: `status=ok`; zero checked duplicates; two trades/two excursions; no closed trade missing an exit event; zero non-protected position snapshots. A JSON copy is in `artifacts/smoke_test_validator_summary.json`.

## Defects and fixes

| Defect confirmed by smoke | Smallest implemented fix | Regression coverage |
|---|---|---|
| Local restart did not use durable active run identity | Discover active run from PostgreSQL in every runtime mode | durable restart identity/state test plus existing Railway coverage |
| Stale same-run collector PID caused readiness race | Bind dead-process detection to the newly spawned expected PID | stale prior-run PID, stale same-run PID, and expected-new-PID death tests |
| A→B→A policy change lost the return epoch | Compare effective config with latest policy epoch | immutable epoch sequence `[0,1,2]` test |
| Protection event missed internal trade link/native IDs | Resolve `trade_log_id` by orderLinkId; emit state-deduplicated exchange acknowledgement | linkage, native ID payload and repeated-snapshot dedupe test |
| Short pybit transport failure was log-only | Mirror pybit disconnect/reconnect lifecycle into durable health events | transport lifecycle handler test |

Historical smoke rows were not rewritten or hidden.

## Tests

- Before launch: **240 tests passed**.
- After the first four fixes: targeted suites passed.
- After the final WebSocket durability fix: targeted suites passed **27/27**.
- Final complete suite after all fixes: **244 tests passed in 8.968 seconds**.
- Known non-failing environment warning: urllib3 reports that system Python uses LibreSSL 2.8.3 rather than OpenSSL 1.1.1+.

## Readiness and next action

Current rating: **not yet ready for a 24–72 hour run**.

Reasons:

1. The corrected source has not been exercised in a new live immutable run.
2. WebSocket transport event durability and native protection acknowledgement were fixed after the run and therefore have only automated coverage.
3. Exit lifecycle was not naturally exercised.
4. Two still-open Testnet positions exist. Starting a new isolated run would correctly fail the clean-exchange precondition; they must close naturally or be handled through a separately approved operational decision.

Required next step: after the existing positions are no longer open, perform one new 10–20 minute Testnet smoke run on the corrected source. Confirm a single durable run, supervisor restart, at least one native protection acknowledgement, and a safely observed/replayed WebSocket health event. Do not begin the 24–72 hour experiment until that passes.

## Artifacts

- `docs/telemetry_validation_report.md`
- `artifacts/smoke_test_table_counts.json`
- `artifacts/smoke_test_consistency_checks.json`
- `artifacts/smoke_test_mfe_mae_examples.json`
- `artifacts/smoke_test_runtime_events.csv`
- `artifacts/smoke_test_validator_summary.json`
