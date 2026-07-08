"""
Запуск бэктеста на данных, уже накопленных в вашей БД (через main.py).

Запуск:
    python run_backtest.py
    python run_backtest.py --symbol ETHUSDT --balance 5000
"""

import argparse
import logging

from config.settings import BybitConfig
from storage.db import Database
from strategy.backtest import Backtester

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")


def main():
    parser = argparse.ArgumentParser(description="Бэктест rule-based комитета на исторических данных")
    parser.add_argument("--symbol", default=None, help="Символ (по умолчанию -- все из конфига)")
    parser.add_argument("--interval", default="15", help="Таймфрейм свечей (по умолчанию 15м)")
    parser.add_argument("--balance", type=float, default=10_000.0, help="Стартовый виртуальный баланс")
    parser.add_argument("--risk-pct", type=float, default=1.0, help="Риск на сделку в %% от баланса")
    parser.add_argument("--max-position", type=float, default=100.0, help="Потолок размера позиции в USDT")
    parser.add_argument("--no-trend-filter", action="store_true", help="Отключить trend filter (EMA50/200)")
    parser.add_argument(
        "--min-history", type=int, default=30,
        help="Минимум свечей до начала поиска сигналов (по умолчанию 30 -- "
             "достаточно для самого комитета индикаторов; EMA200 в trend filter "
             "естественным образом активируется позже, как только в растущем "
             "окне накопится 202+ свечей -- до этого фильтр просто не блокирует)",
    )
    args = parser.parse_args()

    cfg = BybitConfig()
    db = Database(cfg)
    if not db.check_connection():
        print("БД недоступна. Запустите docker compose up -d.")
        return

    symbols = [args.symbol] if args.symbol else cfg.symbols
    backtester = Backtester(
        db=db,
        risk_per_trade_pct=args.risk_pct,
        max_position_usdt=args.max_position,
        trend_filter_enabled=not args.no_trend_filter,
        starting_balance=args.balance,
    )

    for symbol in symbols:
        print("=" * 60)
        try:
            result = backtester.run(symbol, interval=args.interval, min_history=args.min_history)
        except ValueError as e:
            print(f"{symbol}: {e}")
            continue
        print(result.summary())

        if result.trades:
            print("\nПоследние 5 сделок:")
            for t in result.trades[-5:]:
                print(
                    f"  {t.direction:5s} entry={t.entry_price:.4f} exit={t.exit_price:.4f} "
                    f"({t.exit_reason}) pnl={t.pnl_usdt:+.2f} USDT ({t.pnl_pct:+.2f}%)"
                )
    print("=" * 60)


if __name__ == "__main__":
    main()
