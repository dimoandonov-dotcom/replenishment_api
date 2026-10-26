"""
Telegram бот: всяка сутрин (12:30) изпраща справката с оборотите и спадовете.

Настройка: Railway променлива TELEGRAM_TOKEN (от @BotFather). При старт уебкукът
се регистрира сам. Човек пише на бота „потребител ПИН“ (като за пулта) - свързва се.
Кой получава сутрешната справка: REPORT_USERS (по подразбиране „asen“).
"""
from __future__ import annotations
from zoneinfo import ZoneInfo

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

SOFIA = ZoneInfo("Europe/Sofia")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://minmaxai.up.railway.app")


def token() -> str:
    return os.getenv("TELEGRAM_TOKEN", "").strip()


def secret() -> str:
    # тайна в заглавката на всяка заявка от Telegram (за да не може друг да вика уебкука)
    return hashlib.sha256(("tg:" + token()).encode()).hexdigest()[:48]


def report_users() -> set[str]:
    return {x.strip().lower() for x in os.getenv("REPORT_USERS", "asen").split(",") if x.strip()}


def _ensure(db: Session):
    db.execute(text("""CREATE TABLE IF NOT EXISTS tg_users (chat_id BIGINT PRIMARY KEY, name TEXT,
        username TEXT, linked_at TIMESTAMPTZ, reports BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())"""))
    db.execute(text("""CREATE TABLE IF NOT EXISTS tg_invites (code TEXT PRIMARY KEY, username TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), used_at TIMESTAMPTZ, used_by BIGINT)"""))
    db.execute(text("""CREATE TABLE IF NOT EXISTS tg_log (id SERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT now(),
        chat_id BIGINT, kind TEXT, ok BOOLEAN, info TEXT)"""))
    db.commit()


def _call(method: str, payload: dict) -> dict:
    req = urllib.request.Request(f"https://api.telegram.org/bot{token()}/{method}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "{}") or {"ok": False, "description": str(e)}


def set_webhook() -> dict:
    if not token():
        return {"ok": False, "reason": "няма TELEGRAM_TOKEN"}
    _call("setMyCommands", {"commands": [{"command": "otchet", "description": "Справката с оборотите сега"},
                                         {"command": "stop", "description": "Спри сутрешната справка"},
                                         {"command": "start", "description": "Пусни сутрешната справка"}]})
    return _call("setWebhook", {"url": PUBLIC_URL + "/telegram/webhook", "secret_token": secret(),
                                "allowed_updates": ["message"]})


def send(chat_id: int, msg: str) -> dict:
    out = {}
    parts, cur = [], ""
    for line in msg.split("\n"):            # лимит 4096 знака - режем по редове
        if len(cur) + len(line) + 1 > 3900:
            parts.append(cur); cur = ""
        cur += line + "\n"
    parts.append(cur)
    for p in parts:
        if p.strip():
            out = _call("sendMessage", {"chat_id": chat_id, "text": p, "disable_web_page_preview": True})
    return out


HELP = ("Здравей! Аз съм ботът на MinMaxAI (Магазини 300).\n"
        "За да получаваш справката с оборотите, напиши потребителя и ПИН-а си за пулта, напр.:\n"
        "asen 1234\n\nКоманди: „отчет“ – справката сега, „стоп“ – спри сутрешната, „старт“ – пусни я пак.")


def _late_send(db: Session, cid: int, user: str):
    """Свързал се е след 12:30 -> днешните отчети веднага, да не чака до утре."""
    now = datetime.now(SOFIA)
    if user in report_users() and (now.hour, now.minute) >= (12, 30):
        from .viber import reports
        r = {}
        for mm in reports(db):
            r = send(cid, mm)
        db.execute(text("INSERT INTO tg_log(chat_id, kind, ok, info) VALUES (:i,'daily',:ok,'при свързване')"),
                   {"i": cid, "ok": bool(r.get("ok"))})
        db.commit()


def handle(db: Session, upd: dict):
    _ensure(db)
    msg = upd.get("message") or {}
    chat = msg.get("chat") or {}
    cid = chat.get("id")
    if not cid or chat.get("type") != "private":
        return
    frm = msg.get("from") or {}
    name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or frm.get("username") or ""
    txt = (msg.get("text") or "").strip()
    db.execute(text("""INSERT INTO tg_users(chat_id, name) VALUES (:i, :n)
                       ON CONFLICT (chat_id) DO UPDATE SET name = EXCLUDED.name"""), {"i": cid, "n": name})
    db.commit()
    row = db.execute(text("SELECT username FROM tg_users WHERE chat_id = :i"), {"i": cid}).first()
    low = txt.lower().lstrip("/")
    # покана с един клик: t.me/<бот>?start=<код> -> свързва директно, без потребител и ПИН
    if txt.startswith("/start ") and len(txt.split()) == 2:
        code = txt.split()[1]
        inv = db.execute(text("""SELECT username FROM tg_invites WHERE code = :c AND used_at IS NULL
                                 AND created_at > now() - interval '30 days'"""), {"c": code}).first()
        if inv:
            user = inv[0]
            db.execute(text("UPDATE tg_users SET username = :u, linked_at = now(), reports = TRUE WHERE chat_id = :i"),
                       {"u": user, "i": cid})
            db.execute(text("UPDATE tg_invites SET used_at = now(), used_by = :i WHERE code = :c"), {"i": cid, "c": code})
            db.commit()
            send(cid, f"✅ Готово, {name or user}! Всеки ден в 12:30 ще получаваш отчетите с оборотите на магазините.\n"
                      "Команди: „отчет“ – отчетите сега, „стоп“ – спри ги, „старт“ – пусни ги пак.")
            _late_send(db, cid, user)
            return
        if not (row and row[0]):
            send(cid, "Този линк вече е използван или е изтекъл. Поискай нов от Димо.")
            return
    if not (row and row[0]):
        parts = txt.split()
        if len(parts) == 2 and not txt.startswith("/"):
            from . import main as _main, models as m
            user, pin = parts[0].lower(), parts[1]
            if _main.APP_USER and user == _main.APP_USER.lower():
                ok = hmac.compare_digest(pin, _main.APP_PASSWORD)
            else:
                au = db.get(m.AppUser, user)
                ok = bool(au and au.is_active and _main.check_password(pin, au.password_hash))
            if ok:
                db.execute(text("UPDATE tg_users SET username = :u, linked_at = now(), reports = TRUE WHERE chat_id = :i"),
                           {"u": user, "i": cid})
                db.commit()
                extra = ("Всеки ден в 12:30 ще получаваш справката с оборотите." if user in report_users()
                         else "Сутрешната справка в момента се праща само на избраните хора.")
                send(cid, f"✅ Свързан си като {user}. {extra}\nНапиши „отчет“ за справката сега.")
                _late_send(db, cid, user)
                return
            send(cid, "❌ Грешен потребител или ПИН.\n\n" + HELP)
            return
        send(cid, HELP)
        return
    if low in ("стоп", "stop"):
        db.execute(text("UPDATE tg_users SET reports = FALSE WHERE chat_id = :i"), {"i": cid}); db.commit()
        send(cid, "Спрях сутрешната справка. Напиши „старт“, за да я пусна пак.")
    elif low in ("старт", "start"):
        db.execute(text("UPDATE tg_users SET reports = TRUE WHERE chat_id = :i"), {"i": cid}); db.commit()
        send(cid, "Сутрешната справка е пусната (всеки ден в 12:30). Напиши „отчет“ за справката сега.")
    elif low in ("преглед", "заявки", "грешки"):
        from .review import text_report
        send(cid, text_report(db))
    elif low in ("отчет", "otchet", "справка", "оборот", "обороти"):
        from .viber import reports
        for r in reports(db):
            send(cid, r)
    else:
        send(cid, "Команди: „отчет“, „стоп“, „старт“.")


def daily(db: Session, force: bool = False) -> dict:
    _ensure(db)
    if not token():
        return {"sent": 0, "reason": "няма TELEGRAM_TOKEN"}
    done = set() if force else {c for (c,) in db.execute(text("""SELECT DISTINCT chat_id FROM tg_log WHERE kind = 'daily' AND ok
            AND (at AT TIME ZONE 'Europe/Sofia')::date = (now() AT TIME ZONE 'Europe/Sofia')::date""")).all()}
    from .viber import reports
    msgs = reports(db)
    sent, errs = 0, []
    for cid, user in db.execute(text("SELECT chat_id, username FROM tg_users WHERE reports AND username IS NOT NULL")).all():
        if user.lower() not in report_users() or cid in done:
            continue
        r = {}
        for mm in msgs:
            r = send(cid, mm)
        ok = bool(r.get("ok"))
        sent += ok
        if not ok:
            errs.append(f"{user}: {r.get('description')}")
        db.execute(text("INSERT INTO tg_log(chat_id, kind, ok, info) VALUES (:i,'daily',:ok,:info)"),
                   {"i": cid, "ok": ok, "info": json.dumps(r, ensure_ascii=False)[:300]})
    db.commit()
    return {"sent": sent, "errors": errs}


def status(db: Session) -> dict:
    _ensure(db)
    me = _call("getMe", {}) if token() else {}
    wh = _call("getWebhookInfo", {}) if token() else {}
    rows = db.execute(text("SELECT name, username, reports, linked_at FROM tg_users ORDER BY created_at")).all()
    log = db.execute(text("SELECT at, kind, ok, info FROM tg_log ORDER BY id DESC LIMIT 10")).all()
    return {"configured": bool(token()), "bot": (me.get("result") or {}).get("username"),
            "webhook": (wh.get("result") or {}).get("url"), "webhook_error": (wh.get("result") or {}).get("last_error_message"),
            "report_users": sorted(report_users()),
            "users": [{"name": n, "user": u, "reports": r, "linked": l.astimezone(SOFIA).strftime("%d.%m %H:%M") if l else None}
                      for n, u, r, l in rows],
            "log": [{"at": a.astimezone(SOFIA).strftime("%d.%m %H:%M"), "kind": k, "ok": o, "info": i} for a, k, o, i in log]}


def invite(db: Session, username: str) -> dict:
    """Линк-покана с един клик за потребител (важи 30 дни, веднъж)."""
    import secrets
    _ensure(db)
    code = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16]
    db.execute(text("INSERT INTO tg_invites(code, username) VALUES (:c, :u)"), {"c": code, "u": username.lower()})
    db.commit()
    me = _call("getMe", {}) if token() else {}
    bot = (me.get("result") or {}).get("username") or "minmax300_bot"
    return {"user": username.lower(), "link": f"https://t.me/{bot}?start={code}"}


def review_users() -> set[str]:
    return {x.strip().lower() for x in os.getenv("REVIEW_USERS", "dimo").split(",") if x.strip()}


def review_daily(db: Session, force: bool = False) -> dict:
    """13:00 - дневният преглед (къде греши MinMaxAI, къде е права Ани) на REVIEW_USERS."""
    _ensure(db)
    if not token():
        return {"sent": 0, "reason": "няма TELEGRAM_TOKEN"}
    done = set() if force else {c for (c,) in db.execute(text("""SELECT DISTINCT chat_id FROM tg_log WHERE kind = 'review' AND ok
            AND (at AT TIME ZONE 'Europe/Sofia')::date = (now() AT TIME ZONE 'Europe/Sofia')::date""")).all()}
    from .review import text_report
    msg = text_report(db)
    sent = 0
    for cid, user in db.execute(text("SELECT chat_id, username FROM tg_users WHERE username IS NOT NULL")).all():
        if user.lower() not in review_users() or cid in done:
            continue
        r = send(cid, msg); ok = bool(r.get("ok"))
        sent += ok
        db.execute(text("INSERT INTO tg_log(chat_id, kind, ok, info) VALUES (:i,'review',:ok,:info)"),
                   {"i": cid, "ok": ok, "info": json.dumps(r, ensure_ascii=False)[:300]})
    db.commit()
    return {"sent": sent}


def send_to(db: Session, username: str, msg: str) -> bool:
    """Съобщение до свързан потребител по име (напр. "dimo")."""
    if not token():
        return False
    _ensure(db)
    ok = False
    for cid, u in db.execute(text("SELECT chat_id, username FROM tg_users WHERE username IS NOT NULL")).all():
        if (u or "").lower() == username.lower():
            ok = send(int(cid), msg).get("ok", True) is not False or ok
    return ok
