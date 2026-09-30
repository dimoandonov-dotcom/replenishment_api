"""
REST API за системата за автоматични заявки.

Стартиране:
    uvicorn app.main:app --host 0.0.0.0 --port 8000

Документация: http://<сървър>:8000/docs
"""
from __future__ import annotations

from datetime import date, datetime, timezone, timedelta

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

import os as _os
from fastapi import Request
from fastapi.responses import JSONResponse

_API_KEY = _os.getenv("API_KEY", "").strip()
_OPEN_PATHS = {"/health", "/docs", "/openapi.json", "/redoc", "/dashboard"}


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
        "stores_processed": run.stores_processed,
        "orders_created": run.orders_created,
        "order_lines_created": run.order_lines_created,
        "started_at": run.started_at, "completed_at": run.completed_at,
    }


@app.get("/orders")
def list_orders(
    store_id: int | None = None,
    supplier_id: int | None = None,
    status: str | None = None,
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
):
    stmt = select(m.PurchaseOrder).order_by(m.PurchaseOrder.created_at.desc())
    if store_id:
        stmt = stmt.where(m.PurchaseOrder.store_id == store_id)
    if supplier_id:
        stmt = stmt.where(m.PurchaseOrder.supplier_id == supplier_id)
    if status:
        stmt = stmt.where(m.PurchaseOrder.status == status)
    rows = db.execute(stmt.limit(limit)).scalars().all()
    return [{"id": o.id, "store_id": o.store_id, "supplier_id": o.supplier_id,
             "status": o.status, "created_at": o.created_at,
             "line_count": len(o.lines)} for o in rows]


@app.get("/orders/{order_id}")
def get_order(order_id: int, db: Session = Depends(get_db)):
    o = db.get(m.PurchaseOrder, order_id)
    if not o:
        raise HTTPException(404, "Няма такава заявка")
    arts = {
        a.id: a for a in db.execute(
            select(m.Article).where(
                m.Article.id.in_([l.article_id for l in o.lines])
            )
        ).scalars().all()
    }
    return {
        "id": o.id, "store_id": o.store_id, "supplier_id": o.supplier_id,
        "status": o.status, "created_at": o.created_at, "sent_at": o.sent_at,
        "lines": [{
            "sku": arts[l.article_id].sku if l.article_id in arts else None,
            "name": arts[l.article_id].name if l.article_id in arts else None,
            "current_stock": float(l.current_stock),
            "min_stock": float(l.min_stock), "max_stock": float(l.max_stock),
            "suggested_quantity": float(l.suggested_quantity),
            "ordered_quantity": l.ordered_quantity,
            "pack_size": l.pack_size, "notes": l.notes,
        } for l in o.lines],
        "total_units": sum(l.ordered_quantity for l in o.lines),
    }


@app.post("/orders/{order_id}/status")
def set_status(order_id: int, status: str, db: Session = Depends(get_db)):
    allowed = {"draft", "sent", "confirmed", "delivered", "cancelled"}
    if status not in allowed:
        raise HTTPException(400, f"Позволени статуси: {sorted(allowed)}")
    o = db.get(m.PurchaseOrder, order_id)
    if not o:
        raise HTTPException(404, "Няма такава заявка")
    o.status = status
    if status == "sent" and not o.sent_at:
        o.sent_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": o.id, "status": o.status, "sent_at": o.sent_at}


@app.get("/dispatch-runs")
def dispatch_runs(limit: int = Query(20, le=100), db: Session = Depends(get_db)):
    rows = db.execute(
        select(m.DispatchRun).order_by(m.DispatchRun.started_at.desc()).limit(limit)
    ).scalars().all()
    return [{"id": r.id, "status": r.status, "started_at": r.started_at,
             "completed_at": r.completed_at, "stores_processed": r.stores_processed,
             "orders_created": r.orders_created,
             "order_lines_created": r.order_lines_created} for r in rows]


@app.get("/reports/below-minimum")
def below_minimum(store_id: int | None = None, db: Session = Depends(get_db)):
    store_ids = [store_id] if store_id else db.execute(
        select(m.Store.id).where(m.Store.is_active.is_(True))
    ).scalars().all()

    out = []
    for sid in store_ids:
        res = service.calculate_for_store(db, sid, date.today(), respect_schedule=False)
        for l in res.lines:
            out.append({
                "store_id": sid, "sku": l.sku, "name": l.name,
                "current_stock": l.current_stock, "min_stock": l.min_stock,
                "max_stock": l.max_stock, "suggested_quantity": l.suggested_quantity,
            })
    return {"count": len(out), "items": out}


from . import alerts as alerts_svc  # noqa: E402


class AlertScanIn(BaseModel):
    store_ids: list[int] | None = None
    bump_max: bool = True


@app.post("/alerts/scan")
def alerts_scan(payload: AlertScanIn, db: Session = Depends(get_db)):
    return alerts_svc.scan_all(db, payload.store_ids, payload.bump_max)


@app.get("/alerts")
def alerts_list(
    alert_type: str | None = Query(None, pattern="^(slow_mover|stockout)$"),
    store_id: int | None = None,
    supplier_id: int | None = None,
    include_resolved: bool = False,
    db: Session = Depends(get_db),
):
    groups = alerts_svc.list_alerts_grouped(
        db, alert_type, store_id, supplier_id, include_resolved
    )
    return {"group_count": len(groups),
            "total_items": sum(len(g["items"]) for g in groups),
            "groups": groups}


@app.post("/alerts/{alert_id}/resolve")
def alerts_resolve(alert_id: int, db: Session = Depends(get_db)):
    a = db.get(m.ArticleAlert, alert_id)
    if not a:
        raise HTTPException(404, "Няма такъв сигнал")
    a.resolved = True
    db.commit()
    return {"id": a.id, "resolved": True}


from fastapi import File, UploadFile  # noqa: E402
from . import imports as imports_svc  # noqa: E402


@app.post("/import/stock-report")
async def import_stock_report(
    file: UploadFile = File(...),
    captured_at: datetime | None = None,
    db: Session = Depends(get_db),
):
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Очаква се .xlsx файл")
    content = await file.read()
    return imports_svc.import_stock_report(db, content, captured_at)


from fastapi.responses import Response  # noqa: E402
from urllib.parse import quote  # noqa: E402
from . import exports as exports_svc  # noqa: E402


def _attachment(filename: str, content: bytes, media_type: str) -> Response:
    return Response(
        content=content, media_type=media_type,
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/orders/{order_id}/export")
def export_order_file(order_id: int, db: Session = Depends(get_db)):
    try:
        filename, content = exports_svc.export_order(db, order_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return _attachment(
        filename, content,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/orders/export/zip")
def export_orders_zip_file(
    dispatch_run_id: int | None = None,
    supplier_id: int | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    content = exports_svc.export_orders_zip(db, dispatch_run_id, supplier_id, status)
    name = f"zayavki_run_{dispatch_run_id or 'all'}.zip"
    return _attachment(name, content, "application/zip")


from .admin import router as admin_router  # noqa: E402

app.include_router(admin_router)


# ---------------------------------------------------------------------------
# Живо табло - винаги текущи данни, директно от базата, без нужда да питаш
# Отваря се просто с линк + ?key=<API_KEY>, никакви заглавия (headers) не
# трябват - затова е извън обичайната X-API-Key защита на middleware-а.
# ---------------------------------------------------------------------------

import html as _html
from fastapi.responses import HTMLResponse


def _dash_esc(s) -> str:
    return _html.escape(str(s)) if s is not None else ""


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(key: str = "", db: Session = Depends(get_db)):
    if _API_KEY and key != _API_KEY:
        return HTMLResponse(
            "<h3>Невалиден или липсващ ключ. "
            "Отвори с ?key=&lt;твоя API ключ&gt; в адреса.</h3>",
            status_code=401,
        )

    active_stores = db.execute(
        select(func.count()).select_from(m.Store).where(m.Store.is_active.is_(True))
    ).scalar() or 0
    total_stores = db.execute(select(func.count()).select_from(m.Store)).scalar() or 0
    total_articles = db.execute(
        select(func.count()).select_from(m.Article).where(m.Article.is_active.is_(True))
    ).scalar() or 0
    total_settings = db.execute(
        select(func.count()).select_from(m.StoreArticleSetting)
    ).scalar() or 0
    review_count = db.execute(
        select(func.count()).select_from(m.SettingsReviewQueue)
        .where(m.SettingsReviewQueue.resolved.is_(False))
    ).scalar() or 0
    open_alerts = db.execute(
        select(func.count()).select_from(m.ArticleAlert)
        .where(m.ArticleAlert.resolved.is_(False))
    ).scalar() or 0

    suppliers = db.execute(select(m.Supplier)).scalars().all()

    last_run = db.execute(
        select(m.DispatchRun).order_by(m.DispatchRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()

    top_rows = []
    if last_run:
        stmt = (
            select(
                m.PurchaseOrder.store_id,
                func.count(m.PurchaseOrderLine.id).label("cnt"),
            )
            .join(
                m.PurchaseOrderLine,
                m.PurchaseOrderLine.purchase_order_id == m.PurchaseOrder.id,
            )
            .where(m.PurchaseOrder.dispatch_run_id == last_run.id)
            .group_by(m.PurchaseOrder.store_id)
            .order_by(func.count(m.PurchaseOrderLine.id).desc())
            .limit(10)
        )
        top_rows = db.execute(stmt).all()

    store_names = {
        s.id: s.name
        for s in db.execute(
            select(m.Store).where(
                m.Store.id.in_([r.store_id for r in top_rows])
            )
        ).scalars().all()
    }

    recent_runs = db.execute(
        select(m.DispatchRun).order_by(m.DispatchRun.started_at.desc()).limit(8)
    ).scalars().all()

    SOFIA = timezone(timedelta(hours=3))

    def fmt_time(dt):
        if dt is None:
            return "—"
        return dt.astimezone(SOFIA).strftime("%d.%m.%Y %H:%M")

    max_cnt = max([r.cnt for r in top_rows], default=1) or 1
    rack_rows = ""
    for r in top_rows:
        pct = round(100 * r.cnt / max_cnt, 1)
        name = _dash_esc(store_names.get(r.store_id, f"обект {r.store_id}"))
        rack_rows += f"""
        <div class="rack-row">
          <div class="rack-name">{name}</div>
          <div class="rack-track"><div class="rack-bar" style="width:{pct}%"></div></div>
          <div class="rack-val">{r.cnt}</div>
        </div>"""

    run_rows = ""
    for r in recent_runs:
        dot = "" if r.orders_created else " empty"
        run_rows += f"""
        <tr>
          <td><span class="status-dot{dot}"></span>#{r.id} {_dash_esc(r.status)}</td>
          <td>{fmt_time(r.started_at)}</td>
          <td>{r.orders_created or 0}</td>
          <td>{r.order_lines_created or 0}</td>
        </tr>"""

    supplier_lines = ""
    for s in suppliers:
        mode_label = "ежедневно допълване" if s.replenishment_mode == "daily_topup" else "под минимума"
        supplier_lines += f"<div>{_dash_esc(s.name)} — режим: <b>{mode_label}</b></div>"

    generated = fmt_time(datetime.now(timezone.utc))

    page = f"""<!DOCTYPE html>
<html lang="bg"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Диспечерски пулт — живо</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {{
  --ink:#1C2430; --paper:#F3F0E8; --paper-raised:#FBF9F4;
  --line:#D8D2C4; --line-strong:#B8AF9A; --amber:#C96A1F;
  --teal:#2F6358; --teal-soft:#D8E6E1; --slate:#5B6B7A;
  --font-head:"Space Grotesk",system-ui,sans-serif;
  --font-mono:"IBM Plex Mono",ui-monospace,monospace;
}}
@media (prefers-color-scheme: dark) {{
  :root {{ --ink:#EDE9DF; --paper:#14191F; --paper-raised:#1B222A;
    --line:#2C343E; --line-strong:#3C4753; --amber:#E2872E;
    --teal:#6FB3A2; --teal-soft:#1E2E2A; --slate:#8C99A6; }}
}}
* {{ box-sizing:border-box; }}
html,body {{ margin:0; background:var(--paper); color:var(--ink); font-family:var(--font-head); }}
body {{ max-width:980px; margin:0 auto; padding:28px 20px 60px; }}
header {{ display:flex; justify-content:space-between; align-items:flex-end; gap:16px;
  border-bottom:2px solid var(--ink); padding-bottom:18px; margin-bottom:22px; flex-wrap:wrap; }}
.brand-mark {{ font-family:var(--font-mono); font-size:12px; letter-spacing:.12em; color:var(--slate); margin-bottom:6px; }}
h1 {{ font-size:clamp(24px,5vw,32px); margin:0; font-weight:700; }}
.snapshot-time {{ font-family:var(--font-mono); font-size:12.5px; color:var(--slate); text-align:right; line-height:1.6; }}
.snapshot-time b {{ color:var(--ink); }}
.board {{ display:grid; grid-template-columns:repeat(3,1fr); gap:1px; background:var(--line-strong);
  border:1px solid var(--line-strong); margin-bottom:30px; }}
@media (max-width:620px) {{ .board {{ grid-template-columns:repeat(2,1fr); }} }}
.tile {{ background:var(--paper-raised); padding:18px 16px; }}
.tile-label {{ font-family:var(--font-mono); font-size:11px; letter-spacing:.06em; color:var(--slate); margin-bottom:10px; }}
.tile-value {{ font-family:var(--font-mono); font-size:30px; font-weight:600; font-variant-numeric:tabular-nums; line-height:1; }}
.tile-value.amber {{ color:var(--amber); }}
.tile-value.teal {{ color:var(--teal); }}
.tile-sub {{ margin-top:8px; font-size:12px; color:var(--slate); }}
section {{ margin-bottom:34px; }}
.section-head {{ display:flex; align-items:baseline; justify-content:space-between;
  border-bottom:1px solid var(--line-strong); padding-bottom:8px; margin-bottom:16px; }}
h2 {{ font-size:16px; margin:0; font-weight:600; }}
.section-note {{ font-family:var(--font-mono); font-size:11.5px; color:var(--slate); }}
.rack {{ display:flex; flex-direction:column; gap:10px; }}
.rack-row {{ display:grid; grid-template-columns:168px 1fr 44px; align-items:center; gap:10px; }}
.rack-name {{ font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.rack-track {{ height:16px; background:repeating-linear-gradient(90deg,var(--line) 0,var(--line) 1px,transparent 1px,transparent 9px);
  border-bottom:1px solid var(--line-strong); }}
.rack-bar {{ height:100%; background:var(--teal); opacity:.85; }}
.rack-val {{ font-family:var(--font-mono); font-size:12.5px; text-align:right; }}
table {{ width:100%; border-collapse:collapse; font-family:var(--font-mono); font-size:12.5px; }}
th {{ text-align:left; font-weight:500; color:var(--slate); font-size:11px; padding:6px 10px; border-bottom:1px solid var(--line-strong); }}
td {{ padding:9px 10px; border-bottom:1px solid var(--line); }}
th:last-child,td:last-child,th:nth-child(3),td:nth-child(3),th:nth-child(4),td:nth-child(4) {{ text-align:right; }}
tr:last-child td {{ border-bottom:none; }}
.status-dot {{ display:inline-block; width:7px; height:7px; border-radius:50%; background:var(--teal); margin-right:6px; }}
.status-dot.empty {{ background:var(--line-strong); }}
.strip {{ background:var(--paper-raised); border:1px solid var(--line-strong); border-left:4px solid var(--amber);
  padding:14px 16px; font-size:13px; line-height:1.6; }}
footer {{ margin-top:40px; padding-top:16px; border-top:1px solid var(--line-strong);
  font-family:var(--font-mono); font-size:11.5px; color:var(--slate); }}
</style></head><body>

<header>
  <div>
    <div class="brand-mark">СИСТЕМА ЗА АВТОМАТИЧНИ ЗАЯВКИ / ЖИВО ТАБЛО</div>
    <h1>Диспечерски пулт</h1>
  </div>
  <div class="snapshot-time">
    Заредено: <b>{generated}</b> бг. час<br>
    {supplier_lines}
  </div>
</header>

<div class="board">
  <div class="tile"><div class="tile-label">АКТИВНИ ОБЕКТИ</div>
    <div class="tile-value teal">{active_stores}</div>
    <div class="tile-sub">от общо {total_stores} заведени</div></div>
  <div class="tile"><div class="tile-label">АКТИВНИ АРТИКУЛИ</div>
    <div class="tile-value">{total_articles}</div>
    <div class="tile-sub">в номенклатурата</div></div>
  <div class="tile"><div class="tile-label">НАСТРОЙКИ МИН / МАКС</div>
    <div class="tile-value">{total_settings:,}</div>
    <div class="tile-sub">комбинации обект × артикул</div></div>
  <div class="tile"><div class="tile-label">ПОСЛЕДНО ПУСКАНЕ — ЗАЯВКИ</div>
    <div class="tile-value amber">{last_run.orders_created if last_run else 0}</div>
    <div class="tile-sub">{fmt_time(last_run.started_at) if last_run else "няма пускания"}</div></div>
  <div class="tile"><div class="tile-label">ПОСЛЕДНО ПУСКАНЕ — РЕДОВЕ</div>
    <div class="tile-value amber">{last_run.order_lines_created if last_run else 0}</div>
    <div class="tile-sub">поръчани позиции</div></div>
  <div class="tile"><div class="tile-label">СИГНАЛИ / ЗА ПРЕГЛЕД</div>
    <div class="tile-value">{open_alerts} / {review_count}</div>
    <div class="tile-sub">отворени сигнали / чакат мин-макс</div></div>
</div>

<section>
  <div class="section-head"><h2>Обекти по обем в последното пускане</h2>
  <div class="section-note">топ {len(top_rows)}</div></div>
  <div class="rack">{rack_rows or "<p>Няма данни за пускане още.</p>"}</div>
</section>

<section>
  <div class="section-head"><h2>История на пусканията</h2>
  <div class="section-note">последни {len(recent_runs)}</div></div>
  <table><thead><tr><th>Статус</th><th>Час (бг.)</th><th>Заявки</th><th>Редове</th></tr></thead>
  <tbody>{run_rows or "<tr><td colspan=4>Няма пускания.</td></tr>"}</tbody></table>
</section>

<div class="strip">
  Това табло се смята на живо от базата при всяко отваряне — просто
  презареди страницата за актуални данни. Пълна интерактивна документация:
  <a href="/docs">/docs</a>
</div>

<footer>replenishment_api · живо табло</footer>
</body></html>"""
    return HTMLResponse(page)
