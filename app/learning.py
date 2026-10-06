"""
Самообучение на мин/макс (пуска се всяка нощ).

Учи се от:
  1) реалните продажби от Мистрал (последни 14 дни) - формулата на офиса;
  2) изчерпване: наличност <= 0 при артикул, който се продава -> +20%;
  3) заявките на Ани - САМО когато продажбите потвърждават, че е права:
     поръчала е поне 2 пъти, когато ние не бихме, И наличността в тези
     моменти е била под минимума, който продажбите показват.

Никога не пипа:
  - заключени позиции (коригирани от човек в пулта);
  - спрени 0-0 и явните стоп кодове;
  - вина 0.75 и твърд алкохол 0.5/0.7 над таван 3.

Промените по формулата са плавни: най-много ±30% наведнъж и не по-често
от веднъж седмично за една позиция. Изчерпване и потвърден сигнал от Ани
се прилагат веднага.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models as m
from . import service

SOFIA = timezone(timedelta(hours=3))
DAYS = 14
L, R, Z, D, MIN_SHELF = 1, 1, 1.65, 2, 1
CLASSES = [("A", 3, 2), ("B", 1, 3), ("C", 0.3, 5), ("D", 0, 7)]
MAX_STEP = 0.30            # най-много ±30% наведнъж
FORMULA_EVERY_DAYS = 7     # промяна по формулата - веднъж седмично на позиция
STOCKOUT_BOOST = 1.20
ANI_MIN_TIMES = 2

EXPLICIT_STOP = set(
    "1480,1477,15938,1550,24951,24952,11158,7001,15936,"
    "1546,1574,24948,24949,24947,15586,15585".split(","))   # 15940, 15937, 24950 пуснати отново (Димо, 06.10)

_C = lambda n: (n or "").upper()  # noqa: E731
_EXCL = ["БИРА", "СТУДЕН ЧАЙ", "САЙДЕР"]
_HARD = ["РАКИЯ", "ВОДКА", "УИСКИ", "МЕНТА", "МАСТИКА", "УЗО"]


def _capped(name: str) -> bool:
    n = _C(name)
    wine = "ВИНО" in n and re.search(r"750\s*МЛ|0[.,]7\s*Л", n)
    hard = (not any(x in n for x in _EXCL) and any(k in n for k in _HARD)
            and re.search(r"0[.,]?[57]\s*Л\b|500\s*МЛ|700\s*МЛ|(?<![0-9])0[.,][57](?![0-9])", n))
    return bool(wine or hard)


VERY_SLOW = 0.15   # под 0.15 бр./ден (по-малко от 2 бр. за 14 дни): само 1 бройка на рафта


def formula(sold: float) -> tuple[str, int, int]:
    sdp = max(sold, 0) / DAYS
    cls = next(c for c, th, _ in CLASSES if sdp >= th)
    if 0 < sdp < VERY_SLOW:
        return cls, 1, 2
    cover = dict((c, cv) for c, _, cv in CLASSES)[cls]
    mn = max(math.ceil(sdp * (L + R) + Z * math.sqrt(D * sdp * (L + R))), MIN_SHELF)
    return cls, mn, mn + math.ceil(sdp * cover)


def _step(old: float, target: float) -> float:
    lim = max(1.0, abs(old) * MAX_STEP)
    return old + max(-lim, min(lim, target - old))


def run(db: Session, apply: bool = True) -> dict:
    started = datetime.now(timezone.utc)
    notes = []
    # 1) свежи данни + нощни задачи - само при истинското учене (apply=True);
    #    пробата („Какво би научил сега") не дърпа и не записва нищо
    if apply:
        try:
            from . import mistral
            if mistral.configured():
                mistral.sync_sales(db, DAYS)
                mistral.sync_stock(db)
        except Exception as e:  # учим по наличните данни
            notes.append(f"Мистрал: {type(e).__name__}")
        # снимка на качеството за вчера (наличностите в 02:30 = края на деня) + почистване
        try:
            from . import quality
            notes.append(f"качество: {quality.record(db, datetime.now(SOFIA).date() - timedelta(days=1))['pct_ok']}%")
            notes.append(f"изчистени стари наличности: {quality.prune_snapshots(db)}")
        except Exception as e:
            db.rollback()
            notes.append(f"качество: {type(e).__name__}")
        # продажби/наличности/промоции на ВСИЧКИ доставчици (за анализите)
        try:
            from . import salesall
            notes.append(f"всички доставчици: {salesall.sync(db, 3)}")
        except Exception as e:
            db.rollback()
            notes.append(f"всички доставчици: {type(e).__name__}")
        # движения и ревизии: последните 3 дни (по-старите вече са в базата)
        try:
            from . import anomalies
            notes.append(f"движения/ревизии: {anomalies.sync(db, 3)}")
            notes.append(f"двойни документи: {anomalies.detect_duplicate_docs(db, 3)}")
        except Exception as e:
            db.rollback()
            notes.append(f"аномалии: {type(e).__name__}")

    today = datetime.now(SOFIA).date()
    since = today - timedelta(days=DAYS)
    last_sale = db.execute(select(func.max(m.SalesHistory.sale_date))).scalar()
    if last_sale is None or last_sale < today - timedelta(days=2):
        # без свежи продажби не учим - иначе ще свалим мин/макс погрешно
        return {"applied": False, "changes": 0, "counts": {}, "ani_unconfirmed": [],
                "sample": [], "notes": notes + [f"няма свежи продажби (последна дата {last_sale})"],
                "at": datetime.now(SOFIA).strftime("%d.%m.%Y %H:%M"), "seconds": 0}
    # промо дните не се учат: за промо артикулите продажбите се смятат само от
    # дните извън кампанията и се мащабират до 14 дни
    from . import promo as _promo
    pdays = _promo.promo_days(db)
    sales = defaultdict(float)
    promo_sales = defaultdict(float)   # продажбите в промо дните (за нови позиции без друга история)
    for sid, aid, d, q in db.execute(
        select(m.SalesHistory.store_id, m.SalesHistory.article_id, m.SalesHistory.sale_date,
               func.sum(m.SalesHistory.quantity_sold))
        .where(m.SalesHistory.sale_date >= since)
        .group_by(m.SalesHistory.store_id, m.SalesHistory.article_id, m.SalesHistory.sale_date)
    ).all():
        if aid in pdays and any(a <= d <= b for a, b in pdays[aid]):
            promo_sales[(sid, aid)] += float(q or 0)
            continue
        sales[(sid, aid)] += float(q or 0)
    for aid, periods in pdays.items():
        promo_n = sum(1 for i in range(DAYS) if any(a <= since + timedelta(days=i) <= b for a, b in periods))
        if 0 < promo_n < DAYS:
            k = DAYS / (DAYS - promo_n)
            for key in [x for x in sales if x[1] == aid]:
                sales[key] *= k
    if apply:
        try:
            _promo.measure(db)   # обнови реалния ефект на промоциите
        except Exception:
            db.rollback()

    arts = {a.id: a for a in db.execute(select(m.Article)).scalars().all()}
    sku2id = {a.sku: a.id for a in arts.values()}

    # сигнали от Ани: поръчала е, а ние не бихме (наличност над нашия мин)
    ani = defaultdict(list)  # (store, article) -> [наличност в момента на заявката]
    for sid, sku, st in db.execute(
        select(m.ManualOrder.store_id, m.ManualOrderLine.sku, m.ManualOrderLine.stock)
        .join(m.ManualOrderLine, m.ManualOrderLine.order_id == m.ManualOrder.id)
        .where(m.ManualOrder.received_at >= datetime.now(timezone.utc) - timedelta(days=DAYS),
               m.ManualOrderLine.store_qty > 0, m.ManualOrderLine.api_qty == 0,
               m.ManualOrder.store_id.isnot(None))
    ).all():
        aid = sku2id.get(sku)
        if aid is not None:
            ani[(sid, aid)].append(float(st) if st is not None else None)

    recent = set(db.execute(
        select(m.SettingsLog.store_id, m.SettingsLog.article_id)
        .where(m.SettingsLog.source == "learning",
               m.SettingsLog.created_at >= datetime.now(timezone.utc)
               - timedelta(days=FORMULA_EVERY_DAYS))
    ).all())

    changes, signals = [], []
    counts = defaultdict(int)
    stores = db.execute(select(m.Store).where(m.Store.is_active.is_(True))).scalars().all()
    for store in stores:
        plano = set(db.execute(
            select(m.Planogram.article_id).where(m.Planogram.store_id == store.id)
        ).scalars().all())
        settings = {s.article_id: s for s in db.execute(
            select(m.StoreArticleSetting).where(m.StoreArticleSetting.store_id == store.id)
        ).scalars().all()}
        stock = service.latest_stock_map(db, store.id)

        for aid in plano:
            a = arts.get(aid)
            if a is None or not a.is_active or a.no_order or a.sku in EXPLICIT_STOP:
                continue
            if "АМБАЛАЖ" in _C(a.name):
                continue
            s = settings.get(aid)
            if s is not None and s.auto_adjust is False:
                counts["заключени"] += 1
                continue
            sold = sales.get((store.id, aid), 0.0)
            if s is None and sold <= 0 and promo_sales.get((store.id, aid), 0.0) > 0:
                # нова позиция, продава се само в промото: базов мин/макс по половината промо продажби
                sold = promo_sales[(store.id, aid)] * 0.5
            if s is not None and float(s.max_stock) == 0:
                if sold <= 0:
                    continue  # спрян 0-0 без продажби остава спрян
                # има продажби за 14 дни -> пуска се отново по формулата (Димо, 06.10)
            if s is None and sold <= 0:
                continue  # никога непродаван и без настройка - не го пускаме

            cls, mn_t, mx_t = formula(sold)
            reason = f"продажби {sold:g} бр./14 дни, клас {cls}"
            urgent = False
            if s is not None and float(s.max_stock) == 0:
                reason = f"пуснат отново: продава {sold:g} бр./14 дни, клас {cls}"
                urgent = True
            q = stock.get(aid)
            if q is not None and q <= 0 and sold > 0:
                mn_t, mx_t = math.ceil(mn_t * STOCKOUT_BOOST), math.ceil(mx_t * STOCKOUT_BOOST)
                reason = f"свърши (наличност {q:g}) при продажби {sold:g} бр./14 дни"
                urgent = True
            hits = ani.get((store.id, aid), [])
            if len(hits) >= ANI_MIN_TIMES:
                old_min = float(s.min_stock) if s else 0
                confirmed = (mn_t > old_min and
                             sum(1 for h in hits if h is not None and h < mn_t) >= ANI_MIN_TIMES)
                if confirmed:
                    reason = (f"Ани поръча {len(hits)} пъти и продажбите го потвърждават "
                              f"({sold:g} бр./14 дни)")
                    urgent = True
                else:
                    signals.append({"store": store.name, "sku": a.sku,
                                    "name": a.supplier_name or a.name, "times": len(hits),
                                    "sold_14d": sold})
            capped = _capped(a.name) or _capped(a.supplier_name or "")
            if capped and mx_t > 3:
                mn_t, mx_t = min(mn_t, 3), 3

            if s is None:
                new_min, new_max, kind = mn_t, mx_t, "нов"
            else:
                # веднъж седмично на позиция - и за свършване; само потвърден
                # сигнал от Ани може по-често
                if (store.id, aid) in recent and not reason.startswith("Ани"):
                    continue
                om, oM = float(s.min_stock), float(s.max_stock)
                # мъртва зона: дребни разлики не се пипат (без шум всяка нощ)
                if not urgent and abs(mx_t - oM) < max(2, 0.15 * oM) and abs(mn_t - om) < max(2, 0.15 * om):
                    continue
                if urgent:
                    new_min, new_max = max(om, mn_t) if "свърши" in reason else mn_t, max(oM, mx_t)
                else:
                    new_min, new_max = round(_step(om, mn_t)), round(_step(oM, mx_t))
                new_min = max(new_min, MIN_SHELF)
                if capped:  # таванът 3 е правило - важи веднага, без плавност
                    new_min, new_max = min(new_min, 3), min(new_max, 3)
                new_max = max(new_max, new_min)
                if (new_min, new_max) == (om, oM):
                    continue
                kind = "нагоре" if new_max > oM else "надолу" if new_max < oM else "само мин"
            counts[kind] += 1
            changes.append({"store_id": store.id, "store": store.name, "article_id": aid,
                            "sku": a.sku, "name": a.supplier_name or a.name,
                            "old_min": float(s.min_stock) if s else None,
                            "old_max": float(s.max_stock) if s else None,
                            "new_min": new_min, "new_max": new_max,
                            "kind": kind, "reason": reason})

    if apply:
        for c in changes:
            row = db.get(m.StoreArticleSetting, (c["store_id"], c["article_id"]))
            if row is None:
                db.add(m.StoreArticleSetting(store_id=c["store_id"], article_id=c["article_id"],
                                             min_stock=c["new_min"], max_stock=c["new_max"],
                                             auto_adjust=True))
            else:
                row.min_stock, row.max_stock = c["new_min"], c["new_max"]
            db.add(m.SettingsLog(store_id=c["store_id"], article_id=c["article_id"],
                                 old_min=c["old_min"], old_max=c["old_max"],
                                 new_min=c["new_min"], new_max=c["new_max"],
                                 source="learning", reason=c["reason"]))
        db.commit()

    return {
        "applied": apply,
        "at": datetime.now(SOFIA).strftime("%d.%m.%Y %H:%M"),
        "seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        "changes": len(changes), "counts": dict(counts),
        "ani_unconfirmed": signals[:200], "notes": notes,
        "sample": changes[:50],
    }


def recent_log(db: Session, days: int = 14, limit: int = 500, source: str | None = None) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stores = {s.id: s.name for s in db.execute(select(m.Store)).scalars().all()}
    arts = {a.id: a for a in db.execute(select(m.Article)).scalars().all()}
    rows = db.execute(
        select(m.SettingsLog).where(m.SettingsLog.created_at >= since,
                                    *( [m.SettingsLog.source == source] if source else []))
        .order_by(m.SettingsLog.created_at.desc()).limit(limit)
    ).scalars().all()
    f = lambda v: float(v) if v is not None else None  # noqa: E731
    return [{
        "at": r.created_at.astimezone(SOFIA).strftime("%d.%m %H:%M"),
        "store_id": r.store_id, "store": stores.get(r.store_id, r.store_id),
        "sku": arts[r.article_id].sku if r.article_id in arts else "",
        "name": (arts[r.article_id].supplier_name or arts[r.article_id].name) if r.article_id in arts else "",
        "old_min": f(r.old_min), "old_max": f(r.old_max),
        "new_min": f(r.new_min), "new_max": f(r.new_max),
        "source": r.source, "reason": r.reason,
    } for r in rows]
