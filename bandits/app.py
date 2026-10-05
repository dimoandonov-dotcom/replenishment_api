"""
Бандитс AI — заявки и план за производство по продажбите в магазините 300.

Данни от Мистрал (само четене): продажби, наличности, доставки и върнат брак на
артикулите на Бандитс по магазини. Прогноза по магазин × артикул × ден от седмицата,
заявка, съобразена със срока на годност (непродаденото се връща като брак на Бандитс),
и общ план за производство в цеха.
"""
from __future__ import annotations

import base64, hashlib, hmac, json, math, os, secrets, threading, time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from io import BytesIO

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import create_engine, text

SOFIA = timezone(timedelta(hours=3))
DB = create_engine(os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/bandits").replace("postgres://", "postgresql://"),
                   pool_pre_ping=True, future=True)
API_KEY = os.getenv("API_KEY", "")
SECRET = os.getenv("SESSION_SECRET", API_KEY or "dev")
SUPPLIER = os.getenv("SUPPLIER_LIKE", "БАНДИТС")
HISTORY_DAYS = 63
app = FastAPI(title="Бандитс AI")

SHELF = {939: 4, 22249: 4, 9874: 4, 9873: 4, 16570: 6, 16549: 6, 21088: 6, 16551: 6, 26341: 6, 26343: 6, 26342: 6,
         18359: 6, 18938: 6, 18917: 6, 19027: 6, 21086: 6, 16537: 6, 24347: 7, 24346: 7, 22846: 7, 16567: 7,
         16697: 7, 22844: 7, 16694: 4, 16562: 4, 16561: 5, 16692: 4}

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (id INT PRIMARY KEY, name TEXT NOT NULL, active BOOLEAN DEFAULT TRUE);
CREATE TABLE IF NOT EXISTS articles (code INT PRIMARY KEY, name TEXT, sub TEXT, shelf_days INT, active BOOLEAN DEFAULT TRUE,
  price NUMERIC(12,4), updated_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS sales (store_id INT, code INT, day DATE, qty NUMERIC(12,3), rev NUMERIC(12,2), cost NUMERIC(12,2),
  PRIMARY KEY (day, store_id, code));
CREATE TABLE IF NOT EXISTS stock (store_id INT, code INT, qty NUMERIC(12,3), at TIMESTAMPTZ, PRIMARY KEY (store_id, code));
CREATE TABLE IF NOT EXISTS moves (store_id INT, code INT, day DATE, delivered NUMERIC(12,3), returned NUMERIC(12,3),
  PRIMARY KEY (day, store_id, code));
CREATE TABLE IF NOT EXISTS orders (day DATE, store_id INT, code INT, qty INT, forecast NUMERIC(12,2), stock NUMERIC(12,2),
  cap NUMERIC(12,2), waste_pct NUMERIC(6,1), note TEXT, created_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY (day, store_id, code));
CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, name TEXT, pw TEXT, role TEXT DEFAULT 'user');
CREATE TABLE IF NOT EXISTS runs (id SERIAL PRIMARY KEY, kind TEXT, at TIMESTAMPTZ DEFAULT now(), info TEXT);
"""


def init():
    with DB.begin() as c:
        for q in SCHEMA.strip().split(";"):
            if q.strip():
                c.execute(text(q))
        # единствен потребител: Димо (никой друг няма достъп)
        c.execute(text("DELETE FROM users WHERE username <> 'dimo'"))
        if os.getenv("PW_DIMO"):
            c.execute(text("""INSERT INTO users(username, name, pw, role) VALUES ('dimo','Димо',:p,'admin')
                              ON CONFLICT (username) DO UPDATE SET pw = EXCLUDED.pw"""), {"p": _hash(os.getenv("PW_DIMO"))})


# ---------------------------------------------------------------- вход
def _hash(pw: str) -> str:
    salt = secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000).hex()


def _check(pw: str, h: str) -> bool:
    salt, d = h.split("$", 1)
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000).hex(), d)


def _token(user: str) -> str:
    exp = int(time.time()) + 30 * 86400
    msg = f"{user}|{exp}"
    sig = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{msg}|{sig}".encode()).decode()


def _user(request: Request) -> str | None:
    if API_KEY and request.headers.get("x-api-key") == API_KEY:
        return "system"
    t = request.cookies.get("bs")
    if not t:
        return None
    try:
        user, exp, sig = base64.urlsafe_b64decode(t.encode()).decode().split("|")
        ok = hmac.compare_digest(sig, hmac.new(SECRET.encode(), f"{user}|{exp}".encode(), hashlib.sha256).hexdigest())
        return user if ok and int(exp) > time.time() else None
    except Exception:
        return None


@app.middleware("http")
async def auth(request: Request, call_next):
    if request.url.path in ("/", "/health", "/api/login", "/robots.txt"):
        res = await call_next(request)
    elif not _user(request):
        res = JSONResponse({"detail": "Нужен е вход"}, status_code=401)
    else:
        res = await call_next(request)
    # никакво индексиране и кеширане от търсачки и чужди сайтове
    res.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    res.headers["Referrer-Policy"] = "no-referrer"
    res.headers["X-Frame-Options"] = "DENY"
    res.headers["Cache-Control"] = "no-store"
    return res


@app.get("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(p: LoginIn):
    with DB.connect() as c:
        r = c.execute(text("SELECT pw, name FROM users WHERE username = :u"), {"u": p.username.strip().lower()}).first()
    if not r or not _check(p.password, r[0]):
        raise HTTPException(401, "Грешен потребител или парола")
    res = JSONResponse({"ok": True, "name": r[1]})
    res.set_cookie("bs", _token(p.username.strip().lower()), max_age=30 * 86400, httponly=True, secure=True, samesite="lax")
    return res


@app.post("/api/logout")
def logout():
    res = JSONResponse({"ok": True}); res.delete_cookie("bs"); return res


@app.get("/api/me")
def me(request: Request):
    u = _user(request)
    with DB.connect() as c:
        n = c.execute(text("SELECT name FROM users WHERE username = :u"), {"u": u}).scalar()
    return {"user": u, "name": n or u}


@app.get("/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------- Мистрал
def _mssql():
    import pymssql
    return pymssql.connect(server=os.getenv("MSSQL_HOST", ""), port=int(os.getenv("MSSQL_PORT", "1433")),
                           user=os.getenv("MSSQL_USER", ""), password=os.getenv("MSSQL_PASSWORD", ""),
                           database=os.getenv("MSSQL_DB", "MistralAnalytics"), login_timeout=20, timeout=600,
                           as_dict=True, charset="UTF-8")


SYNC = {"running": False, "last": None, "error": None, "progress": ""}


def sync(days: int = HISTORY_DAYS) -> dict:
    t0 = time.time()
    today = datetime.now(SOFIA).date()
    since = today - timedelta(days=days)
    with _mssql() as conn:
        cur = conn.cursor()
        SYNC["progress"] = "доставчик и артикули"
        cur.execute("SELECT ID FROM PARTNERNAME WHERE UPPER(PARTNERNAME) LIKE %s", (f"%{SUPPLIER}%",))
        pids = [int(r["ID"]) for r in cur.fetchall()]
        if not pids:
            raise RuntimeError("Не намирам доставчика в Мистрал")
        P = ",".join(map(str, pids))
        cur.execute(f"""SELECT DISTINCT c.MATERIALCODE AS code FROM SALECONTENT c WITH (NOLOCK)
                        JOIN SALE s WITH (NOLOCK) ON s.NUM = c.NUM AND s.LOCATIONID = c.LOCATIONID
                        WHERE c.LASTPARTNERNAMEID IN ({P}) AND s.REPORTINGDATE >= %s""", (since.isoformat(),))
        codes = sorted({int(r["code"]) for r in cur.fetchall()})
        if not codes:
            raise RuntimeError("Няма продажби на доставчика")
        C = ",".join(map(str, codes))
        cur.execute(f"""SELECT MATERIALCODE AS code, MAX(SEARCHNAME) AS nm, MAX(CATEGORY) AS cat, MAX(AVGDELIVERYPRICE) AS p
                        FROM MATERIAL WITH (NOLOCK) WHERE MATERIALCODE IN ({C}) GROUP BY MATERIALCODE""")
        mat = {int(r["code"]): r for r in cur.fetchall()}
        cur.execute("SELECT CATEGORY AS code, MAX(NAME) AS name FROM CATEGORY GROUP BY CATEGORY")
        cname = {(r["code"] or "").strip(): r["name"] for r in cur.fetchall()}
        # обекти с продажби на Бандитс = магазините 300
        SYNC["progress"] = "продажби"
        cur.execute(f"""SELECT s.LOCATIONID AS loc, c.MATERIALCODE AS code, s.REPORTINGDATE AS d,
                               SUM(c.QTY) AS q, SUM(c.QTY * c.SALEPRICE) AS rev, SUM(c.QTY * c.AVGDELIVERYPRICE) AS cost
                        FROM SALE s WITH (NOLOCK) JOIN SALECONTENT c WITH (NOLOCK) ON c.NUM = s.NUM AND c.LOCATIONID = s.LOCATIONID
                        WHERE c.MATERIALCODE IN ({C}) AND s.REPORTINGDATE >= %s AND s.REPORTINGDATE < %s
                        GROUP BY s.LOCATIONID, c.MATERIALCODE, s.REPORTINGDATE""", (since.isoformat(), today.isoformat()))
        sales = cur.fetchall()
        locs = sorted({int(r["loc"]) for r in sales})
        L = ",".join(map(str, locs))
        cur.execute(f"SELECT ID, NAME FROM LOCATION WHERE ID IN ({L})")
        lname = {int(r["ID"]): (r["NAME"] or "").strip() for r in cur.fetchall()}
        SYNC["progress"] = "наличности"
        cur.execute(f"""SELECT LOCATIONID AS loc, MATERIALCODE AS code, SUM(QTY) AS q FROM MATERIAL WITH (NOLOCK)
                        WHERE MATERIALCODE IN ({C}) AND LOCATIONID IN ({L}) GROUP BY LOCATIONID, MATERIALCODE""")
        stock = cur.fetchall()
        SYNC["progress"] = "доставки и брак"
        cur.execute(f"""SELECT l.LOCATIONID AS loc, l.MATERIALCODE AS code, CAST(l.OPERATIONDATE AS date) AS d,
                               SUM(CASE WHEN l.QTY > 0 THEN l.QTY ELSE 0 END) AS inq,
                               SUM(CASE WHEN l.QTY < 0 THEN -l.QTY ELSE 0 END) AS outq
                        FROM MATERIALQTYLOG l WITH (NOLOCK)
                        WHERE l.OPERATIONTYPE = 2 AND l.MATERIALCODE IN ({C}) AND l.LOCATIONID IN ({L})
                          AND l.OPERATIONDATE >= %s
                        GROUP BY l.LOCATIONID, l.MATERIALCODE, CAST(l.OPERATIONDATE AS date)""", (since.isoformat(),))
        moves = cur.fetchall()
    now = datetime.now(SOFIA)
    with DB.begin() as c:
        for loc in locs:
            c.execute(text("""INSERT INTO stores(id, name) VALUES (:i,:n) ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name"""),
                      {"i": loc, "n": lname.get(loc, str(loc))})
        for code in codes:
            m = mat.get(code, {})
            cat = (m.get("cat") or "").strip()
            c.execute(text("""INSERT INTO articles(code, name, sub, shelf_days, price, active) VALUES (:c,:n,:s,:d,:p,:a)
                              ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, sub = EXCLUDED.sub, price = EXCLUDED.price,
                              shelf_days = COALESCE(articles.shelf_days, EXCLUDED.shelf_days),
                              active = (articles.shelf_days IS NOT NULL OR EXCLUDED.shelf_days IS NOT NULL), updated_at = now()"""),
                      {"c": code, "n": m.get("nm") or str(code), "s": cname.get(cat, ""), "d": SHELF.get(code),
                       "p": float(m.get("p") or 0), "a": code in SHELF})
        c.execute(text("DELETE FROM sales WHERE day >= :d OR day >= :t"), {"d": since, "t": today})
        for r in sales:
            c.execute(text("INSERT INTO sales VALUES (:s,:c,:d,:q,:r,:k) ON CONFLICT DO NOTHING"),
                      {"s": int(r["loc"]), "c": int(r["code"]), "d": r["d"], "q": float(r["q"] or 0),
                       "r": float(r["rev"] or 0), "k": float(r["cost"] or 0)})
        c.execute(text("DELETE FROM stock"))
        for r in stock:
            c.execute(text("INSERT INTO stock VALUES (:s,:c,:q,:a)"),
                      {"s": int(r["loc"]), "c": int(r["code"]), "q": float(r["q"] or 0), "a": now})
        c.execute(text("DELETE FROM moves WHERE day >= :d"), {"d": since})
        for r in moves:
            c.execute(text("INSERT INTO moves VALUES (:s,:c,:d,:i,:o) ON CONFLICT DO NOTHING"),
                      {"s": int(r["loc"]), "c": int(r["code"]), "d": r["d"], "i": float(r["inq"] or 0), "o": float(r["outq"] or 0)})
        info = {"stores": len(locs), "articles": len(codes), "sales_rows": len(sales), "moves": len(moves),
                "seconds": round(time.time() - t0, 1)}
        c.execute(text("INSERT INTO runs(kind, info) VALUES ('sync', :i)"), {"i": json.dumps(info, ensure_ascii=False)})
    return info


# ---------------------------------------------------------------- прогноза и заявка
SAFETY = 0.15          # резерв над прогнозата
WEEK_W = [0.4, 0.3, 0.2, 0.1]   # тегла на последните 4 същи дни от седмицата


def _data():
    with DB.connect() as c:
        last = c.execute(text("SELECT MAX(day) FROM sales")).scalar()
        sales = c.execute(text("SELECT store_id, code, day, qty FROM sales WHERE day >= :d"),
                          {"d": (last or date.today()) - timedelta(days=35)}).all()
        stock = {(s, k): float(q) for s, k, q in c.execute(text("SELECT store_id, code, qty FROM stock")).all()}
        arts = {k: (n, d, s) for k, n, d, s in c.execute(text("SELECT code, name, shelf_days, sub FROM articles WHERE active")).all()}
        stores = {i: n for i, n in c.execute(text("SELECT id, name FROM stores WHERE active")).all()}
        mv = c.execute(text("""SELECT m.store_id, m.code, COALESCE(SUM(s.qty),0), SUM(m.returned) FROM moves m
                                LEFT JOIN (SELECT store_id, code, SUM(qty) qty FROM sales WHERE day >= :d GROUP BY store_id, code) s
                                  ON s.store_id = m.store_id AND s.code = m.code
                                WHERE m.day >= :d GROUP BY m.store_id, m.code"""),
                       {"d": (last or date.today()) - timedelta(days=28)}).all()
    s = defaultdict(dict)
    for st, k, d, q in sales:
        s[(st, k)][d] = max(float(q or 0), 0.0)
    # sold = продадено за 28 дни (mv[2]), o = върнато
    waste = {(st, k): (float(o or 0) / (float(i or 0) + float(o or 0))) if (float(i or 0) + float(o or 0)) > 0 else 0.0
             for st, k, i, o in mv}
    return last, s, stock, arts, stores, waste


def forecast(series: dict, day: date, last: date) -> float:
    """Очаквани продажби за ден: среднопретеглено от същия ден от седмицата (4 седмици) и общото средно за 14 дни."""
    same = []
    for i, w in enumerate(WEEK_W, 1):
        d = day - timedelta(days=7 * i)
        while d > last:
            d -= timedelta(days=7)
        same.append((series.get(d, 0.0), w))
    wk = sum(q * w for q, w in same) / sum(w for _, w in same)
    avg14 = sum(series.get(last - timedelta(days=i), 0.0) for i in range(14)) / 14
    return 0.6 * wk + 0.4 * avg14


def next_delivery_gap(day: date) -> int:
    """Доставки пон–съб; в събота се покриват 2 дни (неделя без доставка)."""
    return 2 if day.weekday() == 5 else 1


def build_orders(day: date, save: bool = True) -> dict:
    last, S, stock, arts, stores, waste = _data()
    if not last:
        return {"ready": False}
    out = []
    for (st, k), ser in S.items():
        if st not in stores or k not in arts:
            continue
        name, shelf, sub = arts[k]
        shelf = shelf or 4
        if sum(ser.values()) <= 0:
            continue
        gap = next_delivery_gap(day)
        need = sum(forecast(ser, day + timedelta(days=i), last) for i in range(gap))
        w = waste.get((st, k), 0.0)
        safety = 0.0 if w > 0.15 else (SAFETY * 0.5 if w > 0.08 else SAFETY)
        target = need * (1 + safety)
        # таван по срока: не повече, отколкото ще се продаде, докато е годно
        cap = sum(forecast(ser, day + timedelta(days=i), last) for i in range(max(1, min(shelf - 1, 7))))
        q = max(stock.get((st, k), 0.0), 0.0)
        qty = max(0, math.ceil(target - q - 0.25))
        qty = int(min(qty, max(0, math.floor(cap - q + 0.5))))
        note = []
        if w > 0.08:
            note.append(f"брак {round(100 * w)}% — по-малък резерв")
        if qty == 0 and target > q:
            note.append("ограничено от срока на годност")
        out.append({"store_id": st, "store": stores[st], "code": k, "name": name, "sub": sub, "shelf": shelf,
                    "forecast": round(need, 1), "stock": round(q, 1), "cap": round(cap, 1), "qty": qty,
                    "waste_pct": round(100 * w, 1), "note": "; ".join(note)})
    if save:
        with DB.begin() as c:
            c.execute(text("DELETE FROM orders WHERE day = :d"), {"d": day})
            for o in out:
                c.execute(text("""INSERT INTO orders(day, store_id, code, qty, forecast, stock, cap, waste_pct, note)
                                  VALUES (:d,:s,:c,:q,:f,:st,:cap,:w,:n)"""),
                          {"d": day, "s": o["store_id"], "c": o["code"], "q": o["qty"], "f": o["forecast"],
                           "st": o["stock"], "cap": o["cap"], "w": o["waste_pct"], "n": o["note"]})
            c.execute(text("INSERT INTO runs(kind, info) VALUES ('orders', :i)"),
                      {"i": json.dumps({"day": day.isoformat(), "lines": sum(1 for o in out if o["qty"] > 0),
                                        "units": sum(o["qty"] for o in out)})})
    return {"ready": True, "day": day.isoformat(), "data_until": last.isoformat(), "lines": out}


# ---------------------------------------------------------------- API
def _day(s: str | None) -> date:
    return date.fromisoformat(s) if s else datetime.now(SOFIA).date()


@app.post("/api/sync")
def api_sync(days: int = HISTORY_DAYS):
    if SYNC["running"]:
        return {"started": False, "status": SYNC}

    def run():
        SYNC.update(running=True, error=None)
        try:
            SYNC["last"] = sync(days)
            build_orders(datetime.now(SOFIA).date())
            build_orders(datetime.now(SOFIA).date() + timedelta(days=1))
        except Exception as e:
            SYNC["error"] = f"{type(e).__name__}: {e}"[:400]
        finally:
            SYNC.update(running=False, progress="")
    threading.Thread(target=run, daemon=True).start()
    return {"started": True}


@app.get("/api/sync")
def api_sync_status():
    with DB.connect() as c:
        r = c.execute(text("SELECT at, info FROM runs WHERE kind='sync' ORDER BY id DESC LIMIT 1")).first()
    return {**SYNC, "last_sync": r[0].astimezone(SOFIA).strftime("%d.%m.%Y %H:%M") if r else None,
            "last_info": json.loads(r[1]) if r else None}


@app.get("/api/orders")
def api_orders(day: str | None = None):
    d = _day(day)
    with DB.connect() as c:
        rows = c.execute(text("""SELECT o.store_id, s.name, o.code, a.name, a.shelf_days, o.qty, o.forecast, o.stock, o.cap,
                                        o.waste_pct, o.note, o.created_at FROM orders o
                                 JOIN stores s ON s.id = o.store_id JOIN articles a ON a.code = o.code
                                 WHERE o.day = :d ORDER BY s.name, a.name"""), {"d": d}).all()
    if not rows:
        res = build_orders(d)
        return api_orders(day) if res.get("ready") else {"ready": False}
    lines = [{"store_id": r[0], "store": r[1], "code": r[2], "name": r[3], "shelf": r[4], "qty": r[5],
              "forecast": float(r[6]), "stock": float(r[7]), "cap": float(r[8]), "waste_pct": float(r[9] or 0), "note": r[10]}
             for r in rows]
    return {"ready": True, "day": d.strftime("%d.%m.%Y"), "iso": d.isoformat(),
            "computed": rows[0][11].astimezone(SOFIA).strftime("%d.%m %H:%M"), "lines": lines}


@app.post("/api/orders/rebuild")
def api_orders_rebuild(day: str | None = None):
    r = build_orders(_day(day))
    return {"ok": r.get("ready", False)}


@app.get("/api/plan")
def api_plan(day: str | None = None):
    o = api_orders(day)
    if not o.get("ready"):
        return o
    agg = defaultdict(lambda: {"qty": 0, "stores": 0, "forecast": 0.0})
    for l in o["lines"]:
        a = agg[(l["code"], l["name"], l["shelf"])]
        a["qty"] += l["qty"]; a["forecast"] += l["forecast"]
        if l["qty"] > 0:
            a["stores"] += 1
    rows = [{"code": k[0], "name": k[1], "shelf": k[2], **v, "forecast": round(v["forecast"], 1)} for k, v in agg.items()]
    rows.sort(key=lambda x: -x["qty"])
    return {"ready": True, "day": o["day"], "computed": o["computed"], "rows": rows, "total": sum(r["qty"] for r in rows)}


@app.get("/api/daily")
def api_daily(day: str | None = None):
    """Продажби на Бандитс по магазини за ден + сравнение със същия ден миналата седмица и средното за 7 дни."""
    with DB.connect() as c:
        last = c.execute(text("SELECT MAX(day) FROM sales")).scalar()
        d = date.fromisoformat(day) if day else last
        if not d:
            return {"ready": False}
        rows = c.execute(text("""SELECT store_id, day, SUM(qty), SUM(rev), SUM(cost) FROM sales
                                 WHERE day BETWEEN :f AND :t AND code IN (SELECT code FROM articles WHERE active)
                                 GROUP BY store_id, day"""),
                         {"f": d - timedelta(days=14), "t": d}).all()
        stores = {i: n for i, n in c.execute(text("SELECT id, name FROM stores")).all()}
        mv = {s: (float(i or 0), float(o or 0)) for s, i, o in c.execute(text(
            "SELECT store_id, SUM(delivered), SUM(returned) FROM moves WHERE day = :d AND code IN (SELECT code FROM articles WHERE active) GROUP BY store_id"), {"d": d}).all()}
        arts = c.execute(text("""SELECT a.name, SUM(s.qty), SUM(s.rev), SUM(s.cost) FROM sales s JOIN articles a ON a.code = s.code
                                 WHERE s.day = :d AND a.active GROUP BY a.name ORDER BY SUM(s.rev) DESC"""), {"d": d}).all()
        series = c.execute(text("SELECT day, SUM(rev) FROM sales WHERE day > :f AND day <= :t AND code IN (SELECT code FROM articles WHERE active) GROUP BY day ORDER BY day"),
                           {"f": d - timedelta(days=28), "t": d}).all()
    per = defaultdict(dict)
    for s, dd, q, r, k in rows:
        per[s][dd] = (float(q or 0), float(r or 0), float(k or 0))
    out, tot = [], defaultdict(float)
    for s, byd in per.items():
        q0, r0, k0 = byd.get(d, (0, 0, 0)); rw = byd.get(d - timedelta(days=7), (0, 0, 0))[1]
        a7 = sum(byd.get(d - timedelta(days=i), (0, 0, 0))[1] for i in range(1, 8)) / 7
        dl, rt = mv.get(s, (0, 0))
        out.append({"store": stores.get(s, s), "units": round(q0), "rev": round(r0, 2), "margin_pct": round(100 * (r0 - k0) / r0, 1) if r0 else None,
                    "vs_week": round(100 * (r0 - rw) / rw, 1) if rw else None, "vs_avg7": round(100 * (r0 - a7) / a7, 1) if a7 else None,
                    "delivered": round(dl), "returned": round(rt)})
        tot["units"] += q0; tot["rev"] += r0; tot["cost"] += k0; tot["rw"] += rw; tot["a7"] += a7; tot["dl"] += dl; tot["rt"] += rt
    out.sort(key=lambda x: -x["rev"])
    return {"ready": True, "day": d.strftime("%d.%m.%Y"), "iso": d.isoformat(), "last": last.isoformat() if last else None,
            "weekday": ["понеделник", "вторник", "сряда", "четвъртък", "петък", "събота", "неделя"][d.weekday()],
            "totals": {"units": round(tot["units"]), "rev": round(tot["rev"], 2),
                       "margin_pct": round(100 * (tot["rev"] - tot["cost"]) / tot["rev"], 1) if tot["rev"] else None,
                       "vs_week": round(100 * (tot["rev"] - tot["rw"]) / tot["rw"], 1) if tot["rw"] else None,
                       "vs_avg7": round(100 * (tot["rev"] - tot["a7"]) / tot["a7"], 1) if tot["a7"] else None,
                       "delivered": round(tot["dl"]), "returned": round(tot["rt"]), "stores": sum(1 for x in out if x["units"] > 0)},
            "stores": out,
            "articles": [{"name": n, "units": round(float(q or 0)), "rev": round(float(r or 0), 2),
                          "margin_pct": round(100 * (float(r) - float(k)) / float(r), 1) if r else None} for n, q, r, k in arts],
            "series": [{"day": dd.strftime("%d.%m"), "rev": round(float(v or 0), 2)} for dd, v in series]}


@app.get("/api/waste")
def api_waste(days: int = 28):
    """Брак = върнато на Бандитс ÷ (продадено + върнато), по магазин и артикул."""
    with DB.connect() as c:
        last = c.execute(text("SELECT MAX(day) FROM sales")).scalar() or date.today()
        f = last - timedelta(days=days)
        rows = c.execute(text("""
            WITH so AS (SELECT store_id, code, SUM(qty) q FROM sales WHERE day > :f GROUP BY store_id, code),
                 re AS (SELECT store_id, code, SUM(returned) r FROM moves WHERE day > :f GROUP BY store_id, code)
            SELECT st.name, a.name, COALESCE(so.q,0), COALESCE(re.r,0), a.price
            FROM so FULL JOIN re ON re.store_id = so.store_id AND re.code = so.code
            JOIN stores st ON st.id = COALESCE(so.store_id, re.store_id)
            JOIN articles a ON a.code = COALESCE(so.code, re.code) AND a.active"""), {"f": f}).all()
    out = [{"store": s_, "name": n, "sold": round(float(q)), "returned": round(float(r)),
            "pct": round(100 * float(r) / (float(q) + float(r)), 1) if (float(q) + float(r)) > 0 else None,
            "eur": round(float(r) * float(p or 0), 2)} for s_, n, q, r, p in rows]
    out.sort(key=lambda x: -(x["eur"] or 0))
    so = sum(x["sold"] for x in out); rt = sum(x["returned"] for x in out)
    return {"days": days, "sold": so, "returned": rt, "pct": round(100 * rt / (so + rt), 1) if (so + rt) else None,
            "eur": round(sum(x["eur"] for x in out), 2), "rows": out}


@app.get("/api/articles")
def api_articles():
    with DB.connect() as c:
        rows = c.execute(text("SELECT code, name, sub, shelf_days, active FROM articles ORDER BY name")).all()
    return [{"code": r[0], "name": r[1], "sub": r[2], "shelf_days": r[3], "active": r[4]} for r in rows]


class ShelfIn(BaseModel):
    code: int
    shelf_days: int


@app.put("/api/articles")
def api_articles_put(items: list[ShelfIn]):
    with DB.begin() as c:
        for i in items:
            c.execute(text("UPDATE articles SET shelf_days = :d WHERE code = :c"), {"d": max(1, min(i.shelf_days, 30)), "c": i.code})
    return {"ok": True}


class ExportIn(BaseModel):
    filename: str = "Справка"
    columns: list[str]
    rows: list[list]


@app.post("/api/export-xlsx")
def export(p: ExportIn):
    from urllib.parse import quote
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook(); ws = wb.active; ws.title = p.filename[:31] or "Справка"
    ws.append(p.columns)
    for c in ws[1]:
        c.font = Font(name="Arial", bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="7A1F1F")
    for r in p.rows[:50000]:
        ws.append(r)
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.font = Font(name="Arial", size=10)
    for i, col in enumerate(ws.columns, 1):
        ws.column_dimensions[col[0].column_letter].width = min(60, max(10, max(len(str(c.value or "")) for c in col) + 2))
    ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
    buf = BytesIO(); wb.save(buf)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(p.filename + '.xlsx')}"})


# ---------------------------------------------------------------- нощно: 03:30 синхронизация + заявки за днес и утре
def scheduler():
    done = None
    while True:
        try:
            now = datetime.now(SOFIA)
            if now.hour == 3 and now.minute >= 30 and done != now.date() and not SYNC["running"]:
                done = now.date()
                SYNC.update(running=True, error=None)
                try:
                    SYNC["last"] = sync(7)
                    build_orders(now.date()); build_orders(now.date() + timedelta(days=1))
                except Exception as e:
                    SYNC["error"] = f"{type(e).__name__}: {e}"[:400]
                finally:
                    SYNC.update(running=False, progress="")
        except Exception:
            pass
        time.sleep(60)


@app.on_event("startup")
def startup():
    init()
    if os.getenv("DISABLE_SCHEDULER") != "1":
        threading.Thread(target=scheduler, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8") as f:
        return f.read()
