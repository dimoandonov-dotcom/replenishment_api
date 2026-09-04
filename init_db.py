"""
Автоматична инициализация на схемата при първо стартиране на Railway.

Безопасно е да се пуска при всеки деплой: проверява дали таблица
'stores' вече съществува - ако да, не прави нищо.
"""
import os
import sys

from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.db import engine  # noqa: E402


def main():
    schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'stores')"
        )).scalar()
        if exists:
            print("Схемата вече съществува - нищо не се прави.")
            return

    print("Инициализирам схемата от schema.sql ...")
    sql = open(schema_path, encoding="utf-8").read()

    # schema.sql съдържа МНОГО SQL изречения (CREATE TABLE, CREATE INDEX...).
    # SQLAlchemy's connection.execute(text(...)) поддържа само ЕДНО изречение
    # наведнъж (extended query protocol) - затова минаваме през суровата
    # psycopg2 връзка, която може да изпълни целия файл наведнъж.
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(sql)
        raw.commit()
        cur.close()
    finally:
        raw.close()
    print("Готово.")


if __name__ == "__main__":
    main()
