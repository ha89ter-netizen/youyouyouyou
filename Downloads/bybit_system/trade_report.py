from decimal import Decimal

from config.settings import BybitConfig
from storage.db import Database
from storage.models import TradeLog


def main():
    cfg = BybitConfig()
    db = Database(cfg)
    session = db.get_session()

    try:
        rows = (
            session.query(TradeLog)
            .order_by(TradeLog.opened_at.desc())
            .limit(20)
            .all()
        )

        print("=" * 70)
        print("BYBIT BOT TRADE REPORT")
        print("=" * 70)

        if not rows:
            print("Журнал пустой. Сделок пока не было.")
            return

        closed = [r for r in rows if r.status == "closed"]
        total_pnl = sum(Decimal(r.pnl_usdt or 0) for r in closed)

        print(f"Последних сделок : {len(rows)}")
        print(f"Закрытых         : {len(closed)}")
        print(f"Общий PnL        : {total_pnl:.4f} USDT")
        print("-" * 70)

        for r in rows:
            pnl = Decimal(r.pnl_usdt or 0)

            print(
                f"{r.opened_at} | {r.symbol} | {r.action} | "
                f"entry={r.entry_price} | exit={r.exit_price} | "
                f"size={r.size_usdt} | pnl={pnl:.4f} | status={r.status}"
            )

    finally:
        session.close()


if __name__ == "__main__":
    main()
