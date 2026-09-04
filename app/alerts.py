"""
Сигнали за проблемни артикули.

1. slow_mover: артикул, който в последните 3 поредни dispatch run-а
   за неговия магазин+доставчик нито веднъж не е влязъл в заявка.

2. stockout: артикул с текуща наличност <= 0 -> изпускаме продажби.
   Еднократно (докато сигналът не се затвори) МАКС се вдига с 10%.
"""
from __future__ import annotations

import math

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from . import models as m
from .service import latest_stock_map, load_settings

STOCKOUT_MAX_BUMP = 0.10
SLOW_MOVER_RUNS = 3


def _open_alert_exists(db: Session, store_id: int, article_id: int, alert_type: str) -> bool:
    return db.execute(
        select(m.ArticleAlert.id).where(
            m.ArticleAlert.store_id == store_id,
            m.ArticleAlert.article_id == article_id,
            m.ArticleAlert.alert_type == alert_type,
            m.ArticleAlert.resolved.is_(False),
        )
    ).first() is not None


def _last_runs_for_store_supplier(db: Session, store_id: int, supplier_id: int, n: int) -> list[int]:
    rows = db.execute(
        select(m.PurchaseOrder.dispatch_run_id)
        .where(
            m.PurchaseOrder.store_id == store_id,
            m.PurchaseOrder.supplier_id == supplier_id,
            m.PurchaseOrder.dispatch_run_id.isnot(None),
        )
        .group_by(m.PurchaseOrder.dispatch_run_id)
        .order_by(m.PurchaseOrder.dispatch_run_id.desc())
        .limit(n)
    ).scalars().all()
    return list(rows)


def scan_slow_movers(db: Session, store_id: int) -> list[m.ArticleAlert]:
    created: list[m.ArticleAlert] = []
    settings = load_settings(db, store_id)

    by_supplier: dict[int, list] = {}
    for s in settings:
        by_supplier.setdefault(s.supplier_id, []).append(s)

    for supplier_id, sup_settings in by_supplier.items():
        runs = _last_runs_for_store_supplier(db, store_id, supplier_id, SLOW_MOVER_RUNS)
        if len(runs) < SLOW_MOVER_RUNS:
            continue

        ordered_article_ids = set(
            db.execute(
                select(m.PurchaseOrderLine.article_id)
                .join(m.PurchaseOrder, m.PurchaseOrder.id == m.PurchaseOrderLine.purchase_order_id)
                .where(
                    m.PurchaseOrder.store_id == store_id,
                    m.PurchaseOrder.supplier_id == supplier_id,
                    m.PurchaseOrder.dispatch_run_id.in_(runs),
                )
            ).scalars().all()
        )

        for s in sup_settings:
            if s.article_id in ordered_article_ids:
                continue
            if _open_alert_exists(db, store_id, s.article_id, "slow_mover"):
                continue
            alert = m.ArticleAlert(
                store_id=store_id, article_id=s.article_id, supplier_id=supplier_id,
                alert_type="slow_mover",
                details=f"{SLOW_MOVER_RUNS} поредни заявки без поръчка "
                        f"(мин {s.min_stock:g} / макс {s.max_stock:g})",
            )
            db.add(alert)
            created.append(alert)
    return created


def scan_stockouts(db: Session, store_id: int, bump_max: bool = True) -> list[m.ArticleAlert]:
    created: list[m.ArticleAlert] = []
    stock = latest_stock_map(db, store_id)
    settings = {s.article_id: s for s in load_settings(db, store_id)}

    for article_id, qty in stock.items():
        if qty > 0:
            continue
        s = settings.get(article_id)
        if s is None:
            continue
        if _open_alert_exists(db, store_id, article_id, "stockout"):
            continue

        old_max = new_max = None
        adjusted = False
        if bump_max:
            row = db.get(m.StoreArticleSetting, (store_id, article_id))
            if row is not None:
                old_max = float(row.max_stock)
                new_max = math.ceil(old_max * (1 + STOCKOUT_MAX_BUMP))
                row.max_stock = new_max
                adjusted = True

        alert = m.ArticleAlert(
            store_id=store_id, article_id=article_id, supplier_id=s.supplier_id,
            alert_type="stockout",
            details=f"Наличност {qty:g} - изпускаме продажби",
            max_adjusted=adjusted, old_max=old_max, new_max=new_max,
        )
        db.add(alert)
        created.append(alert)
    return created


def scan_all(db: Session, store_ids: list[int] | None = None, bump_max: bool = True) -> dict:
    if store_ids is None:
        store_ids = db.execute(
            select(m.Store.id).where(m.Store.is_active.is_(True))
        ).scalars().all()

    slow, outs = 0, 0
    for sid in store_ids:
        slow += len(scan_slow_movers(db, sid))
        outs += len(scan_stockouts(db, sid, bump_max))
    db.commit()
    return {"stores_scanned": len(store_ids),
            "slow_mover_alerts": slow, "stockout_alerts": outs}


def list_alerts_grouped(
    db: Session,
    alert_type: str | None = None,
    store_id: int | None = None,
    supplier_id: int | None = None,
    include_resolved: bool = False,
) -> list[dict]:
    stmt = (
        select(m.ArticleAlert, m.Article, m.Store, m.Supplier)
        .join(m.Article, m.Article.id == m.ArticleAlert.article_id)
        .join(m.Store, m.Store.id == m.ArticleAlert.store_id)
        .outerjoin(m.Supplier, m.Supplier.id == m.ArticleAlert.supplier_id)
        .order_by(m.Store.id, m.Supplier.name, m.Article.name)
    )
    if not include_resolved:
        stmt = stmt.where(m.ArticleAlert.resolved.is_(False))
    if alert_type:
        stmt = stmt.where(m.ArticleAlert.alert_type == alert_type)
    if store_id:
        stmt = stmt.where(m.ArticleAlert.store_id == store_id)
    if supplier_id:
        stmt = stmt.where(m.ArticleAlert.supplier_id == supplier_id)

    grouped: dict[tuple, dict] = {}
    for alert, article, store, supplier in db.execute(stmt).all():
        key = (store.id, supplier.id if supplier else None)
        g = grouped.setdefault(key, {
            "store_id": store.id, "store": store.name,
            "supplier_id": supplier.id if supplier else None,
            "supplier": supplier.name if supplier else None,
            "items": [],
        })
        g["items"].append({
            "alert_id": alert.id,
            "type": alert.alert_type,
            "sku": article.sku, "name": article.name,
            "details": alert.details,
            "max_adjusted": alert.max_adjusted,
            "old_max": float(alert.old_max) if alert.old_max is not None else None,
            "new_max": float(alert.new_max) if alert.new_max is not None else None,
            "created_at": alert.created_at,
        })
    return list(grouped.values())
