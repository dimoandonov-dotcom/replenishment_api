"""Пилот: магазини, за които заявката на MinMaxAI е истинската (НДК доставя по нея)."""
from __future__ import annotations
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session


def _ensure(db: Session):
    db.execute(text("""CREATE TABLE IF NOT EXISTS pilot_stores (store_id INT PRIMARY KEY,
                       since DATE DEFAULT CURRENT_DATE, note TEXT)"""))
    # Надежда е първата (19-20.10.2026) - заявката ѝ не минава през Ани
    db.execute(text("INSERT INTO pilot_stores(store_id, note) VALUES (46, 'първи пилот') ON CONFLICT DO NOTHING"))
    # 2-ра вълна от неделя 25.10.2026 (Димо, 23.10): Чаталджа, Странджа, Възраждане, Дубровник, Младост
    db.execute(text("""INSERT INTO pilot_stores(store_id, since, note) VALUES
                       (1, '2026-10-25', '2-ра вълна'), (27, '2026-10-25', '2-ра вълна'), (26, '2026-10-25', '2-ра вълна'),
                       (14, '2026-10-25', '2-ра вълна'), (37, '2026-10-25', '2-ра вълна') ON CONFLICT DO NOTHING"""))
    db.commit()


def ids(db: Session) -> set[int]:
    _ensure(db)
    return {r[0] for r in db.execute(text("SELECT store_id FROM pilot_stores")).all()}


def since_map(db: Session) -> dict:
    """store_id -> от коя дата (на заявката) MinMaxAI поръчва истински за магазина."""
    _ensure(db)
    return {r[0]: r[1] for r in db.execute(text("SELECT store_id, since FROM pilot_stores")).all()}


def active_ids(db: Session, day=None) -> set[int]:
    from datetime import datetime, timedelta, timezone
    day = day or datetime.now(ZoneInfo("Europe/Sofia")).date()
    return {s for s, d in since_map(db).items() if d is None or d <= day}


def listing(db: Session) -> list[dict]:
    _ensure(db)
    rows = db.execute(text("""SELECT s.id, s.name, p.since FROM stores s LEFT JOIN pilot_stores p ON p.store_id = s.id
                              WHERE s.is_active AND COALESCE(s.mistral_db_path, '') <> 'sofia' ORDER BY s.name""")).all()
    return [{"store_id": i, "store": n, "pilot": d is not None, "since": d.strftime("%d.%m.%Y") if d else None}
            for i, n, d in rows]


def set_pilot(db: Session, store_id: int, on: bool, since=None) -> dict:
    _ensure(db)
    if on:
        db.execute(text("""INSERT INTO pilot_stores(store_id, since) VALUES (:s, COALESCE(:d, CURRENT_DATE))
                           ON CONFLICT (store_id) DO UPDATE SET since = COALESCE(:d, pilot_stores.since)"""),
                   {"s": store_id, "d": since})
    else:
        db.execute(text("DELETE FROM pilot_stores WHERE store_id = :s"), {"s": store_id})
    db.commit()
    return {"store_id": store_id, "pilot": on}
