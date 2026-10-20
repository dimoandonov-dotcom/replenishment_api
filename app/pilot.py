"""Пилот: магазини, за които заявката на MinMaxAI е истинската (НДК доставя по нея)."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


def _ensure(db: Session):
    db.execute(text("""CREATE TABLE IF NOT EXISTS pilot_stores (store_id INT PRIMARY KEY,
                       since DATE DEFAULT CURRENT_DATE, note TEXT)"""))
    # Надежда е първата (19-20.10.2026) - заявката ѝ не минава през Ани
    db.execute(text("INSERT INTO pilot_stores(store_id, note) VALUES (46, 'първи пилот') ON CONFLICT DO NOTHING"))
    db.commit()


def ids(db: Session) -> set[int]:
    _ensure(db)
    return {r[0] for r in db.execute(text("SELECT store_id FROM pilot_stores")).all()}


def listing(db: Session) -> list[dict]:
    _ensure(db)
    rows = db.execute(text("""SELECT s.id, s.name, p.since FROM stores s LEFT JOIN pilot_stores p ON p.store_id = s.id
                              WHERE s.is_active AND COALESCE(s.mistral_db_path, '') <> 'sofia' ORDER BY s.name""")).all()
    return [{"store_id": i, "store": n, "pilot": d is not None, "since": d.strftime("%d.%m.%Y") if d else None}
            for i, n, d in rows]


def set_pilot(db: Session, store_id: int, on: bool) -> dict:
    _ensure(db)
    if on:
        db.execute(text("INSERT INTO pilot_stores(store_id) VALUES (:s) ON CONFLICT DO NOTHING"), {"s": store_id})
    else:
        db.execute(text("DELETE FROM pilot_stores WHERE store_id = :s"), {"s": store_id})
    db.commit()
    return {"store_id": store_id, "pilot": on}
