"""
„От склада“: артикули, които магазините зареждат от нашия склад (Т Логистик), а не от НДК.

Складът пуска съобщение във Вайбър. Ани (или който е) го поставя в раздел „Склад“:
- код без ❌  -> „от склада“: не влиза в заявката към НДК;
- код с ❌ (или в част „изчерпани … поръчвайте от НДК“) -> връща се към НДК.
Всяка промяна се пази (кой, кога, кое съобщение) и отива при Димо в Telegram.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import models as m

SOFIA = ZoneInfo("Europe/Sofia")
SCHEMA = """
CREATE TABLE IF NOT EXISTS sklad_msgs (id BIGSERIAL PRIMARY KEY, body TEXT, by_user TEXT, at TIMESTAMPTZ DEFAULT now(),
  n_sklad INT DEFAULT 0, n_ndk INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS sklad_source (sku TEXT PRIMARY KEY, source TEXT NOT NULL, since TIMESTAMPTZ DEFAULT now(),
  by_user TEXT, msg_id BIGINT);
CREATE TABLE IF NOT EXISTS sklad_log (id BIGSERIAL PRIMARY KEY, sku TEXT, name TEXT, old_source TEXT, new_source TEXT,
  by_user TEXT, msg_id BIGINT, at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS sklad_seen (username TEXT PRIMARY KEY, last_msg_id BIGINT DEFAULT 0)
"""
_ok = False
CODE = re.compile(r"(?<![\d.,])(\d{3,6})(?![\d.,])\s*[–—\-:]")


def ensure(db: Session):
    global _ok
    if not _ok:
        for s in SCHEMA.strip().split(";"):
            if s.strip():
                db.execute(text(s))
        db.commit()
        _ok = True


def sklad_skus(db: Session) -> set[str]:
    """Кодовете, които в момента се зареждат от склада (не се поръчват от НДК)."""
    ensure(db)
    return {r[0] for r in db.execute(text("SELECT sku FROM sklad_source WHERE source = 'sklad'")).all()}


def parse(db: Session, body: str) -> dict:
    """Разчита съобщението. Не записва нищо."""
    ensure(db)
    arts = {a.sku: a for a in db.execute(select(m.Article)).scalars().all()}
    mode = "sklad"
    rows, unknown, seen = [], [], set()
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        codes = CODE.findall(line)
        if not codes:
            # заглавен ред: сменя режима за следващите редове
            if "ндк" in low and ("поръчв" in low or "изчерп" in low or "❌" in line):
                mode = "ndk"
            elif "склад" in low:
                mode = "sklad"
            continue
        src = "ndk" if ("❌" in line or "изчерп" in low) else mode
        if "от склада" in low and "❌" not in line:
            src = "sklad"
        for c in codes:
            if c in seen:
                continue
            seen.add(c)
            a = arts.get(c)
            if a is None:
                unknown.append({"sku": c, "line": line[:80]})
                continue
            rows.append({"sku": c, "name": a.name, "source": src})
    cur = {r[0]: r[1] for r in db.execute(text("SELECT sku, source FROM sklad_source")).all()}
    for r in rows:
        r["now"] = cur.get(r["sku"], "ndk")
        r["change"] = r["now"] != r["source"]
    return {"rows": rows, "unknown": unknown}


def apply(db: Session, body: str, user: str | None) -> dict:
    ensure(db)
    p = parse(db, body)
    who = user or "система"
    msg_id = db.execute(text("INSERT INTO sklad_msgs(body, by_user, n_sklad, n_ndk) VALUES (:b,:u,:s,:n) RETURNING id"),
                        {"b": body, "u": who, "s": sum(r["source"] == "sklad" for r in p["rows"]),
                         "n": sum(r["source"] == "ndk" for r in p["rows"])}).scalar()
    changed = []
    for r in p["rows"]:
        if not r["change"]:
            continue
        if r["source"] == "sklad":
            db.execute(text("""INSERT INTO sklad_source(sku, source, since, by_user, msg_id) VALUES (:k,'sklad',now(),:u,:m)
                               ON CONFLICT (sku) DO UPDATE SET source='sklad', since=now(), by_user=:u, msg_id=:m"""),
                       {"k": r["sku"], "u": who, "m": msg_id})
        else:
            db.execute(text("DELETE FROM sklad_source WHERE sku = :k"), {"k": r["sku"]})
        db.execute(text("""INSERT INTO sklad_log(sku, name, old_source, new_source, by_user, msg_id)
                           VALUES (:k,:n,:o,:s,:u,:m)"""),
                   {"k": r["sku"], "n": r["name"], "o": r["now"], "s": r["source"], "u": who, "m": msg_id})
        changed.append(r)
    db.execute(text("""INSERT INTO sklad_seen(username, last_msg_id) VALUES (:u,:m)
                       ON CONFLICT (username) DO UPDATE SET last_msg_id = :m"""), {"u": who.lower(), "m": msg_id})
    db.commit()
    told = False
    if changed:
        try:
            from . import telegram
            name = {"ani": "Ани"}.get(who.lower(), who)
            lines = [("🏬 склад" if r["source"] == "sklad" else "🚚 НДК") + f": {r['name']} ({r['sku']})" for r in changed]
            told = bool(telegram.send_to(db, "dimo", f"📦 {name} обнови „От склада“:\n" + "\n".join(lines[:40])))
        except Exception:
            db.rollback()
    return {"msg_id": msg_id, "changed": changed, "unchanged": len(p["rows"]) - len(changed),
            "unknown": p["unknown"], "told_dimo": told}


def set_one(db: Session, sku: str, source: str, user: str | None) -> dict:
    """Ръчно превключване на един артикул (бутон в списъка)."""
    return apply(db, f"{sku} – {'❌' if source == 'ndk' else ''}", user)


def last_delivery(skus: list[str]) -> dict:
    """Последна доставка от склада (Т Логистик) и от НДК по код, за последните 30 дни."""
    if not skus:
        return {}
    try:
        from . import mistral
        codes = ",".join(str(int(s)) for s in skus if s.isdigit())
        with mistral.connect("varna") as conn:
            cur = conn.cursor()
            cur.execute(f"""SELECT q.MATERIALCODE c, o.PARTNERNAMEID p, MAX(q.OPERATIONDATE) d
            FROM MATERIALQTYLOG q WITH (NOLOCK) JOIN OPERATIONS o WITH (NOLOCK) ON o.NUM = q.OPERAIONNUM AND o.LOCATIONID = q.LOCATIONID
            WHERE q.OPERATIONTYPE = 2 AND q.MATERIALCODE IN ({codes}) AND o.PARTNERNAMEID IN (186, 33856)
              AND q.OPERATIONDATE > DATEADD(day, -30, GETDATE())
            GROUP BY q.MATERIALCODE, o.PARTNERNAMEID""")
            rows = cur.fetchall()
    except Exception:
        return {}
    out: dict = {}
    for r in rows:
        k = str(r["c"])
        out.setdefault(k, {})["sklad" if int(r["p"]) == 33856 else "ndk"] = r["d"].strftime("%d.%m %H:%M") if r["d"] else None
    return out


def state(db: Session, user: str | None) -> dict:
    ensure(db)
    rows = db.execute(text("""SELECT s.sku, a.name, s.since, s.by_user FROM sklad_source s
                              LEFT JOIN articles a ON a.sku = s.sku WHERE s.source = 'sklad' ORDER BY a.name""")).all()
    deliv = last_delivery([r[0] for r in rows])
    items = []
    for r in rows:
        d = deliv.get(r[0], {})
        items.append({"sku": r[0], "name": r[1], "since": r[2].astimezone(SOFIA).strftime("%d.%m %H:%M"),
                      "by": r[3], "last_sklad": d.get("sklad"), "last_ndk": d.get("ndk")})
    msgs = db.execute(text("SELECT id, by_user, at, n_sklad, n_ndk, body FROM sklad_msgs ORDER BY id DESC LIMIT 10")).all()
    log = db.execute(text("""SELECT at, sku, name, old_source, new_source, by_user FROM sklad_log
                             ORDER BY id DESC LIMIT 60""")).all()
    return {"items": items,
            "msgs": [{"id": x[0], "by": x[1], "at": x[2].astimezone(SOFIA).strftime("%d.%m %H:%M"), "n_sklad": x[3],
                      "n_ndk": x[4], "body": x[5]} for x in msgs],
            "log": [{"at": x[0].astimezone(SOFIA).strftime("%d.%m %H:%M"), "sku": x[1], "name": x[2], "old": x[3],
                     "new": x[4], "by": x[5]} for x in log],
            "new": unseen(db, user)}


def unseen(db: Session, user: str | None) -> int:
    """Колко промени има след последното отваряне на раздела от този потребител."""
    ensure(db)
    u = (user or "система").lower()
    last = db.execute(text("SELECT last_msg_id FROM sklad_seen WHERE username = :u"), {"u": u}).scalar() or 0
    return int(db.execute(text("SELECT count(*) FROM sklad_log WHERE msg_id > :l"), {"l": last}).scalar() or 0)


def mark_seen(db: Session, user: str | None) -> None:
    ensure(db)
    u = (user or "система").lower()
    mx = db.execute(text("SELECT coalesce(max(id), 0) FROM sklad_msgs")).scalar() or 0
    db.execute(text("""INSERT INTO sklad_seen(username, last_msg_id) VALUES (:u,:m)
                       ON CONFLICT (username) DO UPDATE SET last_msg_id = :m"""), {"u": u, "m": mx})
    db.commit()


# ---------------------------------------------------------------------------
# Помощник: отговаря на въпроси за раздела (без външен AI)
# ---------------------------------------------------------------------------
HELP = [
    (("как", "постав", "добав", "съобщ", "вайбър", "viber", "зареди"),
     "Копирай цялото съобщение на склада от Вайбър, постави го в голямото поле и натисни „Прегледай“. "
     "Ще видиш кои артикули минават към склада и кои се връщат към НДК. Ако всичко е наред, натисни „Запиши“."),
    (("❌", "изчерп", "свърш", "няма в склад", "върн", "ндк"),
     "Код с ❌ (или в част „изчерпани … поръчвайте от НДК“) се връща към НДК и от утре пак влиза в заявката. "
     "Ако артикул свърши в склада, просто постави новото съобщение или натисни „→ НДК“ до него в списъка."),
    (("заявк", "влиза", "поръч", "защо няма", "липсва"),
     "Артикулите в списъка „От склада“ НЕ влизат в заявката към НДК. Всичко друго се поръчва нормално. "
     "Промяната важи от следващото пускане на заявката (09:30)."),
    (("грешк", "сбърк", "отмен", "погреш", "върни"),
     "Няма страшно: до всеки артикул има бутон „→ НДК“, който го връща. Всяка промяна се пази с име и час, а Димо получава известие."),
    (("непознат", "не е намер", "непознати", "кода"),
     "„Непознат код“ значи, че артикулът не се поръчва от НДК изобщо, затова се пропуска. Не е нужно да правиш нищо."),
    (("доставк", "т логистик", "пристиг"),
     "В списъка се вижда последната доставка от склада (Т Логистик) и от НДК. Ако артикул е „от склада“, "
     "а склад не е доставял дълго, свържи се със склада."),
]


def help_answer(db: Session, q: str) -> str:
    q0 = (q or "").strip()
    low = q0.lower()
    codes = re.findall(r"\d{3,6}", q0)
    if codes:
        ensure(db)
        out = []
        for c in codes[:10]:
            a = db.execute(select(m.Article).where(m.Article.sku == c)).scalar_one_or_none()
            src = db.execute(text("SELECT source, since, by_user FROM sklad_source WHERE sku = :k"), {"k": c}).first()
            if a is None:
                out.append(f"{c}: не се поръчва от НДК (няма го в системата).")
            elif src and src[0] == "sklad":
                out.append(f"{c} {a.name}: ОТ СКЛАДА от {src[1].astimezone(SOFIA).strftime('%d.%m %H:%M')} ({src[2]}) — не влиза в заявката към НДК.")
            elif getattr(a, "no_order", False):
                out.append(f"{c} {a.name}: отбелязан „Не се поръчва“.")
            else:
                out.append(f"{c} {a.name}: поръчва се от НДК нормално.")
        return "\n".join(out)
    best, score = None, 0
    for keys, ans in HELP:
        s = sum(k in low for k in keys)
        if s > score:
            best, score = ans, s
    return best or ("Мога да помогна с: как се поставя съобщение, какво значи ❌, защо артикул го няма в заявката, "
                    "как се връща към НДК. Може и да напишеш код на артикул (напр. 12817) — ще ти кажа откъде се зарежда.")
