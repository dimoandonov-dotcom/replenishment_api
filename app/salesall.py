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
        "ALTER TABLE sa_promo ADD COLUMN IF NOT EXISTS tp SMALLINT",
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
        cur.execute("SELECT CATEGORY AS code, MAX(FULLNAME) AS fname, MAX(NAME) AS cname FROM CATEGORY GROUP BY CATEGORY")
        cname = {(r["code"] or "").strip(): ((r["fname"] or "").strip() or (r["cname"] or "").strip()) for r in cur.fetchall()}
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
                               COUNT(DISTINCT LOCATIONID) AS n, MIN(SALEPRICE) AS p, MAX(PERCENTAGEDISCOUNT) AS d,
                               MAX(ISNULL(TYPEPROMOTION, 0)) AS tp
                        FROM PROMOTIONSALEPRICE WITH (NOLOCK)
                        WHERE ENDDATE >= DATEADD(day, -{KEEP_DAYS}, GETDATE()) AND STARTDATE <= DATEADD(day, 30, GETDATE())
                          AND LOCATIONID IN ({L})
                        GROUP BY SALEMATERIALCODE, CAST(STARTDATE AS date), CAST(ENDDATE AS date)""")
        pr = [{"c": int(r["code"]), "s": r["s"], "e": r["e"], "n": int(r["n"] or 0), "p": float(r["p"] or 0),
               "d": float(r["d"] or 0), "tp": int(r["tp"] or 0)} for r in cur.fetchall()]
        db.execute(text("TRUNCATE sa_promo"))
        for j in range(0, len(pr), 5000):
            db.execute(text("""INSERT INTO sa_promo(code, start_day, end_day, stores, price, discount, tp)
                               VALUES (:c,:s,:e,:n,:p,:d,:tp) ON CONFLICT DO NOTHING"""), pr[j:j + 5000])
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


def campaigns(db: Session, kind: str = "brochure", back_days: int = 45) -> dict:
    """Акции от Мистрал по вид: brochure = брошура (TYPEPROMOTION=1), silent = тихи акции (0).
    За всяка кампания (период): продажби преди (14 дни) и по време, ръст, оборот, марж, свършили."""
    _ensure(db)
    last = db.execute(text("SELECT MAX(day) FROM sa_sales")).scalar()
    if not last:
        return {"ready": False, "campaigns": []}
    # периодите на брошурата: с поне 20 артикула с тип „брошура" (1); всичко със същите дати е брошура
    bro = {(s, e) for s, e in db.execute(text("""SELECT start_day, end_day FROM sa_promo WHERE tp = 1
                                                  GROUP BY start_day, end_day HAVING COUNT(*) >= 20""")).all()}
    camps = defaultdict(list)
    for code, s, e, n, p, d, t in db.execute(text("""SELECT code, start_day, end_day, stores, price, discount, COALESCE(tp,0)
                                                  FROM sa_promo WHERE start_day <= :t AND end_day >= :t - :b
                                                  ORDER BY start_day DESC"""), {"t": last, "b": back_days}).all():
        is_bro = t == 1 or (s, e) in bro
        if is_bro == (kind == "brochure"):
            camps[(s, e)].append((code, n, float(p or 0), float(d or 0)))
    names = {c: (n, g, sup) for c, n, g, sup in db.execute(text(
        "SELECT code, name, grp, supplier FROM sa_articles")).all()}
    outs_all = {c: n for c, n in db.execute(text("SELECT code, COUNT(*) FROM sa_stock WHERE qty <= 0 GROUP BY code")).all()}
    out = []
    for (s, e), items in sorted(camps.items(), key=lambda x: (x[0][0], x[0][1]), reverse=True):
        p_end = min(e, last); pdays = (p_end - s).days + 1
        if pdays <= 0:
            continue
        codes = [c for c, *_ in items]
        b0 = s - timedelta(days=14)
        agg = {c: [0.0, 0.0, 0.0, 0.0] for c in codes}
        for c, d, q, r, k in db.execute(text("""SELECT code, day, SUM(qty), SUM(rev), SUM(cost) FROM sa_sales
                                                WHERE code = ANY(:c) AND day BETWEEN :b AND :e GROUP BY code, day"""),
                                        {"c": codes, "b": b0, "e": p_end}).all():
            a = agg[c]
            if d < s: a[0] += float(q or 0)
            else: a[1] += float(q or 0); a[2] += float(r or 0); a[3] += float(k or 0)
        rows = []
        for c, n, price, disc in items:
            qb, qd, rd, kd = agg.get(c, [0, 0, 0, 0])
            bpd, dpd = qb / 14, qd / pdays
            nm, g, sup = names.get(c, (str(c), "", ""))
            rows.append({"code": c, "name": nm or str(c), "group": g or "", "supplier": sup or "", "stores": n,
                         "start": s.strftime("%d.%m"), "end": e.strftime("%d.%m"),
                         "promo_price": price, "discount": disc, "before_per_day": round(bpd, 1),
                         "during_per_day": round(dpd, 1), "uplift": round(dpd / bpd, 2) if bpd else None,
                         "units": round(qd), "extra_units": round(qd - bpd * pdays), "rev": round(rd, 2),
                         "margin_pct": round(100 * (rd - kd) / rd, 1) if rd else None,
                         "stores_out": outs_all.get(c, 0) if s <= last <= e else None})
        rows.sort(key=lambda r: -(r["rev"] or 0))
        out.append({"start": s.strftime("%d.%m.%Y"), "end": e.strftime("%d.%m.%Y"), "days_with_data": pdays,
                    "active": s <= last <= e, "items": len(rows), "units": sum(r["units"] for r in rows),
                    "extra_units": sum(r["extra_units"] for r in rows), "rev": round(sum(r["rev"] for r in rows), 2),
                    "rows": rows})
    return {"ready": True, "kind": kind, "data_until": last.strftime("%d.%m.%Y"), "campaigns": out}


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


# ------------------------------------------------------------------ топ артикули и аларми
LOW_COVER_DAYS = 2.0
TOP_N = 10


def _store_names(db):
    return {s.id: s.name for s in db.execute(select(m.Store).where(m.Store.is_active.is_(True))).scalars()}


def _per_store_article(db, days: int, store_id: int | None = None):
    """(store, code) -> [бройки, оборот, себестойност, бройки последна половина, бройки първа половина]."""
    since, last, half = _window(db, days)
    if not since:
        return None, None, None, {}
    q = """SELECT s.store_id, s.code, SUM(s.qty), SUM(s.rev), SUM(s.cost),
                  SUM(CASE WHEN s.day >= :h THEN s.qty ELSE 0 END), SUM(CASE WHEN s.day < :h THEN s.qty ELSE 0 END)
           FROM sa_sales s WHERE s.day BETWEEN :f AND :t""" + (" AND s.store_id = :sid" if store_id else "") + \
        " GROUP BY s.store_id, s.code"
    out = {(st, c): [float(a or 0), float(b or 0), float(k or 0), float(r or 0), float(p or 0)]
           for st, c, a, b, k, r, p in db.execute(text(q), {"f": since, "t": last, "h": half, "sid": store_id}).all()}
    return since, last, half, out


def _arts_meta(db):
    return {c: (n, g, s, sup) for c, n, g, s, sup in db.execute(text(
        "SELECT code, name, grp, sub, supplier FROM sa_articles")).all()}


def _stock_map(db, store_id: int | None = None):
    q = "SELECT store_id, code, qty FROM sa_stock" + (" WHERE store_id = :s" if store_id else "")
    return {(st, c): float(q_ or 0) for st, c, q_ in db.execute(text(q), {"s": store_id}).all()}


def articles(db: Session, days: int = 14, store_id: int | None = None, group: str | None = None,
             limit: int = 500) -> dict:
    """Анализ по артикули: веригата или един магазин."""
    _ensure(db)
    since, last, half, psa = _per_store_article(db, days, store_id)
    if not since:
        return {"ready": False}
    meta, stock = _arts_meta(db), _stock_map(db, store_id)
    rdays = (last - half).days + 1
    agg = defaultdict(lambda: [0.0] * 5)
    stores_sold = defaultdict(int)
    for (st, c), v in psa.items():
        a = agg[c]
        for i in range(5):
            a[i] += v[i]
        if v[0] > 0:
            stores_sold[c] += 1
    stk = defaultdict(float)
    for (st, c), q in stock.items():
        stk[c] += max(q, 0)
    rows = []
    for c, (q, r, k, qr, qp) in agg.items():
        n, g, sub, sup = meta.get(c, (str(c), "Без група", "", ""))
        if group and g != group:
            continue
        pdy = qr / rdays if rdays else 0
        rows.append({"code": c, "name": n or str(c), "group": g, "sub": sub, "supplier": sup, "units": round(q),
                     "rev": round(r, 2), "margin_pct": round(100 * (r - k) / r, 1) if r else None,
                     "trend": round(100 * (qr - qp) / qp, 1) if qp else None, "per_day": round(pdy, 2),
                     "stock": round(stk.get(c, 0)), "cover_days": round(stk.get(c, 0) / pdy, 1) if pdy else None,
                     "stores": stores_sold.get(c, 0)})
    rows.sort(key=lambda x: -x["rev"])
    for i, x in enumerate(rows, 1):
        x["rank"] = i
    return {"ready": True, "from": since.strftime("%d.%m"), "to": last.strftime("%d.%m"),
            "store": _store_names(db).get(store_id) if store_id else "Всички магазини",
            "count": len(rows), "rows": rows[:limit]}


def top_by_group(db: Session, days: int = 14, store_id: int | None = None, n: int = TOP_N) -> dict:
    """Топ N артикула (по оборот) във всяка група - за веригата или за един магазин."""
    a = articles(db, days, store_id, None, 100000)
    if not a.get("ready"):
        return {"ready": False}
    groups = defaultdict(list)
    for r in a["rows"]:
        groups[r["group"]].append(r)
    tot = {g: sum(x["rev"] for x in rows) for g, rows in groups.items()}
    out = []
    for g in sorted(groups, key=lambda g: -tot[g]):
        rows = groups[g][:n]
        for x in rows:
            deep = x["stock"] < -3 * (x["per_day"] or 0) * max(x["stores"], 1) / max(x["stores"], 1)
            x["alert"] = (None if deep else "свършил" if x["stock"] <= 0 else
                          "ниска" if x["cover_days"] is not None and x["cover_days"] < LOW_COVER_DAYS else None)
        out.append({"group": g, "rev": round(tot[g], 2), "articles": len(groups[g]), "top": rows})
    return {"ready": True, "from": a["from"], "to": a["to"], "store": a["store"], "groups": out}


def low_stock_alerts(db: Session, days: int = 14, n: int = TOP_N, cover: float = LOW_COVER_DAYS) -> dict:
    """Аларма: топ N артикула във всяка група на всеки магазин, които са свършили или стигат за < cover дни."""
    _ensure(db)
    since, last, half, psa = _per_store_article(db, days)
    if not since:
        return {"ready": False}
    meta, stock, stores = _arts_meta(db), _stock_map(db), _store_names(db)
    rdays = (last - half).days + 1
    per = defaultdict(list)   # (store, group) -> [(rev, code, v)]
    for (st, c), v in psa.items():
        if st not in stores:
            continue
        g = meta.get(c, (None, "Без група"))[1]
        per[(st, g)].append((v[1], c, v))
    # артикули, при които наличността реално не се води (доставките не се завеждат):
    # на минус в поне половината магазини, където се продават -> не са сигнал за свършване
    neg, nonpos, sold_in = defaultdict(int), defaultdict(int), defaultdict(int)
    for (st, c), v in psa.items():
        if v[0] > 0:
            sold_in[c] += 1
            q = stock.get((st, c), 0.0)
            if q < 0:
                neg[c] += 1
            if q <= 0:
                nonpos[c] += 1
    # на минус в половината магазини, или 0/минус в 80% от тях (кафе на чаша, топла точка, промо пакети)
    untracked = {c for c in sold_in if sold_in[c] >= 3 and
                 (neg[c] >= 0.5 * sold_in[c] or nonpos[c] >= 0.8 * sold_in[c])}
    # проверка с продажбите: продава ли се въпреки „0/минус"? -> стоката е на рафта, грешна е наличността
    recent = {(st, c): float(q or 0) for st, c, q in db.execute(text(
        "SELECT store_id, code, SUM(qty) FROM sa_sales WHERE day > :d GROUP BY store_id, code"),
        {"d": last - timedelta(days=2)}).all()}
    # последна продажба (за колко дни поред няма продажби до последния ден с данни)
    last_sale = {(st, c): d for st, c, d in db.execute(text(
        "SELECT store_id, code, MAX(day) FROM sa_sales WHERE qty > 0 AND day >= :f GROUP BY store_id, code"),
        {"f": since}).all()}
    import math
    skipped = 0
    alerts = []
    for (st, g), items in per.items():
        items.sort(key=lambda x: -x[0])
        for rank, (rev, c, v) in enumerate(items[:n], 1):
            # обичайна скорост: по-голямото от средното за целия период и за втората половина
            pdy = max(v[0] / days, (v[3] / rdays) if rdays else 0)
            if pdy <= 0:
                continue
            q = stock.get((st, c), 0.0)
            cd = q / pdy if pdy else None
            # дълбок минус (над 3 дни продажби) или артикул без водена наличност -> не е свършване
            if c in untracked or q < -3 * pdy:
                if q <= 0:
                    skipped += 1
                continue
            if q <= 0 or (cd is not None and cd < cover):
                nm, _, sub, sup = meta.get(c, (str(c), g, "", ""))
                rev_day = v[1] / days
                sold2 = recent.get((st, c), 0.0)        # продадено последните 2 дни
                ls = last_sale.get((st, c))
                gap = (last - ls).days if ls else days   # дни поред без продажба до последния ден с данни
                # шанс да няма нито една продажба за толкова дни, АКО стоката е на рафта (Поасон)
                p0 = math.exp(-pdy * gap) if gap > 0 else 1.0
                need = math.ceil(math.log(20) / pdy) if pdy > 0 else None   # дни без продажба за 95% сигурност
                if q <= 0:
                    if q < 0 and sold2 > 0:
                        check, verdict = "продава се въпреки минуса — стоката е там, доставката не е заведена", "грешна наличност"
                    elif q == 0 and sold2 > 0:
                        check, verdict = "свършил вчера — продаде последните бройки", "реално"
                    elif p0 < 0.05:
                        check, verdict = (f"сигурно свършил — {gap} дни без продажба, а обикновено продава "
                                          f"{pdy:.1f}/ден (шанс това да е случайно: {100 * p0:.0f}%)"), "реално"
                    else:
                        check, verdict = (f"не може да се каже — {gap} дни без продажба е нормално при "
                                          f"{pdy:.1f}/ден; сигурно ще е след {need} дни без продажба"), "несигурно"
                else:
                    check, verdict = "ще свърши скоро", "реално"
                alerts.append({"store_id": st, "store": stores[st], "group": g, "rank": rank, "code": c,
                               "name": nm or str(c), "supplier": sup, "per_day": round(pdy, 1), "stock": round(q, 1),
                               "cover_days": round(cd, 1) if cd is not None and q > 0 else 0,
                               "status": "свършил" if q <= 0 else "ниска", "rev_per_day": round(rev_day, 2),
                               "sold_2d": round(sold2, 1), "days_no_sale": gap if q <= 0 else None,
                               "check": check, "verdict": verdict})
    order = {"реално": 0, "несигурно": 1, "грешна наличност": 2}
    alerts.sort(key=lambda a: (a["status"] != "свършил", order.get(a["verdict"], 3), -a["rev_per_day"]))
    out_n = sum(1 for a in alerts if a["status"] == "свършил")
    wrong = [a for a in alerts if a["verdict"] == "грешна наличност"]
    real_out = [a for a in alerts if a["status"] == "свършил" and a["verdict"] == "реално"]
    return {"ready": True, "from": since.strftime("%d.%m"), "to": last.strftime("%d.%m"), "cover_threshold": cover,
            "total": len(alerts), "out": out_n, "low": len(alerts) - out_n,
            "lost_rev_per_day": round(sum(a["rev_per_day"] for a in real_out), 2),
            "real_out": len(real_out), "wrong_stock": len(wrong),
            "uncertain": sum(1 for a in alerts if a["verdict"] == "несигурно"),
            "real_out_none_2d": sum(1 for a in real_out if a["sold_2d"] == 0),
            "stores": len({a["store_id"] for a in alerts}), "untracked_articles": len(untracked),
            "untracked_positions": skipped, "rows": alerts[:1500],
            "untracked": sorted([{"code": c, "name": (meta.get(c) or (str(c),))[0] or str(c),
                                  "group": (meta.get(c) or (None, "Без група"))[1],
                                  "stores_sold": sold_in[c], "stores_negative": neg[c], "stores_zero_or_neg": nonpos[c],
                                  "rev": round(sum(v[1] for (st, cc), v in psa.items() if cc == c), 2)}
                                 for c in untracked], key=lambda x: -x["rev"])[:400]}
