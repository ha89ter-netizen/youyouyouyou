# Forensic execution and journal reconciliation report

## Executive summary

This investigation covered Testnet run `testnet-20260728T072254Z-806f25f-544400` from the first internal entry at 2026-07-28 07:23:26 UTC through the read-only exchange snapshot at 2026-08-01 10:37:16 UTC. The snapshot was explicitly verified as Bybit Testnet and has SHA-256 `5d611a3faab65d732ce19eb7264bf22b3b25e8fb179397a1de72fc86f4ecda55`.

The five priority losses and all eleven trades below −1.5R are genuine Bybit Testnet stop-loss executions. They are not fabricated journal values, not duplicate closed-PnL rows, and not the result of a missing or stale stop. For every one of the eleven:

- the opening exchange order and execution exist;
- the filled opening quantity equals the total closed quantity;
- the closing order has `stopOrderType=StopLoss`;
- the exchange trigger equals the intended current SL (original or tightened);
- the journal exit order ID, average exit, fees, P&L, quantity, side, and close time agree with the exchange closed-PnL record;
- every close fill is a taker fill.

The large realized R values came from Testnet stop execution far beyond the stop trigger. The most compelling Testnet anomaly is the pair of SOL longs, trades 153 and 168: two different protective order IDs, two different sets of 13 execution IDs, and close times 50 minutes apart received exactly the same 13-price fill ladder and therefore exactly the same weighted exit `67.49230769`. That cannot be explained by duplicate journal reconciliation.

One separate accounting defect was definitively demonstrated. Internal ETH trade 109 was closed by two protective order IDs at almost the same instant: 0.04 ETH and 0.01 ETH. The old matcher selected only the 0.04 record, so the journal omitted the second closed-PnL record of `−0.05368396 USDT`. The corrected aggregate P&L is therefore `−75.84521036 USDT`, not the journal's `−75.79152640 USDT`.

The five priority trades lost `−29.14700681 USDT`, or 38.43% of corrected run loss. All eleven trades below −1.5R lost `−44.90390397 USDT`, or 59.20% of corrected run loss.

## Evidence standard and limitations

Four labels are used throughout:

- **Confirmed**: directly present in PostgreSQL and/or raw Bybit order, execution, closed-PnL, transaction-log, or wallet responses.
- **Reconstructed**: deterministic arithmetic over confirmed records, such as weighted average price, fee totals, or funding attribution within a non-overlapping position interval.
- **Inferred**: a conclusion supported by evidence but not explicitly stated by an exchange field.
- **Unavailable**: outside retention, absent from retained logs, or not exposed by Bybit.

Sources inspected:

- all 169 `trade_log` rows for the run and their entry/exit identifiers, prices, timestamps, fees, protection values, exit snapshots, and tightening fields;
- 170 Bybit closed-PnL records;
- 501 Bybit execution records, comprising 434 trade executions and 67 settlement/funding executions;
- regular and conditional Bybit order history for all 19 configured symbols;
- 501 transaction-log rows and the wallet snapshot;
- retained `trading.log*`, `main.log*`, self-check and risk-admin logs;
- all order submission, protection, reconciliation, journal, restart, orphan, and migration code;
- the official Bybit V5 documentation for [order history](https://bybit-exchange.github.io/docs/v5/order/order-list), [execution history](https://bybit-exchange.github.io/docs/v5/execution/execution), [closed PnL](https://bybit-exchange.github.io/docs/v5/position/closed-pnl), and [transaction log](https://bybit-exchange.github.io/docs/v5/account/transaction-log).

The earliest retained rotating runtime logs begin after several suspicious trades. Their detailed application log messages are unavailable. This does not prevent execution reconstruction because the exchange IDs, fills, trigger prices, fees, quantities, and times remain present in raw Bybit evidence and PostgreSQL.

No exchange state was changed. No order was submitted, cancelled, amended, or closed. No new run was started. The historical journal rows were not rewritten.

## Exact implementation before the fix

### Trade creation and opening order

`ExecutionEngine.open_position` refreshed the Testnet ticker, rounded quantity down to the instrument lot step, generated an `orderLinkId` of the form `<sanitized-source-first-10>-<16-hex-uuid>`, and submitted a market order with attached `stopLoss` and `takeProfit`. The unique internal opening `orderLinkId` was persisted in `trade_log.order_link_id`. After confirmation, `trade_log.exchange_entry_order_id` held Bybit's opening `orderId`, while the actual average entry and filled notional replaced the candle-price estimate.

The entry order identity was strong. The missing fields were the explicitly requested quantity and confirmed filled quantity; those could only be reconstructed from order/execution history.

### Position association

The bot uses Bybit one-way linear positions (`positionIdx=0`). There is no durable exchange position UUID. A live position was associated by symbol, side, and the invariant that the risk gate should allow at most one live position per symbol. Internal position occupancy was represented by an open `trade_log` row.

### Protective orders

Initial SL/TP were attached to the opening order. Bybit generally exposed their protective order IDs later in conditional order history, often with `parentOrderLinkId` equal to the opening `orderLinkId`.

When protection was tightened through `set_trading_stop`, the response did not provide durable child order IDs. Bybit sometimes cancelled the original parent-linked protection and created a replacement with an empty `parentOrderLinkId`. The journal stored the new trigger prices and tightening time, but not the old/new protective order IDs or lifecycle. This was an observability and future-linkage defect.

### Exit Manager

An Exit Manager close used a unique `orderLinkId` of the form `exit_manag-close-<12-hex-uuid>` and a reduce-only market order. Before this investigation, the accepted exit `orderId` and exit `orderLinkId` were not persisted immediately. Only the decision snapshot (`exit_trigger`) was stored. A restart between order acceptance and later reconciliation therefore lost the strongest direct exit linkage.

### Execution and closed-PnL ingestion

Executions were indexed by closing `orderId` only to infer exit reason from `stopOrderType` or the bot's Exit Manager prefix. The first execution of an order was used for reason inference. All fills remained available at the exchange but were not normalized into the journal.

The prior live matching algorithm operated independently for every unresolved trade:

1. fetch closed-PnL rows for the symbol since the oldest unresolved opening;
2. require closing side opposite the internal position;
3. require `updatedTime` (or fallback `createdTime`) not earlier than internal opening;
4. require `avgEntryPrice` within 0.5% of the journal entry;
5. sort by nearest entry price, then earliest closing time;
6. select the first candidate.

It did **not** use `exchange_entry_order_id`, opening `orderLinkId`, protective `parentOrderLinkId`, closing quantity, submitted Exit Manager identifiers, or a global consumed-record set. Each internal trade was evaluated independently. Consequently:

- one closed-PnL record could theoretically be assigned to two internal trades;
- multiple closed-PnL records belonging to one position were not aggregated;
- an incomplete partial close could prematurely close the internal row;
- idempotence protected repeated closure of the same internal row, but did not enforce global uniqueness of exchange closing order IDs.

No duplicated `exchange_exit_order_id` actually exists in this historical run. The defect was latent for duplicate assignment, but it manifested as missing partial-close accounting in trade 109.

### Restart and orphan reconciliation

On restart, PostgreSQL open/orphaned rows were reconciled against live positions and the same price/time closed-PnL heuristic. API errors did not count as negative evidence. After three successful API cycles with no live position and no match, a trade became `orphaned` and armed a sticky circuit breaker. Legacy orphans outside the seven-day retention window were separately classified and excluded without weakening protection for future orphans.

## Identifier map

| Entity | Before fix | Reliability | After fix |
|---|---|---:|---|
| Internal trade | unique `trade_log.order_link_id`; integer `trade_log.id`; `run_id` | Strong | Unchanged |
| Opening order | opening `orderLinkId`; `exchange_entry_order_id` | Strong | Also persist requested and filled quantity |
| Position | symbol, side, `positionIdx=0`, open journal row | Moderate; no exchange position UUID | Unchanged exchange semantics |
| Protective orders | trigger prices; parent link available only from transient history | Incomplete | Persist order ID, parent link, type, status, trigger, quantities, prices, timestamps, raw payload |
| Executions | grouped transiently by `orderId`; `execId` not persisted | Incomplete | Persist every close order record plus its execution payloads |
| Exit Manager order | generated close `orderLinkId`, not persisted immediately | Incomplete | Persist accepted exit `orderId` and `orderLinkId` before reconciliation |
| Closed-PnL | one selected `orderId` in `trade_log.exchange_exit_order_id` | Incorrect for multi-order closure | One `trade_closures` row per unique exchange closing `orderId`; aggregate all rows for the trade |

## Trades below −1.5R

Initial risk is reconstructed as `filled notional × abs(entry − original SL) / entry`. R therefore measures realized exchange closed P&L against the original journal-estimated risk. It intentionally does not substitute the later tightened stop in the denominator.

| ID | Symbol | Side | Open UTC | Close UTC | P&L | Initial risk | R | Effective SL | Avg exit | Adverse slippage from SL | Exit fills |
|---:|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| 90 | BNBUSDT | Short | 2026-07-28 07:24:34 | 2026-07-28 07:29:45 | −1.3273 | 0.6880 | −1.929 | 571.9 | 579.2625 | 128.7 bps | 7 |
| 127 | XRPUSDT | Long | 2026-07-29 19:05:56 | 2026-07-29 19:21:51 | −3.8840 | 1.5012 | −2.587 | 1.0695 | 1.0448 | 230.9 bps | 1 |
| 152 | BNBUSDT | Short | 2026-07-30 01:16:21 | 2026-07-30 01:24:45 | −2.5393 | 1.5640 | −1.624 | 581.2 | 586.3 | 87.7 bps | 1 |
| 153 | SOLUSDT | Long | 2026-07-30 01:17:00 | 2026-07-30 05:07:38 | −7.8376 | 1.4300 | −5.481 | 73.15 | 67.49230769 | 773.4 bps | 13 |
| 162 | BNBUSDT | Short | 2026-07-30 04:15:47 | 2026-07-30 04:32:00 | −3.5739 | 1.2070 | −2.961 | 579.3 | 592.58235295 | 229.3 bps | 2 |
| 164 | XRPUSDT | Long | 2026-07-30 04:17:37 | 2026-07-30 05:15:07 | −2.4363 | 1.2196 | −1.998 | 1.0611 | 1.04919881 | 112.2 bps | 10 |
| 168 | SOLUSDT | Long | 2026-07-30 05:31:37 | 2026-07-30 05:57:38 | −8.0539 | 1.1570 | −6.961 | 72.72 | 67.49230769 | 718.9 bps | 13 |
| 200 | BNBUSDT | Long | 2026-07-31 08:17:35 | 2026-07-31 10:05:46 | −3.8301 | 1.4400 | −2.660 | 587.0 | 568.4 | 316.9 bps | 1 |
| 213 | DOGEUSDT | Short | 2026-07-31 10:59:54 | 2026-07-31 11:00:41 | −3.7382 | 1.5110 | −2.474 | 0.07052 | 0.07199 | 208.5 bps | 1 |
| 235 | ETHUSDT | Short | 2026-08-01 00:00:05 | 2026-08-01 10:14:39 | −2.1418 | 1.4245 | −1.504 | 1877.59 | 1902.62 | 133.3 bps | 1 |
| 247 | SOLUSDT | Short | 2026-08-01 04:04:42 | 2026-08-01 05:05:18 | −5.5415 | 1.4300 | −3.875 | 73.61 | 77.23 | 491.8 bps | 11 |

Primary classification for all eleven: **Testnet data anomaly**. This classification does not deny the executions: they are confirmed genuine Testnet account executions. It states that the extreme LastPrice-triggered fills, while MarkPrice remained on the non-trigger side, and especially the repeated deterministic SOL ladders are not credible evidence of production-market liquidity.

There is no evidence of missing/failed SL, stale trigger after modification, wrong trade attribution, duplicate closed-PnL ingestion, quantity mismatch, fee error, timezone error, or restart defect in these eleven rows.

The complete machine-readable lifecycle for all eleven—including every execution ID, timestamp, price, quantity, fee, protection record, funding amount and discrepancy—is in `artifacts/suspicious_trade_reconciliation.csv`.

## Detailed reconstruction of the five priority trades

### Trade 168 — SOLUSDT long, −8.05388815 USDT, −6.961R

**Confirmed opening.** Internal opening was 2026-07-30 05:31:37.825 UTC. Bybit opening order `93b463fe-4157-4b6f-b6d3-993698d4cc39`, link `decision_v-ffec13a5b69a4cee`, filled 1.3 SOL at 73.61 in execution `0d05b94b-704a-5f9d-b960-4730b5b9d187`. Opening fee was 0.05263115 USDT.

**Confirmed protection.** Original SL was 72.72 and TP was 75.44. No tightening was recorded. Protective order `e523af2c-c0df-49fb-adeb-71b0508c7bda` had `parentOrderLinkId=decision_v-ffec13a5b69a4cee`, `stopOrderType=StopLoss`, trigger 72.72, reduce-only/close-on-trigger semantics, and filled the entire 1.3 SOL.

**Confirmed exit.** At 2026-07-30 05:57:38.493 UTC, the stop generated 13 distinct taker executions of 0.1 SOL at 64.02, 64.39, 65.20, 65.59, 66.39, 66.79, 67.58, 68.00, 68.76, 69.20, 69.95, 70.40 and 71.13. Weighted exit was 67.49230769. Exit fee was 0.048257 USDT. Closed-PnL and journal both report −8.05388815. No funding settlement occurred during this holding interval.

**Classification.** Testnet data anomaly. Identifiers, quantity and P&L are correct; the abnormal result is the Testnet fill ladder 718.9 bps beyond the stop.

### Trade 153 — SOLUSDT long, −7.83755411 USDT, −5.481R

**Confirmed opening.** Bybit order `6b6077df-4fb9-4581-a861-dafa4c411d82`, link `decision_e-e323bc0c48a94869`, filled 1.3 SOL at 73.44 in execution `6203401b-0b72-5a44-bf8b-81516363158b`. Opening fee was 0.0525096 USDT.

**Confirmed protection lifecycle.** Original SL/TP were 72.34/75.63. At 2026-07-30 02:17:29.897 UTC the journal recorded the one-time tightening to 73.15/74.79. Protective stop order `2b355826-ec4b-4f85-b718-316c267f4966` retained the correct opening parent link and shows trigger 73.15, so the tightened SL was neither stale nor missing.

**Confirmed exit.** At 2026-07-30 05:07:38.543 UTC, thirteen distinct taker executions filled 0.1 SOL each at exactly the same price sequence listed for trade 168. Weighted exit was 67.49230769. Exit fee was 0.048257 USDT. Funding was −0.00478751 USDT and is included in exchange closed P&L. Journal and exchange both report −7.83755411.

**Classification.** Testnet data anomaly. The stop was correctly tightened and triggered; the 773.4 bps adverse fill beyond the current stop caused the excess R loss.

### Trade 247 — SOLUSDT short, −5.54145020 USDT, −3.875R

**Confirmed opening.** Bybit order `21084f8b-2016-4faa-9eb4-ccc062e97508`, link `decision_e-2a26dcb472a84ef7`, filled 1.3 SOL at 73.05 in execution `8269691f-79f6-58c0-b7af-e0dc5fc8c2c8`. Opening fee was 0.05223075 USDT.

**Confirmed protection lifecycle.** Original SL/TP were 74.15/70.87. At 2026-08-01 05:04:57.037 UTC they were tightened to 73.61/71.98. Stop order `e5f89192-35cf-4950-bf89-749243c6f9cf` has the opening parent link and exact tightened trigger 73.61. The TP order was deactivated when the stop closed the position.

**Confirmed exit.** At 2026-08-01 05:05:18.115 UTC, eleven taker fills closed the position: ten fills of 0.1 SOL at 80.62, 80.03, 79.43, 78.82, 78.24, 77.62, 77.06, 76.42, 75.87 and 75.21, plus 0.3 SOL at 74.89. Weighted exit was 77.23; exit fee was 0.05521945 USDT. Journal and exchange P&L are identical.

**Classification.** Testnet data anomaly. The current stop was present and exact; 491.8 bps of adverse Testnet execution beyond it caused the loss.

### Trade 127 — XRPUSDT long, −3.88402555 USDT, −2.587R

**Confirmed opening.** Bybit order `a0bb04c5-dda9-4602-8c77-2f16ce206203`, link `decision_e-4273f314a20b451a`, filled 92.1 XRP at 1.0858. Opening fee was 0.0550012 USDT.

**Confirmed protection and exit.** Parent-linked protective order `9d0fc417-3ec5-40a1-a04b-2b4094844a9f` had stop trigger 1.0695 and filled all 92.1 XRP in one taker execution at 1.0448 on 2026-07-29 19:21:51.341 UTC. Exit fee was 0.05292435 USDT. Journal price, quantity, fee, closing order, time and P&L match the exchange.

**Classification.** Testnet data anomaly. The exchange-observable loss is genuine, but the single Testnet fill was 230.9 bps beyond the stop while the execution's reported MarkPrice was 1.0809.

### Trade 200 — BNBUSDT long, −3.83008880 USDT, −2.660R

**Confirmed opening.** Bybit order `d0ffd360-2afe-4f9e-bf94-4b4111100813`, link `decision_e-88efceb1258345da`, filled 0.16 BNB at 591.7. Opening fee was 0.0520696 USDT.

**Confirmed protection lifecycle.** Original SL/TP were 582.7/609.2. At 2026-07-31 09:17:40.410 UTC they were tightened to 587.0/600.1. The original parent-linked orders were cancelled during replacement. The final stop `14bc8dc7-72c3-466c-8df0-e376d91fe72e` has an empty parent link, as Bybit replacement orders sometimes do, but its trigger is exactly 587.0. This rules out a stale stop.

**Confirmed exit.** One taker fill closed 0.16 BNB at 568.4 on 2026-07-31 10:05:46.739 UTC. Exit fee was 0.0500192 USDT. Journal and exchange closed P&L match exactly.

**Classification.** Testnet data anomaly. The stop lifecycle was correct; the Testnet execution was 316.9 bps beyond the tightened stop while reported MarkPrice was 589.26.

## The identical SOL exit-price issue

The two SOL records are not duplicates:

| Field | Trade 153 | Trade 168 |
|---|---|---|
| Opening order | `6b6077df-4fb9-4581-a861-dafa4c411d82` | `93b463fe-4157-4b6f-b6d3-993698d4cc39` |
| Opening link | `decision_e-e323bc0c48a94869` | `decision_v-ffec13a5b69a4cee` |
| Stop order | `2b355826-ec4b-4f85-b718-316c267f4966` | `e523af2c-c0df-49fb-adeb-71b0508c7bda` |
| Stop trigger | 73.15 | 72.72 |
| Exit execution time | 2026-07-30 05:07:38.543 UTC | 2026-07-30 05:57:38.493 UTC |
| Execution IDs | 13 unique IDs | 13 different unique IDs |
| Fill-price sequence | 64.02 … 71.13 | exactly the same sequence |
| Weighted exit | 67.49230769 | 67.49230769 |

The exchange returned two independent closed-PnL rows and two independent stop orders. The exact repetition of a 13-level price ladder is strong evidence of deterministic Testnet simulator/order-book behaviour. It is not evidence of journal duplication.

## Reconstruction of the remaining six anomalies

- **Trade 90, BNB short:** stop trigger 571.9; seven taker fills from 570.5 to 586.3; weighted exit 579.2625; full 0.08 quantity; no P&L or fee discrepancy. Parent link is unavailable on the final generated stop, but trigger and quantity match.
- **Trade 152, BNB short:** stop trigger 581.2; one full 0.17 fill at 586.3; journal and exchange match. Final generated stop has no parent link but exact intended trigger.
- **Trade 162, BNB short:** parent-linked stop trigger 579.3; two fills, 0.12 at 595.2 and 0.05 at 586.3; weighted exit 592.58235295; full quantity and exact P&L match.
- **Trade 164, XRP long:** parent-linked stop trigger 1.0611; ten fills totaling 93.1 XRP, mostly near 1.0497 plus 11.1 XRP at 1.0448; weighted exit 1.04919881; exact journal/exchange agreement.
- **Trade 213, DOGE short:** final generated stop trigger 0.07052; one full 1439 DOGE fill at 0.07199 only 47 seconds after entry; exact journal/exchange agreement.
- **Trade 235, ETH short:** original 1890.11/1806.32 protection tightened to 1877.59/1835.70; parent-linked stop has exact 1877.59 trigger; one full 0.05 ETH fill at 1902.62; funding +0.01167761 USDT; exact journal/exchange agreement.

## Definitive non-priority accounting defect: trade 109

Internal trade 109 was an ETHUSDT short of 0.05 ETH at 1857.47 with SL 1885.63.

At essentially the same trigger event:

1. original parent-linked stop `000d477f-b86e-4dd3-b5d4-9c38b76b8097` filled four 0.01 executions, total 0.04 ETH, weighted exit 1906.105, closed P&L −2.02633165;
2. generated stop `436434c5-c66a-4989-b99b-ba72cd7de032` filled the remaining 0.01 ETH at 1860.84, closed P&L −0.05368396;
3. reduce-only semantics prevented an over-close, so the two records total exactly the 0.05 opening fill.

The old matcher chose the first closed-PnL candidate and transitioned the internal trade to `closed`. It never aggregated the second closing order. The journal therefore contains:

- exit quantity represented implicitly as 0.04 rather than 0.05;
- P&L −2.02633165 instead of −2.08001561;
- weighted exit 1906.105 instead of 1897.052;
- incomplete closing fee and total-fee attribution.

Primary classification: **partial-fill accounting error**. The underlying exchange behaviour—two protective orders sharing the remaining reduce-only quantity—is also Testnet-specific, but the journal should have accounted for both records.

## Aggregate accounting reconciliation

| Item | USDT | Status |
|---|---:|---|
| Journal closed P&L, 169 rows | −75.79152640 | Confirmed |
| Bybit closed P&L already linked to those 169 rows | −75.79152640 | Exact match |
| Additional Bybit partial closed-PnL record | −0.05368396 | Confirmed, trade 109 |
| Corrected Bybit closed P&L for the run | **−75.84521036** | Confirmed |
| Raw price cash flow before fees | −59.27708500 | Reconstructed from transaction log |
| Entry fees | 8.49645173 | Confirmed |
| Exit fees | 8.50057516 | Confirmed |
| Total fees | 16.99702689 | Confirmed |
| Funding | +0.42890153 | Confirmed |
| Attributable wallet balance change | **−75.84521036** | Exact |
| Open-position unrealized P&L at cutoff | 0 | Confirmed |
| Unexplained residual | **0.00000000** | Exact |

Accounting identity:

`−59.27708500 − 8.49645173 − 8.50057516 + 0.42890153 = −75.84521036`

The wallet value shown by the final snapshot also includes activity outside this run. “Attributable wallet balance change” therefore means the exact sum of this run's identified transaction-log rows, not a naive subtraction from the account's original lifetime balance.

Record counts:

- 169 internal closed trades;
- 170 exchange closed-PnL records;
- 169 exchange records directly stored by the old journal;
- one additional exchange closed-PnL record deterministically linked to trade 109;
- zero duplicate journal exit order IDs;
- zero duplicate exchange closed-PnL order IDs;
- zero internal entries without exchange entry executions;
- zero internal exits without exchange exit executions;
- 68 executions not keyed by the old journal: 67 funding settlements and the one extra trade-109 exit execution;
- zero unexplained exchange trade executions after reconstruction.

## Anomaly classification

| Scope | Primary classification | Evidence |
|---|---|---|
| Trades 90, 127, 152, 153, 162, 164, 168, 200, 213, 235, 247 | Testnet data anomaly | Real StopLoss order/fills, exact trigger, full quantity, abnormal execution beyond trigger; MarkPrice often remained on opposite side |
| Identical SOL average exits | Testnet data anomaly | Distinct orders/times/execution IDs, identical 13-level ladder |
| Trade 109 missing 0.01 close | Partial-fill accounting error | Two exchange closed-PnL rows total entry quantity; old journal persisted only one |
| Potential reuse of one record by two trades | Confirmed code defect, not observed in this run | Old per-trade matcher had no global consumed-record set or unique DB constraint |
| Protective replacement linkage | Confirmed observability/linkage defect | Replacement stop can have empty parent link; lifecycle IDs were not persisted |
| Duplicate closed-PnL ingestion | Not observed | Zero duplicate exchange order IDs and zero duplicate journal exit IDs |
| Wrong side, quantity, fee, timestamp or timezone attribution in 11 outliers | Not observed | All values reconcile to raw order/execution/closed-PnL data |
| Missing or failed SL in 11 outliers | Not observed | Every exit execution has `stopOrderType=StopLoss`; triggers equal intended protection |
| Stale tightened SL in trades 153, 200, 235, 247 | Not observed | Exchange final trigger equals journal tightened stop |

## Implemented reliability fixes

No strategy, signal, threshold, sizing, risk, frequency, or exchange behaviour was changed.

1. **Conservative global matcher.** Closed-PnL rows are deduplicated by exchange `orderId`. Stable submitted exit IDs, known exit IDs, and protective `parentOrderLinkId` have priority. Side, time, entry price, symbol, and quantity remain validation gates. A contested record is not assigned by nearest price.
2. **Full-quantity closure.** The matcher uses `closedSize`, not the original order `qty`, and waits until the sum of all uniquely owned closing rows matches confirmed opening fill quantity. This directly covers trade 109's 0.04 + 0.01 pattern.
3. **Durable per-close records.** New `trade_closures` stores one row per unique exchange closing order, raw closed-PnL payload, every execution payload, fees, quantity, reason, and close time. A unique constraint prevents one exchange close from belonging to two internal trades.
4. **Opening quantity persistence.** Requested and confirmed filled opening quantities are now stored separately.
5. **Exit Manager linkage.** Accepted closing `orderId` and `orderLinkId` are persisted before asynchronous reconciliation, so a restart does not discard the direct identifier.
6. **Protective lifecycle evidence.** New `trade_exchange_orders` stores linked entry/protective/exit orders, parent links, trigger, type, status, quantities, prices, exchange timestamps, observation timestamps and raw payload.
7. **Conditional history ingestion.** Reconciliation reads both regular and StopOrder history and deduplicates it by `orderId`.
8. **Fail closed on ambiguity.** Conflicting duplicate API payloads, incomplete quantity, contested records, and unmatched records remain unresolved. Existing orphan/circuit-breaker protection is unchanged.

## Migrations

Additive, idempotent migrations were run twice successfully against PostgreSQL.

New nullable `trade_log` columns:

- `entry_requested_qty NUMERIC`
- `entry_filled_qty NUMERIC`
- `exchange_exit_order_ids JSONB`
- `submitted_exit_order_id VARCHAR(100)`
- `submitted_exit_order_link_id VARCHAR(100)`

New tables:

- `trade_closures`, with a unique constraint on `exchange_exit_order_id`;
- `trade_exchange_orders`, with a unique constraint on `exchange_order_id`.

All 256 pre-existing `trade_log` rows remained present. Historical rows were not rewritten or silently backfilled. The new evidence tables are empty until a future reconciliation writes new evidence.

## Regression tests

Eight focused tests were added for:

- two same-symbol, same-direction trades with similar entries;
- identical exit prices resolved by stable parent identifiers;
- delayed arrival of a second partial close;
- aggregation of one position closed by multiple order IDs and multiple executions;
- duplicate API payload idempotence;
- conflicting duplicate payloads remaining unresolved;
- restart persistence of submitted exit identifiers;
- stale protective replacement and unmatched records refusing unsafe nearest-price attachment.

Existing tests also cover weighted entry partial fills, restart orphan recovery, repeated reconciliation, API failure isolation, and circuit-breaker idempotence.

Full suite result: **229 tests run, 229 passed, 0 failed, 0 errors** using `python3 -m unittest discover -s tests -q`. Pytest is not installed in the active Python environment; the repository's complete unittest suite was executed directly.

## Unresolved uncertainties

1. The exact internal implementation of Bybit Testnet's matching simulator is unavailable. The raw data proves what the Testnet account was charged, but not why its synthetic book returned those price ladders.
2. Early rotating application logs were overwritten. Exchange and database evidence replaces them for execution facts, but the original log text around some order submissions is unavailable.
3. Bybit replacement protective orders may have empty `parentOrderLinkId`. Future persistence captures the lifecycle, but older replacements can only be reconstructed by trigger, quantity, time, symbol and the eventual closing order ID.
4. Funding is confirmed and reconciled from transaction-log settlement rows, but the current journal schema does not normalize funding per trade. Closed-PnL already includes it. The forensic artifact preserves the reconstructed per-trade amount.
5. The historical `trade_log` table still intentionally contains trade 109's old single-record aggregate. Corrected analytics must use this report/artifacts, or a separately reviewed historical backfill must be performed later.

## Can historical analytics now be trusted?

**Exchange-backed aggregate analytics for this run: yes.** Every trade execution is accounted for and the corrected total matches attributable wallet change with zero residual.

**The existing `trade_log` table by itself: not completely.** It remains high by 0.05368396 USDT because historical rows were not silently mutated. Trade 109's exit price and fee allocation also remain incomplete there.

**The eleven large-loss rows: yes for account-impact facts, no for production-market realism.** Their order IDs, fill prices, quantities, fees and P&L are trustworthy representations of what Bybit Testnet posted. They should not be interpreted as evidence of Mainnet slippage distribution because the repeated deterministic ladders are a Testnet anomaly.

**Future telemetry after deploying this code and migration: structurally ready.** Stable identifiers, one-to-many closures, execution payloads and protection history can now be audited without relying on nearest-price attachment. Any ambiguity remains unresolved and retains existing safety protection.

## Final verdict for the telemetry phase

It is safe to proceed to the **Testnet telemetry phase**, not Mainnet. The execution/accounting defect has a regression-tested fix, migrations are additive and idempotent, and no strategy behaviour was changed. The remaining material risk is the realism of Bybit Testnet liquidity itself; telemetry must clearly separate exchange-observed Testnet fills from estimates of live-market execution quality.

## Artifact index

- `docs/forensic_execution_report.md` — this report.
- `artifacts/suspicious_trade_reconciliation.csv` — complete lifecycle table for all eleven trades below −1.5R.
- `artifacts/unmatched_exchange_records.csv` — 67 funding settlements plus the additional trade-109 closed-PnL/execution evidence.
- `artifacts/aggregate_reconciliation.json` — machine-readable exact accounting reconciliation.
