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
