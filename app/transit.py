"""
Стока „в път“: заявката на Ани от вчера (или онзи ден), за която в Мистрал още няма фактура от НДК.

НДК изписва фактурите сутринта в деня на доставката (към 8-9 ч.), независимо кога ще стигне стоката.
Ако някой магазин не е доставен (напр. в понеделник), фактурата излиза чак на следващата сутрин.
Докато фактурата я няма, поръчаното се брои като наличност в път - иначе MinMaxAI ще поръча същото отново.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m

SOFIA = timezone(timedelta(hours=3))
MAX_WAIT_DAYS = 2          # заявка по-стара от толкова дни вече не се чака


def in_transit(db: Session, now: datetime | None = None) -> dict[tuple[int, int], dict]:
    """(store_id, article_id) -> {"qty": бройки, "ordered": "дд.мм"} - поръчано, но още без фактура в Мистрал."""
    from . import mistral
    from .anomalies import _loc_map
    now = now or datetime.now(SOFIA)
    today = now.astimezone(SOFIA).date()
    since = datetime.combine(today - timedelta(days=MAX_WAIT_DAYS), datetime.min.time(), SOFIA)
    until = datetime.combine(today, datetime.min.time(), SOFIA)      # днешните заявки са за утре - не се броят
    skus = {a.sku: a.id for a in db.execute(select(m.Article)).scalars().all()}
    codes = {a_id: int(sku) for sku, a_id in skus.items() if sku.isdigit()}
    pending = defaultdict(dict)                                       # (sid, aid) -> {qty, day}
    for o in db.execute(select(m.ManualOrder).where(
            m.ManualOrder.store_id.isnot(None), m.ManualOrder.received_at >= since,
            m.ManualOrder.received_at < until)).scalars().all():
        made = o.received_at.astimezone(SOFIA).date()
        for ln in db.execute(select(m.ManualOrderLine).where(m.ManualOrderLine.order_id == o.id)).scalars():
            q = float(ln.store_qty or 0)
            aid = skus.get(ln.sku)
            if q <= 0 or aid is None:
                continue
            p = pending[(o.store_id, aid)]
            if p and p["day"] > made:
                continue                                              # по-новата заявка е меродавна
            if p and p["day"] == made:
                p["qty"] += q
            else:
                pending[(o.store_id, aid)] = {"qty": q, "day": made}
    if not pending:
        return {}
    # доставено ли е: фактура (вид 2) за артикула в магазина с дата от деня на доставката (поръчката + 1) нататък
    delivered = {}
    with mistral.use("varna"), mistral.connect() as conn:
        cur = conn.cursor()
        locs = _loc_map(db, cur)
        rev = defaultdict(list)
        for loc, sid in locs.items():
            rev[sid].append(loc)
        sids = {sid for sid, _ in pending}
        L = ",".join(str(loc) for sid in sids for loc in rev.get(sid, []))
        C = ",".join(sorted({str(codes[aid]) for _, aid in pending if aid in codes}))
        if L and C:
            cur.execute(f"""SELECT LOCATIONID AS loc, MATERIALCODE AS code, MAX(CAST(OPERATIONDATE AS date)) AS d
                            FROM MATERIALQTYLOG WITH (NOLOCK)
                            WHERE OPERATIONTYPE = 2 AND OPERATIONDATE >= %s AND LOCATIONID IN ({L}) AND MATERIALCODE IN ({C})
                            GROUP BY LOCATIONID, MATERIALCODE""", (since.date().isoformat(),))
            by_code = {c: a for a, c in codes.items()}
            for r in cur.fetchall():
                sid, aid = locs.get(r["loc"]), by_code.get(int(r["code"]))
                if sid is None or aid is None:
                    continue
                d = r["d"] if not hasattr(r["d"], "date") else r["d"].date()
                if d and (delivered.get((sid, aid)) is None or d > delivered[(sid, aid)]):
                    delivered[(sid, aid)] = d
    out = {}
    for (sid, aid), p in pending.items():
        dd = delivered.get((sid, aid))
        if dd is not None and dd >= p["day"] + timedelta(days=1):
            continue                                                  # фактурата вече е в Мистрал
        out[(sid, aid)] = {"qty": p["qty"], "ordered": p["day"].strftime("%d.%m")}
    return out
