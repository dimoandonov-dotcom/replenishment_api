"""
Поръчки от клиенти: магазин + артикул + бройки + за кой ден. Добавят се НАД
нормалната заявка (не пипат мин/макс) в първата заявка за деня на доставка или след него.
Щом влязат в записана заявка (сутрешното пускане), се отбелязват като „в заявка“.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

SOFIA = timezone(timedelta(hours=3))


def _ensure(db: Session):
    db.execute(text("""CREATE TABLE IF NOT EXISTS customer_orders (
        id SERIAL PRIMARY KEY, store_id INT NOT NULL, article_id INT NOT NULL, qty NUMERIC(12,2) NOT NULL,
        for_date DATE NOT NULL, customer TEXT, note TEXT, created_by TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), included_run INT, included_at TIMESTAMPTZ,
        cancelled BOOLEAN NOT NULL DEFAULT FALSE)"""))
    db.commit()


def pending(db: Session, store_id: int, day: date, run_id: int | None = None) -> dict[int, list[dict]]:
    """article_id -> отворени поръчки от клиенти за доставка до този ден."""
    try:
        _ensure(db)
        rows = db.execute(text("""SELECT id, article_id, qty, for_date, customer FROM customer_orders
                                  WHERE store_id = :s AND NOT cancelled AND for_date <= :d
                                    AND (included_run IS NULL OR included_run = :r)
                                  ORDER BY for_date, id"""), {"s": store_id, "d": day, "r": run_id or -1}).all()
    except Exception:
        db.rollback()
        return {}
    out: dict[int, list[dict]] = {}
    for i, a, q, fd, c in rows:
        out.setdefault(a, []).append({"id": i, "qty": float(q), "for_date": fd, "customer": c or ""})
    return out


def apply(db: Session, store_id: int, day: date, combined, settings, stock: dict, run_id: int | None = None):
    """Добавя поръчките от клиенти към изчислените редове (над нормалната заявка)."""
    co = pending(db, store_id, day, run_id)
    if not co:
        return
    from . import models as m
    from .engine import OrderLine
    by_aid = {ln.article_id: ln for ln in combined.lines}
    sett = {s.article_id: s for s in settings}
    arts = {}
    for aid in co:
        if aid not in sett and aid not in arts:
            arts[aid] = db.get(m.Article, aid)
    for aid, lst in co.items():
        extra = sum(x["qty"] for x in lst)
        who = ", ".join(f"{x['customer'] or 'клиент'} {x['qty']:g} бр." for x in lst)
        label = f"👤 поръчка от клиент: {who}"
        s = sett.get(aid)
        pack = max(int((s.pack_size if s else getattr(arts.get(aid), "pack_size", 1)) or 1), 1)
        ln = by_aid.get(aid)
        if ln is not None:
            base = float(ln.suggested_quantity or 0)
            total = math.ceil((base + extra) / pack) * pack   # клиентът да е покрит изцяло
            ln.ordered_quantity = max(int(ln.ordered_quantity), int(total))
            ln.suggested_quantity = base + extra
            ln.notes = label + (" · " + ln.notes if ln.notes else "")
        else:
            a = arts.get(aid)
            if s is None and a is None:
                continue
            total = math.ceil(extra / pack) * pack
            combined.lines.append(OrderLine(
                store_id=store_id, article_id=aid, sku=s.sku if s else a.sku,
                name=s.name if s else (a.supplier_name or a.name),
                supplier_id=(s.supplier_id if s else a.default_supplier_id) or 1,
                current_stock=float(stock.get(aid, 0.0) or 0.0),
                min_stock=float(s.min_stock) if s else 0.0, max_stock=float(s.max_stock) if s else 0.0,
                effective_max=0.0, suggested_quantity=extra, ordered_quantity=int(total), pack_size=pack,
                notes=label))


def mark_included(db: Session, store_id: int, day: date, run_id: int):
    try:
        db.execute(text("""UPDATE customer_orders SET included_run = :r, included_at = now()
                           WHERE store_id = :s AND NOT cancelled AND included_run IS NULL AND for_date <= :d"""),
                   {"r": run_id, "s": store_id, "d": day})
        db.commit()
    except Exception:
        db.rollback()


def listing(db: Session, store_id: int | None = None, days: int = 30) -> list[dict]:
    _ensure(db)
    q = """SELECT c.id, c.store_id, s.name, a.sku, COALESCE(a.supplier_name, a.name), c.qty, c.for_date, c.customer,
                  c.note, c.created_by, c.created_at, c.included_run, c.included_at, c.cancelled
           FROM customer_orders c JOIN stores s ON s.id = c.store_id JOIN articles a ON a.id = c.article_id
           WHERE c.created_at >= now() - make_interval(days => :days)""" + (" AND c.store_id = :s" if store_id else "") + \
        " ORDER BY c.for_date DESC, c.id DESC"
    out = []
    for r in db.execute(text(q), {"s": store_id, "days": days}).all():
        st = "отказана" if r[13] else ("в заявка" if r[11] else "чака")
        out.append({"id": r[0], "store_id": r[1], "store": r[2], "sku": r[3], "name": r[4], "qty": float(r[5]),
                    "for_date": r[6].strftime("%d.%m.%Y"), "customer": r[7] or "", "note": r[8] or "",
                    "by": r[9] or "", "created": r[10].astimezone(SOFIA).strftime("%d.%m %H:%M"),
                    "included": r[12].astimezone(SOFIA).strftime("%d.%m %H:%M") if r[12] else None, "status": st})
    return out


def create(db: Session, store_id: int, sku: str, qty: float, for_date: date, customer: str | None,
           note: str | None, who: str | None) -> dict:
    _ensure(db)
    from sqlalchemy import select
    from . import models as m
    a = db.execute(select(m.Article).where(m.Article.sku == sku)).scalar_one_or_none()
    if a is None:
        raise ValueError("Няма такъв артикул")
    if qty <= 0:
        raise ValueError("Бройките трябва да са над 0")
    i = db.execute(text("""INSERT INTO customer_orders(store_id, article_id, qty, for_date, customer, note, created_by)
                           VALUES (:s,:a,:q,:d,:c,:n,:w) RETURNING id"""),
                   {"s": store_id, "a": a.id, "q": qty, "d": for_date, "c": (customer or "").strip() or None,
                    "n": (note or "").strip() or None, "w": who}).scalar()
    db.commit()
    return {"id": i, "sku": a.sku, "name": a.supplier_name or a.name}


def cancel(db: Session, oid: int):
    _ensure(db)
    db.execute(text("UPDATE customer_orders SET cancelled = TRUE WHERE id = :i AND included_run IS NULL"), {"i": oid})
    db.commit()
