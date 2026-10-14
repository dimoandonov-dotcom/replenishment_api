"""
Telegram бот: всяка сутрин (12:30) изпраща справката с оборотите и спадовете.

Настройка: Railway променлива TELEGRAM_TOKEN (от @BotFather). При старт уебкукът
се регистрира сам. Човек пише на бота „потребител ПИН“ (като за пулта) - свързва се.
Кой получава сутрешната справка: REPORT_USERS (по подразбиране „asen“).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

SOFIA = timezone(timedelta(hours=3))
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
    elif low in ("отчет", "otchet", "справка", "оборот", "обороти"):
        from .viber import summary
        send(cid, summary(db))
    else:
        send(cid, "Команди: „отчет“, „стоп“, „старт“.")


def daily(db: Session, force: bool = False) -> dict:
    _ensure(db)
    if not token():
        return {"sent": 0, "reason": "няма TELEGRAM_TOKEN"}
    if not force and db.execute(text("""SELECT 1 FROM tg_log WHERE kind = 'daily' AND ok
            AND (at AT TIME ZONE 'Europe/Sofia')::date = (now() AT TIME ZONE 'Europe/Sofia')::date LIMIT 1""")).first():
        return {"sent": 0, "reason": "днес вече е пратена"}
    from .viber import summary
    msg = summary(db)
    sent, errs = 0, []
    for cid, user in db.execute(text("SELECT chat_id, username FROM tg_users WHERE reports AND username IS NOT NULL")).all():
        if user.lower() not in report_users():
            continue
        r = send(cid, msg); ok = bool(r.get("ok"))
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
