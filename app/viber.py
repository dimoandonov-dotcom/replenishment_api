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


def _n(x) -> str:
    if x is None:
        return "0"
    return (f"{x:,.0f}" if abs(x - round(x)) < 0.05 else f"{x:,.1f}").replace(",", " ").replace(".", ",")


def _short(name: str, n: int = 38) -> str:
    name = (name or "").split(" / ")[0].strip()
    return name if len(name) <= n else name[:n - 1] + "…"


def _store(s: str) -> str:
    return s.replace("м-н ", "").replace("Супермаркет ", "")


ACTION = [
    ("stock", "🔴 СВЪРШИЛА НАЛИЧНОСТ", "провери заявките/доставката",
     lambda a: f"{_short(a['name'])} — продадени {_n(a['qty'])} (обикн. {_n(a['qty_base'])}), остават {_n(a['stock'])} бр."),
    ("neg", "🟠 МИНУСОВА НАЛИЧНОСТ", "заведи доставките, иначе заявките са грешни",
     lambda a: f"{_short(a['name'])} — наличност {_n(a['stock'])} бр. по Мистрал"),
    ("shelf", "🟣 НЕ СЕ ПРОДАВА, А ИМА НАЛИЧНОСТ", "провери рафта, цената, срока",
     lambda a: f"{_short(a['name'])} — 0 продадени (обикн. {_n(a['qty_base'])}/ден), по Мистрал {_n(a['stock'])} бр."),
    ("price", "🔵 ПО-ВИСОКА ЦЕНА", "провери поскъпването",
     lambda a: f"{_short(a['name'])} — {a['cause'].replace('по-висока цена: ', '')}"),
    ("other", "⚪ ПРОДАВАТ СЕ ПО-МАЛКО, А ВСИЧКО Е НАРЕД", "имат наличност, цената и акцията са същите, клиентите не са по-малко — питай управителя дали ги има на рафта и защо не вървят",
     lambda a: f"{_short(a['name'])} — продадени {_n(a['qty'])} (обикн. {_n(a['qty_base'])}), наличност {_n(a['stock'])}"),
]


def summary(db: Session, day=None) -> str:
    from collections import defaultdict
    from . import turnover
    d = turnover.report(db, day, 1)
    if not d.get("ready"):
        return "Няма данни за оборотите още."
    t = d["totals"]
    L = [f"📊 ОБОРОТИ {d['period']} ({d['weekday']})",
         f"Общо: {_e(t['rev'])} € ({_p(t['pct'])} спрямо обичайното {_e(t['base'])} €)"]
    if t.get("receipts") and t.get("receipts_base"):
        bk = f"{t['basket']:.2f}".replace(".", ",") if t.get("basket") else "—"
        L.append(f"Бонове: {_e(t['receipts'])} (обикн. {_e(t['receipts_base'])}) · ср. сметка {bk} €")
    L.append(f"Магазини в спад: {t['stores_down']} от {t['stores']}")

    down = [s for s in d["stores"] if s.get("flag")]
    if down:
        L += ["", "🏪 МАГАЗИНИ В СПАД (над 8%):"]
        for s in down[:12]:
            L.append(f"• {_store(s['store'])}: {_p(s['pct'])} ({_e(s['diff'])} €), {_e(s['rev'])} € вместо {_e(s['base'])} €")
            for c in s["causes"][:2]:
                L.append(f"   ↳ {c['t']}")
            bad = [g for g in s["groups"] if g.get("flag")][:3]
            if bad:
                L.append("   ↳ падат: " + ", ".join(f"{g['grp']} {_e(g['diff'])} €" for g in bad))

    # артикулите по причина: магазин -> артикули
    arts = defaultdict(lambda: defaultdict(list))
    for s in d["stores"]:
        # в магазин с по-малко клиенти/по-кратко работно време спадът вече е обяснен
        explained = any(c["k"] in ("traffic", "hours", "closed") for c in s["causes"])
        for g in s["groups"]:
            for a in g.get("articles") or []:
                if a["k"] == "other" and explained:
                    continue
                arts[a["k"]][s["store"]].append(a)
    eur = {c["k"]: c["eur"] for c in d.get("causes") or []}
    L += ["", "✅ ЗА ДЕЙСТВИЕ"]
    any_ = False
    for k, title, todo, fmt in ACTION:
        by = arts.get(k)
        if not by:
            continue
        any_ = True
        stores = sorted(by.items(), key=lambda x: -sum(a["lost"] for a in x[1]))
        tot_a = sum(len(v) for v in by.values())
        lost_k = sum(a['lost'] for v in by.values() for a in v) if k == "other" else eur.get(k, sum(a['lost'] for v in by.values() for a in v))
        L += ["", f"{title} (−{_e(lost_k)} €, {tot_a} арт.)", f"👉 {todo}"]
        for st, lst in stores[:6]:
            lst = sorted(lst, key=lambda a: -a["lost"])
            L.append(f"• {_store(st)} (−{_e(sum(a['lost'] for a in lst))} €):")
            for a in lst[:4]:
                L.append(f"   – {fmt(a)}")
            if len(lst) > 4:
                L.append(f"   – и още {len(lst) - 4} арт.")
        if len(stores) > 6:
            L.append(f"• … и още {len(stores) - 6} магазина")
    if not any_:
        L.append("Няма артикули за действие 🎉")

    # информативно
    info = []
    chain = [g for g in d.get("groups") or [] if g.get("pct") is not None and g["pct"] <= -10]
    if chain:
        info.append("• Пада във всички магазини (сезон/време): " + ", ".join(f"{g['grp']} {_p(g['pct'])}" for g in chain[:5]))
    pr = arts.get("promo")
    if pr:
        allp = sorted((a for v in pr.values() for a in v), key=lambda a: -a["lost"])
        names = []
        for a in allp:
            nm = _short(a["name"], 30)
            if nm not in names:
                names.append(nm)
        info.append(f"• Свършили акции (−{_e(eur.get('promo', 0))} €): " + ", ".join(names[:5]))
    if eur.get("noise"):
        info.append(f"• Малки артикули, нормално колебание (−{_e(eur['noise'])} €) — не се действа")
    if info:
        L += ["", "ℹ️ ИНФОРМАТИВНО (не изисква действие)"] + info

    L += ["", "💶 МАГАЗИН ПО МАГАЗИН (от най-големия ръст към най-големия спад)",
          "оборот вчера (обичайно) · бонове · ср. сметка · кои групи растат ↑ и падат ↓"]
    for s in sorted(d["stores"], key=lambda s: -(s["pct"] if s["pct"] is not None else 0)):
        pc = s["pct"] or 0
        ic = "🟢" if pc >= 3 else ("🔴" if pc <= -8 else ("🟠" if pc <= -3 else "⚪"))
        L += ["", f"{ic} {_store(s['store'])}: {_e(s['rev'])} € (обикн. {_e(s['base'])}) {_p(s['pct'])} / {'+' if s['diff'] >= 0 else ''}{_e(s['diff'])} €"]
        bits = []
        if s.get("receipts") is not None:
            bits.append(f"бонове {_e(s['receipts'])}" + (f" ({_e(s['receipts_base'])})" if s.get("receipts_base") else ""))
        if s.get("basket"):
            bits.append(f"ср. сметка {s['basket']:.2f}".replace(".", ",") + (f" ({s['basket_base']:.2f})".replace(".", ",") if s.get("basket_base") else "") + " €")
        if s.get("margin_pct") is not None:
            bits.append(f"марж {s['margin_pct']:.1f}%".replace(".", ","))
        if bits:
            L.append("   " + " · ".join(bits))
        gs = [g for g in s["groups"] if abs(g["diff"]) >= 10]
        up = sorted((g for g in gs if g["diff"] > 0), key=lambda g: -g["diff"])[:3]
        dn = sorted((g for g in gs if g["diff"] < 0), key=lambda g: g["diff"])[:3]
        if up:
            L.append("   ↑ " + ", ".join(f"{g['grp']} +{_e(g['diff'])} €" for g in up))
        if dn:
            L.append("   ↓ " + ", ".join(f"{g['grp']} {_e(g['diff'])} €" for g in dn))
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
