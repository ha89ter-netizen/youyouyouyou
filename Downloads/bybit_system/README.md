# Bybit Trading System — модуль подключения к API

Первый модуль системы: подключение к Bybit (REST + WebSocket) для рынка
деривативов (USDT-перпетуалы). Дальше на этой базе строятся Storage,
Analytics, Strategy Engine, Risk Manager, Execution Engine.

## Структура

```
bybit_system/
├── config/
│   └── settings.py       # конфиг: testnet, символы, категория рынка
├── data/
│   ├── rest_client.py     # REST: свечи, стакан, тикеры, funding, OI, позиции
│   └── ws_client.py       # WebSocket: живые потоки (публичные + приватные)
├── main.py                # пример запуска
└── requirements.txt
```

## Установка

```bash
pip install -r requirements.txt
```

### База данных (TimescaleDB)

Проще всего через Docker:

```bash
docker compose up -d
```

Это поднимет TimescaleDB на `localhost:5432` (логин/пароль `postgres`/`postgres`,
база `bybit`). Если используете свою БД — задайте `DATABASE_URL`:

```bash
export DATABASE_URL="postgresql+psycopg2://user:pass@host:5432/dbname"
```

Затем один раз инициализируйте схему (создаст таблицы + hypertables):

```bash
python -m storage.init_db
```

## Настройка

Публичные данные (свечи, стакан, funding rate, ликвидации) работают
**без ключей**. Приватные данные (баланс, позиции, ордера) требуют
переменные окружения:

```bash
export BYBIT_API_KEY="ваш_ключ"
export BYBIT_API_SECRET="ваш_секрет"
export BYBIT_TESTNET="true"   # обязательно true, пока не протестируете всё
```

Ключи создаются в личном кабинете Bybit → API Management.
**Для тестнета** нужны отдельные ключи с https://testnet.bybit.com —
ключи с основной биржи там не работают.

⚠️ Никогда не коммитьте ключи в git и не вставляйте их в код напрямую.

## Запуск

```bash
python main.py
```

### Изолированный Testnet run

Для автономного сбора и торговли используйте supervisor, который создаёт
`run_id`, сохраняет commit/source metadata, не допускает дубликаты процессов,
ждёт свежие market data и только затем запускает торговый цикл:

```bash
python live_run.py start
python live_run.py status
python live_run.py stop
```

PID locks находятся в игнорируемом каталоге `.runtime/`, а heartbeat обоих
процессов записывается в таблицу `run_metadata`. `live_run.py start` всегда
принудительно использует Testnet и откажется стартовать при живых позициях,
активных ордерах, обычных `orphaned`-сделках или уже запущенном процессе.

### Railway Testnet

В Railway задайте корневой каталог сервиса `Downloads/bybit_system`. Команда
запуска уже зафиксирована в `railway.json`; точное значение:

```bash
python -u live_run.py run
```

Обязательные переменные Railway:

```bash
RUNTIME_MODE=railway
BYBIT_TESTNET=true
BYBIT_API_KEY=<отдельный Testnet key>
BYBIT_API_SECRET=<отдельный Testnet secret>
DATABASE_URL=${{Postgres.DATABASE_URL}}
STORAGE_MAX_DATABASE_BYTES=<PostgreSQL volume quota in bytes>
RAILWAY_DEPLOYMENT_DRAINING_SECONDS=30
```

Текущие подтверждённые оператором лимиты и два этапа time-based сужения:

```bash
MAX_OPEN_POSITIONS=10
MAX_DAILY_TRADES=200
TIME_RANGE_TIGHTENING_ENABLED=true
TIME_RANGE_TIGHTENING_AFTER_SECONDS=3600
TIME_RANGE_TIGHTENING_FACTOR=0.5
TIME_RANGE_SECOND_TIGHTENING_AFTER_SECONDS=18000
TIME_RANGE_SECOND_TIGHTENING_FACTOR=0.5
```

Research telemetry uses conservative defaults and may be configured without
changing trading behaviour:

```bash
TELEMETRY_ACCOUNT_INTERVAL_SEC=60
TELEMETRY_POSITION_INTERVAL_SEC=30
STRATEGY_VERSION=frozen-current
STORAGE_ENTRY_BLOCK_RATIO=0.70
TELEMETRY_OUTBOX_DELIVERED_RETENTION_HOURS=24
TELEMETRY_OUTBOX_CLEANUP_BATCH_SIZE=1000
TELEMETRY_OUTBOX_CLEANUP_MAX_BATCHES=10
HEALTH_EVENT_DEDUP_WINDOW_SECONDS=60
HEALTH_CONDITION_REMINDER_SECONDS=900
POSITION_CLOSE_VISIBILITY_GRACE_SECONDS=120
RAW_TRADES_RETENTION_HOURS=168
ORDERBOOK_RETENTION_HOURS=168
LIQUIDATIONS_RETENTION_HOURS=720
FUNDING_RAW_RETENTION_HOURS=6
OPEN_INTEREST_RAW_RETENTION_HOURS=6
HIGH_FREQUENCY_RETENTION_INTERVAL_SECONDS=1800
WS_RECONNECT_INITIAL_SECONDS=5
WS_RECONNECT_MAX_SECONDS=60
WS_RECONNECT_JITTER_RATIO=0.20
WS_RECONNECT_STABLE_RESET_SECONDS=120
WS_RECONNECT_RESTART_AFTER_SECONDS=900
COLLECTOR_RESTART_INITIAL_SECONDS=5
COLLECTOR_RESTART_MAX_SECONDS=60
COLLECTOR_RESTART_STABLE_RESET_SECONDS=300
PROTECTIVE_TRIGGER_BY=LastPrice
SLIPPAGE_ELEVATED_PCT=0.25
SLIPPAGE_ANOMALOUS_PCT=1.0
SLIPPAGE_ELEVATED_R=0.25
SLIPPAGE_ANOMALOUS_R=0.75
MAX_REALIZED_LOSS_R=1.5
PROTECTIVE_QUARANTINE_SECONDS=3600
PROTECTIVE_ANOMALY_STICKY_COUNT=2
HEALTH_HTTP_ENABLED=true
OPERATOR_MONITOR_INTERVAL_SECONDS=30
RETENTION_MAX_ROWS_PER_RUN=400000
```

Raw funding/open-interest ticks are retained for six hours; before deletion,
complete UTC-minute buckets are preserved as count/min/max/average rollups.
This bounds the two highest-churn tables without changing current strategy
inputs or discarding their longer-term research history.

### PostgreSQL growth and maintenance

Run the read-only audit before changing any retention setting:

```bash
python -m tools.storage_audit          # add --json for machine-readable output
```

It classifies every table as A (critical trading evidence), B (audit/research
history), C (reconstructible raw market data under bounded retention) or D
(ephemeral bookkeeping), and separates heap from index size. Only C and D are
ever bounded automatically; A and B are never auto-deleted.

The audit exists because of a specific trap. Autovacuum reclaims heap space for
reuse but never rebuilds a bloated btree, so the delete-heavy retention loop
can leave an index several times larger than its own table. When that is what
dominates, deleting more rows returns no space and only adds more bloat. The
audit reports it explicitly and estimates what `REINDEX` would return.

`REINDEX` rewrites an index and is an owner-approved maintenance action, never
something the trading process does to itself. `CONCURRENTLY` avoids blocking
writes, and each statement is independent, so it is safe to stop between them:

```bash
psql "$DATABASE_URL" -c "REINDEX INDEX CONCURRENTLY funding_rate_pkey;"
psql "$DATABASE_URL" -c "REINDEX INDEX CONCURRENTLY open_interest_pkey;"
python -m tools.storage_audit          # confirm the space came back
```

A failed `REINDEX CONCURRENTLY` leaves an invalid index behind; find it with
`SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;` and drop only
that invalid copy. Do not `VACUUM FULL` a table the bot is using: it takes an
exclusive lock and blocks the trading cycle.

## Telegram operator surface

```bash
TELEGRAM_ALERTS_ENABLED=true
TELEGRAM_BOT_TOKEN=<secret token from BotFather>
TELEGRAM_CHAT_ID=<destination chat id>
TELEGRAM_USER_ID=<your own numeric Telegram user id>
TELEGRAM_REPORT_INTERVAL_MINUTES=60
TELEGRAM_REPORT_PERIOD=24h            # 24h | utc_day | run
TELEGRAM_ALERT_ESCALATION_SECONDS=180
TELEGRAM_ALERT_REMINDER_SECONDS=3600
```

Get `TELEGRAM_CHAT_ID` and `TELEGRAM_USER_ID` by messaging the bot once and
reading `https://api.telegram.org/bot<TOKEN>/getUpdates`: `message.chat.id` is
the chat, `message.from.id` is you. In a private chat they are equal; in a
group they are not, and both are checked. All three values are read directly
from the process environment and are never persisted in run metadata,
telemetry or logs.

**Authorization fails closed.** Every message, command and inline button must
come from `TELEGRAM_CHAT_ID` *and* `TELEGRAM_USER_ID`. If `TELEGRAM_USER_ID`
is not configured, commands and buttons are disabled entirely — alerts are
still delivered, but nobody can control the bot. Callback queries are
authorized exactly like commands, so a forwarded message cannot become a
remote control. There is no command that runs shell, SQL, or arbitrary
exchange orders.

### Commands

| Command | Effect |
| --- | --- |
| `/status` | Is the bot working right now, and are entries allowed |
| `/report` | Full trading report for the configured period |
| `/positions` | Open positions with side and unrealized P&L |
| `/health` | Per-component detail: heartbeats, data age, outbox, breaker |
| `/pause` | Request a pause: new entries stop, positions stay managed |
| `/resume` | Request a resume: deterministic safety checks must pass first |

### Health states

| State | Meaning |
| --- | --- |
| `HEALTHY` | Everything fresh, new entries permitted |
| `DEGRADED` | Running, but an input the strategy depends on is unhealthy |
| `PAUSED` | Running and managing positions; new entries blocked by a gate |
| `STOPPED` | The runtime is not observable, or PostgreSQL is unusable |

The state is derived from data the trading process already writes —
`run_metadata` heartbeats, `risk_state` breaker causes, the storage guard, the
telemetry outbox and raw market-data timestamps. There is no second source of
truth for Telegram. `GET /healthz` and `GET /status` expose the same snapshot.

### Alerts, not telemetry

Raw engineering events such as `market_collector/websocket_disconnect` are no
longer forwarded to Telegram; they remain in `operational_health_events` and
the rotating log files. The owner gets lifecycle communication instead: a
problem has to make the canonical state non-`HEALTHY` **and** stay that way for
`TELEGRAM_ALERT_ESCALATION_SECONDS` before one `BOT WARNING` is sent. An
unchanged problem repeats at most every `TELEGRAM_ALERT_REMINDER_SECONDS`; a
worsening state alerts immediately; recovery sends exactly one `BOT RECOVERED`.
A WebSocket blip that reconnects on its own therefore produces no message at
all. Failed deliveries stay in a bounded durable retry queue.

### Recovery and resume are separate

Bounded automatic recovery (WebSocket reconnect, subscription restore, REST
candle repair, reconciliation) needs no approval and continues on its own.
Resuming *new entries* after a safety pause does. `/resume` only appends a row
to `operator_control_commands`; the trading process consumes it inside its own
cycle and re-validates, against live state, that PostgreSQL is available, the
runtime is running normally, both heartbeats are fresh, market data is younger
than five minutes, position state is readable, no telemetry has dead-lettered,
and no circuit-breaker cause other than the owner's own pause remains. If any
check fails the request is rejected with the reason, and trading stays paused.
`/resume` can only clear the `operator_pause` cause: it can never clear an
orphan, daily-loss or protective-execution cause.

This indirection is required for correctness as well as safety — the monitor
runs in the supervisor process while the Risk Manager owns `risk_state` in the
trader process, so a direct write from Telegram would be overwritten.

### Hourly report

One consolidated report per `TELEGRAM_REPORT_INTERVAL_MINUTES` (this replaces
the former once-a-day summary). The period is printed in the message itself and
never mixed. Metric definitions, all sourced from `trade_log` rows the journal
writes only after Bybit confirms closure, plus `position_snapshots` for live
values:

| Metric | Definition |
| --- | --- |
| Qualifying closed trade | `status='closed'`, non-null `pnl_usdt`, `closed_at` in period |
| Realized PnL | Sum of `pnl_usdt` over qualifying closed trades (net of fees) |
| Gross Profit | Sum of positive `pnl_usdt` |
| Gross Loss | Sum of negative `pnl_usdt` |
| Win Rate | Positive-PnL trades / qualifying closed trades; exactly zero is not a win |
| Unrealized PnL | Sum of newest `PositionSnapshot.unrealized_pnl` per open trade |
| Best / Worst symbol | Highest / lowest aggregate realized PnL over the same set |

Trades in `orphaned` status have an unknown result: they are excluded from
performance and reported separately, never averaged in as flat. A value that
cannot be read is rendered as `UNAVAILABLE`, never as `0` — "no positions" and
"position state could not be read" are different statements.

Telegram is a visibility and control surface only. If it is unavailable the
trading engine keeps running, keeps managing protection and keeps its own
fail-closed behaviour; nothing in the trading path waits on it.

### A future AI assistant

`operator_control.py` is the seam. An assistant may read status and reports and
explain them, and may translate natural language into one of the same explicit
commands — which still pass authorization and every deterministic check above.
An LLM is never the authority on whether trading is safe. No AI provider is
wired into this path today.

`PROTECTIVE_TRIGGER_BY` supports only `LastPrice` and `MarkPrice`. The default
remains `LastPrice` to preserve the frozen run behaviour; changing it affects
real exchange triggering and therefore requires a separately approved smoke
test. A first anomalous protective trigger-to-fill result without a certified
exchange trigger timestamp creates a durable timed entry quarantine; a second
active anomaly escalates it to sticky. A proven realized loss outside
`MAX_REALIZED_LOSS_R` remains immediately sticky. Existing positions and their
exchange-native protection continue to be managed in every case. A trailing
activation/configuration price is not treated as the unknown dynamic trailing
trigger price.

`MAX_REALIZED_LOSS_R` is an execution safety envelope, not an SL distance. An
exchange-confirmed result at or below the configured negative R limit creates
a durable sticky breaker for future entries regardless of exit mechanism.
It does not change, cancel, or recreate protection on existing positions. A
market protective order still cannot guarantee its eventual fill price.

Local `python -u live_run.py start` and Railway `python -u live_run.py run` now
use the same restart-capable supervisor. If the collector exhausts its internal
WebSocket recovery budget, it is recreated with bounded process-level backoff;
the trader keeps managing existing positions and remains fail-closed for new
entries while market data is stale.

These values affect observation cadence/identification only. The resolved
values are included in immutable run metadata and any later change creates a
new policy epoch.

`DATABASE_URL` обязан указывать на внешний PostgreSQL: Railway-режим отвергает
отсутствующее значение, SQLite и localhost. `STORAGE_MAX_DATABASE_BYTES`
обязан соответствовать реальной квоте Railway volume; при недоступной БД или
достижении 70% этой квоты новые входы блокируются, но открытые позиции продолжают
управляться. `RAILWAY_GIT_COMMIT_SHA`
предоставляется Railway автоматически; при другом способе сборки задайте
`COMMIT_SHA` явно. `OPENAI_API_KEY` нужен только если текущая конфигурация
использует OpenAI-анализ. 30-секундный draining window даёт collector время
сбросить буферы в PostgreSQL после SIGTERM.

В Railway дочерние collector/trader работают под foreground-supervisor,
наследуют stdout/stderr и запускаются с unbuffered Python. Файлы `logs/` и
`.runtime/` считаются временными и не используются как источник истины.
Активный `run_id`, журнал сделок, комиссии, exchange order IDs, historical
orphans, heartbeat и RiskManager state восстанавливаются из PostgreSQL.
PostgreSQL advisory locks предотвращают второй supervisor, collector или
trader даже в другом контейнере. Старые PID и heartbeat в таблице являются
только диагностикой и не блокируют новый процесс после рестарта.

Статус `historical_orphan` предназначен только для явно проверенных старых
Testnet-сделок, результат которых уже вышел за окно хранения биржи. Новые
`orphaned`-сделки по-прежнему взводят circuit breaker и не переводятся в этот
статус автоматически.

Скрипт:
1. Через REST получает последние 200 свечей по каждому символу и
   сразу сохраняет их в TimescaleDB.
2. Подписывается на живые WS-потоки: стакан, сделки, свечи, ликвидации,
   тикер (funding rate + open interest) — всё пишется в БД через
   `MarketDataStore` с буферизацией (не по одной записи, а пачками
   раз в 1-5 секунд, чтобы не перегружать БД).
3. Ctrl+C — буферы сбрасываются в БД перед выходом, данные не теряются.

## Структура БД

| Таблица | Что хранит | Ключ |
|---|---|---|
| `candles` | Свечи (OHLCV) | symbol, interval, start_time |
| `trades` | Лента сделок | symbol, trade_id, ts |
| `funding_rate` | История funding rate | symbol, funding_ts |
| `open_interest` | Открытый интерес | symbol, ts |
| `liquidations` | Лента ликвидаций | symbol, ts, side, price |
| `orderbook_snapshots` | Топ стакана (best bid/ask) | symbol, ts |

Все таблицы — TimescaleDB hypertables, партиционированные по времени
(chunk = 1 день). Повторная запись одной и той же строки (например, после
реконнекта WS) безопасна — используется `ON CONFLICT DO NOTHING`.

## Реконструкция стакана

`data/orderbook_state.py` держит в памяти полное состояние стакана
(цена → размер) для каждого символа и правильно применяет поток Bybit:
`snapshot` — полная замена состояния, `delta` — точечные правки
(размер `0` = уровень удалён). Лучший бид/аск берётся из АКТУАЛЬНОГО
состояния, а не из сырого сообщения — на delta-сообщениях Bybit
присылает только изменившиеся уровни, которые могут быть где угодно
в глубине стакана, а не обязательно в топе.

## Бэктест

`strategy/backtest.py` прогоняет rule-based комитет (schema) + trend filter
на исторических данных, уже накопленных в вашей БД через `main.py`. Не требует
работающего Bybit API — только собранная история свечей и funding rate.

AI Market Analyst не исполняет сделки и не является источником ордеров, поэтому
бэктест оценивает только торговую механику и rule-based часть. Аналитические
заключения LLM/AI нужно оценивать отдельно по журналу решений.

Защита от заглядывания в будущее (проверено тестами): решение принимается
строго на данных ДО текущей свечи, вход — по цене открытия следующей после
сигнала. Пока позиция "открыта" в симуляции — новый сигнал не ищется.

```bash
python run_backtest.py                          # все символы из конфига
python run_backtest.py --symbol ETHUSDT          # один символ
python run_backtest.py --balance 5000 --risk-pct 2.0
python run_backtest.py --no-trend-filter         # сравнить со схемой без trend filter
python run_backtest.py --min-history 30          # меньше данных для старта (полезно, когда истории мало)
```

Если позиция остаётся открытой к концу доступных исторических данных —
она принудительно закрывается по последней цене и попадает в отчёт с
пометкой "конец периода бэктеста", а не исчезает из статистики молча.

Выдаёт: число сделок, win rate, итоговый PnL, profit factor, максимальную
просадку, последние 5 сделок с ценами входа/выхода и причиной закрытия.

## Что дальше

Следующие модули (по мере готовности):
- **Analytics** — индикаторы поверх сохранённых данных, модуль бэктеста

## Торговый модуль (Strategy Engine + Risk Manager + Execution Engine)

Отдельная точка входа `trading_main.py` — включает реальную (пока testnet)
автономную торговлю.

### Как это устроено

```
Strategy Engine (каждые N секунд для каждого символа):
  ├── Market Context Engine (market_context.py)
  │     Определяет TREND/RANGE/BREAKOUT/REVERSAL, volatility, liquidity,
  │     volume expansion, funding bias, open interest trend и confidence.
  │
  ├── Meta Strategy Manager (meta_strategy.py)
  │     Выбирает, какие эксперты имеют право голосовать в данном режиме рынка,
  │     и уменьшает размер позиции при HIGH VOLATILITY / LOW LIQUIDITY.
  │
  ├── Independent Experts (strategy/experts.py + rule_based.py)
  │     EMA, RSI, VWAP, Momentum, OrderBook, Funding и существующий
  │     TechnicalRuleCommittee голосуют обычными Signal.
  │
  ├── Decision Engine (decision_engine.py)
  │     Сравнивает LONG/SHORT/HOLD, создаёт TradeDecisionReport и объясняет,
  │     почему победил один сценарий и почему отклонены остальные.
  │
  ├── Exit Manager (strategy/engine.py: _manage_exit)
  │     Если по символу уже есть открытая позиция — НЕ открывает новую, а
  │     проверяет, не пора ли закрыть текущую по разворотному решению комитета
  │     или смене старшего тренда против направления позиции.
  │
  ├── Portfolio Risk Engine (portfolio_risk.py)
  │     Проверяет корреляционный риск: например BTC LONG + ETH LONG + SOL LONG.
  │
  ├── Risk Manager (risk/risk_manager.py)
  │     ЕДИНСТВЕННЫЙ компонент, который может одобрить сделку.
  │     - Volatility gate: блокирует вход при аномально высоком ATR%
  │     - Liquidity gate: блокирует вход при широком спреде
  │     - Risk-based sizing: размер = (баланс × risk%) / stop_loss%
  │     - Дневной лимит убытка в % от баланса → circuit breaker
  │     - Лимит открытых позиций, запрет дублей по символу
  │
  └── Execution Engine / Paper Trading Engine
        Отправляет ордер на Bybit с обязательным SL/TP. Идемпотентность
        через orderLinkId. PaperTradingEngine может эмулировать тот же путь
        без реальных ордеров.
```

Дополнительно, каждый цикл:
- **Trailing Stop** — если нереализованная прибыль по открытой позиции
  достигла `TRAILING_ACTIVATION_PCT`, автоматически выставляется trailing
  stop на дистанции `TRAILING_DISTANCE_PCT` от цены.
- **Trade Journal** — сверяет открытые в БД сделки с `get_closed_pnl` на
  бирже; закрывшиеся сделки помечаются, PnL попадает в дневной счётчик риска.

### Настройка лимитов риска

Через переменные окружения (см. `config/settings.py` для дефолтов):

```bash
export RISK_PER_TRADE_PCT="1.0"       # риск на сделку в % от баланса (не фикс. сумма!)
export MAX_POSITION_USDT="100"        # жёсткий потолок размера позиции
export MAX_LEVERAGE="3"               # макс. плечо
export MAX_DAILY_LOSS_PCT="3.0"       # дневной лимит убытка в % от баланса на начало дня
export MAX_OPEN_POSITIONS="10"        # макс. одновременно открытых позиций
export MAX_DAILY_TRADES="200"         # макс. новых сделок за сутки UTC
export DEFAULT_STOP_LOSS_PCT="1.5"    # SL по умолчанию, если стратегия не задала свой
export MAX_VOLATILITY_ATR_PCT="3.0"   # выше этого ATR% -- не входить (слишком дёргано)
export MAX_SPREAD_PCT="0.15"          # шире этого спреда -- не входить (низкая ликвидность)
export TREND_FILTER_ENABLED="true"    # блокировать сигналы против EMA50/200
export TRAILING_STOP_ENABLED="true"
export TRAILING_ACTIVATION_PCT="1.0"  # прибыль %, при которой включается trailing stop
export TRAILING_DISTANCE_PCT="0.8"    # дистанция trailing stop от цены, %
export TIME_RANGE_TIGHTENING_AFTER_SECONDS="3600"         # первое сужение через 1 час
export TIME_RANGE_TIGHTENING_FACTOR="0.5"                 # оставшийся диапазон пополам
export TIME_RANGE_SECOND_TIGHTENING_AFTER_SECONDS="18000" # второе сужение через 5 часов
export TIME_RANGE_SECOND_TIGHTENING_FACTOR="0.5"          # ещё раз пополам
export DECISION_INTERVAL_SEC="60"     # как часто пересматривать рынок
```

**Как считается размер позиции**: `размер = (баланс × RISK_PER_TRADE_PCT%) / stop_loss_pct%`,
затем обрезается потолком `MAX_POSITION_USDT` и 90% баланса. Узкий стоп → больше
допустимый номинал при том же долларовом риске; широкий стоп → меньше номинал.
Так позиция автоматически адаптируется под волатильность конкретной сделки.

Начните с консервативных значений и увеличивайте только после того,
как понаблюдаете за поведением системы на testnet.

### Журнал сделок

Каждый вход и выход пишется в таблицу `trade_log` (символ, источник решения,
причина, цена входа/выхода, PnL). Каждый цикл система сверяет
открытые в журнале сделки с `get_closed_pnl` на бирже — если позиция закрылась
(по стопу, тейку, trailing stop или вручную), журнал обновляется автоматически,
а результат попадает в дневной счётчик Risk Manager.

**Важная деталь реализации**: сверка идёт НЕ по `orderLinkId`. В реальном ответе
Bybit `get_closed_pnl` поле `orderLinkId` отсутствует вовсе — когда позиция
закрывается по стоп-лоссу/тейк-профиту/trailing stop, закрывающий ордер
создаётся биржей автоматически и никак не привязан к нашему исходному
`orderLinkId`. Вместо этого сверка идёт по связке символ + цена входа
(с допуском 0.5% на проскальзывание) + время (закрытие не может быть раньше
открытия). Это надёжно, потому что Risk Manager физически не даёт открыть
вторую позицию по тому же символу, пока не закрыта текущая — то есть в
любой момент на символ существует максимум одна "открытая" запись в журнале.

Посмотреть журнал:

```bash
docker exec -it bybit_timescaledb psql -U postgres -d bybit -c "SELECT symbol, source, status, pnl_usdt, reason FROM trade_log ORDER BY opened_at DESC LIMIT 20;"
```

### Ключи для запуска

```bash
export BYBIT_API_KEY="ключ_с_testnet.bybit.com"
export BYBIT_API_SECRET="секрет_с_testnet.bybit.com"
export BYBIT_TESTNET="true"
```

Ключи Bybit для testnet создаются отдельно на https://testnet.bybit.com —
ключи с основной биржи там не работают. `OPENAI_API_KEY` больше не обязателен
для основного торгового цикла: AI Market Analyst не открывает сделки и сейчас
работает как аналитический слой без права на исполнение.

## Professional Decision Platform

Торговый цикл теперь строится не вокруг одной стратегии, а вокруг
инвестиционного комитета. Старые стратегии не удалены: rule-based комитет
остался в системе и подключён как один из экспертов. LLM не имеет права
открывать позиции; AI Market Analyst пишет только аналитическое заключение.

### Архитектурная схема

```
Market Data
  ↓
Market Context Engine (market_context.py)
  ↓
Meta Strategy Manager (meta_strategy.py)
  ↓
Independent Experts (strategy/experts.py + existing rule_based.py)
  ↓
Decision Engine + TradeDecisionReport (decision_engine.py)
  ↓
Portfolio Risk Engine (portfolio_risk.py)
  ↓
Risk Manager (risk/risk_manager.py)
  ↓
Execution Engine / Paper Trading Engine
  ↓
Trade Journal (storage/journal.py)
  ↓
Strategy Performance Manager (strategy/performance_manager.py)
  ↓
Replay / Analytics / Self Improvement Reports
```

### Новые классы

- `MarketContext`, `MarketContextEngine`
- `MetaStrategyDecision`, `StrategyPermission`, `MetaStrategyManager`
- `ExpertVote`, `TradeDecisionReport`, `DecisionEngine`
- `ExpertSignalCollector`
- `AIMarketAnalysis`, `AIMarketAnalyst`
- `PortfolioRiskResult`, `PortfolioRiskEngine`
- `PaperPosition`, `PaperTrade`, `PaperTradingEngine`
- `ReplayEvent`, `ReplayEngine`
- `StrategyPerformance`, `StrategyPerformanceManager`

### Новые сервисы

- `market_context.py` — определяет Trend, Range, Breakout, Reversal,
  volatility, liquidity, volume expansion, funding bias и open interest trend.
- `meta_strategy.py` — решает, какие эксперты могут голосовать в текущем
  режиме рынка, и уменьшает размер позиции при high volatility / low liquidity.
- `decision_engine.py` — собирает голоса экспертов, объясняет победителя и
  формирует `TradeDecisionReport`.
- `strategy/experts.py` — независимые эксперты EMA, RSI, VWAP, Momentum,
  OrderBook, Funding плюс существующий `TechnicalRuleCommittee`.
- `portfolio_risk.py` — блокирует перегрузку коррелированными позициями
  вроде BTC LONG + ETH LONG + SOL LONG.
- `paper_trading.py` — эмуляция сделок без реальных ордеров: PnL, комиссии,
  проскальзывание.
- `replay_engine.py` — проигрывает исторические свечи как online-поток.
- `strategy/performance_manager.py` — считает win rate, profit factor,
  average RR, holding time, max drawdown, Sharpe, expectancy и рейтинг.
- `ai_market_analyst.py` — аналитическое заключение без права на исполнение.

### Полный цикл одной сделки

1. `StrategyEngine` загружает свечи, funding, OI, стакан, поток сделок и
   ликвидации из БД.
2. `MarketContextEngine` строит `MarketContext`: например `Trend=UP`,
   `Volatility=HIGH`, `Liquidity=GOOD`, `Funding=POSITIVE`,
   `Volume=EXPANDING`, `Confidence=87%`.
3. `MetaStrategyManager` разрешает только подходящие источники. Например,
   в `TREND` режиме включаются EMA/VWAP/Momentum/OrderBook, а в `RANGE`
   режиме приоритет получают RSI/VWAP/Funding.
4. `ExpertSignalCollector` собирает независимые мнения экспертов:
   `EMA -> LONG`, `RSI -> HOLD`, `OrderBook -> LONG`, `Funding -> SHORT`.
5. `DecisionEngine` считает вес голосов, объясняет, почему победил LONG,
   почему отклонены SHORT/HOLD, и создаёт `TradeDecisionReport`.
6. `PortfolioRiskEngine` проверяет корреляционный риск по уже открытым
   позициям.
7. `RiskManager` применяет дневной лимит, ATR/spread gates, risk-based sizing,
   лимит позиций и множитель размера от Meta Strategy.
8. `ExecutionEngine` открывает позицию на Bybit testnet с SL/TP, либо
   `PaperTradingEngine` может эмулировать такую же сделку без биржи.
9. `TradeJournal` сохраняет вход с полным объяснением решения, затем
   автоматически подтягивает факт закрытия и PnL.
10. `StrategyPerformanceManager` накапливает статистику по источникам сигналов.

### Что масштабируется дальше

- Добавление новых экспертов без изменения Risk/Execution: достаточно вернуть
  обычный `Signal`.
- Более точные режимы `MarketContext`: отдельные модели ликвидности, OI,
  funding, volatility clustering.
- Хранилище `TradeDecisionReport` в отдельной таблице, чтобы смотреть не только
  факт сделки, но и все отклонённые сценарии.
- Paper Trading и Replay как полноценная лаборатория перед testnet.
- Self Improvement отчёт после каждых 100 сделок: рекомендации человеку без
  автоматического изменения параметров.

## Testnet Self-Check

Перед первым запуском торгового цикла на Bybit Testnet используйте:

```bash
python testnet_self_check.py
python testnet_self_check.py --skip-test-order
```

Self-check печатает Trading Mode, проверяет ключи, Testnet gate, Bybit REST,
БД, свечи, стакан, funding, open interest, WebSocket, Market Context, Meta
Strategy, Experts, Decision Engine, Portfolio Risk, Risk Manager, Paper Trading,
Replay Engine и журнал. Если все критические проверки успешны и
`--skip-test-order` не указан, скрипт открывает одну минимальную TESTNET-сделку,
сразу закрывает её, сохраняет журнал, подтягивает PnL и пересчитывает
статистику стратегий.

Если `BYBIT_TESTNET` не равен `true`, self-check немедленно завершится и не
создаст никаких ордеров.

### Запуск

```bash
# 1. Сначала должен поработать сбор данных (main.py), нужны свечи в БД
python main.py   # оставьте работать хотя бы 10-15 минут в отдельном окне терминала

# 2. Отдельным окном — торговый цикл
python trading_main.py
```

### Circuit breaker

Если дневной убыток достиг `MAX_DAILY_LOSS_USDT` — Risk Manager
блокирует все новые сделки до следующего календарного дня. Сброс
вручную (если вы разобрались, что произошло, и уверены, что можно
продолжать) — через `RiskManager.manual_reset_circuit_breaker()`.
Автоматического сброса намеренно нет.

## Важно про безопасность

- Начинайте **только** с `testnet=True`. Даже когда стратегия покажет
  прибыль на тестнете, переход на реальный счёт — отдельный, осознанный шаг.
- API-ключи создавайте с минимально необходимыми правами (без вывода
  средств), даже на этапе разработки.
