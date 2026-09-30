"""
Инициализация и миграции на базата при всяко стартиране.

1) Ако базата е празна - създава схемата от schema.sql.
2) Винаги пуска MIGRATIONS - идемпотентни промени (ADD COLUMN IF NOT
   EXISTS, CREATE TABLE IF NOT EXISTS), за да се обновява и вече
   съществуваща жива база без загуба на данни.
"""
import os
import sys

from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.db import engine  # noqa: E402

MIGRATIONS = [
    "ALTER TABLE stores ADD COLUMN IF NOT EXISTS size_class TEXT",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS supplier_name TEXT",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS base_price NUMERIC(12,4)",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS trade_discount NUMERIC(8,4)",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS delivery_price NUMERIC(12,4)",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS price_note TEXT",
    """CREATE TABLE IF NOT EXISTS planogram (
        store_id   INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
        article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
        PRIMARY KEY (store_id, article_id)
    )""",
]


def run_sql_file(path):
    sql = open(path, encoding="utf-8").read()
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(sql)
        raw.commit()
        cur.close()
    finally:
        raw.close()


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'stores')"
        )).scalar()
    if not exists:
        print("Инициализирам схемата от schema.sql ...")
        run_sql_file(os.path.join(base, "schema.sql"))

    with engine.begin() as conn:
        for stmt in MIGRATIONS:
            conn.execute(text(stmt))
    print("Миграции: OK")


if __name__ == "__main__":
    main()
