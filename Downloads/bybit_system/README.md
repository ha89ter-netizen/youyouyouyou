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
export MAX_OPEN_POSITIONS="2"         # макс. одновременно открытых позиций
export DEFAULT_STOP_LOSS_PCT="1.5"    # SL по умолчанию, если стратегия не задала свой
export MAX_VOLATILITY_ATR_PCT="3.0"   # выше этого ATR% -- не входить (слишком дёргано)
export MAX_SPREAD_PCT="0.15"          # шире этого спреда -- не входить (низкая ликвидность)
export TREND_FILTER_ENABLED="true"    # блокировать сигналы против EMA50/200
export TRAILING_STOP_ENABLED="true"
export TRAILING_ACTIVATION_PCT="1.0"  # прибыль %, при которой включается trailing stop
export TRAILING_DISTANCE_PCT="0.8"    # дистанция trailing stop от цены, %
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
