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
KEEP_HIDE_DAYS = 90            # „оставяме“ скрива предложението за 90 дни

KINDS = {"dead": "Мъртъв — 0 продажби, има наличност",
         "slow": "Много бавен — под 1 бр./магазин на месец",
         "falling": "Изпада — продажбите спадат силно"}


def _ensure(db: Session):
    db.execute(text("""CREATE TABLE IF NOT EXISTS delist_decisions (code INT PRIMARY KEY, decision TEXT NOT NULL,
        note TEXT, by_user TEXT, at TIMESTAMPTZ NOT NULL DEFAULT now())"""))
    db.commit()


def proposals(db: Session, include_seasonal: bool = False) -> dict:
    from .salesall import _ensure as _sa
    _sa(db); _ensure(db)
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
               COALESCE(st.sq,0), COALESCE(st.sv,0), COALESCE(st.nst,0)
        FROM sa_articles a LEFT JOIN s ON s.code = a.code LEFT JOIN st ON st.code = a.code
        WHERE COALESCE(st.sq,0) > 0 OR COALESCE(s.q,0) > 0"""),
        {"f": since, "t": last, "mid": mid}).all()
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
    out = []
    stats = defaultdict(lambda: [0, 0.0])
    for (code, name, grp, sub, sup, q, r, c, ns, f, l, q1, q2, sq, sv, nst) in rows:
        grp = grp or "Без група"
        if grp in SKIP_GROUPS or (grp in SEASONAL and not include_seasonal):
            continue
        if (name or "").upper().startswith(("АМБАЛАЖ", "ТОРБА", "ТОРБИЧКА")):
            continue
        q, r, c, q1, q2, sq, sv = map(lambda x: float(x or 0), (q, r, c, q1, q2, sq, sv))
        stores = max(int(nst or 0), int(ns or 0), 1)
        per_store_month = q / stores / days * 30
        share, cshare, nsub = cum.get(code, (0.0, 100.0, 1))
        is_new = f is not None and (f - since).days >= days - NEW_DAYS and q > 0
        kind = None
        if q <= 0 and sq > 0:
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
            reason = f"0 продажби за {days} дни, а има {sq:g} бр. в {nst} маг. ({sv:,.0f} € замразени)".replace(",", " ")
        elif kind == "slow":
            reason = (f"{q:g} бр. за {days} дни в {stores} маг. = {per_store_month:.2f} бр./магазин на месец; "
                      f"{share:.1f}% от оборота на подгрупата")
        else:
            reason = (f"първа половина {q1:g} бр. → втора {q2:g} бр. ({100 * (q2 - q1) / q1:+.0f}%); "
                      f"{per_store_month:.1f} бр./магазин на месец")
        stats[kind][0] += 1; stats[kind][1] += sv
        out.append({"code": code, "name": name or str(code), "grp": grp, "sub": sub or "", "supplier": sup or "",
                    "kind": kind, "reason": reason, "units": round(q, 1), "rev": round(r, 2),
                    "margin_pct": round(100 * (r - c) / r, 1) if r else None,
                    "stores_sold": int(ns or 0), "stores_stock": int(nst or 0), "per_store_month": round(per_store_month, 2),
                    "stock": round(sq, 1), "stock_eur": round(sv, 2),
                    "cover_days": round(sq / daily) if daily > 0 else None,
                    "last_sale": l.strftime("%d.%m") if l else None, "sub_share": share, "sub_items": nsub,
                    "decision": d[0] if d else None, "decision_by": d[2] if d else None,
                    "decision_at": d[3].astimezone(SOFIA).strftime("%d.%m.%Y") if d else None})
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
