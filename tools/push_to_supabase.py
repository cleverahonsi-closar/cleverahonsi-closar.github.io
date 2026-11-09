"""One-time move: data/closar.db -> Supabase. Also sets up the tables and lets your login use the studio.

    python tools/push_to_supabase.py you@example.com

Needs online.yaml with pg_url (the Supabase connection string). Safe to run again: rows are upserted by id.
Your login (Supabase > Authentication > Users) must exist first; that account becomes the only owner.
"""
import sqlite3
import sys
from pathlib import Path

import psycopg
import yaml

ROOT = Path(__file__).resolve().parent.parent
TABLES = ("posts", "state", "upwork_jobs", "contacts", "events")
KEYS = {"state": "k"}


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    owner = sys.argv[1].strip().lower()
    pg_url = (yaml.safe_load((ROOT / "online.yaml").read_text(encoding="utf-8")) or {}).get("pg_url")
    if not pg_url:
        sys.exit("online.yaml has no pg_url")
    src = sqlite3.connect(ROOT / "data" / "closar.db")
    src.row_factory = sqlite3.Row
    with psycopg.connect(pg_url, prepare_threshold=None) as pg:
        pg.execute((ROOT / "supabase" / "schema.sql").read_text(encoding="utf-8"))
        print("tables ready")
        for t in TABLES:
            pg_cols = {r[0] for r in pg.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (t,))}
            rows = src.execute(f"SELECT * FROM {t}").fetchall()
            if not rows:
                print(f"{t}: 0 rows")
                continue
            cols = [c for c in rows[0].keys() if c in pg_cols]
            key = KEYS.get(t, "id")
            sql = (f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                   f"ON CONFLICT ({key}) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols if c != key)}")
            with pg.cursor() as cur:
                cur.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
            print(f"{t}: {len(rows)} rows")
        for t in ("contacts", "events"):   # new rows continue after the highest id copied over
            pg.execute(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), GREATEST((SELECT COALESCE(MAX(id), 0) FROM {t}), 1))")
        pg.execute("INSERT INTO owners (uid) SELECT id FROM auth.users WHERE lower(email)=%s ON CONFLICT DO NOTHING",
                       (owner,))
        found = pg.execute("SELECT count(*) FROM owners o JOIN auth.users u ON u.id=o.uid WHERE lower(u.email)=%s", (owner,)).fetchone()[0]
        print("owner:", owner, "ok" if found else "NOT FOUND - add this user in Supabase > Authentication > Users, then run again")
    print("done")


if __name__ == "__main__":
    main()
