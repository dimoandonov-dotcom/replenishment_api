"""
Импорт на наличности от Excel справки (режим "справки" - докато няма
директен достъп до Firebird).

Очакван формат (както в справките от Mistral):
    Oбект | Мат. № | Име на материал | К-во | [Последен доставчик]
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from io import BytesIO

import openpyxl
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m


def normalize_store_name(name: str) -> str:
    if not name:
        return ""
    n = str(name).strip().upper()
    n = re.sub(r"^\d+\.\s*", "", n)
    n = re.sub(r"^М-Н\s+", "", n)
    n = re.sub(r"^МАГАЗИН\s+", "", n)
    n = re.sub(r"^СУПЕРМАРКЕТ\s+", "", n)
    n = re.sub(r"^АЛКОХОЛ И ЦИГАРИ\s*/\s*", "", n)
    n = n.strip("/ ").strip()
    n = re.sub(r"\s+", " ", n).strip()
    if n.endswith("Ъ") and not n.endswith("ЪР"):
        n = n[:-1]
    return n


def store_lookup_map(db: Session) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in db.execute(select(m.Store)).scalars().all():
        out[normalize_store_name(s.name)] = s.id
    for a in db.execute(select(m.StoreAlias)).scalars().all():
        out[a.alias_normalized] = a.store_id
    return out


def import_stock_report(
    db: Session,
    file_bytes: bytes,
    captured_at: datetime | None = None,
    only_known_articles: bool = True,
) -> dict:
    captured_at = captured_at or datetime.now(timezone.utc)

    wb = openpyxl.load_workbook(BytesIO(file_bytes), data_only=True, read_only=True)
    ws = wb.worksheets[0]

    stores = store_lookup_map(db)
    skus = {a.sku: a.id for a in db.execute(select(m.Article)).scalars().all()}

    inserted = 0
    unknown_stores: dict[str, int] = {}
    unknown_skus = 0
    bad_rows = 0
    header_seen = False

    for row in ws.iter_rows(values_only=True):
        if not row or all(v is None for v in row):
            continue
        obj, sku, name = row[0], row[1] if len(row) > 1 else None, row[2] if len(row) > 2 else None
        qty = row[3] if len(row) > 3 else None

        if isinstance(obj, str) and "БЕКТ" in obj.upper().replace("O", "О"):
            header_seen = True
            continue
        if sku is None or not isinstance(sku, (int, float)):
            bad_rows += 1
            continue

        store_id = stores.get(normalize_store_name(obj)) if isinstance(obj, str) else None
        if store_id is None:
            key = str(obj).strip() if obj else "(празно)"
            unknown_stores[key] = unknown_stores.get(key, 0) + 1
            continue

        article_id = skus.get(str(int(sku)))
        if article_id is None:
            unknown_skus += 1
            if only_known_articles:
                continue
            art = m.Article(sku=str(int(sku)), name=str(name or ""), pack_size=1)
            db.add(art)
            db.flush()
            skus[art.sku] = art.id
            article_id = art.id

        try:
            q = float(qty) if qty is not None else 0.0
        except (TypeError, ValueError):
            bad_rows += 1
            continue

        db.add(m.StockSnapshot(
            store_id=store_id, article_id=article_id,
            quantity=q, captured_at=captured_at,
        ))
        inserted += 1

    db.commit()
    return {
        "inserted": inserted,
        "unknown_skus_skipped": unknown_skus,
        "unknown_stores": unknown_stores,
        "bad_rows": bad_rows,
        "captured_at": captured_at.isoformat(),
        "note": ("SKU-та извън номенклатурата се пропускат "
                 "(само артикули с настройки участват в заявки)")
                if only_known_articles else "непознатите SKU-та бяха добавени",
    }
