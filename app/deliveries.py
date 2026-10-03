"""
Доставки от НДК (Мистрал, вид движение 2) срещу заявката на MinMaxAI.

НДК доставя на следващия ден. Доставка на ден D в магазин S се сравнява със
заявката на MinMaxAI от D-1 (пускане в 06:00) и със заявката на Ани от D-1.
Така имаме сравнение с реално поръчаното/полученото, докато заработи изцяло
сравнението с Ани, и „перо" за наличностите: поръчано, но не заведено като доставка.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import models as m

SOFIA = timezone(timedelta(hours=3))


def _bg(dt) -> date:
    return dt.astimezone(SOFIA).date()


def _for_day(dt) -> date:
    """НДК доставя на СЛЕДВАЩИЯ ден: заявка днес -> доставка утре."""
    return dt.astimezone(SOFIA).date() + timedelta(days=1)


def _delivered(db: Session, since: date) -> dict:
    """(store, day) -> {article_id: qty} - само приходи (qty_in) от вид 2, без амбалаж."""
    out = defaultdict(dict)
    skip = {a.id for a in db.execute(select(m.Article)).scalars().all() if "АМБАЛАЖ" in (a.name or "").upper()}
    try:
        rows = db.execute(text("""SELECT store_id, article_id, day, qty_in FROM stock_movements
                                  WHERE optype = 2 AND qty_in > 0 AND day >= :d"""), {"d": since}).all()
    except Exception:
        db.rollback()
        return out
    for s, a, d, q in rows:
        if a in skip:
            continue
        out[(s, d)][a] = out[(s, d)].get(a, 0.0) + float(q)
    return out


def _ours(db: Session, since: date) -> dict:
    """(store, delivery_day) -> {article_id: line}; заявката от D-1 е за доставка на D (последната за деня)."""
    out = {}
    pos = db.execute(select(m.PurchaseOrder).where(
        m.PurchaseOrder.created_at >= datetime.combine(since - timedelta(days=1), datetime.min.time(), SOFIA))
        .order_by(m.PurchaseOrder.created_at)).scalars().all()
    for po in pos:
        out[(po.store_id, _for_day(po.created_at))] = {l.article_id: l for l in po.lines}
    return out


def _ani(db: Session, since: date) -> dict:
    out = {}
    skus = {a.sku: a.id for a in db.execute(select(m.Article)).scalars().all()}
    for o in db.execute(select(m.ManualOrder).where(
            m.ManualOrder.store_id.isnot(None),
            m.ManualOrder.received_at >= datetime.combine(since - timedelta(days=1), datetime.min.time(), SOFIA))
            .order_by(m.ManualOrder.received_at)).scalars().all():
        ls = db.execute(select(m.ManualOrderLine).where(m.ManualOrderLine.order_id == o.id)).scalars().all()
        out[(o.store_id, _for_day(o.received_at))] = {
            skus[l.sku]: float(l.store_qty) for l in ls if l.sku in skus and float(l.store_qty) > 0}
    return out


def summary(db: Session, days: int = 14) -> dict:
    since = datetime.now(SOFIA).date() - timedelta(days=days)
    dl, ours, ani = _delivered(db, since), _ours(db, since), _ani(db, since)
    arts = {a.id: a for a in db.execute(select(m.Article)).scalars().all()}
    stores = {s.id: s.name for s in db.execute(select(m.Store)).scalars().all()}
    price = lambda aid: float(getattr(arts.get(aid), "delivery_price", None) or 0)  # noqa: E731
    rows, tot = [], defaultdict(float)
    for key in sorted(set(dl) | set(ours), key=lambda k: (k[1], stores.get(k[0], "")), reverse=True):
        s, d = key
        if d < since:
            continue
        D, O = dl.get(key, {}), {a: float(l.ordered_quantity) for a, l in ours.get(key, {}).items() if l.ordered_quantity > 0}
        if not D and not O:
            continue
        both = len(set(D) & set(O))
        r = {"store_id": s, "store": stores.get(s, s), "day": d.strftime("%d.%m.%Y"), "iso": d.isoformat(),
             "has_ours": key in ours, "has_ani": key in ani, "delivered_lines": len(D), "our_lines": len(O),
             "both": both, "only_delivered": len(set(D) - set(O)), "only_ours": len(set(O) - set(D)),
             "delivered_units": round(sum(D.values()), 1), "our_units": round(sum(O.values()), 1),
             "delivered_eur": round(sum(q * price(a) for a, q in D.items()), 2),
             "our_eur": round(sum(q * price(a) for a, q in O.items()), 2)}
        rows.append(r)
        if key in ours and D:  # броим само дни, в които има и двете
            for k in ("both", "only_delivered", "only_ours", "delivered_units", "our_units", "delivered_eur", "our_eur"):
                tot[k] += r[k]
            tot["days"] += 1
        if key in ours and not D and O:
            tot["ordered_not_delivered"] += 1
    lines = tot["both"] + tot["only_delivered"]
    tot["match_pct"] = round(100 * tot["both"] / lines, 1) if lines else 0.0
    return {"days": days, "totals": {k: round(v, 2) for k, v in tot.items()}, "rows": rows}


def detail(db: Session, store_id: int, day: str) -> dict:
    from . import compare
    d = date.fromisoformat(day)
    since = d
    D = _delivered(db, since).get((store_id, d), {})
    O = _ours(db, since).get((store_id, d), {})
    A = _ani(db, since).get((store_id, d), {})
    arts = {a.id: a for a in db.execute(select(m.Article)).scalars().all()}
    plano = set(db.execute(select(m.Planogram.article_id).where(m.Planogram.store_id == store_id)).scalars().all())
    sett = {x.article_id: x for x in db.execute(select(m.StoreArticleSetting)
                                                .where(m.StoreArticleSetting.store_id == store_id)).scalars().all()}
    series = compare._series(db, store_id, d - timedelta(days=1))
    out = []
    for aid in set(D) | {a for a, l in O.items() if l.ordered_quantity > 0} | set(A):
        a = arts.get(aid)
        if a is None:
            continue
        ln = O.get(aid)
        dq, oq = D.get(aid, 0.0), float(ln.ordered_quantity) if ln else 0.0
        st = float(ln.current_stock) if ln else None
        s = sett.get(aid)
        mn = float(ln.min_stock) if ln else (float(s.min_stock) if s else None)
        mx = float(ln.max_stock) if ln else (float(s.max_stock) if s else None)
        if dq > 0 and oq > 0:
            note = "и двамата" if abs(dq - oq) < 0.01 else "различно количество"
        elif dq > 0:
            note = ("само магазин — няма „да“ в планограмата" if aid not in plano else
                    "само магазин — наличността е над мин" if (st is not None and mn is not None and st >= mn) else "само магазин")
        else:
            note = "само MinMaxAI — под мин"
        fake = SimpleNamespace(store_qty=dq, api_qty=oq, stock=st, min_stock=mn, max_stock=mx, note=note)
        ex = compare.explain(fake, series.get(a.sku))
        txt = ex["text"].replace("Ани поръчва", "Доставено е").replace("по Ани", "по доставката")
        verdict = ex["verdict"]
        aq = A.get(aid)
        if dq == 0 and oq > 0:
            if aq:  # Ани го е поръчала - просто още не е заведено като доставка
                note = "поръчано от Ани — още не е заведено"
                verdict = "чака"
                txt = txt.replace("; Ани не го е поръчала.", f"; Ани също го е поръчала ({compare._fmt(aq)} бр.).")
                txt = txt.split(" След доставка:")[0] + (
                    f" Доставката още не е заведена в Мистрал — ако не се появи до края на деня, "
                    f"значи не е доставено или не е заведено.")
            else:
                txt = txt.replace("; Ани не го е поръчала.", "; Ани не го е поръчала и не е доставено.")
        out.append({"sku": a.sku, "name": a.supplier_name or a.name, "delivered": dq, "ours": oq,
                    "ani": A.get(aid), "stock": st, "min": mn, "max": mx, "note": note,
                    "series": series.get(a.sku, [0.0] * 14), "explain": txt, "verdict": verdict,
                    "sales_per_day": ex["sdp"], "diff": oq - dq})
    out.sort(key=lambda r: (abs(r["diff"]) < 0.01, -abs(r["diff"]), r["name"] or ""))
    days14 = [(d - timedelta(days=15 - i)).strftime("%d.%m") for i in range(14)]
    return {"store": db.get(m.Store, store_id).name, "day": d.strftime("%d.%m.%Y"), "days": days14,
            "has_ours": bool(O), "is_today": d >= datetime.now(SOFIA).date(), "lines": out}
