"""
Бавни артикули (Димо 21.11): списък за Асен - бавните артикули по магазини, за да им сложи
по-нисък максимум (освобождава склада за бързите) или да спре поръчките с тикче.
Данни: продажби 30 дни от sales_history, последна наличност от stock_snapshots,
мин/макс от store_article_settings. Само НДК, Варненските обекти, артикули в планограмата.
"""
from __future__ import annotations

from datetime import date, timedelta
from statistics import median

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from .db import get_db

router = APIRouter()


KEEP_DAYS_SLOW = 14     # бавният магазин задържа продажби за толкова дни (поне 1 бр. и минимума си)
TARGET_DAYS_FAST = 7    # бързият магазин се допълва до толкова дни продажби (поне минимума си)


def transfers(a: dict) -> list[dict]:
    """Предложение за прехвърляне: излишъкът от бавните магазини към бързите, на които не им стига."""
    import math
    donors = []
    for s in a["stores"]:
        keep = max(s["min"], math.ceil(s["adr"] * KEEP_DAYS_SLOW), 1 if s["max"] > 0 else 0)
        extra = int(s["stock"] - keep)
        if extra > 0:
            donors.append([s, extra])
    takers = []
    for s in a.get("fast", []):
        target = max(s["min"], math.ceil(s["adr"] * TARGET_DAYS_FAST))
        need = int(math.ceil(target - max(s["stock"], 0)))
        if need > 0:
            takers.append([s, need])
    donors.sort(key=lambda d: -d[1])
    takers.sort(key=lambda t: -t[0]["adr"])          # първо най-бързите
    out = []
    for t in takers:
        for d in donors:
            if t[1] <= 0:
                break
            q = min(t[1], d[1])
            if q <= 0:
                continue
            out.append({"from_id": d[0]["store_id"], "from": d[0]["store"], "to_id": t[0]["store_id"], "to": t[0]["store"],
                        "qty": q, "to_adr": t[0]["adr"], "to_stock": t[0]["stock"]})
            d[1] -= q
            t[1] -= q
    return out


@router.get("/ui/slow")
def slow(store_id: int = 0, max_adr: float = 0.3, days: int = 30, db: Session = Depends(get_db)):
    days = max(7, min(days, 90))
    since = date.today() - timedelta(days=days)
    rows = db.execute(text("""
        WITH st AS (SELECT DISTINCT ON (store_id, article_id) store_id, article_id, quantity
                    FROM stock_snapshots ORDER BY store_id, article_id, captured_at DESC, id DESC),
             sl AS (SELECT store_id, article_id, SUM(quantity_sold) q, COUNT(DISTINCT sale_date) d
                    FROM sales_history WHERE sale_date >= :since GROUP BY store_id, article_id)
        SELECT s.id sid, s.name sname, a.id aid, a.sku, a.name, a.pack_size pk, COALESCE(a.no_order, false) no_order,
               x.min_stock mn, x.max_stock mx, x.auto_adjust auto, COALESCE(st.quantity, 0) stock,
               COALESCE(sl.q, 0) sold, COALESCE(sl.d, 0) sale_days
        FROM stores s
        JOIN planogram p ON p.store_id = s.id
        JOIN articles a ON a.id = p.article_id AND a.default_supplier_id = 1 AND a.is_active
        JOIN store_article_settings x ON x.store_id = s.id AND x.article_id = a.id
        LEFT JOIN st ON st.store_id = s.id AND st.article_id = a.id
        LEFT JOIN sl ON sl.store_id = s.id AND sl.article_id = a.id
        WHERE s.is_active AND COALESCE(s.mistral_db_path, '') <> 'sofia' AND a.name NOT ILIKE :amb
          AND (:sid = 0 OR s.id = :sid)
    """), {"since": since, "sid": store_id, "amb": "%АМБАЛАЖ%"}).mappings().all()

    arts: dict[int, dict] = {}
    for r in rows:
        adr = float(r["sold"]) / days
        a = arts.setdefault(r["aid"], {"sku": r["sku"], "name": r["name"], "pack": int(r["pk"] or 1),
                                       "no_order": r["no_order"], "stores": [], "fast_stores": 0, "all_stores": 0})
        a["all_stores"] += 1
        row = {"store_id": r["sid"], "store": r["sname"], "min": float(r["mn"]), "max": float(r["mx"]),
               "locked": r["auto"] is False, "stock": float(r["stock"]), "sold": float(r["sold"]),
               "adr": round(adr, 3)}
        if adr > max_adr:
            a["fast_stores"] += 1
            a.setdefault("fast", []).append(row)
            continue
        a["stores"].append(row)
    out = []
    for a in arts.values():
        ss = a["stores"]
        if not ss:
            continue
        stock = sum(max(s["stock"], 0) for s in ss)
        sold = sum(s["sold"] for s in ss)
        live = [s["max"] for s in ss if s["max"] > 0]
        a.update({"slow_stores": len(ss), "stock": stock, "sold": sold,
                  "adr_store": round(sold / days / len(ss), 3),
                  "days_stock": round(stock / (sold / days)) if sold else None,
                  "max_now": median(live) if live else 0, "stopped_stores": sum(1 for s in ss if s["max"] <= 0)})
        a.setdefault("fast", [])
        a["transfers"] = transfers(a)
        out.append(a)
    # най-отгоре: най-много стока, която стои в складовете (места), после най-бавните
    out.sort(key=lambda a: (-a["stock"], -(a["days_stock"] if a["days_stock"] is not None else 10**6)))
    return {"days": days, "max_adr": max_adr, "store_id": store_id, "articles": out,
            "totals": {"articles": len(out), "stock": sum(a["stock"] for a in out),
                       "sold": sum(a["sold"] for a in out)}}
