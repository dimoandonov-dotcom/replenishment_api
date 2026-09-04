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
    ArticleSetting,
    ClosurePeriod,
    ScheduleEntry,
    generate_order_lines,
    group_lines_by_supplier,
    is_order_day,
)


def latest_stock_map(db: Session, store_id: int) -> dict[int, float]:
    """
    Последната известна наличност за всеки артикул в даден магазин.
    Ползва DISTINCT ON - най-новият snapshot по article_id.
    """
    subq = (
        select(
            m.StockSnapshot.article_id,
            m.StockSnapshot.quantity,
            func.row_number()
            .over(
                partition_by=m.StockSnapshot.article_id,
                order_by=m.StockSnapshot.captured_at.desc(),
            )
            .label("rn"),
        )
        .where(m.StockSnapshot.store_id == store_id)
        .subquery()
    )
    rows = db.execute(
        select(subq.c.article_id, subq.c.quantity).where(subq.c.rn == 1)
    ).all()
    return {r.article_id: float(r.quantity) for r in rows}


def load_settings(
    db: Session, store_id: int, supplier_id: int | None = None
) -> list[ArticleSetting]:
    """
    Настройките за автоматична заявка в даден магазин.
    Артикули в settings_review_queue НЕ се включват - те нямат валидни min/max.
    """
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
        out.append(
            ArticleSetting(
                store_id=store_id,
                article_id=article.id,
                sku=article.sku,
                name=article.name,
                supplier_id=sup_id,
                min_stock=float(setting.min_stock),
                max_stock=float(setting.max_stock),
                pack_size=article.pack_size or 1,
                auto_adjust=setting.auto_adjust,
            )
        )
    return out


def load_schedules(
    db: Session, store_id: int
) -> dict[tuple[int, int], list[ScheduleEntry]]:
    """
    Всички графици за магазина, групирани по (магазин, доставчик).
    Един доставчик може да има РЕДИЦА дни - напр. НДК доставя всеки делник,
    затова пазим списък, а не един запис.
    """
    rows = (
        db.execute(
            select(m.StoreSupplierSchedule).where(
                m.StoreSupplierSchedule.store_id == store_id
            )
        )
        .scalars()
        .all()
    )
    out: dict[tuple[int, int], list[ScheduleEntry]] = {}
    for r in rows:
        out.setdefault((r.store_id, r.supplier_id), []).append(
            ScheduleEntry(
                store_id=r.store_id,
                supplier_id=r.supplier_id,
                order_weekday=r.weekday,
                delivery_weekday=r.delivery_weekday or r.weekday,
            )
        )
    return out


def schedule_for_date(
    schedules: dict[tuple[int, int], list[ScheduleEntry]],
    key: tuple[int, int],
    order_date: date,
) -> ScheduleEntry | None:
    """Графикът, който важи за конкретната дата (по ден от седмицата)."""
    entries = schedules.get(key) or []
    wd = order_date.isoweekday()
    for e in entries:
        if e.order_weekday == wd:
            return e
    return entries[0] if entries else None


def load_closures(db: Session) -> list[ClosurePeriod]:
    rows = db.execute(select(m.SupplierClosure)).scalars().all()
    return [
        ClosurePeriod(
            supplier_id=r.supplier_id,
            start_date=r.start_date,
            end_date=r.end_date,
            reason=r.reason or "",
        )
        for r in rows
    ]


def load_avg_daily_sales(
    db: Session, store_id: int, days: int = 30
) -> dict[tuple[int, int], float]:
    """Средни дневни продажби за последните N дни - за предпразничните добавки."""
    stmt = (
        select(
            m.SalesHistory.article_id,
            func.sum(m.SalesHistory.quantity_sold).label("total"),
            func.count(func.distinct(m.SalesHistory.sale_date)).label(
                "day_count"
            ),
        )
        .where(m.SalesHistory.store_id == store_id)
        .group_by(m.SalesHistory.article_id)
    )
    out = {}
    for row in db.execute(stmt).all():
        dc = row.day_count or 1
        out[(store_id, row.article_id)] = float(row.total or 0) / dc
    return out
def calculate_for_store(
    db: Session,
    store_id: int,
    order_date: date,
    supplier_id: int | None = None,
    respect_schedule: bool = True,
):
    """
    Изчислява (без да записва) какво трябва да се поръча за един магазин.
    respect_schedule=True -> само доставчици, за които днес е ден за заявка.
    """
    settings = load_settings(db, store_id, supplier_id)
    schedules = load_schedules(db, store_id)

    if respect_schedule:
        allowed = {
            sup
            for (st, sup), entries in schedules.items()
            if any(
                e.order_weekday == order_date.isoweekday() for e in entries
            )
        }
        settings = [s for s in settings if s.supplier_id in allowed]

    stock = latest_stock_map(db, store_id)
    closures = load_closures(db)
    avg_sales = load_avg_daily_sales(db, store_id)

    supplier_rows = db.execute(select(m.Supplier)).scalars().all()
    modes = {
        s.id: (s.replenishment_mode or "below_min") for s in supplier_rows
    }
    weekend_flags = {
        s.id: bool(s.apply_weekend_buffer) for s in supplier_rows
    }

    by_mode: dict[str, list[ArticleSetting]] = {}
    for s in settings:
        by_mode.setdefault(
            modes.get(s.supplier_id, "below_min"), []
        ).append(s)

    combined = None
    for mode, subset in by_mode.items():
        for wknd in (True, False):
            group = [
                s
                for s in subset
                if weekend_flags.get(s.supplier_id, False) == wknd
            ]
            if not group:
                continue
            res = generate_order_lines(
                settings=group,
                stock_by_article=stock,
                order_date=order_date,
                schedules=schedules,
                closures=closures,
                avg_daily_sales=avg_sales,
                mode=mode,
                weekend_buffer=wknd,
            )
            if combined is None:
                combined = res
            else:
                combined.lines.extend(res.lines)
                combined.skipped_above_min += res.skipped_above_min
                combined.skipped_no_stock_data.extend(
                    res.skipped_no_stock_data
                )

    if combined is None:
        combined = generate_order_lines([], stock, order_date)
    return combined


def persist_orders(
    db: Session, result, store_id: int, dispatch_run_id: int | None = None
) -> list[m.PurchaseOrder]:
    """Записва изчислените редове като purchase_orders, групирани по доставчик."""
    created = []
    for supplier_id, lines in group_lines_by_supplier(
        result.lines
    ).items():
        po = m.PurchaseOrder(
            store_id=store_id,
            supplier_id=supplier_id,
            status="draft",
            dispatch_run_id=dispatch_run_id,
        )
        db.add(po)
        db.flush()
        for ln in lines:
            db.add(
                m.PurchaseOrderLine(
                    purchase_order_id=po.id,
                    article_id=ln.article_id,
                    current_stock=ln.current_stock,
                    min_stock=ln.min_stock,
                    max_stock=ln.max_stock,
                    effective_max=ln.effective_max,
                    suggested_quantity=ln.suggested_quantity,
                    ordered_quantity=ln.ordered_quantity,
                    pack_size=ln.pack_size,
                    notes=ln.notes or None,
                )
            )
        created.append(po)
    return created


def run_dispatch(
    db: Session,
    order_date: date,
    store_ids: list[int] | None = None,
    supplier_id: int | None = None,
    respect_schedule: bool = True,
) -> m.DispatchRun:
    """
    Пълен цикъл за всички (или избрани) магазини - това вика планировчикът
    всеки ден в определения час.
    """
    run = m.DispatchRun(status="running")
    db.add(run)
    db.flush()

    if store_ids is None:
        store_ids = [
            r
            for r in db.execute(
                select(m.Store.id).where(m.Store.is_active.is_(True))
            )
            .scalars()
            .all()
        ]

    orders_created = 0
    lines_created = 0
    for sid in store_ids:
        res = calculate_for_store(
            db, sid, order_date, supplier_id, respect_schedule
        )
        if not res.lines:
            continue
        pos = persist_orders(db, res, sid, dispatch_run_id=run.id)
        orders_created += len(pos)
        lines_created += len(res.lines)

    run.completed_at = datetime.now(timezone.utc)
    run.status = "success"
    run.stores_processed = len(store_ids)
    run.orders_created = orders_created
    run.order_lines_created = lines_created
    run.emails_sent = 0
    db.commit()
    return run

