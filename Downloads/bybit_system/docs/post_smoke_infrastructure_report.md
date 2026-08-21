# Post-smoke infrastructure and execution-safety report

## Scope

This stage used run `testnet-20260818T191954Z-9cb4120-c67920` only as read-only
research input. No bot was started, no Bybit order was created/amended/cancelled,
and no strategy signal, threshold, sizing rule, universe, initial SL/TP distance
or Exit Manager rule was changed.

## Infrastructure fixes

### WebSocket recovery

- bounded exponential reconnect delay: 5s initial, 60s maximum by default;
- symmetric configurable jitter;
- no zero-delay retry path;
- backoff resets only after messages have flowed for a stable window;
- a finite degradation budget causes the collector to exit so the Railway
  supervisor/platform restarts it instead of spinning forever;
- repeated health events are deduplicated/rate-limited and the next durable
  event records the suppressed count;
- recovery is a distinct durable event.

### REST candle recovery

The collector reads the last durable primary-timeframe candle and requests the
whole detected gap (bounded to 1000 rows) from REST. Only closed candles are
upserted. REST recovery is explicitly not treated as recovery of order book,
public trade flow, funding or open interest. Consequently, entries remain
blocked when any required non-candle input is stale.

### PostgreSQL and outbox

- entry durability remains fail-closed when PostgreSQL is unavailable or above
  its configured capacity threshold;
- management/reconciliation of existing positions happens before wallet and
  new-entry gates;
- outbox retry is bounded and dead-letters after the configured attempt budget;
- cleanup deletes only old rows with `status=delivered` and non-null
  `delivered_at`;
- pending/dead-letter/unconfirmed rows are never cleaned;
- cleanup is one bounded batch and runs off the trading-cycle thread;
- metrics include pending, delivered, failed/dead-letter and oldest-pending age.

The Railway PostgreSQL instance previously returned `database system is in
recovery mode`. Code cannot safely repair that server. Before any Railway smoke
test an operator must:

1. keep trading services stopped;
2. inspect Railway Postgres logs/volume health and create a backup/export if the
   server becomes readable;
3. connect with `railway connect Postgres` and verify
   `SELECT pg_is_in_recovery(), now(), pg_database_size(current_database());`;
4. require `pg_is_in_recovery() = false` and successful read/write in the
   intended Testnet database;
5. if Railway cannot recover it, restore into a new Postgres service from a
   verified backup; never destroy the old service before backup/retention is
   confirmed;
6. point `DATABASE_URL` to the healthy private service and run
   `python -m storage.init_db` once; migrations are additive/idempotent;
7. run the complete tests and telemetry validator before enabling a smoke test.

## Protection and execution safety

The existing default remains `LastPrice`. `PROTECTIVE_TRIGGER_BY=MarkPrice` is
now supported for static long/short SL and TP at entry and during amendment.
Post-entry and post-amendment read-back verifies position prices, both child
order IDs and trigger source. Missing protection is checked against a fresh
position read before an alarm is emitted, preventing a natural-close race from
being classified as unprotected exposure.

Terminal protection evidence is now explicit and idempotent:

`protection_requested → exchange_acknowledged → child_ids_observed → verified_active → triggered/filled/cancelled/superseded/position_closed/reconciled`.

No watchdog automatically creates or replaces missing orders.

For exchange-confirmed protective exits, telemetry records intended trigger,
trigger source, nearest retained mark/last values, actual weighted fill,
absolute/%/R slippage, order/execution IDs and classification. A classified
`anomalous` fill creates a sticky durable RiskManager cause. It blocks future
entries only; it does not cancel or alter existing protection. Reset remains an
explicit operator action after reconciliation.

Recommendation for the next separately approved short Testnet smoke test:
use `MarkPrice` for static SL/TP because the completed run demonstrated isolated
LastPrice spikes while MarkPrice remained near the position. This is not a
claim that MarkPrice eliminates market-order slippage, and it does not change
Bybit trailing-stop trigger semantics. The default was intentionally not
changed in code.

## Read-only breakeven replay conclusion

The complete numerical report is in
`docs/breakeven_counterfactual_report.md`. Baselines reproduced:

- actual 26 trades: **−21.6458 USDT**;
- excluding five confirmed anomalous fills: **−5.1329 USDT** (21 trades);
- five anomalous fills normalized to the latest intended stop with declared
  fee assumptions: **−8.8199 USDT**.

No tested breakeven threshold made any sample profitable. The superficially
best actual-run results at `+0.25%`/`+0.25R` are not robust: they damaged seven
of eight winners and sharply reduced Profit Factor. `+0.50%` was worse than the
actual baseline. The least unstable neighborhood was approximately
`+0.75%–1.00%` / `+0.75R–1.00R`; it produced only small improvements, remained
negative, and is far too weak to authorize live activation on 26 trades.

Replay assumptions/limitations:

- ordered trajectory: 36,816 retained public LastPrice/position-last samples;
- all 26 trades had replayable ordered samples (minimum 37 per trade);
- trade-scoped funding was unavailable for all 26 and was explicitly assumed
  zero;
- expected exit fee rate was 0.055%;
- expected normal slippage buffer was 2 bps;
- historical tick size was inferred from retained price increments, not an
  exchange-certified metadata snapshot;
- collector gaps can understate activation/return events;
- the replay never exits at MFE and never uses future observations to choose an
  activation time.

## Behaviour-impact inventory

Potentially affects real execution only when exercised:

- `PROTECTIVE_TRIGGER_BY=MarkPrice` changes exchange trigger semantics, but the
  default remains `LastPrice`;
- an anomalous protective fill now blocks new entries with a sticky breaker;
- stricter protection read-back blocks new entries while child/source state is
  unverified;
- collector degradation eventually restarts the supervised service;
- REST-recovered closed candles may restore candle freshness, but other stale
  inputs continue to block entries.

Telemetry retention, event deduplication, lifecycle finalization, replay tools
and the natural-close race fix do not alter signals or order prices.

## Tests

- baseline before this stage: **325 passed**;
- focused infrastructure/protection/replay regression group: **131 passed**;
- final complete suite: **359 passed, 0 failed, 1 environment warning**;
- warning: local Python links `urllib3` to LibreSSL 2.8.3 rather than OpenSSL
  1.1.1+; it did not fail a test but should be removed by the Railway image's
  supported Python/OpenSSL build.

Coverage added includes reconnect failure/recovery/backoff reset/no-spin, REST
gap recovery, health suppression counters, restart-safe delivered-only outbox
cleanup, background cleanup scheduling, both static trigger sources, read-back,
normal/elevated/anomalous protective slippage, sticky entry halt invocation,
terminal lifecycle idempotency, natural-close/missing-protection race, and
deterministic long/short percent/R breakeven calculations with fees, funding,
slippage, tick rounding and insufficient-data handling.

## Readiness

Code readiness for a short controlled local Testnet smoke test: **85/100**.
Railway readiness remains blocked until PostgreSQL exits recovery mode and the
operator completes the checks above. A 24–72 hour run is not approved by this
stage.
