"""
Продажби, наличности и промоции на ВСИЧКИ доставчици (не само НДК) - за анализите.
Данните се дърпат от Мистрал всяка нощ (последните 3 дни) и при първо пускане (30 дни).
  sa_sales    - магазин × артикул × ден: бройки, оборот, себестойност
  sa_articles - артикул: име, група (от дървото на Мистрал), доставчик
  sa_stock    - текуща наличност на всеки артикул по магазин (с доставна цена)
  sa_promo    - промоции от Мистрал (PROMOTIONSALEPRICE) по артикул и период
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import models as m

SOFIA = timezone(timedelta(hours=3))
KEEP_DAYS = 60
_job = {"running": False, "last": None, "error": None, "progress": ""}


def _ensure(db: Session):
    for q in [
        """CREATE TABLE IF NOT EXISTS sa_sales (store_id INT NOT NULL, code INT NOT NULL, day DATE NOT NULL,
             qty NUMERIC(14,3) NOT NULL, rev NUMERIC(14,2) NOT NULL, cost NUMERIC(14,2) NOT NULL,
             PRIMARY KEY (day, store_id, code))""",
        "CREATE INDEX IF NOT EXISTS ix_sa_sales_code ON sa_sales(code)",
        """CREATE TABLE IF NOT EXISTS sa_articles (code INT PRIMARY KEY, name TEXT, grp TEXT, sub TEXT,
             supplier TEXT, updated_at TIMESTAMPTZ DEFAULT now())""",
        """CREATE TABLE IF NOT EXISTS sa_stock (store_id INT NOT NULL, code INT NOT NULL, qty NUMERIC(14,3) NOT NULL,
             cost NUMERIC(14,4), PRIMARY KEY (store_id, code))""",
        """CREATE TABLE IF NOT EXISTS sa_promo (code INT NOT NULL, start_day DATE NOT NULL, end_day DATE NOT NULL,
             stores INT, price NUMERIC(12,4), discount NUMERIC(8,2), PRIMARY KEY (code, start_day, end_day))""",
    ]:
        db.execute(text(q))
    db.commit()


def _bulk(db: Session, sql: str, rows: list[tuple]):
    """Бързо групово вмъкване (psycopg2 execute_values)."""
    if not rows:
        return
    from psycopg2.extras import execute_values
    raw = db.connection().connection
    with raw.cursor() as c:
        execute_values(c, sql, rows, page_size=5000)


def status() -> dict:
    return dict(_job)


def _sync(db: Session, days: int):
    from . import mistral
    from .anomalies import _loc_map
    _ensure(db)
    t0 = time.time()
    today = datetime.now(SOFIA).date()
    with mistral.connect() as conn:
        cur = conn.cursor()
        locs = _loc_map(db, cur)
        L = ",".join(map(str, locs))
        names, cats, parts = {}, {}, {}
        for i in range(days, 0, -1):
            d = today - timedelta(days=i)
            _job["progress"] = f"продажби {d.strftime('%d.%m')}"
            cur.execute(f"""
                SELECT c.LOCATIONID AS loc, c.MATERIALCODE AS code, SUM(c.QTY) AS q,
                       SUM(c.QTY * c.SALEPRICE) AS rev, SUM(c.QTY * c.AVGDELIVERYPRICE) AS cost,
                       MAX(c.MATERIALNAMEID) AS nid, MAX(c.CATEGORYID) AS cid, MAX(c.LASTPARTNERNAMEID) AS pid
                FROM SALE s WITH (NOLOCK)
                JOIN SALECONTENT c WITH (NOLOCK) ON c.NUM = s.NUM AND c.LOCATIONID = s.LOCATIONID
                WHERE s.REPORTINGDATE = %s AND s.LOCATIONID IN ({L})
                GROUP BY c.LOCATIONID, c.MATERIALCODE""", (d.isoformat(),))
            rows = cur.fetchall()
            db.execute(text("DELETE FROM sa_sales WHERE day = :d"), {"d": d})
            batch = []
            for r in rows:
                sid = locs.get(r["loc"])
                if sid is None:
                    continue
                code = int(r["code"])
                batch.append((sid, code, d, float(r["q"] or 0), float(r["rev"] or 0), float(r["cost"] or 0)))
                if r["nid"]: names[code] = int(r["nid"])
                if r["cid"]: cats[code] = (r["loc"], int(r["cid"]))
                if r["pid"]: parts[code] = int(r["pid"])
            _bulk(db, "INSERT INTO sa_sales(store_id, code, day, qty, rev, cost) VALUES %s", batch)
            db.commit()
        # речници: имена, групи, доставчици
        _job["progress"] = "имена, групи, доставчици"
        nm = {}
        ids = sorted(set(names.values()))
        for j in range(0, len(ids), 1000):
            cur.execute(f"SELECT ID, MATERIAL FROM MATERIALNAME WHERE ID IN ({','.join(map(str, ids[j:j+1000]))})")
            nm.update({r["ID"]: r["MATERIAL"] for r in cur.fetchall()})
        # група: кодът при артикула (MATERIAL.CATEGORY, напр. "1.76.") -> име/път от CATEGORY
        cur.execute("SELECT CATEGORY AS code, MAX(FULLNAME) AS full, MAX(NAME) AS name FROM CATEGORY GROUP BY CATEGORY")
        cname = {(r["code"] or "").strip(): ((r["full"] or "").strip() or (r["name"] or "").strip()) for r in cur.fetchall()}
        cur.execute(f"""SELECT MATERIALCODE AS code, MAX(CATEGORY) AS cat, MAX(SEARCHNAME) AS nm
                        FROM MATERIAL WITH (NOLOCK) WHERE LOCATIONID IN ({L}) GROUP BY MATERIALCODE""")
        mcat = {int(r["code"]): ((r["cat"] or "").strip(), r["nm"]) for r in cur.fetchall()}
        pn = {}
        pids = sorted(set(parts.values()))
        for j in range(0, len(pids), 1000):
            cur.execute(f"SELECT ID, PARTNERNAME FROM PARTNERNAME WHERE ID IN ({','.join(map(str, pids[j:j+1000]))})")
            pn.update({r["ID"]: r["PARTNERNAME"] for r in cur.fetchall()})

        def path(code: str) -> list[str]:
            full = cname.get(code, "")
            if full:
                return [x.strip() for x in full.strip("/").split("/") if x.strip()]
            parts_ = [p for p in code.split(".") if p]      # "1.76." -> ["1.", "1.76."]
            return [cname.get(".".join(parts_[:i + 1]) + ".", "") for i in range(len(parts_)) if cname.get(".".join(parts_[:i + 1]) + ".")]

        arts = []
        for code in set(names) | set(parts) | set(mcat):
            cat, mnm = mcat.get(code, ("", None))
            seg = path(cat) if cat else []
            arts.append({"c": code, "n": nm.get(names.get(code)) or mnm, "g": seg[0] if seg else "Без група",
                         "s": seg[1] if len(seg) > 1 else (seg[0] if seg else "Без група"),
                         "p": pn.get(parts.get(code)) or "Неизвестен"})
        for j in range(0, len(arts), 2000):
            db.execute(text("""INSERT INTO sa_articles(code, name, grp, sub, supplier) VALUES (:c,:n,:g,:s,:p)
                               ON CONFLICT (code) DO UPDATE SET name=COALESCE(EXCLUDED.name, sa_articles.name),
                               grp=EXCLUDED.grp, sub=EXCLUDED.sub, supplier=EXCLUDED.supplier, updated_at=now()"""),
                       arts[j:j + 2000])
        db.commit()
        # наличности на всички артикули
        _job["progress"] = "наличности"
        cur.execute(f"""SELECT LOCATIONID AS loc, MATERIALCODE AS code, SUM(QTY) AS q, MAX(AVGDELIVERYPRICE) AS p
                        FROM MATERIAL WITH (NOLOCK) WHERE LOCATIONID IN ({L}) AND QTY <> 0
                        GROUP BY LOCATIONID, MATERIALCODE""")
        st = [{"s": locs[r["loc"]], "c": int(r["code"]), "q": float(r["q"] or 0), "p": float(r["p"] or 0)}
              for r in cur.fetchall() if r["loc"] in locs]
        db.execute(text("TRUNCATE sa_stock"))
        agg = {}
        for x in st:
            k = (x["s"], x["c"]); q, p = agg.get(k, (0.0, 0.0)); agg[k] = (q + x["q"], max(p, x["p"]))
        _bulk(db, "INSERT INTO sa_stock(store_id, code, qty, cost) VALUES %s",
              [(k[0], k[1], v[0], v[1]) for k, v in agg.items()])
        db.commit()
        # промоции от Мистрал
        _job["progress"] = "промоции"
        cur.execute(f"""SELECT SALEMATERIALCODE AS code, CAST(STARTDATE AS date) AS s, CAST(ENDDATE AS date) AS e,
                               COUNT(DISTINCT LOCATIONID) AS n, MIN(SALEPRICE) AS p, MAX(PERCENTAGEDISCOUNT) AS d
                        FROM PROMOTIONSALEPRICE WITH (NOLOCK)
                        WHERE ENDDATE >= DATEADD(day, -{KEEP_DAYS}, GETDATE()) AND STARTDATE <= DATEADD(day, 30, GETDATE())
                          AND LOCATIONID IN ({L})
                        GROUP BY SALEMATERIALCODE, CAST(STARTDATE AS date), CAST(ENDDATE AS date)""")
        pr = [{"c": int(r["code"]), "s": r["s"], "e": r["e"], "n": int(r["n"] or 0), "p": float(r["p"] or 0),
               "d": float(r["d"] or 0)} for r in cur.fetchall()]
        db.execute(text("TRUNCATE sa_promo"))
        for j in range(0, len(pr), 5000):
            db.execute(text("""INSERT INTO sa_promo(code, start_day, end_day, stores, price, discount)
                               VALUES (:c,:s,:e,:n,:p,:d) ON CONFLICT DO NOTHING"""), pr[j:j + 5000])
        db.commit()
    db.execute(text("DELETE FROM sa_sales WHERE day < :d"), {"d": today - timedelta(days=KEEP_DAYS)})
    db.commit()
    return {"days": days, "articles": len(arts), "stock_rows": len(st), "promo_rows": len(pr),
            "seconds": round(time.time() - t0, 1)}


def sync(db: Session, days: int = 3) -> dict:
    """Синхронно (за нощната задача)."""
    return _sync(db, days)


def start_background(days: int = 30) -> dict:
    if _job["running"]:
        return {"started": False, "status": status()}
    from .db import SessionLocal

    def run():
        _job.update(running=True, error=None, progress="старт")
        try:
            with SessionLocal() as db:
                _job["last"] = _sync(db, days)
        except Exception as e:
            _job["error"] = f"{type(e).__name__}: {e}"[:400]
        finally:
            _job.update(running=False, progress="")
    threading.Thread(target=run, daemon=True).start()
    return {"started": True}


# ------------------------------------------------------------------ анализи
def _window(db, days):
    last = db.execute(text("SELECT MAX(day) FROM sa_sales")).scalar()
    if not last:
        return None, None, None
    since = last - timedelta(days=days - 1)
    return since, last, since + timedelta(days=days // 2)


def overview(db: Session, days: int = 14, by: str = "grp") -> dict:
    """by: grp (група) | supplier (доставчик) | store (магазин)."""
    _ensure(db)
    since, last, half = _window(db, days)
    if not since:
        return {"ready": False}
    key = {"grp": "a.grp", "supplier": "a.supplier", "store": "s.store_id"}[by]
    rows = db.execute(text(f"""
        SELECT {key} AS k, SUM(s.qty) q, SUM(s.rev) r, SUM(s.cost) c,
               SUM(CASE WHEN s.day >= :h THEN s.qty ELSE 0 END) qr,
               SUM(CASE WHEN s.day <  :h THEN s.qty ELSE 0 END) qp,
               SUM(CASE WHEN s.day >= :h THEN s.rev ELSE 0 END) rr,
               SUM(CASE WHEN s.day <  :h THEN s.rev ELSE 0 END) rp,
               COUNT(DISTINCT s.code) n
        FROM sa_sales s LEFT JOIN sa_articles a ON a.code = s.code
        WHERE s.day BETWEEN :f AND :t GROUP BY {key}"""), {"f": since, "t": last, "h": half}).all()
    skey = {"grp": "a.grp", "supplier": "a.supplier", "store": "st.store_id"}[by]
    stock = {k: (float(q or 0), float(v or 0)) for k, q, v in db.execute(text(f"""
        SELECT {skey}, SUM(GREATEST(st.qty,0)), SUM(GREATEST(st.qty,0) * COALESCE(st.cost,0))
        FROM sa_stock st LEFT JOIN sa_articles a ON a.code = st.code GROUP BY {skey}""")).all()}
    stores = {s.id: s.name for s in db.execute(select(m.Store)).scalars()}
    rdays = (last - half).days + 1
    tot = {"units": 0, "rev": 0, "margin": 0}
    out = []
    for k, q, r, c, qr, qp, rr, rp, n in rows:
        q, r, c = float(q or 0), float(r or 0), float(c or 0)
        sq, sv = stock.get(k, (0, 0))
        pd = float(qr or 0) / rdays if rdays else 0
        tot["units"] += q; tot["rev"] += r; tot["margin"] += r - c
        out.append({"key": k, "name": stores.get(k, k) if by == "store" else (k or "—"), "units": round(q),
                    "rev": round(r, 2), "margin": round(r - c, 2), "margin_pct": round(100 * (r - c) / r, 1) if r else None,
                    "trend": round(100 * (float(rr or 0) - float(rp or 0)) / float(rp), 1) if rp else None,
                    "articles": int(n), "stock": round(sq), "stock_eur": round(sv, 2),
                    "cover_days": round(sq / pd, 1) if pd else None})
    out.sort(key=lambda x: -x["rev"])
    for x in out:
        x["share"] = round(100 * x["rev"] / tot["rev"], 1) if tot["rev"] else 0
    return {"ready": True, "from": since.strftime("%d.%m"), "to": last.strftime("%d.%m"), "by": by,
            "totals": {k: round(v, 2) for k, v in tot.items()} | {"margin_pct": round(100 * tot["margin"] / tot["rev"], 1) if tot["rev"] else None},
            "rows": out}


def detail(db: Session, by: str, key: str, days: int = 14) -> dict:
    since, last, half = _window(db, days)
    cond = {"grp": "a.grp = :k", "supplier": "a.supplier = :k", "store": "s.store_id = CAST(:k AS INT)"}[by]
    rows = db.execute(text(f"""
        SELECT s.code, MAX(a.name), MAX(a.sub), MAX(a.supplier), SUM(s.qty), SUM(s.rev), SUM(s.cost),
               SUM(CASE WHEN s.day >= :h THEN s.qty ELSE 0 END), SUM(CASE WHEN s.day < :h THEN s.qty ELSE 0 END)
        FROM sa_sales s LEFT JOIN sa_articles a ON a.code = s.code
        WHERE s.day BETWEEN :f AND :t AND {cond} GROUP BY s.code ORDER BY SUM(s.rev) DESC LIMIT 400"""),
        {"f": since, "t": last, "h": half, "k": key}).all()
    scond = "st.store_id = CAST(:k AS INT)" if by == "store" else ("a.grp = :k" if by == "grp" else "a.supplier = :k")
    stock = {c: float(q or 0) for c, q in db.execute(text(f"""SELECT st.code, SUM(GREATEST(st.qty,0)) FROM sa_stock st
        LEFT JOIN sa_articles a ON a.code = st.code WHERE {scond} GROUP BY st.code"""), {"k": key}).all()}
    rdays = (last - half).days + 1
    arts = []
    for code, n, sub, sup, q, r, c, qr, qp in rows:
        q, r, c = float(q or 0), float(r or 0), float(c or 0)
        pd = float(qr or 0) / rdays
        sq = stock.get(code, 0)
        arts.append({"code": code, "name": n or str(code), "sub": sub, "supplier": sup, "units": round(q),
                     "rev": round(r, 2), "margin_pct": round(100 * (r - c) / r, 1) if r else None,
                     "trend": round(100 * (float(qr or 0) - float(qp or 0)) / float(qp), 1) if qp else None,
                     "stock": round(sq), "cover_days": round(sq / pd, 1) if pd else None})
    return {"by": by, "key": key, "from": since.strftime("%d.%m"), "to": last.strftime("%d.%m"), "articles": arts}


def promos(db: Session) -> dict:
    """Промоциите от Мистрал (последните 60 дни и текущите): ефект по артикул."""
    _ensure(db)
    last = db.execute(text("SELECT MAX(day) FROM sa_sales")).scalar()
    if not last:
        return {"ready": False, "campaigns": []}
    camps = defaultdict(list)
    for code, s, e, n, p, d in db.execute(text("""SELECT code, start_day, end_day, stores, price, discount FROM sa_promo
                                                  WHERE start_day <= :t AND end_day >= :t - 45
                                                  ORDER BY start_day DESC""" ), {"t": last}).all():
        camps[(s, e)].append((code, n, float(p or 0), float(d or 0)))
    out = []
    for (s, e), items in sorted(camps.items(), key=lambda x: x[0][0], reverse=True)[:12]:
        codes = [c for c, *_ in items]
        p_end = min(e, last); pdays = (p_end - s).days + 1
        if pdays <= 0:
            continue
        b0 = s - timedelta(days=14)
        agg = {c: [0.0, 0.0, 0.0, 0.0] for c in codes}   # q_before, q_during, rev_during, cost_during
        for c, d, q, r, k in db.execute(text("""SELECT code, day, SUM(qty), SUM(rev), SUM(cost) FROM sa_sales
                                                WHERE code = ANY(:c) AND day BETWEEN :b AND :e GROUP BY code, day"""),
                                        {"c": codes, "b": b0, "e": p_end}).all():
            a = agg[c]
            if d < s: a[0] += float(q or 0)
            else: a[1] += float(q or 0); a[2] += float(r or 0); a[3] += float(k or 0)
        names = {c: (n, g, sup) for c, n, g, sup in db.execute(text(
            "SELECT code, name, grp, supplier FROM sa_articles WHERE code = ANY(:c)"), {"c": codes}).all()}
        outs = {c: n for c, n in db.execute(text("""SELECT st.code, COUNT(*) FROM sa_stock st
            WHERE st.code = ANY(:c) AND st.qty <= 0 GROUP BY st.code"""), {"c": codes}).all()}
        rows = []
        for c, n, price, disc in items:
            qb, qd, rd, kd = agg.get(c, [0, 0, 0, 0])
            bpd, dpd = qb / 14, qd / pdays
            nm, g, sup = names.get(c, (str(c), "", ""))
            rows.append({"code": c, "name": nm or str(c), "group": g, "supplier": sup, "stores": n,
                         "promo_price": price, "discount": disc, "before_per_day": round(bpd, 1),
                         "during_per_day": round(dpd, 1), "uplift": round(dpd / bpd, 2) if bpd else None,
                         "units": round(qd), "extra_units": round(qd - bpd * pdays), "rev": round(rd, 2),
                         "margin_pct": round(100 * (rd - kd) / rd, 1) if rd else None, "stores_out": outs.get(c, 0)})
        rows.sort(key=lambda r: -(r["rev"] or 0))
        out.append({"start": s.strftime("%d.%m.%Y"), "end": e.strftime("%d.%m.%Y"), "days_with_data": pdays,
                    "active": s <= last <= e, "items": len(rows), "units": sum(r["units"] for r in rows),
                    "extra_units": sum(r["extra_units"] for r in rows), "rev": round(sum(r["rev"] for r in rows), 2),
                    "rows": rows[:300]})
    return {"ready": True, "data_until": last.strftime("%d.%m.%Y"), "campaigns": out}
