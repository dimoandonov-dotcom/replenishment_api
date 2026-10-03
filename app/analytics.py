"""
Анализи: (1) ефект на брошурата/промоциите, (2) продажби и наличности по групи.
Всичко е по НДК артикулите, от продажбите и наличностите от Мистрал.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import models as m

SOFIA = timezone(timedelta(hours=3))
BEER = {"АРИАНА", "ШУМЕНСКО", "ПИРИНСКО", "СТАРОПРАМЕН", "ХАЙНЕКЕН", "ЗАГОРКА", "БУРГАСКО", "КАМЕНИЦА",
        "МАДРИ", "ТУБОРГ", "СТЕЛА", "КОРОНА", "АМСТЕЛ", "ЖАТЕЦКИ", "АСТИКА", "МОРЕТИ", "БЕКС", "ЛЕВЕН"}
SPIRITS = {"ВОДКА", "РАКИЯ", "УИСКИ", "ДЖИН", "МЕНТА", "МАСТИКА", "РОМ", "КОНЯК", "ЛИКЬОР", "УЗО", "СПИРТНИ"}
GROUPS = ["Бира", "Безалкохолни", "Вода", "Сокове и лимонади", "Студен чай", "Сайдер", "Спиртни напитки",
          "Вино", "Енергийни", "Други"]


def group_of(a) -> str:
    c, n = (a.category or "").upper(), (a.supplier_name or a.name or "").upper()
    if "САЙДЕР" in n or "САМЪРСБИ" in c or "САМЪРСБИ" in n:
        return "Сайдер"
    if "ВИНО" in n or c == "ВИНО":
        return "Вино"
    if any(k in c for k in SPIRITS) or re.search(r"\b(ВОДКА|РАКИЯ|УИСКИ|ДЖИН|МАСТИКА|РОМ|КОНЯК|ЛИКЬОР|УЗО)\b", n) \
            or (re.search(r"\bМЕНТА\b", n) and "4,5%" not in n):
        return "Спиртни напитки"
    if "БИРА" in n or any(b in c for b in BEER) or any(b in n for b in BEER):
        return "Бира"
    if "ЕНЕРГ" in n or "ЕНЕРГ" in c:
        return "Енергийни"
    if "СТУДЕН ЧАЙ" in n or "ФЮЗ" in n:
        return "Студен чай"
    if "ВОДА" in n or c == "ВОДА":
        return "Вода"
    if "СОК" in n or "НЕКТАР" in n or "КАПИ" in n or "ЛИМОНАД" in n or "СОК" in c or "КАПИ" in c:
        return "Сокове и лимонади"
    if c.startswith("БЕЗАЛКОХОЛНИ") or re.search(r"КОЛА|ФАНТА|СПРАЙТ|ШВЕПС|ТОНИК", n):
        return "Безалкохолни"
    return "Други"


def _arts(db):
    return {a.id: a for a in db.execute(select(m.Article)).scalars().all()
            if a.is_active and "АМБАЛАЖ" not in (a.name or "").upper()}


def _last_sale(db) -> date:
    return db.execute(select(func.max(m.SalesHistory.sale_date))).scalar() or datetime.now(SOFIA).date()


def _daily(db, since: date, until: date, store_id: int | None = None):
    q = (select(m.SalesHistory.store_id, m.SalesHistory.article_id, m.SalesHistory.sale_date,
                func.sum(m.SalesHistory.quantity_sold))
         .where(m.SalesHistory.sale_date >= since, m.SalesHistory.sale_date <= until)
         .group_by(m.SalesHistory.store_id, m.SalesHistory.article_id, m.SalesHistory.sale_date))
    if store_id:
        q = q.where(m.SalesHistory.store_id == store_id)
    return [(s, a, d, max(float(v or 0), 0.0)) for s, a, d, v in db.execute(q).all()]


def _stock_now(db) -> dict:
    """(store, article) -> последна наличност."""
    rows = db.execute(text("""SELECT DISTINCT ON (store_id, article_id) store_id, article_id, quantity
                              FROM stock_snapshots ORDER BY store_id, article_id, captured_at DESC""")).all()
    return {(s, a): float(q) for s, a, q in rows}


def _price(a) -> float:
    return float(a.delivery_price or 0)


# ---------------------------------------------------------------- групи
def groups(db: Session, days: int = 14) -> dict:
    arts, stores = _arts(db), {s.id: s.name for s in db.execute(select(m.Store).where(m.Store.is_active.is_(True))).scalars()}
    until = _last_sale(db)
    since = until - timedelta(days=days - 1)
    half = since + timedelta(days=days // 2)
    g = defaultdict(lambda: {"units": 0.0, "eur": 0.0, "recent": 0.0, "prev": 0.0, "stock": 0.0, "stock_eur": 0.0,
                             "series": defaultdict(float), "stores": set()})
    for s, a, d, q in _daily(db, since, until):
        if a not in arts or s not in stores:
            continue
        x = g[group_of(arts[a])]
        x["units"] += q; x["eur"] += q * _price(arts[a]); x["series"][d] += q
        x["recent" if d >= half else "prev"] += q
        if q > 0:
            x["stores"].add(s)
    for (s, a), q in _stock_now(db).items():
        if a in arts and s in stores and q > 0:
            x = g[group_of(arts[a])]
            x["stock"] += q; x["stock_eur"] += q * _price(arts[a])
    total = sum(x["units"] for x in g.values()) or 1
    rdays = (until - half).days + 1
    days_list = [since + timedelta(days=i) for i in range(days)]
    out = []
    for name in GROUPS:
        if name not in g:
            continue
        x = g[name]
        per_day = x["recent"] / rdays if rdays else 0
        out.append({"group": name, "units": round(x["units"]), "eur": round(x["eur"], 2),
                    "share": round(100 * x["units"] / total, 1),
                    "trend": round(100 * (x["recent"] - x["prev"]) / x["prev"], 1) if x["prev"] else None,
                    "stock": round(x["stock"]), "stock_eur": round(x["stock_eur"], 2),
                    "cover_days": round(x["stock"] / per_day, 1) if per_day else None,
                    "stores": len(x["stores"]),
                    "series": [round(x["series"].get(d, 0)) for d in days_list]})
    return {"from": since.strftime("%d.%m"), "to": until.strftime("%d.%m"), "days": [d.strftime("%d.%m") for d in days_list],
            "groups": out}


def group_detail(db: Session, group: str, days: int = 14) -> dict:
    arts = {k: v for k, v in _arts(db).items() if group_of(v) == group}
    stores = {s.id: s.name for s in db.execute(select(m.Store).where(m.Store.is_active.is_(True))).scalars()}
    until = _last_sale(db); since = until - timedelta(days=days - 1); half = since + timedelta(days=days // 2)
    rdays = (until - half).days + 1
    pa = defaultdict(lambda: [0.0, 0.0, 0.0]); ps = defaultdict(lambda: [0.0, 0.0, 0.0])
    for s, a, d, q in _daily(db, since, until):
        if a in arts and s in stores:
            i = 1 if d >= half else 2
            pa[a][0] += q; pa[a][i] += q; ps[s][0] += q; ps[s][i] += q
    st = _stock_now(db); sa = defaultdict(float); ss = defaultdict(float)
    for (s, a), q in st.items():
        if a in arts and s in stores and q > 0:
            sa[a] += q; ss[s] += q
    def row(units, rec, prev, stock):
        pdy = rec / rdays if rdays else 0
        return {"units": round(units), "trend": round(100 * (rec - prev) / prev, 1) if prev else None,
                "stock": round(stock), "cover_days": round(stock / pdy, 1) if pdy else None}
    articles = sorted([{"sku": arts[a].sku, "name": arts[a].supplier_name or arts[a].name, "brand": arts[a].category,
                        "eur": round(pa[a][0] * _price(arts[a]), 2), **row(*pa[a], sa[a])} for a in arts],
                      key=lambda r: -r["units"])
    by_store = sorted([{"store_id": s, "store": stores[s], **row(*ps[s], ss[s])} for s in stores],
                      key=lambda r: -r["units"])
    return {"group": group, "from": since.strftime("%d.%m"), "to": until.strftime("%d.%m"),
            "articles": articles, "stores": by_store}


# ---------------------------------------------------------------- брошура
def promo_report(db: Session) -> dict:
    from . import promo as P
    P._ensure(db); db.commit()
    arts = {a.id: a for a in db.execute(select(m.Article)).scalars().all()}
    stores = {s.id: s.name for s in db.execute(select(m.Store).where(m.Store.is_active.is_(True))).scalars()}
    last = _last_sale(db)
    out = []
    for pid, name, s, e in db.execute(text("SELECT id, name, start_day, end_day FROM promotions ORDER BY start_day DESC")).all():
        aids = [a for (a,) in db.execute(text("SELECT article_id FROM promotion_items WHERE promo_id=:p"), {"p": pid}).all()]
        if not aids:
            continue
        b0, p_end = s - timedelta(days=14), min(e, last)
        pdays = max((p_end - s).days + 1, 0)
        rows = _daily(db, b0, p_end)
        series = defaultdict(float); per = defaultdict(lambda: [0.0, 0.0]); per_store = defaultdict(lambda: [0.0, 0.0])
        for st, a, d, q in rows:
            if a not in aids or st not in stores:
                continue
            series[d] += q
            per[a][0 if d < s else 1] += q
        outs = defaultdict(set)
        for st, a, d in db.execute(text("""SELECT store_id, article_id, (captured_at AT TIME ZONE 'Europe/Sofia')::date
                                           FROM stock_snapshots WHERE quantity <= 0 AND article_id = ANY(:a)
                                           AND captured_at >= :s"""), {"a": aids, "s": s}).all():
            if st in stores:
                outs[a].add(st)
        items = []
        for a in aids:
            if a not in arts:
                continue
            bpd, dpd = per[a][0] / 14, (per[a][1] / pdays if pdays else 0)
            extra = per[a][1] - bpd * pdays
            items.append({"sku": arts[a].sku, "name": arts[a].supplier_name or arts[a].name,
                          "before_per_day": round(bpd, 1), "during_per_day": round(dpd, 1),
                          "uplift": round(dpd / bpd, 2) if bpd else None, "units": round(per[a][1]),
                          "extra_units": round(extra), "eur": round(per[a][1] * _price(arts[a]), 2),
                          "extra_eur": round(extra * _price(arts[a]), 2), "stores_out": len(outs[a])})
        items.sort(key=lambda r: -(r["extra_units"] or 0))
        days_list = [b0 + timedelta(days=i) for i in range((p_end - b0).days + 1)]
        out.append({"id": pid, "name": name, "start": s.strftime("%d.%m"), "end": e.strftime("%d.%m"),
                    "promo_days": pdays, "data_until": last.strftime("%d.%m"),
                    "units": sum(i["units"] for i in items), "extra_units": sum(i["extra_units"] for i in items),
                    "eur": round(sum(i["eur"] for i in items), 2), "extra_eur": round(sum(i["extra_eur"] for i in items), 2),
                    "stores_out": len(set().union(*outs.values())) if outs else 0,
                    "days": [d.strftime("%d.%m") for d in days_list], "promo_start_index": 14,
                    "series": [round(series.get(d, 0)) for d in days_list], "items": items})
    return {"promos": out}


def promo_article_stores(db: Session, sku: str, promo_id: int) -> dict:
    a = db.execute(select(m.Article).where(m.Article.sku == sku)).scalar_one()
    s, e = db.execute(text("SELECT start_day, end_day FROM promotions WHERE id=:p"), {"p": promo_id}).one()
    stores = {x.id: x.name for x in db.execute(select(m.Store).where(m.Store.is_active.is_(True))).scalars()}
    last = _last_sale(db); p_end = min(e, last); pdays = max((p_end - s).days + 1, 1)
    per = defaultdict(lambda: [0.0, 0.0])
    for st, aid, d, q in _daily(db, s - timedelta(days=14), p_end):
        if aid == a.id and st in stores:
            per[st][0 if d < s else 1] += q
    stock = _stock_now(db)
    rows = [{"store": stores[st], "before_per_day": round(v[0] / 14, 1), "during_per_day": round(v[1] / pdays, 1),
             "uplift": round((v[1] / pdays) / (v[0] / 14), 2) if v[0] else None,
             "stock": stock.get((st, a.id))} for st, v in per.items()]
    rows.sort(key=lambda r: -r["during_per_day"])
    return {"sku": sku, "name": a.supplier_name or a.name, "rows": rows}
