"""Applies scripts/init_db.sql (idempotent -- safe to re-run) against
DATABASE_URL. Run once per Neon branch before the first migration:

    python3 scripts/init_db.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db


def main():
    if not db.enabled():
        print("DATABASE_URL is not set (check your .env) -- nothing to do.")
        sys.exit(1)

    sql_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "init_db.sql")
    with open(sql_path) as f:
        sql = f.read()

    with db.get_pool().connection() as conn:
        conn.execute(sql)
    print("Schema applied.")


if __name__ == "__main__":
    main()
