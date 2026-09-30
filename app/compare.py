"""
Сравнение: заявките на магазините (от Viber, през страницата anindk)
срещу заявките, които MinMaxAI би пуснал в същия момент.

При получаване на заявка от магазин:
  1) ако наличностите са по-стари от 30 мин - дърпаме свежи от Мистрал;
  2) смятаме нашата заявка за този магазин по същите наличности;
  3) записваме и двете рамо до рамо (manual_orders + manual_order_lines).
Така сравнението е честно - един и същ момент, една и съща наличност.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import models as m
from . import service
from .imports import normalize_store_name

SOFIA = timezone(timedelta(hours=3))
STALE_AFTER = timedelta(minutes=30)


def resolve_store(db: Session, raw: str) -> int | None:
    n = normalize_store_name(raw or "")
    if not n:
        return None
    lk: dict[str, int] = {}
    for s in db.execute(select(m.Store)).scalars().all():
        lk[normalize_store_name(s.name)] = s.id
    for al in db.execute(select(m.StoreAlias)).scalars().all():
        lk[al.alias_normalized] = al.store_id
    if n in lk:
        return lk[n]
    # "АНДЖИ-ГЛАДСТОН 10" -> "АНДЖИ"; "Д.ЛАВРЕНТИЙ" -> "ЛАВРЕНТИЙ"
    for cand in (n.split("-")[0].strip(), n.replace("Д.", "", 1).strip(),
                 n.split(" ")[0].strip()):
        if cand in lk:
            return lk[cand]
    return None


def _fresh_stock(db: Session) -> datetime | None:
    last = db.execute(select(func.max(m.StockSnapshot.captured_at))).scalar()
    now = datetime.now(timezone.utc)
    if last is None or now - last > STALE_AFTER:
        try:
            from . import mistral
            if mistral.configured():
                mistral.sync_stock(db)
                last = db.execute(select(func.max(m.StockSnapshot.captured_at))).scalar()
        except Exception:
            pass  # сравняваме по последните налични данни
    return last


def record(db: Session, store_raw: str, lines: list[dict], raw_text: str | None,
           source: str = "anindk") -> dict:
    """lines: [{sku, name, qty}] - заявката на магазина в бройки."""
    store_id = resolve_store(db, store_raw)
    stock_at = _fresh_stock(db)
    order = m.ManualOrder(store_id=store_id, store_raw=store_raw.strip(),
                          raw_text=(raw_text or "")[:20000] or None,
                          source=source, stock_at=stock_at)
    db.add(order)
    db.flush()

    store_qty: dict[str, float] = {}
    names: dict[str, str] = {}
    for ln in lines:
        sku = str(ln.get("sku") or "").strip()
        if not sku:
            continue
        store_qty[sku] = store_qty.get(sku, 0.0) + float(ln.get("qty") or 0)
        if ln.get("name"):
            names[sku] = str(ln["name"])

    api: dict[str, object] = {}
    ctx: dict[str, dict] = {}
    if store_id is not None:
        today = datetime.now(SOFIA).date()
        res = service.calculate_for_store(db, store_id, today, None, False)
        api = {l.sku: l for l in res.lines}
        arts = {a.sku: a for a in db.execute(select(m.Article)).scalars().all()}
        settings = {
            s.article_id: s for s in db.execute(
                select(m.StoreArticleSetting)
                .where(m.StoreArticleSetting.store_id == store_id)
            ).scalars().all()
        }
        stock = service.latest_stock_map(db, store_id)
        plano = set(db.execute(
            select(m.Planogram.article_id).where(m.Planogram.store_id == store_id)
        ).scalars().all())
        for sku in set(store_qty) | set(api):
            a = arts.get(sku)
            if a is None:
                ctx[sku] = {"note": "непознат код"}
                continue
            s = settings.get(a.id)
            ctx[sku] = {
                "name": a.supplier_name or a.name,
                "stock": stock.get(a.id),
                "min": float(s.min_stock) if s else None,
                "max": float(s.max_stock) if s else None,
                "pack": a.pack_size,
                "plano": a.id in plano,
                "price": float(a.delivery_price) if a.delivery_price is not None else None,
                "active": a.is_active,
            }

    for sku in set(store_qty) | set(api):
        c = ctx.get(sku, {})
        our = api.get(sku)
        db.add(m.ManualOrderLine(
            order_id=order.id, sku=sku,
            name=c.get("name") or names.get(sku) or (our.name if our else None),
            store_qty=store_qty.get(sku, 0.0),
            api_qty=float(our.ordered_quantity) if our else 0.0,
            stock=c.get("stock"), min_stock=c.get("min"), max_stock=c.get("max"),
            pack_size=c.get("pack"), in_planogram=c.get("plano"),
            price=c.get("price"), note=_reason(store_qty.get(sku, 0.0), our, c),
        ))
    db.commit()
    return {"order_id": order.id, "store_id": store_id,
            "matched_store": store_id is not None,
            "store_lines": len(store_qty), "api_lines": len(api)}


def _reason(sq: float, our, c: dict) -> str:
    """Кратко обяснение защо се разминаваме - за човека, който гледа."""
    if c.get("note"):
        return c["note"]
    aq = float(our.ordered_quantity) if our else 0.0
    if sq > 0 and aq > 0:
        return "и двамата" if abs(sq - aq) < 0.01 else "различно количество"
    if sq > 0:
        if c.get("active") is False:
            return "само магазин — артикулът е спрян"
        if c.get("max") == 0:
            return "само магазин — спрян 0-0"
        if c.get("plano") is False:
            return "само магазин — извън планограмата"
        if c.get("min") is None:
            return "само магазин — няма мин/макс"
        st = c.get("stock")
        if st is not None and st >= c["min"]:
            return "само магазин — наличността е над мин"
        return "само магазин"
    return "само MinMaxAI — под мин"


# ---------------------------------------------------------------------------
# Справки
# ---------------------------------------------------------------------------

def _money(q, p):
    return float(q or 0) * float(p or 0)


def summary(db: Session, days: int = 14) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    orders = db.execute(
        select(m.ManualOrder).where(m.ManualOrder.received_at >= since)
        .order_by(m.ManualOrder.received_at.desc())
    ).scalars().all()
    stores = {s.id: s.name for s in db.execute(select(m.Store)).scalars().all()}
    ids = [o.id for o in orders]
    lines = db.execute(
        select(m.ManualOrderLine).where(m.ManualOrderLine.order_id.in_(ids))
    ).scalars().all() if ids else []
    by_order: dict[int, list] = {}
    for ln in lines:
        by_order.setdefault(ln.order_id, []).append(ln)

    rows, tot = [], {"orders": 0, "both": 0, "only_store": 0, "only_api": 0,
                     "same_qty": 0, "store_units": 0.0, "api_units": 0.0,
                     "store_value": 0.0, "api_value": 0.0}
    for o in orders:
        ls = by_order.get(o.id, [])
        st = {
            "both": sum(1 for l in ls if l.store_qty > 0 and l.api_qty > 0),
            "only_store": sum(1 for l in ls if l.store_qty > 0 and l.api_qty == 0),
            "only_api": sum(1 for l in ls if l.store_qty == 0 and l.api_qty > 0),
            "same_qty": sum(1 for l in ls if l.store_qty > 0 and l.store_qty == l.api_qty),
            "store_units": float(sum(l.store_qty for l in ls)),
            "api_units": float(sum(l.api_qty for l in ls)),
            "store_value": round(sum(_money(l.store_qty, l.price) for l in ls), 2),
            "api_value": round(sum(_money(l.api_qty, l.price) for l in ls), 2),
        }
        for k, v in st.items():
            tot[k] += v
        tot["orders"] += 1
        rows.append({
            "id": o.id, "store": stores.get(o.store_id, o.store_raw),
            "matched": o.store_id is not None,
            "at": o.received_at.astimezone(SOFIA).strftime("%d.%m.%Y %H:%M"),
            "stock_at": o.stock_at.astimezone(SOFIA).strftime("%d.%m %H:%M") if o.stock_at else None,
            **st,
        })
    tot["store_value"] = round(tot["store_value"], 2)
    tot["api_value"] = round(tot["api_value"], 2)
    return {"days": days, "totals": tot, "orders": rows}


def detail(db: Session, order_id: int) -> dict:
    o = db.get(m.ManualOrder, order_id)
    if o is None:
        return {}
    store = db.get(m.Store, o.store_id) if o.store_id else None
    ls = db.execute(
        select(m.ManualOrderLine).where(m.ManualOrderLine.order_id == order_id)
    ).scalars().all()
    # средни продажби на ден за последните 14 дни (за преценка кой е прав)
    sdp = {}
    if o.store_id:
        since = o.received_at.date() - timedelta(days=14)
        for aid, sku, q in db.execute(
            select(m.SalesHistory.article_id, m.Article.sku,
                   func.sum(m.SalesHistory.quantity_sold))
            .join(m.Article, m.Article.id == m.SalesHistory.article_id)
            .where(m.SalesHistory.store_id == o.store_id,
                   m.SalesHistory.sale_date >= since)
            .group_by(m.SalesHistory.article_id, m.Article.sku)
        ).all():
            sdp[sku] = round(float(q or 0) / 14, 2)
    out = []
    for l in ls:
        out.append({
            "sku": l.sku, "name": l.name,
            "store_qty": float(l.store_qty), "api_qty": float(l.api_qty),
            "diff": float(l.api_qty) - float(l.store_qty),
            "stock": float(l.stock) if l.stock is not None else None,
            "min": float(l.min_stock) if l.min_stock is not None else None,
            "max": float(l.max_stock) if l.max_stock is not None else None,
            "pack": l.pack_size, "price": float(l.price) if l.price is not None else None,
            "sales_per_day": sdp.get(l.sku), "note": l.note,
        })
    out.sort(key=lambda r: (r["note"] != "различно количество",
                            -abs(r["diff"]), r["name"] or ""))
    return {
        "id": o.id, "store": store.name if store else o.store_raw,
        "at": o.received_at.astimezone(SOFIA).strftime("%d.%m.%Y %H:%M"),
        "stock_at": o.stock_at.astimezone(SOFIA).strftime("%d.%m.%Y %H:%M") if o.stock_at else None,
        "raw_text": o.raw_text, "lines": out,
    }


def export_xlsx(db: Session, days: int = 14) -> bytes:
    import openpyxl
    from io import BytesIO
    from openpyxl.styles import Font, PatternFill

    s = summary(db, days)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "По заявки"
    hdr = ["Магазин", "Получена", "Наличности от", "Съвпадащи редове", "Само магазин",
           "Само MinMaxAI", "Еднакво к-во", "Бройки магазин", "Бройки MinMaxAI",
           "Стойност магазин €", "Стойност MinMaxAI €"]
    ws.append(hdr)
    for r in s["orders"]:
        ws.append([r["store"], r["at"], r["stock_at"], r["both"], r["only_store"],
                   r["only_api"], r["same_qty"], r["store_units"], r["api_units"],
                   r["store_value"], r["api_value"]])
    d2 = wb.create_sheet("Всички редове")
    d2.append(["Магазин", "Получена", "Код", "Артикул", "Магазин поръча", "MinMaxAI",
               "Разлика", "Наличност", "Мин", "Макс", "Опак.", "Продажби/ден",
               "Цена €", "Коментар"])
    for r in s["orders"]:
        d = detail(db, r["id"])
        for l in d["lines"]:
            d2.append([d["store"], d["at"], int(l["sku"]) if l["sku"].isdigit() else l["sku"],
                       l["name"], l["store_qty"], l["api_qty"], l["diff"], l["stock"],
                       l["min"], l["max"], l["pack"], l["sales_per_day"], l["price"],
                       l["note"]])
    hf = Font(bold=True, color="FFFFFF")
    hb = PatternFill("solid", fgColor="2F6358")
    for sh, widths in ((ws, [30, 17, 14, 12, 12, 13, 12, 13, 14, 15, 16]),
                       (d2, [28, 17, 9, 44, 12, 11, 9, 10, 7, 7, 7, 12, 9, 36])):
        for c in sh[1]:
            c.font, c.fill = hf, hb
        for i, w in enumerate(widths):
            sh.column_dimensions[openpyxl.utils.get_column_letter(i + 1)].width = w
        sh.freeze_panes = "A2"
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
