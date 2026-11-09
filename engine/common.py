"""Shared paths, config, database and log for the Closar studio (engine adapted from Gig Radar)."""
import datetime as dt
import json
import os
import sqlite3
import threading
import urllib.request
from pathlib import Path

import yaml

SYSTEM = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("CLOSAR_DB") or SYSTEM / "data" / "closar.db")   # CLOSAR_DB: test on a copy
LOG_PATH = SYSTEM / "data" / "closar.log"
PROMPTS = SYSTEM / "prompts"

_lock = threading.Lock()
_migrated = False

STAGES = ("cold", "messaged", "replied", "talks", "paid", "lost", "skip")


def cfg():
    return yaml.safe_load((SYSTEM / "config.yaml").read_text(encoding="utf-8"))


def services():
    """{id: {name, icon, keywords, ...}} for the three menus."""
    return cfg().get("services") or {}


def proof_text():
    return (SYSTEM / "proof.yaml").read_text(encoding="utf-8")


def areas():
    """The 14 business areas + the creator area: signs, fix, prices, message lines (areas.yaml)."""
    return yaml.safe_load((SYSTEM / "areas.yaml").read_text(encoding="utf-8")) or {}


def proof():
    try:
        return yaml.safe_load(proof_text()) or {}
    except yaml.YAMLError:
        return {}


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def today():
    return dt.date.today().isoformat()


def days_from_today(n):
    return (dt.date.today() + dt.timedelta(days=int(n))).isoformat()


def log(where, msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} [{where}] {msg}"
    print(line)
    with _lock, open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def http_get(url, timeout=25):
    """Plain GET that honestly identifies itself (never a fake browser)."""
    req = urllib.request.Request(url, headers={"User-Agent": cfg()["user_agent"], "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def http_json(url, timeout=25):
    return json.loads(http_get(url, timeout))


SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY,          -- source:native id
  source TEXT, platform TEXT, url TEXT, author TEXT, title TEXT, text TEXT,
  posted_at TEXT, found_at TEXT,
  dm_url TEXT, reply_hint TEXT,
  stage TEXT DEFAULT 'queued',  -- queued | filtered | scored | error
  guess TEXT,                   -- keyword menu guess
  verdict TEXT, score INTEGER, category TEXT, budget TEXT, flags TEXT, why TEXT,
  draft TEXT, reply_via TEXT,
  outreach TEXT DEFAULT 'todo', -- todo | sent | answered | dismissed (older rows: replied | won)
  sent_at TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);

-- The CRM: one row per person or business, whichever hunt found them.
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  service TEXT,                 -- business | creator
  source TEXT,                  -- post | prospect | pain | reddit | manual
  source_ref TEXT,              -- post id or the search that found them
  dedupe_key TEXT UNIQUE,
  name TEXT, handle TEXT, platform TEXT, profile_url TEXT, website TEXT, email TEXT,
  evidence TEXT, evidence_url TEXT,          -- what was found about them, and where
  stage TEXT DEFAULT 'cold',    -- cold | messaged | replied | talks | paid | lost | skip
  score INTEGER, why TEXT, flags TEXT,
  contact_via TEXT, dm_url TEXT,
  dm_draft TEXT, email_subject TEXT, email_body TEXT, followup_draft TEXT,
  value REAL, currency TEXT, note TEXT,
  scored INTEGER DEFAULT 0,
  follow_ups INTEGER DEFAULT 0, next_follow_up TEXT, last_contact_at TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contact_id INTEGER, at TEXT, kind TEXT, channel TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS events_contact ON events(contact_id);
"""

# Columns added to posts after v1. Old rows: kind 'hire', menu taken from the old category.
POST_COLUMNS = {
    "kind": "TEXT DEFAULT 'hire'",   # hire | question | pain
    "service": "TEXT",
    "mention": "TEXT",               # Reddit answers: optional line about your work
    "mention_used": "INTEGER DEFAULT 0",
    "contact_id": "INTEGER",
}
OLD_CATEGORY = {}
# Columns added for the audit (Closar): signs found, the main problem and when the audit ran.
CONTACT_COLUMNS = {"audit": "TEXT", "main_area": "TEXT", "audited_at": "TEXT", "kind": "TEXT"}


def _migrate(con):
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(posts)")}
    for col, decl in POST_COLUMNS.items():
        if col not in have:
            con.execute(f"ALTER TABLE posts ADD COLUMN {col} {decl}")
    have = {r[1] for r in con.execute("PRAGMA table_info(contacts)")}
    for col, decl in CONTACT_COLUMNS.items():
        if col not in have:
            con.execute(f"ALTER TABLE contacts ADD COLUMN {col} {decl}")
    for old, new in OLD_CATEGORY.items():
        con.execute("UPDATE posts SET service=? WHERE service IS NULL AND (category=? OR (category IS NULL AND guess=?))",
                    (new, old, old))
    con.commit()


def db():
    global _migrated
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    if not _migrated:
        with _lock:
            if not _migrated:
                _migrate(con)
                _migrated = True
    return con


def set_state(k, v):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (k, v))


def get_state(k, default=None):
    with db() as con:
        row = con.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def due_slot(times, state_key):
    """The latest of today's HH:MM times that has passed and hasn't run yet, else None (marks it as run)."""
    hhmm = dt.datetime.now().strftime("%H:%M")
    due = [t for t in sorted(times or []) if t <= hhmm]
    slot = f"{today()} {due[-1]}" if due else None
    if slot and get_state(state_key) != slot:
        set_state(state_key, slot)
        return slot
    return None
