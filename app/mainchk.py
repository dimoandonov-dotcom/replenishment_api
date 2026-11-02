"""Проверка „главна база“ - наша версия на проверката на Хрис, без достъп до базите на обектите.

1) Изоставане: от колко време главната не е получила продажба от магазина, спрямо това колко
   продажби обикновено има за такова време. Ако се очакват 10+ продажби, а няма нито една -
   главната изостава (или касата/връзката на обекта не работи) и заявката се смята със стари данни.
2) Главна срещу „цел“: наличността в Мистрал срещу последната ревизия + движенията след нея
   (същото като колоната „Главна − цел“ при Хрис). Взима се от сутрешната снимка за всички магазини.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

TZ = ZoneInfo("Europe/Sofia")
LAG_MIN = 60          # поне час тишина (след изоставането на нашето копие)
LAG_EXPECTED = 10     # и поне 10 очаквани продажби за това време
DIFF_MIN = 6          # разлика в бройки за списъка „главна ≠ цел“
AMB = {17729, 17730, 6884}   # амбалаж и „стока бр“ - не са за заявки

_told: dict = {}


def lag(db: Session) -> dict:
    from . import mistral
    with mistral.use("varna"), mistral.connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT l.ID AS loc, l.NAME AS name, MAX(q.OPERATIONDATE) AS last_sale,
                   SUM(CASE WHEN q.OPERATIONDATE < CONVERT(date, GETDATE())
                             AND DATEPART(hour, q.OPERATIONDATE) BETWEEN 7 AND 22 THEN 1 ELSE 0 END) AS n7,
                   GETDATE() AS now
            FROM MATERIALQTYLOG q WITH (NOLOCK) JOIN LOCATION l ON l.ID = q.LOCATIONID
            WHERE q.OPERATIONTYPE = 1 AND q.OPERATIONDATE >= DATEADD(day, -7, CONVERT(date, GETDATE()))
            GROUP BY l.ID, l.NAME""")
        rows = cur.fetchall()
    if not rows:
        return {"copy_lag_min": None, "stores": []}
    now = rows[0]["now"]
    newest = max(r["last_sale"] for r in rows)
    copy_lag = max(0, int((now - newest).total_seconds() // 60))
    out = []
    for r in rows:
        silence = int((newest - r["last_sale"]).total_seconds() // 60)
        rate = (r["n7"] or 0) / (7 * 16 * 60)
        exp = round(rate * silence, 1)
        bad = silence >= LAG_MIN and exp >= LAG_EXPECTED and 7 <= now.hour <= 22
        out.append({"loc": r["loc"], "store": r["name"], "last_sale": r["last_sale"].strftime("%d.%m %H:%M"),
                    "silence_min": silence, "expected": exp, "lagging": bad})
    out.sort(key=lambda x: -x["expected"] if x["lagging"] else x["silence_min"] * -0.001)
    return {"copy_lag_min": copy_lag, "at": now.strftime("%d.%m %H:%M"), "stores": out}


def diffs(db: Session, day=None) -> dict:
    day = day or datetime.now(TZ).date()
    rows = db.execute(text("""
        SELECT s.name AS store, o.mc, a.name AS art, o.mistral, o.own, o.flag
        FROM ownstock_snap o JOIN stores s ON s.id = o.store_id
        LEFT JOIN articles a ON a.sku = CAST(o.mc AS text)
        WHERE o.day = :d AND o.own IS NOT NULL AND o.mistral IS NOT NULL
          AND ABS(o.mistral - o.own) >= :m"""), {"d": day, "m": DIFF_MIN}).all()
    items = [{"store": r.store, "mc": r.mc, "name": r.art or str(r.mc), "mistral": float(r.mistral),
              "target": float(r.own), "diff": float(r.mistral) - float(r.own), "flag": r.flag or ""}
             for r in rows if r.mc not in AMB]
    items.sort(key=lambda x: -abs(x["diff"]))
    return {"day": str(day), "items": items}


def text_report(db: Session) -> str:
    L = []
    try:
        lg = lag(db)
        bad = [s for s in lg["stores"] if s["lagging"]]
        if bad:
            L.append("🛰 Главната изостава (заявката се смята със стари данни):")
            for s in bad:
                L.append(f"   {s['store']}: няма продажби от {s['last_sale']} ({s['silence_min']} мин, "
                         f"обикновено ~{int(s['expected'])} за това време)")
    except Exception as e:
        db.rollback()
        L.append(f"🛰 проверката за изоставане не мина: {type(e).__name__}")
    try:
        d = diffs(db)["items"]
        big = [x for x in d if abs(x["diff"]) >= 20][:12]
        if big:
            L.append("🔎 Мистрал ≠ ревизия + движения (20+ бр.):")
            for x in big:
                L.append(f"   {x['store']}: {x['name'][:40]} - Мистрал {x['mistral']:g}, по ревизия {x['target']:g}")
    except Exception:
        db.rollback()
    return "\n".join(L)


def alert_if_lagging(db: Session) -> str | None:
    """Вика се от refresher-а: Telegram до Димо веднъж на ден за магазин, който изостава."""
    lg = lag(db)
    today = datetime.now(TZ).date()
    new = [s for s in lg["stores"] if s["lagging"] and _told.get(s["loc"]) != today]
    if not new:
        return None
    from . import telegram
    msg = "🛰 Главната база не получава продажби от:\n" + "\n".join(
        f"• {s['store']}: последна {s['last_sale']}, тишина {s['silence_min']} мин (очаквани ~{int(s['expected'])})"
        for s in new) + "\nЗаявката за тях се смята със стари данни - провери връзката/касата на обекта."
    if telegram.send_to(db, "dimo", msg):
        for s in new:
            _told[s["loc"]] = today
    return msg
