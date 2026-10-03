"""
„Качество на заявките" - проверява резултата в магазините, без да зависи от Ани.

За всяка позиция магазин × артикул с „да" в планограмата и мин/макс > 0:
  out   - свършил: наличност <= 0, а се продава (поръчали сме МАЛКО)
  over  - свръхзапас: стига за над 21 дни и е над макс (поръчали сме МНОГО)
  low   - под минимума: влиза в днешната заявка (нормално)
  ok    - точно
  idle  - 0 наличност и 0 продажби (никога не е зареждан) - не се брои
Оценка = (точни + под минимума) / (всички броени).
Всяка нощ се записва снимка за деня -> тенденция по дни.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import models as m
from . import service

SOFIA = timezone(timedelta(hours=3))
OVER_DAYS = 21
LABEL = {"out": "свършил", "over": "свръхзапас", "low": "под минимума — в заявката", "ok": "точно"}


def positions(db: Session, store_id: int | None = None) -> list[dict]:
    since = datetime.now(SOFIA).date() - timedelta(days=14)
    q = (select(m.SalesHistory.store_id, m.SalesHistory.article_id, func.sum(m.SalesHistory.quantity_sold))
         .where(m.SalesHistory.sale_date >= since)
         .group_by(m.SalesHistory.store_id, m.SalesHistory.article_id))
    if store_id:
        q = q.where(m.SalesHistory.store_id == store_id)
    sales = {(s, a): max(float(v or 0), 0.0) for s, a, v in db.execute(q).all()}
    arts = {a.id: a for a in db.execute(select(m.Article)).scalars().all()}
    plano = defaultdict(set)
    pq = select(m.Planogram.store_id, m.Planogram.article_id)
    if store_id:
        pq = pq.where(m.Planogram.store_id == store_id)
    for s, a in db.execute(pq).all():
        plano[s].add(a)
    sq = select(m.StoreArticleSetting)
    if store_id:
        sq = sq.where(m.StoreArticleSetting.store_id == store_id)
    sett = defaultdict(dict)
    for x in db.execute(sq).scalars().all():
        sett[x.store_id][x.article_id] = x
    stores = {s.id: s.name for s in db.execute(select(m.Store).where(m.Store.is_active.is_(True))).scalars().all()}
    out = []
    for sid, name in stores.items():
        if store_id and sid != store_id:
            continue
        stock = service.latest_stock_map(db, sid)
        for aid in plano.get(sid, ()):
            a, s = arts.get(aid), sett[sid].get(aid)
            if a is None or not a.is_active or a.no_order or s is None or float(s.max_stock) <= 0:
                continue
            st = stock.get(aid)
            if st is None:
                continue
            sold = sales.get((sid, aid), 0.0)
            sdp = sold / 14
            mn, mx = float(s.min_stock), float(s.max_stock)
            if st <= 0 and sold <= 0:
                status = "idle"
            elif st <= 0:
                status = "out"
            elif (sdp > 0 and st / sdp > OVER_DAYS and st > mx) or (sdp == 0 and st > mx):
                status = "over"
            elif st < mn:
                status = "low"
            else:
                status = "ok"
            out.append({"store_id": sid, "store": name, "sku": a.sku, "name": a.supplier_name or a.name,
                        "stock": st, "min": mn, "max": mx, "sold_14d": round(sold, 1), "per_day": round(sdp, 2),
                        "cover_days": round(st / sdp, 1) if sdp > 0 and st > 0 else None, "status": status})
    return out


def _agg(rows: list[dict]) -> dict:
    c = defaultdict(int)
    for r in rows:
        c[r["status"]] += 1
    counted = c["out"] + c["over"] + c["low"] + c["ok"]
    good = c["ok"] + c["low"]
    return {"positions": counted, "ok": c["ok"], "low": c["low"], "out": c["out"], "over": c["over"],
            "idle": c["idle"], "pct_ok": round(100 * good / counted, 1) if counted else 0.0}


def compute(db: Session) -> dict:
    rows = positions(db)
    by = defaultdict(list)
    for r in rows:
        by[(r["store_id"], r["store"])].append(r)
    stores = [{"store_id": k[0], "store": k[1], **_agg(v)} for k, v in by.items()]
    stores.sort(key=lambda r: (-r["out"], -r["over"], r["store"]))
    return {"at": datetime.now(SOFIA).strftime("%d.%m.%Y %H:%M"), "totals": _agg(rows), "stores": stores}


def _ensure(db: Session):
    db.execute(text("""CREATE TABLE IF NOT EXISTS quality_daily (
        day DATE PRIMARY KEY, data JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())"""))


def record(db: Session, day: date | None = None) -> dict:
    """Записва снимка за деня (вика се всяка нощ; за вчерашния ден)."""
    _ensure(db)
    day = day or datetime.now(SOFIA).date()
    c = compute(db)
    data = {"totals": c["totals"], "stores": [{k: s[k] for k in ("store_id", "store", "pct_ok", "out", "over", "positions")}
                                               for s in c["stores"]]}
    db.execute(text("""INSERT INTO quality_daily(day, data) VALUES (:d, CAST(:j AS JSONB))
                       ON CONFLICT (day) DO UPDATE SET data = EXCLUDED.data, created_at = now()"""),
               {"d": day, "j": json.dumps(data, ensure_ascii=False)})
    db.commit()
    return {"day": day.isoformat(), **c["totals"]}


def history(db: Session, days: int = 30) -> list[dict]:
    _ensure(db)
    db.commit()
    rows = db.execute(text("SELECT day, data FROM quality_daily WHERE day >= :d ORDER BY day"),
                      {"d": datetime.now(SOFIA).date() - timedelta(days=days)}).all()
    return [{"day": d.strftime("%d.%m"), **(j if isinstance(j, dict) else json.loads(j))["totals"]} for d, j in rows]


def prune_snapshots(db: Session, keep_days: int = 0, max_days: int = 60) -> int:
    """Пазим само последната наличност за всеки ден и не по-стари от max_days."""
    old = db.execute(text("DELETE FROM stock_snapshots WHERE captured_at < now() - make_interval(days => :d)"),
                     {"d": max_days}).rowcount or 0
    r = db.execute(text("""
        DELETE FROM stock_snapshots s USING (
          SELECT id FROM (
            SELECT id, row_number() OVER (
              PARTITION BY store_id, article_id, (captured_at AT TIME ZONE 'Europe/Sofia')::date
              ORDER BY captured_at DESC) rn
            FROM stock_snapshots WHERE captured_at < now() - make_interval(days => :k)
          ) x WHERE rn > 1
        ) d WHERE s.id = d.id"""), {"k": keep_days})
    db.commit()
    return old + (r.rowcount or 0)
