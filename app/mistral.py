"""
Връзка към базата на Мистрал (MS SQL Server, база MistralAnalytics).
Само четене. Параметрите идват от настройките на сървъра:
MSSQL_HOST, MSSQL_PORT, MSSQL_USER, MSSQL_PASSWORD, MSSQL_DB.

Ключови таблици (по описанието от офиса):
  MATERIAL        LOCATIONID + MATERIALCODE -> QTY, LASTDELIVERYPRICE(WOTAX), PARTNERNUM, DEFAULTPARTNERNUM
  LOCATION        ID -> NAME
  PARTNER         LOCATIONID + NUM -> NAME, TAXNUM
  PRICELIST       LOCATIONID + PRICELISTID + MATERIALCODE -> PRICE
  PRICELISTNAME   LOCATIONID + ID, PRICELISTTYPE = 0 е основната
PARTNER и UOM са по обект - винаги се връзват и по LOCATIONID.
"""
from __future__ import annotations

import os

import pymssql

TABLES = ["MATERIAL", "LOCATION", "PARTNER", "PRICELIST", "PRICELISTNAME",
          "STORAGEPARTNER", "UOM", "BARCODE"]


def connect():
    return pymssql.connect(
        server=os.getenv("MSSQL_HOST", ""),
        port=int(os.getenv("MSSQL_PORT", "1433")),
        user=os.getenv("MSSQL_USER", ""),
        password=os.getenv("MSSQL_PASSWORD", ""),
        database=os.getenv("MSSQL_DB", "MistralAnalytics"),
        login_timeout=15, timeout=120, charset="UTF-8", as_dict=True,
    )


def configured() -> bool:
    return bool(os.getenv("MSSQL_HOST") and os.getenv("MSSQL_PASSWORD"))


def probe() -> dict:
    """Какво има в базата - колони, брой редове, обекти, пример."""
    out: dict = {"tables": {}}
    with connect() as conn:
        cur = conn.cursor()
        for t in TABLES:
            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME = %s ORDER BY ORDINAL_POSITION", (t,))
            cols = [f"{r['COLUMN_NAME']}:{r['DATA_TYPE']}" for r in cur.fetchall()]
            cnt = None
            if cols:
                cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
                cnt = cur.fetchone()["n"]
            out["tables"][t] = {"rows": cnt, "columns": cols}
        cur.execute("SELECT ID, NAME FROM LOCATION ORDER BY ID")
        out["locations"] = [{"id": r["ID"], "name": r["NAME"]} for r in cur.fetchall()]
        loc = out["locations"][0]["id"] if out["locations"] else None
        if loc is not None:
            cur.execute(
                "SELECT TOP 5 m.MATERIALCODE, m.MATERIAL, m.QTY, "
                "m.LASTDELIVERYPRICEWOTAX, m.PARTNERNUM, m.DEFAULTPARTNERNUM, "
                "m.LASTDELIVERYDATE "
                "FROM MATERIAL m WHERE m.LOCATIONID = %s AND m.QTY <> 0", (loc,))
            out["sample_material"] = [
                {k: (str(v) if v is not None else None) for k, v in r.items()}
                for r in cur.fetchall()
            ]
            cur.execute(
                "SELECT TOP 40 p.NUM, p.NAME, p.TAXNUM FROM PARTNER p "
                "WHERE p.LOCATIONID = %s AND (p.NAME LIKE N'%%НДК%%' OR p.NAME LIKE N'%%NDK%%' "
                "OR p.NAME LIKE N'%%ТАБАКО%%' OR p.NAME LIKE N'%%ОРБИКО%%' "
                "OR p.NAME LIKE N'%%АЙВА%%' OR p.NAME LIKE N'%%ЕКСПРЕС%%')", (loc,))
            out["sample_partners"] = [
                {k: (str(v) if v is not None else None) for k, v in r.items()}
                for r in cur.fetchall()
            ]
    return out


def sync_stock(db) -> dict:
    """
    Дърпа текущите наличности от Мистрал за всички наши артикули във
    всички наши магазини и ги записва като нова снимка (stock_snapshots).
    Обектите се разпознават по име (същото нормализиране + псевдоними).
    """
    from datetime import datetime, timezone
    from sqlalchemy import insert, select
    from . import models as m
    from .imports import normalize_store_name

    lk: dict[str, int] = {}
    for s in db.execute(select(m.Store)).scalars().all():
        lk[normalize_store_name(s.name)] = s.id
    for al in db.execute(select(m.StoreAlias)).scalars().all():
        lk[al.alias_normalized] = al.store_id
    active_ids = {
        s.id for s in db.execute(
            select(m.Store).where(m.Store.is_active.is_(True))
        ).scalars().all()
    }
    arts = {
        int(a.sku): a.id for a in db.execute(select(m.Article)).scalars().all()
        if a.sku.isdigit()
    }
    if not arts:
        return {"inserted": 0, "note": "няма артикули"}

    started = datetime.now(timezone.utc)
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT ID, NAME FROM LOCATION")
        loc_map, unmatched = {}, []
        for r in cur.fetchall():
            sid = lk.get(normalize_store_name(r["NAME"]))
            if sid is None:
                n = normalize_store_name(r["NAME"])
                sid = lk.get(n.replace("Д.", "", 1).strip())
            if sid is not None and sid in active_ids:
                loc_map[r["ID"]] = sid
            else:
                unmatched.append(r["NAME"])
        codes = ",".join(str(c) for c in arts)
        locs = ",".join(str(l) for l in loc_map)
        cur.execute(
            f"SELECT LOCATIONID, MATERIALCODE, QTY FROM MATERIAL "
            f"WHERE LOCATIONID IN ({locs}) AND MATERIALCODE IN ({codes})"
        )
        rows = cur.fetchall()

    now = datetime.now(timezone.utc)
    batch = [
        {"store_id": loc_map[r["LOCATIONID"]],
         "article_id": arts[int(r["MATERIALCODE"])],
         "quantity": float(r["QTY"] or 0), "captured_at": now}
        for r in rows
        if r["LOCATIONID"] in loc_map and int(r["MATERIALCODE"]) in arts
    ]
    if batch:
        db.execute(insert(m.StockSnapshot), batch)
        db.commit()
    return {
        "inserted": len(batch),
        "stores": len(set(b["store_id"] for b in batch)),
        "articles": len(set(b["article_id"] for b in batch)),
        "skipped_locations": unmatched,
        "seconds": round((now - started).total_seconds(), 1),
        "captured_at": now.isoformat(),
    }


def list_tables() -> list[dict]:
    """Всички таблици с приблизителен брой редове (бързо, от метаданните)."""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT t.name AS name, SUM(p.rows) AS rows "
            "FROM sys.tables t JOIN sys.partitions p "
            "ON p.object_id = t.object_id AND p.index_id IN (0,1) "
            "GROUP BY t.name ORDER BY SUM(p.rows) DESC"
        )
        return [{"table": r["name"], "rows": int(r["rows"] or 0)} for r in cur.fetchall()]


def sample_table(table: str, n: int = 5) -> dict:
    """Колони + първите n реда. Името се проверява срещу списъка на базата."""
    n = max(1, min(n, 50))
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sys.tables WHERE name = %s", (table,))
        row = cur.fetchone()
        if not row:
            raise ValueError("Няма такава таблица")
        safe = row["name"]
        cur.execute(
            "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = %s ORDER BY ORDINAL_POSITION", (safe,))
        cols = [f"{r['COLUMN_NAME']}:{r['DATA_TYPE']}" for r in cur.fetchall()]
        cur.execute(f"SELECT TOP {n} * FROM [{safe}]")
        rows = [{k: (str(v) if v is not None else None) for k, v in r.items()}
                for r in cur.fetchall()]
    return {"table": safe, "columns": cols, "rows": rows}
