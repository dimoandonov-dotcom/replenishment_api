"""
Аномалии в наличностите по магазини.

Всяка нощ (с ученето) дърпа от Мистрал за НДК артикулите:
  - всички движения (MATERIALQTYLOG) по ден и вид:
      1 = продажба, 2 = доставка (+ връщане към доставчик), 37/38 = корекция
      надолу/нагоре (ревизии и ръчни), 4 = брак/отписване, друго
  - резултатите от ревизиите (INVENTORY + INVENTORYCONTENT): по Мистрал /
    намерено / разлика в бройки и пари

и търси:
  shortage   - липси от ревизия (намерено < по Мистрал)
  surplus    - излишъци от ревизия
  negative   - отрицателна наличност сега (продадено без заведена доставка)
  phantom    - „фантомна" наличност: по Мистрал има, продаваше се, а 4+ дни
               нула продажби -> вероятно я няма на рафта и няма да се поръча
  writeoff   - брак/отписване
  correction - корекции надолу без ревизия (ръчни)
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import models as m
from . import service

SOFIA = timezone(timedelta(hours=3))
OPS = {1: "продажба", 2: "доставка", 4: "брак", 37: "корекция надолу", 38: "корекция нагоре"}
KIND = {
    "shortage": "Липси от ревизия", "surplus": "Излишъци от ревизия",
    "negative": "Отрицателна наличност", "phantom": "Фантомна наличност",
    "writeoff": "Брак / отписване", "correction": "Ръчни корекции надолу",
    "undelivered": "Незаведена доставка", "short_delivery": "Непълна доставка",
    "suspicious_delivery": "Подозрително голяма доставка (OCR?)",
}
PHANTOM_DAYS = 4


def _ensure(db: Session):
    db.execute(text("""CREATE TABLE IF NOT EXISTS stock_movements (
        store_id INT NOT NULL, article_id INT NOT NULL, day DATE NOT NULL, optype INT NOT NULL,
        qty_in NUMERIC(14,3) NOT NULL DEFAULT 0, qty_out NUMERIC(14,3) NOT NULL DEFAULT 0, n INT NOT NULL DEFAULT 0,
        PRIMARY KEY (store_id, article_id, day, optype))"""))
    db.execute(text("""CREATE TABLE IF NOT EXISTS inventory_results (
        store_id INT NOT NULL, article_id INT NOT NULL, inv_num INT NOT NULL, inv_at TIMESTAMPTZ,
        book_qty NUMERIC(14,3), found_qty NUMERIC(14,3), diff_qty NUMERIC(14,3), diff_money NUMERIC(14,4),
        PRIMARY KEY (store_id, article_id, inv_num))"""))
    db.commit()


def _loc_map(db: Session, cur) -> dict:
    from .imports import normalize_store_name
    lk = {normalize_store_name(s.name): s.id for s in db.execute(select(m.Store)).scalars().all()}
    for al in db.execute(select(m.StoreAlias)).scalars().all():
        lk[al.alias_normalized] = al.store_id
    active = {s.id for s in db.execute(select(m.Store).where(m.Store.is_active.is_(True))).scalars().all()}
    cur.execute("SELECT ID, NAME FROM LOCATION")
    out = {}
    for r in cur.fetchall():
        n = normalize_store_name(r["NAME"])
        sid = lk.get(n) or lk.get(n.replace("Д.", "", 1).strip())
        if sid in active:
            out[r["ID"]] = sid
    return out


def sync(db: Session, days: int = 14) -> dict:
    from . import mistral
    _ensure(db)
    arts = {int(a.sku): a.id for a in db.execute(select(m.Article).where(m.Article.is_active.is_(True))).scalars().all()
            if a.sku.isdigit()}
    since = datetime.now(SOFIA).date() - timedelta(days=days)
    with mistral.connect() as conn:
        cur = conn.cursor()
        locs = _loc_map(db, cur)
        L = ",".join(str(x) for x in locs)
        C = ",".join(str(x) for x in arts)
        cur.execute(f"""
            SELECT LOCATIONID AS loc, CAST(OPERATIONDATE AS date) AS d, MATERIALCODE AS code, OPERATIONTYPE AS op,
                   SUM(CASE WHEN QTY > 0 THEN QTY ELSE 0 END) AS qin, SUM(CASE WHEN QTY < 0 THEN -QTY ELSE 0 END) AS qout,
                   COUNT(*) AS n
            FROM MATERIALQTYLOG
            WHERE OPERATIONDATE >= %s AND LOCATIONID IN ({L}) AND MATERIALCODE IN ({C})
            GROUP BY LOCATIONID, CAST(OPERATIONDATE AS date), MATERIALCODE, OPERATIONTYPE""", (since.isoformat(),))
        mv = cur.fetchall()
        cur.execute(f"""
            SELECT i.LOCATIONID AS loc, i.INVENTORYNUM AS num, i.DATEINVENTORY AS dt, c.MATERIALCODE AS code,
                   c.SEARCHQTY AS book, c.QTYFOUND AS found, c.DIFFERENCEQTY AS diff, c.DIFFERENCEMONEY AS money
            FROM INVENTORY i JOIN INVENTORYCONTENT c ON c.INVENTORYNUM = i.INVENTORYNUM AND c.LOCATIONID = i.LOCATIONID
            WHERE i.DATEINVENTORY >= %s AND i.LOCATIONID IN ({L}) AND c.MATERIALCODE IN ({C})""", (since.isoformat(),))
        inv = cur.fetchall()
    db.execute(text("DELETE FROM stock_movements WHERE day >= :d"), {"d": since})
    rows = [{"s": locs[r["loc"]], "a": arts[int(r["code"])], "d": r["d"], "o": int(r["op"]),
             "i": float(r["qin"] or 0), "u": float(r["qout"] or 0), "n": int(r["n"])}
            for r in mv if r["loc"] in locs and int(r["code"]) in arts]
    if rows:
        db.execute(text("""INSERT INTO stock_movements(store_id, article_id, day, optype, qty_in, qty_out, n)
                           VALUES (:s, :a, :d, :o, :i, :u, :n)
                           ON CONFLICT (store_id, article_id, day, optype) DO UPDATE
                           SET qty_in = EXCLUDED.qty_in, qty_out = EXCLUDED.qty_out, n = EXCLUDED.n"""), rows)
    irows = [{"s": locs[r["loc"]], "a": arts[int(r["code"])], "n": int(r["num"]),
              "t": r["dt"].replace(tzinfo=SOFIA) if r["dt"] else None,
              "b": float(r["book"] or 0), "f": float(r["found"] or 0), "q": float(r["diff"] or 0), "m": float(r["money"] or 0)}
             for r in inv if r["loc"] in locs and int(r["code"]) in arts]
    if irows:
        db.execute(text("""INSERT INTO inventory_results(store_id, article_id, inv_num, inv_at, book_qty, found_qty, diff_qty, diff_money)
                           VALUES (:s, :a, :n, :t, :b, :f, :q, :m)
                           ON CONFLICT (store_id, article_id, inv_num) DO UPDATE
                           SET inv_at = EXCLUDED.inv_at, book_qty = EXCLUDED.book_qty, found_qty = EXCLUDED.found_qty,
                               diff_qty = EXCLUDED.diff_qty, diff_money = EXCLUDED.diff_money"""), irows)
    db.commit()
    return {"movements": len(rows), "inventory_lines": len(irows), "stores": len(set(locs.values())), "since": since.isoformat()}


def items(db: Session, days: int = 7, store_id: int | None = None) -> list[dict]:
    """Всички аномалии (ред по ред) за последните `days` дни."""
    _ensure(db)
    since = datetime.now(SOFIA).date() - timedelta(days=days)
    arts = {a.id: a for a in db.execute(select(m.Article)).scalars().all()}
    stores = {s.id: s.name for s in db.execute(select(m.Store).where(m.Store.is_active.is_(True))).scalars().all()}
    price = lambda aid: float(arts[aid].delivery_price or 0) if aid in arts else 0.0  # noqa: E731
    name = lambda aid: (arts[aid].supplier_name or arts[aid].name) if aid in arts else str(aid)  # noqa: E731
    sku = lambda aid: arts[aid].sku if aid in arts else ""  # noqa: E731
    sf = "AND store_id = :sid" if store_id else ""
    p = {"d": since, "sid": store_id}
    out = []

    # 1) ревизии
    inv_days = set()
    for sid, aid, num, at, b, f, q, mny in db.execute(text(f"""
            SELECT store_id, article_id, inv_num, inv_at, book_qty, found_qty, diff_qty, diff_money
            FROM inventory_results WHERE inv_at >= :d {sf}"""), p).all():
        if sid not in stores or not q:
            continue
        inv_days.add((sid, at.astimezone(SOFIA).date()))
        kind = "shortage" if float(q) < 0 else "surplus"
        out.append({"kind": kind, "store_id": sid, "store": stores[sid], "sku": sku(aid), "name": name(aid),
                    "qty": float(q), "eur": round(float(mny or 0) or float(q) * price(aid), 2),
                    "day": at.astimezone(SOFIA).strftime("%d.%m"),
                    "info": f"по Мистрал {float(b):g}, намерено {float(f):g}"})

    # 2) брак и ръчни корекции надолу (корекциите в ден на ревизия са от нея - не ги броим двойно)
    for sid, aid, d, op, qout in db.execute(text(f"""
            SELECT store_id, article_id, day, optype, qty_out FROM stock_movements
            WHERE day >= :d AND optype IN (4, 37) AND qty_out > 0 {sf}"""), p).all():
        if sid not in stores:
            continue
        if op == 37 and (sid, d) in inv_days:
            continue
        kind = "writeoff" if op == 4 else "correction"
        out.append({"kind": kind, "store_id": sid, "store": stores[sid], "sku": sku(aid), "name": name(aid),
                    "qty": -float(qout), "eur": round(-float(qout) * price(aid), 2), "day": d.strftime("%d.%m"),
                    "info": OPS.get(op, f"вид {op}")})

    # 3) отрицателна и 4) фантомна наличност - по текущата наличност
    today = datetime.now(SOFIA).date()
    sales = defaultdict(lambda: [0.0] * 14)
    q = (select(m.SalesHistory.store_id, m.SalesHistory.article_id, m.SalesHistory.sale_date, m.SalesHistory.quantity_sold)
         .where(m.SalesHistory.sale_date >= today - timedelta(days=14)))
    if store_id:
        q = q.where(m.SalesHistory.store_id == store_id)
    for sid, aid, d, qty in db.execute(q).all():
        i = (d - (today - timedelta(days=14))).days
        if 0 <= i < 14:
            sales[(sid, aid)][i] += max(float(qty or 0), 0.0)
    last_in = {(s, a): d for s, a, d in db.execute(text(f"""
        SELECT store_id, article_id, MAX(day) FROM stock_movements WHERE optype = 2 AND qty_in > 0 {sf.replace('AND', 'AND', 1)}
        GROUP BY store_id, article_id"""), p).all()}
    plano = defaultdict(set)
    pq = select(m.Planogram.store_id, m.Planogram.article_id)
    if store_id:
        pq = pq.where(m.Planogram.store_id == store_id)
    for s, a in db.execute(pq).all():
        plano[s].add(a)
    for sid in stores:
        if store_id and sid != store_id:
            continue
        stock = service.latest_stock_map(db, sid)
        for aid in plano.get(sid, ()):
            st = stock.get(aid)
            if st is None or aid not in arts or not arts[aid].is_active:
                continue
            ser = sales[(sid, aid)]
            sdp = sum(ser) / 14
            if st < 0:
                li = last_in.get((sid, aid))
                out.append({"kind": "negative", "store_id": sid, "store": stores[sid], "sku": sku(aid), "name": name(aid),
                            "qty": st, "eur": round(st * price(aid), 2), "day": today.strftime("%d.%m"),
                            "info": f"последна доставка {li.strftime('%d.%m') if li else 'няма за 14 дни'} · продава {sdp:.1f}/ден"})
            elif st >= 1 and sdp >= 1 and sum(ser[-PHANTOM_DAYS:]) == 0 and sum(ser[:-PHANTOM_DAYS]) > 0:
                out.append({"kind": "phantom", "store_id": sid, "store": stores[sid], "sku": sku(aid), "name": name(aid),
                            "qty": st, "eur": round(st * price(aid), 2), "day": today.strftime("%d.%m"),
                            "info": f"продаваше {sum(ser[:-PHANTOM_DAYS]) / (14 - PHANTOM_DAYS):.1f}/ден, от {PHANTOM_DAYS} дни 0 продажби"})

    # подозрително големи доставки / корекции нагоре - типично при грешно OCR въвеждане
    try:
        from . import deliveries as _dl
        dsince = today - timedelta(days=days)
        ani_q = _dl._ani(db, dsince)
        mx = {(x.store_id, x.article_id): float(x.max_stock)
              for x in db.execute(select(m.StoreArticleSetting)).scalars().all()}
        q = text("""SELECT store_id, article_id, day, optype, qty_in FROM stock_movements
                    WHERE optype IN (2, 38) AND qty_in >= 50 AND day >= :d""")
        for sid, aid, d, op, qin in db.execute(q, {"d": dsince}).all():
            if (store_id and sid != store_id) or sid not in stores or aid not in arts:
                continue
            if "АМБАЛАЖ" in name(aid).upper():
                continue   # бутилки/каси - винаги големи бройки, не са стока
            qin = float(qin)
            ordered = ani_q.get((sid, d), {}).get(aid)
            m_ = mx.get((sid, aid), 0.0)
            why = None
            if ordered and qin > 3 * ordered:
                why = f"поръчано {ordered:g} бр., заведено {qin:g} бр. ({qin / ordered:.0f}×)"
            elif not ordered and m_ > 0 and qin > 4 * m_:
                why = f"заведено {qin:g} бр. при макс {m_:g} в магазина ({qin / m_:.0f}×)"
            elif m_ == 0 and qin >= 100:
                why = f"заведено {qin:g} бр., а артикулът няма мин/макс в магазина"
            if why:
                out.append({"kind": "suspicious_delivery", "store_id": sid, "store": stores[sid], "sku": sku(aid),
                            "name": name(aid), "qty": qin, "eur": round(qin * price(aid), 2), "day": d.strftime("%d.%m"),
                            "info": ("доставка: " if op == 2 else "корекция нагоре: ") + why})
    except Exception:
        db.rollback()

    # поръчано от Ани -> доставено ли е и заведено в Мистрал? (само завършени дни)
    try:
        from . import deliveries
        dsince = today - timedelta(days=days)
        dl, ani = deliveries._delivered(db, dsince), deliveries._ani(db, dsince)
        for (sid, d), lines in ani.items():
            if d >= today or d < dsince or (store_id and sid != store_id) or sid not in stores:
                continue
            # НДК доставя в същия или следващия ден (понякога на части) -> гледаме съседните дни
            got = {}
            for dd in (d - timedelta(days=1), d, d + timedelta(days=1)):
                for k_, v_ in dl.get((sid, dd), {}).items():
                    got[k_] = got.get(k_, 0.0) + v_
            for aid, q in lines.items():
                if aid not in arts or "АМБАЛАЖ" in name(aid).upper():
                    continue
                g = got.get(aid, 0.0)
                if g <= 0:
                    out.append({"kind": "undelivered", "store_id": sid, "store": stores[sid], "sku": sku(aid), "name": name(aid),
                                "qty": -q, "eur": round(-q * price(aid), 2), "day": d.strftime("%d.%m"),
                                "info": f"Ани поръча {q:g} бр. — в Мистрал няма доставка за деня (не е доставено или не е заведено)"})
                elif g < q - 0.01:
                    out.append({"kind": "short_delivery", "store_id": sid, "store": stores[sid], "sku": sku(aid), "name": name(aid),
                                "qty": g - q, "eur": round((g - q) * price(aid), 2), "day": d.strftime("%d.%m"),
                                "info": f"поръчано {q:g} бр., доставено {g:g} бр."})
    except Exception:
        db.rollback()
    return out


def summary(db: Session, days: int = 7) -> dict:
    rows = items(db, days)
    tot = {k: {"n": 0, "qty": 0.0, "eur": 0.0} for k in KIND}
    by = defaultdict(lambda: {k: {"n": 0, "qty": 0.0, "eur": 0.0} for k in KIND})
    names = {}
    for r in rows:
        for t in (tot[r["kind"]], by[r["store_id"]][r["kind"]]):
            t["n"] += 1
            t["qty"] += r["qty"]
            t["eur"] += r["eur"]
        names[r["store_id"]] = r["store"]
    stores = []
    for sid, k in by.items():
        loss = -(k["shortage"]["eur"] + k["writeoff"]["eur"] + k["correction"]["eur"])
        stores.append({"store_id": sid, "store": names[sid], "loss_eur": round(loss, 2),
                       **{f"{x}_n": k[x]["n"] for x in KIND}, **{f"{x}_eur": round(k[x]["eur"], 2) for x in KIND}})
    stores.sort(key=lambda r: (-r["loss_eur"], -(r["negative_n"] + r["phantom_n"])))
    for v in tot.values():
        v["qty"], v["eur"] = round(v["qty"], 1), round(v["eur"], 2)
    last = db.execute(text("SELECT MAX(day) FROM stock_movements")).scalar()
    return {"days": days, "kinds": KIND, "totals": tot, "stores": stores,
            "data_until": last.strftime("%d.%m.%Y") if last else None}
