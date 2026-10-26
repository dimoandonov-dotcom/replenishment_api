"""
Ръчни промени в заявката на MinMaxAI (Ани, докато интегрираме всичко).
Всяка промяна се пази (кой, кога, старо -> ново, защо) и отива в Telegram на Димо.
Промяната важи за заявката за деня: и за прегледа/Excel, и за вече пуснатата заявка (09:30).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import models as m

SOFIA = timezone(timedelta(hours=2))

SCHEMA = """
CREATE TABLE IF NOT EXISTS order_edits (id BIGSERIAL PRIMARY KEY, day DATE, store_id INT, article_id INT, sku TEXT,
  old_qty INT, new_qty INT, by_user TEXT, reason TEXT, at TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS order_edits_day ON order_edits(day, store_id);
"""
_ok = False


def ensure(db: Session):
    global _ok
    if not _ok:
        for s in SCHEMA.strip().split(";"):
            if s.strip():
                db.execute(text(s))
        db.commit()
        _ok = True


def overrides(db: Session, store_id: int, day: date) -> dict[int, int]:
    """Последната промяна за всеки артикул в заявката за деня."""
    ensure(db)
    rows = db.execute(text("""SELECT DISTINCT ON (article_id) article_id, new_qty FROM order_edits
                              WHERE day = :d AND store_id = :s ORDER BY article_id, at DESC, id DESC"""),
                      {"d": day, "s": store_id}).all()
    return {a: int(q) for a, q in rows}


def apply(db: Session, store_id: int, day: date, combined, settings, stock) -> None:
    """Слага ръчните промени върху изчислената заявка (в calculate_for_store)."""
    ov = overrides(db, store_id, day)
    if not ov:
        return
    from .engine import OrderLine
    by = {ln.article_id: ln for ln in combined.lines}
    sett = {s.article_id: s for s in settings}
    for aid, q in ov.items():
        ln = by.get(aid)
        if ln is not None:
            if q <= 0:
                combined.lines.remove(ln)
                continue
            if ln.ordered_quantity != q:
                ln.notes = f"✏️ ръчно {ln.ordered_quantity}→{q}" + (" · " + ln.notes if ln.notes else "")
                ln.ordered_quantity = q
            continue
        if q <= 0:
            continue
        s_ = sett.get(aid)
        a = db.get(m.Article, aid)
        if a is None:
            continue
        combined.lines.append(OrderLine(
            store_id=store_id, article_id=aid, sku=a.sku, name=a.name,
            supplier_id=s_.supplier_id if s_ else (a.default_supplier_id or 1),
            current_stock=float(stock.get(aid, 0.0) or 0.0), min_stock=s_.min_stock if s_ else 0.0,
            max_stock=s_.max_stock if s_ else 0.0, effective_max=s_.max_stock if s_ else 0.0,
            suggested_quantity=0.0, ordered_quantity=q, pack_size=s_.pack_size if s_ else (a.pack_size or 1),
            notes=f"✏️ добавен ръчно ({q})"))


def save(db: Session, store_id: int, day: date, changes: list[dict], user: str | None) -> dict:
    """changes: [{sku, qty, old, reason}] -> записва, оправя вече пуснатата заявка, съобщава на Димо."""
    ensure(db)
    st = db.get(m.Store, store_id)
    if st is None:
        raise ValueError("няма такъв магазин")
    who = user or "система"
    arts = {a.sku: a for a in db.execute(select(m.Article).where(m.Article.sku.in_([c["sku"] for c in changes]))).scalars()}
    # вече пуснатата днес заявка (последното пускане с този магазин)
    start = datetime.combine(day, datetime.min.time(), SOFIA)
    po = db.execute(select(m.PurchaseOrder).where(m.PurchaseOrder.store_id == store_id, m.PurchaseOrder.created_at >= start,
                                                  m.PurchaseOrder.created_at < start + timedelta(days=1))
                    .order_by(m.PurchaseOrder.id.desc())).scalars().first()
    pol = {l.article_id: l for l in po.lines} if po is not None else {}
    done, lines_txt = [], []
    for c in changes:
        a = arts.get(str(c["sku"]))
        if a is None:
            continue
        q = max(0, int(c["qty"]))
        old = pol[a.id].ordered_quantity if a.id in pol else c.get("old")
        old = int(old) if old is not None else 0
        if q == old:
            continue
        reason = (c.get("reason") or "").strip()[:200] or None
        db.execute(text("""INSERT INTO order_edits(day, store_id, article_id, sku, old_qty, new_qty, by_user, reason)
                           VALUES (:d,:s,:a,:k,:o,:n,:u,:r)"""),
                   {"d": day, "s": store_id, "a": a.id, "k": a.sku, "o": old, "n": q, "u": who, "r": reason})
        if po is not None:
            ln = pol.get(a.id)
            if ln is not None and q <= 0:
                db.delete(ln)
            elif ln is not None:
                ln.ordered_quantity = q
                ln.notes = f"✏️ {who}: {old}→{q}" + (" · " + ln.notes if ln.notes else "")
            elif q > 0:
                db.add(m.PurchaseOrderLine(purchase_order_id=po.id, article_id=a.id, current_stock=0, min_stock=0,
                                           max_stock=0, effective_max=0, suggested_quantity=0, ordered_quantity=q,
                                           pack_size=a.pack_size or 1, notes=f"✏️ добавен от {who}"))
        done.append({"sku": a.sku, "name": a.name, "old": old, "new": q, "reason": reason})
        lines_txt.append(f"• {a.name} ({a.sku}): {old} → {q}" + (f" — {reason}" if reason else ""))
    db.commit()
    sent = False
    if done:
        try:
            from . import telegram
            name = {"ani": "Ани"}.get(who.lower(), who)
            msg = (f"✏️ {name} промени заявката на {st.name} за {day.strftime('%d.%m')}\n" + "\n".join(lines_txt[:40])
                   + (f"\n… и още {len(lines_txt) - 40}" if len(lines_txt) > 40 else ""))
            sent = bool(telegram.send_to(db, "dimo", msg))
        except Exception:
            db.rollback()
    return {"saved": len(done), "changes": done, "order_updated": po is not None, "told_dimo": sent}


def listing(db: Session, days: int = 7, store_id: int | None = None) -> list[dict]:
    ensure(db)
    q = """SELECT e.at, e.day, e.store_id, s.name, e.sku, a.name, e.old_qty, e.new_qty, e.by_user, e.reason
           FROM order_edits e LEFT JOIN stores s ON s.id = e.store_id LEFT JOIN articles a ON a.id = e.article_id
           WHERE e.day >= :d""" + (" AND e.store_id = :s" if store_id else "") + " ORDER BY e.at DESC, e.id DESC LIMIT 500"
    rows = db.execute(text(q), {"d": date.today() - timedelta(days=days), "s": store_id}).all()
    return [{"at": r[0].astimezone(SOFIA).strftime("%d.%m %H:%M"), "day": r[1].strftime("%d.%m"), "store_id": r[2], "store": r[3],
             "sku": r[4], "name": r[5], "old": r[6], "new": r[7], "by": r[8], "reason": r[9]} for r in rows]
