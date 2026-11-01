"""
Марж и печалба преди разходи по магазини (Варна + София), от движенията в Мистрал.
  оборот        = продадени бройки × продажна цена (с ДДС)
  себестойност  = продадени бройки × средна доставна цена (с ДДС)
  доставки      = доставки + сторна по доставна цена (с ДДС)
  ревизии       = ± при ревизия по средна доставна цена
  печалба преди разходи = марж + ревизии
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
  SUM(CASE WHEN q.OPERATIONTYPE IN (37, 38) THEN q.QTY * q.AVGDELIVERYPRICE ELSE 0 END) AS revdiff
FROM MATERIALQTYLOG q WITH (NOLOCK) JOIN LOCATION l ON l.ID = q.LOCATIONID
WHERE q.OPERATIONDATE >= %s AND q.OPERATIONDATE < %s
GROUP BY q.LOCATIONID"""


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


def _region(region: str, a: date, b: date) -> list[dict]:
    from . import mistral
    with mistral.use(region), mistral.connect() as conn:
        cur = conn.cursor()
        cur.execute(SQL, (a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d")))
        rows = cur.fetchall()
        cur.execute("SELECT MAX(OPERATIONDATE) AS d FROM MATERIALQTYLOG WITH (NOLOCK) WHERE OPERATIONTYPE = 1")
        last = cur.fetchone()["d"]
    out = []
    for r in rows:
        rev, cogs = float(r["rev"] or 0), float(r["cogs"] or 0)
        if rev < 10:
            continue
        dl, rv = float(r["deliv"] or 0), float(r["revdiff"] or 0)
        out.append({"region": "София" if region == "sofia" else "Варна", "loc": int(r["loc"]), "store": (r["name"] or "").strip(),
                    "rev": round(rev, 2), "cogs": round(cogs, 2), "margin": round(rev - cogs, 2),
                    "margin_pct": round((rev - cogs) / rev * 100, 1), "deliv": round(dl, 2), "revdiff": round(rv, 2),
                    "profit": round(rev - cogs + rv, 2), "profit_net": round((rev - cogs + rv) / VAT, 2),
                    "data_until": last.strftime("%d.%m.%Y %H:%M") if last else None})
    _last[region] = last
    return out


_last: dict = {}


def report(month: str | None = None, d_from: str | None = None, d_to: str | None = None, fresh: bool = False) -> dict:
    a, b, label = _period(month, d_from, d_to)
    key = (a, b)
    if not fresh and key in _cache and time.time() - _cache[key][0] < CACHE_SEC:
        return _cache[key][1]
    rows = []
    for reg in ("varna", "sofia"):
        try:
            rows += _region(reg, a, b)
        except Exception as e:      # единият регион недостъпен - другият пак се показва
            rows.append({"region": reg, "error": f"{type(e).__name__}: {e}"[:200]})
    good = [r for r in rows if "error" not in r]
    tot = {k: round(sum(r[k] for r in good), 2) for k in ("rev", "cogs", "margin", "deliv", "revdiff", "profit", "profit_net")}
    tot["margin_pct"] = round(tot["margin"] / tot["rev"] * 100, 1) if tot["rev"] else 0
    # предходен период със същата дължина - за сравнение
    stale = []
    for reg, name in (("varna", "Варна"), ("sofia", "София")):
        l = _last.get(reg)
        if l is not None and l.date() < min(b - timedelta(days=1), date.today()) - timedelta(days=1):
            stale.append(f"{name}: данните в копието на Мистрал свършват на {l:%d.%m.%Y %H:%M} - периодът е непълен")
    out = {"period": label, "stale": stale, "from": str(a), "to": str(b - timedelta(days=1)), "days": (b - a).days,
           "rows": sorted(good, key=lambda r: -r["rev"]), "total": tot,
           "errors": [r for r in rows if "error" in r]}
    _cache[key] = (time.time(), out)
    return out


def compare(month: str | None = None) -> dict:
    """Месецът срещу предишния (по магазин): оборот и марж %."""
    cur = report(month)
    a = date.fromisoformat(cur["from"])
    pa = date(a.year - (a.month == 1), (a.month - 2) % 12 + 1, 1)
    prev = report(f"{pa.year}-{pa.month:02d}")
    pm = {(r["region"], r["loc"]): r for r in prev["rows"]}
    for r in cur["rows"]:
        p = pm.get((r["region"], r["loc"]))
        if p:
            # оборот на ден, за да е честно при непълен месец
            r["rev_day_prev"] = round(p["rev"] / max(prev["days"], 1), 2)
            r["rev_day"] = round(r["rev"] / max(cur["days"], 1), 2)
            r["rev_change_pct"] = round((r["rev_day"] / r["rev_day_prev"] - 1) * 100, 1) if r["rev_day_prev"] else None
            r["margin_pct_prev"] = p["margin_pct"]
    cur["prev_period"] = prev["period"]
    cur["prev_total"] = prev["total"]
    return cur
