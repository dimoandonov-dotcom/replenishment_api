"""
Дневен преглед: нашата заявка (сутрешното пускане) срещу заявките на Ани за деня.

За всеки артикул от двете заявки се гледа дали наличността + поръчаното ще стигне
до СЛЕДВАЩАТА доставка (днешната заявка идва утре; после - по графика на магазина).
  MinMaxAI пропуска   - ние поръчваме по-малко и стоката няма да стигне      -> наша грешка
  MinMaxAI излишно    - поръчваме, а наличността и без това стига            -> наша грешка
  Ани пропуска        - тя поръчва по-малко и стоката няма да стигне
  Ани излишно         - тя докарва запас за над 10 дни (и повече от нас)
  Ани спрени          - поръчва артикули без мин/макс, „Не се поръчва“, 0-0 или без „да“ в планограмата
  без продажби        - поръчано (от когото и да е) при 0 продажби за 14 дни - за решение по асортимента
"""
from __future__ import annotations
from zoneinfo import ZoneInfo

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m, service

SOFIA = ZoneInfo("Europe/Sofia")
OVER_DAYS = 10          # над толкова дни запас след доставката = излишно

CATS = {
    "our_miss": "🔴 MinMaxAI пропуска — ще свърши преди следващата доставка",
    "our_over": "🟠 MinMaxAI поръчва излишно — наличността стига",
    "slowpack": "🐢 Бавен артикул на празен рафт — цял кашон е запас за седмици (решение: да се зарежда ли)",
    "ani_miss": "🔵 Ани пропуска — ще свърши преди следващата доставка",
    "ani_over": "🟣 Ани докарва запас за над 10 дни",
    "ani_stop": "⛔ Ани поръчва спрени / извън асортимента",
    "nosale": "⚪ Поръчано без продажби 14 дни (асортимент)",
}
OURS = ("our_miss", "our_over")


def _must_days(db: Session, store_id: int, day: date) -> int:
    """Колко дни трябва да стигне стоката: до утрешната доставка + до следващата след нея."""
    try:
        from .engine import SAME_DAY_DELIVERY
        sch = service.load_schedules(db, store_id)
        entries = [e for lst in sch.values() for e in lst]
        deliv = {e.delivery_weekday for e in entries if e.delivery_weekday}
        wd = day.isoweekday()
        today = next((e for e in entries if e.order_weekday == wd and e.delivery_weekday), None)
        if not deliv or today is None:
            return 2
        dw = today.delivery_weekday
        lead = 0 if SAME_DAY_DELIVERY else ((dw - wd) % 7 or 7)
        gap = next((k for k in range(1, 8) if ((dw - 1 + k) % 7) + 1 in deliv), 1)
        return lead + gap
    except Exception:
        return 2


def _n(x) -> str:
    return (f"{x:g}" if float(x) == int(x) else f"{x:.1f}").replace(".", ",")


def _explain(cat: str, aq: float, oq: float, q: float, sdp: float, must: int, pack: int, why: str) -> str:
    """Обяснение с думи: какво продава, колко има, колко трябва и кой е прав."""
    if sdp <= 0:
        base = f"Няма нито една продажба за последните 14 дни. Наличност {_n(q)} бр."
    else:
        base = (f"Продава {_n(sdp)} бр./ден. Наличност {_n(q)} бр. ≈ {_n(max(q, 0) / sdp)} дни. "
                f"Трябва да стигне за {must} дни (до утрешната доставка и до следващата след нея) = {_n(sdp * must)} бр.")
    d = lambda x: _n((max(q, 0) + x) / sdp) if sdp > 0 else "—"  # noqa: E731
    who = f" Ани поръчва {_n(aq)} → стига {d(aq)} дни; MinMaxAI {_n(oq)} → {d(oq)} дни."
    tail = {
        "our_miss": " С нашето количество стоката ще свърши преди следващата доставка — права е Ани, грешката е наша.",
        "our_over": " Наличността и без поръчка стига с поне ден и половина резерв — нашата поръчка е излишна.",
        "slowpack": f" Рафтът е празен, но кашонът е {pack} бр. — запас за над 2 седмици. Решение: да се зарежда ли.",
        "ani_miss": " С количеството на Ани стоката ще свърши преди следващата доставка — тук сме прави ние.",
        "ani_over": " Ани докарва запас за над 10 дни — излишна стока на склад.",
        "ani_stop": f" Артикулът е спрян за този магазин ({why}), а Ани го поръчва.",
        "nosale": " Поръчано без продажби — за решение по асортимента (договорка или махаме).",
    }.get(cat, "")
    return base + who + tail


def build(db: Session, day: date | None = None) -> dict:
    day = day or datetime.now(SOFIA).date()
    start = datetime.combine(day, datetime.min.time(), SOFIA)
    end = start + timedelta(days=1)
    arts = {a.id: a for a in db.execute(select(m.Article)).scalars().all()}
    by_sku = {a.sku: a for a in arts.values()}
    stores = {s.id: s.name for s in db.execute(select(m.Store)).scalars()}
    # Ани: сбор по магазин и артикул от всички нейни заявки за деня
    ani = defaultdict(lambda: defaultdict(float))
    ani_at = {}
    for o in db.execute(select(m.ManualOrder).where(m.ManualOrder.received_at >= start,
                                                    m.ManualOrder.received_at < end,
                                                    m.ManualOrder.store_id.isnot(None))).scalars():
        ani_at[o.store_id] = max(ani_at.get(o.store_id, o.received_at), o.received_at)
        for l in db.execute(select(m.ManualOrderLine).where(m.ManualOrderLine.order_id == o.id)).scalars():
            if float(l.store_qty or 0) > 0:
                a = by_sku.get(l.sku)
                if a:
                    ani[o.store_id][a.id] += float(l.store_qty)
    if not ani:
        return {"ready": False, "day": day.strftime("%d.%m.%Y"), "reason": "Няма заявки от Ани за деня в системата."}
    # нашата заявка: последното пускане за деня
    run = db.execute(select(m.DispatchRun).where(m.DispatchRun.started_at >= start, m.DispatchRun.started_at < end)
                     .order_by(m.DispatchRun.started_at.desc())).scalars().first()
    ours = defaultdict(dict)
    stock_at = {}
    if run:
        for po in db.execute(select(m.PurchaseOrder).where(m.PurchaseOrder.dispatch_run_id == run.id)).scalars():
            stock_at[po.store_id] = po.created_at
            for l in po.lines:
                ours[po.store_id][l.article_id] = float(l.ordered_quantity or 0)
    rows = []
    sdays = [day - timedelta(days=14 - i) for i in range(14)]          # 14-те дни преди заявката
    fmt = lambda t: t.astimezone(SOFIA).strftime("%d.%m %H:%M") if t else None  # noqa: E731
    for sid, amap in ani.items():
        must = _must_days(db, sid, day)
        series = defaultdict(lambda: [0.0] * 14)
        for aid, dd, q in db.execute(select(m.SalesHistory.article_id, m.SalesHistory.sale_date,
                                            m.SalesHistory.quantity_sold).where(
                m.SalesHistory.store_id == sid, m.SalesHistory.sale_date >= sdays[0],
                m.SalesHistory.sale_date <= sdays[-1])).all():
            series[aid][(dd - sdays[0]).days] += float(q or 0)
        st = service.stock_as_of(db, sid, stock_at.get(sid) or start + timedelta(hours=6))
        adr = service.load_avg_daily_sales(db, sid)
        sett = {s.article_id: s for s in db.execute(select(m.StoreArticleSetting)
                                                   .where(m.StoreArticleSetting.store_id == sid)).scalars()}
        plano = set(db.execute(select(m.Planogram.article_id).where(m.Planogram.store_id == sid)).scalars())
        for aid in set(amap) | set(ours.get(sid, {})):
            a = arts.get(aid)
            if a is None:
                continue
            aq, oq = amap.get(aid, 0.0), ours.get(sid, {}).get(aid, 0.0)
            if abs(aq - oq) < 0.01:
                continue
            q = float(st.get(aid, 0.0) or 0.0)
            sdp = adr.get((sid, aid), 0.0)
            need = sdp * must
            s = sett.get(aid)
            stop = (a.no_order or s is None or float(s.max_stock) == 0 or (plano and aid not in plano))
            cat = None
            if sdp <= 0:
                cat = "ani_stop" if (aq > oq and stop) else "nosale"
            elif aq > oq and stop and oq == 0:
                cat = "ani_stop"
            elif oq < aq and max(q, 0) + oq < need:
                pk = max(int(a.pack_size or 1), 1)
                cat = "slowpack" if (oq == 0 and pk > 1 and pk / sdp > 14) else "our_miss"
            elif oq > aq and max(q, 0) + aq < need:
                cat = "ani_miss"
            elif oq > aq and aq == 0 and q >= sdp * (must + 1.5):
                cat = "our_over"
            elif aq > oq and (max(q, 0) + aq) / sdp > max(OVER_DAYS, must + 3):
                cat = "ani_over"
            if not cat:
                continue
            why_stop = ("„Не се поръчва“" if a.no_order else "няма мин/макс" if s is None
                        else "спрян 0-0" if float(s.max_stock) == 0 else "без „да“ в планограмата")
            rows.append({
                "series": [round(x, 1) for x in series.get(aid, [0.0] * 14)],
                "min": float(s.min_stock) if s is not None else None,
                "max": float(s.max_stock) if s is not None else None,
                "ani_at": fmt(ani_at.get(sid)), "our_at": fmt(stock_at.get(sid)),
                "explain": _explain(cat, aq, oq, q, sdp, must, int(a.pack_size or 1), why_stop),
                "cat": cat, "store_id": sid, "store": stores.get(sid, str(sid)), "sku": a.sku,
                "name": a.supplier_name or a.name, "ani": aq, "ours": oq, "stock": round(q, 1),
                "per_day": round(sdp, 2), "must_days": must, "pack": int(a.pack_size or 1),
                "days_now": round(q / sdp, 1) if sdp > 0 else None,
                "days_ani": round((max(q, 0) + aq) / sdp, 1) if sdp > 0 else None,
                "days_ours": round((max(q, 0) + oq) / sdp, 1) if sdp > 0 else None,
                "why": why_stop if cat == "ani_stop" else "",
            })
    cnt = defaultdict(int)
    for r in rows:
        cnt[r["cat"]] += 1
    tot_ani = sum(sum(v.values()) for v in ani.values())
    tot_ours = sum(sum(ours.get(s, {}).values()) for s in ani)
    order = {k: i for i, k in enumerate(CATS)}
    rows.sort(key=lambda r: (order[r["cat"]], r["store"], -abs(r["ani"] - r["ours"])))
    return {"ready": True, "day": day.strftime("%d.%m.%Y"), "stores": len(ani), "run_id": run.id if run else None,
            "series_days": [x.strftime("%d.%m") for x in sdays],
            "ani_units": tot_ani, "our_units": tot_ours, "cats": CATS, "counts": dict(cnt),
            "our_errors": sum(cnt[k] for k in OURS), "rows": rows}


def text_report(db: Session, day: date | None = None) -> str:
    d = build(db, day)
    if not d.get("ready"):
        return f"📋 Преглед на заявките {d.get('day')}: {d.get('reason')}"
    L = [f"📋 ПРЕГЛЕД НА ЗАЯВКИТЕ {d['day']} — MinMaxAI срещу Ани",
         f"Магазини с заявка от Ани: {d['stores']} · Ани {d['ani_units']:,.0f} бр. · MinMaxAI {d['our_units']:,.0f} бр.".replace(",", " "),
         f"Наши грешки: {d['our_errors']} реда", ""]
    by = defaultdict(list)
    for r in d["rows"]:
        by[r["cat"]].append(r)
    for k, title in CATS.items():
        lst = by.get(k, [])
        if not lst:
            continue
        L.append(f"{title}: {len(lst)}")
        show = lst if k in OURS else lst[:8]
        for r in show[:25]:
            st = r["store"].replace("м-н ", "")
            if k == "ani_stop":
                L.append(f"  • {st}: {r['name'][:40]} — Ани {r['ani']:g} ({r['why']})")
            elif k == "nosale":
                L.append(f"  • {st}: {r['name'][:40]} — Ани {r['ani']:g} / ние {r['ours']:g}")
            else:
                L.append(f"  • {st}: {r['name'][:40]} — Ани {r['ani']:g} / ние {r['ours']:g} · "
                         f"нал. {r['stock']:g}, {r['per_day']:g}/ден, трябва за {r['must_days']} дни "
                         f"→ стига: Ани {r['days_ani']:g} д. / ние {r['days_ours']:g} д.")
        if len(lst) > len(show[:25]):
            L.append(f"  … и още {len(lst) - len(show[:25])}")
        L.append("")
    L.append("Подробно: https://minmaxai.up.railway.app/#cmp")
    return "\n".join(L)


def apply_ani(db: Session, day: date | None = None, dry: bool = True, cats: tuple = OURS) -> dict:
    """Където е права Ани (наши грешки) - количеството в нашата заявка става като нейното."""
    d = build(db, day)
    if not d.get("ready") or not d.get("run_id"):
        return {"applied": 0, "reason": d.get("reason") or "няма наше пускане за деня", "changes": []}
    skus = {a.sku: a for a in db.execute(select(m.Article)).scalars().all()}
    pos = {po.store_id: po for po in db.execute(select(m.PurchaseOrder).where(
        m.PurchaseOrder.dispatch_run_id == d["run_id"])).scalars()}
    changes = []
    for r in d["rows"]:
        if r["cat"] not in cats:
            continue
        a = skus.get(r["sku"]); po = pos.get(r["store_id"])
        if a is None or po is None:
            continue
        line = next((l for l in po.lines if l.article_id == a.id), None)
        old = float(line.ordered_quantity) if line is not None else 0.0
        new = float(r["ani"])
        if abs(old - new) < 0.01:
            continue
        changes.append({"store": r["store"], "sku": r["sku"], "name": r["name"], "cat": r["cat"],
                        "ours": old, "ani": new, "stock": r["stock"], "per_day": r["per_day"]})
        if dry:
            continue
        note = f"по Ани (преглед: беше {old:g})"
        if line is None and new > 0:
            s = db.get(m.StoreArticleSetting, (r["store_id"], a.id))
            db.add(m.PurchaseOrderLine(purchase_order_id=po.id, article_id=a.id, current_stock=r["stock"],
                                       min_stock=float(s.min_stock) if s else 0, max_stock=float(s.max_stock) if s else 0,
                                       effective_max=float(s.max_stock) if s else 0, suggested_quantity=new,
                                       ordered_quantity=int(round(new)), pack_size=int(a.pack_size or 1), notes=note))
        elif line is not None and new <= 0:
            db.delete(line)
        elif line is not None:
            line.ordered_quantity = int(round(new))
            line.notes = ((line.notes or "") + " · " + note).strip(" ·")
    if not dry:
        db.commit()
    return {"day": d["day"], "run_id": d["run_id"], "dry": dry, "applied": 0 if dry else len(changes),
            "changes": changes}


def ani_stopped(db: Session, days: int = 14) -> dict:
    """Какво поръчва Ани, а при нас е спряно / извън асортимента - по артикул и магазин, за последните N дни."""
    since = datetime.combine(datetime.now(SOFIA).date() - timedelta(days=days - 1), datetime.min.time(), SOFIA)
    arts = {a.sku: a for a in db.execute(select(m.Article)).scalars().all()}
    stores = {s.id: s.name for s in db.execute(select(m.Store)).scalars()}
    sett = {(s.store_id, s.article_id): s for s in db.execute(select(m.StoreArticleSetting)).scalars().all()}
    plano = set(db.execute(select(m.Planogram.store_id, m.Planogram.article_id)).all())
    queue = set(db.execute(select(m.SettingsReviewQueue.store_id, m.SettingsReviewQueue.article_id)
                           .where(m.SettingsReviewQueue.resolved.is_(False))).all())
    plano_stores = {s for s, _ in plano}
    agg = {}
    for o in db.execute(select(m.ManualOrder).where(m.ManualOrder.received_at >= since,
                                                    m.ManualOrder.store_id.isnot(None))).scalars():
        day = o.received_at.astimezone(SOFIA).strftime("%d.%m")
        for l in db.execute(select(m.ManualOrderLine).where(m.ManualOrderLine.order_id == o.id)).scalars():
            q = float(l.store_qty or 0)
            if q <= 0:
                continue
            a = arts.get(l.sku)
            sid = o.store_id
            if a is None:
                why = "няма го в НДК номенклатурата"
            elif a.no_order:
                why = "„Не се поръчва“" + (f" ({a.no_order_reason})" if a.no_order_reason else "")
            elif not a.is_active:
                why = "неактивен артикул"
            elif (sid, a.id) in queue:
                why = "чака преглед (без потвърден мин/макс)"
            elif (sid, a.id) not in sett:
                why = "няма мин/макс"
            elif float(sett[(sid, a.id)].max_stock) == 0:
                why = "спрян 0-0"
            elif sid in plano_stores and (sid, a.id) not in plano:
                why = "без „да“ в планограмата"
            else:
                continue
            k = (l.sku, sid)
            r = agg.setdefault(k, {"sku": l.sku, "name": (a.supplier_name or a.name) if a else (l.name or l.sku),
                                   "store_id": sid, "store": stores.get(sid, str(sid)), "why": why,
                                   "times": 0, "qty": 0.0, "days": []})
            r["times"] += 1; r["qty"] += q
            if day not in r["days"]:
                r["days"].append(day)
    rows = sorted(agg.values(), key=lambda r: (r["name"], r["store"]))
    by_art = {}
    for r in rows:
        b = by_art.setdefault(r["sku"], {"sku": r["sku"], "name": r["name"], "stores": 0, "times": 0, "qty": 0.0,
                                         "why": {}})
        b["stores"] += 1; b["times"] += r["times"]; b["qty"] += r["qty"]
        b["why"][r["why"]] = b["why"].get(r["why"], 0) + 1
    arts_out = sorted(by_art.values(), key=lambda b: (-b["stores"], -b["times"]))
    return {"days": days, "articles": arts_out, "rows": rows}
