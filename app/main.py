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

import hmac as _hmac
import hashlib as _hashlib
import os as _os
from fastapi import Request
from fastapi.responses import JSONResponse

# Достъп:
#  - хора: потребител + парола (APP_USER / APP_PASSWORD) -> бисквитка за сесия
#  - машини (cron, скриптове): header X-API-Key (API_KEY)
_API_KEY = _os.getenv("API_KEY", "").strip()
APP_USER = _os.getenv("APP_USER", "").strip()
APP_PASSWORD = _os.getenv("APP_PASSWORD", "").strip()
SESSION_COOKIE = "minmax_session"
_OPEN_PATHS = {
    "/", "/health", "/docs", "/openapi.json", "/redoc",
    "/dashboard", "/orders-view", "/ui/login", "/ui/logout", "/brand/logo",
}


def hash_password(pw: str, salt: str | None = None) -> str:
    salt = salt or _os.urandom(8).hex()
    h = _hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${h}"


def check_password(pw: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return _hmac.compare_digest(hash_password(pw, salt), stored)


def _secret_for(username: str) -> str | None:
    """Таен ключ на сесията - сменя се автоматично при смяна на паролата."""
    if APP_USER and username.lower() == APP_USER.lower():
        return APP_PASSWORD
    from .db import SessionLocal
    with SessionLocal() as db:
        u = db.get(m.AppUser, username.lower())
        return u.password_hash if (u and u.is_active) else None


def session_token(username: str | None = None) -> str:
    username = (username or APP_USER).lower()
    sec = _secret_for(username) or ""
    sig = _hmac.new((_API_KEY + "|" + sec).encode(), ("session:" + username).encode(),
                    _hashlib.sha256).hexdigest()
    return f"{username}.{sig}"


def session_user(request: Request) -> str | None:
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if not cookie:
        return None
    if "." not in cookie:  # стара сесия на главния потребител
        sec = (_API_KEY + "|" + APP_PASSWORD).encode()
        legacy = _hmac.new(sec, ("session:" + APP_USER).encode(), _hashlib.sha256).hexdigest()
        return APP_USER.lower() if (APP_USER and _hmac.compare_digest(cookie, legacy)) else None
    user = cookie.split(".", 1)[0]
    if _secret_for(user) is None:
        return None
    return user if _hmac.compare_digest(cookie, session_token(user)) else None


INGEST_TOKEN = _os.getenv("INGEST_TOKEN", "").strip()
# Пътища, в които страницата anindk (публична, без вход) може само да
# ЗАПИСВА заявки за сравнение - с отделен ограничен ключ, не с главния.
_INGEST_PATHS = {"/compare/manual-order"}


def is_authenticated(request: Request) -> bool:
    if (request.url.path in _INGEST_PATHS and INGEST_TOKEN
            and _hmac.compare_digest(request.headers.get("X-Ingest-Token", ""), INGEST_TOKEN)):
        return True
    if _API_KEY and _hmac.compare_digest(request.headers.get("X-API-Key", ""), _API_KEY):
        return True
    user = session_user(request)
    if user:
        request.state.user = user
        return True
    return False


@app.middleware("http")
async def _require_auth(request: Request, call_next):
    # логото се ВИЖДА без вход (нужно е на екрана за вход), но се сменя само с вход
    open_path = request.url.path in _OPEN_PATHS and not (
        request.url.path == "/brand/logo" and request.method != "GET")
    if open_path:
        return await call_next(request)
    if not _API_KEY and not APP_PASSWORD:
        client = request.client.host if request.client else ""
        if client in ("127.0.0.1", "::1", "localhost", "testclient"):
            return await call_next(request)
    if not is_authenticated(request):
        if request.method == "GET" and request.url.path == "/presentation":
            from fastapi.responses import RedirectResponse
            return RedirectResponse("/?next=/presentation", status_code=302)  # първо вход, после файла
        return JSONResponse(status_code=401, content={"detail": "Нужен е вход"})
    # „Анализи" - само за определени потребители (по подразбиране Димо и Асен);
    # с API ключ (системни задачи) - разрешено
    p = request.url.path
    # профили „само Анализи" (напр. Теди): виждат само раздела „Анализи", без промени
    user0 = getattr(request.state, "user", None)
    if user0 is not None and user0.lower() in analytics_only_users():
        ok = (request.method == "GET" and (p.startswith("/ui/analytics") or p in ("/", "/ui/me", "/stores")))
        if not ok:
            return JSONResponse(status_code=403, content={"detail": "Профилът е само за „Анализи“"})
    if p.startswith("/ui/analytics") or p.startswith("/analytics"):
        user = getattr(request.state, "user", None)
        if user is not None and user.lower() not in analytics_users():
            return JSONResponse(status_code=403, content={"detail": "Нямате достъп до „Анализи“"})
    return await call_next(request)


def analytics_users() -> set[str]:
    import os as __os
    raw = __os.getenv("ANALYTICS_USERS", "dimo,asen,tedi")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def analytics_only_users() -> set[str]:
    import os as __os
    raw = __os.getenv("ANALYTICS_ONLY_USERS", "tedi")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


# anindk.netlify.app праща копие на заявките на магазините (CORS)
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://anindk.netlify.app", "http://anindk.netlify.app"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Ingest-Token"],
)


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
def upsert_settings(
    request: Request,
    items: list[SettingIn],
    source: str = Query("api", pattern="^(api|manual|learning|rules|import)$"),
    reason: str | None = None,
    db: Session = Depends(get_db),
):
    """
    source=manual  -> корекция от човек в пулта: записва се и се ЗАКЛЮЧВА
                      (автоматичното учене никога не я презаписва).
    source=learning -> автоматично учене: заключените позиции се пропускат.
    Всяка промяна отива в дневника settings_log.
    """
    who = getattr(request.state, "user", None)
    if source == "manual" and who:
        reason = f"{reason or 'корекция'} · {who}"
    sku_map = {a.sku: a.id for a in db.execute(select(m.Article)).scalars().all()}
    updated, unknown, invalid, skipped_locked = 0, [], [], 0

    for it in items:
        aid = sku_map.get(it.sku)
        if aid is None:
            unknown.append(it.sku)
            continue
        if it.max_stock < it.min_stock:
            invalid.append({"sku": it.sku, "reason": "max_stock < min_stock"})
            continue

        existing = db.get(m.StoreArticleSetting, (it.store_id, aid))
        if existing is not None and source == "learning" and existing.auto_adjust is False:
            skipped_locked += 1
            continue
        lock = False if source == "manual" else (
            existing.auto_adjust if existing is not None else it.auto_adjust)
        old = (float(existing.min_stock), float(existing.max_stock)) if existing else (None, None)
        if existing:
            existing.min_stock = it.min_stock
            existing.max_stock = it.max_stock
            existing.auto_adjust = lock
            if it.supplier_id:
                existing.supplier_id = it.supplier_id
        else:
            db.add(m.StoreArticleSetting(
                store_id=it.store_id, article_id=aid,
                min_stock=it.min_stock, max_stock=it.max_stock,
                supplier_id=it.supplier_id, auto_adjust=lock,
            ))
        if old != (float(it.min_stock), float(it.max_stock)):
            db.add(m.SettingsLog(
                store_id=it.store_id, article_id=aid,
                old_min=old[0], old_max=old[1],
                new_min=it.min_stock, new_max=it.max_stock,
                source=source, reason=reason,
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
    return {"updated": updated, "unknown_skus": unknown, "invalid": invalid,
            "skipped_locked": skipped_locked}


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


# Единният интерфейс (табло, заявки, асортимент, планограма, импорт)
from .ui import router as ui_router  # noqa: E402

app.include_router(ui_router)


# Автоматично опресняване на наличностите от Мистрал (на всеки 30 мин, 07-22 ч.)
@app.on_event("startup")
def _start_refresher():
    import os as __os
    if __os.getenv("DISABLE_REFRESHER") != "1":
        from . import refresher
        refresher.start()
