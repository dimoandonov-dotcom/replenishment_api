"""
„Минуси за оправяне“ - отрицателните наличности на НДК стоката като прости задачи за Наде/магазините.

Всеки минус е в една от петте групи (по реда на важност):
  code   - сменен код: НДК доставя под новия код, касата продава по стария -> корекция между кодовете
  ocr    - грешно заведена фактура (OCR): поръчаният е заведен като друг артикул -> корекция между двата
  check  - продава се и има доставки -> провери последната фактура от НДК / ревизия
  nodeliv- продава се, а доставка под този код няма 14+ дни -> Наде търси под какъв код идва
  old    - стар минус без продажби и доставки 14+ дни -> корекция до 0

Отметката „Готово“ важи 48 часа: ако след това артикулът още е на минус, редът се връща.
"""
from __future__ import annotations

import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import models as m

SOFIA = timezone(timedelta(hours=3))
CODE_PAIRS = {"14314": "24725"}          # Корона 355 -> Корона 330 (НДК доставя само 330)
DONE_HOURS = 48
_cache: dict = {"at": 0.0, "data": None}
CACHE_SEC = 15 * 60

TASKS = {
    "code": ("Сменени кодове от НДК",
             "НДК доставя артикула под нов код, а касата продава по стария. Старият код отива на минус, новият е на плюс.",
             ["Отвори Мистрал → Корекция на наличност в магазина.",
              "Добави бройките на стария код и извади същите от новия код – точно както пише в колона „Направи“.",
              "Ако пише „преброй“: преброй стоката на рафта и направи ревизия.",
              "Спри стария код за продажба на касата – да се маркира само с новия.",
              "Натисни „Готово“."]),
    "ocr": ("Грешно заведени фактури (OCR)",
            "При въвеждане на фактурата от НДК артикулът е заведен като друг. Поръчаният е на минус, а другият има бройки, които ги няма.",
            ["Отвори Мистрал → Корекция на наличност в магазина.",
             "Добави бройките на артикула на минус и извади същите от грешно заведения – както пише в колона „Направи“.",
             "Ако пише „преброй“: преброй на рафта и направи ревизия.",
             "Натисни „Готово“."]),
    "check": ("Свършила стока – провери фактурата от НДК",
              "Артикулът се продава и се доставя, но е на минус. Или е свършил и касата продава „на минус“, или последната фактура не е заведена изцяло.",
              ["Намери последната фактура от НДК за магазина (датата е в таблицата).",
               "Провери дали артикулът е в нея и дали бройките в Мистрал са като във фактурата.",
               "Ако липсва или е по-малко – допиши/коригирай фактурата.",
               "Ако всичко е вярно – преброй рафта и направи ревизия.",
               "Натисни „Готово“."]),
    "nodeliv": ("Продава се, но не се доставя под този код",
                "Касата продава артикула всеки ден, а доставка под този код няма от над 2 седмици. Стоката идва, но се завежда под друг код.",
                ["Виж последните фактури от НДК за магазина и намери под какъв код се завежда тази стока.",
                 "Направи корекция: + на този код, − на кода, под който е заведена.",
                 "Ако кодът на касата е грешен – оправи го.",
                 "Натисни „Готово“."]),
    "old": ("Стари минуси без продажби",
            "Артикулът не се продава и не се доставя от над 2 седмици, но е останал на минус от стара грешка.",
            ["Преброй артикула на рафта и в склада.",
             "Ако няма нищо – корекция до 0 (добави бройките от колона „Минус“).",
             "Ако има стока – направи ревизия с реалното количество.",
             "Натисни „Готово“."]),
}
ORDER = ["code", "ocr", "check", "nodeliv", "old"]


def _ensure(db: Session):
    db.execute(text("""CREATE TABLE IF NOT EXISTS minus_fixes (store_id INT, article_id INT, by_user TEXT,
                       at TIMESTAMPTZ DEFAULT now(), qty NUMERIC, PRIMARY KEY (store_id, article_id))"""))
    db.commit()


def _short(s: str) -> str:
    return re.sub(r"\s+/\s*\d.*$|\s{2,}/.*$", "", s or "").strip()


def _compute(db: Session) -> dict:
    from . import anomalies
    today = datetime.now(SOFIA).date()
    stores = {s.id: s.name for s in db.execute(select(m.Store).where(m.Store.is_active.is_(True), m.ORDERING)).scalars()}
    arts = {a.id: a for a in db.execute(select(m.Article).where(m.Article.is_active.is_(True))).scalars()}
    sku2id = {a.sku: a.id for a in arts.values()}
    snap = db.execute(text("""SELECT DISTINCT ON (store_id, article_id) store_id, article_id, quantity, captured_at
                              FROM stock_snapshots WHERE captured_at >= now() - interval '3 days'
                              ORDER BY store_id, article_id, captured_at DESC""")).all()
    stock = {(s, a): float(q) for s, a, q, _ in snap}
    asof = max((t for *_, t in snap), default=None)
    sold = {(s, a): float(q or 0) for s, a, q in db.execute(
        select(m.SalesHistory.store_id, m.SalesHistory.article_id, func.sum(m.SalesHistory.quantity_sold))
        .where(m.SalesHistory.sale_date > today - timedelta(days=14))
        .group_by(m.SalesHistory.store_id, m.SalesHistory.article_id)).all()}
    lastd = {(s, a): d for s, a, d in db.execute(text("""SELECT store_id, article_id, MAX(day) FROM stock_movements
                              WHERE optype = 2 AND qty_in > 0 AND day >= :d GROUP BY store_id, article_id"""),
                                                 {"d": today - timedelta(days=45)}).all()}
    # сменени кодове: изрично + „замяна (с) NNNNN“ в името
    code = dict(CODE_PAIRS)
    for a in arts.values():
        mm = re.search(r"замяна(?:\s+с)?\s+(\d{4,6})", f"{a.name} {a.supplier_name or ''}")
        if mm and mm.group(1) in sku2id:
            code[a.sku] = mm.group(1)
    # OCR замени: в магазина + най-честата замяна за артикула (ако е в 3+ магазина)
    ocr_store, ocr_glob = {}, defaultdict(Counter)
    try:
        for r in anomalies.items(db, 30):
            if r.get("kind") != "ocr_swap":
                continue
            mx = re.search(r"а е заведено (\d+)", r.get("info") or "")
            if mx:
                ocr_store[(r["store_id"], r["sku"])] = mx.group(1)
                ocr_glob[r["sku"]][mx.group(1)] += 1
    except Exception:
        db.rollback()
    ocr_any = {y: c.most_common(1)[0][0] for y, c in ocr_glob.items() if c.most_common(1)[0][1] >= 3}
    _ensure(db)
    marks = {(s, a): (u, t) for s, a, u, t in db.execute(text(
        "SELECT store_id, article_id, by_user, at FROM minus_fixes WHERE at >= now() - make_interval(hours => :h)"),
        {"h": DONE_HOURS}).all()}
    nm = lambda aid: _short(arts[aid].supplier_name or arts[aid].name)  # noqa: E731
    rows = defaultdict(list)
    for (sid, aid), q in stock.items():
        if q >= 0 or sid not in stores or aid not in arts or "АМБАЛАЖ" in (arts[aid].name or "").upper():
            continue
        a = arts[aid]
        x = -q
        pd = round(sold.get((sid, aid), 0.0) / 14, 1)
        ld = lastd.get((sid, aid))
        age = (today - ld).days if ld else None
        partner = code.get(a.sku)
        kind = "code" if partner else None
        if not kind:
            partner = ocr_store.get((sid, a.sku)) or ocr_any.get(a.sku)
            kind = "ocr" if partner else None
        r = {"store_id": sid, "store": stores[sid], "article_id": aid, "sku": a.sku, "name": nm(aid), "qty": q,
             "per_day": pd, "last": ld.strftime("%d.%m") if ld else None}
        if kind:
            pid = sku2id.get(partner)
            have = max(0.0, stock.get((sid, pid), 0.0)) if pid else 0.0
            mv = min(x, have)
            todo = []
            if mv > 0:
                todo.append(f"+{mv:g} на {a.sku}, −{mv:g} на {partner}")
            if x - mv > 0:
                todo.append(f"останалите {x - mv:g} бр.: преброй и ревизия")
            r.update(partner=f"{nm(pid) if pid else partner} ({partner})", partner_qty=stock.get((sid, pid), 0.0) if pid else None,
                     todo="; ".join(todo))
        elif ld is None or age > 14:
            if pd <= 0.05:
                kind, r["todo"] = "old", f"корекция +{x:g} до 0 (или ревизия, ако има стока)"
            else:
                kind, r["todo"] = "nodeliv", f"продава {pd:g}/ден, без доставка от 14+ дни"
        else:
            kind, r["todo"] = "check", f"последна фактура {r['last']}, продава {pd:g}/ден"
        mk = marks.get((sid, aid))
        if mk:
            r["done_by"], r["done_at"] = mk[0], mk[1].astimezone(SOFIA).strftime("%d.%m %H:%M")
        rows[kind].append(r)
    tasks = []
    for k in ORDER:
        rr = sorted(rows.get(k, []), key=lambda r: (r["name"], r["store"]) if k in ("code", "ocr") else (r["store"], r["qty"]))
        if not rr:
            continue
        t, w, steps = TASKS[k]
        tasks.append({"id": k, "title": t, "what": w, "steps": steps, "rows": rr,
                      "n": len(rr), "qty": sum(r["qty"] for r in rr), "left": sum(1 for r in rr if "done_by" not in r)})
    allr = [r for t in tasks for r in t["rows"]]
    by = defaultdict(lambda: {"n": 0, "qty": 0.0, "done": 0})
    for r in allr:
        b = by[(r["store_id"], r["store"])]
        b["n"] += 1; b["qty"] += r["qty"]; b["done"] += 1 if "done_by" in r else 0
    return {"asof": asof.astimezone(SOFIA).strftime("%d.%m %H:%M") if asof else None,
            "n": len(allr), "qty": sum(r["qty"] for r in allr), "left": sum(1 for r in allr if "done_by" not in r),
            "tasks": tasks,
            "stores": sorted([{"store_id": s, "store": n, **v} for (s, n), v in by.items()], key=lambda x: x["qty"])}


def get(db: Session, fresh: bool = False) -> dict:
    if fresh or not _cache["data"] or time.time() - _cache["at"] > CACHE_SEC:
        _cache["data"], _cache["at"] = _compute(db), time.time()
    return _cache["data"]


def mark(db: Session, store_id: int, article_id: int, done: bool, user: str | None) -> dict:
    _ensure(db)
    if done:
        q = db.execute(text("""SELECT quantity FROM stock_snapshots WHERE store_id = :s AND article_id = :a
                               ORDER BY captured_at DESC LIMIT 1"""), {"s": store_id, "a": article_id}).scalar()
        db.execute(text("""INSERT INTO minus_fixes(store_id, article_id, by_user, at, qty) VALUES (:s,:a,:u,now(),:q)
                           ON CONFLICT (store_id, article_id) DO UPDATE SET by_user = EXCLUDED.by_user, at = now(), qty = EXCLUDED.qty"""),
                   {"s": store_id, "a": article_id, "u": user or "система", "q": q})
    else:
        db.execute(text("DELETE FROM minus_fixes WHERE store_id = :s AND article_id = :a"), {"s": store_id, "a": article_id})
    db.commit()
    # обнови кеша на място (без пълно преизчисляване)
    d = _cache.get("data")
    if d:
        now = datetime.now(SOFIA).strftime("%d.%m %H:%M")
        for t in d["tasks"]:
            for r in t["rows"]:
                if r["store_id"] == store_id and r["article_id"] == article_id:
                    if done:
                        r["done_by"], r["done_at"] = user or "система", now
                    else:
                        r.pop("done_by", None); r.pop("done_at", None)
            t["left"] = sum(1 for r in t["rows"] if "done_by" not in r)
        d["left"] = sum(t["left"] for t in d["tasks"])
        for s in d["stores"]:
            s["done"] = sum(1 for t in d["tasks"] for r in t["rows"] if r["store_id"] == s["store_id"] and "done_by" in r)
    return {"ok": True}
