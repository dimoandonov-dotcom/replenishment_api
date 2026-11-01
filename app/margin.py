"""
Марж и печалба преди разходи по магазини (Варна + София), от движенията в Мистрал.
  оборот        = продадени бройки × продажна цена (с ДДС)
  себестойност  = продадени бройки × средна доставна цена (с ДДС)
  доставки      = доставки + сторна по доставна цена (с ДДС)
  ревизии       = ± при ревизия по средна доставна цена
  брак          = бракувано по средна доставна цена
  печалба преди разходи = марж + ревизии + брак
„Без съмнителни цени“: изключени са артикулите, при които себестойността е под 35% или над 150%
от продажната (грешна доставна цена в Мистрал - напр. Кока-Кола 2л от склада на 0.27 €).
Проверено 01.11.2026: доставните цени в Мистрал са с ДДС (фактура НДК × 1.2);
оборотът от движенията се разминава с касовите бележки с ~0.4% (отстъпки по бележка).
"""
from __future__ import annotations

import time
from datetime import date, timedelta

_cache: dict = {}
CACHE_SEC = 1800
VAT = 1.2

SQL = """SELECT q.LOCATIONID AS loc, MAX(l.NAME) AS name,
  SUM(CASE WHEN q.OPERATIONTYPE = 1 THEN -q.QTY * q.PRICE ELSE 0 END) AS rev,
  SUM(CASE WHEN q.OPERATIONTYPE = 1 THEN -q.QTY * q.AVGDELIVERYPRICE ELSE 0 END) AS cogs,
  SUM(CASE WHEN q.OPERATIONTYPE IN (2, 24) THEN q.QTY * q.PRICE ELSE 0 END) AS deliv,
  SUM(CASE WHEN q.OPERATIONTYPE IN (37, 38) THEN q.QTY * q.AVGDELIVERYPRICE ELSE 0 END) AS revdiff,
  SUM(CASE WHEN q.OPERATIONTYPE = 4 THEN q.QTY * q.AVGDELIVERYPRICE ELSE 0 END) AS brak
FROM MATERIALQTYLOG q WITH (NOLOCK) JOIN LOCATION l ON l.ID = q.LOCATIONID
WHERE q.OPERATIONDATE >= %s AND q.OPERATIONDATE < %s
GROUP BY q.LOCATIONID"""

# по артикул: за откриване на грешни доставни цени
SQL_ART = """SELECT q.MATERIALCODE AS mc, MAX(m.MATERIAL) AS name, SUM(-q.QTY) AS qty,
  SUM(-q.QTY * q.PRICE) AS rev, SUM(-q.QTY * q.AVGDELIVERYPRICE) AS cost
FROM MATERIALQTYLOG q WITH (NOLOCK) JOIN MATERIAL m ON m.LOCATIONID = q.LOCATIONID AND m.MATERIALCODE = q.MATERIALCODE
WHERE q.OPERATIONTYPE = 1 AND q.OPERATIONDATE >= %s AND q.OPERATIONDATE < %s
GROUP BY q.MATERIALCODE"""
SQL_SUS = """SELECT q.LOCATIONID AS loc, SUM(-q.QTY * q.PRICE) AS rev, SUM(-q.QTY * q.AVGDELIVERYPRICE) AS cost
FROM MATERIALQTYLOG q WITH (NOLOCK)
WHERE q.OPERATIONTYPE = 1 AND q.OPERATIONDATE >= %s AND q.OPERATIONDATE < %s AND q.MATERIALCODE IN ({codes})
GROUP BY q.LOCATIONID"""
LOW, HIGH, MIN_REV = 0.35, 1.5, 50.0     # себестойност под 35% или над 150% от продажната = съмнителна цена
GENERIC = {6884}                          # „СТОКА БР“ - общ артикул на OCR


def ok_codes(db) -> set[int]:
    """Артикули, за които Димо е казал „цената е вярна“ (напр. торбички)."""
    from sqlalchemy import text
    try:
        db.execute(text("CREATE TABLE IF NOT EXISTS margin_ok (mc INT PRIMARY KEY, by_user TEXT, at TIMESTAMPTZ DEFAULT now())"))
        db.commit()
        return {r[0] for r in db.execute(text("SELECT mc FROM margin_ok")).all()}
    except Exception:
        db.rollback()
        return set()


def set_ok(db, mc: int, ok: bool, user: str | None) -> None:
    from sqlalchemy import text
    ok_codes(db)
    if ok:
        db.execute(text("INSERT INTO margin_ok(mc, by_user) VALUES (:m,:u) ON CONFLICT (mc) DO NOTHING"), {"m": mc, "u": user})
    else:
        db.execute(text("DELETE FROM margin_ok WHERE mc = :m"), {"m": mc})
    db.commit()
    _cache.clear()


def _period(month: str | None, d_from: str | None, d_to: str | None) -> tuple[date, date, str]:
    if d_from and d_to:
        a, b = date.fromisoformat(d_from), date.fromisoformat(d_to) + timedelta(days=1)
        return a, b, f"{a:%d.%m}–{(b - timedelta(days=1)):%d.%m.%Y}"
    today = date.today()
    if not month:
        month = f"{today.year}-{today.month:02d}"
    y, m = map(int, month.split("-"))
    a = date(y, m, 1)
    b = date(y + (m == 12), m % 12 + 1, 1)
    return a, min(b, today + timedelta(days=1)), f"{a:%m.%Y}"


def _region(region: str, a: date, b: date, okset: set[int]) -> tuple[list[dict], list[dict]]:
    from . import mistral
    rn = "София" if region == "sofia" else "Варна"
    p = (a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d"))
    with mistral.use(region), mistral.connect() as conn:
        cur = conn.cursor()
        cur.execute(SQL, p)
        rows = cur.fetchall()
        cur.execute("SELECT MAX(OPERATIONDATE) AS d FROM MATERIALQTYLOG WITH (NOLOCK) WHERE OPERATIONTYPE = 1")
        last = cur.fetchone()["d"]
        cur.execute(SQL_ART, p)
        arts = cur.fetchall()
        sus = []
        for x in arts:
            rv, c, mc = float(x["rev"] or 0), float(x["cost"] or 0), int(x["mc"])
            if mc in okset:
                continue
            if mc in GENERIC or (rv >= MIN_REV and (c < LOW * rv or c > HIGH * rv)) or (rv >= MIN_REV and c <= 0):
                q = float(x["qty"] or 0)
                sus.append({"region": rn, "mc": mc, "name": (x["name"] or "").strip(), "qty": round(q, 2), "rev": round(rv, 2),
                            "cost": round(c, 2), "margin_pct": round((rv - c) / rv * 100, 1) if rv else None,
                            "price": round(rv / q, 3) if q else None, "unit_cost": round(c / q, 3) if q else None,
                            "effect": round(rv - c - 0.18 * rv, 2)})     # колко надува/сваля маржа спрямо ~18%
        per_loc = {}
        if sus:
            cur.execute(SQL_SUS.format(codes=",".join(str(x["mc"]) for x in sus)), p)
            per_loc = {int(r["loc"]): (float(r["rev"] or 0), float(r["cost"] or 0)) for r in cur.fetchall()}
    out = []
    for r in rows:
        rev, cogs = float(r["rev"] or 0), float(r["cogs"] or 0)
        if rev < 10:
            continue
        dl, rv, br = float(r["deliv"] or 0), float(r["revdiff"] or 0), float(r["brak"] or 0)
        sr, sc = per_loc.get(int(r["loc"]), (0.0, 0.0))
        cr, cc = rev - sr, cogs - sc
        out.append({"region": rn, "loc": int(r["loc"]), "store": (r["name"] or "").strip(),
                    "rev": round(rev, 2), "cogs": round(cogs, 2), "margin": round(rev - cogs, 2),
                    "margin_pct": round((rev - cogs) / rev * 100, 1), "deliv": round(dl, 2), "revdiff": round(rv, 2),
                    "brak": round(br, 2),
                    "profit": round(rev - cogs + rv + br, 2), "profit_net": round((rev - cogs + rv + br) / VAT, 2),
                    # без артикулите със съмнителни доставни цени
                    "sus_rev": round(sr, 2), "rev_clean": round(cr, 2), "cogs_clean": round(cc, 2),
                    "margin_clean": round(cr - cc, 2), "margin_clean_pct": round((cr - cc) / cr * 100, 1) if cr else 0,
                    "data_until": last.strftime("%d.%m.%Y %H:%M") if last else None})
    _last[region] = last
    return out, sus


_last: dict = {}


def report(month: str | None = None, d_from: str | None = None, d_to: str | None = None, fresh: bool = False, db=None) -> dict:
    a, b, label = _period(month, d_from, d_to)
    key = (a, b)
    if not fresh and key in _cache and time.time() - _cache[key][0] < CACHE_SEC:
        return _cache[key][1]
    rows, sus = [], []
    okset = ok_codes(db) if db is not None else set()
    for reg in ("varna", "sofia"):
        try:
            r_, s_ = _region(reg, a, b, okset)
            rows += r_
            sus += s_
        except Exception as e:      # единият регион недостъпен - другият пак се показва
            rows.append({"region": reg, "error": f"{type(e).__name__}: {e}"[:200]})
    good = [r for r in rows if "error" not in r]
    tot = {k: round(sum(r[k] for r in good), 2) for k in ("rev", "cogs", "margin", "deliv", "revdiff", "brak", "profit",
                                                          "profit_net", "rev_clean", "cogs_clean", "margin_clean", "sus_rev")}
    tot["margin_pct"] = round(tot["margin"] / tot["rev"] * 100, 1) if tot["rev"] else 0
    tot["margin_clean_pct"] = round(tot["margin_clean"] / tot["rev_clean"] * 100, 1) if tot["rev_clean"] else 0
    # предходен период със същата дължина - за сравнение
    stale = []
    for reg, name in (("varna", "Варна"), ("sofia", "София")):
        l = _last.get(reg)
        if l is not None and l.date() < min(b - timedelta(days=1), date.today()) - timedelta(days=1):
            stale.append(f"{name}: данните в копието на Мистрал свършват на {l:%d.%m.%Y %H:%M} - периодът е непълен")
    out = {"period": label, "stale": stale, "from": str(a), "to": str(b - timedelta(days=1)), "days": (b - a).days,
           "rows": sorted(good, key=lambda r: -r["rev"]), "total": tot,
           "suspicious": sorted(sus, key=lambda x: -abs(x["effect"])),
           "errors": [r for r in rows if "error" in r]}
    _cache[key] = (time.time(), out)
    return out


def compare(month: str | None = None, db=None) -> dict:
    """Месецът срещу предишния (по магазин): оборот и марж %."""
    cur = report(month, db=db)
    a = date.fromisoformat(cur["from"])
    pa = date(a.year - (a.month == 1), (a.month - 2) % 12 + 1, 1)
    prev = report(f"{pa.year}-{pa.month:02d}", db=db)
    pm = {(r["region"], r["loc"]): r for r in prev["rows"]}
    for r in cur["rows"]:
        p = pm.get((r["region"], r["loc"]))
        if p:
            # оборот на ден, за да е честно при непълен месец
            r["rev_day_prev"] = round(p["rev"] / max(prev["days"], 1), 2)
            r["rev_day"] = round(r["rev"] / max(cur["days"], 1), 2)
            r["rev_change_pct"] = round((r["rev_day"] / r["rev_day_prev"] - 1) * 100, 1) if r["rev_day_prev"] else None
            r["margin_pct_prev"] = p["margin_pct"]
            r["margin_clean_pct_prev"] = p.get("margin_clean_pct")
    cur["prev_period"] = prev["period"]
    cur["prev_total"] = prev["total"]
    return cur
