# Research telemetry and immutable run design

## Scope and invariants

This design adds scientific observability to the frozen Testnet strategy. It does not change signals, voting, indicators, thresholds, sizing, SL/TP distances, risk limits, frequency, symbol selection, or Exit Manager rules. Exchange-mutating calls remain confined to `ExecutionEngine`, and Mainnet protection remains unchanged.

The system separates three kinds of state:

1. **Scientific state (PostgreSQL):** immutable run identity, effective policy epochs, account/position snapshots, excursions, decisions, rejections, protection and exit lifecycle, health events, journal, fills/order evidence and RiskManager state.
2. **Liveness state (PostgreSQL):** mutable service heartbeat/PID fields in `run_metadata`. These are observations, not process locks or scientific identity.
3. **Ephemeral process state:** Unix PIDs, file locks, WebSocket sessions, in-memory buffers and `.runtime/*.json`. These are recreated after a restart and are never the sole source of research evidence.

## Before telemetry-v1

The repository already persisted:

- candles, public trades, funding, open interest, liquidations and top-of-book snapshots;
- `trade_log` entry/exit journal, entry/exit snapshots, expert votes and selected exchange IDs;
- deterministic exchange-ID-first close reconciliation, closure records, execution evidence and protective-order evidence;
- RiskManager daily P&L, limits, pending entries, blocked symbols and circuit-breaker causes;
- basic `run_metadata` with run ID, commit SHA, start time, service PIDs and heartbeats;
- rotating local logs and Railway stdout/stderr.

It did not persist periodic account/equity or position paths, MFE/MAE, normalized candidate rejections, complete SL/TP lifecycle events, structured final exits, policy epochs or most operational failures. Basic `run_metadata.environment_summary` was not a complete effective configuration and had historically been mutable. Local logs could therefore contain the only evidence for some events.

## Schema

Migration `2026-08-01-research-telemetry-v1` is additive and idempotent. It creates ten tables and run-scoped indexes without rewriting `trade_log` or any historical row:

| Table | Purpose | Idempotency/uniqueness |
|---|---|---|
| `trading_runs` | Immutable scientific identity and startup manifest | `run_id` primary key |
| `run_policy_epochs` | Effective configuration changes | unique `(run_id, epoch)` |
| `account_snapshots` | Periodic account/equity/exposure state | unique `(run_id, snapshot_bucket)` |
| `position_snapshots` | Periodic open-position/protection state | unique `(run_id, trade_log_id, snapshot_bucket)` |
| `trade_excursions` | Restart-safe MFE/MAE | unique trade ID and orderLinkId |
| `trade_protection_events` | Append-only SL/TP/trailing lifecycle | deterministic `event_key` |
| `trade_exit_events` | Structured final closure | one per trade ID/orderLinkId |
| `decision_events` | Structured evaluations and decision stages | deterministic `event_key` |
| `rejection_events` | Normalized rejection facts | deterministic `event_key` |
| `operational_health_events` | API/data/WS/heartbeat/recovery facts | deterministic `event_key` |

Historical values that did not exist remain `NULL`; the migration never manufactures them. The machine-readable schema description is in `artifacts/telemetry_schema_manifest.json`.

## Immutable run identity

`live_run.py` creates the run ID, commits the existing supervisor row, assigns the resolved `RUN_ID` and commit to the in-memory configuration, and then creates `trading_runs` before spawning collector/trader.

Epoch zero captures:

- strategy version, git SHA/branch and dirty flag;
- source-tree SHA-256, Python version and installed-dependency fingerprint;
- runtime/deployment mode and startup hostname/container identifier;
- Testnet/trading mode, enabled symbols and all timeframes;
- all resolved dataclass settings grouped into strategy, risk, exit and filter documents;
- allow-listed environment-variable names and non-secret values;
- schema/migration versions and startup account snapshot;
- a canonical SHA-256 of the complete effective settings document.

Secrets are replaced with `***`. A database URL is parsed and rendered with its password hidden. Arbitrary environment variables are not collected.

The original row is never updated except that an explicit operator stop may set `application_stopped_at` once. A configuration change appends a `run_policy_epochs` row containing the full new effective document and a field-level diff. A source-tree, commit, Python-version or dependency-fingerprint change refuses resume under the same run ID; it cannot silently masquerade as the original run.

Railway restarts may reuse the active run only when the durable run is active for the same commit and the immutable fingerprints still match. Local `.runtime/current_run.json` is only a convenience hint.

## Account snapshots

Default cadence is 60 seconds (`TELEMETRY_ACCOUNT_INTERVAL_SEC`). The Bybit V5 unified wallet response provides wallet balance, equity, available balance, perpetual unrealized P&L, cumulative realized P&L where present, margin balance, initial/used margin and maintenance margin. Missing exchange fields stay `NULL`.

The service reconstructs open-position count, gross long/short notional, net exposure, high-water equity and drawdown. Every row labels its source, fetch status, source timestamp and stale flag. A failed fetch produces a durable failed/stale row when PostgreSQL is available and a health event; it is never converted to a zero balance.

The Bybit `cumRealisedPnl` field is account/coin cumulative, not run-scoped. Run-scoped realized P&L remains reconstructible from `trade_exit_events`/exchange closure evidence and is not falsely labelled as the wallet field.

## Position snapshots

Default cadence is 30 seconds (`TELEMETRY_POSITION_INTERVAL_SEC`). For each exchange position that maps to a durable open `trade_log` row, the snapshot stores quantity, actual exchange average entry, mark/last price, unrealized P&L/R, original/current SL and TP, risk/distances, age, market-data age, protection status/order IDs, Exit Manager trigger state, entry regime and volatility regime.

Snapshot buckets make writes idempotent and restart-safe. An exchange position without an internal trade is not guessed onto a row; it creates an unresolved health event. Incomplete SL/TP creates a protection anomaly event.

## MFE and MAE

The reference entry is the actual weighted average entry reported for the filled position. Initial risk is:

```text
initial_risk_usdt = actual_filled_quantity × abs(weighted_entry - original_stop)
```

For a long at entry `E` and observed price `P`:

```text
favorable_distance = max(0, P - E)
adverse_distance   = max(0, E - P)
```

For a short:

```text
favorable_distance = max(0, E - P)
adverse_distance   = max(0, P - E)
```

For either side:

```text
excursion_pct  = price_distance / E × 100
excursion_usdt = price_distance × quantity_at_observation
excursion_R    = excursion_usdt / initial_risk_usdt
```

The tracker consumes only public trades already persisted with timestamps between entry and the observation, plus contemporaneous polled mark/last prices. At closure it incorporates persisted public trades only through the confirmed close timestamp and actual closing execution/average-exit observations. It never reads a future candle and never uses a candle high/low that became known after closure.

The maximum price excursion, price/time, quantity at observation, percentage, USDT/R representation, time-to-extreme, maximum polled unrealized profit/loss and TP/SL crossings are persisted incrementally. A restart continues from the durable last market timestamp and existing extrema. A fast trade with no periodic snapshot is finalized from its bounded public-trade path and closing executions.

### Sampling limitation

This is research-grade for received public trades, but it is not an exchange-certified complete tick archive. A WebSocket/collector gap can miss the true intra-gap high/low and understate MFE/MAE. Poll-derived unrealized values can also miss extrema between polls. Quantity-at-extreme is exact at a position snapshot; if quantity changes between samples, the exact intermediate exposure is unavailable and is explicitly described in `sampling_limitations`. No value is fabricated to hide this limitation.

## Decision and rejection events

Each symbol evaluation receives a unique evaluation ID. Structured events preserve:

- durable market timestamp/age and freshness checks;
- every expert output, confidence, reason, expected RR and ignored status;
- confirmation families, committee score, regime, trend, spread and funding;
- trend/freshness/portfolio/risk filter results;
- proposed entry/SL/TP/quantity/risk when the candidate reaches that stage;
- final decision and normalized rejection stage/code/reason;
- policy epoch, commit SHA and config hash.

Insufficient or stale data is itself a rejected evaluation. Later stages (risk approval, ranking, anti-burst, re-check and exchange submission) append separate phase events instead of overwriting the committee decision.

## Protection and exits

Initial protection, missing protection, rejected/successful tightening, trailing activation and final exchange trigger are appended to `trade_protection_events`. Events contain old/new structured values, exchange IDs when available, source module, reason, outcome, safe raw status and policy epoch.

`trade_exit_events` records the journal reason, requested Exit Manager reason, exchange-observed mechanism, all closing order/execution IDs, realized P&L, fees, realized R and finalized MFE/MAE. Trade-scoped funding remains `NULL` when Bybit closed-PnL cannot isolate it; `closedPnl` is retained exactly and no funding amount is invented.

Static SL/TP creation and amendment support `LastPrice` and `MarkPrice` through
`PROTECTIVE_TRIGGER_BY`; `LastPrice` remains the default. Read-back must observe
both child IDs and the configured trigger source before protection is verified.
Bybit trailing stop does not expose the same configurable trigger source in the
used V5 endpoint, so this option does not silently claim to alter trailing
semantics. Protective exits persist intended trigger, source, nearest retained
mark/last observation, weighted fill, absolute/%/R slippage and a
normal/elevated/anomalous classification. Bybit history does not provide a
certified trigger timestamp, so the near-trigger market observation is a
fill-time proxy and is labeled as such in raw evidence.

## Failure isolation and health telemetry

The prior top-of-cycle control flow fetched wallet balance before positions. A wallet failure raised out of `run_once` and skipped all position management. The flow now:

1. fetches positions;
2. resolves pending entries;
3. manages time-range protection and trailing;
4. reconciles exchange closures;
5. snapshots positions/excursions;
6. independently fetches account state;
7. evaluates open-position Exit Manager paths even if wallet state failed;
8. allows new entries only when wallet/risk state is available.

This changes failure isolation, not an exit rule. If the position endpoint itself fails, the cycle fails closed because there is no safe exchange position state.

Durable health events cover REST refresh failures, WS callback failures, stale WS/reconnect/recovery, stale candles, account/position/protective-order fetch failures, restart recovery and heartbeat failures/recovery. Critical decision, protection, exit and health commands use a PostgreSQL write-ahead outbox with deterministic event keys, bounded exponential retry and dead-letter status. A bounded in-memory queue is used only for the narrow case where PostgreSQL itself cannot accept the outbox row; stderr remains the last-resort evidence if both the process and database fail before recovery. New entries fail closed while durable storage is unavailable.

Collector reconnect uses bounded exponential backoff with jitter, a stable
connection reset window and a finite degradation budget. Exhausting the budget
exits the collector so `live_run.py run`/Railway can restart the supervised
container. During a WS outage, closed strategy candles are backfilled from the
last durable boundary via REST. This does not manufacture healthy order-book or
public-trade flow state, so entry freshness checks remain fail-closed.

Heartbeat errors no longer kill the heartbeat thread; it retries and records failure/recovery through the telemetry callback when storage permits.

## Retention and reconstruction

Research-critical facts are durable PostgreSQL rows and are never subject to automatic retention. Bounded retention applies only to raw high-frequency `trades`, `orderbook_snapshots`, `liquidations`, `funding_rate` and `open_interest`; raw data at or after the oldest still-open trade is retained for excursion reconstruction. Before older funding/OI ticker samples are deleted, minute count/min/max/average aggregates are durably inserted into `funding_rate_minute_rollups` and `open_interest_minute_rollups`. Deletes are batched and run on a background cadence. PostgreSQL capacity is monitored and new entries fail closed before the configured quota threshold. Railway platform logs and local rotating files remain useful diagnostics but are not required for reconstruction. Railway filesystem files, PIDs, locks, WebSocket objects and in-memory buffers are intentionally ephemeral.

Delivered outbox envelopes are not the audit record: the normalized target
rows are. Confirmed deliveries older than the configured retention window are
deleted in bounded batches on a background maintenance thread. Pending,
dead-letter and unconfirmed rows are never deleted. Metrics retain
pending/delivered/dead-letter counts and oldest-pending age.

## Validation

Run the read-only validator after a smoke test:

```bash
python -u tools/validate_run_telemetry.py --run-id <RUN_ID> --format markdown --output docs/telemetry_validation_report.md
```

It reports run metadata, snapshot/MFE/MAE/protection/decision coverage, missing data, stale periods, health events, duplicates and consistency checks. It does not run migrations or call the exchange.

## Approved smoke-test start procedure

Do not start until credentials, database and an exchange-clean state have been independently confirmed. For Railway, use the repository root `Downloads/bybit_system` and:

```bash
python -u live_run.py run
```

Required Railway variables:

```text
RUNTIME_MODE=railway
BYBIT_TESTNET=true
BYBIT_API_KEY=<Bybit Testnet key>
BYBIT_API_SECRET=<Bybit Testnet secret>
DATABASE_URL=${{Postgres.DATABASE_URL}}
STORAGE_MAX_DATABASE_BYTES=<PostgreSQL volume quota in bytes>
RAILWAY_DEPLOYMENT_DRAINING_SECONDS=30
```

Optional automatic operator notifications require
`TELEGRAM_ALERTS_ENABLED=true`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID`.
The credentials are secret environment-only values and are not captured in
immutable metadata. The supervisor exposes non-secret `GET /healthz` and
`GET /status` endpoints on Railway's `PORT`.

Railway supplies `RAILWAY_GIT_COMMIT_SHA`; otherwise set `COMMIT_SHA`. `OPENAI_API_KEY` is required only when the frozen current runtime uses the OpenAI analyst. Optional telemetry-only cadence settings are `TELEMETRY_ACCOUNT_INTERVAL_SEC=60` and `TELEMETRY_POSITION_INTERVAL_SEC=30`.

For a supervised local smoke test, load the same Testnet variables with `RUNTIME_MODE=local` and run:

```bash
python3 live_run.py start
python3 live_run.py status
```

Safe local shutdown is:

```bash
python3 live_run.py stop
```

The requested smoke test was not started by this implementation task.
