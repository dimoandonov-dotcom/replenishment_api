"""
„Провери списък“: магазинът пише какво иска да добави (Viber текст) - за всеки ред:
стига ли наличността до следващата доставка, която още можем да променим,
при нормален и при силен ден, и колко ще се натрупа, ако се добави.

Плюс cover() - същата сметка за единичната проверка „Провери артикул“.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m, service

SOFIA = ZoneInfo("Europe/Sofia")
WD = ["пон", "вт", "ср", "чет", "пет", "съб", "нед"]
SIZE_WORDS = {250, 330, 440, 500, 700, 750, 1000}
DISTINCT = {"ЗИРО", "БЕЗ", "0%", "ТЪМНО", "ЛЕКО", "РАДЛЕР", "МАЛЦ", "КЕН", "ЧЕРИ", "СИЛВЪР", "РЕЗЕРВА", "ЛАЙТ"}


# ---------------------------------------------------------------------------
# сметка до доставката
# ---------------------------------------------------------------------------

def horizon(db: Session, store_id: int, now: datetime | None = None) -> tuple[float, date | None]:
    """Колко дни продажби трябва да покрие наличността, ако артикулът НЕ влезе в днешната заявка:
    до доставката на СЛЕДВАЩАТА заявка (утрешната или по графика)."""
    now = now or datetime.now(SOFIA)
    today = now.date()
    rows = db.execute(select(m.StoreSupplierSchedule).where(
        m.StoreSupplierSchedule.store_id == store_id, m.StoreSupplierSchedule.supplier_id == 1)).scalars().all()
    by_wd = {r.weekday: r.delivery_weekday for r in rows}
    deliv = None
    for k in range(1, 8):
        d = today + timedelta(days=k)
        if d.isoweekday() in by_wd:
            dw = by_wd[d.isoweekday()] or (d.isoweekday() % 7 + 1)
            gap = (dw - d.isoweekday()) % 7 or 1
            deliv = d + timedelta(days=gap)
            break
    if deliv is None:
        deliv = today + timedelta(days=2)
    rest_today = max(0.0, (23 - now.hour - now.minute / 60) / 15)       # магазинът продава ~08-23
    h = min(rest_today, 1.0) + max(0, (deliv - today).days - 1) + 0.5     # + половин ден до доставката
    return round(h, 2), deliv


def daily_sales(db: Session, store_id: int, codes: list[int], days: int = 14) -> dict[int, list[float]]:
    from . import mistral
    from .anomalies import _loc_map
    if not codes:
        return {}
    with mistral.use("varna"), mistral.connect() as conn:
        cur = conn.cursor()
        locs = _loc_map(db, cur)
        loc = next((l for l, s in locs.items() if s == store_id), None)
        if loc is None:
            return {}
        cur.execute(f"""SELECT MATERIALCODE AS c, DATEDIFF(day, CONVERT(date, OPERATIONDATE), CONVERT(date, GETDATE())) AS ago,
                               -SUM(QTY) AS s
                        FROM MATERIALQTYLOG WITH (NOLOCK)
                        WHERE LOCATIONID = {loc} AND OPERATIONTYPE = 1 AND MATERIALCODE IN ({','.join(map(str, codes))})
                          AND OPERATIONDATE >= DATEADD(day, -{days}, CONVERT(date, GETDATE()))
                          AND OPERATIONDATE < CONVERT(date, GETDATE())
                        GROUP BY MATERIALCODE, DATEDIFF(day, CONVERT(date, OPERATIONDATE), CONVERT(date, GETDATE()))""")
        out: dict[int, list[float]] = {}
        for r in cur.fetchall():
            out.setdefault(int(r["c"]), [0.0] * days)[int(r["ago"]) - 1] = float(r["s"] or 0)
    return out


def _n(x) -> str:
    return (f"{x:g}" if float(x) == int(x) else f"{x:.1f}").replace(".", ",")


def promo_info(db: Session) -> dict:
    """article_id -> (начало, край, коефициент) за промоциите, които вървят днес."""
    from . import promo as _promo
    today = datetime.now(SOFIA).date()
    out = {}
    try:
        fac = _promo.active(db, today)
        for aid, periods in _promo.promo_days(db).items():
            for s_, e_ in periods:
                if s_ <= today <= e_:
                    out[aid] = (s_, e_, float(fac.get(aid, 1.0)))
    except Exception:
        db.rollback()
    return out


def verdict(stock: float | None, in_order: float, series: list[float], h: float, deliv: date | None,
            add: float = 0.0, pack: int = 1, promo: tuple | None = None) -> dict:
    avg = sum(series) / len(series) if series else 0.0
    peak = max(series) if series else 0.0
    promo_note = ""
    if promo:
        s_, e_, f_ = promo
        days_in = min((datetime.now(SOFIA).date() - s_).days, len(series))
        since = sum(series[:days_in]) / days_in if days_in >= 1 else 0.0       # series[0] = вчера
        avg = max(avg * max(f_, 1.0), since)
        peak = max(peak, max(series[:days_in]) if days_in >= 1 else 0.0)
        promo_note = f" 🏷 Промоция до {e_.strftime('%d.%m')} - сметнато с {_n(round(avg, 1))}/ден."
    dn = f"{WD[deliv.weekday()]} {deliv.strftime('%d.%m')}" if deliv else "следващата доставка"
    if stock is None:
        return {"level": "?", "text": "Няма данни за наличността."}
    have = max(stock, 0) + in_order
    need = avg * h
    need_peak = avg * max(h - 1, 0) + peak        # един силен ден в периода
    out = {"avg": round(avg, 2), "peak": peak, "need": round(need, 1), "need_peak": round(need_peak, 1), "have": have}
    if avg <= 0:
        out.update(level="ok", text=f"Не се е продавал 14 дни - {_n(have)} бр. стигат.")
    elif have < need:
        rec = math.ceil(max(need_peak - have, 0) / pack) * pack if pack > 1 else math.ceil(need_peak - have)
        out.update(level="red", rec=rec,
                   text=f"🔴 Няма да стигне до {dn}: има {_n(have)}, нужни ~{_n(round(need))} ({_n(round(avg, 1))}/ден). "
                        f"Добави ~{rec} бр.")
    elif have < need_peak:
        rec = math.ceil(max(need_peak - have, 0) / pack) * pack if pack > 1 else math.ceil(need_peak - have)
        out.update(level="amber", rec=rec,
                   text=f"🟠 На ръба: стига до {dn} при нормален ден (~{_n(round(need))}), "
                        f"но не и при силен ден ({_n(peak)} бр.). За сигурност +{rec} бр.")
    else:
        out.update(level="ok", text=f"🟢 Стига до {dn}: има {_n(have)}, нужни ~{_n(round(need))}"
                                    f" (при силен ден ~{_n(round(need_peak))}).")
    out["text"] += promo_note
    out["avg"] = round(avg, 2)
    if add and avg > 0:
        days = (have + add) / avg
        out["after_days"] = round(days, 1)
        out["text"] += f" С добавените +{_n(add)} ще има за {_n(round(days))} дни" + (" - ще се трупа." if days > 10 else ".")
    elif add and avg <= 0:
        out["text"] += f" Добавените +{_n(add)} ще стоят."
    return out


# ---------------------------------------------------------------------------
# разчитане на текста от Viber
# ---------------------------------------------------------------------------

UNIT = r"(?:к\.?|k\.?|каса|каси|кашона?|стека?|стек\.|ст\.?|бр\.?|броя)"
QTY_RE = re.compile(r"([-–:]\s*|\s|^)(?:по\s*)?(\d+)\s*(" + UNIT + r")?\s*(\(\s*-?\d+\s*\)?)?\s*$", re.I)


def _split(text: str) -> list[str]:
    t = text.replace("\r", "\n")
    t = re.sub(r"\[\s*\d{1,2}\s+\w+\s+\d{4}[^\]]*\][^:\n]*:", "\n", t)        # „[ 03 ноември 2026 г. 9:44 ] М-н X:“
    # „2л.-12бр пиринско кен-12бр“ - след количество с мярка започва нов ред
    t = re.sub(r"(\d\s*(?:бр\.?|к\.|к\b|стека?|каси|каса|кашона?))\s+(?=[^\d\s(,;])", "\\1\n", t, flags=re.I)
    t = re.sub(r":\s+(?=[^\d\s])", "\n", t)                                # „…в наличното: капи…“
    parts = re.split(r"(?<=\))\s*,?\s*|,\s+(?=[^\d\s\-–])|;|\n|\s{2,}|\t", t)
    return [p.strip(" ,.;") for p in parts if p and p.strip(" ,.;")]


def _sizes_l(s: str) -> list[float]:
    out = []
    for num, unit in re.findall(r"(\d+(?:[.,]\d+)?)\s*(МЛ|ML|Л|L)\b", s.upper()):
        v = float(num.replace(",", "."))
        out.append(v / 1000 if unit in ("МЛ", "ML") else v)
    return out


def _q_size(words: list[str]) -> tuple[float | None, list[str]]:
    """Размер от думите на магазина: 0,500 / 0.5 / 330 / 1л / 1,5 / 2л. -> литри."""
    rest, size = [], None
    for w in words:
        x = w.lower().rstrip(".")
        mm = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(мл|л)?\.?", x)
        if mm and size is None:
            v = float(mm.group(1).replace(",", "."))
            if mm.group(2) == "мл" or (mm.group(2) is None and v >= 100):
                v = v / 1000
            size = v
            continue
        rest.append(w)
    return size, rest


def _score(qwords: list[str], qsize: float | None, art: m.Article, sw: float = 1.0) -> float:
    name = (art.name or art.supplier_name or "").upper()
    nwords = re.findall(r"[А-ЯA-Z0-9%]+", name)
    sc = 0.0
    for w in qwords:
        w = w.upper().strip(".")
        if len(w) < 2 or w in ("ПО", "И"):
            continue
        if any(n.startswith(w) or (len(w) >= 5 and n.startswith(w[:5])) for n in nwords):
            sc += 2.0
        else:
            sc -= 1.0
    qset = {w.upper().strip(".") for w in qwords}
    for d in DISTINCT:
        if d in nwords and not any(q.startswith(d[:3]) for q in qset):
            sc -= 1.2                                                   # „кола 1л“ не е „кола ЗИРО 1л“
    sizes = _sizes_l(name)
    if qsize is not None:
        if any(abs(s - qsize) < 0.01 for s in sizes):
            sc += 3.0 * sw
        elif any(qsize == int(qsize) and qsize <= s < qsize + 0.35 for s in sizes):
            sc += 2.0 * sw
        else:
            sc -= 3.0 * sw
    sc -= 0.05 * len(nwords)
    return sc


def parse(db: Session, store_id: int, text: str) -> list[dict]:
    arts = db.execute(select(m.Article).join(m.StoreArticleSetting, m.StoreArticleSetting.article_id == m.Article.id)
                      .where(m.StoreArticleSetting.store_id == store_id)).scalars().all()
    by_sku = {a.sku: a for a in arts}
    all_sku = None
    out, ctx, pending_noqty, ctx_size = [], [], [], None
    for chunk in _split(text):
        mq = QTY_RE.search(chunk)
        qty, unit, name = None, None, chunk
        # количество само ако има тире/двоеточие пред него, мерна единица, „по“ или „(наличност)“ след него
        if mq and mq.start() > 0 and (mq.group(1).strip() or mq.group(3) or mq.group(4)
                                      or re.search(r"\bпо\s*\d+\s*$", chunk, re.I)):
            qty, unit = int(mq.group(2)), (mq.group(3) or "").lower()
            name = chunk[:mq.start()]
        name = re.sub(r"\(\s*-?\d+\s*\)", " ", name)
        name = re.sub(r"[-–:]+\s*$", "", name).strip(" -–:,.")
        if not name:
            continue
        # код
        code = None
        mc = re.match(r"^(\d{3,6})\b", name) or re.match(r"^[^\d]*?\s(\d{4,6})$", name)   # „добавям 1645“
        if mc and int(mc.group(1)) not in SIZE_WORDS:
            code = mc.group(1)
        words = [w for w in re.split(r"[\s\-–/]+", name) if w]
        art, alt, score, near = None, [], None, []
        if code:
            art = by_sku.get(code)
            if art is None:
                if all_sku is None:
                    all_sku = {a.sku: a for a in db.execute(select(m.Article)).scalars().all()}
                art = all_sku.get(code)
            score = 99.0 if art else None
            ctx, ctx_size = [], None
        else:
            size, rest = _q_size(words)
            has_can = any(w.lower().strip(".") == "кен" for w in rest)
            letters = [w for w in rest if re.search(r"[а-яa-z]{2,}", w.lower()) and w.lower().strip(".") not in ("кен", "мл", "л")]
            pick = None
            if letters:
                sz, sw = (size, 1.0) if size is not None else ((None, 0) if has_can else (ctx_size, 0.4))   # „б. лимон“ след „швепс 1,250 …“
                ranked = sorted(((_score(rest, sz, a, sw), a) for a in arts), key=lambda x: -x[0])
                if ranked and ranked[0][0] > 0:
                    nm = (ranked[0][1].name or "").upper()
                    if any(len(w.strip(".")) >= 3 and w.strip(".").upper()[:5] in nm for w in letters):
                        pick = (ranked, rest)
            if pick is None and ctx:
                qw = ctx + [w for w in rest if w not in ctx]                    # „зиро 1 л.“ след „кола …“
                ranked = sorted(((_score(qw, size, a), a) for a in arts), key=lambda x: -x[0])
                if ranked and ranked[0][0] > 0:
                    pick = (ranked, qw)
            if pick is None and letters:
                ranked = sorted(((_score(rest, size, a), a) for a in arts), key=lambda x: -x[0])
                if ranked and ranked[0][0] > 0:
                    pick = (ranked, rest)
            if pick:
                ranked, qw = pick
                score, art = ranked[0]
                # без размер („бургаско кен“) - ако има почти равни, после избира най-продаваният;
                # само сред същата марка и само ако размерът не е наследен от предния ред
                words_ = [w.strip(".").upper()[:5] for w in letters if len(w.strip(".")) >= 3]
                near = ([a for s_, a in ranked[1:4] if s_ > 0 and s_ >= score - 0.6
                         and all(w in (a.name or "").upper() for w in words_)]
                        if size is None and (has_can or ctx_size is None or qw is not rest) else [])
                alt = [a.name or a.supplier_name for s_, a in ranked[1:3] if s_ > 0 and s_ >= score - 1.0]
                nm = (art.name or art.supplier_name or "").upper()
                keep = [w for w in ctx if len(w.strip(".")) >= 3 and w.strip(".").upper()[:5] in nm]   # „кола“ + „зиро“
                ctx = keep + [w for w in qw if re.search(r"[а-яa-z]{2,}", w.lower()) and w not in keep]
                if size is not None:
                    ctx_size = size
        pieces = None
        if qty is not None:
            pk = max(int(art.pack_size or 1), 1) if art else 1
            pieces = qty * pk if unit and (unit[0] in "кk" or unit.startswith("ст")) else qty
        row = {"text": chunk, "sku": art.sku if art else None, "name": (art.name or art.supplier_name) if art else None,
               "add": pieces, "alt": alt, "art_id": art.id if art else None, "pack": max(int(art.pack_size or 1), 1) if art else 1,
               "_near": [art] + near if art and near else [], "_qty": qty, "_unit": unit}
        if art is None and qty is None:
            continue                                                    # „остава и добавям“ - не е артикул
        if qty is None:
            pending_noqty.append(row)
        elif pending_noqty and re.search(r"\bпо\s*\d", chunk, re.I):
            for r in pending_noqty:                                     # „крадец ябълки, вишни по 12бр“
                r["add"] = qty
            pending_noqty = []
        else:
            pending_noqty = []
        out.append(row)
    return out


def check_list(db: Session, store_id: int, text: str) -> dict:
    st = db.get(m.Store, store_id)
    rows = parse(db, store_id, text)
    h, deliv = horizon(db, store_id)
    stock = service.latest_stock_map(db, store_id)
    today = datetime.now(SOFIA).date()
    try:
        res = service.calculate_for_store(db, store_id, today, None, False)
        lines = {ln.article_id: ln for ln in res.lines}
    except Exception:
        db.rollback()
        lines = {}
    codes = {int(r["sku"]) for r in rows if r["sku"] and r["sku"].isdigit()}
    codes |= {int(a.sku) for r in rows for a in r.get("_near", []) if a.sku.isdigit()}
    ser = daily_sales(db, store_id, sorted(codes))
    pinfo = promo_info(db)
    for r in rows:                      # „бургаско кен“ без размер -> този, който се продава най-много
        near = r.pop("_near", [])
        if len(near) > 1:
            best = max(near, key=lambda a: sum(ser.get(int(a.sku), [0])) if a.sku.isdigit() else 0)
            if best.sku != r["sku"]:
                r["alt"] = [r["name"]] + [x for x in r["alt"] if x != (best.name or best.supplier_name)]
                pk = max(int(best.pack_size or 1), 1)
                r.update(sku=best.sku, name=best.name or best.supplier_name, art_id=best.id, pack=pk)
                if r.get("_qty") is not None and r.get("_unit") and (r["_unit"][0] in "кk" or r["_unit"].startswith("ст")):
                    r["add"] = r["_qty"] * pk
        r.pop("_qty", None); r.pop("_unit", None)
    for r in rows:
        if not r["sku"]:
            r.update(level="?", why="Не намерих артикула - напиши кода.")
            continue
        ln = lines.get(r["art_id"])
        r["in_order"] = int(ln.ordered_quantity) if ln is not None else 0
        v = verdict(stock.get(r["art_id"]), r["in_order"], ser.get(int(r["sku"]), [0.0] * 14), h, deliv,
                    float(r["add"] or 0), r["pack"], pinfo.get(r["art_id"]))
        r.update(level=v["level"], why=v["text"], stock=stock.get(r["art_id"]), avg=v.get("avg"), peak=v.get("peak"),
                 need=v.get("need"), rec=v.get("rec"), after_days=v.get("after_days"))
        r.pop("art_id", None)
    order = {"red": 0, "amber": 1, "?": 2, "ok": 3}
    rows.sort(key=lambda r: (order.get(r["level"], 9), -(r.get("after_days") or 0)))
    return {"store": st.name if st else "", "horizon_days": h,
            "delivery": f"{WD[deliv.weekday()]} {deliv.strftime('%d.%m')}" if deliv else None,
            "rows": rows,
            "summary": {k: sum(1 for r in rows if r["level"] == k) for k in ("red", "amber", "ok", "?")},
            "piles": sum(1 for r in rows if (r.get("after_days") or 0) > 10)}
