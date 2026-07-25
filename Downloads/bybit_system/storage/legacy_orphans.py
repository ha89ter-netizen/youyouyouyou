"""One-time, explicit classification of the seven unrecoverable Testnet fills."""

import logging

from sqlalchemy import text

from config.settings import BybitConfig
from storage.db import Database
from storage.migrations import run_safe_migrations

logger = logging.getLogger(__name__)

LEGACY_ORPHAN_IDS = (
    "decision_e-434a4aaec4ed4dbd",
    "decision_e-53cfb8511ae24fa3",
    "decision_v-9bb32c34664d4982",
    "decision_e-4bfba75e63ef4bd0",
    "decision_e-f00837daad6c4106",
    "decision_v-0a7edcc9afd4436d",
    "decision_e-12e556041f284704",
)

LEGACY_REASON = (
    "Historical Testnet fill; exit outcome is unrecoverable because the "
    "exchange execution and closed-PnL retention window expired."
)


def classify_known_legacy_orphans(engine) -> int:
    """Classify only the reviewed IDs, only while they are still orphaned."""
    placeholders = ", ".join(f":oid_{i}" for i in range(len(LEGACY_ORPHAN_IDS)))
    params = {f"oid_{i}": oid for i, oid in enumerate(LEGACY_ORPHAN_IDS)}
    params["reason"] = LEGACY_REASON
    statement = text(
        "UPDATE trade_log "
        "SET status='historical_orphan', legacy_orphan_reason=:reason, "
        "legacy_classified_at=CURRENT_TIMESTAMP "
        f"WHERE status='orphaned' AND order_link_id IN ({placeholders})"
    )
    with engine.begin() as conn:
        result = conn.execute(statement, params)
    return int(result.rowcount or 0)


def main() -> None:
    cfg = BybitConfig()
    if not cfg.testnet:
        raise SystemExit("Refusing legacy Testnet classification outside Testnet mode")
    db = Database(cfg)
    run_safe_migrations(db.engine)
    changed = classify_known_legacy_orphans(db.engine)
    print(f"legacy_orphans_classified={changed}")


if __name__ == "__main__":
    main()
