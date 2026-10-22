"""
Сигнал за грешно заведена фактура от НДК (OCR замяна) - до час след завеждането.

Всеки 30 мин (08-20 ч.) refresher-ът дърпа документите от Мистрал за последните 3 дни и сравнява
всяка фактура на НДК със заявката (на Ани, или нашата за пилотните магазини). Ако поръчан артикул
липсва, а на негово място е заведен непоръчан със същото количество - праща съобщение в Telegram/Viber
на OCR_ALERT_USERS (по подразбиране nadejda,dimo) с точно какво да се сторнира и заведе.

Ако една и съща замяна се появи в 3+ магазина - отделен сигнал до OCR_PATTERN_USERS (dimo):
грешна връзка в OCR програмата, която трябва да се оправи веднъж.

Всяка замяна се праща само веднъж (таблица ocr_alerts).
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

SOFIA = timezone(timedelta(hours=3))
PATTERN_MIN = 3


def _users(env: str, default: str) -> set[str]:
    return {x.strip().lower() for x in os.getenv(env, default).split(",") if x.strip()}


def _ensure(db: Session):
    db.execute(text("""CREATE TABLE IF NOT EXISTS ocr_alerts (key TEXT PRIMARY KEY, kind TEXT, store_id INT,
                       info TEXT, sent_to TEXT, sent_at TIMESTAMPTZ DEFAULT now())"""))
    db.commit()


def _short(s: str, n: int = 40) -> str:
    s = re.sub(r"\s*/\s*\d+.*$", "", s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def find(db: Session) -> list[dict]:
    """Незапочнатите (непоправени) OCR замени от вчера и днес."""
    from . import anomalies
    today = datetime.now(SOFIA).date()
    days = {today.strftime("%d.%m"), (today - timedelta(days=1)).strftime("%d.%m")}
    out = []
    for r in anomalies.items(db, 3):
        if r.get("kind") != "ocr_swap" or r.get("fixed") or r.get("day") not in days:
            continue
        info = r.get("info") or ""
        m_doc = re.search(r"доставка № (\S+):", info)
        m_x = re.search(r"а е заведено (\d+) (.+?) — ([\d.]+) бр", info)
        ordered, booked = (r.get("name") or "").split(" → ", 1) if " → " in (r.get("name") or "") else (r.get("name"), "")
        out.append({"store_id": r["store_id"], "store": r["store"], "day": r["day"],
                    "doc": m_doc.group(1) if m_doc else "—",
                    "sku": r["sku"], "ordered": ordered, "booked_sku": m_x.group(1) if m_x else "",
                    "booked": booked, "qty": float(m_x.group(3)) if m_x else float(r.get("qty") or 0),
                    "repeat": int(r.get("repeat") or 0)})
    return out


def _msg(rows: list[dict]) -> str:
    lines = ["⚠️ Грешно заведена фактура от НДК", ""]
    by = defaultdict(list)
    for r in rows:
        by[(r["store"], r["doc"], r["day"])].append(r)
    for (store, doc, day), rr in sorted(by.items()):
        lines.append(f"🏪 {store} · фактура {doc} ({day})")
        for r in rr:
            q = f"{r['qty']:g}"
            lines.append(f"   Поръчано: {_short(r['ordered'])} ({r['sku']})")
            lines.append(f"   Заведено: {_short(r['booked'])} ({r['booked_sku']}) – {q} бр.")
            lines.append(f"   👉 Сторнирай {_short(r['booked'], 30)} {q} бр. и заведи {_short(r['ordered'], 30)} {q} бр.")
        lines.append("")
    return "\n".join(lines).strip()


def _pattern_msg(y: str, x: str, n: int, stores: list[str]) -> str:
    return ("🔧 Грешна връзка в OCR програмата\n\n"
            f"Във фактурите на НДК „{_short(y, 50)}“ се завежда като „{_short(x, 50)}“ – вече в {n} магазина"
            f" ({', '.join(stores[:6])}{'…' if len(stores) > 6 else ''}).\n\n"
            "👉 Оправете връзката на този артикул в OCR програмата, иначе грешката ще се повтаря при всяка доставка.")


def _send(db: Session, users: set[str], msg: str) -> list[str]:
    from . import telegram, viber
    sent = []
    if telegram.token():
        telegram._ensure(db)
        for cid, u in db.execute(text("SELECT chat_id, username FROM tg_users WHERE username IS NOT NULL")).all():
            if (u or "").lower() in users:
                try:
                    if telegram.send(int(cid), msg).get("ok", True) is not False:
                        sent.append(f"tg:{u}")
                except Exception:
                    pass
    if viber.token():
        viber._ensure(db)
        for vid, u in db.execute(text("SELECT viber_id, username FROM viber_users WHERE username IS NOT NULL")).all():
            if (u or "").lower() in users:
                try:
                    viber.send(vid, msg)
                    sent.append(f"viber:{u}")
                except Exception:
                    pass
    return sent


def scan(db: Session, send: bool = True) -> dict:
    _ensure(db)
    rows = find(db)
    done = {k for (k,) in db.execute(text("SELECT key FROM ocr_alerts")).all()}
    key = lambda r: f"swap:{r['store_id']}:{r['doc']}:{r['sku']}:{r['booked_sku']}"  # noqa: E731
    new = [r for r in rows if key(r) not in done]
    res = {"found": len(rows), "new": len(new), "sent_to": [], "patterns": []}
    if new:
        msg = _msg(new)
        res["message"] = msg
        if send:
            to = _send(db, _users("OCR_ALERT_USERS", "nadejda,dimo"), msg)
            res["sent_to"] = to
            if to:      # никой не е свързан -> пробваме пак следващия път
                for r in new:
                    db.execute(text("""INSERT INTO ocr_alerts(key, kind, store_id, info, sent_to) VALUES (:k,'swap',:s,:i,:t)
                                       ON CONFLICT DO NOTHING"""),
                               {"k": key(r), "s": r["store_id"], "i": f"{r['sku']}→{r['booked_sku']} {r['qty']:g}",
                                "t": ",".join(to)})
                db.commit()
    # повтаряща се замяна -> грешна връзка в OCR (веднъж на двойка)
    pairs = defaultdict(set)
    names = {}
    for r in rows:
        pairs[(r["sku"], r["booked_sku"])].add(r["store"])
        names[(r["sku"], r["booked_sku"])] = (r["ordered"], r["booked"])
    for (y, x), stores in pairs.items():
        n = max(len(stores), max((r["repeat"] for r in rows if r["sku"] == y and r["booked_sku"] == x), default=0))
        k = f"pattern:{y}:{x}"
        if n < PATTERN_MIN or k in done:
            continue
        msg = _pattern_msg(*names[(y, x)], n, sorted(stores))
        res["patterns"].append(msg)
        if send:
            to = _send(db, _users("OCR_PATTERN_USERS", "dimo"), msg)
            if to:
                db.execute(text("INSERT INTO ocr_alerts(key, kind, info, sent_to) VALUES (:k,'pattern',:i,:t) ON CONFLICT DO NOTHING"),
                           {"k": k, "i": f"{y}→{x} в {n} магазина", "t": ",".join(to)})
                db.commit()
    return res


def recent(db: Session, days: int = 7) -> list[dict]:
    _ensure(db)
    rows = db.execute(text("""SELECT key, kind, store_id, info, sent_to, sent_at FROM ocr_alerts
                              WHERE sent_at >= now() - make_interval(days => :d) ORDER BY sent_at DESC"""), {"d": days}).all()
    return [{"key": k, "kind": kd, "store_id": s, "info": i, "sent_to": t,
             "at": a.astimezone(SOFIA).strftime("%d.%m %H:%M")} for k, kd, s, i, t, a in rows]
