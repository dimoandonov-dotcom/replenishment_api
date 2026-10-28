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


# ---------------------------------------------------------------------------
# Съдия: ревизията. Всяка сутрин пазим снимка (Мистрал / наша / за заявка);
# при ревизия сравняваме кое е било по-близо до преброеното.
#   прогноза Мистрал = SEARCHQTY (очакваното от Мистрал в момента на броене)
#   наша прогноза    = SEARCHQTY + (наша - Мистрал) от последната снимка преди броенето
# ---------------------------------------------------------------------------
SNAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS ownstock_snap (day DATE, store_id INT, mc INT, mistral NUMERIC(12,2), own NUMERIC(12,2),
  eff NUMERIC(12,2), phantom BOOLEAN, flag TEXT, why TEXT, at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY (day, store_id, mc))
"""
_snap_ok = False


def _snap_ensure(db: Session):
    global _snap_ok
    if not _snap_ok:
        db.execute(text(SNAP_SCHEMA))
        db.commit()
        _snap_ok = True


def snapshot(db: Session) -> dict:
    """Снимка за днес за пилотните магазини (веднъж на ден)."""
    from . import pilot
    _snap_ensure(db)
    today = datetime.now(TZ).date()
    n = 0
    for sid in sorted(pilot.active_ids(db)):
        try:
            d = compute(db, sid, fresh=True)
        except Exception:
            db.rollback()
            continue
        for sku, it in d["items"].items():
            ours = it["effective"] if (it["phantom"] or it.get("ndk_fix") or it["own"] is not None) else it["mistral"]
            db.execute(text("""INSERT INTO ownstock_snap(day, store_id, mc, mistral, own, eff, phantom, flag, why)
                               VALUES (:d,:s,:m,:mi,:o,:e,:p,:f,:w)
                               ON CONFLICT (day, store_id, mc) DO UPDATE SET mistral=:mi, own=:o, eff=:e, phantom=:p, flag=:f, why=:w, at=now()"""),
                       {"d": today, "s": sid, "m": int(sku), "mi": it["mistral"], "o": it["own"], "e": ours,
                        "p": bool(it["phantom"]), "f": it.get("flag") or "", "w": (it.get("why") or "")[:300]})
            n += 1
        db.commit()
    return {"day": str(today), "rows": n}


def snapshot_if_needed(db: Session) -> str | None:
    _snap_ensure(db)
    now = datetime.now(TZ)
    if now.hour < 6:
        return None
    have = db.execute(text("SELECT 1 FROM ownstock_snap WHERE day = :d LIMIT 1"), {"d": now.date()}).first()
    if have:
        return None
    return str(snapshot(db))


def score(db: Session, days: int = 30) -> dict:
    """При всяка ревизия в пилотните магазини: Мистрал или ние бяхме по-близо до преброеното."""
    from . import mistral, pilot
    from .anomalies import _loc_map
    _snap_ensure(db)
    snaps = db.execute(text("""SELECT day, store_id, mc, mistral, eff, phantom, flag, why, at FROM ownstock_snap
                               WHERE day >= :d"""), {"d": datetime.now(TZ).date() - timedelta(days=days)}).all()
    if not snaps:
        return {"stores": [], "total": {}, "since": None, "note": "още няма снимки - първата е тази сутрин"}
    by: dict = {}
    for r in snaps:
        by.setdefault((r[1], r[2]), []).append(r)
    first = min(r[8] for r in snaps)
    names = {s.id: s.name for s in db.execute(select(m.Store)).scalars()}
    sids = sorted(pilot.active_ids(db))
    with mistral.use("varna"), mistral.connect() as conn:
        cur = conn.cursor()
        locs = {s: l for l, s in _loc_map(db, cur).items() if s in sids}
        if not locs:
            return {"stores": [], "total": {}}
        cur.execute(f"""SELECT i.LOCATIONID AS loc, i.INVENTORYNUM AS num, i.DATEINVENTORY AS d, c.MATERIALCODE AS mc,
                               c.SEARCHQTY AS exp, c.QTYFOUND AS found
                        FROM INVENTORY i WITH (NOLOCK) JOIN INVENTORYCONTENT c WITH (NOLOCK)
                          ON c.LOCATIONID = i.LOCATIONID AND c.INVENTORYNUM = i.INVENTORYNUM
                        WHERE i.LOCATIONID IN ({','.join(map(str, locs.values()))}) AND i.DATEINVENTORY >= %s
                          AND i.CHECKEDMATERIALCOUNT >= i.MATERIALCOUNT""", (first.astimezone(TZ).replace(tzinfo=None),))
        revs = cur.fetchall()
    sid_by_loc = {l: s for s, l in locs.items()}
    stores: dict = {}
    for r in revs:
        sid = sid_by_loc.get(r["loc"])
        cand = [x for x in by.get((sid, int(r["mc"])), []) if x[8].astimezone(TZ).replace(tzinfo=None) < r["d"]]
        if not cand:
            continue
        sn = max(cand, key=lambda x: x[8])
        exp_m = float(r["exp"] or 0)
        gap = float(sn[4] or 0) - float(sn[3] or 0)      # наша - Мистрал към снимката
        exp_o = max(exp_m + gap, 0.0)                    # (фантом: снимката казва 0 -> gap = -Мистрал)
        found = float(r["found"] or 0)
        st = stores.setdefault(sid, {"store_id": sid, "store": names.get(sid), "counted": 0, "differ": 0, "ours": 0,
                                     "mistral": 0, "tie": 0, "err_ours": 0.0, "err_mistral": 0.0, "examples": []})
        st["counted"] += 1
        if abs(gap) < 0.5 and not sn[5]:
            continue                                    # предвиждахме същото като Мистрал - не се брои
        st["differ"] += 1
        eo, em = abs(exp_o - found), abs(exp_m - found)
        st["err_ours"] += eo
        st["err_mistral"] += em
        win = "ours" if eo + 0.01 < em else "mistral" if em + 0.01 < eo else "tie"
        st[win] += 1
        if len(st["examples"]) < 15:
            st["examples"].append({"mc": int(r["mc"]), "rev": r["d"].strftime("%d.%m"), "found": found, "mistral": exp_m,
                                   "ours": round(exp_o, 2), "win": win, "flag": sn[6], "why": sn[7]})
    tot = {k: sum(s[k] for s in stores.values()) for k in ("counted", "differ", "ours", "mistral", "tie")}
    tot["err_ours"] = round(sum(s["err_ours"] for s in stores.values()), 1)
    tot["err_mistral"] = round(sum(s["err_mistral"] for s in stores.values()), 1)
    nm = {}
    try:
        from . import ndkinv
        _, nm = ndkinv.code_map(db)
    except Exception:
        db.rollback()
    for s in stores.values():
        s["err_ours"], s["err_mistral"] = round(s["err_ours"], 1), round(s["err_mistral"], 1)
        for e in s["examples"]:
            e["name"] = nm.get(e["mc"], "")
    return {"stores": sorted(stores.values(), key=lambda x: -x["differ"]), "total": tot,
            "since": first.astimezone(TZ).strftime("%d.%m %H:%M")}
