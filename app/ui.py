"""
Единен интерфейс - всичко на едно място (/):
табло, заявки по магазин с корекции и Excel, асортимент и цени,
планограма, пускане на заявки и импорт на файлове.

Вход с потребител и парола (APP_USER / APP_PASSWORD) -> бисквитка за
сесия. Машините (cron) ползват X-API-Key.
"""
from __future__ import annotations

import hmac
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from urllib.parse import quote

from . import models as m
from . import service
from . import exports as exports_svc
from .db import get_db

router = APIRouter(tags=["Интерфейс"])

_API_KEY = os.getenv("API_KEY", "").strip()
_SOFIA = timezone(timedelta(hours=3))
_PAGE = Path(__file__).with_name("ui_page.html")


def _key_ok(key: str) -> bool:
    return (not _API_KEY) or key == _API_KEY


def _fmt(dt) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(_SOFIA).strftime("%d.%m.%Y %H:%M")


def _f(v) -> float | None:
    return float(v) if v is not None else None


_PROMO = re.compile(
    r"(\d+[.,]\d+)\s*€?\s*до\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})"
)


def _active_promo(note: str | None, today: date):
    """
    Колоната "Корекции" пази история: "5.64€ до 25.05.26г/5.41€ до ...".
    Промо е активно само ако крайната му дата е днес или по-късно.
    Връща (цена, дата) на най-скоро изтичащото активно промо или None.
    """
    if not note:
        return None
    best = None
    for price, d, mth, y in _PROMO.findall(note):
        yy = int(y) + (2000 if len(y) == 2 else 0)
        try:
            until = date(yy, int(mth), int(d))
        except ValueError:
            continue
        if until >= today and (best is None or until < best[1]):
            best = (float(price.replace(",", ".")), until)
    return best


# ---------------------------------------------------------------------------
# Страница
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def app_page():
    # Самата страница не съдържа данни - всички данни идват от защитените
    # endpoints с X-API-Key. Ключът се въвежда веднъж и браузърът го помни.
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"))


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/ui/login", include_in_schema=False)
def ui_login(payload: LoginIn, request: Request):
    from . import main as _main
    ok_user = hmac.compare_digest(payload.username.strip().lower(), _main.APP_USER.lower())
    ok_pass = hmac.compare_digest(payload.password, _main.APP_PASSWORD)
    if not (_main.APP_USER and _main.APP_PASSWORD and ok_user and ok_pass):
        raise HTTPException(401, "Грешен потребител или парола")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        _main.SESSION_COOKIE, _main.session_token(),
        max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax",
        secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
    )
    return resp


@router.post("/ui/logout", include_in_schema=False)
def ui_logout():
    from . import main as _main
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_main.SESSION_COOKIE)
    return resp


@router.get("/dashboard", include_in_schema=False)
def old_dashboard():
    return RedirectResponse("/")


@router.get("/orders-view", include_in_schema=False)
def old_orders_view():
    return RedirectResponse("/#orders")


# ---------------------------------------------------------------------------
# Данни за таблото
# ---------------------------------------------------------------------------

@router.get("/ui/summary")
def ui_summary(db: Session = Depends(get_db)):
    count = lambda stmt: db.execute(stmt).scalar() or 0  # noqa: E731

    active_stores = count(
        select(func.count()).select_from(m.Store)
        .where(m.Store.is_active.is_(True))
    )
    articles = count(
        select(func.count()).select_from(m.Article)
        .where(m.Article.is_active.is_(True))
    )
    priced = count(
        select(func.count()).select_from(m.Article)
        .where(m.Article.delivery_price.isnot(None))
    )
    settings = count(select(func.count()).select_from(m.StoreArticleSetting))
    planogram = count(select(func.count()).select_from(m.Planogram))
    review = count(
        select(func.count()).select_from(m.SettingsReviewQueue)
        .where(m.SettingsReviewQueue.resolved.is_(False))
    )
    alerts = count(
        select(func.count()).select_from(m.ArticleAlert)
        .where(m.ArticleAlert.resolved.is_(False))
    )
    last_stock = db.execute(
        select(func.max(m.StockSnapshot.captured_at))
    ).scalar()

    runs = db.execute(
        select(m.DispatchRun).order_by(m.DispatchRun.started_at.desc()).limit(10)
    ).scalars().all()
    last_real = next((r for r in runs if (r.orders_created or 0) > 0), None)

    top, run_value = [], 0.0
    if last_real:
        rows = db.execute(
            select(
                m.PurchaseOrder.store_id,
                m.Store.name,
                func.count(m.PurchaseOrderLine.id),
                func.sum(m.PurchaseOrderLine.ordered_quantity),
                func.sum(
                    m.PurchaseOrderLine.ordered_quantity
                    * func.coalesce(m.Article.delivery_price, 0)
                ),
            )
            .join(m.PurchaseOrderLine,
                  m.PurchaseOrderLine.purchase_order_id == m.PurchaseOrder.id)
            .join(m.Article, m.Article.id == m.PurchaseOrderLine.article_id)
            .join(m.Store, m.Store.id == m.PurchaseOrder.store_id)
            .where(m.PurchaseOrder.dispatch_run_id == last_real.id)
            .group_by(m.PurchaseOrder.store_id, m.Store.name)
        ).all()
        for sid, name, lines, units, value in rows:
            top.append({
                "store_id": sid, "store": name, "lines": lines,
                "units": int(units or 0), "value": round(float(value or 0), 2),
            })
            run_value += float(value or 0)
        top.sort(key=lambda r: -r["value"])

    suppliers = db.execute(select(m.Supplier)).scalars().all()
    return {
        "generated": _fmt(datetime.now(timezone.utc)),
        "active_stores": active_stores,
        "articles": articles,
        "priced_articles": priced,
        "settings": settings,
        "planogram_rows": planogram,
        "review": review,
        "alerts": alerts,
        "last_stock_at": _fmt(last_stock),
        "suppliers": [
            {"id": s.id, "name": s.name, "mode": s.replenishment_mode}
            for s in suppliers
        ],
        "last_run": None if not last_real else {
            "id": last_real.id, "at": _fmt(last_real.started_at),
            "orders": last_real.orders_created,
            "lines": last_real.order_lines_created,
            "value": round(run_value, 2),
        },
        "top_stores": top[:12],
        "runs": [
            {"id": r.id, "at": _fmt(r.started_at), "status": r.status,
             "stores": r.stores_processed, "orders": r.orders_created,
             "lines": r.order_lines_created}
            for r in runs
        ],
    }


# ---------------------------------------------------------------------------
# Асортимент и цени
# ---------------------------------------------------------------------------

@router.get("/ui/assortment")
def ui_assortment(db: Session = Depends(get_db)):
    plano_cnt = dict(db.execute(
        select(m.Planogram.article_id, func.count())
        .group_by(m.Planogram.article_id)
    ).all())
    set_cnt = dict(db.execute(
        select(m.StoreArticleSetting.article_id, func.count())
        .where(m.StoreArticleSetting.max_stock > 0)
        .group_by(m.StoreArticleSetting.article_id)
    ).all())
    arts = db.execute(select(m.Article).order_by(m.Article.category, m.Article.name)).scalars().all()
    today = datetime.now(_SOFIA).date()
    out = []
    for a in arts:
        promo = _active_promo(a.price_note, today)
        dp = _f(a.delivery_price)
        out.append({
            "sku": a.sku,
            "name": a.supplier_name or a.name,
            "category": a.category,
            "pack_size": a.pack_size,
            "pack_type": a.pack_type,
            "base_price": _f(a.base_price),
            "trade_discount": _f(a.trade_discount),
            "delivery_price": _f(a.delivery_price),
            "price_note": a.price_note,
            "planogram_stores": plano_cnt.get(a.id, 0),
            "active_settings": set_cnt.get(a.id, 0),
            "is_active": a.is_active,
            "promo_price": promo[0] if promo else None,
            "promo_until": promo[1].strftime("%d.%m.%Y") if promo else None,
            "effective_price": promo[0] if promo else dp,
        })
    return out


# ---------------------------------------------------------------------------
# Планограма по магазин
# ---------------------------------------------------------------------------

@router.get("/ui/planogram/{store_id}")
def ui_planogram(store_id: int, db: Session = Depends(get_db)):
    store = db.get(m.Store, store_id)
    if not store:
        raise HTTPException(404, "Няма такъв магазин")
    plano = set(db.execute(
        select(m.Planogram.article_id).where(m.Planogram.store_id == store_id)
    ).scalars().all())
    settings = {
        s.article_id: s for s in db.execute(
            select(m.StoreArticleSetting)
            .where(m.StoreArticleSetting.store_id == store_id)
        ).scalars().all()
    }
    stock = service.latest_stock_map(db, store_id)
    ids = plano | set(settings)
    arts = {
        a.id: a for a in db.execute(
            select(m.Article).where(m.Article.id.in_(ids))
        ).scalars().all()
    } if ids else {}

    rows = []
    for aid, a in arts.items():
        s = settings.get(aid)
        in_plano = aid in plano
        if in_plano and s is None:
            status = "missing_minmax"
        elif in_plano and s is not None and float(s.max_stock) <= 0:
            status = "stopped"
        elif in_plano:
            status = "ok"
        else:
            status = "not_in_planogram"
        rows.append({
            "sku": a.sku, "name": a.supplier_name or a.name,
            "category": a.category, "in_planogram": in_plano,
            "min": _f(s.min_stock) if s else None,
            "max": _f(s.max_stock) if s else None,
            "stock": stock.get(aid),
            "status": status, "is_active": a.is_active,
        })
    rows.sort(key=lambda r: (r["category"] or "", r["name"] or ""))
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "store": {"id": store.id, "name": store.name,
                  "size_class": store.size_class, "address": store.address},
        "counts": counts, "rows": rows,
    }


# ---------------------------------------------------------------------------
# Excel за преглед на заявка
# ---------------------------------------------------------------------------

@router.get("/orders/preview/{store_id}/export")
def export_preview(
    store_id: int,
    order_date: date | None = None,
    respect_schedule: bool = False,
    db: Session = Depends(get_db),
):
    store = db.get(m.Store, store_id)
    if not store:
        raise HTTPException(404, f"Няма магазин с id {store_id}")
    d = order_date or date.today()
    res = service.calculate_for_store(db, store_id, d, None, respect_schedule)
    content = exports_svc.build_workbook_from_lines(res.lines, db)
    filename = exports_svc.order_filename(store)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


# ---------------------------------------------------------------------------
# Мистрал (MS SQL) - проверка на връзката и съдържанието
# ---------------------------------------------------------------------------

@router.get("/ui/mistral/probe")
def mistral_probe():
    from . import mistral
    if not mistral.configured():
        raise HTTPException(400, "Връзката към Мистрал не е настроена")
    try:
        return mistral.probe()
    except Exception as e:  # покажи реалната причина - таймаут, вход и т.н.
        raise HTTPException(502, f"Мистрал: {type(e).__name__}: {e}")


@router.post("/stock/sync-mistral")
def stock_sync_mistral(db: Session = Depends(get_db)):
    """Текущи наличности от Мистрал -> нова снимка. Вика се от cron и от бутона."""
    from . import mistral
    if not mistral.configured():
        raise HTTPException(400, "Връзката към Мистрал не е настроена")
    try:
        return mistral.sync_stock(db)
    except Exception as e:
        raise HTTPException(502, f"Мистрал: {type(e).__name__}: {e}")


@router.get("/ui/mistral/tables")
def mistral_tables():
    from . import mistral
    try:
        return mistral.list_tables()
    except Exception as e:
        raise HTTPException(502, f"Мистрал: {type(e).__name__}: {e}")


@router.get("/ui/mistral/sample")
def mistral_sample(table: str, n: int = 5):
    from . import mistral
    try:
        return mistral.sample_table(table, n)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"Мистрал: {type(e).__name__}: {e}")
