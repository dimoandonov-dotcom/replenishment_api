"""
Пилотни магазини „под лупа“: справка в Telegram (10:00 и 18:00) до PILOT_REPORT_USERS (по подразбиране dimo).

За всеки пилотен магазин:
  1) днешната заявка на MinMaxAI (за утре): редове, бройки;
  2) доставката днес срещу нашата заявка от вчера: липсващи / по-малко / непоръчани редове
     (ако фактурата още я няма в Мистрал - казва го);
  3) свършили: наличност <= 0 при артикул, който се продава;
  4) препълнени: наличност над 1,5 × макса;
  5) грешно заведени фактури (OCR) днес/вчера.
"""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import models as m

SOFIA = timezone(timedelta(hours=3))


def users() -> set[str]:
    return {x.strip().lower() for x in os.getenv("PILOT_REPORT_USERS", "dimo").split(",") if x.strip()}


def _n(a) -> str:
    import re
    return re.sub(r"\s+/\s*\d.*$|\s{2,}/.*$", "", a.supplier_name or a.name or "").strip()[:34]


def build(db: Session) -> dict:
    from . import deliveries, pilot, service
    now = datetime.now(SOFIA)
    today = now.date()
    sids = sorted(pilot.active_ids(db, today + timedelta(days=1)))   # и тези, които започват утре
    since = pilot.since_map(db)
    if not sids:
        return {"text": "", "stores": []}
    arts = {a.id: a for a in db.execute(select(m.Article)).scalars()}
    stores = {s.id: s.name for s in db.execute(select(m.Store).where(m.Store.id.in_(sids))).scalars()}
    ours = deliveries._ours(db, today - timedelta(days=1))
    dl = deliveries._delivered(db, today - timedelta(days=1))
    s14 = {(s, a): float(q or 0) for s, a, q in db.execute(
        select(m.SalesHistory.store_id, m.SalesHistory.article_id, func.sum(m.SalesHistory.quantity_sold))
        .where(m.SalesHistory.sale_date > today - timedelta(days=14), m.SalesHistory.store_id.in_(sids))
        .group_by(m.SalesHistory.store_id, m.SalesHistory.article_id)).all()}
    try:
        from . import ocralert
        ocr = ocralert.find(db)
    except Exception:
        db.rollback()
        ocr = []
    start = datetime.combine(today, datetime.min.time(), SOFIA)
    lines, out = [f"🔎 ПИЛОТ под лупа – {now.strftime('%d.%m %H:%M')}", ""], []
    for sid in sids:
        st = stores.get(sid, str(sid))
        live = since.get(sid) is None or since[sid] <= today
        head = f"🏪 {st}" + ("" if live else f" (започва {since[sid].strftime('%d.%m')})")
        lines.append(head)
        r = {"store_id": sid, "store": st}
        # 1) днешната заявка
        po = db.execute(select(m.PurchaseOrder).where(m.PurchaseOrder.store_id == sid, m.PurchaseOrder.created_at >= start)
                        .order_by(m.PurchaseOrder.created_at.desc())).scalars().first()
        if po:
            ol = [l for l in po.lines if float(l.ordered_quantity or 0) > 0]
            lines.append(f"   📝 Заявка за утре: {len(ol)} реда, {sum(float(l.ordered_quantity) for l in ol):g} бр.")
        else:
            lines.append("   📝 Заявка за утре: ❗ НЯМА (или е неделя/без доставка)")
        # 2) днешната доставка срещу нашата заявка от вчера
        ordered = {a: float(l.ordered_quantity or 0) for a, l in (ours.get((sid, today)) or {}).items()
                   if float(l.ordered_quantity or 0) > 0}
        got = dl.get((sid, today), {})
        if live and ordered:
            if not got:
                lines.append(f"   🚚 Днешна доставка: фактурата още не е заведена ({len(ordered)} поръчани реда)")
            else:
                miss = [(a, q) for a, q in ordered.items() if got.get(a, 0) <= 0]
                short = [(a, q, got[a]) for a, q in ordered.items() if 0 < got.get(a, 0) < q - 0.01]
                extra = [(a, q) for a, q in got.items() if a not in ordered]
                ok = len(ordered) - len(miss) - len(short)
                lines.append(f"   🚚 Днешна доставка: {ok} от {len(ordered)} реда точно"
                             + (f", ❗ {len(miss)} липсват" if miss else "") + (f", {len(short)} по-малко" if short else "")
                             + (f", {len(extra)} непоръчани" if extra else ""))
                for a, q in miss[:6]:
                    lines.append(f"      – липсва {_n(arts[a])} ({arts[a].sku}) {q:g} бр.")
                for a, q, g in short[:4]:
                    lines.append(f"      – {_n(arts[a])}: поръчани {q:g}, дошли {g:g}")
                for a, q in extra[:4]:
                    lines.append(f"      – непоръчан {_n(arts[a])} ({arts[a].sku}) {q:g} бр.")
                r.update(missing=len(miss), short=len(short), extra=len(extra))
        # 3) свършили и 4) препълнени
        stock = service.latest_stock_map(db, sid)
        sets = {x.article_id: x for x in db.execute(select(m.StoreArticleSetting).where(
            m.StoreArticleSetting.store_id == sid)).scalars()}
        out_ = sorted([(a, s14[(sid, a)]) for a, q in stock.items() if q <= 0 and s14.get((sid, a), 0) >= 3
                       and a in arts and arts[a].is_active and "АМБАЛАЖ" not in (arts[a].name or "").upper()],
                      key=lambda z: -z[1])
        over = sorted([(a, q, float(sets[a].max_stock)) for a, q in stock.items()
                       if a in sets and float(sets[a].max_stock or 0) > 0 and q > 1.5 * float(sets[a].max_stock) and q - float(sets[a].max_stock) >= 6],
                      key=lambda z: -(z[1] - z[2]))
        lines.append(f"   🔴 Свършили (продават се): {len(out_)}" + (": " + ", ".join(
            f"{_n(arts[a])} ({s / 14:.1f}/ден)" for a, s in out_[:5]) if out_ else ""))
        if over:
            lines.append(f"   🟠 Препълнени (над 1,5× макс): {len(over)}: " + ", ".join(
                f"{_n(arts[a])} {q:g}/{mx:g}" for a, q, mx in over[:4]))
        sw = [x for x in ocr if x["store_id"] == sid]
        for x in sw[:3]:
            lines.append(f"   ⚠️ Грешна фактура {x['doc']}: поръчан {x['ordered'][:28]}, заведен {x['booked'][:28]} {x['qty']:g} бр.")
        r.update(out=len(out_), over=len(over), ocr=len(sw), ordered_today=bool(po))
        out.append(r)
        lines.append("")
    return {"text": "\n".join(lines).strip(), "stores": out}


def send(db: Session) -> dict:
    from . import telegram
    rep = build(db)
    if not rep["text"]:
        return {"sent": [], "reason": "няма пилотни магазини"}
    sent = []
    if telegram.token():
        telegram._ensure(db)
        us = users()
        for cid, u in db.execute(text("SELECT chat_id, username FROM tg_users WHERE username IS NOT NULL")).all():
            if (u or "").lower() in us:
                telegram.send(int(cid), rep["text"])
                sent.append(u)
    return {"sent": sent, "stores": rep["stores"]}
