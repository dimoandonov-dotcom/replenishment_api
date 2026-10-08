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
    "double_delivery": "Двойно заведена доставка",
    "ocr_repeat": "Повтаряща се OCR грешка",
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

    # корекции надолу след даден ден (ревизия или ръчна корекция) - за „поправено с ревизия/корекция"
    _down: dict = {}

    def down_after(sid, aid, d) -> float:
        if not _down:
            _down["_"] = True
            since_ = datetime.now(SOFIA).date() - timedelta(days=max(days, 14) + 2)
            for s_, a_, d_, q_ in db.execute(text("""SELECT store_id, article_id, day, qty_out FROM stock_movements
                                                     WHERE optype = 37 AND qty_out > 0 AND day >= :d"""), {"d": since_}).all():
                _down.setdefault((s_, a_), []).append((d_, float(q_)))
            for s_, a_, at_, q_ in db.execute(text("""SELECT store_id, article_id, inv_at, diff_qty FROM inventory_results
                                                      WHERE diff_qty < 0 AND inv_at >= :d"""), {"d": since_}).all():
                _down.setdefault((s_, a_), []).append((at_.astimezone(SOFIA).date() if at_ else since_, -float(q_)))
        return sum(q_ for d_, q_ in _down.get((sid, aid), []) if d_ >= d)

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
        # заведеното (без сторното) и нетното след поправка; поправените остават със зелен знак
        q = text("""SELECT store_id, article_id, day, MIN(optype),
                           MAX(CASE WHEN optype IN (2, 38) THEN qty_in ELSE 0 END) AS gross,
                           SUM(CASE WHEN optype IN (2, 38) THEN qty_in ELSE 0 END)
                           - SUM(CASE WHEN optype = 24 THEN qty_out - qty_in ELSE 0 END) AS net
                    FROM stock_movements WHERE optype IN (2, 38, 24) AND day >= :d
                    GROUP BY store_id, article_id, day
                    HAVING SUM(CASE WHEN optype IN (2, 38) THEN qty_in ELSE 0 END) >= 50""")
        for sid, aid, d, op, gross, net in db.execute(q, {"d": dsince}).all():
            gross, net = float(gross or 0), float(net or 0)
            fixed_ = net < gross - 0.01
            qin = (gross - net) if fixed_ else net     # поправено: показваме грешно заведеното (сторнираното)
            if (store_id and sid != store_id) or sid not in stores or aid not in arts:
                continue
            if "АМБАЛАЖ" in name(aid).upper():
                continue   # бутилки/каси - винаги големи бройки, не са стока
            ordered = ani_q.get((sid, d), {}).get(aid)
            m_ = mx.get((sid, aid), 0.0)
            why = None
            if ordered and qin > 3 * ordered:
                why = f"поръчано {ordered:g} бр., заведено {qin:g} бр. ({qin / ordered:.0f}×)"
            elif not ordered and m_ > 0 and qin > 4 * m_:
                why = f"заведено {qin:g} бр. при макс {m_:g} в магазина ({qin / m_:.0f}×)"
            elif m_ == 0 and qin >= 100:
                why = f"заведено {qin:g} бр., а артикулът няма мин/макс в магазина"
            if why and not fixed_:
                exc = qin - max(m_, float(ordered or 0), 1)
                dn = down_after(sid, aid, d)
                if exc > 0 and dn >= 0.5 * exc:
                    fixed_, net = True, None
                    why += f" · след това коригирано с ревизия/корекция надолу −{dn:g} бр."
            if why:
                out.append({"kind": "suspicious_delivery", "store_id": sid, "store": stores[sid], "sku": sku(aid),
                            "name": name(aid), "qty": qin, "eur": 0.0 if fixed_ else round(qin * price(aid), 2),
                            "day": d.strftime("%d.%m"), "fixed": fixed_,
                            "info": ("доставка: " if op == 2 else "корекция нагоре: ") + why
                                    + (f" · поправено на {net:g} бр." if fixed_ and net is not None else "")})
    except Exception:
        db.rollback()

    # двойно заведена доставка: два документа от един доставчик в един ден, еднакви в 80%+ от редовете
    try:
        dsince2 = today - timedelta(days=days)
        for sid, d, da, dbb, p, n, la, lb, sa, sb, ta, tb, ua, ub, e, x, fx in db.execute(text(
                "SELECT store_id, day, doc_a, doc_b, partner, same_lines, lines_a, lines_b, sum_a, sum_b, saved_a, saved_b, "
                "user_a, user_b, excess_eur, examples, COALESCE(fixed, FALSE) FROM dup_docs WHERE day >= :d"), {"d": dsince2}).all():
            if (store_id and sid != store_id) or sid not in stores:
                continue
            out.append({"kind": "double_delivery", "store_id": sid, "store": stores[sid], "sku": "",
                        "name": f"{p}: № {da} и № {dbb}", "qty": n, "eur": 0.0 if fx else float(e or 0),
                        "day": d.strftime("%d.%m"), "fixed": bool(fx),
                        "info": (f"№ {da} ({ta} ч., {float(sa or 0):.2f} €, {la} реда) и № {dbb} ({tb} ч., {float(sb or 0):.2f} €, "
                                 f"{lb} реда) — {n} еднакви реда. Напр.: {x}")})
    except Exception:
        db.rollback()

    # повтаряща се OCR грешка: един артикул е заведен с ЕДНО И СЪЩО странно количество в няколко магазина
    try:
        dsince3 = today - timedelta(days=max(days, 14))
        mx3 = {(x.store_id, x.article_id): float(x.max_stock)
               for x in db.execute(select(m.StoreArticleSetting)).scalars().all()}
        groups = defaultdict(list)
        # нетно доставено: доставка (2) минус сторно/корекция на документа (24) - поправените не са аномалия
        fixed_map = {}
        for sid, aid, d, qin, net in db.execute(text("""SELECT store_id, article_id, day,
                                                          MAX(CASE WHEN optype = 2 THEN qty_in ELSE 0 END),
                                                          SUM(CASE WHEN optype = 2 THEN qty_in - qty_out ELSE 0 END)
                                                          - SUM(CASE WHEN optype = 24 THEN qty_out - qty_in ELSE 0 END)
                                                   FROM stock_movements WHERE optype IN (2, 24) AND day >= :d
                                                   GROUP BY store_id, article_id, day
                                                   HAVING MAX(CASE WHEN optype = 2 THEN qty_in ELSE 0 END) >= 20"""),
                                           {"d": dsince3}).all():
            # qty_in на деня е сбор (66 + поправените 6 = 72) - първоначалното е нетното + сторното
            net = float(net or 0)
            storno = float(qin or 0) - net
            if storno > 0.01:
                qin = storno            # грешно заведеното количество (сторнираното)
                fixed_map[(sid, aid, d)] = net
            if sid not in stores or aid not in arts or "АМБАЛАЖ" in name(aid).upper():
                continue
            qin = float(qin); m_ = mx3.get((sid, aid), 0.0)
            pk = int(getattr(arts[aid], "pack_size", 1) or 1)
            qs = str(int(qin)) if qin == int(qin) else ""
            odd = (pk > 1 and int(qin) % pk != 0) or (len(qs) >= 2 and len(set(qs)) == 1)   # не е кратно / 66, 88, 222
            if odd and qin > 2 * pk and (m_ == 0 or qin > 3 * m_):
                groups[(aid, qin)].append((sid, d, m_))
        for (aid, qin), occ in groups.items():
            if len({s_ for s_, _, _ in occ}) < 3:
                continue
            for sid, d, m_ in occ:
                if store_id and sid != store_id:
                    continue
                excess = qin - (m_ or 0)
                fx = fixed_map.get((sid, aid, d))
                how = "поправено на"
                if fx is None:
                    pk_ = int(getattr(arts[aid], "pack_size", 1) or 1)
                    exc = qin - max(m_, pk_, 1)
                    dn = down_after(sid, aid, d)
                    if exc > 0 and dn >= 0.5 * exc:
                        fx, how = dn, "след това коригирано с ревизия/корекция надолу −"
                out.append({"kind": "ocr_repeat", "store_id": sid, "store": stores[sid], "sku": sku(aid), "name": name(aid),
                            "qty": qin, "eur": 0.0 if fx is not None else round(excess * price(aid), 2),
                            "day": d.strftime("%d.%m"), "fixed": fx is not None,
                            "info": f"заведено {qin:g} бр. в {len({s_ for s_, _, _ in occ})} магазина (при макс {m_:g}) — "
                                    f"едно и също количество навсякъде" + ((f" · {how}{fx:g} бр." if how.endswith("−") else f" · {how} {fx:g} бр.") if fx is not None else "")})
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
    tot = {k: {"n": 0, "qty": 0.0, "eur": 0.0, "fixed": 0} for k in KIND}
    by = defaultdict(lambda: {k: {"n": 0, "qty": 0.0, "eur": 0.0} for k in KIND})
    names = {}
    for r in rows:
        if r.get("fixed"):            # поправените не се броят - само новите грешки
            tot[r["kind"]]["fixed"] += 1
            continue
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


# ---------------------------------------------------------------------------
# Двойно заведени документи за доставка (всички доставчици) - нощна проверка в Мистрал
# ---------------------------------------------------------------------------

OWN_SUPPLIERS = ("ТРИСТА БГ", "БАНДИТС")


def detect_duplicate_docs(db: Session, days: int = 3) -> dict:
    """Два документа от един и същ доставчик в един и същ ден и магазин, които съвпадат
    в поне 80% от редовете си (същият артикул и количество) -> вероятно двойно заведени."""
    from . import mistral
    db.execute(text("""CREATE TABLE IF NOT EXISTS dup_docs (
        store_id INT NOT NULL, day DATE NOT NULL, doc_a TEXT, doc_b TEXT, partner TEXT,
        same_lines INT, lines_a INT, lines_b INT, sum_a NUMERIC(14,2), sum_b NUMERIC(14,2),
        saved_a TEXT, saved_b TEXT, user_a TEXT, user_b TEXT, excess_eur NUMERIC(14,2),
        examples TEXT, PRIMARY KEY (store_id, day, doc_a, doc_b))"""))
    db.execute(text("ALTER TABLE dup_docs ADD COLUMN IF NOT EXISTS fixed BOOLEAN DEFAULT FALSE"))
    db.commit()
    since = datetime.now(SOFIA).date() - timedelta(days=days)
    found = 0
    with mistral.connect() as conn:
        cur = conn.cursor()
        locs = _loc_map(db, cur)
        L = ",".join(map(str, locs))
        # първоначално заведеното (2, само плюс) и нетното след сторно (24) - за да се види поправеното
        cur.execute(f"""SELECT LOCATIONID AS loc, OPERAIONNUM AS op, MATERIALCODE AS code,
                               SUM(CASE WHEN OPERATIONTYPE = 2 AND QTY > 0 THEN QTY ELSE 0 END) AS q,
                               SUM(QTY) AS net, CAST(MIN(OPERATIONDATE) AS date) AS d
                        FROM MATERIALQTYLOG WITH (NOLOCK)
                        WHERE OPERATIONTYPE IN (2, 24) AND OPERATIONDATE >= %s AND LOCATIONID IN ({L})
                        GROUP BY LOCATIONID, OPERAIONNUM, MATERIALCODE""", (since.isoformat(),))
        ops = defaultdict(dict); opday = {}; opnet = defaultdict(float)
        for r in cur.fetchall():
            k = (int(r["loc"]), int(r["op"]))
            opnet[k] += float(r["net"] or 0)
            if float(r["q"] or 0) <= 0:
                continue
            ops[k][int(r["code"])] = float(r["q"]); opday[k] = r["d"]
        # заглавията на документите
        meta = {}
        byloc = defaultdict(list)
        for loc, op in ops:
            byloc[loc].append(op)
        for loc, nums in byloc.items():
            for i in range(0, len(nums), 500):
                cur.execute(f"""SELECT o.NUM, o.PARTNERNAMEID, o.DATESAVED, o.USERID, od.DOCUMENTNUM, od.DOCSUM
                                FROM OPERATIONS o WITH (NOLOCK)
                                LEFT JOIN OPERATIONDOCUMENT od WITH (NOLOCK) ON od.LOCATIONID = o.LOCATIONID AND od.NUM = o.NUM
                                WHERE o.LOCATIONID = %s AND o.NUM IN ({','.join(map(str, nums[i:i+500]))})""", (loc,))
                for r in cur.fetchall():
                    meta[(loc, int(r["NUM"]))] = r
        pids = sorted({int(m_["PARTNERNAMEID"]) for m_ in meta.values() if m_["PARTNERNAMEID"]})
        pn = {}
        for i in range(0, len(pids), 500):
            cur.execute(f"SELECT ID, PARTNERNAME FROM PARTNERNAME WHERE ID IN ({','.join(map(str, pids[i:i+500]))})")
            pn.update({int(r["ID"]): r["PARTNERNAME"] for r in cur.fetchall()})
    arts = {int(a.sku): a for a in db.execute(select(m.Article)).scalars().all() if a.sku.isdigit()}
    db.execute(text("DELETE FROM dup_docs WHERE day >= :d"), {"d": since})
    groups = defaultdict(list)
    for k in ops:
        mm = meta.get(k)
        if not mm:
            continue
        groups[(k[0], opday[k], mm["PARTNERNAMEID"])].append(k)
    for (loc, d, pid), ks in groups.items():
        if len(ks) < 2 or loc not in locs:
            continue
        for i in range(len(ks)):
            for j in range(i + 1, len(ks)):
                a, b = ops[ks[i]], ops[ks[j]]
                same = [c for c, q in a.items() if b.get(c) == q]
                small = min(len(a), len(b))
                ma, mb = meta[ks[i]], meta[ks[j]]
                na = str(ma["DOCUMENTNUM"] or "").split(".")[0]; nb = str(mb["DOCUMENTNUM"] or "").split(".")[0]
                pname = (pn.get(int(pid or 0)) or "").upper()
                own = any(k in pname for k in OWN_SUPPLIERS)
                same_num = bool(na) and na == nb
                one_digit = bool(na) and len(na) == len(nb) and na != nb and sum(x != y for x, y in zip(na, nb)) == 1
                same_sum = abs(float(ma["DOCSUM"] or 0) - float(mb["DOCSUM"] or 0)) < 0.05 and float(ma["DOCSUM"] or 0) > 0
                share = len(same) / small if small else 0
                if same_num and len(same) >= 3:
                    conf = "сигурен — еднакъв номер на документа"
                elif own:
                    continue      # собствено производство: еднакви количества по няколко пъти на ден са нормални
                elif one_digit and share >= 0.8 and len(same) >= 3:
                    consecutive = na.isdigit() and nb.isdigit() and abs(int(na) - int(nb)) == 1
                    conf = ("вероятен — последователни номера с почти еднакво съдържание (две фактури или двойно въведена)"
                            if consecutive else "сигурен — номерата се различават с една цифра (грешно разчетен номер)")
                elif same_sum and share >= 0.8 and len(same) >= 5:
                    conf = "сигурен — еднаква сума и еднакви редове"
                elif share >= 0.9 and len(same) >= 10:
                    conf = "вероятен — 90%+ еднакви редове"
                else:
                    continue
                if True:
                    excess = min(abs(float(ma["DOCSUM"] or 0)), abs(float(mb["DOCSUM"] or 0))) * (len(same) / max(len(a), len(b)))
                    ex = [conf] + [f"{(arts[c].supplier_name or arts[c].name) if c in arts else c}: {a[c]:g}" for c in same[:4]]
                    ga, gb = sum(a.values()), sum(b.values())
                    fixed = (ga > 0 and opnet[ks[i]] <= 0.1 * ga) or (gb > 0 and opnet[ks[j]] <= 0.1 * gb)
                    db.execute(text("""INSERT INTO dup_docs VALUES (:s,:d,:da,:dbb,:p,:n,:la,:lb,:sa,:sb,:ta,:tb,:ua,:ub,:e,:x,:fx)
                                       ON CONFLICT DO NOTHING"""),
                               {"s": locs[loc], "d": d, "da": str(ma["DOCUMENTNUM"] or "").split(".")[0],
                                "dbb": str(mb["DOCUMENTNUM"] or "").split(".")[0], "p": pn.get(int(pid or 0), "?"),
                                "n": len(same), "la": len(a), "lb": len(b), "sa": float(ma["DOCSUM"] or 0),
                                "sb": float(mb["DOCSUM"] or 0), "ta": str(ma["DATESAVED"])[11:16], "tb": str(mb["DATESAVED"])[11:16],
                                "ua": str(ma["USERID"]), "ub": str(mb["USERID"]), "e": round(excess, 2), "x": " · ".join(ex),
                                "fx": bool(fixed)})
                    found += 1
    db.commit()
    return {"days": days, "duplicates": found}
