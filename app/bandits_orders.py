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
SOFT_API = "https://api-production-eda9.up.railway.app"       # Бандитс софтуеър (документите: стокови, КИ)
MASTER = "https://bandits-import.netlify.app/master.json"     # обекти и артикули на Бандитс
MAP_FIX = {26359: 669, 25833: 550}                                      # тортия чипотле - без код в името
OBHOD_API = "https://script.google.com/macros/s/AKfycbzxTtMoij_A6yORV2-OE2RBEF1U5ZkEo1vAQZOMaYW4U_32KPbrKQ-7wnuLbTmMjmJrmQ/exec"
LEFT_EXTRA = {"Закуски|Рогче": 537, "Закуски|Кренвиршка": 539, "Закуски|Баница": 538, "Закуски|Милинка": 542}
LEFT_DAYS = 3                                                 # остатък от обхода важи до 3 дни
EXT_BASE = 100000                                             # обектите извън Мистрал: id от 100001
ALIAS = {"ДЕТЛАВРЕНТИЙ": "ЛАВРЕНТИЙ", "ДЕТЕЛИНАТРОШЕВО": "ТРОШЕВО", "ПР21РОЗА": "РОЗА"}
SHELF_CAT = {"Салати": 4, "Десерти": 7, "Принцеса": 7, "ВЕГА": 4}


def _norm(s: str) -> str:
    import re
    k = re.sub(r"[^А-ЯA-Z0-9]", "", (s or "").upper().replace("300 ", "", 1))
    return ALIAS.get(k, k)


def _bcode_of(name: str) -> int | None:
    """Кодът на Бандитс е в края на името в Мистрал: „... 200ГР 446“, „... / 510“."""
    import re
    m = re.search(r"(?:/\s*)?\b(\d{2,3})\s*$", (name or "").strip())
    return int(m.group(1)) if m else None

SCHEMA = """
CREATE TABLE IF NOT EXISTS bd_stores (loc INT PRIMARY KEY, name TEXT, active BOOLEAN DEFAULT TRUE);
CREATE TABLE IF NOT EXISTS bd_articles (code INT PRIMARY KEY, name TEXT, sub TEXT, shelf_days INT, price NUMERIC(12,4),
  active BOOLEAN DEFAULT TRUE, updated_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS bd_sales (loc INT, code INT, day DATE, qty NUMERIC(12,3), PRIMARY KEY (day, loc, code));
CREATE TABLE IF NOT EXISTS bd_stock (loc INT, code INT, qty NUMERIC(12,3), at TIMESTAMPTZ, PRIMARY KEY (loc, code));
CREATE TABLE IF NOT EXISTS bd_moves (loc INT, code INT, day DATE, delivered NUMERIC(12,3), returned NUMERIC(12,3),
  PRIMARY KEY (day, loc, code));
CREATE TABLE IF NOT EXISTS bd_schedule (loc INT PRIMARY KEY, delivery_days TEXT DEFAULT '0,1,3,4', note TEXT);
CREATE TABLE IF NOT EXISTS bd_map (mcode INT PRIMARY KEY, bcode INT);
CREATE TABLE IF NOT EXISTS bd_ext (obekt TEXT PRIMARY KEY, id SERIAL, razdel TEXT, firma TEXT, grad TEXT, loc INT);
CREATE TABLE IF NOT EXISTS bd_soft (day DATE, store_id INT, bcode INT, delivered NUMERIC(12,3), returned NUMERIC(12,3),
  PRIMARY KEY (day, store_id, bcode));
CREATE TABLE IF NOT EXISTS bd_meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS bd_moni (delivery_day DATE, store_id INT, code INT, qty NUMERIC(10,2), sent_at TEXT,
  PRIMARY KEY (delivery_day, store_id, code));
CREATE TABLE IF NOT EXISTS bd_left (store_id INT, bcode INT, day DATE, qty NUMERIC(10,2), src TEXT, PRIMARY KEY (store_id, bcode));
CREATE TABLE IF NOT EXISTS bd_orders (delivery_day DATE, loc INT, code INT, qty INT, per_day NUMERIC(10,2),
  need NUMERIC(10,2), stock NUMERIC(10,2), cap NUMERIC(10,2), waste_pct NUMERIC(6,1), cover_days INT, note TEXT,
  created_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY (delivery_day, loc, code));
CREATE TABLE IF NOT EXISTS bd_runs (id SERIAL PRIMARY KEY, kind TEXT, at TIMESTAMPTZ DEFAULT now(), info TEXT);
"""


def ensure(db: Session):
    for q in SCHEMA.strip().split(";"):
        if q.strip():
            db.execute(text(q))
    # v2 (23.10): артикулите са по кодовете на Бандитс; график по подразбиране пон, вт, чет, пет
    if not db.execute(text("SELECT 1 FROM bd_meta WHERE k = 'v2'")).first():
        db.execute(text("TRUNCATE bd_articles, bd_orders"))
        db.execute(text("ALTER TABLE bd_schedule ALTER COLUMN delivery_days SET DEFAULT '0,1,3,4'"))
        db.execute(text("UPDATE bd_schedule SET delivery_days = '0,1,3,4'"))
        db.execute(text("INSERT INTO bd_meta VALUES ('v2', 'ok')"))
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
        cur.execute(f"""SELECT MATERIALCODE AS code, MAX(MATERIAL) AS nm, MAX(CATEGORY) AS cat, MAX(AVGDELIVERYPRICE) AS p
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
    for code in codes:
        m_ = mat.get(code, {})
        nm = (m_.get("nm") or str(code)).strip()
        b = MAP_FIX.get(code) or _bcode_of(nm) or code              # без код на Бандитс в името -> остава кодът от Мистрал (напр. принцеса с кайма)
        db.execute(text("INSERT INTO bd_map VALUES (:m,:b) ON CONFLICT (mcode) DO UPDATE SET bcode = EXCLUDED.bcode"), {"m": code, "b": b})
        db.execute(text("""INSERT INTO bd_articles(code, name, sub, shelf_days, price) VALUES (:c,:n,:s,:d,:p)
                           ON CONFLICT (code) DO UPDATE SET shelf_days = COALESCE(bd_articles.shelf_days, EXCLUDED.shelf_days),
                           active = TRUE, updated_at = now()"""),
                   {"c": b, "n": nm, "s": cname.get((m_.get("cat") or "").strip(), ""), "d": SHELF.get(code), "p": float(m_.get("p") or 0)})
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


def sync_soft(db: Session, days: int = 35) -> dict:
    """Обектите и артикулите на Бандитс (master.json) + доставки (стокови) и връщания (КИ) от Бандитс софтуеър."""
    import json as _json
    import urllib.parse
    import urllib.request
    ensure(db)
    master = _json.loads(urllib.request.urlopen(MASTER, timeout=60).read().decode())
    locs = {_norm(n): l for l, n in db.execute(text("SELECT loc, name FROM bd_stores WHERE loc < :e"), {"e": EXT_BASE}).all()}
    for o in master.get("objects", []):
        ob = (o.get("obekt") or "").strip()
        if not ob:
            continue
        loc = locs.get(_norm(ob)) if (o.get("razdel") or "").startswith("300") else None
        db.execute(text("""INSERT INTO bd_ext(obekt, razdel, firma, grad, loc) VALUES (:o,:r,:f,:g,:l)
                           ON CONFLICT (obekt) DO UPDATE SET razdel = EXCLUDED.razdel, firma = EXCLUDED.firma,
                           grad = EXCLUDED.grad, loc = EXCLUDED.loc"""),
                   {"o": ob, "r": o.get("razdel"), "f": o.get("firma"), "g": o.get("grad"), "l": loc})
    ext = {o: (EXT_BASE + i, l, r) for o, i, l, r in db.execute(text("SELECT obekt, id, loc, razdel FROM bd_ext")).all()}
    for ob, (eid, loc, razdel) in ext.items():
        if loc is None:
            db.execute(text("INSERT INTO bd_stores(loc, name) VALUES (:i,:n) ON CONFLICT (loc) DO UPDATE SET name = EXCLUDED.name"),
                       {"i": eid, "n": ob})
            db.execute(text("INSERT INTO bd_schedule(loc) VALUES (:i) ON CONFLICT DO NOTHING"), {"i": eid})
    for a in master.get("articles", []):
        try:
            c = int(a["code"])
        except Exception:
            continue
        db.execute(text("""INSERT INTO bd_articles(code, name, sub, shelf_days) VALUES (:c,:n,:s,:d)
                           ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, sub = EXCLUDED.sub, active = TRUE,
                           shelf_days = COALESCE(bd_articles.shelf_days, EXCLUDED.shelf_days)"""),
                   {"c": c, "n": a.get("name") or str(c), "s": a.get("cat") or "", "d": SHELF_CAT.get(a.get("cat") or "", 6)})
    since = datetime.now(SOFIA).date() - timedelta(days=days)
    q = urllib.parse.urlencode({"name": "АРТИКУЛИ", "from": since.strftime("%d.%m.%Y")})
    rows = _json.loads(urllib.request.urlopen(f"{SOFT_API}/sheet?{q}", timeout=180).read().decode()).get("data", [])
    agg = defaultdict(lambda: [0.0, 0.0])
    issued = defaultdict(set)                          # обект -> дати на изписване (за графика)
    for r in rows:
        kind = r.get("Вид")
        if kind not in ("Стокова", "КИ"):
            continue
        e = ext.get((r.get("Обект") or "").strip())
        if not e:
            continue
        sid = e[1] if e[1] is not None else e[0]
        try:
            c = int(r.get("Код")); qn = float(str(r.get("Кол.") or 0).replace(",", "."))
            d = date.fromisoformat(r.get("_d"))
        except Exception:
            continue
        if kind == "Стокова":
            issued[sid].add(d)
            d = d + timedelta(days=1)                   # стоковата се изписва в деня преди доставката
        agg[(d, sid, c)][0 if kind == "Стокова" else 1] += abs(qn)
    # остатъците от обхода на Бела (какво е в хладилника при посещението)
    nleft = 0
    try:
        om = {k: int(v) for k, v in (master.get("orderMap") or {}).items() if str(v).isdigit()}
        om.update(LEFT_EXTRA)
        lr = _json.loads(urllib.request.urlopen(f"{OBHOD_API}?action=getLeftovers&days=7", timeout=120).read().decode()).get("rows", {})
        db.execute(text("DELETE FROM bd_left"))
        for key, v in lr.items():
            ob, rest = key.split("|", 1) if "|" in key else (key, "")
            e = ext.get(ob.strip()); c = om.get(rest)
            if not e or not c:
                continue
            try:
                d = datetime.strptime(v.get("date", ""), "%d.%m.%Y").date(); qn = float(v.get("qty") or 0)
            except Exception:
                continue
            db.execute(text("""INSERT INTO bd_left VALUES (:s,:c,:d,:q,:src) ON CONFLICT (store_id, bcode)
                               DO UPDATE SET day = EXCLUDED.day, qty = EXCLUDED.qty, src = EXCLUDED.src WHERE bd_left.day <= EXCLUDED.day"""),
                       {"s": e[1] if e[1] is not None else e[0], "c": c, "d": d, "q": qn, "src": v.get("source")})
            nleft += 1
    except Exception:
        db.rollback()
    # график на доставките от Бандитс софтуеър: дните на изписване за последните 4 седмици (поне 2 пъти) + 1 ден
    # (ръчно зададеният в таба не се пипа)
    nsch = 0
    lim = datetime.now(SOFIA).date() - timedelta(days=28)
    for sid, ds in issued.items():
        cnt = defaultdict(int)
        for d in ds:
            if d >= lim:
                cnt[d.weekday()] += 1
        wd = sorted({(w + 1) % 7 for w, n in cnt.items() if n >= 2})
        if wd:
            db.execute(text("""INSERT INTO bd_schedule(loc, delivery_days, note) VALUES (:l,:d,'бандитс')
                               ON CONFLICT (loc) DO UPDATE SET delivery_days = EXCLUDED.delivery_days, note = 'бандитс'
                               WHERE bd_schedule.note IS DISTINCT FROM 'ръчно'"""), {"l": sid, "d": ",".join(map(str, wd))})
            nsch += 1
    db.execute(text("DELETE FROM bd_soft WHERE day >= :d"), {"d": since})
    if agg:
        db.execute(text("INSERT INTO bd_soft VALUES (:d,:s,:c,:i,:o)"),
                   [{"d": d, "s": s_, "c": c, "i": v[0], "o": v[1]} for (d, s_, c), v in agg.items()])
    db.commit()
    return {"objects": len(ext), "lines": len(rows), "rows": len(agg), "leftovers": nleft, "schedules": nsch}


def sync_all(db: Session) -> dict:
    out = {"mistral": sync(db)}
    try:
        out["soft"] = sync_soft(db)
    except Exception as e:
        db.rollback()
        out["soft"] = f"{type(e).__name__}: {e}"[:200]
    return out


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
    """Заявка за доставка на `day` за всички обекти с доставка в този ден.
    Магазини 300: продажбите от касата (Мистрал). Другите обекти: взето минус върнато (Бандитс софтуеър).
    Наличност: доставено от Бандитс за последните (срок-1) дни минус продаденото - Мистрал за Бандитс е неточен."""
    ensure(db)
    today = datetime.now(SOFIA).date()
    last = db.execute(text("SELECT MAX(day) FROM bd_sales")).scalar() or (today - timedelta(days=1))
    mp = {m: b for m, b in db.execute(text("SELECT mcode, bcode FROM bd_map")).all()}
    arts = {k: (n, d or 6, s) for k, n, d, s in db.execute(text("SELECT code, name, shelf_days, sub FROM bd_articles WHERE active")).all()}
    stores = {i: n for i, n in db.execute(text("SELECT loc, name FROM bd_stores WHERE active")).all()}
    sch = schedule(db)
    S = defaultdict(lambda: defaultdict(float))            # (store, bcode) -> {day: продадено}
    for st, k, d, q in db.execute(text("SELECT loc, code, day, qty FROM bd_sales WHERE day >= :d"), {"d": last - timedelta(days=35)}).all():
        S[(st, mp.get(k, k))][d] += max(float(q or 0), 0.0)
    mstock = defaultdict(float)
    for st, k, q in db.execute(text("SELECT loc, code, qty FROM bd_stock")).all():
        mstock[(st, mp.get(k, k))] += float(q or 0)
    D = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))   # (store, bcode) -> {day: [доставено, върнато]}
    for d, st, c, i, o in db.execute(text("SELECT day, store_id, bcode, delivered, returned FROM bd_soft WHERE day >= :d"),
                                     {"d": today - timedelta(days=35)}).all():
        D[(st, c)][d][0] += float(i or 0); D[(st, c)][d][1] += float(o or 0)
    LEFT = {(s_, c): (d, float(q)) for s_, c, d, q in db.execute(text("SELECT store_id, bcode, day, qty FROM bd_left WHERE day >= :d"),
                                                                {"d": today - timedelta(days=LEFT_DAYS)}).all()}
    out = []
    for key in set(S) | set(D):
        st, k = key
        if st not in stores or k not in arts:
            continue
        dd = sch.get(st) or {0, 1, 3, 4}
        if day.weekday() not in dd:
            continue
        name, shelf, sub = arts[k]
        cover = _cover(dd, day)
        dl = D.get(key, {})
        deliv28 = sum(v[0] for d, v in dl.items() if d > today - timedelta(days=28))
        ret28 = sum(v[1] for d, v in dl.items() if d > today - timedelta(days=28))
        w = ret28 / deliv28 if deliv28 > 0 else 0.0
        ser = S.get(key)
        if st < EXT_BASE:                               # магазин 300: САМО касата от Мистрал (Димо, 23.10)
            ser = ser or {}
            sold14 = sum(ser.get(last - timedelta(days=i), 0.0) for i in range(DAYS))
            fc = lambda x: _forecast(ser, x, last)       # noqa: E731
            src = "каса"
        else:                                           # друг обект: взето - върнато за 28 дни
            net = max(deliv28 - ret28, 0.0)
            sold14 = net / 2
            fc = lambda x, _p=net / 28: _p               # noqa: E731
            src = "взето−върнато"
        if sold14 <= 0:
            continue
        per_day = sold14 / DAYS
        # наличността трябва да стигне до ОБЯД в деня на следващата доставка (Димо, 23.10): + половин ден
        need = sum(fc(day + timedelta(days=i)) for i in range(cover)) + 0.5 * fc(day + timedelta(days=cover))
        safety = 0.0 if w > 0.15 else (SAFETY * 0.5 if w > 0.08 else SAFETY)
        target = need * (1 + safety)
        cap = sum(fc(day + timedelta(days=i)) for i in range(max(1, min(shelf - 1, 7))))
        # наличност при доставката: доставеното за последните (срок-1) дни минус продаденото/очакваното до деня
        k_ = max(1, shelf - 1)
        got = sum(v[0] - v[1] for d, v in dl.items() if d > today - timedelta(days=k_))
        note = []
        lf = LEFT.get(key)
        if lf:
            # Бела е видяла X бр. сутринта на ден L: + доставеното след това − продаденото от L до доставката
            L_, lq = lf
            after = sum(v[0] for d, v in dl.items() if d > L_)
            if st < EXT_BASE:
                sold_since = sum(q for d, q in ser.items() if L_ <= d <= last)
            else:
                sold_since = per_day * max(0, (last - L_).days + 1)
            sold_since += per_day * max(0, (day - last).days - 1)
            est = max(0.0, lq + after - sold_since)
            src_st = f"по обхода на Бела ({L_.strftime('%d.%m')}: {lq:g} бр.)"
        elif dl:
            gone = sum(ser.get(today - timedelta(days=i), 0.0) for i in range(1, k_ + 1)) if st < EXT_BASE else per_day * k_
            gone += per_day * max(0, (day - last).days - 1)          # от последния ден с данни до доставката
            est = max(0.0, got - gone)
            src_st = "по доставките на Бандитс"
        else:
            est = min(max(mstock.get(key, 0.0), 0.0), cap)
            src_st = "по Мистрал"
        est = min(est, cap)
        qty = max(0, math.ceil(target - est - 0.25))
        qty = int(min(qty, max(0, math.floor(cap - est + 0.5))))
        if qty == 0 and sold14 >= 2:
            qty = 1
            note.append("поне 1 бр. - никога 0 за артикул, който се продава")
        if w > 0.08:
            note.append(f"върнати {round(100 * w)}% - по-малък резерв")
        note.append(f"до {WD[(day + timedelta(days=cover)).weekday()]} обяд ({cover + 0.5:g} дни)")
        out.append({"loc": st, "store": stores[st], "code": k, "name": name, "sub": sub or "", "shelf": shelf,
                    "sold14": round(sold14, 1), "per_day": round(per_day, 2), "need": round(need, 1),
                    "stock": round(est, 1), "cap": round(cap, 1), "qty": qty, "waste_pct": round(100 * w, 1),
                    "cover": cover, "note": "; ".join([f"{src}, наличност {src_st}"] + note)})
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


def set_schedule(db: Session, loc: int, days: list[int], note: str | None = "ръчно") -> dict:
    ensure(db)
    v = ",".join(str(d) for d in sorted(set(days)) if 0 <= d <= 6)
    db.execute(text("""INSERT INTO bd_schedule(loc, delivery_days, note) VALUES (:l,:d,:n)
                       ON CONFLICT (loc) DO UPDATE SET delivery_days = EXCLUDED.delivery_days, note = EXCLUDED.note"""),
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


# ------------------------------------------------------------------ сравнение с Мони
def compare(db: Session, day: date) -> dict:
    """Нашата заявка за доставка на `day` срещу това, което Мони е пуснала (стоковите на Бандитс за деня),
    и колко реално се е продало до следващата доставка (каса - само за магазините 300)."""
    ensure(db)
    stores = {i: n for i, n in db.execute(text("SELECT loc, name FROM bd_stores")).all()}
    arts = {k: (n, s) for k, n, s in db.execute(text("SELECT code, name, sub FROM bd_articles")).all()}
    mp = {m: b for m, b in db.execute(text("SELECT mcode, bcode FROM bd_map")).all()}
    sch = schedule(db)
    ours = {(l, c): (int(q or 0), int(cv or 1)) for l, c, q, cv in db.execute(text(
        "SELECT loc, code, qty, cover_days FROM bd_orders WHERE delivery_day = :d"), {"d": day}).all()}
    moni = {(s_, c): float(q or 0) for s_, c, q in db.execute(text(
        "SELECT store_id, code, qty FROM bd_moni WHERE delivery_day = :d AND qty > 0"), {"d": day}).all()}
    moni_src = "заявката на Мони"
    sent = db.execute(text("SELECT MAX(sent_at) FROM bd_moni WHERE delivery_day = :d"), {"d": day}).scalar()
    if not moni:                                     # няма я в „Бандитс заявки“ -> какво е издадено (стоковите)
        moni = {(s_, c): float(i or 0) for s_, c, i in db.execute(text(
            "SELECT store_id, bcode, delivered FROM bd_soft WHERE day = :d AND delivered > 0"), {"d": day}).all()}
        moni_src = "стоковите за деня"
    last = db.execute(text("SELECT MAX(day) FROM bd_sales")).scalar()
    sold = defaultdict(float)
    for l, k, d, q in db.execute(text("SELECT loc, code, day, qty FROM bd_sales WHERE day >= :d AND day < :e"),
                                 {"d": day, "e": day + timedelta(days=7)}).all():
        cv = _cover(sch.get(l) or {0, 1, 3, 4}, day)
        if d < day + timedelta(days=cv):
            sold[(l, mp.get(k, k))] += max(float(q or 0), 0.0)
    rows = []
    for key in set(ours) | set(moni):
        l, c = key
        if l not in stores:
            continue
        o, cv = ours.get(key, (0, _cover(sch.get(l) or {0, 1, 3, 4}, day)))
        m_ = moni.get(key, 0.0)
        has_sales = l < EXT_BASE and last is not None and last >= day + timedelta(days=cv - 1)
        s_ = sold.get(key) if has_sales else None
        better = None
        if s_ is not None and abs(o - m_) >= 1:
            better = "MinMaxAI" if abs(o - s_) < abs(m_ - s_) else ("Мони" if abs(m_ - s_) < abs(o - s_) else "равно")
        n, sub = arts.get(c, (str(c), ""))
        rows.append({"loc": l, "store": stores[l], "code": c, "name": n, "sub": sub or "", "ours": o, "moni": m_,
                     "diff": o - m_, "sold": s_, "better": better})
    rows.sort(key=lambda r: (r["store"], r["sub"], r["name"]))
    tot = {"ours": sum(r["ours"] for r in rows), "moni": sum(r["moni"] for r in rows),
           "same": sum(1 for r in rows if abs(r["diff"]) < 1), "lines": len(rows),
           "ours_better": sum(1 for r in rows if r["better"] == "MinMaxAI"),
           "moni_better": sum(1 for r in rows if r["better"] == "Мони"),
           "judged": sum(1 for r in rows if r["better"] in ("MinMaxAI", "Мони", "равно"))}
    ours_at = db.execute(text("SELECT MIN(created_at) FROM bd_orders WHERE delivery_day = :d"), {"d": day}).scalar()
    return {"day": day.isoformat(), "weekday": WD[day.weekday()], "has_ours": bool(ours), "has_moni": bool(moni),
            "moni_src": moni_src, "moni_sent": sent, "ours_at": ours_at.astimezone(SOFIA).strftime("%d.%m %H:%M") if ours_at else None,
            "sales_until": last.strftime("%d.%m.%Y") if last else None, "totals": tot, "rows": rows}


# ------------------------------------------------------------------ заявката на Мони (Бандитс заявки)
def fetch_moni(db: Session, day: date) -> dict:
    """Заявката на Мони за доставка на `day` от приложението „Бандитс заявки“.
    Апито връща заявката за същия ден от седмицата ПРЕДИ подадената дата -> питаме с day+7."""
    import json as _json
    import urllib.request
    ensure(db)
    r = _json.loads(urllib.request.urlopen(f"{OBHOD_API}?action=getPreviousOrder&date={(day + timedelta(days=7)).strftime('%d.%m.%Y')}",
                                           timeout=120).read().decode())
    if not r.get("found") or r.get("dateDelivery") != day.strftime("%d.%m.%Y"):
        return {"found": False}
    master = _json.loads(urllib.request.urlopen(MASTER, timeout=60).read().decode())
    om = {k: int(v) for k, v in (master.get("orderMap") or {}).items() if str(v).isdigit()}
    om.update(LEFT_EXTRA)
    ext = {o: (EXT_BASE + i if l is None else l) for o, i, l in db.execute(text("SELECT obekt, id, loc FROM bd_ext")).all()}
    up = {o.upper(): sid for o, sid in ext.items()}
    pref = {"Сити": ["СИТИ "], "Стар": ["СТАР "], "Други": [""]}
    agg, miss = defaultdict(float), set()
    for row in r.get("rows", []):
        ob = (row.get("obekt") or "").strip().upper()
        sh = row.get("sheet") or ""
        cands = (["300 "] if sh.startswith("300") else pref.get(sh, ["300 ", "СИТИ ", "СТАР ", ""]))
        sid = next((up[p_ + ob] for p_ in cands if p_ + ob in up), None)
        if sid is None:                                  # „Царевец“ -> „300 ЦАРЕВЕЦ+“, „ПР-21 Роза“ и т.н.
            sid = next((v for k, v in up.items() if _norm(k) == _norm(ob) and (not sh.startswith("300") or k.startswith("300"))), None)
        c = om.get(f"{row.get('cat')}|{row.get('art')}")
        if sid is None or c is None:
            miss.add(f"{row.get('obekt')} / {row.get('cat')}|{row.get('art')}")
            continue
        agg[(sid, c)] += float(row.get("qty") or 0)
    db.execute(text("DELETE FROM bd_moni WHERE delivery_day = :d"), {"d": day})
    if agg:
        db.execute(text("INSERT INTO bd_moni VALUES (:d,:s,:c,:q,:t)"),
                   [{"d": day, "s": s_, "c": c, "q": q, "t": r.get("sentAt")} for (s_, c), q in agg.items()])
    db.commit()
    return {"found": True, "sent_at": r.get("sentAt"), "version": r.get("version"), "lines": len(agg), "unmatched": sorted(miss)[:20]}
