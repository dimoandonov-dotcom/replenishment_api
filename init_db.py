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
    """CREATE TABLE IF NOT EXISTS manual_orders (
        id          SERIAL PRIMARY KEY,
        store_id    INTEGER REFERENCES stores(id),
        store_raw   TEXT NOT NULL,
        received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        source      TEXT NOT NULL DEFAULT 'anindk',
        raw_text    TEXT,
        stock_at    TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS manual_order_lines (
        id           SERIAL PRIMARY KEY,
        order_id     INTEGER NOT NULL REFERENCES manual_orders(id) ON DELETE CASCADE,
        sku          TEXT NOT NULL,
        name         TEXT,
        store_qty    NUMERIC(12,2) NOT NULL DEFAULT 0,
        api_qty      NUMERIC(12,2) NOT NULL DEFAULT 0,
        stock        NUMERIC(12,2),
        min_stock    NUMERIC(12,2),
        max_stock    NUMERIC(12,2),
        pack_size    INTEGER,
        in_planogram BOOLEAN,
        price        NUMERIC(12,4),
        note         TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_mol_order ON manual_order_lines(order_id)",
    "CREATE INDEX IF NOT EXISTS ix_mo_received ON manual_orders(received_at)",
    """CREATE TABLE IF NOT EXISTS settings_log (
        id         SERIAL PRIMARY KEY,
        store_id   INTEGER NOT NULL REFERENCES stores(id),
        article_id INTEGER NOT NULL REFERENCES articles(id),
        old_min    NUMERIC(12,2), old_max NUMERIC(12,2),
        new_min    NUMERIC(12,2), new_max NUMERIC(12,2),
        source     TEXT NOT NULL,
        reason     TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_slog_store ON settings_log(store_id)",
    "CREATE INDEX IF NOT EXISTS ix_slog_created ON settings_log(created_at)",
    """CREATE TABLE IF NOT EXISTS app_assets (
        key        TEXT PRIMARY KEY,
        mime       TEXT NOT NULL,
        content    BYTEA NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS app_users (
        username      TEXT PRIMARY KEY,
        display_name  TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        is_active     BOOLEAN NOT NULL DEFAULT TRUE,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
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
