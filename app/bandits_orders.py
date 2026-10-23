"""
Бандитс: автоматични заявки за сандвичи и десерти (само за Димо).

Същият принцип като при НДК, но за бързоразвалящи се стоки:
  - продажби от Мистрал за последните 14 дни (по ден от седмицата, 4 седмици назад + средно 14 дни);
  - покритие до следващата доставка по графика на магазина (bd_schedule; по подразбиране пон–съб);
  - таван по срока на годност: не повече, отколкото ще се продаде, докато е годно;
  - никога 0 за артикул, който се продава и е на 0 (поне 1 бр.);
  - по-малък резерв, ако има много върнат брак.
Наличностите в Мистрал за Бандитс често са грешни (доставките се завеждат късно) - отрицателната наличност се брои за 0.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

SOFIA = timezone(timedelta(hours=3))
SUPPLIER = "БАНДИТС"
DAYS = 14
SAFETY = 0.15
WEEK_W = [0.4, 0.3, 0.2, 0.1]
SHELF = {939: 4, 22249: 4, 9874: 4, 9873: 4, 16570: 6, 16549: 6, 21088: 6, 16551: 6, 26341: 6, 26343: 6, 26342: 6,
         18359: 6, 18938: 6, 18917: 6, 19027: 6, 21086: 6, 16537: 6, 24347: 7, 24346: 7, 22846: 7, 16567: 7,
         16697: 7, 22844: 7, 16694: 4, 16562: 4, 16561: 5, 16692: 4, 26404: 7}
WD = ["пон", "вт", "ср", "чет", "пет", "съб", "нед"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS bd_stores (loc INT PRIMARY KEY, name TEXT, active BOOLEAN DEFAULT TRUE);
CREATE TABLE IF NOT EXISTS bd_articles (code INT PRIMARY KEY, name TEXT, sub TEXT, shelf_days INT, price NUMERIC(12,4),
  active BOOLEAN DEFAULT TRUE, updated_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS bd_sales (loc INT, code INT, day DATE, qty NUMERIC(12,3), PRIMARY KEY (day, loc, code));
CREATE TABLE IF NOT EXISTS bd_stock (loc INT, code INT, qty NUMERIC(12,3), at TIMESTAMPTZ, PRIMARY KEY (loc, code));
CREATE TABLE IF NOT EXISTS bd_moves (loc INT, code INT, day DATE, delivered NUMERIC(12,3), returned NUMERIC(12,3),
  PRIMARY KEY (day, loc, code));
CREATE TABLE IF NOT EXISTS bd_schedule (loc INT PRIMARY KEY, delivery_days TEXT DEFAULT '0,1,2,3,4,5', note TEXT);
CREATE TABLE IF NOT EXISTS bd_orders (delivery_day DATE, loc INT, code INT, qty INT, per_day NUMERIC(10,2),
  need NUMERIC(10,2), stock NUMERIC(10,2), cap NUMERIC(10,2), waste_pct NUMERIC(6,1), cover_days INT, note TEXT,
  created_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY (delivery_day, loc, code));
CREATE TABLE IF NOT EXISTS bd_runs (id SERIAL PRIMARY KEY, kind TEXT, at TIMESTAMPTZ DEFAULT now(), info TEXT);
"""


def ensure(db: Session):
    for q in SCHEMA.strip().split(";"):
        if q.strip():
            db.execute(text(q))
    db.commit()


# ------------------------------------------------------------------ данни от Мистрал
def sync(db: Session, days: int = 35) -> dict:
    from . import mistral
    ensure(db)
    today = datetime.now(SOFIA).date()
    since = today - timedelta(days=days)
    with mistral.use("varna"), mistral.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT ID FROM PARTNERNAME WHERE UPPER(PARTNERNAME) LIKE %s", (f"%{SUPPLIER}%",))
        pids = [int(r["ID"]) for r in cur.fetchall()]
        if not pids:
            raise RuntimeError("Не намирам Бандитс в Мистрал")
        P = ",".join(map(str, pids))
        cur.execute(f"""SELECT DISTINCT c.MATERIALCODE AS code FROM SALECONTENT c WITH (NOLOCK)
                        JOIN SALE s WITH (NOLOCK) ON s.NUM = c.NUM AND s.LOCATIONID = c.LOCATIONID
                        WHERE c.LASTPARTNERNAMEID IN ({P}) AND s.REPORTINGDATE >= %s""", (since.isoformat(),))
        codes = sorted({int(r["code"]) for r in cur.fetchall()} | set(SHELF))
        if not codes:
            raise RuntimeError("Няма продажби на Бандитс")
        C = ",".join(map(str, codes))
        cur.execute(f"""SELECT MATERIALCODE AS code, MAX(SEARCHNAME) AS nm, MAX(CATEGORY) AS cat, MAX(AVGDELIVERYPRICE) AS p
                        FROM MATERIAL WITH (NOLOCK) WHERE MATERIALCODE IN ({C}) GROUP BY MATERIALCODE""")
        mat = {int(r["code"]): r for r in cur.fetchall()}
        # само стоката на Бандитс: в името има „БАНДИТС“ или е от известните (Хляб и кифли и др.)
        codes = [c for c in codes if c in SHELF or "БАНДИТС" in ((mat.get(c) or {}).get("nm") or "").upper()]
        C = ",".join(map(str, codes))
        cur.execute("SELECT CATEGORY AS code, MAX(NAME) AS name FROM CATEGORY GROUP BY CATEGORY")
        cname = {(r["code"] or "").strip(): r["name"] for r in cur.fetchall()}
        cur.execute(f"""SELECT s.LOCATIONID AS loc, c.MATERIALCODE AS code, s.REPORTINGDATE AS d, SUM(c.QTY) AS q
                        FROM SALE s WITH (NOLOCK) JOIN SALECONTENT c WITH (NOLOCK) ON c.NUM = s.NUM AND c.LOCATIONID = s.LOCATIONID
                        WHERE c.MATERIALCODE IN ({C}) AND s.REPORTINGDATE >= %s AND s.REPORTINGDATE < %s
                        GROUP BY s.LOCATIONID, c.MATERIALCODE, s.REPORTINGDATE""", (since.isoformat(), today.isoformat()))
        sales = cur.fetchall()
        locs = sorted({int(r["loc"]) for r in sales})
        L = ",".join(map(str, locs)) or "0"
        cur.execute(f"SELECT ID, NAME FROM LOCATION WHERE ID IN ({L})")
        lname = {int(r["ID"]): (r["NAME"] or "").strip() for r in cur.fetchall()}
        cur.execute(f"""SELECT LOCATIONID AS loc, MATERIALCODE AS code, SUM(QTY) AS q FROM MATERIAL WITH (NOLOCK)
                        WHERE MATERIALCODE IN ({C}) AND LOCATIONID IN ({L}) GROUP BY LOCATIONID, MATERIALCODE""")
        stock = cur.fetchall()
        cur.execute(f"""SELECT l.LOCATIONID AS loc, l.MATERIALCODE AS code, CAST(l.OPERATIONDATE AS date) AS d,
                               SUM(CASE WHEN l.QTY > 0 THEN l.QTY ELSE 0 END) AS inq,
                               SUM(CASE WHEN l.QTY < 0 THEN -l.QTY ELSE 0 END) AS outq
                        FROM MATERIALQTYLOG l WITH (NOLOCK)
                        WHERE l.OPERATIONTYPE = 2 AND l.MATERIALCODE IN ({C}) AND l.LOCATIONID IN ({L}) AND l.OPERATIONDATE >= %s
                        GROUP BY l.LOCATIONID, l.MATERIALCODE, CAST(l.OPERATIONDATE AS date)""", (since.isoformat(),))
        moves = cur.fetchall()
    now = datetime.now(SOFIA)
    for loc in locs:
        db.execute(text("INSERT INTO bd_stores(loc, name) VALUES (:i,:n) ON CONFLICT (loc) DO UPDATE SET name = EXCLUDED.name"),
                   {"i": loc, "n": lname.get(loc, str(loc))})
        db.execute(text("INSERT INTO bd_schedule(loc) VALUES (:i) ON CONFLICT DO NOTHING"), {"i": loc})
    db.execute(text("UPDATE bd_articles SET active = (code = ANY(:c))"), {"c": codes})
    for code in codes:
        m_ = mat.get(code, {})
        db.execute(text("""INSERT INTO bd_articles(code, name, sub, shelf_days, price) VALUES (:c,:n,:s,:d,:p)
                           ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, sub = EXCLUDED.sub, price = EXCLUDED.price,
                           shelf_days = COALESCE(bd_articles.shelf_days, EXCLUDED.shelf_days), active = TRUE, updated_at = now()"""),
                   {"c": code, "n": (m_.get("nm") or str(code)).strip(), "s": cname.get((m_.get("cat") or "").strip(), ""),
                    "d": SHELF.get(code), "p": float(m_.get("p") or 0)})
    db.execute(text("DELETE FROM bd_sales WHERE day >= :d"), {"d": since})
    if sales:
        db.execute(text("INSERT INTO bd_sales VALUES (:s,:c,:d,:q) ON CONFLICT DO NOTHING"),
                   [{"s": int(r["loc"]), "c": int(r["code"]), "d": r["d"], "q": float(r["q"] or 0)} for r in sales])
    db.execute(text("DELETE FROM bd_stock"))
    if stock:
        db.execute(text("INSERT INTO bd_stock VALUES (:s,:c,:q,:a)"),
                   [{"s": int(r["loc"]), "c": int(r["code"]), "q": float(r["q"] or 0), "a": now} for r in stock])
    db.execute(text("DELETE FROM bd_moves WHERE day >= :d"), {"d": since})
    if moves:
        db.execute(text("INSERT INTO bd_moves VALUES (:s,:c,:d,:i,:o) ON CONFLICT DO NOTHING"),
                   [{"s": int(r["loc"]), "c": int(r["code"]), "d": r["d"], "i": float(r["inq"] or 0), "o": float(r["outq"] or 0)}
                    for r in moves])
    info = {"stores": len(locs), "articles": len(codes), "sales_rows": len(sales), "at": now.strftime("%d.%m %H:%M")}
    db.execute(text("INSERT INTO bd_runs(kind, info) VALUES ('sync', :i)"), {"i": str(info)})
    db.commit()
    return info


# ------------------------------------------------------------------ прогноза и заявка
def _forecast(series: dict, day: date, last: date) -> float:
    same = []
    for i, w in enumerate(WEEK_W, 1):
        d = day - timedelta(days=7 * i)
        while d > last:
            d -= timedelta(days=7)
        same.append((series.get(d, 0.0), w))
    wk = sum(q * w for q, w in same) / sum(w for _, w in same)
    avg14 = sum(series.get(last - timedelta(days=i), 0.0) for i in range(DAYS)) / DAYS
    return 0.6 * wk + 0.4 * avg14


def _cover(delivery_days: set[int], day: date) -> int:
    """Колко дни трябва да покрие доставката на `day` - до следващата доставка по графика."""
    for i in range(1, 8):
        if (day + timedelta(days=i)).weekday() in delivery_days:
            return i
    return 7


def schedule(db: Session) -> dict[int, set[int]]:
    ensure(db)
    return {loc: {int(x) for x in (d or "").split(",") if x.strip().isdigit()}
            for loc, d in db.execute(text("SELECT loc, delivery_days FROM bd_schedule")).all()}


def build(db: Session, day: date, save: bool = True) -> dict:
    """Заявка за доставка на `day` за всички магазини, които имат доставка в този ден."""
    ensure(db)
    last = db.execute(text("SELECT MAX(day) FROM bd_sales")).scalar()
    if not last:
        return {"ready": False, "lines": []}
    sales = db.execute(text("SELECT loc, code, day, qty FROM bd_sales WHERE day >= :d"), {"d": last - timedelta(days=35)}).all()
    stock = {(s, k): float(q) for s, k, q in db.execute(text("SELECT loc, code, qty FROM bd_stock")).all()}
    arts = {k: (n, d or 4, s) for k, n, d, s in db.execute(text("SELECT code, name, shelf_days, sub FROM bd_articles WHERE active")).all()}
    stores = {i: n for i, n in db.execute(text("SELECT loc, name FROM bd_stores WHERE active")).all()}
    sch = schedule(db)
    mv = db.execute(text("SELECT loc, code, SUM(delivered), SUM(returned) FROM bd_moves WHERE day >= :d GROUP BY loc, code"),
                    {"d": last - timedelta(days=28)}).all()
    waste = {(s, k): (float(o or 0) / (float(i or 0) + float(o or 0))) if (float(i or 0) + float(o or 0)) > 0 else 0.0
             for s, k, i, o in mv}
    S = defaultdict(dict)
    for st, k, d, q in sales:
        S[(st, k)][d] = max(float(q or 0), 0.0)
    out = []
    for (st, k), ser in S.items():
        if st not in stores or k not in arts:
            continue
        dd = sch.get(st) or {0, 1, 2, 3, 4, 5}
        if day.weekday() not in dd:
            continue
        sold14 = sum(ser.get(last - timedelta(days=i), 0.0) for i in range(DAYS))
        if sold14 <= 0:
            continue
        name, shelf, sub = arts[k]
        cover = _cover(dd, day)
        need = sum(_forecast(ser, day + timedelta(days=i), last) for i in range(cover))
        w = waste.get((st, k), 0.0)
        safety = 0.0 if w > 0.15 else (SAFETY * 0.5 if w > 0.08 else SAFETY)
        target = need * (1 + safety)
        cap = sum(_forecast(ser, day + timedelta(days=i), last) for i in range(max(1, min(shelf - 1, 7))))
        raw = stock.get((st, k), 0.0)
        note = []
        # над това, което се продава до изтичане на срока, наличността е невъзможна (грешно заведено / върнато)
        q0 = min(max(raw, 0.0), cap)
        if raw > cap + 2:
            note.append(f"съмнителна наличност {raw:g} (повече, отколкото се продава до изтичане) - броя {q0:.0f}")
        qty = max(0, math.ceil(target - q0 - 0.25))
        qty = int(min(qty, max(0, math.floor(cap - q0 + 0.5))))
        if qty == 0 and sold14 >= 2:
            qty = 1
            note.append("поне 1 бр. - никога 0 за артикул, който се продава")
        if raw < 0:
            note.append(f"в Мистрал е {raw:g} (незаведени доставки?)")
        if w > 0.08:
            note.append(f"брак {round(100 * w)}% - по-малък резерв")
        if cover > 1:
            note.append(f"покрива {cover} дни до следващата доставка")
        out.append({"loc": st, "store": stores[st], "code": k, "name": name, "sub": sub or "", "shelf": shelf,
                    "sold14": round(sold14, 1), "per_day": round(sold14 / DAYS, 2), "need": round(need, 1),
                    "stock": round(raw, 1), "cap": round(cap, 1), "qty": qty, "waste_pct": round(100 * w, 1),
                    "cover": cover, "note": "; ".join(note)})
    out.sort(key=lambda r: (r["store"], r["sub"], r["name"]))
    if save:
        db.execute(text("DELETE FROM bd_orders WHERE delivery_day = :d"), {"d": day})
        if out:
            db.execute(text("""INSERT INTO bd_orders(delivery_day, loc, code, qty, per_day, need, stock, cap, waste_pct, cover_days, note)
                               VALUES (:d,:s,:c,:q,:p,:n,:st,:cap,:w,:cv,:nt)"""),
                       [{"d": day, "s": o["loc"], "c": o["code"], "q": o["qty"], "p": o["per_day"], "n": o["need"], "st": o["stock"],
                         "cap": o["cap"], "w": o["waste_pct"], "cv": o["cover"], "nt": o["note"]} for o in out])
        db.execute(text("INSERT INTO bd_runs(kind, info) VALUES ('orders', :i)"),
                   {"i": f"{day.isoformat()}: {sum(1 for o in out if o['qty'] > 0)} реда, {sum(o['qty'] for o in out)} бр."})
        db.commit()
    plan = defaultdict(lambda: {"qty": 0, "stores": 0})
    for o in out:
        if o["qty"] > 0:
            p = plan[(o["code"], o["name"], o["sub"])]
            p["qty"] += o["qty"]; p["stores"] += 1
    return {"ready": True, "day": day.isoformat(), "weekday": WD[day.weekday()], "data_until": last.strftime("%d.%m.%Y"),
            "lines": out, "stores": len({o["loc"] for o in out}),
            "units": sum(o["qty"] for o in out),
            "plan": sorted([{"code": c, "name": n, "sub": s, **v} for (c, n, s), v in plan.items()], key=lambda x: -x["qty"])}


def saved(db: Session, day: date) -> dict:
    ensure(db)
    rows = db.execute(text("""SELECT o.loc, s.name, o.code, a.name, a.sub, o.qty, o.per_day, o.need, o.stock, o.cap, o.waste_pct,
                                     o.cover_days, o.note, o.created_at
                              FROM bd_orders o JOIN bd_stores s ON s.loc = o.loc JOIN bd_articles a ON a.code = o.code
                              WHERE o.delivery_day = :d ORDER BY s.name, a.sub, a.name"""), {"d": day}).all()
    if not rows:
        return build(db, day, save=True)
    lines = [{"loc": r[0], "store": r[1], "code": r[2], "name": r[3], "sub": r[4] or "", "qty": r[5], "per_day": float(r[6] or 0),
              "need": float(r[7] or 0), "stock": float(r[8] or 0), "cap": float(r[9] or 0), "waste_pct": float(r[10] or 0),
              "cover": r[11], "note": r[12] or ""} for r in rows]
    plan = defaultdict(lambda: {"qty": 0, "stores": 0})
    for o in lines:
        if o["qty"] > 0:
            p = plan[(o["code"], o["name"], o["sub"])]
            p["qty"] += o["qty"]; p["stores"] += 1
    last = db.execute(text("SELECT MAX(day) FROM bd_sales")).scalar()
    return {"ready": True, "day": day.isoformat(), "weekday": WD[day.weekday()], "data_until": last.strftime("%d.%m.%Y") if last else None,
            "created": rows[0][13].astimezone(SOFIA).strftime("%d.%m %H:%M"), "lines": lines,
            "stores": len({o["loc"] for o in lines}), "units": sum(o["qty"] for o in lines),
            "plan": sorted([{"code": c, "name": n, "sub": s, **v} for (c, n, s), v in plan.items()], key=lambda x: -x["qty"])}


def set_schedule(db: Session, loc: int, days: list[int], note: str | None = None) -> dict:
    ensure(db)
    v = ",".join(str(d) for d in sorted(set(days)) if 0 <= d <= 6)
    db.execute(text("""INSERT INTO bd_schedule(loc, delivery_days, note) VALUES (:l,:d,:n)
                       ON CONFLICT (loc) DO UPDATE SET delivery_days = EXCLUDED.delivery_days, note = COALESCE(EXCLUDED.note, bd_schedule.note)"""),
               {"l": loc, "d": v, "n": note})
    db.commit()
    return {"loc": loc, "delivery_days": v}


def schedule_list(db: Session) -> list[dict]:
    ensure(db)
    return [{"loc": l, "store": n, "days": [int(x) for x in (d or "").split(",") if x.strip().isdigit()], "note": nt, "active": a}
            for l, n, d, nt, a in db.execute(text("""SELECT s.loc, s.name, c.delivery_days, c.note, s.active FROM bd_stores s
                                                     LEFT JOIN bd_schedule c ON c.loc = s.loc ORDER BY s.name""")).all()]


def xlsx(db: Session, day: date) -> bytes:
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font
    d = saved(db, day)
    wb = Workbook(); ws = wb.active; ws.title = "План за цеха"
    ws.append([f"Бандитс - план за производство за {day.strftime('%d.%m.%Y')} ({d.get('weekday')})"]); ws["A1"].font = Font(bold=True, size=13)
    ws.append(["Код", "Артикул", "Група", "Бройки", "Магазини"])
    for p in d["plan"]:
        ws.append([p["code"], p["name"], p["sub"], p["qty"], p["stores"]])
    ws.append(["", "ОБЩО", "", d["units"], d["stores"]])
    for c, wdt in zip("ABCDE", (8, 46, 22, 10, 10)):
        ws.column_dimensions[c].width = wdt
    by = defaultdict(list)
    for o in d["lines"]:
        if o["qty"] > 0:
            by[o["store"]].append(o)
    ws2 = wb.create_sheet("По магазини")
    ws2.append(["Магазин", "Код", "Артикул", "Бройки", "Продава/ден", "Наличност", "Бележка"])
    for st in sorted(by):
        for o in by[st]:
            ws2.append([st, o["code"], o["name"], o["qty"], o["per_day"], o["stock"], o["note"]])
    for c, wdt in zip("ABCDEFG", (28, 8, 44, 9, 11, 10, 50)):
        ws2.column_dimensions[c].width = wdt
    for st in sorted(by):
        w = wb.create_sheet(st[:30].replace("/", "-").replace("\\", "-").replace("*", "").replace("?", "").replace("[", "").replace("]", "").replace(":", ""))
        w.append([f"{st} - доставка {day.strftime('%d.%m.%Y')}"]); w["A1"].font = Font(bold=True)
        w.append(["Код", "Артикул", "Бройки"])
        for o in by[st]:
            w.append([o["code"], o["name"], o["qty"]])
        w.column_dimensions["B"].width = 44
    bio = BytesIO(); wb.save(bio)
    return bio.getvalue()
