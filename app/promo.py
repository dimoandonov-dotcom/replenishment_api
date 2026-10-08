"""
Промо режим: по време на кампания (брошура trista.bg) мин/макс на промо артикулите
се вдигат временно с измерения ефект на промоцията, а нощното учене НЕ учи от
промо дните (иначе след кампанията мин/макс остават надути).
Настройките в базата не се променят - вдигането важи само за изчисляването на заявката.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import models as m

SOFIA = timezone(timedelta(hours=3))
DEFAULT_UPLIFT = 1.5
MIN_UPLIFT, MAX_UPLIFT = 1.0, 3.0


def _ensure(db: Session):
    db.execute(text("""CREATE TABLE IF NOT EXISTS promotions (
        id SERIAL PRIMARY KEY, name TEXT NOT NULL, start_day DATE NOT NULL, end_day DATE NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())"""))
    db.execute(text("""CREATE TABLE IF NOT EXISTS promotion_items (
        promo_id INT NOT NULL REFERENCES promotions(id) ON DELETE CASCADE,
        article_id INT NOT NULL REFERENCES articles(id),
        uplift NUMERIC(6,2) NOT NULL DEFAULT 1.5, measured BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY (promo_id, article_id))"""))


RAMP_DAYS = 3   # толкова дни преди старта на брошурата зареждането се вдига постепенно


def create(db: Session, name: str, start: date, end: date, skus: list[str]) -> dict:
    """Нова промоция или добавяне към съществуваща със същите дати."""
    _ensure(db)
    pid = db.execute(text("""SELECT id FROM promotions WHERE start_day=:s AND ABS(end_day - CAST(:e AS date)) <= 1
                             ORDER BY id LIMIT 1"""), {"s": start, "e": end}).scalar()
    if pid is None:
        pid = db.execute(text("INSERT INTO promotions(name, start_day, end_day) VALUES (:n,:s,:e) RETURNING id"),
                         {"n": name, "s": start, "e": end}).scalar()
    ids = {a.sku: a.id for a in db.execute(select(m.Article)).scalars().all()}
    added, unknown = 0, []
    for s in skus:
        if s in ids:
            db.execute(text("INSERT INTO promotion_items(promo_id, article_id, uplift) VALUES (:p,:a,:u) "
                            "ON CONFLICT DO NOTHING"), {"p": pid, "a": ids[s], "u": DEFAULT_UPLIFT})
            added += 1
        else:
            unknown.append(s)
    db.commit()
    measure(db, pid)
    return {"id": pid, "items": added, "unknown": unknown}


def measure(db: Session, promo_id: int | None = None) -> list[dict]:
    """Реален ефект: средно на ден по време на промото / средно 14 дни преди него (всички магазини)."""
    _ensure(db)
    q = "SELECT id, name, start_day, end_day FROM promotions" + (" WHERE id = :p" if promo_id else "")
    out = []
    for pid, name, s, e in db.execute(text(q), {"p": promo_id}).all():
        last = db.execute(select(func.max(m.SalesHistory.sale_date))).scalar()
        if last is None:
            continue
        p_end = min(e, last)
        p_days = (p_end - s).days + 1
        for (aid,) in db.execute(text("SELECT article_id FROM promotion_items WHERE promo_id=:p"), {"p": pid}).all():
            before = float(db.execute(select(func.coalesce(func.sum(m.SalesHistory.quantity_sold), 0)).where(
                m.SalesHistory.article_id == aid, m.SalesHistory.sale_date >= s - timedelta(days=14),
                m.SalesHistory.sale_date < s)).scalar() or 0)
            n_before = db.execute(select(func.count(func.distinct(m.SalesHistory.sale_date))).where(
                m.SalesHistory.sale_date >= s - timedelta(days=14), m.SalesHistory.sale_date < s)).scalar() or 0
            during = float(db.execute(select(func.coalesce(func.sum(m.SalesHistory.quantity_sold), 0)).where(
                m.SalesHistory.article_id == aid, m.SalesHistory.sale_date >= s,
                m.SalesHistory.sale_date <= p_end)).scalar() or 0)
            b, d = (before / n_before if n_before else 0), (during / p_days if p_days > 0 else 0)
            measured = p_days >= 2 and b > 0
            up = min(MAX_UPLIFT, max(MIN_UPLIFT, d / b)) if measured else DEFAULT_UPLIFT
            db.execute(text("UPDATE promotion_items SET uplift=:u, measured=:m WHERE promo_id=:p AND article_id=:a"),
                       {"u": round(up, 2), "m": measured, "p": pid, "a": aid})
            a = db.get(m.Article, aid)
            out.append({"promo": name, "sku": a.sku, "name": a.supplier_name or a.name, "before_per_day": round(b, 1),
                        "during_per_day": round(d, 1), "uplift": round(up, 2), "measured": measured, "promo_days": p_days})
    db.commit()
    return out


def _factors(db: Session, day: date) -> tuple[dict[int, float], dict[int, float], dict[int, str]]:
    """(започнали, предстоящи до RAMP_DAYS дни, етикети) - article_id -> коефициент за деня.
    Преди старта коефициентът расте постепенно: 1/4, 2/4, 3/4 от вдигането, в деня на старта - цялото."""
    _ensure(db)
    run, ramp, label = {}, {}, {}
    for a, u, s, e in db.execute(text("""SELECT i.article_id, i.uplift, p.start_day, p.end_day FROM promotion_items i
                                         JOIN promotions p ON p.id = i.promo_id
                                         WHERE p.end_day >= :d AND p.start_day <= :r"""),
                                 {"d": day, "r": day + timedelta(days=RAMP_DAYS)}).all():
        u = float(u)
        per = f"{s.strftime('%d.%m')}–{e.strftime('%d.%m')}"
        if s <= day:
            if u > run.get(a, 0):
                run[a] = u
                label[a] = f"📰 брошура {per}"
        else:
            k = (s - day).days                      # 1..RAMP_DAYS дни до старта
            f = 1 + (u - 1) * (RAMP_DAYS + 1 - k) / (RAMP_DAYS + 1)
            if a not in run and f > ramp.get(a, 0):
                ramp[a] = round(f, 2)
                label[a] = f"📰 брошура от {s.strftime('%d.%m')} — зареждане {RAMP_DAYS + 1 - k}/{RAMP_DAYS + 1}"
    for a in run:
        ramp.pop(a, None)
    return run, ramp, label


def active(db: Session, day: date) -> dict[int, float]:
    """article_id -> коефициент за деня (започнала промоция или зареждане преди старта)."""
    try:
        run, ramp, _ = _factors(db, day)
        return {**ramp, **run}
    except Exception:
        db.rollback()
        return {}


def labels(db: Session, day: date) -> dict[int, str]:
    try:
        return _factors(db, day)[2]
    except Exception:
        db.rollback()
        return {}


def promo_days(db: Session) -> dict[int, list[tuple[date, date]]]:
    """article_id -> периоди на промоция (за да не се учи от тях)."""
    try:
        _ensure(db)
        out: dict[int, list] = {}
        for a, s, e in db.execute(text("""SELECT i.article_id, p.start_day, p.end_day FROM promotion_items i
                                          JOIN promotions p ON p.id = i.promo_id""")).all():
            out.setdefault(a, []).append((s, e))
        return out
    except Exception:
        db.rollback()
        return {}


def apply(settings: list, factors: dict[int, float]) -> list:
    """Временно вдига мин/макс на промо артикулите (само за тази заявка)."""
    for s in settings:
        f = factors.get(s.article_id)
        if f and f > 1.0 and s.max_stock > 0:
            s.min_stock = math.ceil(s.min_stock * f)
            s.max_stock = max(math.ceil(s.max_stock * f), s.min_stock)
    return settings


def listing(db: Session) -> list[dict]:
    _ensure(db)
    db.commit()
    out = []
    for pid, name, s, e in db.execute(text("SELECT id, name, start_day, end_day FROM promotions ORDER BY start_day DESC")).all():
        items = db.execute(text("""SELECT a.sku, COALESCE(a.supplier_name, a.name), i.uplift, i.measured
                                   FROM promotion_items i JOIN articles a ON a.id = i.article_id WHERE i.promo_id=:p
                                   ORDER BY i.uplift DESC"""), {"p": pid}).all()
        today = datetime.now(SOFIA).date()
        out.append({"id": pid, "name": name, "start": s.strftime("%d.%m.%Y"), "end": e.strftime("%d.%m.%Y"),
                    "active": s <= today <= e,
                    "status": "активна" if s <= today <= e else ("предстои" if s > today else "минала"),
                    "items": [{"sku": k, "name": n, "uplift": float(u), "measured": bool(mm)} for k, n, u, mm in items]})
    return out


def apply_store(db: Session, settings: list, day: date, store_id: int) -> list:
    """
    Промо режим, съобразен с ежедневните доставки: мин/макс се смятат по СЪЩАТА формула,
    но от реалната скорост на продажби в ТОЗИ магазин по време на промото (не умножаване).
    Ако промото още няма данни (първи ден) - вдигане най-много ×1.5.
    """
    try:
        run, ramp, _ = _factors(db, day)
    except Exception:
        db.rollback(); run, ramp = {}, {}
    if not run and not ramp:
        return settings
    factors = {**ramp, **run}
    from . import learning
    try:
        rows = db.execute(text("""SELECT p.start_day FROM promotions p
                                  WHERE :d BETWEEN p.start_day AND p.end_day ORDER BY p.start_day LIMIT 1"""),
                          {"d": day}).all()
        start = rows[0][0] if rows else day
    except Exception:
        db.rollback(); start = day
    until = day - timedelta(days=1)
    days_ = max((until - start).days + 1, 0)
    sold = {}
    if days_ > 0:
        for aid, q in db.execute(select(m.SalesHistory.article_id, func.sum(m.SalesHistory.quantity_sold))
                                 .where(m.SalesHistory.store_id == store_id,
                                        m.SalesHistory.article_id.in_(list(run) or [0]),
                                        m.SalesHistory.sale_date >= start, m.SalesHistory.sale_date <= until)
                                 .group_by(m.SalesHistory.article_id)).all():
            sold[aid] = max(float(q or 0), 0.0)
    for s_ in settings:
        f = factors.get(s_.article_id)
        if not f or f <= 1.0 or s_.max_stock <= 0:
            continue
        if s_.article_id in ramp:                      # преди старта: постепенно вдигане
            s_.min_stock = math.ceil(s_.min_stock * f)
            s_.max_stock = max(math.ceil(s_.max_stock * f), s_.min_stock)
            continue
        if days_ > 0:
            sdp = sold.get(s_.article_id, 0.0) / days_
            _, mn, mx = learning.formula(sdp * learning.DAYS)
            if mx > s_.max_stock:                      # само нагоре, никога над „база × коефициент"
                s_.min_stock = min(mn, math.ceil(s_.min_stock * f))
                s_.max_stock = max(min(mx, math.ceil(s_.max_stock * f)), s_.min_stock)
        else:
            g = min(f, 1.5)
            s_.min_stock = math.ceil(s_.min_stock * g)
            s_.max_stock = max(math.ceil(s_.max_stock * g), s_.min_stock)
    return settings


def sync_from_mistral(db: Session) -> dict:
    """Брошурата от Мистрал (тип 1, поне 20 артикула с едни дати) -> промоция за нашите артикули.
    Ако брошурата е въведена в Мистрал преди старта, зареждането се вдига от 3 дни преди него."""
    _ensure(db); db.commit()
    today = datetime.now(SOFIA).date()
    out = []
    try:
        ranges = db.execute(text("""SELECT start_day, end_day FROM sa_promo WHERE tp = 1 AND end_day >= :t
                                    GROUP BY start_day, end_day HAVING COUNT(*) >= 20"""), {"t": today}).all()
    except Exception:
        db.rollback()
        return {"error": "няма sa_promo"}
    skus = {a.sku for a in db.execute(select(m.Article)).scalars().all()}
    for s, e in ranges:
        codes = [str(c) for (c,) in db.execute(text("SELECT code FROM sa_promo WHERE start_day=:s AND end_day=:e"),
                                               {"s": s, "e": e}).all()]
        mine = [c for c in codes if c in skus]
        if mine:
            # в Мистрал краят е денят след последния (00:00) - пазим датите както са
            r = create(db, f"Брошура {s.strftime('%d.%m')}–{e.strftime('%d.%m')} (Мистрал)", s, e, mine)
            out.append({"start": str(s), "end": str(e), "items": r["items"]})
    return {"brochures": out}


STOCKUP_DAYS = 30   # последен ден на брошурата: запас за толкова дни напред (докато е отстъпката)


def stockup_targets(db: Session, store_id: int, day: date) -> dict[int, tuple[float, float, str]]:
    """В последния ден на брошурата: article_id -> (цел бройки, продажби/ден в брошурата, етикет).
    Целта = продажбите на ден в ТОЗИ магазин по време на брошурата × STOCKUP_DAYS."""
    try:
        _ensure(db)
        rows = db.execute(text("""SELECT i.article_id, MIN(p.start_day), MAX(p.end_day) FROM promotion_items i
                                  JOIN promotions p ON p.id = i.promo_id WHERE p.end_day = :d
                                  GROUP BY i.article_id"""), {"d": day}).all()
    except Exception:
        db.rollback()
        return {}
    if not rows:
        return {}
    out = {}
    for aid, s, e in rows:
        until = day - timedelta(days=1)
        n = (until - s).days + 1
        if n <= 0:
            continue
        q = db.execute(select(func.coalesce(func.sum(m.SalesHistory.quantity_sold), 0)).where(
            m.SalesHistory.store_id == store_id, m.SalesHistory.article_id == aid,
            m.SalesHistory.sale_date >= s, m.SalesHistory.sale_date <= until)).scalar() or 0
        sdp = max(float(q), 0.0) / n
        if sdp <= 0:
            continue
        out[aid] = (math.ceil(sdp * STOCKUP_DAYS), round(sdp, 2),
                    f"📰 последен ден на брошурата {s.strftime('%d.%m')}–{e.strftime('%d.%m')} — запас за {STOCKUP_DAYS} дни "
                    f"({sdp:.1f} бр./ден)")
    return out
