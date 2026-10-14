"""
Предложения за делистване на база продажбите във веригата (всички доставчици).

Прозорец: всички налични дни продажби (до 60). За всеки артикул с наличност или продажби:
  мъртви    - 0 продажби за целия период, а има наличност в магазините
  бавни     - под 1 бр. на магазин на месец и в долните 5% от оборота на подгрупата си
  изпадащи  - последната половина на периода продава под 40% от първата, а и без това е слаб
Не се предлагат: нови артикули (първа продажба в последните 3 седмици), артикули в текуща
акция, сезонни групи и служебни групи. Решенията („делистваме“ / „оставяме“) се пазят.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

SOFIA = timezone(timedelta(hours=3))
SKIP_GROUPS = {"Без група", "Консумативи", "ПРОМОЦИИ", "Вестници-списания", "Кафемашина", "Топла точка"}
SEASONAL = {"Сладоледи", "Яйца и боя за яйца"}
SLOW_PER_STORE_MONTH = 1.0     # под 1 бр. на магазин на месец
TAIL_SHARE = 95.0              # в последните 5% от оборота на подгрупата
NEW_DAYS = 21
KEEP_HIDE_DAYS = 90
NSTORES = 52            # „оставяме“ скрива предложението за 90 дни

KINDS = {"dead": "Мъртъв — 0 продажби, има наличност",
         "slow": "Много бавен — под 1 бр./магазин на месец",
         "falling": "Изпада — продажбите спадат силно"}


def _ensure(db: Session):
    db.execute(text("""CREATE TABLE IF NOT EXISTS sa_dates (code INT PRIMARY KEY, last_delivery DATE, last_sale DATE,
        first_delivery_store DATE, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"""))
    db.execute(text("""CREATE TABLE IF NOT EXISTS delist_decisions (code INT PRIMARY KEY, decision TEXT NOT NULL,
        note TEXT, by_user TEXT, at TIMESTAMPTZ NOT NULL DEFAULT now())"""))
    db.commit()


_GENERIC = {"ЦИГАРИ", "ВИНО", "БИРА", "ВОДКА", "УИСКИ", "РАКИЯ", "ДЖИН", "КОНЯК", "БРЕНДИ", "ВОДА", "СОК", "ШОКОЛАД",
            "БОНБОНИ", "БИСКВИТИ", "ВАФЛА", "ВАФЛИ", "ЧИПС", "СИРЕНЕ", "КАШКАВАЛ", "САЛАМ", "КАФЕ", "ЧАЙ",
            "БОНБОНИЕРА", "ДЪВКИ", "НАПИТКА", "ЛИКЬОР", "МАСТИКА", "ШАМПОАН", "КРЕМ", "ПРАХ", "ПРЕПАРАТ"}


def _word(name: str | None) -> str:
    """Видът на артикула = първата дума (ЦИГАРИ, ВИНО, БИРА...)."""
    import re
    w = re.findall(r"[А-ЯA-Z]{3,}", (name or "").upper())
    return w[0] if w else ""


def _vol(name: str | None) -> float | None:
    """Разфасовка в мл/гр: 750МЛ, 0.5Л, 1,5Л, 150ГР, 1КГ."""
    import re
    g = re.search(r"(\d+(?:[.,]\d+)?)\s*(МЛ|Л|ГР|КГ|Г)(?![А-Я])", (name or "").upper())
    if not g:
        return None
    v = float(g.group(1).replace(",", "."))
    return v * (1000 if g.group(2) in ("Л", "КГ") else 1)


def _alts(o: dict, good: dict) -> list[dict]:
    """До 3 заместителя: същата подгрупа и същия вид (първа дума), сходна цена и разфасовка,
    продават поне 2 бр./магазин/месец. Първо тези, които още ги няма във всички магазини."""
    if not o["_word"]:
        return []
    pool = [g for g in good.get((o["grp"], o["sub"]), [])
            if g["code"] != o["code"] and g["word"] == o["_word"]]
    cost = o.get("_cost")
    if cost:
        pool = [g for g in pool if 0.5 * cost <= g["cost"] <= 2 * cost]
    v = _vol(o["name"])
    if v:
        pool = [g for g in pool if not g["vol"] or 0.5 * v <= g["vol"] <= 2 * v]
    pool.sort(key=lambda g: (g["stores"] >= NSTORES, -g["psm"]))
    res = []
    for g in pool[:3]:
        res.append({k: g[k] for k in ("code", "name", "psm", "stores", "price", "margin_pct", "supplier")} |
                   {"x": round(g["psm"] / o["per_store_month"], 1) if o["per_store_month"] > 0 else None,
                    "gain": round(g["psm"] - o["per_store_month"], 1), "missing_stores": max(NSTORES - g["stores"], 0)})
    return res


def sync_dates(db: Session, force: bool = False) -> int:
    """Последна доставка и последна продажба на всеки артикул (по всички магазини) от Мистрал."""
    _ensure(db)
    age = db.execute(text("SELECT EXTRACT(EPOCH FROM now() - MAX(updated_at)) FROM sa_dates")).scalar()
    if not force and age is not None and age < 6 * 3600:
        return 0
    from . import mistral
    from .anomalies import _loc_map
    with mistral.connect() as conn:
        cur = conn.cursor()
        L = ",".join(map(str, _loc_map(db, cur)))
        cur.execute(f"""SELECT MATERIALCODE AS code, MAX(CAST(LASTDELIVERYDATE AS date)) AS ld,
                               MAX(CAST(LASTSALEDATE AS date)) AS ls
                        FROM MATERIAL WITH (NOLOCK) WHERE LOCATIONID IN ({L}) GROUP BY MATERIALCODE""")
        rows = [{"c": int(r["code"]), "d": r["ld"], "s": r["ls"]} for r in cur.fetchall()]
    db.execute(text("TRUNCATE sa_dates"))
    for j in range(0, len(rows), 5000):
        db.execute(text("INSERT INTO sa_dates(code, last_delivery, last_sale) VALUES (:c, :d, :s)"), rows[j:j + 5000])
    db.commit()
    return len(rows)


def proposals(db: Session, include_seasonal: bool = False) -> dict:
    from .salesall import _ensure as _sa
    _sa(db); _ensure(db)
    try:
        sync_dates(db)
    except Exception:
        db.rollback()
    first, last = db.execute(text("SELECT MIN(day), MAX(day) FROM sa_sales")).one()
    if not last:
        return {"ready": False, "rows": []}
    since = max(first, last - timedelta(days=59))
    days = (last - since).days + 1
    mid = since + timedelta(days=days // 2)
    today = datetime.now(SOFIA).date()
    rows = db.execute(text("""
        WITH s AS (SELECT code, SUM(qty) q, SUM(rev) r, SUM(cost) c, COUNT(DISTINCT store_id) ns,
                          MIN(day) f, MAX(day) l,
                          SUM(CASE WHEN day < :mid THEN qty ELSE 0 END) q1,
                          SUM(CASE WHEN day >= :mid THEN qty ELSE 0 END) q2
                   FROM sa_sales WHERE day BETWEEN :f AND :t GROUP BY code),
             st AS (SELECT code, SUM(GREATEST(qty,0)) sq, SUM(GREATEST(qty,0) * COALESCE(cost,0)) sv,
                           COUNT(*) FILTER (WHERE qty > 0) nst
                    FROM sa_stock GROUP BY code)
        SELECT a.code, a.name, a.grp, a.sub, a.supplier, COALESCE(s.q,0), COALESCE(s.r,0), COALESCE(s.c,0),
               COALESCE(s.ns,0), s.f, s.l, COALESCE(s.q1,0), COALESCE(s.q2,0),
               COALESCE(st.sq,0), COALESCE(st.sv,0), COALESCE(st.nst,0), dt.last_delivery, dt.last_sale
        FROM sa_articles a LEFT JOIN s ON s.code = a.code LEFT JOIN st ON st.code = a.code
        LEFT JOIN sa_dates dt ON dt.code = a.code
        WHERE COALESCE(st.sq,0) > 0 OR COALESCE(s.q,0) > 0"""),
        {"f": since, "t": last, "mid": mid}).all()
    global NSTORES
    NSTORES = db.execute(text("SELECT COUNT(DISTINCT store_id) FROM sa_stock")).scalar() or 52
    promo = {c for (c,) in db.execute(text("SELECT DISTINCT code FROM sa_promo WHERE end_day >= :d AND start_day <= :d"),
                                      {"d": today}).all()}
    dec = {c: (d, n, b, at) for c, d, n, b, at in db.execute(text(
        "SELECT code, decision, note, by_user, at FROM delist_decisions")).all()}
    # дял в оборота на подгрупата (кумулативно, от най-големия)
    by_sub = defaultdict(list)
    for r in rows:
        by_sub[(r[2] or "", r[3] or "")].append((float(r[6] or 0), r[0]))
    cum = {}
    for k, lst in by_sub.items():
        tot = sum(x for x, _ in lst) or 1.0
        acc = 0.0
        for rev, code in sorted(lst, key=lambda x: -x[0]):
            acc += rev
            cum[code] = (round(100 * rev / tot, 2), round(100 * acc / tot, 1), len(lst))
    # силни артикули по подгрупа - кандидати за заместители
    good = defaultdict(list)
    for (code, name, grp, sub, sup, q, r, c, ns, f, l, q1, q2, sq, sv, nst, ldel, lsale) in rows:
        q, r, c = float(q or 0), float(r or 0), float(c or 0)
        if q <= 0 or (grp or "") in SKIP_GROUPS:
            continue
        stores_ = max(int(nst or 0), int(ns or 0), 1)
        psm = q / stores_ / days * 30
        if psm < 2:
            continue
        good[(grp or "", sub or "")].append({"code": code, "name": name or str(code), "word": _word(name), "vol": _vol(name),
                                             "psm": round(psm, 1), "stores": stores_, "stores_sold": int(ns or 0),
                                             "price": round(r / q, 2), "cost": c / q,
                                             "margin_pct": round(100 * (r - c) / r, 1) if r else None,
                                             "supplier": sup or ""})
    out = []
    stats = defaultdict(lambda: [0, 0.0])
    for (code, name, grp, sub, sup, q, r, c, ns, f, l, q1, q2, sq, sv, nst, ldel, lsale) in rows:
        grp = grp or "Без група"
        if grp in SKIP_GROUPS or (grp in SEASONAL and not include_seasonal):
            continue
        if "АМБАЛАЖ" in (name or "").upper() or (name or "").upper().startswith(("ТОРБА", "ТОРБИЧКА")):
            continue
        q, r, c, q1, q2, sq, sv = map(lambda x: float(x or 0), (q, r, c, q1, q2, sq, sv))
        stores = max(int(nst or 0), int(ns or 0), 1)
        per_store_month = q / stores / days * 30
        share, cshare, nsub = cum.get(code, (0.0, 100.0, 1))
        is_new = f is not None and (f - since).days >= days - NEW_DAYS and q > 0
        # нов/току-що зареден: доставка в последните 3 седмици и още без продажби -> не е мъртъв
        fresh = ldel is not None and (today - ldel).days <= NEW_DAYS and q <= 0
        # продажба след последния ден в базата (днес) - продава се
        sold_now = lsale is not None and lsale > last
        kind = None
        if fresh or sold_now:
            kind = None
        elif q <= 0 and sq > 0:
            kind = "dead"
        elif is_new or code in promo:
            kind = None
        elif per_store_month < SLOW_PER_STORE_MONTH and cshare > TAIL_SHARE:
            kind = "slow"
        elif q1 > 0 and q2 < q1 * 0.4 and per_store_month < 3 and cshare > 80:
            kind = "falling"
        if not kind:
            continue
        d = dec.get(code)
        if d and d[0] == "keep" and (datetime.now(timezone.utc) - d[3]).days < KEEP_HIDE_DAYS:
            continue
        daily = q / days
        if kind == "dead":
            eur = f"{sv:,.0f}".replace(",", " ")
            ls_txt = f"последна продажба {lsale.strftime('%d.%m.%Y')}" if lsale else "няма продажба в Мистрал"
            ld_txt = f", последна доставка {ldel.strftime('%d.%m.%Y')}" if ldel else ""
            reason = f"0 продажби за {days} дни ({ls_txt}{ld_txt}), а има {sq:g} бр. в {nst} маг. ({eur} € замразени)"
        elif kind == "slow":
            reason = (f"{q:g} бр. за {days} дни в {stores} маг. = {per_store_month:.2f} бр./магазин на месец; "
                      f"{share:.1f}% от оборота на подгрупата")
        else:
            reason = (f"първа половина {q1:g} бр. → втора {q2:g} бр. ({100 * (q2 - q1) / q1:+.0f}%); "
                      f"{per_store_month:.1f} бр./магазин на месец")
        stats[kind][0] += 1; stats[kind][1] += sv
        out.append({"_cost": (c / q) if q > 0 else (sv / sq if sq > 0 else None), "_word": _word(name),
                    "price": round(r / q, 2) if q > 0 else None,
                    "code": code, "name": name or str(code), "grp": grp, "sub": sub or "", "supplier": sup or "",
                    "kind": kind, "reason": reason, "units": round(q, 1), "rev": round(r, 2),
                    "margin_pct": round(100 * (r - c) / r, 1) if r else None,
                    "stores_sold": int(ns or 0), "stores_stock": int(nst or 0), "per_store_month": round(per_store_month, 2),
                    "stock": round(sq, 1), "stock_eur": round(sv, 2),
                    "cover_days": round(sq / daily) if daily > 0 else None,
                    "last_sale": (lsale or l).strftime("%d.%m.%y") if (lsale or l) else None,
                    "last_delivery": ldel.strftime("%d.%m.%y") if ldel else None, "sub_share": share, "sub_items": nsub,
                    "decision": d[0] if d else None, "decision_by": d[2] if d else None,
                    "decision_at": d[3].astimezone(SOFIA).strftime("%d.%m.%Y") if d else None})
    for o in out:
        o["alts"] = _alts(o, good)
        o.pop("_cost", None); o.pop("_word", None)
    order = {"dead": 0, "slow": 1, "falling": 2}
    out.sort(key=lambda x: (order[x["kind"]], -x["stock_eur"]))
    return {"ready": True, "from": since.strftime("%d.%m"), "to": last.strftime("%d.%m.%Y"), "days": days,
            "kinds": KINDS, "stats": {k: {"n": v[0], "stock_eur": round(v[1], 2)} for k, v in stats.items()},
            "rows": out}


def decide(db: Session, code: int, decision: str, note: str | None, who: str | None) -> dict:
    _ensure(db)
    if decision == "clear":
        db.execute(text("DELETE FROM delist_decisions WHERE code = :c"), {"c": code})
    else:
        db.execute(text("""INSERT INTO delist_decisions(code, decision, note, by_user) VALUES (:c,:d,:n,:b)
                           ON CONFLICT (code) DO UPDATE SET decision = EXCLUDED.decision, note = EXCLUDED.note,
                           by_user = EXCLUDED.by_user, at = now()"""),
                   {"c": code, "d": decision, "n": note, "b": who})
    db.commit()
    return {"ok": True}


def article(db: Session, code: int) -> dict:
    """Подробно за един артикул: по магазини (наличност, продажби, последна продажба/доставка) и по седмици."""
    from . import mistral
    from .anomalies import _loc_map
    from . import models as m
    from sqlalchemy import select
    stores = {s.id: s.name for s in db.execute(select(m.Store)).scalars()}
    last = db.execute(text("SELECT MAX(day) FROM sa_sales")).scalar()
    since = last - timedelta(days=59)
    sold = {sid: (float(q or 0), float(r or 0)) for sid, q, r in db.execute(text(
        "SELECT store_id, SUM(qty), SUM(rev) FROM sa_sales WHERE code = :c AND day >= :f GROUP BY store_id"),
        {"c": code, "f": since}).all()}
    weeks = [{"from": (since + timedelta(days=7 * i)).strftime("%d.%m"), "qty": 0.0} for i in range(9)]
    for d, q in db.execute(text("SELECT day, SUM(qty) FROM sa_sales WHERE code = :c AND day >= :f GROUP BY day"),
                           {"c": code, "f": since}).all():
        i = min((d - since).days // 7, 8)
        weeks[i]["qty"] += float(q or 0)
    per = {}
    try:
        with mistral.connect() as conn:
            cur = conn.cursor()
            locs = _loc_map(db, cur)
            L = ",".join(map(str, locs))
            cur.execute(f"""SELECT LOCATIONID AS loc, SUM(QTY) AS q, MAX(LASTSALEDATE) AS ls, MAX(LASTDELIVERYDATE) AS ld,
                                   MAX(AVGDELIVERYPRICE) AS p, MAX(MATERIAL) AS nm
                            FROM MATERIAL WITH (NOLOCK) WHERE MATERIALCODE = %s AND LOCATIONID IN ({L})
                            GROUP BY LOCATIONID""", (code,))
            for r in cur.fetchall():
                sid = locs.get(r["loc"])
                if sid is None:
                    continue
                per[sid] = {"stock": float(r["q"] or 0), "last_sale": r["ls"], "last_delivery": r["ld"],
                            "price": float(r["p"] or 0)}
    except Exception as e:
        per = {}
    rows = []
    for sid in set(per) | set(sold):
        p = per.get(sid, {})
        q, r = sold.get(sid, (0.0, 0.0))
        if not p.get("stock") and not q:
            continue
        rows.append({"store": stores.get(sid, str(sid)), "stock": round(p.get("stock", 0), 2), "sold": round(q, 2),
                     "rev": round(r, 2),
                     "last_sale": p["last_sale"].strftime("%d.%m.%Y") if p.get("last_sale") else None,
                     "last_delivery": p["last_delivery"].strftime("%d.%m.%Y") if p.get("last_delivery") else None})
    rows.sort(key=lambda x: (-x["sold"], -x["stock"]))
    nm = db.execute(text("SELECT name FROM sa_articles WHERE code = :c"), {"c": code}).scalar()
    return {"code": code, "name": nm, "weeks": [{**w, "qty": round(w["qty"], 1)} for w in weeks], "stores": rows}


def store_swaps(db: Session, store_id: int) -> dict:
    """За един магазин: слабите артикули, които държи, и с какво да се заменят (силни в същата подгрупа, които го няма)."""
    p = proposals(db)
    if not p.get("ready"):
        return p
    last = db.execute(text("SELECT MAX(day) FROM sa_sales")).scalar()
    since = last - timedelta(days=p["days"] - 1)
    st = {c: float(q or 0) for c, q in db.execute(text(
        "SELECT code, qty FROM sa_stock WHERE store_id = :s"), {"s": store_id}).all()}
    sold = {c: float(q or 0) for c, q in db.execute(text(
        "SELECT code, SUM(qty) FROM sa_sales WHERE store_id = :s AND day >= :f GROUP BY code"),
        {"s": store_id, "f": since}).all()}
    rows = []
    for r in p["rows"]:
        if st.get(r["code"], 0) <= 0:
            continue
        alts = [a for a in r["alts"] if st.get(a["code"], 0) <= 0 and sold.get(a["code"], 0) <= 0]
        rows.append({**r, "store_stock": st.get(r["code"], 0), "store_sold": sold.get(r["code"], 0),
                     "store_alts": alts})
    rows.sort(key=lambda x: (not x["store_alts"], -x["stock_eur"]))
    from . import models as m
    s = db.get(m.Store, store_id)
    return {"ready": True, "store": s.name if s else "", "days": p["days"], "rows": rows,
            "with_alts": sum(1 for x in rows if x["store_alts"])}
