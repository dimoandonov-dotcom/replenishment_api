"""
Сутрешен доклад за наличностите (Димо, 19.11): всеки ден в 9:20 за Надежда -
бързите артикули и всичко под 2 бр., с анализ: стига ли до следващата доставка,
фантомна наличност, ще го поръча ли днешната заявка (09:30) и защо не.
Наличностите и продажбите са на живо от Мистрал (само четене).
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .db import get_db

SOFIA = ZoneInfo("Europe/Sofia")
FAST_ADR = 2.0          # бърз артикул: 2+ бр. на ден
LOW_STOCK = 2           # „под 2 бр.“
router = APIRouter()
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]


def _next_delivery(db: Session, store_id: int, today: date) -> date:
    """Следващата доставка от НДК след днес (по графика), иначе утре."""
    deliv = {s.delivery_weekday for s in db.execute(select(m.StoreSupplierSchedule).where(
        m.StoreSupplierSchedule.store_id == store_id, m.StoreSupplierSchedule.supplier_id == 1)).scalars()
        if getattr(s, "delivery_weekday", None)}
    for k in range(1, 8):
        d = today + timedelta(days=k)
        if not deliv or d.isoweekday() in deliv:
            return d
    return today + timedelta(days=1)


def build(db: Session, store_id: int) -> dict:
    from . import mistral, service
    from .anomalies import _loc_map
    from .engine import is_display
    st = db.get(m.Store, store_id)
    if not st:
        raise HTTPException(404, "Няма такъв магазин")
    now = datetime.now(SOFIA)
    today = now.date()
    nd = _next_delivery(db, store_id, today)
    days_to = max((nd - today).days, 1)          # стоката идва през деня -> до нея има толкова дни продажби

    sett = {s.article_id: s for s in db.execute(select(m.StoreArticleSetting).where(
        m.StoreArticleSetting.store_id == store_id, m.StoreArticleSetting.supplier_id == 1)).scalars()}
    plano = set(db.execute(select(m.Planogram.article_id).where(m.Planogram.store_id == store_id)).scalars())
    arts = {a.id: a for a in db.execute(select(m.Article).where(m.Article.id.in_(list(sett) or [0]))).scalars()}
    code2aid = {int(a.sku): aid for aid, a in arts.items() if (a.sku or "").isdigit()}

    stock, sales = {}, defaultdict(lambda: defaultdict(float))
    since = today - timedelta(days=14)
    with mistral.use("varna"), mistral.connect() as conn:
        cur = conn.cursor()
        locs = [loc for loc, sid in _loc_map(db, cur).items() if sid == store_id]
        if not locs or not code2aid:
            raise HTTPException(400, "Няма връзка магазин - Мистрал")
        L = ",".join(str(x) for x in locs)
        C = ",".join(str(c) for c in code2aid)
        cur.execute(f"SELECT MATERIALCODE AS c, SUM(QTY) AS q FROM MATERIAL WITH (NOLOCK) "
                    f"WHERE LOCATIONID IN ({L}) AND MATERIALCODE IN ({C}) GROUP BY MATERIALCODE")
        for r in cur.fetchall():
            stock[code2aid[int(r["c"])]] = float(r["q"] or 0)
        cur.execute(f"""SELECT MATERIALCODE AS c, CAST(OPERATIONDATE AS date) AS d, SUM(QTY) AS q
                        FROM MATERIALQTYLOG WITH (NOLOCK)
                        WHERE OPERATIONTYPE = 1 AND LOCATIONID IN ({L}) AND MATERIALCODE IN ({C})
                          AND OPERATIONDATE >= %s GROUP BY MATERIALCODE, CAST(OPERATIONDATE AS date)""",
                    (since.isoformat(),))
        for r in cur.fetchall():
            d = r["d"].date() if hasattr(r["d"], "date") else r["d"]
            if isinstance(d, str):
                d = date.fromisoformat(d[:10])
            sales[code2aid[int(r["c"])]][d] += -float(r["q"] or 0)

    # какво ще поръча заявката в 09:30 (предварително - по последните синхронизирани наличности)
    order = {}
    try:
        res = service.calculate_with_transit(db, store_id, today, None, False)
        order = {ln.article_id: (ln.ordered_quantity, ln.notes or "") for ln in res.lines}
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print("morning preview:", type(e).__name__, e)

    days14 = [today - timedelta(days=k) for k in range(1, 15)]
    fast, low = [], []
    for aid, s in sett.items():
        a = arts.get(aid)
        if a is None or not a.is_active or float(s.max_stock or 0) <= 0 or a.no_order:
            continue
        q = stock.get(aid, 0.0)
        dq = sales.get(aid, {})
        adr = sum(dq.get(d, 0.0) for d in days14) / 14
        today_sold = dq.get(today, 0.0)
        need = adr * days_to
        last_sale = max((d for d, v in dq.items() if v > 0), default=None)
        silent = (today - last_sale).days if last_sale else 15
        oq, onote = order.get(aid, (0, ""))
        row = {"sku": a.sku, "name": a.name, "stock": q, "adr": round(adr, 2), "need": round(need, 1),
               "cover_days": round(q / adr, 1) if adr > 0 else None, "today_sold": today_sold,
               "silent_days": silent, "order": oq, "order_note": onote,
               "min": float(s.min_stock or 0), "max": float(s.max_stock or 0), "in_plano": aid in plano}
        # анализ
        if adr >= FAST_ADR and q > 0 and silent >= 2:
            row["flag"] = "🕳"
            row["why"] = f"няма продажба от {silent} дни при {q:g} бр. в Мистрал - вероятно рафтът е празен"
        elif q < need:
            row["flag"] = "🔴"
            row["why"] = f"няма да стигне до доставката ({WD[nd.weekday()]}): трябват ~{math.ceil(need)}"
        elif q < need + adr:
            row["flag"] = "🟠"
            row["why"] = f"на ръба: трябват ~{math.ceil(need)} до доставката ({WD[nd.weekday()]}), силен ден може да го изчисти"
        else:
            row["flag"] = "🟢"
            row["why"] = f"стига за ~{row['cover_days']:g} дни"
        if oq:
            row["why"] += f" · заявката поръчва {oq:g}"
        elif row["flag"] in ("🔴", "🟠", "🕳") or q < LOW_STOCK:
            if aid not in plano:
                row["why"] += " · НЕ се поръчва: няма „да“ в планограмата"
            elif adr <= 0:
                row["why"] += " · не се поръчва: не се е продавал 14 дни"
            else:
                row["why"] += " · заявката НЕ го поръчва - провери"
        if adr >= FAST_ADR:
            fast.append(row)
        if q < LOW_STOCK and (adr > 0 or aid in plano):
            low.append(row)
    order_key = {"🕳": 0, "🔴": 1, "🟠": 2, "🟢": 3}
    fast.sort(key=lambda r: (order_key[r["flag"]], -r["adr"]))
    low.sort(key=lambda r: (-r["adr"], r["stock"]))
    problems = [r for r in fast if r["flag"] != "🟢"] + [r for r in low if r["adr"] > 0 and not r["order"] and r not in fast]
    return {"store": st.name, "at": now.strftime("%d.%m %H:%M"), "next_delivery": nd.strftime("%d.%m") + " " + WD[nd.weekday()],
            "days_to_delivery": days_to, "fast": fast, "low": low, "problems": len(problems)}


def text(rep: dict) -> str:
    out = [f"📦 {rep['store']} — наличности {rep['at']}",
           f"Следваща доставка: {rep['next_delivery']} (трябват {rep['days_to_delivery']} дни продажби)"]
    f = rep["fast"]
    bad = [r for r in f if r["flag"] != "🟢"]
    out.append("")
    out.append(f"⚡ БЪРЗИ АРТИКУЛИ (2+/ден): {len(f)} бр., проблемни {len(bad)}")
    for r in f:
        out.append(f"{r['flag']} {r['sku']} {r['name'][:34]} — {r['stock']:g} бр., {r['adr']:g}/ден · {r['why']}")
    lo = rep["low"]
    out.append("")
    out.append(f"⬇️ ПОД 2 БР.: {len(lo)}")
    for r in lo:
        sold = f"{r['adr']:g}/ден" if r["adr"] > 0 else "без продажби 14 дни"
        out.append(f"• {r['sku']} {r['name'][:34]} — {r['stock']:g} бр., {sold} · "
                   + (f"заявката поръчва {r['order']:g}" if r["order"] else r["why"].split(" · ")[-1]))
    out.append("")
    if rep["problems"]:
        out.append(f"⚠️ За действие: {rep['problems']} артикула (🕳 провери рафта/склада, 🔴/🟠 без поръчка - добави).")
    else:
        out.append("✅ Всичко бързо стига до доставката, а липсващите се поръчват.")
    return "\n".join(out)


@router.get("/ui/morning/{store_id}")
def morning(store_id: int, fmt: str = "json", db: Session = Depends(get_db)):
    rep = build(db, store_id)
    if fmt == "text":
        return PlainTextResponse(text(rep))
    rep["text"] = text(rep)
    return rep
