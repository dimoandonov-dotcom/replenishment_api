"""
REST API за системата за автоматични заявки.

Стартиране:
    uvicorn app.main:app --host 0.0.0.0 --port 8000

Документация: http://<сървър>:8000/docs
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from . import models as m, service
from .db import get_db

app = FastAPI(
    title="Автоматични заявки към доставчици",
    description="Наличности от Mistral (read-only) + min/max логика в тази система.",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# Защита с API ключ.
# ---------------------------------------------------------------------------
import os as _os
from fastapi import Request
from fastapi.responses import JSONResponse

_API_KEY = _os.getenv("API_KEY", "").strip()
_OPEN_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


@app.middleware("http")
async def _require_api_key(request: Request, call_next):
    if request.url.path in _OPEN_PATHS:
        return await call_next(request)
    if _API_KEY:
        if request.headers.get("X-API-Key", "") != _API_KEY:
            return JSONResponse(status_code=401, content={"detail": "Невалиден или липсващ X-API-Key"})
    else:
        client = request.client.host if request.client else ""
        if client not in ("127.0.0.1", "::1", "localhost", "testclient"):
            return JSONResponse(status_code=403, content={
                "detail": "API_KEY не е конфигуриран - достъп само от localhost"})
    return await call_next(request)


class OrderLineOut(BaseModel):
    sku: str
    name: str
    supplier_id: int
    current_stock: float
    min_stock: float
    max_stock: float
    effective_max: float
    suggested_quantity: float
    ordered_quantity: int
    pack_size: int
    notes: str = ""


class PreviewOut(BaseModel):
    store_id: int
    order_date: date
    total_lines: int
    total_units: int
    skipped_above_min: int
    skipped_no_stock_data: list[str]
    lines: list[OrderLineOut]


class SettingIn(BaseModel):
    store_id: int
    sku: str
    min_stock: float = Field(ge=0)
    max_stock: float = Field(ge=0)
    supplier_id: int | None = None
    auto_adjust: bool = True


class StockIn(BaseModel):
    store_id: int
    sku: str
    quantity: float
    captured_at: datetime | None = None


class DispatchIn(BaseModel):
    order_date: date | None = None
    store_ids: list[int] | None = None
    supplier_id: int | None = None
    respect_schedule: bool = True


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok", "time": datetime.now(timezone.utc)}


@app.get("/stores")
def list_stores(db: Session = Depends(get_db)):
    rows = db.execute(select(m.Store).order_by(m.Store.id)).scalars().all()
    return [{"id": s.id, "name": s.name, "is_active": s.is_active} for s in rows]


@app.get("/suppliers")
def list_suppliers(db: Session = Depends(get_db)):
    rows = db.execute(select(m.Supplier).order_by(m.Supplier.name)).scalars().all()
    return [{"id": s.id, "name": s.name, "email": s.contact_email,
             "replenishment_mode": s.replenishment_mode,
             "is_active": s.is_active} for s in rows]


@app.get("/articles")
def list_articles(
    supplier_id: int | None = None,
    search: str | None = None,
    limit: int = Query(100, le=1000),
    db: Session = Depends(get_db),
):
    stmt = select(m.Article).where(m.Article.is_active.is_(True))
    if supplier_id:
        stmt = stmt.where(m.Article.default_supplier_id == supplier_id)
    if search:
        stmt = stmt.where(m.Article.name.ilike(f"%{search}%"))
    rows = db.execute(stmt.limit(limit)).scalars().all()
    return [{"id": a.id, "sku": a.sku, "name": a.name,
             "pack_size": a.pack_size, "pack_type": a.pack_type,
             "supplier_id": a.default_supplier_id} for a in rows]


@app.post("/stock/ingest")
def ingest_stock(items: list[StockIn], db: Session = Depends(get_db)):
    sku_map = {
        a.sku: a.id
        for a in db.execute(select(m.Article)).scalars().all()
    }
    inserted, unknown = 0, []
    for it in items:
        aid = sku_map.get(it.sku)
        if aid is None:
            unknown.append(it.sku)
            continue
        db.add(m.StockSnapshot(
            store_id=it.store_id, article_id=aid, quantity=it.quantity,
            captured_at=it.captured_at or datetime.now(timezone.utc),
        ))
        inserted += 1
    db.commit()
    return {"inserted": inserted, "unknown_skus": unknown[:50],
            "unknown_count": len(unknown)}


@app.get("/stock/{store_id}")
def current_stock(store_id: int, db: Session = Depends(get_db)):
    stock = service.latest_stock_map(db, store_id)
    if not stock:
        return {"store_id": store_id, "items": [], "note": "няма данни за наличност"}
    arts = {
        a.id: a for a in db.execute(
            select(m.Article).where(m.Article.id.in_(stock.keys()))
        ).scalars().all()
    }
    return {
        "store_id": store_id,
        "items": [
            {"sku": arts[aid].sku, "name": arts[aid].name, "quantity": q}
            for aid, q in stock.items() if aid in arts
        ],
    }


@app.get("/settings/{store_id}")
def get_settings(store_id: int, db: Session = Depends(get_db)):
    stmt = (
        select(m.StoreArticleSetting, m.Article)
        .join(m.Article, m.Article.id == m.StoreArticleSetting.article_id)
        .where(m.StoreArticleSetting.store_id == store_id)
    )
    return [
        {"sku": a.sku, "name": a.name, "min_stock": float(s.min_stock),
         "max_stock": float(s.max_stock), "pack_size": a.pack_size,
         "supplier_id": s.supplier_id or a.default_supplier_id,
         "auto_adjust": s.auto_adjust}
        for s, a in db.execute(stmt).all()
    ]


@app.put("/settings")
def upsert_settings(items: list[SettingIn], db: Session = Depends(get_db)):
    sku_map = {a.sku: a.id for a in db.execute(select(m.Article)).scalars().all()}
    updated, unknown, invalid = 0, [], []

    for it in items:
        aid = sku_map.get(it.sku)
        if aid is None:
            unknown.append(it.sku)
            continue
        if it.max_stock < it.min_stock:
            invalid.append({"sku": it.sku, "reason": "max_stock < min_stock"})
            continue

        existing = db.get(m.StoreArticleSetting, (it.store_id, aid))
        if existing:
            existing.min_stock = it.min_stock
            existing.max_stock = it.max_stock
            existing.auto_adjust = it.auto_adjust
            if it.supplier_id:
                existing.supplier_id = it.supplier_id
        else:
            db.add(m.StoreArticleSetting(
                store_id=it.store_id, article_id=aid,
                min_stock=it.min_stock, max_stock=it.max_stock,
                supplier_id=it.supplier_id, auto_adjust=it.auto_adjust,
            ))
        q = db.execute(
            select(m.SettingsReviewQueue).where(
                m.SettingsReviewQueue.store_id == it.store_id,
                m.SettingsReviewQueue.article_id == aid,
            )
        ).scalar_one_or_none()
        if q:
            q.resolved = True
        updated += 1

    db.commit()
    return {"updated": updated, "unknown_skus": unknown, "invalid": invalid}


@app.get("/review-queue")
def review_queue(store_id: int | None = None, db: Session = Depends(get_db)):
    stmt = (
        select(m.SettingsReviewQueue, m.Article, m.Store)
        .join(m.Article, m.Article.id == m.SettingsReviewQueue.article_id)
        .join(m.Store, m.Store.id == m.SettingsReviewQueue.store_id)
        .where(m.SettingsReviewQueue.resolved.is_(False))
    )
    if store_id:
        stmt = stmt.where(m.SettingsReviewQueue.store_id == store_id)
    return [
        {"store_id": st.id, "store": st.name, "sku": a.sku,
         "name": a.name, "reason": q.reason}
        for q, a, st in db.execute(stmt).all()
    ]


@app.get("/orders/preview/{store_id}", response_model=PreviewOut)
def preview_order(
    store_id: int,
    order_date: date | None = None,
    supplier_id: int | None = None,
    respect_schedule: bool = False,
    db: Session = Depends(get_db),
):
    if not db.get(m.Store, store_id):
        raise HTTPException(404, f"Няма магазин с id {store_id}")

    d = order_date or date.today()
    res = service.calculate_for_store(db, store_id, d, supplier_id, respect_schedule)
    return PreviewOut(
        store_id=store_id, order_date=d,
        total_lines=res.total_lines, total_units=res.total_units,
        skipped_above_min=res.skipped_above_min,
        skipped_no_stock_data=res.skipped_no_stock_data[:100],
        lines=[OrderLineOut(**{
            "sku": l.sku, "name": l.name, "supplier_id": l.supplier_id,
            "current_stock": l.current_stock, "min_stock": l.min_stock,
            "max_stock": l.max_stock, "effective_max": l.effective_max,
            "suggested_quantity": l.suggested_quantity,
            "ordered_quantity": l.ordered_quantity,
            "pack_size": l.pack_size, "notes": l.notes,
        }) for l in res.lines],
    )


@app.post("/orders/dispatch")
def dispatch(payload: DispatchIn, db: Session = Depends(get_db)):
    d = payload.order_date or date.today()
    run = service.run_dispatch(
        db, d, payload.store_ids, payload.supplier_id, payload.respect_schedule
    )
    return {
        "dispatch_run_id": run.id, "status": run.status,
        "
