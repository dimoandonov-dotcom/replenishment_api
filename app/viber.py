"""
Viber бот: всяка сутрин (12:30) изпраща справката с оборотите и спадовете.

Настройка: в Railway променлива VIBER_TOKEN (от partners.viber.com -> бот акаунт).
При старт уебкукът се регистрира сам. Човек се абонира за бота и пише
„потребител ПИН“ (същите като за пулта) - така ботът знае, че е той.
Кой получава справката: VIBER_REPORT_USERS (по подразбиране „asen“).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

SOFIA = timezone(timedelta(hours=3))
API = "https://chatapi.viber.com/pa/"
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://minmaxai.up.railway.app")
BOT_NAME = os.getenv("VIBER_BOT_NAME", "MinMaxAI 300")


def token() -> str:
    return os.getenv("VIBER_TOKEN", "").strip()


def report_users() -> set[str]:
    return {x.strip().lower() for x in os.getenv("VIBER_REPORT_USERS", "asen").split(",") if x.strip()}


def _ensure(db: Session):
    db.execute(text("""CREATE TABLE IF NOT EXISTS viber_users (viber_id TEXT PRIMARY KEY, name TEXT,
        username TEXT, linked_at TIMESTAMPTZ, reports BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())"""))
    db.execute(text("""CREATE TABLE IF NOT EXISTS viber_log (id SERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT now(),
        viber_id TEXT, kind TEXT, ok BOOLEAN, info TEXT)"""))
    db.commit()


def _call(method: str, payload: dict) -> dict:
    req = urllib.request.Request(API + method, data=json.dumps(payload).encode(),
                                 headers={"X-Viber-Auth-Token": token(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode() or "{}")


def set_webhook() -> dict:
    if not token():
        return {"ok": False, "reason": "няма VIBER_TOKEN"}
    return _call("set_webhook", {"url": PUBLIC_URL + "/viber/webhook",
                                 "event_types": ["subscribed", "unsubscribed", "conversation_started", "message"],
                                 "send_name": True})


def send(viber_id: str, msg: str) -> dict:
    out = {}
    for i in range(0, len(msg), 6500):          # лимит ~7000 знака на съобщение
        out = _call("send_message", {"receiver": viber_id, "sender": {"name": BOT_NAME},
                                     "type": "text", "text": msg[i:i + 6500]})
    return out


def verify(body: bytes, sig: str) -> bool:
    if not token() or not sig:
        return False
    return hmac.compare_digest(hmac.new(token().encode(), body, hashlib.sha256).hexdigest(), sig)


HELP = ("Здравей! Аз съм ботът на MinMaxAI.\n"
        "За да получаваш сутрешната справка с оборотите, напиши потребителя и ПИН-а си за пулта, напр.:\n"
        "asen 1234\n\nКоманди: „отчет“ – справката сега, „стоп“ – спри сутрешната, „старт“ – пусни я пак.")


def handle(db: Session, ev: dict) -> dict | None:
    """Събитие от Viber. Връща отговор за conversation_started (welcome)."""
    _ensure(db)
    e = ev.get("event")
    if e == "conversation_started":
        return {"sender": {"name": BOT_NAME}, "type": "text", "text": HELP}
    if e == "subscribed":
        u = ev.get("user", {})
        db.execute(text("""INSERT INTO viber_users(viber_id, name) VALUES (:i, :n)
                           ON CONFLICT (viber_id) DO UPDATE SET name = EXCLUDED.name"""), {"i": u.get("id"), "n": u.get("name")})
        db.commit()
        send(u.get("id"), HELP)
        return None
    if e == "unsubscribed":
        db.execute(text("UPDATE viber_users SET reports = FALSE WHERE viber_id = :i"), {"i": ev.get("user_id")})
        db.commit()
        return None
    if e != "message":
        return None
    u = ev.get("sender", {}); vid = u.get("id")
    txt = ((ev.get("message") or {}).get("text") or "").strip()
    db.execute(text("""INSERT INTO viber_users(viber_id, name) VALUES (:i, :n)
                       ON CONFLICT (viber_id) DO UPDATE SET name = EXCLUDED.name"""), {"i": vid, "n": u.get("name")})
    db.commit()
    row = db.execute(text("SELECT username FROM viber_users WHERE viber_id = :i"), {"i": vid}).first()
    linked = row and row[0]
    low = txt.lower()
    if not linked:
        parts = txt.split()
        if len(parts) == 2:
            from . import main as _main, models as m
            user, pin = parts[0].lower(), parts[1]
            ok = False
            if _main.APP_USER and user == _main.APP_USER.lower():
                ok = hmac.compare_digest(pin, _main.APP_PASSWORD)
            else:
                au = db.get(m.AppUser, user)
                ok = bool(au and au.is_active and _main.check_password(pin, au.password_hash))
            if ok:
                db.execute(text("UPDATE viber_users SET username = :u, linked_at = now(), reports = TRUE WHERE viber_id = :i"),
                           {"u": user, "i": vid})
                db.commit()
                extra = ("Всяка сутрин в 12:30 ще получаваш справката с оборотите." if user in report_users()
                         else "Свързан си. (Сутрешната справка в момента се праща само на избраните хора.)")
                send(vid, f"✅ Свързан си като {user}. {extra}\nНапиши „отчет“ за справката сега.")
                return None
        send(vid, "Не те познавам още. " + HELP)
        return None
    if low in ("стоп", "stop"):
        db.execute(text("UPDATE viber_users SET reports = FALSE WHERE viber_id = :i"), {"i": vid}); db.commit()
        send(vid, "Спрях сутрешната справка. Напиши „старт“, за да я пусна пак.")
    elif low in ("старт", "start"):
        db.execute(text("UPDATE viber_users SET reports = TRUE WHERE viber_id = :i"), {"i": vid}); db.commit()
        send(vid, "Пуснах сутрешната справка (12:30).")
    elif low in ("отчет", "справка", "оборот", "обороти"):
        send(vid, summary(db))
    else:
        send(vid, "Команди: „отчет“, „стоп“, „старт“.")
    return None


# ------------------------------------------------------------------ справката
def _e(x) -> str:
    return f"{x:,.0f}".replace(",", " ") if x is not None else "—"


def _p(x) -> str:
    return "—" if x is None else f"{x:+.1f}%".replace(".", ",")


def summary(db: Session, day=None) -> str:
    from . import turnover
    d = turnover.report(db, day, 1)
    if not d.get("ready"):
        return "Няма данни за оборотите още."
    t = d["totals"]
    L = [f"📊 ОБОРОТИ {d['period']} ({d['weekday']})",
         f"Общо: {_e(t['rev'])} € ({_p(t['pct'])} спрямо обичайното {_e(t['base'])} €)"]
    if t.get("receipts") and t.get("receipts_base"):
        L.append(f"Бонове: {_e(t['receipts'])} (обикн. {_e(t['receipts_base'])}) · ср. сметка {t['basket']:.2f} €".replace(".", ","))
    L.append(f"Магазини в спад: {t['stores_down']} от {t['stores']}")
    down = [s for s in d["stores"] if s.get("flag")]
    if down:
        L += ["", "🔻 СПАД (над 8%):"]
        for s in down[:12]:
            why = "; ".join(c["t"] for c in s["causes"][:2])
            L.append(f"• {s['store']}: {_p(s['pct'])} ({_e(s['diff'])} €)" + (f" — {why}" if why else ""))
    if d.get("causes"):
        L += ["", "🎯 ПРИЧИНИ (загуба €):"]
        for c in d["causes"][:6]:
            L.append(f"• {c['name']}: {_e(c['eur'])} € ({c['share']:.0f}%)")
    dr = d.get("drops") or []
    if dr:
        L += ["", "📦 НАЙ-ГОЛЕМИ СПАДОВЕ ПО ГРУПИ:"]
        for r in dr[:8]:
            why = "; ".join(c["t"] for c in (r.get("causes") or [])[:2])
            L.append(f"• {r['store']} / {r['grp']}: −{_e(r['lost'])} € ({_p(r['pct'])})" + (f" — {why}" if why else ""))
    L += ["", "💶 ОБОРОТ ПО МАГАЗИНИ:"]
    for i, s in enumerate(sorted(d["stores"], key=lambda s: -s["rev"]), 1):
        L.append(f"{i}. {s['store']}: {_e(s['rev'])} € ({_p(s['pct'])})")
    L += ["", f"Подробно (с вход): {PUBLIC_URL}/#turn"]
    return "\n".join(L)


def daily(db: Session, force: bool = False) -> dict:
    """Изпраща справката на абонираните и избраните хора (веднъж на ден, освен с force)."""
    _ensure(db)
    if not token():
        return {"sent": 0, "reason": "няма VIBER_TOKEN"}
    if not force and db.execute(text("""SELECT 1 FROM viber_log WHERE kind = 'daily' AND ok
            AND (at AT TIME ZONE 'Europe/Sofia')::date = (now() AT TIME ZONE 'Europe/Sofia')::date LIMIT 1""")).first():
        return {"sent": 0, "reason": "днес вече е пратена"}
    msg = summary(db)
    sent, errs = 0, []
    for vid, user in db.execute(text("""SELECT viber_id, username FROM viber_users
                                        WHERE reports AND username IS NOT NULL""")).all():
        if user.lower() not in report_users():
            continue
        try:
            r = send(vid, msg); ok = r.get("status") == 0
            sent += ok
            db.execute(text("INSERT INTO viber_log(viber_id, kind, ok, info) VALUES (:i,'daily',:ok,:info)"),
                       {"i": vid, "ok": ok, "info": json.dumps(r)[:300]})
        except Exception as e:
            errs.append(f"{user}: {e}")
            db.execute(text("INSERT INTO viber_log(viber_id, kind, ok, info) VALUES (:i,'daily',FALSE,:info)"),
                       {"i": vid, "info": str(e)[:300]})
    db.commit()
    return {"sent": sent, "errors": errs}


def status(db: Session) -> dict:
    _ensure(db)
    rows = db.execute(text("SELECT name, username, reports, linked_at FROM viber_users ORDER BY created_at")).all()
    log = db.execute(text("SELECT at, kind, ok, info FROM viber_log ORDER BY id DESC LIMIT 10")).all()
    return {"configured": bool(token()), "report_users": sorted(report_users()),
            "subscribers": [{"name": n, "user": u, "reports": r, "linked": l.astimezone(SOFIA).strftime("%d.%m %H:%M") if l else None}
                            for n, u, r, l in rows],
            "log": [{"at": a.astimezone(SOFIA).strftime("%d.%m %H:%M"), "kind": k, "ok": o, "info": i} for a, k, o, i in log]}
