"""
Дневни обороти по магазин и група + анализ на спадовете и причините.

Сравнението е винаги с ОБИЧАЙНОТО ниво: средното от същия ден от седмицата през
последните 4 седмици (за 7-дневен прозорец - същите 7 дни от предните 4 седмици).
Така понеделник се сравнява с понеделници, а не с петък.

Причините се търсят автоматично:
  магазин  - по-малко клиенти (бонове), по-малка средна сметка, работил по-кратко, 0 оборот
  група    - общ спад във веригата (сезон/време/празник) или само в този магазин
  артикул  - свършила наличност, свършила акция, по-висока цена, не се продава при наличност
"""
from __future__ import annotations

import json
import statistics
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import models as m

SOFIA = timezone(timedelta(hours=3))
WEEKS = 4                    # базата: същите дни от предните 4 седмици
STORE_DROP_PCT = -8.0        # магазин в червено под -8%
GRP_DROP_PCT = -15.0         # група в магазин: спад поне 15% ...
GRP_DROP_EUR = 15.0          # ... и поне 15 € на ден загуба
ART_DROP_EUR = 2.0           # артикул в причините: поне 2 € на ден загуба
CHAIN_PCT = -10.0            # групата пада и във веригата -> общ спад
HOURS_DIFF_MIN = 45          # отворил по-късно / затворил по-рано с 45+ мин.
SHELF_MIN_PER_DAY = 3.0      # „не се продава при наличност“ само за артикули с 3+ бр./ден (иначе 0 е случайност)

CAUSES = {
    "stock": "Свършила наличност",
    "promo": "Свършила акция",
    "price": "По-висока цена",
    "shelf": "Не се продава при наличност",
    "neg": "Минусова наличност (данните не са верни)",
    "chain": "Общ спад във веригата",
    "traffic": "По-малко клиенти",
    "hours": "Работил по-кратко",
    "closed": "Без оборот (затворен / няма данни)",
    "noise": "Нормално колебание (малки артикули)",
    "other": "Продават се по-малко, а всичко е наред",
}

_job = {"running": False, "last": None, "error": None, "progress": ""}


def _ensure(db: Session):
    for q in [
        """CREATE TABLE IF NOT EXISTS to_receipts (store_id INT NOT NULL, day DATE NOT NULL, receipts INT NOT NULL,
             first_at TIME, last_at TIME, PRIMARY KEY (day, store_id))""",
        """CREATE TABLE IF NOT EXISTS to_daily (store_id INT NOT NULL, day DATE NOT NULL, grp TEXT NOT NULL,
             rev NUMERIC(14,2) NOT NULL, cost NUMERIC(14,2) NOT NULL, qty NUMERIC(14,3) NOT NULL,
             PRIMARY KEY (day, store_id, grp))""",
        """CREATE TABLE IF NOT EXISTS to_report (day DATE NOT NULL, n INT NOT NULL, data JSONB NOT NULL,
             built_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (day, n))""",
    ]:
        db.execute(text(q))
    db.commit()


# ------------------------------------------------------------------ данни
def _each_region(fn, db):
    """Пуска fn() за всеки регион (Варна, София); грешка в София не спира Варна."""
    from . import mistral
    res = []
    for rg in mistral.regions():
        with mistral.use(rg):
            try:
                res.append(fn())
            except Exception:
                db.rollback()
                if rg == "varna":
                    raise
    return res


def sync_receipts(db: Session, days: int = 3, only: date | None = None) -> dict:
    """Брой касови бонове и първи/последен бон на ден по магазин (от Мистрал) - всички региони."""
    res = _each_region(lambda: _sync_receipts_region(db, days, only), db)
    return {"days": days, "rows": sum(r["rows"] for r in res)}


def _sync_receipts_region(db: Session, days: int = 3, only: date | None = None) -> dict:
    from . import mistral
    from .anomalies import _loc_map
    rids = mistral.store_ids(db)
    _ensure(db)
    today = datetime.now(SOFIA).date()
    n = 0
    with mistral.connect() as conn:
        cur = conn.cursor()
        locs = _loc_map(db, cur)
        L = ",".join(map(str, locs))
        for d in ([only] if only else [today - timedelta(days=i) for i in range(days, 0, -1)]):
            _job["progress"] = f"бонове {d.strftime('%d.%m')}"
            cur.execute(f"""SELECT LOCATIONID AS loc, COUNT(*) AS n, MIN(SALEDATE) AS f, MAX(SALEDATE) AS l
                            FROM SALE WITH (NOLOCK) WHERE REPORTINGDATE = %s AND LOCATIONID IN ({L})
                            GROUP BY LOCATIONID""", (d.isoformat(),))
            agg: dict[int, list] = {}
            for r in cur.fetchall():
                sid = locs.get(r["loc"])
                if sid is None:
                    continue
                f, l = r["f"], r["l"]
                a = agg.setdefault(sid, [0, None, None])
                a[0] += int(r["n"] or 0)
                if f is not None and (a[1] is None or f < a[1]): a[1] = f
                if l is not None and (a[2] is None or l > a[2]): a[2] = l
            db.execute(text("DELETE FROM to_receipts WHERE day = :d AND store_id = ANY(:ids)"), {"d": d, "ids": rids})
            for sid, (cnt, f, l) in agg.items():
                db.execute(text("""INSERT INTO to_receipts(store_id, day, receipts, first_at, last_at)
                                   VALUES (:s,:d,:n,:f,:l)"""),
                           {"s": sid, "d": d, "n": cnt, "f": f.time() if f and f.date() == d else None,
                            "l": l.time() if l and l.date() == d else None})
                n += 1
            db.commit()
    return {"days": days, "rows": n}


def resync_sales_day(db: Session, d: date) -> int:
    """Пак дърпа продажбите за деня (в 02:30 някои магазини още не са качили всички бележки) - всички региони."""
    return sum(_each_region(lambda: _resync_sales_day_region(db, d), db))


def _resync_sales_day_region(db: Session, d: date) -> int:
    from . import mistral
    from .anomalies import _loc_map
    from .salesall import _bulk
    rids = mistral.store_ids(db)
    with mistral.connect() as conn:
        cur = conn.cursor()
        locs = _loc_map(db, cur)
        L = ",".join(map(str, locs))
        cur.execute(f"""
            SELECT c.LOCATIONID AS loc, c.MATERIALCODE AS code, SUM(c.QTY) AS q,
                   SUM(c.QTY * c.SALEPRICE) AS rev, SUM(c.QTY * c.AVGDELIVERYPRICE) AS cost
            FROM SALE s WITH (NOLOCK)
            JOIN SALECONTENT c WITH (NOLOCK) ON c.NUM = s.NUM AND c.LOCATIONID = s.LOCATIONID
            WHERE s.REPORTINGDATE = %s AND s.LOCATIONID IN ({L})
            GROUP BY c.LOCATIONID, c.MATERIALCODE""", (d.isoformat(),))
        agg: dict[tuple, list] = {}
        for r in cur.fetchall():
            sid = locs.get(r["loc"])
            if sid is None:
                continue
            a = agg.setdefault((sid, int(r["code"])), [0.0, 0.0, 0.0])
            a[0] += float(r["q"] or 0); a[1] += float(r["rev"] or 0); a[2] += float(r["cost"] or 0)
    if not agg:
        return 0
    db.execute(text("DELETE FROM sa_sales WHERE day = :d AND store_id = ANY(:ids)"), {"d": d, "ids": rids})
    _bulk(db, "INSERT INTO sa_sales(store_id, code, day, qty, rev, cost) VALUES %s",
          [(k[0], k[1], d, v[0], v[1], v[2]) for k, v in agg.items()])
    db.commit()
    return len(agg)


def resync(db: Session, d: date) -> dict:
    """Ръчно: пак продажбите и боновете за деня, после отчетите, в които влиза."""
    out = {"day": d.isoformat(), "sales_rows": resync_sales_day(db, d), "receipts": sync_receipts(db, only=d)}
    last = db.execute(text("SELECT MAX(day) FROM sa_sales")).scalar()
    out["rollup"] = rollup(db, (last - d).days + 1) if last else 0
    _refresh(db, d, 1)
    _refresh(db, d, 7)
    return out


def morning(db: Session) -> dict:
    """Сутрешна поправка (08:00): вчерашните продажби и бонове пак, после отчетът наново."""
    y = datetime.now(SOFIA).date() - timedelta(days=1)
    out = {"day": y.isoformat(), "sales_rows": resync_sales_day(db, y)}
    out["receipts"] = sync_receipts(db, 1)
    out["rollup"] = rollup(db, 1)
    for n in (1, 7):
        _refresh(db, y, n)
    # последните 7 дни: сверка с Мистрал, непълните дни се дърпат наново
    fixed = []
    for i in range(2, 9):
        d = y - timedelta(days=i - 1)
        try:
            if any(r["status"] == "непълно в базата" for r in check(db, d)["rows"]):
                resync(db, d); fixed.append(d.isoformat())
        except Exception:
            db.rollback()
    out["fixed_days"] = fixed
    return out


def rollup(db: Session, days: int = 3) -> int:
    """Оборот магазин × група × ден в трайна таблица (sa_sales се пази само 60 дни)."""
    _ensure(db)
    last = db.execute(text("SELECT MAX(day) FROM sa_sales")).scalar()
    if not last:
        return 0
    since = last - timedelta(days=days - 1)
    db.execute(text("DELETE FROM to_daily WHERE day BETWEEN :f AND :t"), {"f": since, "t": last})
    r = db.execute(text("""INSERT INTO to_daily(store_id, day, grp, rev, cost, qty)
        SELECT s.store_id, s.day, COALESCE(a.grp, 'Без група'), SUM(s.rev), SUM(s.cost), SUM(s.qty)
        FROM sa_sales s LEFT JOIN sa_articles a ON a.code = s.code
        WHERE s.day BETWEEN :f AND :t GROUP BY s.store_id, s.day, COALESCE(a.grp, 'Без група')"""),
                   {"f": since, "t": last})
    db.commit()
    return r.rowcount or 0


def nightly(db: Session) -> dict:
    """След нощното дърпане на продажбите: бонове, трайни обороти и готов отчет за вчера."""
    out = {}
    try:
        out["receipts"] = sync_receipts(db, 3)
    except Exception as e:
        db.rollback(); out["receipts"] = f"{type(e).__name__}: {e}"[:200]
    try:
        out["rollup"] = rollup(db, 3)
        last = db.execute(text("SELECT MAX(day) FROM sa_sales")).scalar()
        if last:
            for i in range(3):          # продажбите за 3 дни се дърпат наново -> и отчетите им
                for n in (1, 7):
                    _refresh(db, last - timedelta(days=i), n)
            out["report"] = last.isoformat()
    except Exception as e:
        db.rollback(); out["report"] = f"{type(e).__name__}: {e}"[:200]
    return out


def start_backfill(days: int = 56, only: str | None = None) -> dict:
    if _job["running"]:
        return {"started": False, "status": dict(_job)}
    from .db import SessionLocal

    def run():
        _job.update(running=True, error=None, progress="старт")
        try:
            with SessionLocal() as db:
                if only:
                    from . import mistral
                    with mistral.use(only):
                        r = _sync_receipts_region(db, days, None)
                else:
                    r = sync_receipts(db, days)
                _job["progress"] = "обороти по групи"
                r["rollup"] = rollup(db, days)
                last = db.execute(text("SELECT MAX(day) FROM sa_sales")).scalar()
                if last:
                    _job["progress"] = "отчети"
                    for i in range(3):
                        for n in (1, 7):
                            _refresh(db, last - timedelta(days=i), n)
                _job["last"] = r
        except Exception as e:
            _job["error"] = f"{type(e).__name__}: {e}"[:400]
        finally:
            _job.update(running=False, progress="")
    threading.Thread(target=run, daemon=True).start()
    return {"started": True}


def status() -> dict:
    return dict(_job)


# ------------------------------------------------------------------ анализ
def _windows(day: date, n: int):
    """Дните на текущия прозорец (w=0) и на базовите седмици (w=1..4)."""
    return {w: [day - timedelta(days=7 * w + i) for i in range(n)] for w in range(WEEKS + 1)}


def _wcase(day: date, n: int, col: str = "s.day") -> str:
    # седмица 0..4 според разстоянието от деня; -1 = извън прозорците
    return (f"CASE WHEN ((CAST(:d AS DATE) - {col}) % 7) < {n} AND (CAST(:d AS DATE) - {col}) >= 0 "
            f"AND (CAST(:d AS DATE) - {col}) < {7 * WEEKS + 7} THEN (CAST(:d AS DATE) - {col}) / 7 ELSE -1 END")


def _pct(cur: float, base: float) -> float | None:
    return round(100 * (cur - base) / base, 1) if base else None


def _tmin(t) -> int | None:
    return t.hour * 60 + t.minute if t is not None else None


def _hm(mins) -> str:
    return f"{int(mins) // 60:02d}:{int(mins) % 60:02d}" if mins is not None else "—"


def _eur(x: float) -> str:
    return f"{x:,.0f} €".replace(",", " ")


def build(db: Session, day: date, n: int = 1) -> dict:
    _ensure(db)
    from .salesall import _ensure as _sa_ensure
    _sa_ensure(db)
    oldest = day - timedelta(days=7 * WEEKS + n - 1)
    have = db.execute(text("SELECT MIN(day), MAX(day) FROM rg_sa_sales")).one()
    if not have[0] or day > have[1]:
        return {"ready": False, "reason": f"няма продажби за {day.strftime('%d.%m.%Y')}"}
    stores = {s.id: s.name for s in db.execute(select(m.Store)).scalars()}
    P = {"d": day, "f": oldest, "t": day}
    wc = _wcase(day, n)

    # 1) магазин × група × седмица
    sg = defaultdict(lambda: [[0.0, 0.0] for _ in range(WEEKS + 1)])   # (sid, grp) -> w -> [rev, cost]
    for sid, g, w, r, c in db.execute(text(f"""
            SELECT s.store_id, COALESCE(a.grp, 'Без група'), {wc} AS w, SUM(s.rev), SUM(s.cost)
            FROM rg_sa_sales s LEFT JOIN sa_articles a ON a.code = s.code
            WHERE s.day BETWEEN :f AND :t GROUP BY 1, 2, 3"""), P).all():
        if w is None or w < 0:
            continue
        sg[(sid, g)][w][0] += float(r or 0); sg[(sid, g)][w][1] += float(c or 0)

    st_w = defaultdict(lambda: [0.0] * (WEEKS + 1))
    for (sid, g), ws in sg.items():
        for w in range(WEEKS + 1):
            st_w[sid][w] += ws[w][0]
    # базови седмици на магазина: само седмиците с оборот (затворен ден/липса на данни не дърпа базата надолу)
    base_weeks = {sid: [w for w in range(1, WEEKS + 1) if ws[w] > 0] for sid, ws in st_w.items()}
    has_base = have[0] <= oldest

    def base(ws, sid, idx=None):
        bw = base_weeks.get(sid) or []
        if not bw:
            return 0.0
        vals = [(ws[w] if idx is None else ws[w][idx]) for w in bw]
        return sum(vals) / len(vals)

    # 2) бонове и часове
    rc = defaultdict(lambda: [None] * (WEEKS + 1))   # sid -> w -> [receipts, first, last] (сума за n дни)
    for sid, w, cnt, f, l in db.execute(text(f"""
            SELECT store_id, {_wcase(day, n, 'day')} AS w, SUM(receipts),
                   MIN(first_at), MAX(last_at)
            FROM rg_to_receipts WHERE day BETWEEN :f AND :t GROUP BY 1, 2"""), P).all():
        if w is None or w < 0:
            continue
        rc[sid][w] = [int(cnt or 0), _tmin(f), _tmin(l)]

    # 3) верига по група
    chain_g = defaultdict(lambda: [0.0, 0.0])        # grp -> [cur, base]
    for (sid, g), ws in sg.items():
        chain_g[g][0] += ws[0][0]
        chain_g[g][1] += base(ws, sid, 0)
    chain_pct = {g: _pct(c, b) for g, (c, b) in chain_g.items()}

    # 4) магазини
    store_rows, flagged = [], []
    for sid in sorted(set(st_w) | set(rc)):
        if sid not in stores:
            continue
        cur_r = st_w[sid][0]
        b_r = base(st_w[sid], sid)
        r0 = rc[sid][0]
        rb = [rc[sid][w] for w in (base_weeks.get(sid) or []) if rc[sid][w]]
        rec0 = r0[0] if r0 else None
        recb = sum(x[0] for x in rb) / len(rb) if rb else None
        bask0 = cur_r / rec0 if rec0 else None
        baskb = b_r / recb if recb else None
        causes = []
        if cur_r <= 0 and b_r > 0:
            causes.append({"k": "closed", "t": "Няма оборот — магазинът е бил затворен или касата не е подала данни"})
        else:
            if rec0 is not None and recb and _pct(rec0, recb) is not None and _pct(rec0, recb) <= -8:
                causes.append({"k": "traffic", "t": f"По-малко клиенти: {rec0 / n:.0f} бона на ден срещу обичайно {recb / n:.0f} ({_pct(rec0, recb):+.0f}%)"})
            if bask0 and baskb and _pct(bask0, baskb) <= -8:
                causes.append({"k": "basket", "t": f"По-малка средна сметка: {bask0:.2f} € срещу {baskb:.2f} € ({_pct(bask0, baskb):+.0f}%)"})
            if n == 1 and r0 and rb:
                fb = [x[1] for x in rb if x[1] is not None]; lb = [x[2] for x in rb if x[2] is not None]
                if r0[1] is not None and fb and r0[1] - statistics.median(fb) >= HOURS_DIFF_MIN:
                    causes.append({"k": "hours", "t": f"Отворил по-късно: първи бон {_hm(r0[1])} (обичайно {_hm(statistics.median(fb))})"})
                if r0[2] is not None and lb and statistics.median(lb) - r0[2] >= HOURS_DIFF_MIN:
                    causes.append({"k": "hours", "t": f"Затворил по-рано: последен бон {_hm(r0[2])} (обичайно {_hm(statistics.median(lb))})"})
        pct = _pct(cur_r, b_r)
        row = {"store_id": sid, "store": stores[sid], "rev": round(cur_r, 2), "base": round(b_r, 2),
               "diff": round(cur_r - b_r, 2), "pct": pct,
               "receipts": rec0, "receipts_base": round(recb) if recb else None,
               "basket": round(bask0, 2) if bask0 else None, "basket_base": round(baskb, 2) if baskb else None,
               "first": _hm(r0[1]) if r0 and n == 1 else None, "last": _hm(r0[2]) if r0 and n == 1 else None,
               "causes": causes, "groups": [], "weeks": len(base_weeks.get(sid) or [])}
        cost0 = sum(ws[0][1] for (s3, _g), ws in sg.items() if s3 == sid)
        row["margin"] = round(cur_r - cost0, 2)
        row["margin_pct"] = round(100 * (cur_r - cost0) / cur_r, 1) if cur_r else None
        store_rows.append(row)

        # групите на магазина
        for (s2, g), ws in sg.items():
            if s2 != sid:
                continue
            c0, b0 = ws[0][0], base(ws, sid, 0)
            if b0 <= 0 and c0 <= 0:
                continue
            gp = _pct(c0, b0)
            gr = {"grp": g, "rev": round(c0, 2), "base": round(b0, 2), "diff": round(c0 - b0, 2), "pct": gp,
                  "chain_pct": chain_pct.get(g), "flag": False, "causes": [], "articles": []}
            if cur_r > 0 and gp is not None and gp <= GRP_DROP_PCT and (b0 - c0) >= GRP_DROP_EUR * n:
                gr["flag"] = True
                flagged.append((sid, g, gr))
            row["groups"].append(gr)
        row["groups"].sort(key=lambda x: x["diff"])

    # 5) артикулите на маркираните групи -> причини
    if flagged:
        _article_causes(db, day, n, flagged, base_weeks, chain_pct)

    # 6) обобщение на причините (загубени € по причина)
    by_cause = defaultdict(float)
    drops = []
    for row in store_rows:
        smiss = max(row["base"] - row["rev"], 0)
        lost_g = 0.0
        ks = {c["k"] for c in row["causes"]}
        is_closed = "closed" in ks
        if is_closed:
            by_cause["closed"] += smiss
            drops.append({"store_id": row["store_id"], "store": row["store"], "grp": "всички групи", "lost": round(smiss, 2),
                          "pct": row["pct"], "chain_pct": None, "causes": row["causes"]})
        store_k = "hours" if "hours" in ks else ("traffic" if "traffic" in ks else None)
        rp = _pct(row["receipts"] or 0, row["receipts_base"] or 0) if row["receipts"] and row["receipts_base"] else None
        store_drop = max(-(rp or 0), 0) if store_k == "traffic" else max(-(row["pct"] or 0), 0)
        for gr in row["groups"]:
            if not gr["flag"]:
                continue
            lost = gr["base"] - gr["rev"]
            lost_g += lost
            explained = 0.0
            if is_closed:
                gr.pop("_split", None)
                continue
            for k, eur in gr.pop("_split", {}).items():
                by_cause[k] += eur; explained += eur
            rest = lost - explained
            # необяснената част: по-малко клиенти / по-кратко работно време в магазина
            if rest > 0 and store_k:
                part = min(rest, gr["base"] * store_drop / 100)
                by_cause[store_k] += part; rest -= part
                gr["causes"].append({"k": store_k, "t": CAUSES[store_k].lower() + " в магазина"})
            if rest > 0:
                by_cause["other"] += rest
            drops.append({"store_id": row["store_id"], "store": row["store"], "grp": gr["grp"], "lost": round(lost, 2),
                          "pct": gr["pct"], "chain_pct": gr["chain_pct"], "causes": gr["causes"][:4]})
        row["flag"] = row["pct"] is not None and row["pct"] <= STORE_DROP_PCT
        row["lost_groups"] = round(lost_g, 2)
        if row["causes"] and smiss > 0 and row["flag"]:
            row["causes_eur"] = round(smiss, 2)
    for gr_ in (g for r in store_rows for g in r["groups"]):
        gr_.pop("_split", None)
    drops.sort(key=lambda x: -x["lost"])
    store_rows.sort(key=lambda r: (r["pct"] if r["pct"] is not None else 0))

    tot_c = sum(r["rev"] for r in store_rows); tot_b = sum(r["base"] for r in store_rows)
    rec_c = sum(r["receipts"] or 0 for r in store_rows); rec_b = sum(r["receipts_base"] or 0 for r in store_rows)
    groups = sorted(({"grp": g, "rev": round(c, 2), "base": round(b, 2), "diff": round(c - b, 2), "pct": chain_pct.get(g)}
                     for g, (c, b) in chain_g.items() if b > 0 or c > 0), key=lambda x: x["diff"])
    lost_total = sum(by_cause.values())
    cause_rows = sorted(({"k": k, "name": CAUSES.get(k, k), "eur": round(v, 2),
                          "share": round(100 * v / lost_total, 1) if lost_total else 0}
                         for k, v in by_cause.items() if v > 0.5), key=lambda x: -x["eur"])
    d0 = day - timedelta(days=n - 1)
    return {"ready": True, "day": day.isoformat(), "n": n,
            "period": day.strftime("%d.%m.%Y") if n == 1 else f"{d0.strftime('%d.%m')} – {day.strftime('%d.%m.%Y')}",
            "weekday": ["понеделник", "вторник", "сряда", "четвъртък", "петък", "събота", "неделя"][day.weekday()],
            "base_info": (f"средно от същия ден в предните {WEEKS} седмици" if n == 1 else f"средно от същите 7 дни в предните {WEEKS} седмици"),
            "partial_base": not has_base,
            "totals": {"rev": round(tot_c, 2), "base": round(tot_b, 2), "diff": round(tot_c - tot_b, 2), "pct": _pct(tot_c, tot_b),
                       "receipts": rec_c or None, "receipts_base": rec_b or None,
                       "basket": round(tot_c / rec_c, 2) if rec_c else None, "basket_base": round(tot_b / rec_b, 2) if rec_b else None,
                       "stores_down": sum(1 for r in store_rows if r["flag"]), "stores": len(store_rows),
                       "lost": round(sum(d["lost"] for d in drops), 2)},
            "causes": cause_rows, "groups": groups, "stores": store_rows, "drops": drops[:150]}


def _article_causes(db: Session, day: date, n: int, flagged, base_weeks, chain_pct):
    pairs = {(sid, g) for sid, g, _ in flagged}
    sids = sorted({sid for sid, _ in pairs})
    grps = sorted({g for _, g in pairs})
    P = {"d": day, "f": day - timedelta(days=7 * WEEKS + n - 1), "t": day, "s": sids, "g": grps}
    art = defaultdict(lambda: [[0.0, 0.0] for _ in range(WEEKS + 1)])   # (sid, code) -> w -> [qty, rev]
    meta = {}
    for sid, code, g, nm, w, q, r in db.execute(text(f"""
            SELECT s.store_id, s.code, COALESCE(a.grp, 'Без група'), MAX(a.name), {_wcase(day, n)} AS w, SUM(s.qty), SUM(s.rev)
            FROM rg_sa_sales s LEFT JOIN sa_articles a ON a.code = s.code
            WHERE s.day BETWEEN :f AND :t AND s.store_id = ANY(:s) AND COALESCE(a.grp, 'Без група') = ANY(:g)
            GROUP BY 1, 2, 3, 5"""), P).all():
        if w is None or w < 0 or (sid, g) not in pairs:
            continue
        art[(sid, code)][w][0] += float(q or 0); art[(sid, code)][w][1] += float(r or 0)
        meta[code] = (nm or str(code), g)
    codes = sorted({c for _, c in art})
    stock = {(s, c): float(q or 0) for s, c, q in db.execute(text(
        "SELECT store_id, code, qty FROM rg_sa_stock WHERE code = ANY(:c) AND store_id = ANY(:s)"), {"c": codes, "s": sids}).all()}
    # акции: имало ли е промо в базовите дни, а в текущите не
    win = _windows(day, n)
    cur_days, base_days = set(win[0]), {d for w in range(1, WEEKS + 1) for d in win[w]}
    promo_b, promo_c = defaultdict(int), set()
    for c, s, e in db.execute(text("""SELECT code, start_day, end_day FROM sa_promo WHERE code = ANY(:c)
                                        AND end_day >= :f AND start_day <= :t"""), {"c": codes, "f": P["f"], "t": day}).all():
        for d in cur_days:
            if s <= d <= e:
                promo_c.add(c)
        promo_b[c] += sum(1 for d in base_days if s <= d <= e)
    by_pair = defaultdict(list)
    for (sid, code), ws in art.items():
        bw = base_weeks.get(sid) or []
        if not bw:
            continue
        q0, r0 = ws[0]
        qb = sum(ws[w][0] for w in bw) / len(bw); rb = sum(ws[w][1] for w in bw) / len(bw)
        lost = rb - r0
        if lost < ART_DROP_EUR * n:
            continue
        nm, g = meta[code]
        sq = stock.get((sid, code))
        p0 = r0 / q0 if q0 > 0 else None
        pb = rb / qb if qb > 0 else None
        promo_end = code in promo_b and promo_b[code] >= max(len(base_days) // 3, 1) and code not in promo_c
        if promo_end:
            k, t = "promo", f"свърши акцията (продаваше {qb / n:.1f}/ден в акция, сега {q0 / n:.1f})"
        elif sq is not None and sq < 0:
            k, t = "neg", f"наличност {sq:g} бр. по Мистрал — незаведени доставки, не се знае дали е свършил"
        elif (sq is not None and sq <= 0) or (sq is None and q0 == 0):
            k, t = "stock", f"свършила наличност ({'0 бр.' if sq is None else f'{sq:g} бр.'} в края на деня)"
        elif q0 == 0 and sq and sq > 0 and qb / n >= SHELF_MIN_PER_DAY:
            k, t = "shelf", f"0 продажби при наличност {sq:g} бр. (обикновено {qb / n:.1f}/ден) — провери рафта/цената/срока"
        elif qb / n < SHELF_MIN_PER_DAY and q0 < qb:
            k, t = "noise", f"малки продажби ({qb / n:.1f}/ден) — нормално колебание"
        elif p0 and pb and p0 > pb * 1.04:
            k, t = "price", f"по-висока цена: {p0:.2f} € срещу {pb:.2f} € ({_pct(p0, pb):+.0f}%)"
        elif sq is not None and qb > 0 and sq < qb / n * 0.5:
            k, t = "stock", f"почти свършил: {sq:g} бр. в края на деня при {qb / n:.1f}/ден продажби"
        else:
            k, t = "other", "по-малко продажби"
        by_pair[(sid, g)].append({"code": code, "name": nm, "qty": round(q0, 1), "qty_base": round(qb, 1),
                                  "rev": round(r0, 2), "base": round(rb, 2), "lost": round(lost, 2),
                                  "stock": sq, "k": k, "cause": t})
    for sid, g, gr in flagged:
        arts = sorted(by_pair.get((sid, g), []), key=lambda x: -x["lost"])
        gr["articles"] = arts[:12]
        lost_g = gr["base"] - gr["rev"]
        split = defaultdict(float)
        for a in arts:
            split[a["k"]] += a["lost"]
        cp = chain_pct.get(g)
        causes = []
        chain_part = 0.0
        if cp is not None and cp <= CHAIN_PCT:
            # частта от спада, която идва от общия спад във веригата
            chain_part = max(0.0, min(lost_g, gr["base"] * -cp / 100))
            causes.append({"k": "chain", "t": f"групата пада и в цялата верига ({cp:+.0f}%) — сезон, време, празник или доставчик"})
        for k in ("stock", "neg", "promo", "price", "shelf", "noise"):
            if split.get(k):
                nn = sum(1 for a in arts if a["k"] == k)
                causes.append({"k": k, "t": f"{CAUSES[k]}: {nn} арт. (−{_eur(split[k])})"})
        if not causes:
            causes.append({"k": "other", "t": "по-слаби продажби на много артикули, без наличност/акция/цена като причина"
                           + (f" · във веригата групата е {cp:+.0f}%" if cp is not None and not chain_part else "")})
        # разпределяме загубата: първо общия спад, остатъкът - по причините при артикулите
        split.pop("other", None)
        rest = max(lost_g - chain_part, 0.0)
        tot = sum(split.values())
        if tot > rest:
            split = defaultdict(float, {k: (v * rest / tot if tot else 0) for k, v in split.items()})
        if chain_part:
            split["chain"] = chain_part
        gr["causes"] = causes
        gr["_split"] = dict(split)


def _refresh(db: Session, day: date, n: int):
    """Отчетът наново за Варна и (ако е вързана) за София."""
    from . import mistral
    from .db import region
    report(db, day, n, refresh=True)
    if "sofia" in mistral.regions():
        try:
            with region(db, "sofia"):
                report(db, day, n, refresh=True)
        except Exception:
            db.rollback()


def report(db: Session, day: date | None = None, n: int = 1, refresh: bool = False) -> dict:
    _ensure(db)
    from .db import REGION
    rg = REGION.get()
    nk = n + {"varna": 0, "sofia": 1000, "all": 2000}.get(rg, 0)     # кешът е отделен по регион
    if day is None:
        day = db.execute(text("SELECT MAX(day) FROM rg_sa_sales")).scalar()
        if not day:
            return {"ready": False, "reason": "няма продажби"}
    if not refresh:
        row = db.execute(text("SELECT data, built_at FROM to_report WHERE day = :d AND n = :n"), {"d": day, "n": nk}).first()
        if row:
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            data["built_at"] = row[1].astimezone(SOFIA).strftime("%d.%m %H:%M")
            return data
    t0 = time.time()
    data = build(db, day, n)
    data["region"] = rg
    data["seconds"] = round(time.time() - t0, 1)
    if data.get("ready"):
        db.execute(text("""INSERT INTO to_report(day, n, data) VALUES (:d, :n, CAST(:j AS JSONB))
                           ON CONFLICT (day, n) DO UPDATE SET data = EXCLUDED.data, built_at = now()"""),
                   {"d": day, "n": nk, "j": json.dumps(data, ensure_ascii=False)})
        db.commit()
    data["built_at"] = datetime.now(SOFIA).strftime("%d.%m %H:%M")
    return data


def store_history(db: Session, store_id: int, days: int = 56) -> dict:
    """Дневен оборот и бонове на магазин (за графиката)."""
    _ensure(db)
    last = db.execute(text("SELECT MAX(day) FROM rg_sa_sales")).scalar()
    if not last:
        return {"days": []}
    since = last - timedelta(days=days - 1)
    rev = {d: float(r or 0) for d, r in db.execute(text("""SELECT day, SUM(rev) FROM rg_sa_sales WHERE store_id = :s
            AND day BETWEEN :f AND :t GROUP BY day"""), {"s": store_id, "f": since, "t": last}).all()}
    rec = {d: int(n or 0) for d, n in db.execute(text("""SELECT day, receipts FROM rg_to_receipts WHERE store_id = :s
            AND day BETWEEN :f AND :t"""), {"s": store_id, "f": since, "t": last}).all()}
    out = []
    for i in range(days):
        d = since + timedelta(days=i)
        out.append({"day": d.strftime("%d.%m"), "wd": "пвсчпсн"[d.weekday()], "rev": round(rev.get(d, 0), 2),
                    "receipts": rec.get(d)})
    return {"store_id": store_id, "days": out}


def check(db: Session, day: date) -> dict:
    """Пълнота на данните за деня: всеки обект в Мистрал срещу нашата база (оборот, бонове, последен бон)."""
    from . import mistral
    from .anomalies import _loc_map
    _ensure(db)
    stores = {s.id: (s.name, s.is_active) for s in db.execute(select(m.Store)).scalars()}
    locs, lname, live = {}, {}, {}

    def _one():
        rg = mistral.current()
        with mistral.connect() as conn:
            cur = conn.cursor()
            for k, v in _loc_map(db, cur).items():
                locs[(rg, k)] = v
            cur.execute("SELECT ID, NAME FROM LOCATION")
            for r in cur.fetchall():
                lname[(rg, r["ID"])] = r["NAME"]
            cur.execute("""SELECT s.LOCATIONID AS loc, COUNT(DISTINCT s.NUM) AS n, MAX(s.SALEDATE) AS l,
                                  SUM(c.QTY * c.SALEPRICE) AS rev
                           FROM SALE s WITH (NOLOCK) JOIN SALECONTENT c WITH (NOLOCK) ON c.NUM = s.NUM AND c.LOCATIONID = s.LOCATIONID
                           WHERE s.REPORTINGDATE = %s GROUP BY s.LOCATIONID""", (day.isoformat(),))
            for r in cur.fetchall():
                live[(rg, r["loc"])] = (int(r["n"] or 0), r["l"], float(r["rev"] or 0))
    _each_region(_one, db)
    ours = {sid: float(r or 0) for sid, r in db.execute(text(
        "SELECT store_id, SUM(rev) FROM sa_sales WHERE day = :d GROUP BY store_id"), {"d": day}).all()}
    rec = {sid: (n, l) for sid, n, l in db.execute(text(
        "SELECT store_id, receipts, last_at FROM to_receipts WHERE day = :d"), {"d": day}).all()}
    rows, by_sid = [], defaultdict(lambda: [0, None, 0.0, []])
    for loc, (n, l, rev) in live.items():
        sid = locs.get(loc)
        if sid is None:
            rows.append({"loc": loc[1], "mistral": lname.get(loc, str(loc[1])), "store": None, "status": "не е вързан към магазин",
                         "live_rev": round(rev, 2), "live_receipts": n, "live_last": l.strftime("%H:%M") if l else None})
            continue
        a = by_sid[sid]; a[0] += n; a[2] += rev; a[3].append(lname.get(loc, str(loc[1])))
        if l and (a[1] is None or l > a[1]): a[1] = l
    for sid, (name, active) in stores.items():
        if not active:
            continue
        n, l, rev, ln = by_sid.get(sid, [0, None, 0.0, []])
        o = ours.get(sid, 0.0); rc = rec.get(sid, (None, None))
        diff = round(rev - o, 2)
        if not ln:
            st = "няма продажби в Мистрал"
        elif abs(diff) < max(1.0, rev * 0.002):
            st = "ok"
        else:
            st = "непълно в базата"
        rows.append({"store_id": sid, "store": name, "mistral": ", ".join(ln), "status": st,
                     "live_rev": round(rev, 2), "our_rev": round(o, 2), "diff": diff,
                     "live_receipts": n, "our_receipts": rc[0],
                     "live_last": l.strftime("%H:%M") if l else None, "our_last": rc[1].strftime("%H:%M") if rc[1] else None})
    rows.sort(key=lambda r: (r["status"] == "ok", -(abs(r.get("diff") or 0))))
    return {"day": day.isoformat(), "checked_at": datetime.now(SOFIA).strftime("%d.%m %H:%M"),
            "stores": sum(1 for r in rows if r.get("store_id")), "ok": sum(1 for r in rows if r["status"] == "ok"),
            "rows": rows}


def probe(db: Session, day: date) -> dict:
    """Разбивка на оборота за деня (за сверка с ръчни справки)."""
    from . import mistral
    from .anomalies import _loc_map
    out = {}
    with mistral.connect() as conn:
        cur = conn.cursor()
        locs = _loc_map(db, cur)
        L = ",".join(map(str, locs))
        base = f"""FROM SALE s WITH (NOLOCK) JOIN SALECONTENT c WITH (NOLOCK) ON c.NUM = s.NUM AND c.LOCATIONID = s.LOCATIONID"""
        def q(sql, *a):
            cur.execute(sql, a); return [{k: (float(v) if hasattr(v, "as_integer_ratio") or type(v).__name__ == "Decimal" else (str(v) if v is not None else None)) for k, v in r.items()} for r in cur.fetchall()]
        out["by_optype"] = q(f"""SELECT s.OPERATIONTYPE AS op, s.DOCUMENTTYPEID AS dt, CASE WHEN s.PARTNERNAMEID <> 0 THEN 1 ELSE 0 END AS partner,
                 COUNT(DISTINCT s.NUM) AS n, SUM(c.QTY * c.SALEPRICE) AS rev, SUM(CASE WHEN c.QTY < 0 THEN c.QTY * c.SALEPRICE ELSE 0 END) AS neg
                 {base} WHERE s.REPORTINGDATE = %s AND s.LOCATIONID IN ({L}) GROUP BY s.OPERATIONTYPE, s.DOCUMENTTYPEID, CASE WHEN s.PARTNERNAMEID <> 0 THEN 1 ELSE 0 END""", day.isoformat())
        out["by_saledate"] = q(f"""SELECT SUM(c.QTY * c.SALEPRICE) AS rev {base} WHERE CAST(s.SALEDATE AS date) = %s AND s.LOCATIONID IN ({L})""", day.isoformat())
        out["all_locations"] = q(f"""SELECT SUM(c.QTY * c.SALEPRICE) AS rev, SUM(c.QTY * c.DEFAULTSALEPRICE) AS rev_default {base} WHERE s.REPORTINGDATE = %s""", day.isoformat())
        out["unmapped"] = q(f"""SELECT s.LOCATIONID AS loc, SUM(c.QTY * c.SALEPRICE) AS rev {base} WHERE s.REPORTINGDATE = %s AND s.LOCATIONID NOT IN ({L}) GROUP BY s.LOCATIONID""", day.isoformat())
        out["by_store"] = q(f"""SELECT s.LOCATIONID AS loc, SUM(c.QTY * c.SALEPRICE) AS rev {base} WHERE s.REPORTINGDATE = %s AND s.LOCATIONID IN ({L}) GROUP BY s.LOCATIONID""", day.isoformat())
        cur.execute("SELECT ID, NAME FROM LOCATION"); nm = {r["ID"]: r["NAME"] for r in cur.fetchall()}
        for r in out["unmapped"] + out["by_store"]:
            r["name"] = nm.get(int(float(r["loc"])))
    return out
