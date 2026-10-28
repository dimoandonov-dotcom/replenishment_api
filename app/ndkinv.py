"""
Електронни фактури на НДК (PDF от noreply@ndkbg.com в invoices@trista.bg):
    четем фактурата по НДК код -> очаквани бройки по артикул в Мистрал
    -> сравняваме с осчетоводеното (OCR) в Мистрал по номер на фактура
    -> разминаванията (размяна на артикул, грешно количество, липсващ ред) светват същия ден.
Докато фактурата не е осчетоводена, редовете ѝ са „в път“ - точна стока в път без OCR.
"""
from __future__ import annotations

import io
import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

TZ = ZoneInfo("Europe/Sofia")
SCHEMA = """
CREATE TABLE IF NOT EXISTS ndk_invoices (
  num TEXT PRIMARY KEY, mail_uid TEXT, inv_date DATE, delivery_date DATE, store_name TEXT, store_id INT, loc INT,
  lines JSONB, n_lines INT, total NUMERIC(12,2), fetched_at TIMESTAMPTZ DEFAULT now(),
  booked_num BIGINT, booked_at TIMESTAMPTZ, booked JSONB, diffs JSONB, status TEXT DEFAULT 'pending', checked_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS ndk_invoices_store ON ndk_invoices(store_id, inv_date);
CREATE TABLE IF NOT EXISTS ndk_mail_state (k TEXT PRIMARY KEY, v TEXT);
ALTER TABLE ndk_invoices ADD COLUMN IF NOT EXISTS obj TEXT;
CREATE TABLE IF NOT EXISTS ndk_obj_map (obj TEXT PRIMARY KEY, store_id INT, loc INT);
CREATE TABLE IF NOT EXISTS ndk_code_map (code TEXT PRIMARY KEY, mc INT, how TEXT, n INT DEFAULT 1, at TIMESTAMPTZ DEFAULT now())
"""
_ok = False
PACK = re.compile(r"(\d+)\s*бр\s*/\s*(стек|каса|кашон|пакет)", re.I)
CODE_IN_NAME = re.compile(r"(?<!\d)(\d{4,6})(?!\d)")


def ensure(db: Session):
    global _ok
    if not _ok:
        for s in SCHEMA.strip().split(";"):
            if s.strip():
                db.execute(text(s))
        db.commit()
        _ok = True


# ---------------------------------------------------------------------------
# 1) Четене на PDF
# ---------------------------------------------------------------------------
def parse_pdf(data: bytes) -> dict:
    import pdfplumber
    head, lines = "", []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pi, page in enumerate(pdf.pages):
            if pi == 0:
                head = page.extract_text() or ""
            for tb in page.extract_tables() or []:
                for row in tb:
                    if not row or len(row) < 7:
                        continue
                    no = (row[0] or "").strip()
                    if not no.isdigit():
                        continue
                    code = (row[3] or "").strip()
                    name = re.sub(r"\s+", " ", (row[4] or "").replace("\n", " ")).strip()
                    unit = (row[5] or "").strip().lower()
                    try:
                        qty = float((row[6] or "0").replace(",", "."))
                    except ValueError:
                        qty = 0.0
                    m = PACK.search(name)
                    per = int(m.group(1)) if m else 1
                    pieces = qty * (per if unit in ("стек", "каса", "кашон", "пакет") else 1)
                    try:
                        total = float((row[-1] or "0").replace(",", "."))
                    except ValueError:
                        total = 0.0
                    lines.append({"no": int(no), "code": code, "name": name, "unit": unit, "qty": qty, "per": per,
                                  "pieces": pieces, "total": total})
    num = re.search(r"(\d{6,10})\s*/\s*(\d{2}\.\d{2}\.\d{4})", head)
    store = re.search(r'МАГАЗИН\s*"([^"]+)"', head)
    deliv = re.search(r"ДАТА НА ДОСТАВКА\s*:\s*(\d{2}\.\d{2}\.\d{4})", head)
    obj = re.search(r"ОБЕКТ\s*:\s*(.+?)\s+РЕГИСТРАЦИОНЕН", head)
    addr = re.search(r"АДРЕС ЗА ДОСТАВКА\s*:\s*(.+)", head)
    eik = re.search(r"ЕИК\s*:\s*(\d+)", head)
    def _d(s):
        try:
            return datetime.strptime(s, "%d.%m.%Y").date()
        except Exception:
            return None
    return {"num": num.group(1).lstrip("0") if num else None, "inv_date": _d(num.group(2)) if num else None,
            "store_name": store.group(1).strip() if store else None,
            "delivery_date": _d(deliv.group(1)) if deliv else None, "lines": lines,
            "obj": " | ".join(x for x in [(eik.group(1) if eik else ""), (obj.group(1).strip() if obj else ""),
                                          (addr.group(1).strip() if addr else "")] if x)[:300],
            "total": sum(l["total"] for l in lines)}


# ---------------------------------------------------------------------------
# 2) НДК код -> артикул в Мистрал (от имената: „БИРА АРИАНА 1Л  463929“, „... /10235“)
# ---------------------------------------------------------------------------
_map_cache: dict = {"at": None, "map": {}, "names": {}}


def code_map(db: Session) -> tuple[dict[str, int], dict[int, str]]:
    """{ндк код: MATERIALCODE}, {MATERIALCODE: име}. Еднозначни кодове; при двусмислие се пропуска."""
    from . import mistral
    now = datetime.now()
    if _map_cache["at"] and now - _map_cache["at"] < timedelta(hours=6):
        return _map_cache["map"], _map_cache["names"]
    with mistral.use("varna"), mistral.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT MATERIALCODE AS c, MAX(MATERIAL) AS n FROM MATERIAL WITH (NOLOCK) WHERE ACTIVE = 1 GROUP BY MATERIALCODE")
        rows = cur.fetchall()
    cand: dict[str, set] = {}
    names = {}
    for r in rows:
        mc, name = int(r["c"]), r["n"] or ""
        names[mc] = name
        tail = name.split("/", 1)[1] if "/" in name else ""
        codes = set(CODE_IN_NAME.findall(tail))
        m = re.search(r"\s(\d{5,6})\s*$", name)
        if m:
            codes.add(m.group(1))
        for c in codes:
            cand.setdefault(c, set()).add(mc)
    cm = {c: next(iter(v)) for c, v in cand.items() if len(v) == 1}
    try:   # научени връзки (от сдвоени редове фактура <-> Мистрал)
        ensure(db)
        for c, mc in db.execute(text("SELECT code, mc FROM ndk_code_map")).all():
            cm[c] = int(mc)
    except Exception:
        db.rollback()
    _map_cache.update({"at": now, "map": cm, "names": names})
    return cm, names


# ---------------------------------------------------------------------------
# 3) Осчетоводеното в Мистрал по номер на фактура
# ---------------------------------------------------------------------------
def booked_lines(loc: int, num: str) -> tuple[int | None, datetime | None, dict[int, float]]:
    from . import mistral
    import os
    with mistral.use("varna"), mistral.connect() as conn:
        cur = conn.cursor()
        cur.execute("""SELECT TOP 1 o.NUM AS num, od.DOCUMENTDATE AS d FROM OPERATIONDOCUMENT od WITH (NOLOCK)
                       JOIN OPERATIONS o WITH (NOLOCK) ON o.NUM = od.NUM AND o.LOCATIONID = od.LOCATIONID
                       WHERE od.LOCATIONID = %s AND o.PARTNERNAMEID = %s
                         AND (od.DOCUMENTNUM = %s OR od.DOCUMENTNUM = %s) ORDER BY o.NUM DESC""",
                    (loc, int(os.getenv("NDK_PARTNER_ID", "186")), num, num.zfill(10)))
        r = cur.fetchone()
        if not r:
            return None, None, {}
        cur.execute("""SELECT MATERIALCODE AS c, SUM(QTY) AS q, MAX(OPERATIONDATE) AS d FROM MATERIALQTYLOG WITH (NOLOCK)
                       WHERE LOCATIONID = %s AND OPERAIONNUM = %s AND OPERATIONTYPE IN (2, 24) GROUP BY MATERIALCODE""", (loc, r["num"]))
        rows = cur.fetchall()
        at = max((x["d"] for x in rows if x["d"]), default=None)
        return int(r["num"]), at, {int(x["c"]): float(x["q"] or 0) for x in rows if float(x["q"] or 0) != 0}


def find_loc(num: str) -> int | None:
    """В кой обект на Мистрал е осчетоводена фактурата (по номер)."""
    from . import mistral
    import os
    with mistral.use("varna"), mistral.connect() as conn:
        cur = conn.cursor()
        cur.execute("""SELECT TOP 1 od.LOCATIONID AS loc FROM OPERATIONDOCUMENT od WITH (NOLOCK)
                       JOIN OPERATIONS o WITH (NOLOCK) ON o.NUM = od.NUM AND o.LOCATIONID = od.LOCATIONID
                       WHERE o.PARTNERNAMEID = %s AND (od.DOCUMENTNUM = %s OR od.DOCUMENTNUM = %s)""",
                    (int(os.getenv("NDK_PARTNER_ID", "186")), num, num.zfill(10)))
        r = cur.fetchone()
        return int(r["loc"]) if r else None


AMB = {17729, 17730}   # амбалаж


def compare(inv: dict, cm: dict, names: dict, booked: dict[int, float]) -> list[dict]:
    exp: dict[int, float] = {}
    unmapped = []
    for l in inv["lines"]:
        mc = cm.get(l["code"])
        if mc is None:
            unmapped.append(l)
            continue
        exp[mc] = exp.get(mc, 0.0) + l["pieces"]
    bk = {c: q for c, q in booked.items() if c not in AMB}
    diffs = []
    for mc, q in exp.items():
        b = bk.get(mc, 0.0)
        if abs(b - q) > 0.01:
            diffs.append({"kind": "missing" if b == 0 else "qty", "mc": mc, "name": names.get(mc, ""), "expected": q, "booked": b})
    for mc, b in bk.items():
        if mc not in exp:
            diffs.append({"kind": "extra", "mc": mc, "name": names.get(mc, ""), "expected": 0.0, "booked": b})
    # размяна: липсващ ред и излишен ред с едно и също количество
    miss = [d for d in diffs if d["kind"] == "missing"]
    extra = [d for d in diffs if d["kind"] == "extra"]
    for m_ in miss:
        for e in extra:
            if not e.get("paired") and abs(e["booked"] - m_["expected"]) < 0.01:
                m_["kind"], m_["swap_with"], e["paired"] = "swap", e["mc"], True
                m_["swap_name"] = e["name"]
                break
    diffs = [d for d in diffs if not d.get("paired")]
    # неразпознат ред от фактурата + излишен ред в Мистрал със същите бройки -> същият артикул (учим се)
    learned = []
    extra = [d for d in diffs if d["kind"] == "extra"]
    for l in unmapped:
        same = [e for e in extra if not e.get("paired") and abs(e["booked"] - l["pieces"]) < 0.01]
        same_u = [u for u in unmapped if abs(u["pieces"] - l["pieces"]) < 0.01]
        if len(same) == 1 and len(same_u) == 1:
            same[0]["paired"] = True
            learned.append((l["code"], same[0]["mc"]))
            continue
        if len(same) > 1:   # по ключова дума от името
            w = (l["name"].split() or [""])[0].upper()
            hit = [e for e in same if w and w in (e["name"] or "").upper()]
            if len(hit) == 1:
                hit[0]["paired"] = True
                learned.append((l["code"], hit[0]["mc"]))
                continue
        diffs.append({"kind": "unmapped", "code": l["code"], "name": l["name"], "expected": l["pieces"], "booked": None})
    diffs = [d for d in diffs if not d.get("paired")]
    compare.learned = learned
    return diffs


# ---------------------------------------------------------------------------
# 4) Обхождане на пощата и записване
# ---------------------------------------------------------------------------
def _store_id(db: Session, store_name: str | None) -> int | None:
    from .imports import normalize_store_name
    from . import models as m
    from sqlalchemy import select
    if not store_name:
        return None
    n = normalize_store_name(store_name)
    for s in db.execute(select(m.Store)).scalars():
        if normalize_store_name(s.name) == n:
            return s.id
    for al in db.execute(select(m.StoreAlias)).scalars():
        if al.alias_normalized == n:
            return al.store_id
    return None


def _loc_for(db: Session, store_id: int | None) -> int | None:
    if store_id is None:
        return None
    from . import mistral
    from .anomalies import _loc_map
    with mistral.use("varna"), mistral.connect() as conn:
        locs = _loc_map(db, conn.cursor())
    return next((l for l, s in locs.items() if s == store_id), None)


def sync_mail(db: Session, days: int = 3, limit: int = 400) -> dict:
    """Нови фактури от пощата -> ndk_invoices (status pending)."""
    import email
    import datetime as dt
    from . import ndkmail
    ensure(db)
    known = {r[0] for r in db.execute(text("SELECT mail_uid FROM ndk_invoices WHERE mail_uid IS NOT NULL")).all()}
    M = ndkmail._conn()
    new, skipped = 0, 0
    try:
        M.select("INBOX", readonly=True)
        since = (dt.date.today() - dt.timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = M.search(None, f'(SINCE {since} FROM "ndkbg.com")')
        ids = data[0].split()
        for i in ids[-limit:]:
            uid = i.decode()
            if uid in known:
                continue
            typ, full = M.fetch(i, "(BODY.PEEK[])")
            msg = email.message_from_bytes(b"".join(p[1] for p in full if isinstance(p, tuple)))
            pdf = None
            for part in msg.walk():
                fn = part.get_filename() or ""
                if fn.lower().endswith(".pdf"):
                    pdf = part.get_payload(decode=True)
                    break
            if not pdf:
                skipped += 1
                continue
            try:
                inv = parse_pdf(pdf)
            except Exception:
                skipped += 1
                continue
            if not inv["num"] or not inv["lines"]:
                skipped += 1
                continue
            sid = _store_id(db, inv["store_name"])
            loc = None
            if inv.get("obj"):
                om = db.execute(text("SELECT store_id, loc FROM ndk_obj_map WHERE obj = :o"), {"o": inv["obj"]}).first()
                if om:
                    sid = sid or om[0]
                    loc = om[1]
            db.execute(text("""INSERT INTO ndk_invoices(num, mail_uid, inv_date, delivery_date, store_name, store_id, loc, obj, lines, n_lines, total)
                               VALUES (:n,:u,:d,:dd,:sn,:s,:lc,:ob,CAST(:l AS jsonb),:nl,:t)
                               ON CONFLICT (num) DO UPDATE SET mail_uid = EXCLUDED.mail_uid, obj = EXCLUDED.obj"""),
                       {"n": inv["num"], "u": uid, "d": inv["inv_date"], "dd": inv["delivery_date"], "sn": inv["store_name"],
                        "s": sid, "lc": loc, "ob": inv.get("obj"), "l": json.dumps(inv["lines"], ensure_ascii=False),
                        "nl": len(inv["lines"]), "t": inv["total"]})
            new += 1
        db.commit()
    finally:
        M.logout()
    return {"new": new, "skipped": skipped, "seen": len(ids)}


def check_pending(db: Session, max_age_days: int = 14) -> dict:
    """За всяка неосчетоводена фактура: намираме я в Мистрал и сравняваме."""
    ensure(db)
    cm, names = code_map(db)
    rows = db.execute(text("""SELECT num, store_id, loc, lines, status, obj FROM ndk_invoices
                              WHERE status IN ('pending','differs') AND inv_date >= :d ORDER BY inv_date""" ),
                      {"d": date.today() - timedelta(days=max_age_days)}).all()
    checked, booked_n, differs = 0, 0, 0
    locmap = None
    for num, sid, loc, lines, status, obj in rows:
        if loc is None and sid is not None:
            loc = _loc_for(db, sid)
        if loc is None:
            loc = find_loc(num)          # осчетоводена ли е някъде -> оттам знаем обекта
            if loc is None:
                continue
            if locmap is None:
                from . import mistral
                from .anomalies import _loc_map
                with mistral.use("varna"), mistral.connect() as conn:
                    locmap = _loc_map(db, conn.cursor())
            sid = sid or locmap.get(loc)
            if obj:
                db.execute(text("""INSERT INTO ndk_obj_map(obj, store_id, loc) VALUES (:o,:s,:l)
                                   ON CONFLICT (obj) DO UPDATE SET store_id = :s, loc = :l"""), {"o": obj, "s": sid, "l": loc})
        db.execute(text("UPDATE ndk_invoices SET loc = :l, store_id = COALESCE(store_id, :s) WHERE num = :n"),
                   {"l": loc, "s": sid, "n": num})
        onum, at, booked = booked_lines(loc, num)
        checked += 1
        if onum is None:
            continue
        inv = {"lines": lines if isinstance(lines, list) else json.loads(lines)}
        diffs = compare(inv, cm, names, booked)
        for code, mc in getattr(compare, "learned", []):
            db.execute(text("""INSERT INTO ndk_code_map(code, mc, how) VALUES (:c,:m,'pair')
                               ON CONFLICT (code) DO UPDATE SET n = ndk_code_map.n + (CASE WHEN ndk_code_map.mc = :m THEN 1 ELSE 0 END)"""),
                       {"c": code, "m": mc})
            cm[code] = mc
        real = [d for d in diffs if d["kind"] != "unmapped"]
        st = "differs" if real else "ok"
        if st == "differs":
            differs += 1
        booked_n += 1
        db.execute(text("""UPDATE ndk_invoices SET booked_num = :o, booked_at = :a, booked = CAST(:b AS jsonb),
                           diffs = CAST(:df AS jsonb), status = :s, checked_at = now() WHERE num = :n"""),
                   {"o": onum, "a": at, "b": json.dumps({str(k): v for k, v in booked.items()}),
                    "df": json.dumps(diffs, ensure_ascii=False), "s": st, "n": num})
    db.commit()
    return {"checked": checked, "booked": booked_n, "differs": differs}


def run(db: Session) -> dict:
    a = sync_mail(db)
    b = check_pending(db)
    return {"mail": a, "check": b}


# ---------------------------------------------------------------------------
# 5) Справки
# ---------------------------------------------------------------------------
def listing(db: Session, days: int = 7, store_id: int | None = None, only_diff: bool = False) -> list[dict]:
    ensure(db)
    q = """SELECT i.num, i.inv_date, i.delivery_date, i.store_name, i.store_id, s.name, i.n_lines, i.total, i.status,
                  i.booked_at, i.diffs FROM ndk_invoices i LEFT JOIN stores s ON s.id = i.store_id
           WHERE i.inv_date >= :d""" + (" AND i.store_id = :s" if store_id else "") + (" AND i.status = 'differs'" if only_diff else "") + \
        " ORDER BY i.inv_date DESC, i.num DESC LIMIT 600"
    rows = db.execute(text(q), {"d": date.today() - timedelta(days=days), "s": store_id}).all()
    out = []
    for r in rows:
        diffs = r[10] or []
        out.append({"num": r[0], "date": r[1].strftime("%d.%m") if r[1] else None, "delivery": r[2].strftime("%d.%m") if r[2] else None,
                    "store_name": r[3], "store_id": r[4], "store": r[5], "lines": r[6], "total": float(r[7] or 0), "status": r[8],
                    "booked_at": r[9].astimezone(TZ).strftime("%d.%m %H:%M") if r[9] else None,
                    "diffs": [d for d in diffs if d["kind"] != "unmapped"], "unmapped": sum(1 for d in diffs if d["kind"] == "unmapped")})
    return out


def pending_transit(db: Session) -> dict[tuple[int, str], float]:
    """{(store_id, MATERIALCODE като str): бройки} - фактурирано от НДК, още неосчетоводено = стока в път (без OCR)."""
    ensure(db)
    cm, _ = code_map(db)
    rows = db.execute(text("""SELECT store_id, lines FROM ndk_invoices WHERE status = 'pending' AND store_id IS NOT NULL
                              AND inv_date >= :d"""), {"d": date.today() - timedelta(days=5)}).all()
    out: dict = {}
    for sid, lines in rows:
        for l in (lines if isinstance(lines, list) else json.loads(lines)):
            mc = cm.get(l["code"])
            if mc is not None:
                out[(sid, str(mc))] = out.get((sid, str(mc)), 0.0) + l["pieces"]
    return out


def text_report(db: Session, days: int = 1) -> str:
    rows = listing(db, days, only_diff=True)
    if not rows:
        return ""
    L = [f"📄 НДК фактури с разминаване спрямо осчетоводеното ({len(rows)}):"]
    for r in rows[:25]:
        L.append(f"\n• {r['store'] or r['store_name']} - ф. {r['num']} ({r['date']}), осчетоводена {r['booked_at']}")
        for d in r["diffs"][:6]:
            if d["kind"] == "swap":
                L.append(f"   🔁 {d['name']} {d['expected']:g} бр. е записан като {d['swap_name']}")
            elif d["kind"] == "missing":
                L.append(f"   ❌ липсва: {d['name']} {d['expected']:g} бр.")
            elif d["kind"] == "extra":
                L.append(f"   ➕ излишен: {d['name']} {d['booked']:g} бр. (няма го във фактурата)")
            else:
                L.append(f"   ≠ {d['name']}: фактура {d['expected']:g}, записани {d['booked']:g}")
    return "\n".join(L)
