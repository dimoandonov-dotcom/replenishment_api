"""
„Провери артикул“: защо даден артикул е/не е в заявката на магазина -
мин/макс, наличност, продажби, опаковка и ясна причина.
"""
from __future__ import annotations
from zoneinfo import ZoneInfo

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import models as m, service

SOFIA = ZoneInfo("Europe/Sofia")


def _n(x) -> str:
    if x is None:
        return "—"
    return (f"{x:g}" if float(x) == int(x) else f"{x:.1f}").replace(".", ",")


def check(db: Session, store_id: int, q: str) -> dict:
    from .engine import SKIP_COVER_DAYS, MAX_PACK_COVER_DAYS
    q = (q or "").strip()
    if not q:
        return {"rows": []}
    st = db.get(m.Store, store_id)
    stmt = select(m.Article)
    if q.isdigit():
        stmt = stmt.where(m.Article.sku == q)
    else:
        for w in q.split():
            like = f"%{w}%"
            stmt = stmt.where(or_(m.Article.name.ilike(like), m.Article.supplier_name.ilike(like)))
    arts = db.execute(stmt.limit(15)).scalars().all()
    if not arts:
        return {"store": st.name if st else "", "rows": [], "msg": "Няма такъв артикул в НДК номенклатурата"}
    ids = [a.id for a in arts]
    sett = {s.article_id: s for s in db.execute(select(m.StoreArticleSetting).where(
        m.StoreArticleSetting.store_id == store_id, m.StoreArticleSetting.article_id.in_(ids))).scalars()}
    plano = set(db.execute(select(m.Planogram.article_id).where(
        m.Planogram.store_id == store_id, m.Planogram.article_id.in_(ids))).scalars())
    stock = service.latest_stock_map(db, store_id)
    avg = service.load_avg_daily_sales(db, store_id)
    today = datetime.now(SOFIA).date()
    try:
        res = service.calculate_for_store(db, store_id, today, None, False)
        lines = {ln.article_id: ln for ln in res.lines}
    except Exception:
        db.rollback()
        lines = {}
    in_review = set(db.execute(select(m.SettingsReviewQueue.article_id).where(
        m.SettingsReviewQueue.store_id == store_id, m.SettingsReviewQueue.resolved.is_(False),
        m.SettingsReviewQueue.article_id.in_(ids))).scalars())
    rows = []
    for a in arts:
        s = sett.get(a.id)
        sq = stock.get(a.id)
        ad = avg.get((store_id, a.id), 0.0)
        pk = max(int(a.pack_size or 1), 1)
        mn = float(s.min_stock) if s else None
        mx = float(s.max_stock) if s else None
        ln = lines.get(a.id)
        if a.no_order:
            ok, why = False, f"🚫 Маркиран „Не се поръчва“{(' — ' + a.no_order_reason) if a.no_order_reason else ''}. Махни го от раздел „Не се поръчват“, ако трябва да се зарежда."
        elif not a.is_active:
            ok, why = False, "⛔ Артикулът е неактивен в номенклатурата — не се поръчва."
        elif a.id in in_review:
            ok, why = False, "⏳ Мин/макс чака преглед (опашка за проверка) — докато не се потвърди, не се поръчва."
        elif a.id not in plano:
            ok, why = False, "📋 Няма „да“ в планограмата на този магазин — не се поръчва."
        elif s is None:
            ok, why = False, "❗ Няма мин/макс за този магазин — не е в асортимента му. Задай мин/макс от „Асортимент и цени“."
        elif mx == 0:
            ok, why = False, "⏸ Мин/макс е 0–0 (спрян, защото не се е продавал). Вдигни мин/макс, ако трябва да се зарежда."
        elif ln is not None:
            ok, why = True, f"✅ Влиза в заявката: {ln.ordered_quantity} бр." + (f" ({ln.notes})" if ln.notes else "")
        elif sq is None:
            ok, why = False, "❓ Няма данни за наличността от Мистрал."
        elif sq >= mn:
            ok, why = False, f"✔ Наличността {_n(sq)} бр. е над минимума {_n(mn)} — не трябва да се поръчва."
        elif ad > 0 and sq >= ad * SKIP_COVER_DAYS:
            ok, why = False, (f"✔ Под минимума, но наличността {_n(sq)} бр. стига за {_n(sq / ad)} дни "
                              f"(продава {_n(ad)}/ден) — утре пак има доставка.")
        elif ad <= 0:
            ok, why = False, f"💤 Под минимума ({_n(sq)} бр.), но не се е продавал последните 14 дни — не трупаме."
        elif pk > 1 and pk / ad > MAX_PACK_COVER_DAYS:
            ok, why = False, (f"📦 Под минимума, но опаковката е {pk} бр. и би стигнала за {_n(pk / ad)} дни "
                              f"(продава {_n(ad)}/ден) — не се поръчва цял стек за толкова дълго.")
        else:
            ok, why = False, (f"📦 Под минимума, но липсват само {_n(max(mx - sq, 0))} бр. — под половин опаковка от {pk} бр.; "
                              "ще влезе, когато наличността падне още.")
        rows.append({"sku": a.sku, "name": a.supplier_name or a.name, "min": mn, "max": mx,
                     "manual": bool(s and not s.auto_adjust), "stock": sq, "per_day": round(ad, 2), "pack": pk,
                     "planogram": a.id in plano, "in_order": ok, "qty": ln.ordered_quantity if ln is not None else 0,
                     "why": why})
    rows.sort(key=lambda r: (not r["planogram"], r["name"]))
    return {"store": st.name if st else "", "rows": rows}
