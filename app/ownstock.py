"""
„Наша наличност“ - водим я сами, независимо от числото в Мистрал:
    последна ревизия (преброено) + доставки - продажби (± сторна, брак)
Ревизията нулира натрупаните грешки (OCR на НДК фактури), касата е точна.

Освен това: „фантомна наличност“ - артикул, който се продава всеки ден, изведнъж спре
да се продава, а Мистрал показва стока -> най-вероятно реално е 0.

Ползва се за пилотните магазини: заявката взема по-малката от Мистрал/наша,
а при фантом - 0. Всичко се вижда в „Заявки по магазин“ (бутон „🧮 Наличност“).
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import models as m

TZ = ZoneInfo("Europe/Sofia")
BASE_MAX_DAYS = 45          # ревизия, по-стара от това, не е основа
PHANTOM_MIN_PER_DAY = 0.4   # под това продажбите са твърде редки, за да съдим по тишина
_cache: dict[int, tuple[float, dict]] = {}
CACHE_SEC = 600


def _codes_for_store(db: Session, store_id: int) -> dict[int, int]:
    """MATERIALCODE -> article_id за артикулите от планограмата + настройките на магазина."""
    rows = db.execute(text("""SELECT DISTINCT a.sku, a.id FROM articles a
                              LEFT JOIN planogram p ON p.article_id = a.id AND p.store_id = :s
                              LEFT JOIN store_article_settings st ON st.article_id = a.id AND st.store_id = :s
                              WHERE (p.article_id IS NOT NULL OR st.article_id IS NOT NULL)"""), {"s": store_id}).all()
    return {int(k): i for k, i in rows if str(k).isdigit()}


def compute(db: Session, store_id: int, fresh: bool = False) -> dict:
    """Пълната сметка за магазина. Кешира се 10 мин."""
    now = time.time()
    if not fresh and store_id in _cache and now - _cache[store_id][0] < CACHE_SEC:
        return _cache[store_id][1]
    from . import mistral
    from .anomalies import _loc_map
    codes = _codes_for_store(db, store_id)
    out = {"store_id": store_id, "at": datetime.now(TZ).strftime("%d.%m %H:%M"), "items": {}, "loc": None}
    if not codes:
        return out
    with mistral.use("varna"), mistral.connect() as conn:
        cur = conn.cursor()
        locs = _loc_map(db, cur)
        loc = next((l for l, s in locs.items() if s == store_id), None)
        if loc is None:
            return out
        out["loc"] = loc
        cl = ",".join(map(str, codes))
        # 1) последна завършена ревизия на всеки артикул (преброено количество)
        cur.execute(f"""SELECT x.MATERIALCODE AS code, x.QTYFOUND AS found, x.DATEINVENTORY AS d, x.INVENTORYNUM AS num, x.PARTNER AS partner
            FROM (SELECT c.MATERIALCODE, c.QTYFOUND, i.DATEINVENTORY, i.INVENTORYNUM, i.PARTNER,
                         ROW_NUMBER() OVER (PARTITION BY c.MATERIALCODE ORDER BY i.DATEINVENTORY DESC) rn
                  FROM INVENTORYCONTENT c WITH (NOLOCK) JOIN INVENTORY i WITH (NOLOCK)
                    ON i.LOCATIONID = c.LOCATIONID AND i.INVENTORYNUM = c.INVENTORYNUM
                  WHERE c.LOCATIONID = {loc} AND c.MATERIALCODE IN ({cl})
                    AND i.CHECKEDMATERIALCOUNT >= i.MATERIALCOUNT AND i.MATERIALCOUNT > 0
                    AND i.DATEINVENTORY >= DATEADD(day, -{BASE_MAX_DAYS}, GETDATE())) x WHERE x.rn = 1""")
        base = {int(r["code"]): r for r in cur.fetchall()}
        # 2) движения след ревизията (без самите ревизии 37/38)
        mov: dict[int, dict] = {}
        if base:
            vals = ",".join(f"({c},'{r['d'].strftime('%Y-%m-%d %H:%M:%S')}')" for c, r in base.items())
            cur.execute(f"""SELECT q.MATERIALCODE AS code, q.OPERATIONTYPE AS t, SUM(q.QTY) AS qty, COUNT(*) AS n
                FROM MATERIALQTYLOG q WITH (NOLOCK) JOIN (VALUES {vals}) v(code, t0) ON v.code = q.MATERIALCODE
                WHERE q.LOCATIONID = {loc} AND q.OPERATIONDATE > v.t0 AND q.OPERATIONTYPE NOT IN (37, 38)
                GROUP BY q.MATERIALCODE, q.OPERATIONTYPE""")
            for r in cur.fetchall():
                mov.setdefault(int(r["code"]), {})[int(r["t"])] = float(r["qty"] or 0)
        # 3) продажби 28 дни и последна продажба; наличност в Мистрал
        cur.execute(f"""SELECT MATERIALCODE AS code, SUM(-QTY) AS sold, MAX(OPERATIONDATE) AS last_sale,
                               COUNT(DISTINCT CONVERT(date, OPERATIONDATE)) AS days
                        FROM MATERIALQTYLOG WITH (NOLOCK) WHERE LOCATIONID = {loc} AND OPERATIONTYPE = 1
                          AND MATERIALCODE IN ({cl}) AND OPERATIONDATE >= DATEADD(day, -28, GETDATE())
                        GROUP BY MATERIALCODE""")
        sales = {int(r["code"]): r for r in cur.fetchall()}
        cur.execute(f"SELECT MATERIALCODE AS code, QTY FROM MATERIAL WITH (NOLOCK) WHERE LOCATIONID = {loc} AND MATERIALCODE IN ({cl})")
        mq = {int(r["code"]): float(r["QTY"] or 0) for r in cur.fetchall()}
        # дни с продажби (28 дни) - за обичайната пауза между продажбите
        cur.execute(f"""SELECT MATERIALCODE AS code, CONVERT(date, OPERATIONDATE) AS d FROM MATERIALQTYLOG WITH (NOLOCK)
                        WHERE LOCATIONID = {loc} AND OPERATIONTYPE = 1 AND MATERIALCODE IN ({cl})
                          AND OPERATIONDATE >= DATEADD(day, -28, GETDATE()) GROUP BY MATERIALCODE, CONVERT(date, OPERATIONDATE)""")
        sdays: dict[int, list] = {}
        for r in cur.fetchall():
            sdays.setdefault(int(r["code"]), []).append(r["d"])
        # последна ревизия изобщо (и непълна) - скорошно броене бие „тишината“
        cur.execute(f"""SELECT c.MATERIALCODE AS code, MAX(i.DATEINVENTORY) AS d FROM INVENTORYCONTENT c WITH (NOLOCK)
                        JOIN INVENTORY i WITH (NOLOCK) ON i.LOCATIONID = c.LOCATIONID AND i.INVENTORYNUM = c.INVENTORYNUM
                        WHERE c.LOCATIONID = {loc} AND c.MATERIALCODE IN ({cl}) AND c.QTYFOUND > 0
                          AND i.DATEINVENTORY >= DATEADD(day, -7, GETDATE()) GROUP BY c.MATERIALCODE""")
        counted7 = {int(r["code"]): r["d"] for r in cur.fetchall()}
        # последна доставка (за фантома: тишина след доставка е още по-подозрителна)
        cur.execute(f"""SELECT MATERIALCODE AS code, MAX(OPERATIONDATE) AS d FROM MATERIALQTYLOG WITH (NOLOCK)
                        WHERE LOCATIONID = {loc} AND OPERATIONTYPE = 2 AND MATERIALCODE IN ({cl})
                          AND OPERATIONDATE >= DATEADD(day, -60, GETDATE()) GROUP BY MATERIALCODE""")
        lastd = {int(r["code"]): r["d"] for r in cur.fetchall()}

    names = {a.id: a.name for a in db.execute(select(m.Article).where(m.Article.id.in_(list(codes.values())))).scalars()}
    try:   # поправки по е-фактурите на НДК (фактура - осчетоводено от OCR)
        from . import ndkinv
        fixes = ndkinv.corrections(db, store_id)
    except Exception:
        db.rollback()
        fixes = []
    now_dt = datetime.now()
    for code, aid in codes.items():
        s = sales.get(code)
        sold = float(s["sold"] or 0) if s else 0.0
        per_day = sold / 28.0
        last_sale = s["last_sale"] if s else None
        silence = (now_dt - last_sale).total_seconds() / 86400 if last_sale else None
        it = {"sku": str(code), "name": names.get(aid, ""), "mistral": mq.get(code, 0.0), "per_day": round(per_day, 2),
              "last_sale": last_sale.strftime("%d.%m") if last_sale else None,
              "last_delivery": lastd[code].strftime("%d.%m") if code in lastd else None,
              "base": None, "base_date": None, "deliveries": 0.0, "sales": 0.0, "other": 0.0, "own": None,
              "phantom": False, "effective": mq.get(code, 0.0), "flag": "", "why": ""}
        b = base.get(code)
        t0 = b["d"] if b is not None else None
        fx = [f for f in fixes if f["mc"] == code and (t0 is None or (f["at"] and f["at"].replace(tzinfo=None) > t0))]
        it["ndk_fix"] = round(sum(f["delta"] for f in fx), 2)
        it["ndk_fix_docs"] = sorted({f["num"] for f in fx})
        if b is not None:
            mv = mov.get(code, {})
            dl = mv.get(2, 0.0)
            sl = -mv.get(1, 0.0)
            other = sum(q for t, q in mv.items() if t not in (1, 2))
            own = float(b["found"] or 0) + dl - sl + other + it["ndk_fix"]
            it.update({"base": float(b["found"] or 0), "base_date": b["d"].strftime("%d.%m"),
                       "base_ndk": bool((b["partner"] or "").strip()), "deliveries": dl, "sales": sl, "other": other,
                       "own": round(own, 2)})
        # фантом: продава се редовно, но мълчи много по-дълго от обичайната си пауза, а има „стока“;
        # скорошно броене (до 7 дни, с намерено > 0) бие тишината
        stock_now = it["own"] if it["own"] is not None else it["mistral"]
        days = sorted(sdays.get(code, []))
        if per_day >= PHANTOM_MIN_PER_DAY and silence is not None and stock_now > 0 and len(days) >= 4 and code not in counted7:
            gaps = [(b - a).days for a, b in zip(days, days[1:])]
            typical = max(gaps) if gaps else 1
            need = max(4.0, 1.5 * typical)
            if silence >= need:
                it["phantom"] = True
                it["flag"] = "🕳"
                it["why"] = (f"продава {per_day:.1f}/ден (обичайна пауза до {typical} дни), а няма продажба от {silence:.0f} дни "
                             f"при „наличност“ {stock_now:g} - вероятно е 0")
        it["counted_recently"] = counted7[code].strftime("%d.%m") if code in counted7 else None
        if it["phantom"]:
            it["effective"] = 0.0
        elif it["own"] is None and it["ndk_fix"]:
            it["own"] = round(it["mistral"] + it["ndk_fix"], 2)
            it["effective"] = max(it["own"], 0.0)
            it["flag"] = "📄"
            it["why"] = f"Мистрал {it['mistral']:g} {it['ndk_fix']:+g} по фактура {', '.join(it['ndk_fix_docs'])} = {it['own']:g}"
        if not it["phantom"] and it["own"] is not None and it["flag"] != "📄":
            own = max(it["own"], 0.0)
            mi = max(it["mistral"], 0.0)
            # с доказателство от фактурата вярваме на нашата; иначе по-малката
            it["effective"] = own if it["ndk_fix"] else min(own, mi)
            diff = own - it["mistral"]
            if abs(diff) >= 1:
                it["flag"] = "≠"
                it["flag"] = "📄" if it["ndk_fix"] else "≠"
                it["why"] = (f"ревизия {it['base_date']}: {it['base']:g} + доставки {it['deliveries']:g} - продажби {it['sales']:g}"
                             + (f" {it['other']:+g}" if it["other"] else "")
                             + (f" {it['ndk_fix']:+g} по фактура {', '.join(it['ndk_fix_docs'])}" if it["ndk_fix"] else "")
                             + f" = {own:g}, Мистрал {it['mistral']:g}")
        out["items"][str(code)] = it
    _cache[store_id] = (now, out)
    return out


def adjust(db: Session, store_id: int, stock: dict[int, float]) -> tuple[dict[int, float], dict[int, str]]:
    """Наличност за заявката: по-малката от Мистрал/наша; фантом -> 0. Връща и бележки по article_id."""
    from . import pilot
    if store_id not in pilot.active_ids(db):
        return stock, {}
    data = compute(db, store_id)
    if not data["items"]:
        return stock, {}
    codes = _codes_for_store(db, store_id)
    notes: dict[int, str] = {}
    new = dict(stock)
    for sku, it in data["items"].items():
        aid = codes.get(int(sku))
        if aid is None:
            continue
        cur = float(new.get(aid, it["mistral"]) or 0.0)
        if it["phantom"]:
            new[aid] = 0.0
            notes[aid] = "🕳 фантомна наличност: " + it["why"]
        elif it.get("ndk_fix") and abs(it["effective"] - cur) >= 0.5:
            new[aid] = it["effective"]
            notes[aid] = f"📄 по фактура на НДК: наличност {it['effective']:g} (Мистрал {it['mistral']:g}, {it['ndk_fix']:+g})"
        elif it["own"] is not None and it["effective"] < cur - 0.5:
            new[aid] = it["effective"]
            notes[aid] = f"🧮 наша наличност {it['own']:g} (Мистрал {it['mistral']:g})"
    return new, notes


def listing(db: Session, store_id: int, fresh: bool = False) -> dict:
    d = compute(db, store_id, fresh)
    items = sorted(d["items"].values(), key=lambda x: (0 if x["phantom"] else 1 if x["flag"] else 2, -(x["per_day"] or 0)))
    n_base = sum(1 for x in items if x["own"] is not None)
    return {"store_id": store_id, "at": d["at"], "items": items,
            "summary": {"articles": len(items), "with_base": n_base, "phantom": sum(1 for x in items if x["phantom"]),
                        "differ": sum(1 for x in items if x["flag"] == "≠"),
                        "no_base": len(items) - n_base}}


def phantoms_all(db: Session) -> list[dict]:
    """Фантоми във всички пилотни магазини (за дневната проверка и Telegram)."""
    from . import pilot
    out = []
    names = {s.id: s.name for s in db.execute(select(m.Store)).scalars()}
    for sid in sorted(pilot.active_ids(db)):
        try:
            d = compute(db, sid)
        except Exception:
            db.rollback()
            continue
        for it in d["items"].values():
            if it["phantom"]:
                out.append({"store_id": sid, "store": names.get(sid), **it})
    return out
