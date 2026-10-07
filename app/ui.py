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

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
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


_FAILS: dict[str, list[float]] = {}
_MAX_FAILS, _WINDOW = 5, 15 * 60


@router.post("/ui/login", include_in_schema=False)
def ui_login(payload: LoginIn, request: Request, db: Session = Depends(get_db)):
    import time
    from . import main as _main
    user = payload.username.strip().lower()
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "").split(",")[0].strip()
    key = f"{user}|{ip}"
    now = time.time()
    fails = [t for t in _FAILS.get(key, []) if now - t < _WINDOW]
    if len(fails) >= _MAX_FAILS:
        mins = int((_WINDOW - (now - fails[0])) // 60) + 1
        raise HTTPException(429, f"Твърде много грешни опити. Опитай след {mins} мин.")
    ok = False
    if _main.APP_USER and user == _main.APP_USER.lower():
        ok = hmac.compare_digest(payload.password, _main.APP_PASSWORD)
    else:
        u = db.get(m.AppUser, user)
        ok = bool(u and u.is_active and _main.check_password(payload.password, u.password_hash))
    if not ok:
        _FAILS[key] = fails + [now]
        raise HTTPException(401, "Грешен потребител или парола")
    _FAILS.pop(key, None)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        _main.SESSION_COOKIE, _main.session_token(user),
        max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax",
        secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
    )
    return resp


@router.get("/ui/me")
def ui_me(request: Request, db: Session = Depends(get_db)):
    from . import main as _main
    user = getattr(request.state, "user", None)
    if not user:
        return {"user": None, "name": "система", "analytics": True}
    u = db.get(m.AppUser, user)
    return {"user": user, "name": u.display_name if u else "Димо",
            "analytics": user.lower() in _main.analytics_users(),
            "analytics_only": user.lower() in _main.analytics_only_users(),
            "anomalies_only": user.lower() in _main.anomalies_only_users()}


class UserIn(BaseModel):
    username: str
    display_name: str
    password: str


@router.post("/users")
def users_upsert(payload: UserIn, request: Request, db: Session = Depends(get_db)):
    """Създава/обновява потребител. Само главният потребител или API ключ."""
    from . import main as _main
    who = getattr(request.state, "user", None)
    if who and who != _main.APP_USER.lower():
        raise HTTPException(403, "Само главният потребител добавя потребители")
    name = payload.username.strip().lower()
    if not name.isascii() or not name.replace("_", "").isalnum():
        raise HTTPException(400, "Потребителското име - само латиница и цифри")
    if len(payload.password) < 4:
        raise HTTPException(400, "Паролата е поне 4 знака")
    u = db.get(m.AppUser, name)
    h = _main.hash_password(payload.password)
    if u is None:
        db.add(m.AppUser(username=name, display_name=payload.display_name.strip(), password_hash=h))
    else:
        u.display_name, u.password_hash, u.is_active = payload.display_name.strip(), h, True
    db.commit()
    return {"ok": True, "username": name}


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
            "no_order": bool(a.no_order), "no_order_reason": a.no_order_reason,
            "no_order_by": a.no_order_by,
            "no_order_at": a.no_order_at.astimezone(_SOFIA).strftime("%d.%m.%Y %H:%M") if a.no_order_at else None,
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
            "status": "no_order" if (a.no_order and in_plano) else status, "is_active": a.is_active,
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


@router.post("/sales/sync-mistral")
def sales_sync_mistral(days: int = Query(14, ge=1, le=90), db: Session = Depends(get_db)):
    """Продажби от касовите бонове в Мистрал за последните N дни -> sales_history."""
    from . import mistral
    try:
        return mistral.sync_sales(db, days)
    except Exception as e:
        raise HTTPException(502, f"Мистрал: {type(e).__name__}: {e}")


@router.get("/ui/sales")
def ui_sales(db: Session = Depends(get_db)):
    """Продажби по магазин и артикул за заредения период (за справки)."""
    rows = db.execute(
        select(
            m.SalesHistory.store_id, m.SalesHistory.article_id,
            func.sum(m.SalesHistory.quantity_sold),
            func.min(m.SalesHistory.sale_date), func.max(m.SalesHistory.sale_date),
        ).group_by(m.SalesHistory.store_id, m.SalesHistory.article_id)
    ).all()
    return [
        {"store_id": s, "article_id": a, "qty": float(q or 0),
         "from": str(d1), "to": str(d2)}
        for s, a, q, d1, d2 in rows
    ]


@router.get("/ui/mistral/supplier-info")
def mistral_supplier_info(codes: str):
    """Кой доставя артикулите по обекти. codes = 13407,20830,..."""
    from . import mistral
    try:
        parsed = [int(c) for c in codes.split(",") if c.strip().isdigit()]
    except ValueError:
        raise HTTPException(400, "Кодовете трябва да са числа")
    try:
        return mistral.supplier_info(parsed)
    except Exception as e:
        raise HTTPException(502, f"Мистрал: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Сравнение: заявки на магазините срещу MinMaxAI
# ---------------------------------------------------------------------------

class ManualLine(BaseModel):
    sku: str
    name: str | None = None
    qty: float


class ManualOrderIn(BaseModel):
    store: str
    lines: list[ManualLine]
    text: str | None = None
    source: str = "anindk"
    made_at: datetime | None = None   # кога е направена (за закъснели копия)


@router.post("/compare/manual-order")
def compare_manual_order(payload: ManualOrderIn, db: Session = Depends(get_db)):
    """Получава заявка на магазин (от anindk) и записва нашата за същия момент."""
    from . import compare
    return compare.record(db, payload.store,
                          [l.model_dump() for l in payload.lines],
                          payload.text, payload.source, payload.made_at)


@router.post("/compare/upload")
async def compare_upload(files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    """
    Ръчно качване на Excel заявки на магазини (бланката на НДК:
    ArtNomer / Artikul / MerEd / Kol). Магазинът се взима от името на файла.
    """
    import openpyxl
    from io import BytesIO
    from . import compare
    out = []
    for f in files:
        wb = openpyxl.load_workbook(BytesIO(await f.read()), data_only=True)
        # файлът от anindk („Заявки_….xlsx") има по един лист за магазин;
        # иначе - един лист, магазинът е в името на файла
        multi = len(wb.worksheets) > 1 or not wb.worksheets[0].title.lower().startswith("sheet")
        for ws in wb.worksheets:
            title = ws.title.strip()
            if "капачки" in title.lower():
                continue
            lines = []
            for r in ws.iter_rows(min_row=2, values_only=True, max_col=5):
                if r and r[0] is not None and len(r) > 3 and isinstance(r[3], (int, float)):
                    lines.append({"sku": str(int(r[0])) if isinstance(r[0], (int, float)) else str(r[0]),
                                  "name": r[1], "qty": float(r[3])})
            if not lines:
                continue
            store = title if multi else (f.filename or "").rsplit(".", 1)[0]
            res = compare.record(db, store, lines, None, "upload")
            out.append({"file": f.filename, "sheet": title, **res})
    return out


@router.get("/ui/compare")
def ui_compare(days: int = Query(14, ge=1, le=120), db: Session = Depends(get_db)):
    from . import compare
    return compare.summary(db, days)


@router.get("/ui/compare/export")
def ui_compare_export(days: int = Query(14, ge=1, le=120), db: Session = Depends(get_db)):
    from . import compare
    content = compare.export_xlsx(db, days)
    name = f"Сравнение_заявки_{days}дни.xlsx"
    return Response(content=content,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8\'\'{quote(name)}"})


@router.get("/ui/compare/{order_id}/xlsx")
def ui_compare_xlsx(order_id: int, side: str = Query("api", pattern="^(api|store)$"),
                    db: Session = Depends(get_db)):
    from . import compare
    r = compare.order_xlsx(db, order_id, side)
    if r is None:
        raise HTTPException(404, "Няма такава заявка")
    name, content = r
    return Response(content=content,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@router.get("/ui/compare/{order_id}")
def ui_compare_detail(order_id: int, db: Session = Depends(get_db)):
    from . import compare
    d = compare.detail(db, order_id)
    if not d:
        raise HTTPException(404, "Няма такава заявка")
    return d


@router.delete("/ui/compare/{order_id}")
def ui_compare_delete(order_id: int, db: Session = Depends(get_db)):
    o = db.get(m.ManualOrder, order_id)
    if o:
        db.delete(o)
        db.commit()
    return {"deleted": bool(o)}


# ---------------------------------------------------------------------------
# Самообучение на мин/макс
# ---------------------------------------------------------------------------

@router.post("/learning/run")
def learning_run(apply: bool = True, db: Session = Depends(get_db)):
    """Нощното учене (вика се от cron в 02:30). apply=false = само преглед."""
    from . import learning
    return learning.run(db, apply)


@router.get("/ui/learning")
def ui_learning(days: int = Query(14, ge=1, le=90), source: str | None = None, db: Session = Depends(get_db)):
    from . import learning
    stores = {x.id: x.name for x in db.execute(select(m.Store)).scalars().all()}
    arts = {a.id: a for a in db.execute(select(m.Article)).scalars().all()}
    locked = [
        {"store_id": x.store_id, "store": stores.get(x.store_id), "sku": arts[x.article_id].sku,
         "name": arts[x.article_id].supplier_name or arts[x.article_id].name,
         "min": float(x.min_stock), "max": float(x.max_stock)}
        for x in db.execute(select(m.StoreArticleSetting)
                            .where(m.StoreArticleSetting.auto_adjust.is_(False))).scalars().all()
        if x.article_id in arts
    ]
    return {"log": learning.recent_log(db, days, 500, source), "locked": locked}


class UnlockIn(BaseModel):
    store_id: int
    sku: str


@router.post("/settings/unlock")
def settings_unlock(items: list[UnlockIn], db: Session = Depends(get_db)):
    """Отключва позиции, за да ги поеме автоматичното учене."""
    arts = {a.sku: a.id for a in db.execute(select(m.Article)).scalars().all()}
    n = 0
    for it in items:
        row = db.get(m.StoreArticleSetting, (it.store_id, arts.get(it.sku)))
        if row is not None and row.auto_adjust is False:
            row.auto_adjust = True
            n += 1
    db.commit()
    return {"unlocked": n}


# ---------------------------------------------------------------------------
# Логото на 300 - качва се веднъж от пулта, показва се навсякъде
# ---------------------------------------------------------------------------

@router.get("/brand/logo", include_in_schema=False)
def brand_logo(db: Session = Depends(get_db)):
    a = db.get(m.AppAsset, "logo")
    if a is None:
        raise HTTPException(404, "Няма качено лого")
    return Response(content=a.content, media_type=a.mime,
                    headers={"Cache-Control": "public, max-age=300"})


@router.post("/brand/logo")
async def brand_logo_upload(request: Request, db: Session = Depends(get_db)):
    """Качване на логото (PNG / SVG / JPG / WEBP, до 2 MB). GET /brand/logo е публичен,
    POST изисква вход - минава през общата проверка."""
    form = await request.form()
    f = form.get("file")
    if f is None:
        raise HTTPException(400, "Липсва файл")
    data = await f.read()
    mime = (f.content_type or "").lower()
    if mime not in ("image/png", "image/svg+xml", "image/jpeg", "image/webp"):
        raise HTTPException(400, "Само PNG, SVG, JPG или WEBP")
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(400, "Файлът е над 2 MB")
    a = db.get(m.AppAsset, "logo")
    if a is None:
        db.add(m.AppAsset(key="logo", mime=mime, content=data))
    else:
        a.mime, a.content = mime, data
    db.commit()
    return {"ok": True, "bytes": len(data)}


class NoOrderIn(BaseModel):
    no_order: bool
    reason: str | None = None


@router.post("/articles/{sku}/no-order")
def article_no_order(sku: str, payload: NoOrderIn, request: Request, db: Session = Depends(get_db)):
    """Отбелязва артикул „Не се поръчва“ (или го връща). Помни се трайно."""
    a = db.execute(select(m.Article).where(m.Article.sku == sku)).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "Няма такъв артикул")
    who = getattr(request.state, "user", None) or "система"
    a.no_order = payload.no_order
    a.no_order_reason = (payload.reason or "").strip() or None if payload.no_order else None
    a.no_order_by = who
    a.no_order_at = datetime.now(timezone.utc)
    db.commit()
    plano = db.execute(select(func.count()).select_from(m.Planogram)
                       .where(m.Planogram.article_id == a.id)).scalar() or 0
    return {"sku": a.sku, "no_order": a.no_order, "by": who, "in_planogram_stores": plano}


@router.get("/ui/orders/{store_id}/explain")
def ui_order_explain(store_id: int, db: Session = Depends(get_db)):
    """Графика на продажбите + обяснение за всеки ред от заявката на магазина."""
    from . import compare
    return compare.order_explain(db, store_id)


@router.get("/ui/refresh-status")
def ui_refresh_status(db: Session = Depends(get_db)):
    from . import refresher
    last = db.execute(select(func.max(m.StockSnapshot.captured_at))).scalar()
    return {"stock_at": _fmt(last), **refresher.status()}


# ---------------------------------------------------------------------------
# „Не се поръчват" по обекти
# ---------------------------------------------------------------------------

@router.get("/ui/not-ordered")
def ui_not_ordered(db: Session = Depends(get_db)):
    from . import notordered
    return notordered.summary(db)


@router.get("/ui/not-ordered/list")
def ui_not_ordered_list(reason: str | None = None, selling: bool = False, db: Session = Depends(get_db)):
    from . import notordered
    rows = notordered.all_rows(db, reason or None, selling)
    return {"count": len(rows), "rows": rows, "reasons": notordered.REASONS}


@router.get("/ui/not-ordered/{store_id}")
def ui_not_ordered_store(store_id: int, db: Session = Depends(get_db)):
    from . import notordered
    st = db.get(m.Store, store_id)
    if not st:
        raise HTTPException(404, "Няма такъв магазин")
    return {"store": st.name, "reasons": notordered.REASONS, "rows": notordered.store_rows(db, store_id)}


# ---------------------------------------------------------------------------
# Презентацията - отваря се само след вход
# ---------------------------------------------------------------------------

@router.get("/presentation", include_in_schema=False)
def presentation(db: Session = Depends(get_db)):
    a = db.get(m.AppAsset, "presentation")
    if a is None:
        raise HTTPException(404, "Още няма качена презентация")
    return Response(content=a.content, media_type="application/pdf",
                    headers={"Content-Disposition": "inline; filename*=UTF-8''MinMaxAI_prezentaciya.pdf",
                             "Cache-Control": "private, max-age=60"})


@router.post("/presentation")
async def presentation_upload(request: Request, db: Session = Depends(get_db)):
    from . import main as _main
    who = getattr(request.state, "user", None)
    if who and who != _main.APP_USER.lower():
        raise HTTPException(403, "Само главният потребител качва презентацията")
    f = (await request.form()).get("file")
    if f is None:
        raise HTTPException(400, "Липсва файл")
    data = await f.read()
    if not data.startswith(b"%PDF") or len(data) > 15 * 1024 * 1024:
        raise HTTPException(400, "Само PDF до 15 MB")
    a = db.get(m.AppAsset, "presentation")
    if a is None:
        db.add(m.AppAsset(key="presentation", mime="application/pdf", content=data))
    else:
        a.content, a.mime = data, "application/pdf"
    db.commit()
    return {"ok": True, "bytes": len(data)}


# ---------------------------------------------------------------------------
# Качество на заявките
# ---------------------------------------------------------------------------

@router.get("/ui/quality")
def ui_quality(days: int = Query(30, ge=1, le=120), db: Session = Depends(get_db)):
    from . import quality
    c = quality.compute(db)
    return {**c, "history": quality.history(db, days)}


@router.get("/ui/quality/list")
def ui_quality_list(status: str | None = None, store_id: int | None = None, db: Session = Depends(get_db)):
    from . import quality
    rows = [r for r in quality.positions(db, store_id) if r["status"] != "idle" and (not status or r["status"] == status)]
    order = {"out": 0, "over": 1, "low": 2, "ok": 3}
    rows.sort(key=lambda r: (order[r["status"]], -r["per_day"], r["store"]))
    return {"count": len(rows), "rows": rows, "labels": quality.LABEL}


@router.post("/quality/record")
def quality_record(db: Session = Depends(get_db)):
    """Ръчна снимка за днес (нормално става всяка нощ в 02:30)."""
    from . import quality
    return quality.record(db)


@router.post("/quality/prune")
def quality_prune(db: Session = Depends(get_db)):
    from . import quality
    return {"deleted": quality.prune_snapshots(db)}


# ---------------------------------------------------------------------------
# Наличности в два момента (напр. преди и след ревизия)
# ---------------------------------------------------------------------------

@router.get("/ui/stock-compare")
def ui_stock_compare(store_id: int, t1: str, t2: str, db: Session = Depends(get_db)):
    """Наличност на всеки артикул от планограмата към t1 и към t2 (ISO, бг. време)."""
    from sqlalchemy import text as _t
    def at(ts: str) -> dict:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_SOFIA)
        rows = db.execute(_t("""
            SELECT DISTINCT ON (article_id) article_id, quantity, captured_at
            FROM stock_snapshots WHERE store_id = :s AND captured_at <= :t
            ORDER BY article_id, captured_at DESC"""), {"s": store_id, "t": dt}).all()
        return {a: (float(q), c) for a, q, c in rows}
    a1, a2 = at(t1), at(t2)
    arts = {a.id: a for a in db.execute(select(m.Article)).scalars().all()}
    plano = set(db.execute(select(m.Planogram.article_id).where(m.Planogram.store_id == store_id)).scalars().all())
    out = []
    for aid in plano:
        a = arts.get(aid)
        if a is None:
            continue
        q1 = a1.get(aid, (None, None))[0]
        q2 = a2.get(aid, (None, None))[0]
        out.append({"sku": a.sku, "name": a.supplier_name or a.name, "before": q1, "after": q2,
                    "diff": None if q1 is None or q2 is None else round(q2 - q1, 2)})
    snap = lambda d: _fmt(max((c for _, c in d.values()), default=None))  # noqa: E731
    return {"store_id": store_id, "t1_snapshot": snap(a1), "t2_snapshot": snap(a2), "rows": out}


@router.get("/ui/mistral/movement-types")
def mistral_movement_types(days: int = Query(7, ge=1, le=60), ndk_only: bool = True, db: Session = Depends(get_db)):
    from . import mistral
    codes = None
    if ndk_only:
        codes = [int(a.sku) for a in db.execute(select(m.Article).where(m.Article.is_active.is_(True))).scalars().all()
                 if a.sku.isdigit()]
    try:
        return mistral.movement_types(days, codes)
    except Exception as e:
        raise HTTPException(502, f"Мистрал: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Аномалии в наличностите
# ---------------------------------------------------------------------------

@router.post("/anomalies/sync")
def anomalies_sync(days: int = Query(14, ge=1, le=60), db: Session = Depends(get_db)):
    from . import anomalies
    try:
        return anomalies.sync(db, days)
    except Exception as e:
        db.rollback()
        raise HTTPException(502, f"Мистрал: {type(e).__name__}: {e}")


@router.get("/ui/anomalies")
def ui_anomalies(days: int = Query(7, ge=1, le=60), db: Session = Depends(get_db)):
    from . import anomalies
    return anomalies.summary(db, days)


@router.get("/ui/anomalies/list")
def ui_anomalies_list(kind: str | None = None, store_id: int | None = None,
                      days: int = Query(7, ge=1, le=60), db: Session = Depends(get_db)):
    from . import anomalies
    rows = [r for r in anomalies.items(db, days, store_id) if not kind or r["kind"] == kind]
    rows.sort(key=lambda r: (r["eur"], r["qty"]))
    return {"count": len(rows), "rows": rows, "kinds": anomalies.KIND}


# ---------------------------------------------------------------------------
# Доставки от НДК (Мистрал) срещу заявките на MinMaxAI
# ---------------------------------------------------------------------------

@router.get("/ui/deliveries")
def ui_deliveries(days: int = Query(14, ge=1, le=60), db: Session = Depends(get_db)):
    from . import deliveries
    return deliveries.summary(db, days)


@router.get("/ui/deliveries/{store_id}/{day}")
def ui_delivery_detail(store_id: int, day: str, db: Session = Depends(get_db)):
    from . import deliveries
    return deliveries.detail(db, store_id, day)


@router.get("/ui/deliveries/by-article")
def ui_deliveries_by_article(days: int = Query(7, ge=1, le=60), db: Session = Depends(get_db)):
    """Доставено от НДК по артикул за последните N дни (Мистрал, вид 2): бройки и в колко магазина."""
    from sqlalchemy import text as _t
    since = datetime.now(_SOFIA).date() - timedelta(days=days)
    rows = db.execute(_t("""SELECT article_id, SUM(qty_in), COUNT(DISTINCT store_id), MAX(day)
                            FROM stock_movements WHERE optype = 2 AND qty_in > 0 AND day >= :d
                            GROUP BY article_id"""), {"d": since}).all()
    arts = {a.id: a for a in db.execute(select(m.Article)).scalars().all()}
    return {"days": days, "since": since.isoformat(), "rows": [
        {"sku": arts[a].sku, "name": arts[a].supplier_name or arts[a].name, "qty": float(q), "stores": int(n),
         "last": d.strftime("%d.%m")} for a, q, n, d in rows if a in arts]}


# ---------------------------------------------------------------------------
# Промоции (брошури) - временно вдигане на мин/макс
# ---------------------------------------------------------------------------

class PromoIn(BaseModel):
    name: str
    start: date
    end: date
    skus: list[str]


@router.post("/promos")
def promos_create(payload: PromoIn, db: Session = Depends(get_db)):
    from . import promo
    return promo.create(db, payload.name, payload.start, payload.end, payload.skus)


@router.get("/ui/promos")
def ui_promos(db: Session = Depends(get_db)):
    from . import promo
    return {"promos": promo.listing(db), "measured": promo.measure(db)}


@router.delete("/promos/{promo_id}")
def promos_delete(promo_id: int, db: Session = Depends(get_db)):
    from sqlalchemy import text as _t
    db.execute(_t("DELETE FROM promotions WHERE id=:p"), {"p": promo_id}); db.commit()
    return {"deleted": promo_id}


# ---------------------------------------------------------------------------
# Анализи: брошура и групи
# ---------------------------------------------------------------------------

@router.get("/ui/analytics/promo")
def ui_an_promo(db: Session = Depends(get_db)):
    from . import analytics
    return analytics.promo_report(db)


@router.get("/ui/analytics/promo/{promo_id}/{sku}")
def ui_an_promo_article(promo_id: int, sku: str, db: Session = Depends(get_db)):
    from . import analytics
    return analytics.promo_article_stores(db, sku, promo_id)


@router.get("/ui/analytics/groups")
def ui_an_groups(days: int = Query(14, ge=4, le=60), db: Session = Depends(get_db)):
    from . import analytics
    return analytics.groups(db, days)


@router.get("/ui/analytics/group")
def ui_an_group(name: str, days: int = Query(14, ge=4, le=60), db: Session = Depends(get_db)):
    from . import analytics
    return analytics.group_detail(db, name, days)


@router.get("/ui/mistral/probe-sales-all")
def ui_probe_sales_all(day_offset: int = Query(1, ge=1, le=30), db: Session = Depends(get_db)):
    from . import mistral
    return mistral.probe_sales_all(db, day_offset)


# ---------------------------------------------------------------------------
# Анализи - всички доставчици
# ---------------------------------------------------------------------------

@router.post("/analytics/sync-all")
def an_sync_all(days: int = Query(30, ge=1, le=60)):
    from . import salesall
    return salesall.start_background(days)


@router.get("/ui/analytics/all/status")
def an_all_status():
    from . import salesall
    return salesall.status()


@router.get("/ui/analytics/all")
def an_all(days: int = Query(14, ge=4, le=60), by: str = Query("grp", pattern="^(grp|supplier|store)$"),
           db: Session = Depends(get_db)):
    from . import salesall
    return salesall.overview(db, days, by)


@router.get("/ui/analytics/all/detail")
def an_all_detail(by: str, key: str, days: int = Query(14, ge=4, le=60), db: Session = Depends(get_db)):
    from . import salesall
    return salesall.detail(db, by, key, days)


@router.get("/ui/analytics/all/promos")
def an_all_promos(db: Session = Depends(get_db)):
    from . import salesall
    return salesall.promos(db)


@router.get("/ui/analytics/all/articles")
def an_all_articles(days: int = Query(14, ge=4, le=60), store_id: int | None = None, group: str | None = None,
                    db: Session = Depends(get_db)):
    from . import salesall
    return salesall.articles(db, days, store_id, group)


@router.get("/ui/analytics/all/top")
def an_all_top(days: int = Query(14, ge=4, le=60), store_id: int | None = None, db: Session = Depends(get_db)):
    from . import salesall
    return salesall.top_by_group(db, days, store_id)


@router.get("/ui/analytics/all/alerts")
def an_all_alerts(days: int = Query(14, ge=4, le=60), cover: float = Query(2.0, ge=0.5, le=14),
                  db: Session = Depends(get_db)):
    from . import salesall
    return salesall.low_stock_alerts(db, days, 10, cover)


@router.get("/ui/db-size")
def ui_db_size(db: Session = Depends(get_db)):
    """Размер на базата и на най-големите таблици (MB)."""
    from sqlalchemy import text as _t
    total = db.execute(_t("SELECT pg_database_size(current_database())")).scalar()
    rows = db.execute(_t("""SELECT relname, pg_total_relation_size(relid), n_live_tup
                            FROM pg_stat_user_tables ORDER BY 2 DESC LIMIT 15""")).all()
    return {"total_mb": round(total / 1048576, 1),
            "tables": [{"table": r[0], "mb": round(r[1] / 1048576, 1), "rows": r[2]} for r in rows]}


@router.post("/admin/vacuum")
def admin_vacuum(table: str = Query("stock_snapshots", pattern="^(stock_snapshots|sa_sales|sa_stock|stock_movements)$")):
    """Връща празното място от изтрити редове на диска (VACUUM FULL)."""
    from .db import engine
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        from sqlalchemy import text as _t
        c.execute(_t(f"VACUUM FULL ANALYZE {table}"))
    return {"ok": True, "table": table}


@router.post("/orders/recalc-run/{run_id}")
def orders_recalc_run(run_id: int, add_new: bool = False, db: Session = Depends(get_db)):
    """Преизчислява пускане по наличността от часа му, с текущите правила.
    add_new=true добавя и редове, които новите правила изискват (наличност от snapshot-а към часа)."""
    return service.recalc_run(db, run_id, add_new)


@router.get("/ui/negative-selling")
def ui_negative_selling(days: int = Query(3, ge=1, le=14), db: Session = Depends(get_db)):
    """НДК артикули с наличност под нула, които продължават да се продават (последните N дни с данни)."""
    from sqlalchemy import text as _t
    last = db.execute(select(func.max(m.SalesHistory.sale_date))).scalar()
    if not last:
        return {"rows": []}
    rec = {(s_, a): float(q or 0) for s_, a, q in db.execute(select(
        m.SalesHistory.store_id, m.SalesHistory.article_id, func.sum(m.SalesHistory.quantity_sold))
        .where(m.SalesHistory.sale_date > last - timedelta(days=days))
        .group_by(m.SalesHistory.store_id, m.SalesHistory.article_id)).all()}
    s14 = {(s_, a): float(q or 0) for s_, a, q in db.execute(select(
        m.SalesHistory.store_id, m.SalesHistory.article_id, func.sum(m.SalesHistory.quantity_sold))
        .where(m.SalesHistory.sale_date > last - timedelta(days=14))
        .group_by(m.SalesHistory.store_id, m.SalesHistory.article_id)).all()}
    stock = db.execute(_t("""SELECT DISTINCT ON (store_id, article_id) store_id, article_id, quantity, captured_at
                             FROM stock_snapshots ORDER BY store_id, article_id, captured_at DESC""")).all()
    arts = {a.id: a for a in db.execute(select(m.Article)).scalars().all()}
    stores = {x.id: x.name for x in db.execute(select(m.Store).where(m.Store.is_active.is_(True))).scalars()}
    rows = []
    for sid, aid, q, at in stock:
        q = float(q)
        if q >= 0 or sid not in stores or aid not in arts or "АМБАЛАЖ" in (arts[aid].name or "").upper():
            continue
        sold = rec.get((sid, aid), 0.0)
        if sold <= 0:
            continue
        a = arts[aid]
        rows.append({"store": stores[sid], "sku": a.sku, "name": a.supplier_name or a.name, "stock": q,
                     f"sold_{days}d": sold, "sold_14d": s14.get((sid, aid), 0.0),
                     "per_day": round(s14.get((sid, aid), 0.0) / 14, 2),
                     "eur": round(-q * float(a.delivery_price or 0), 2)})
    rows.sort(key=lambda r: r["stock"])
    return {"data_until": last.strftime("%d.%m.%Y"), "days": days, "count": len(rows), "rows": rows}


@router.get("/ui/article-deliveries")
def ui_article_deliveries(sku: str, days: int = Query(30, ge=1, le=60), db: Session = Depends(get_db)):
    """Всички заведени доставки (Мистрал, вид 2) на един артикул по магазини и дати."""
    from sqlalchemy import text as _t
    a = db.execute(select(m.Article).where(m.Article.sku == sku)).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "Няма такъв артикул")
    stores = {x.id: x.name for x in db.execute(select(m.Store)).scalars()}
    since = datetime.now(_SOFIA).date() - timedelta(days=days)
    rows = db.execute(_t("""SELECT store_id, day, qty_in, qty_out FROM stock_movements
                            WHERE article_id = :a AND optype = 2 AND day >= :d ORDER BY day DESC, store_id"""),
                      {"a": a.id, "d": since}).all()
    return {"sku": sku, "name": a.supplier_name or a.name, "pack": a.pack_size,
            "rows": [{"store": stores.get(s_, s_), "day": d.strftime("%d.%m.%Y"), "qty_in": float(qi or 0),
                      "qty_out": float(qo or 0)} for s_, d, qi, qo in rows]}


@router.get("/ui/mistral/movements")
def ui_mistral_movements(store_id: int, sku: str, day_from: str, day_to: str, db: Session = Depends(get_db)):
    """Само четене: всеки ред от дневника на движенията в Мистрал за артикул в магазин (за проверка)."""
    from . import mistral
    from .anomalies import _loc_map
    with mistral.connect() as conn:
        cur = conn.cursor()
        locs = {v: k for k, v in _loc_map(db, cur).items()}
        loc = locs.get(store_id)
        if loc is None:
            raise HTTPException(404, "Магазинът няма обект в Мистрал")
        cur.execute("""SELECT OPERATIONDATE, OPERATIONTYPE, OPERAIONNUM, QTY
                       FROM MATERIALQTYLOG WITH (NOLOCK)
                       WHERE LOCATIONID = %s AND MATERIALCODE = %s
                         AND OPERATIONDATE >= %s AND OPERATIONDATE < DATEADD(day, 1, CAST(%s AS date))
                       ORDER BY OPERATIONDATE""", (loc, int(sku), day_from, day_to))
        return {"location_id": loc, "rows": [{"at": str(r["OPERATIONDATE"])[:19], "type": int(r["OPERATIONTYPE"]),
                                              "doc": str(r["OPERAIONNUM"]), "qty": float(r["QTY"])} for r in cur.fetchall()]}


@router.get("/ui/mistral/operation")
def ui_mistral_operation(store_id: int, nums: str, db: Session = Depends(get_db)):
    """Само четене: проследява операции от дневника до реалния документ - номер, дата, доставчик,
    въвел го потребител, кога е записан/редактиран, бележка. (Без пароли.)"""
    from . import mistral
    from .anomalies import _loc_map
    ids = [int(x) for x in nums.split(",") if x.strip().isdigit()][:20]
    with mistral.connect() as conn:
        cur = conn.cursor()
        loc = {v: k for k, v in _loc_map(db, cur).items()}.get(store_id)
        if loc is None or not ids:
            raise HTTPException(404, "Няма обект или номера")
        cur.execute(f"""SELECT o.NUM, o.DOCUMENTNUM, o.DOCUMENTDATE, o.DOCUMENTTYPEID, o.OPERATIONDOCTYPE, o.DOCUMENTSUM,
                               o.DATESAVED, o.LASTEDITDATE, o.EXECUTEDATE, o.NOTE, o.USERID, o.PARTNERNAMEID, o.PARENTOF,
                               o.EDITNUM, o.CHECKUSERCODE
                        FROM OPERATIONS o WITH (NOLOCK) WHERE o.LOCATIONID = %s AND o.NUM IN ({','.join(map(str, ids))})""", (loc,))
        ops = cur.fetchall()
        out = []
        # кои артикули/количества има всеки документ (за сравнение на двата)
        for o in ops:
            cur.execute("SELECT TOP 1 PARTNERNAME FROM PARTNERNAME WHERE ID = %s", (o["PARTNERNAMEID"],))
            p = cur.fetchone()
            cur.execute("SELECT TOP 1 NAME, FIRSTNAME, LASTNAME, CODE FROM USERS WHERE ID = %s ORDER BY CASE WHEN LOCATIONID = %s THEN 0 ELSE 1 END",
                        (o["USERID"], loc))
            u = cur.fetchone() or {}
            cur.execute("""SELECT DOCUMENTNUM, DOCUMENTDATE, DOCUMENTTYPEID, DOCNOTE, DOCSUM, DOCUMENTOUTNUM
                           FROM OPERATIONDOCUMENT WITH (NOLOCK) WHERE LOCATIONID = %s AND NUM = %s""", (loc, o["NUM"]))
            docs = [{k: str(v) for k, v in d.items() if v not in (None, "")} for d in cur.fetchall()]
            cur.execute("""SELECT MATERIALCODE, SUM(QTY) AS q FROM MATERIALQTYLOG WITH (NOLOCK)
                           WHERE LOCATIONID = %s AND OPERAIONNUM = %s GROUP BY MATERIALCODE""", (loc, o["NUM"]))
            lines = {int(r["MATERIALCODE"]): float(r["q"]) for r in cur.fetchall()}
            out.append({"operation": int(o["NUM"]), "lines": lines, "document_num": str(o["DOCUMENTNUM"]),
                        "document_date": str(o["DOCUMENTDATE"])[:19], "document_type": o["DOCUMENTTYPEID"],
                        "sum": float(o["DOCUMENTSUM"] or 0), "saved": str(o["DATESAVED"])[:19],
                        "edited": str(o["LASTEDITDATE"])[:19] if o["LASTEDITDATE"] else None,
                        "note": o["NOTE"], "partner": (p or {}).get("PARTNERNAME"),
                        "user": " ".join(x for x in [u.get("NAME"), u.get("FIRSTNAME"), u.get("LASTNAME")] if x) or o["USERID"],
                        "user_code": u.get("CODE"), "check_user": o["CHECKUSERCODE"], "parent": str(o["PARENTOF"]),
                        "documents": docs})
    return {"location_id": loc, "operations": out}


@router.get("/ui/mistral/delivery-docs")
def ui_delivery_docs(store_id: int, day: str, db: Session = Depends(get_db)):
    """Само четене: всички документи за доставка (вид 2) в магазин за ден - номер, сума, час,
    доставчик, потребител, редове и кои редове се повтарят между документите."""
    from . import mistral
    from .anomalies import _loc_map
    d = date.fromisoformat(day) if "-" in day else datetime.strptime(day + f".{datetime.now(_SOFIA).year}", "%d.%m.%Y").date()
    arts = {int(a.sku): (a.supplier_name or a.name) for a in db.execute(select(m.Article)).scalars().all() if a.sku.isdigit()}
    with mistral.connect() as conn:
        cur = conn.cursor()
        loc = {v: k for k, v in _loc_map(db, cur).items()}.get(store_id)
        if loc is None:
            raise HTTPException(404, "Магазинът няма обект в Мистрал")
        cur.execute("""SELECT OPERAIONNUM AS op, MATERIALCODE AS code, SUM(QTY) AS q, MIN(OPERATIONDATE) AS at
                       FROM MATERIALQTYLOG WITH (NOLOCK)
                       WHERE LOCATIONID = %s AND OPERATIONTYPE = 2
                         AND OPERATIONDATE >= %s AND OPERATIONDATE < DATEADD(day, 1, CAST(%s AS date))
                       GROUP BY OPERAIONNUM, MATERIALCODE""", (loc, d.isoformat(), d.isoformat()))
        ops = {}
        for r in cur.fetchall():
            ops.setdefault(int(r["op"]), {})[int(r["code"])] = float(r["q"])
        docs = []
        for op, lines in sorted(ops.items()):
            cur.execute("""SELECT o.DATESAVED, o.USERID, o.PARTNERNAMEID, od.DOCUMENTNUM, od.DOCSUM
                           FROM OPERATIONS o WITH (NOLOCK)
                           LEFT JOIN OPERATIONDOCUMENT od WITH (NOLOCK) ON od.LOCATIONID = o.LOCATIONID AND od.NUM = o.NUM
                           WHERE o.LOCATIONID = %s AND o.NUM = %s""", (loc, op))
            o = cur.fetchone() or {}
            pn = None
            if o.get("PARTNERNAMEID"):
                cur.execute("SELECT TOP 1 PARTNERNAME FROM PARTNERNAME WHERE ID = %s", (o["PARTNERNAMEID"],))
                pn = (cur.fetchone() or {}).get("PARTNERNAME")
            docs.append({"operation": op, "document_num": str(o.get("DOCUMENTNUM") or "—").split(".")[0],
                         "sum": float(o.get("DOCSUM") or 0), "saved": str(o.get("DATESAVED"))[11:16] if o.get("DATESAVED") else "",
                         "partner": pn, "user": o.get("USERID"), "lines": len(lines),
                         "_lines": lines})
    # повтарящи се редове между документите (еднакъв артикул и количество)
    for i, a in enumerate(docs):
        rep = []
        for j, b in enumerate(docs):
            if i == j:
                continue
            same = [c for c, q in a["_lines"].items() if b["_lines"].get(c) == q]
            if same:
                rep.append({"with": b["document_num"], "same_lines": len(same),
                            "examples": [f"{arts.get(c, c)}: {a['_lines'][c]:g}" for c in same[:6]]})
        a["repeats"] = rep
    for a in docs:
        a.pop("_lines", None)
    return {"store_id": store_id, "day": d.strftime("%d.%m.%Y"), "documents": docs}


@router.post("/anomalies/duplicate-docs")
def an_dup_docs(days: int = Query(14, ge=1, le=30), db: Session = Depends(get_db)):
    from . import anomalies
    return anomalies.detect_duplicate_docs(db, days)


# ---------------------------------------------------------------------------
# Експорт на всяка таблица от пулта в Excel
# ---------------------------------------------------------------------------

class ExportSheet(BaseModel):
    name: str
    columns: list[str]
    rows: list[list]


class ExportIn(BaseModel):
    filename: str = "Справка"
    sheets: list[ExportSheet]


@router.post("/ui/export-xlsx")
def ui_export_xlsx(payload: ExportIn):
    """Таблицата, която е на екрана -> .xlsx (Arial, заглавен ред, автофилтър, замразен ред)."""
    import re
    from io import BytesIO
    from urllib.parse import quote
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    wb = Workbook(); wb.remove(wb.active)
    F, B = Font(name="Arial", size=10), Font(name="Arial", size=10, bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="2B2420")
    for sh in payload.sheets[:10]:
        title = re.sub(r"[\\/*?:\[\]]", " ", sh.name)[:31] or "Справка"
        ws = wb.create_sheet(title)
        ws.append(sh.columns)
        for c in ws[1]:
            c.font, c.fill = B, fill
            c.alignment = Alignment(wrap_text=True, vertical="center")
        for r in sh.rows[:100000]:
            ws.append(r)
        widths = [len(str(c)) for c in sh.columns]
        for row in ws.iter_rows(min_row=2):
            for i, c in enumerate(row):
                c.font = F
                if isinstance(c.value, float):
                    c.number_format = "#,##0.00" if abs(c.value - round(c.value)) > 1e-9 else "#,##0"
                if i < len(widths):
                    widths[i] = max(widths[i], min(len(str(c.value or "")), 60))
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = max(8, min(w + 2, 62))
        ws.freeze_panes = "A2"
        if ws.max_row > 1:
            ws.auto_filter.ref = ws.dimensions
    buf = BytesIO(); wb.save(buf)
    fn = re.sub(r"[^\w\s.-]", "", payload.filename, flags=re.UNICODE).strip() or "Справка"
    stamp = datetime.now(_SOFIA).strftime("%d.%m.%Y")
    return Response(content=buf.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fn + ' ' + stamp + '.xlsx')}"})


@router.get("/ui/version")
def ui_version():
    """Версията на пулта (сменя се при всяко качване) - страницата се презарежда сама."""
    import os as _os
    return {"version": _os.getenv("RAILWAY_DEPLOYMENT_ID") or _os.getenv("RAILWAY_GIT_COMMIT_SHA") or "dev"}
