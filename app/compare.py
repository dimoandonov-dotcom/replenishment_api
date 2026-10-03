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


def _fresh_stock(db: Session, store_id: int | None) -> datetime | None:
    """Свежи наличности от Мистрал за ТОЗИ обект в същата секунда."""
    if store_id is not None:
        try:
            from . import mistral
            if mistral.configured():
                mistral.sync_stock(db, only_store_id=store_id)
        except Exception:
            pass  # сравняваме по последните налични данни
    return db.execute(
        select(func.max(m.StockSnapshot.captured_at))
        .where(m.StockSnapshot.store_id == store_id)
    ).scalar() if store_id is not None else None


def record(db: Session, store_raw: str, lines: list[dict], raw_text: str | None,
           source: str = "anindk", made_at: datetime | None = None) -> dict:
    """lines: [{sku, name, qty}] - заявката на магазина в бройки.
    made_at - кога Ани е направила заявката (при закъсняло копие от опашката)."""
    store_id = resolve_store(db, store_raw)
    stock_at = _fresh_stock(db, store_id)
    order = m.ManualOrder(store_id=store_id, store_raw=store_raw.strip(),
                          raw_text=(raw_text or "")[:20000] or None,
                          source=source, stock_at=stock_at)
    if made_at is not None:
        if made_at.tzinfo is None:
            made_at = made_at.replace(tzinfo=timezone.utc)
        if timedelta(0) <= datetime.now(timezone.utc) - made_at <= timedelta(days=3):
            order.received_at = made_at
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
                "active": a.is_active, "no_order": a.no_order,
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
        if c.get("plano") is False:
            return "само магазин — няма „да“ в планограмата"
        if c.get("no_order"):
            return "само магазин — артикулът не се поръчва"
        if c.get("active") is False:
            return "само магазин — артикулът е спрян"
        if c.get("max") == 0:
            return "само магазин — спрян 0-0"
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

def _off(l) -> bool:
    """Редът на магазина е за артикул без „да“ в планограмата на този магазин."""
    n = l.note or ""
    return "планограма" in n


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

    rows, tot = [], {"orders": 0, "both": 0, "only_store": 0, "only_api": 0, "off_plano": 0,
                     "same_qty": 0, "store_units": 0.0, "api_units": 0.0,
                     "store_value": 0.0, "api_value": 0.0}
    for o in orders:
        ls = by_order.get(o.id, [])
        st = {
            "both": sum(1 for l in ls if l.store_qty > 0 and l.api_qty > 0),
            "only_store": sum(1 for l in ls if l.store_qty > 0 and l.api_qty == 0 and not _off(l)),
            "off_plano": sum(1 for l in ls if l.store_qty > 0 and _off(l)),
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


def _series(db: Session, store_id: int, day) -> dict[str, list[float]]:
    """Продажби ден по ден за 14-те дни преди заявката: sku -> [14 стойности]."""
    start = day - timedelta(days=14)
    out: dict[str, list[float]] = {}
    for sku, d, q in db.execute(
        select(m.Article.sku, m.SalesHistory.sale_date, func.sum(m.SalesHistory.quantity_sold))
        .join(m.Article, m.Article.id == m.SalesHistory.article_id)
        .where(m.SalesHistory.store_id == store_id,
               m.SalesHistory.sale_date >= start, m.SalesHistory.sale_date < day)
        .group_by(m.Article.sku, m.SalesHistory.sale_date)
    ).all():
        i = (d - start).days
        if 0 <= i < 14:
            out.setdefault(sku, [0.0] * 14)[i] = max(float(q or 0), 0.0)
    return out


def _fmt(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".") if x is not None else "—"


def explain(l, series: list[float] | None) -> dict:
    """Обяснение с думи + оценка по продажбите кой е по-близо до търсенето."""
    sq, aq = float(l.store_qty), float(l.api_qty)
    st = float(l.stock) if l.stock is not None else None
    mn = float(l.min_stock) if l.min_stock is not None else None
    mx = float(l.max_stock) if l.max_stock is not None else None
    ser = series or [0.0] * 14
    sold = sum(ser)
    sdp = sold / 14
    last7, prev7 = sum(ser[7:]), sum(ser[:7])
    zero_days = sum(1 for v in ser if v == 0)
    note = l.note or ""
    parts = []

    def cover(q):
        return None if sdp <= 0 or q is None else q / sdp

    base = f"Продава {_fmt(sdp)} бр./ден ({_fmt(sold)} бр. за 14 дни)" if sold > 0 else "Не е продаван нито веднъж за 14 дни"
    if sold > 0 and prev7 > 0 and last7 > prev7 * 1.3:
        base += ", продажбите растат"
    elif sold > 0 and last7 < prev7 * 0.7:
        base += ", продажбите падат"
    parts.append(base + ".")
    c_now = cover(st)
    if st is not None:
        parts.append(f"Наличност {_fmt(st)} бр." + (f" ≈ {_fmt(c_now)} дни продажби." if c_now is not None else ""))

    if note.startswith("и двамата") and abs(sq - aq) < 0.01:
        parts.append(f"И двамата поръчват {_fmt(sq)} бр. — пълно съвпадение.")
    elif note == "различно количество":
        parts.append(f"Ани поръчва {_fmt(sq)} бр., MinMaxAI {_fmt(aq)} бр. (допълва до макс {_fmt(mx)} в цели опаковки).")
    elif note.startswith("само магазин"):
        if "над мин" in note:
            parts.append(f"Ани поръчва {_fmt(sq)} бр., но наличността е над минимума {_fmt(mn)} — MinMaxAI още не поръчва.")
            if c_now is not None and c_now < 2:
                parts.append("Внимание: наличността стига за под 2 дни — минимумът изглежда нисък; ученето ще го вдигне, ако продажбите го потвърдят.")
        elif "планограма" in note:
            parts.append(f"Ани поръчва {_fmt(sq)} бр., но в планограмата няма „да“ за този артикул в този магазин — по правилата не се поръчва."
                         + (f" Продава {_fmt(sdp)} бр./ден — ако трябва да се зарежда, сложете „да“ в планограмата." if sdp >= 0.5 else ""))
        elif "не се поръчва" in note:
            parts.append(f"Ани поръчва {_fmt(sq)} бр., но артикулът е отбелязан „Не се поръчва“ в асортимента — MinMaxAI не го поръчва.")
        elif "0-0" in note or "спрян" in note:
            parts.append(f"Ани поръчва {_fmt(sq)} бр., но артикулът е спрян (0-0) по правилата."
                         + (" Продава се над 1 бр./ден — спирането да се преразгледа." if sdp >= 1 else ""))
        elif "няма мин/макс" in note:
            parts.append(f"Ани поръчва {_fmt(sq)} бр., но позицията няма мин/макс — MinMaxAI не я поръчва; ученето ще я настрои, щом започне да се продава.")
        else:
            parts.append(f"Ани поръчва {_fmt(sq)} бр.; MinMaxAI не поръчва.")
    elif note.startswith("само MinMaxAI"):
        parts.append(f"MinMaxAI поръчва {_fmt(aq)} бр., защото наличността е под минимума {_fmt(mn)}; Ани не го е поръчала.")
        if c_now is not None and c_now < 2:
            parts.append("Рискът да свърши преди следващата доставка е висок.")
    elif note:
        parts.append(note)

    # оценка: след доставка запасът трябва да е между 2 дни и (дни покритие + 2)
    verdict = "равни" if abs(sq - aq) < 0.01 else "неясно"
    if "планограма" in note:
        return {"text": " ".join(parts), "verdict": "планограма", "sdp": round(sdp, 2),
                "sold_14d": round(sold, 1), "zero_days": zero_days}
    if abs(sq - aq) >= 0.01 and st is not None:
        if sdp <= 0:
            verdict = "Ани" if sq < aq else "MinMaxAI"
            parts.append("Без продажби — по-малката поръчка е по-правилна.")
        else:
            hi = 16 if sdp < 0.3 else 7 if sdp < 1 else 5 if sdp < 3 else 4
            def dist(q):
                c = (st + q) / sdp
                return 0 if 2 <= c <= hi else (2 - c if c < 2 else c - hi)
            ds, da = dist(sq), dist(aq)
            cs, ca = (st + sq) / sdp, (st + aq) / sdp
            parts.append(f"След доставка: по Ани ≈ {_fmt(cs)} дни запас, по MinMaxAI ≈ {_fmt(ca)} дни (разумно: 2–{hi} дни).")
            verdict = "равни" if abs(ds - da) < 0.5 else ("MinMaxAI" if da < ds else "Ани")
    return {"text": " ".join(parts), "verdict": verdict, "sdp": round(sdp, 2),
            "sold_14d": round(sold, 1), "zero_days": zero_days}


def detail(db: Session, order_id: int) -> dict:
    o = db.get(m.ManualOrder, order_id)
    if o is None:
        return {}
    store = db.get(m.Store, o.store_id) if o.store_id else None
    ls = db.execute(
        select(m.ManualOrderLine).where(m.ManualOrderLine.order_id == order_id)
    ).scalars().all()
    day = o.received_at.astimezone(SOFIA).date()
    series = _series(db, o.store_id, day) if o.store_id else {}
    now_stock = {}
    if o.store_id:
        idmap = {a.id: a.sku for a in db.execute(select(m.Article)).scalars().all()}
        now_stock = {idmap[k]: v for k, v in service.latest_stock_map(db, o.store_id).items() if k in idmap}
    days = [(day - timedelta(days=14 - i)).strftime("%d.%m") for i in range(14)]
    out = []
    for l in ls:
        ex = explain(l, series.get(l.sku))
        out.append({
            "sku": l.sku, "name": l.name,
            "store_qty": float(l.store_qty), "api_qty": float(l.api_qty),
            "diff": float(l.api_qty) - float(l.store_qty),
            "stock": float(l.stock) if l.stock is not None else None,
            "stock_now": float(now_stock[l.sku]) if l.sku in now_stock else None,
            "min": float(l.min_stock) if l.min_stock is not None else None,
            "max": float(l.max_stock) if l.max_stock is not None else None,
            "pack": l.pack_size, "price": float(l.price) if l.price is not None else None,
            "sales_per_day": ex["sdp"], "note": l.note,
            "series": series.get(l.sku, [0.0] * 14),
            "explain": ex["text"], "verdict": ex["verdict"],
        })
    out.sort(key=lambda r: (abs(r["diff"]) < 0.01, -abs(r["diff"]), r["name"] or ""))
    verdicts = {}
    for r in out:
        if abs(r["diff"]) >= 0.01:
            verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1  # "планограма" се брои отделно
    return {
        "id": o.id, "store": store.name if store else o.store_raw,
        "at": o.received_at.astimezone(SOFIA).strftime("%d.%m.%Y %H:%M"),
        "stock_at": o.stock_at.astimezone(SOFIA).strftime("%d.%m.%Y %H:%M") if o.stock_at else None,
        "raw_text": o.raw_text, "days": days, "verdicts": verdicts, "lines": out,
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
           "Само MinMaxAI", "Без „да“ в планограмата", "Еднакво к-во", "Бройки магазин", "Бройки MinMaxAI",
           "Стойност магазин €", "Стойност MinMaxAI €"]
    ws.append(hdr)
    for r in s["orders"]:
        ws.append([r["store"], r["at"], r["stock_at"], r["both"], r["only_store"],
                   r["only_api"], r["off_plano"], r["same_qty"], r["store_units"], r["api_units"],
                   r["store_value"], r["api_value"]])
    d2 = wb.create_sheet("Всички редове")
    d2.append(["Магазин", "Получена", "Код", "Артикул", "Магазин поръча", "MinMaxAI",
               "Разлика", "Наличност при заявката", "Наличност сега", "Мин", "Макс", "Опак.", "Продажби/ден",
               "Цена €", "Коментар", "По-близо до продажбите", "Обяснение"])
    for r in s["orders"]:
        d = detail(db, r["id"])
        for l in d["lines"]:
            d2.append([d["store"], d["at"], int(l["sku"]) if l["sku"].isdigit() else l["sku"],
                       l["name"], l["store_qty"], l["api_qty"], l["diff"], l["stock"], l["stock_now"],
                       l["min"], l["max"], l["pack"], l["sales_per_day"], l["price"],
                       l["note"], l["verdict"], l["explain"]])
    hf = Font(bold=True, color="FFFFFF")
    hb = PatternFill("solid", fgColor="2F6358")
    for sh, widths in ((ws, [30, 17, 14, 12, 12, 13, 12, 13, 14, 15, 16]),
                       (d2, [28, 17, 9, 44, 12, 11, 9, 14, 12, 7, 7, 7, 12, 9, 36, 16, 90])):
        for c in sh[1]:
            c.font, c.fill = hf, hb
        for i, w in enumerate(widths):
            sh.column_dimensions[openpyxl.utils.get_column_letter(i + 1)].width = w
        sh.freeze_panes = "A2"
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def order_xlsx(db: Session, order_id: int, side: str) -> tuple[str, bytes] | None:
    """Excel в бланката на НДК: side='store' (заявката на Ани) или 'api' (нашата)."""
    from .exports import _write_workbook, order_filename
    o = db.get(m.ManualOrder, order_id)
    if o is None:
        return None
    ls = db.execute(
        select(m.ManualOrderLine).where(m.ManualOrderLine.order_id == order_id)
    ).scalars().all()
    rows = [(l.sku, l.name or "", l.store_qty if side == "store" else l.api_qty)
            for l in ls if (l.store_qty if side == "store" else l.api_qty) > 0]
    store = db.get(m.Store, o.store_id) if o.store_id else None
    base = order_filename(store)[:-5] if store else o.store_raw
    when = o.received_at.astimezone(SOFIA).strftime("%d.%m %H.%M")
    tag = "магазин" if side == "store" else "MinMaxAI"
    return f"{base} {when} {tag}.xlsx", _write_workbook(rows)


def explain_order_line(l, series: list[float] | None) -> str:
    """Защо MinMaxAI поръчва точно толкова (за раздел „Заявки по магазин")."""
    ser = series or [0.0] * 14
    sold = sum(ser)
    sdp = sold / 14
    last7, prev7 = sum(ser[7:]), sum(ser[:7])
    st, mn, mx = float(l.current_stock), float(l.min_stock), float(l.max_stock)
    pack, q = int(l.pack_size or 1), int(l.ordered_quantity)
    need = max(mx - st, 0)
    parts = []
    if sold > 0:
        t = f"Продава {_fmt(sdp)} бр./ден ({_fmt(sold)} бр. за 14 дни)"
        if prev7 > 0 and last7 > prev7 * 1.3:
            t += ", продажбите растат"
        elif last7 < prev7 * 0.7:
            t += ", продажбите падат"
        parts.append(t + ".")
    else:
        parts.append("Не е продаван за последните 14 дни — поръчва се само по минимума.")
    cov = f" ≈ {_fmt(st / sdp)} дни продажби" if sdp > 0 and st > 0 else ""
    if st < 0:
        parts.append(f"Наличността е на минус ({_fmt(st)}) — вероятно доставка, която не е заведена в Мистрал; системата допълва от минуса.")
    else:
        parts.append(f"Наличност {_fmt(st)} бр.{cov}, под минимума {_fmt(mn)} — затова се поръчва.")
    parts.append(f"До максимума {_fmt(mx)} липсват {_fmt(need)} бр.")
    if pack > 1:
        packs = need / pack
        rnd = q // pack
        rule = ("до X.5 се закръгля надолу" if packs - int(packs) <= 0.5 else "над X.5 се закръгля нагоре")
        if packs < 1 and rnd == 1:
            rule = "под минимума винаги поне 1 опаковка"
        parts.append(f"Това са {_fmt(round(packs, 2))} опаковки по {pack} бр. → {rule} → {rnd} опак. = {q} бр.")
    else:
        parts.append(f"Поръчва се на брой: {q} бр.")
    if sdp > 0:
        after = (st + q) / sdp
        parts.append(f"След доставка ≈ {_fmt(after)} дни запас.")
        if after < 2:
            parts.append("⚠️ Максимумът е нисък спрямо продажбите — стига за под 2 дни; нощното учене ще го вдигне (ако не е заключен 🔒).")
        elif after > 21:
            parts.append("⚠️ Запасът ще е голям спрямо продажбите (над 3 седмици) — максимумът може да се намали.")
    return " ".join(parts)


def order_explain(db: Session, store_id: int) -> dict:
    today = datetime.now(SOFIA).date()
    res = service.calculate_for_store(db, store_id, today, None, False)
    series = _series(db, store_id, today)
    days = [(today - timedelta(days=14 - i)).strftime("%d.%m") for i in range(14)]
    return {"days": days, "items": {
        l.sku: {"series": series.get(l.sku, [0.0] * 14), "text": explain_order_line(l, series.get(l.sku))}
        for l in res.lines}}
