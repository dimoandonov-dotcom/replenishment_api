"""
Свързва базата данни с ядрото (engine.py):
чете настройки/наличности -> смята заявки -> записва purchase_orders.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from . import models as m
from .engine import (
    ArticleSetting, ClosurePeriod, ScheduleEntry,
    generate_order_lines, group_lines_by_supplier, is_order_day,
)


def latest_stock_map(db: Session, store_id: int) -> dict[int, float]:
    """Последната известна наличност за всеки артикул в даден магазин."""
    subq = (
        select(
            m.StockSnapshot.article_id,
            m.StockSnapshot.quantity,
            func.row_number().over(
                partition_by=m.StockSnapshot.article_id,
                order_by=m.StockSnapshot.captured_at.desc(),
            ).label("rn"),
        )
        .where(m.StockSnapshot.store_id == store_id)
        .subquery()
    )
    rows = db.execute(
        select(subq.c.article_id, subq.c.quantity).where(subq.c.rn == 1)
    ).all()
    return {r.article_id: float(r.quantity) for r in rows}


def load_settings(db: Session, store_id: int, supplier_id: int | None = None) -> list[ArticleSetting]:
    """Настройките за автоматична заявка в даден магазин."""
    in_review = select(m.SettingsReviewQueue.article_id).where(
        m.SettingsReviewQueue.store_id == store_id,
        m.SettingsReviewQueue.resolved.is_(False),
    )

    stmt = (
        select(m.StoreArticleSetting, m.Article)
        .join(m.Article, m.Article.id == m.StoreArticleSetting.article_id)
        .where(
            m.StoreArticleSetting.store_id == store_id,
            m.Article.is_active.is_(True),
            m.StoreArticleSetting.article_id.notin_(in_review),
        )
    )

    out: list[ArticleSetting] = []
    for setting, article in db.execute(stmt).all():
        sup_id = setting.supplier_id or article.default_supplier_id
        if sup_id is None:
            continue
        if supplier_id is not None and sup_id != supplier_id:
            continue
        out.append(ArticleSetting(
            store_id=store_id,
            article_id=article.id,
            sku=article.sku,
            name=article.name,
            supplier_id=sup_id,
            min_stock=float(setting.min_stock),
            max_stock=float(setting.max_stock),
            pack_size=article.pack_size or 1,
            auto_adjust=setting.auto_adjust,
        ))
    return out


def load_schedules(db: Session, store_id: int) -> dict[tuple[int, int], list[ScheduleEntry]]:
    """Всички графици за магазина, групирани по (магазин, доставчик)."""
    rows = db.execute(
        select(m.StoreSupplierSchedule).where(m.StoreSupplierSchedule.store_id == store_id)
    ).scalars().all()
    out: dict[tuple[int, int], list[ScheduleEntry]] = {}
    for r in rows:
        out.setdefault((r.store_id, r.supplier_id), []).append(
            ScheduleEntry(
                store_id=r.store_id, supplier_id=r.supplier_id,
                order_weekday=r.weekday, delivery_weekday=r.delivery_weekday or r.weekday,
            )
        )
    return out


def schedule_for_date(
    schedules: dict[tuple[int, int], list[ScheduleEntry]],
    key: tuple[int, int],
    order_date: date,
) -> ScheduleEntry | None:
    """Графикът, който важи за конкретната дата."""
    entries = schedules.get(key) or []
    wd = order_date.isoweekday()
    for e in entries:
        if e.order_weekday == wd:
            return e
    return entries[0] if entries else None


def load_closures(db: Session) -> list[ClosurePeriod]:
    rows = db.execute(select(m.SupplierClosure)).scalars().all()
    return [
        ClosurePeriod(supplier_id=r.supplier_id, start_date=r.start_date,
                      end_date=r.end_date, reason=r.reason or "")
        for r in rows
    ]


def load_avg_daily_sales(db: Session, store_id: int, days: int = 30) -> dict[tuple[int, int], float]:
    """Средни дневни продажби за последните N дни."""
    stmt = (
        select(
            m.SalesHistory.article_id,
