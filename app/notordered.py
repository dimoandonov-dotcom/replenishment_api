"""
„Не се поръчват" - всичко, което при сегашните настройки НИКОГА няма да
влезе в заявка за даден обект, групирано по причина, с продажби и наличност,
за да се види веднага кое се продава, а не се поръчва.

Причини:
  no_order      - отбелязан „Не се поръчва" в асортимента
  stopped       - спрян 0-0 (Viber правила / явен стоп код / ръчно)
  no_minmax     - по планограма, но без мин/макс
  inactive      - артикулът е спрян/изваден от НДК
  not_in_plano  - продава се в обекта, но не е в планограмата му
(„над минимума" не е тук - това е нормално, ще се поръча, щом падне.)
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models as m
from . import service

SOFIA = timezone(timedelta(hours=3))
REASONS = {
    "no_order": "🚫 Отбелязан „Не се поръчва“",
    "stopped": "⛔ Спрян 0-0",
    "no_minmax": "❔ Без мин/макс",
    "inactive": "💤 Изваден от НДК",
    "not_in_plano": "📋 Продава се, но не е в планограмата",
}


def _sales14(db: Session, store_id: int | None = None) -> dict:
    since = datetime.now(SOFIA).date() - timedelta(days=14)
    q = (select(m.SalesHistory.store_id, m.SalesHistory.article_id,
                func.sum(m.SalesHistory.quantity_sold))
         .where(m.SalesHistory.sale_date >= since)
         .group_by(m.SalesHistory.store_id, m.SalesHistory.article_id))
    if store_id is not None:
        q = q.where(m.SalesHistory.store_id == store_id)
    return {(s, a): max(float(v or 0), 0.0) for s, a, v in db.execute(q).all()}


def store_rows(db: Session, store_id: int, arts=None, sales=None, plano_all=None, settings_all=None) -> list[dict]:
    from .learning import formula, _capped
    arts = arts or {a.id: a for a in db.execute(select(m.Article)).scalars().all()}
    sales = sales if sales is not None else _sales14(db, store_id)
    plano = (plano_all.get(store_id, set()) if plano_all is not None else set(db.execute(
        select(m.Planogram.article_id).where(m.Planogram.store_id == store_id)).scalars().all()))
    settings = (settings_all.get(store_id, {}) if settings_all is not None else {
        s.article_id: s for s in db.execute(select(m.StoreArticleSetting)
                                            .where(m.StoreArticleSetting.store_id == store_id)).scalars().all()})
    stock = service.latest_stock_map(db, store_id)
    sold_ids = {a for (s, a), v in sales.items() if s == store_id and v > 0}
    rows = []
    for aid in plano | sold_ids:
        a = arts.get(aid)
        if a is None or "АМБАЛАЖ" in (a.name or "").upper():
            continue
        s = settings.get(aid)
        in_plano = aid in plano
        sold = sales.get((store_id, aid), 0.0)
        if a.no_order:
            reason = "no_order"
        elif not a.is_active:
            if not in_plano and sold <= 0:
                continue
            reason = "inactive"
        elif in_plano and s is None:
            reason = "no_minmax"
        elif in_plano and float(s.max_stock) == 0:
            reason = "stopped"
        elif not in_plano and sold > 0:
            reason = "not_in_plano"
        else:
            continue
        cls, mn, mx = formula(sold)
        if (_capped(a.name) or _capped(a.supplier_name or "")) and mx > 3:
            mn, mx = min(mn, 3), 3
        rows.append({
            "sku": a.sku, "name": a.supplier_name or a.name, "category": a.category,
            "reason": reason, "reason_label": REASONS[reason],
            "in_planogram": in_plano, "sold_14d": round(sold, 1), "per_day": round(sold / 14, 2),
            "stock": stock.get(aid), "no_order_reason": a.no_order_reason, "no_order_by": a.no_order_by,
            "suggest_min": mn if sold > 0 else None, "suggest_max": mx if sold > 0 else None,
        })
    rows.sort(key=lambda r: (-r["sold_14d"], r["reason"], r["name"] or ""))
    return rows


def summary(db: Session) -> dict:
    arts = {a.id: a for a in db.execute(select(m.Article)).scalars().all()}
    sales = _sales14(db)
    plano_all, settings_all = defaultdict(set), defaultdict(dict)
    for sid, aid in db.execute(select(m.Planogram.store_id, m.Planogram.article_id)).all():
        plano_all[sid].add(aid)
    for s in db.execute(select(m.StoreArticleSetting)).scalars().all():
        settings_all[s.store_id][s.article_id] = s
    out, tot = [], defaultdict(int)
    for st in db.execute(select(m.Store).where(m.Store.is_active.is_(True)).order_by(m.Store.name)).scalars().all():
        rows = store_rows(db, st.id, arts, sales, plano_all, settings_all)
        c = defaultdict(int)
        selling = 0
        for r in rows:
            c[r["reason"]] += 1
            tot[r["reason"]] += 1
            if r["sold_14d"] > 0:
                selling += 1
        tot["selling"] += selling
        out.append({"store_id": st.id, "store": st.name, "total": len(rows),
                    "selling": selling, **{k: c[k] for k in REASONS}})
    out.sort(key=lambda r: (-r["selling"], -r["total"]))
    return {"reasons": REASONS, "totals": dict(tot), "stores": out}
