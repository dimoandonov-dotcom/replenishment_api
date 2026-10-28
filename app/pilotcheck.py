"""
Проверка на пилотните заявки ПРЕДИ да тръгнат към НДК (Димо, 26.10).
За всеки пилотен магазин - днешната (последна) заявка на MinMaxAI:
  ⛔ повтаря заявка, която е още в път (НДК още няма фактура за стока след нея)
  ⚠️ количество, което вдига наличността много над максимума
  ⚠️ цял стек за артикул без продажби
  ⚠️ рязко по-голяма заявка от обичайното за магазина
  ⚠️ свършил артикул с продажби, който липсва в заявката
и какво реално е дошло вчера: нашата заявка или чужда (на Ани).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import models as m

TZ = ZoneInfo("Europe/Sofia")
AMB = (17729, 17730)                 # каса / бутилка амбалаж


def _ndk_goods(db: Session, sids: list[int], since) -> dict:
    """store_id -> {sku: qty} - стока по фактури от НДК от `since` нататък (без амбалаж)."""
    import os
    from . import mistral
    from .anomalies import _loc_map
    out = defaultdict(lambda: defaultdict(float))
    with mistral.use("varna"), mistral.connect() as conn:
        cur = conn.cursor()
        locs = _loc_map(db, cur)
        rev = {}
        for loc, sid in locs.items():
            if sid in sids:
                rev[loc] = sid
        if not rev:
            return out
        cur.execute(f"""SELECT q.LOCATIONID AS loc, q.MATERIALCODE AS code, SUM(q.QTY) AS qty
                        FROM MATERIALQTYLOG q WITH (NOLOCK) JOIN OPERATIONS o WITH (NOLOCK) ON o.NUM = q.OPERAIONNUM
                        WHERE q.OPERATIONTYPE = 2 AND o.PARTNERNAMEID = %s AND q.OPERATIONDATE >= %s
                          AND q.LOCATIONID IN ({','.join(map(str, rev))}) AND q.MATERIALCODE NOT IN ({','.join(map(str, AMB))})
                        GROUP BY q.LOCATIONID, q.MATERIALCODE""",
                    (int(os.getenv("NDK_PARTNER_ID", "186")), since.strftime("%Y-%m-%d %H:%M:%S")))
        for r in cur.fetchall():
            q = float(r["qty"] or 0)
            if q > 0:
                out[rev[r["loc"]]][str(r["code"])] += q
    return out


def check(db: Session) -> dict:
    from . import pilot, service
    now = datetime.now(TZ)
    today = now.date()
    start = datetime.combine(today, datetime.min.time(), TZ)
    sids = sorted(pilot.active_ids(db, today))
    stores = {s.id: s.name for s in db.execute(select(m.Store)).scalars()}
    arts = {a.id: a for a in db.execute(select(m.Article)).scalars()}
    sku2id = {a.sku: a.id for a in arts.values()}
    # фактури от НДК от вчера сутринта (за вчерашната заявка) - стока, без амбалаж
    y0 = start - timedelta(days=1)
    try:
        goods = _ndk_goods(db, sids, y0.replace(tzinfo=None))
    except Exception as e:
        goods, gerr = {}, f"{type(e).__name__}: {e}"[:150]
    else:
        gerr = None
    out = []
    for sid in sids:
        pos = db.execute(select(m.PurchaseOrder).where(m.PurchaseOrder.store_id == sid)
                         .order_by(m.PurchaseOrder.created_at.desc()).limit(12)).scalars().all()
        today_po = next((p for p in pos if p.created_at.astimezone(TZ).date() == today), None)
        prev_po = next((p for p in pos if p.created_at.astimezone(TZ).date() < today), None)
        st = {"store_id": sid, "store": stores.get(sid, sid), "stop": [], "warn": [], "info": []}
        out.append(st)
        if today_po is None:
            st["warn"].append("днес няма заявка на MinMaxAI")
            continue
        lines = [l for l in today_po.lines if l.ordered_quantity > 0]
        st["lines"], st["units"] = len(lines), sum(l.ordered_quantity for l in lines)
        st["made"] = today_po.created_at.astimezone(TZ).strftime("%H:%M")
        stock = service.latest_stock_map(db, sid)
        sales = service.load_avg_daily_sales(db, sid, 14)
        got = goods.get(sid, {})
        # 1) вчерашната заявка - дошла ли е и чия
        pend = {}
        if prev_po is not None and prev_po.created_at.astimezone(TZ).date() == today - timedelta(days=1):
            yl = {arts[l.article_id].sku: l.ordered_quantity for l in prev_po.lines if l.ordered_quantity > 0 and l.article_id in arts}
            if not got:
                pend = yl
                st["info"].append(f"вчерашната ни заявка ({len(yl)} реда) още НЕ е дошла - НДК няма фактура за стока")
            else:
                match = len(set(yl) & set(got))
                ani = db.execute(text("""SELECT l.sku FROM manual_order_lines l JOIN manual_orders o ON o.id = l.order_id
                                          WHERE o.store_id = :s AND l.store_qty > 0 AND o.received_at >= :a AND o.received_at < :b"""),
                                 {"s": sid, "a": y0, "b": start}).all()
                ani = {r[0] for r in ani}
                am = len(ani & set(got))
                if ani and am > match:
                    st["stop"].append(f"вчера е дошла заявката на АНИ ({am} от {len(got)} реда съвпадат с нейната, с нашата само {match})"
                                      " - към НДК не е отишла нашата")
                else:
                    st["info"].append(f"вчерашната ни заявка е дошла: {match} от {len(yl)} реда")
        # 2) редовете в днешната заявка
        dup, over, nosale = [], [], []
        for l in lines:
            a = arts.get(l.article_id)
            if a is None:
                continue
            real = max(float(stock.get(l.article_id, 0.0) or 0.0), 0.0)
            mx, pk = float(l.max_stock or 0), max(int(l.pack_size or 1), 1)
            if a.sku in pend and real + pend[a.sku] >= float(l.min_stock or 0) and "в път" not in (l.notes or ""):
                dup.append(f"{a.name} {l.ordered_quantity} бр. (в път {pend[a.sku]})")
            elif sales.get((sid, l.article_id), 0.0) <= 0 and l.ordered_quantity > max(mx, 1) and "клиент" not in (l.notes or ""):
                nosale.append(f"{a.name} {l.ordered_quantity} бр. (макс {mx:g}, без продажби)")
            elif mx > 0 and real + l.ordered_quantity > mx + pk and real + l.ordered_quantity > 1.5 * mx:
                over.append(f"{a.name}: {real:g} + {l.ordered_quantity} при макс {mx:g}")
        if dup:
            st["stop"].append(f"{len(dup)} реда повтарят вчерашната заявка, която е още в път: " + "; ".join(dup[:8]))
        if nosale:
            st["warn"].append(f"{len(nosale)} реда без продажби над максимума: " + "; ".join(nosale[:6]))
        if over:
            st["warn"].append(f"{len(over)} реда много над максимума: " + "; ".join(over[:6]))
        # 3) рязко по-голяма от обичайното
        prev_units = [sum(l.ordered_quantity for l in p.lines) for p in pos if p.created_at.astimezone(TZ).date() < today]
        if len(prev_units) >= 3:
            med = sorted(prev_units)[len(prev_units) // 2]
            if med > 0 and st["units"] > 2 * med and st["units"] - med > 50:
                st["warn"].append(f"заявката е {st['units']} бр. - обичайно ~{med} бр.")
        # 4) свършили артикули с продажби, които ги няма в заявката и не са в път
        inorder = {l.article_id for l in lines}
        try:
            keep = {x.article_id for x in service.load_settings(db, sid, None) if x.max_stock > 0}
        except Exception:
            db.rollback()
            keep = set()
        miss = []
        for (s_, aid), v in sales.items():
            if s_ != sid or v < 1 or aid in inorder or aid not in arts:
                continue
            if (float(stock.get(aid, 0.0) or 0.0) <= 0 and arts[aid].sku not in pend and arts[aid].is_active
                    and not arts[aid].no_order and aid in keep):
                miss.append(f"{arts[aid].name} ({v:.1f}/ден)")
        if miss:
            st["warn"].append(f"{len(miss)} свършили артикула с продажби ги няма в заявката: " + "; ".join(miss[:6]))
        # 5) фантомна наличност (наша сметка): Мистрал показва стока, а продажбите мълчат -> поръчано като 0; да се преброи
        try:
            from . import ownstock
            ph = [x for x in ownstock.compute(db, sid)["items"].values() if x["phantom"]]
            if ph:
                st["warn"].append(f"{len(ph)} фантомна наличност (да се преброят, заявени като 0): "
                                  + "; ".join(f"{x['name']} (Мистрал {x['mistral']:g}, посл. продажба {x['last_sale']})" for x in ph[:6]))
        except Exception:
            db.rollback()
    ok = [s for s in out if not s["stop"] and not s["warn"]]
    return {"at": now.strftime("%d.%m %H:%M"), "stores": out, "ok": len(ok), "stop": sum(1 for s in out if s["stop"]),
            "warn": sum(1 for s in out if s["warn"] and not s["stop"]), "goods_error": gerr, "text": _text(out, now) + _inv_text(db)}


def _inv_text(db: Session) -> str:
    try:
        from . import ndkinv
        t = ndkinv.text_report(db, 2)
        return ("\n\n" + t) if t else ""
    except Exception:
        db.rollback()
        return ""


def _text(out: list, now) -> str:
    L = [f"Пилот - проверка на заявките за НДК, {now.strftime('%d.%m %H:%M')}"]
    for s in out:
        head = "⛔" if s["stop"] else ("⚠️" if s["warn"] else "✅")
        L.append(f"\n{head} {s['store']}: {s.get('lines', 0)} реда / {s.get('units', 0)} бр." + (f" (от {s['made']})" if s.get("made") else ""))
        for x in s["stop"]:
            L.append("   ⛔ " + x)
        for x in s["warn"]:
            L.append("   ⚠️ " + x)
        for x in s["info"]:
            L.append("   ℹ️ " + x)
    return "\n".join(L)


def delivery_status(db: Session, store_id: int) -> dict:
    """За „Заявки по магазин“: дошла ли е вчерашната заявка (фактура от НДК за стока, без амбалаж) и коя заявка е."""
    import os
    from . import mistral, pilot
    from .anomalies import _loc_map
    now = datetime.now(TZ)
    today = now.date()
    y = today - timedelta(days=1)
    start_y = datetime.combine(y, datetime.min.time(), TZ)
    start_t = datetime.combine(today, datetime.min.time(), TZ)
    # вчерашните заявки: нашата (пилот) и на Ани
    ours = db.execute(select(m.PurchaseOrder).where(m.PurchaseOrder.store_id == store_id, m.PurchaseOrder.created_at >= start_y,
                                                    m.PurchaseOrder.created_at < start_t)
                      .order_by(m.PurchaseOrder.created_at.desc())).scalars().first()
    arts = {a.id: a.sku for a in db.execute(select(m.Article)).scalars()}
    o_sku = {arts.get(l.article_id) for l in ours.lines if l.ordered_quantity > 0} if ours else set()
    a_sku = {r[0] for r in db.execute(text("""SELECT l.sku FROM manual_order_lines l JOIN manual_orders o ON o.id = l.order_id
                                              WHERE o.store_id = :s AND l.store_qty > 0 AND o.received_at >= :a AND o.received_at < :b"""),
                                      {"s": store_id, "a": start_y, "b": start_t}).all()}
    pil = store_id in pilot.active_ids(db, y)
    got, first, last = {}, None, None
    with mistral.use("varna"), mistral.connect() as conn:
        cur = conn.cursor()
        locs = [loc for loc, sid in _loc_map(db, cur).items() if sid == store_id]
        if locs:
            cur.execute(f"""SELECT q.MATERIALCODE AS code, SUM(q.QTY) AS qty, MIN(o.DATESAVED) AS t1, MAX(o.DATESAVED) AS t2
                            FROM MATERIALQTYLOG q WITH (NOLOCK) JOIN OPERATIONS o WITH (NOLOCK) ON o.NUM = q.OPERAIONNUM
                            WHERE q.OPERATIONTYPE = 2 AND o.PARTNERNAMEID = %s AND q.OPERATIONDATE >= %s
                              AND q.LOCATIONID IN ({','.join(map(str, locs))}) AND q.MATERIALCODE NOT IN ({','.join(map(str, AMB))})
                            GROUP BY q.MATERIALCODE""",
                        (int(os.getenv("NDK_PARTNER_ID", "186")), (start_y + timedelta(hours=12)).replace(tzinfo=None)))
            for r in cur.fetchall():
                q = float(r["qty"] or 0)
                if q > 0:
                    got[str(r["code"])] = q
                    first = r["t1"] if first is None or r["t1"] < first else first
                    last = r["t2"] if last is None or r["t2"] > last else last
    ordered = o_sku if (pil and o_sku) else (a_sku or o_sku)
    whose = "MinMaxAI" if (pil and o_sku) else ("Ани" if a_sku else ("MinMaxAI" if o_sku else None))
    res = {"store_id": store_id, "ordered_by": whose, "ordered_lines": len(ordered), "arrived": bool(got),
           "lines": len(got), "units": round(sum(got.values()), 1),
           "at": last.strftime("%d.%m %H:%M") if last else None,
           "match": len(ordered & set(got)), "missing": len(ordered - set(got))}
    if got and pil and o_sku and a_sku:
        mo, ma = len(o_sku & set(got)), len(a_sku & set(got))
        if ma > mo:
            res["warning"] = f"дошла е заявката на Ани ({ma} реда съвпадат), не нашата ({mo})"
    return res
