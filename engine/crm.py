"""The client pipeline: one contact per person or business, from whichever hunt found them.
Stages: cold -> messaged -> replied -> talks -> paid (or lost / skip). Nothing here sends anything."""
import csv
import io
import json
import re
import urllib.parse

import free
from common import STAGES, cfg, days_from_today, db, now

FIELDS = ("service", "name", "handle", "platform", "profile_url", "website", "email", "evidence", "evidence_url",
          "contact_via", "dm_url", "dm_draft", "email_subject", "email_body", "followup_draft", "value", "currency",
          "note", "next_follow_up", "score", "why")


def _norm_url(u):
    u = (u or "").strip().lower()
    if not u:
        return ""
    p = urllib.parse.urlparse(u if "://" in u else "https://" + u)
    host = p.netloc.removeprefix("www.").removeprefix("m.")
    path = p.path.rstrip("/")
    return host + (path if host not in ("", ) else "")


def dedupe_key(d):
    """Same person found twice (by email, profile, website or handle) becomes one contact."""
    if d.get("email"):
        return "email:" + d["email"].strip().lower()
    if d.get("profile_url"):
        return "url:" + _norm_url(d["profile_url"])
    if d.get("website"):
        return "site:" + _norm_url(d["website"]).split("/")[0]
    if d.get("handle") and d.get("platform"):
        return f"handle:{d['platform'].lower()}:{d['handle'].lstrip('@').lower()}"
    if d.get("source_ref"):
        return "ref:" + d["source_ref"]
    return "name:" + re.sub(r"\W+", "", (d.get("name") or "").lower())


def row(r):
    if r is None:
        return None
    r = dict(r)
    r["flags"] = json.loads(r.get("flags") or "[]")
    return r


def get(cid):
    with db() as con:
        return row(con.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone())


def find_existing(d):
    with db() as con:
        r = con.execute("SELECT id FROM contacts WHERE dedupe_key=?", (dedupe_key(d),)).fetchone()
    return r["id"] if r else None


def add(d, source, source_ref="", stage="cold"):
    """Insert a contact unless the same person is already there. Returns (id, created)."""
    d = {k: v for k, v in d.items() if v not in (None, "")}
    d["source_ref"] = source_ref or d.get("source_ref", "")
    key = dedupe_key(d)
    with db() as con:
        old = con.execute("SELECT id FROM contacts WHERE dedupe_key=?", (key,)).fetchone()
        if old:
            return old["id"], False
        cols = [k for k in FIELDS if k in d]
        con.execute(
            f"INSERT INTO contacts (source, source_ref, dedupe_key, stage, created_at, updated_at, flags{''.join(', ' + c for c in cols)})"
            f" VALUES (?,?,?,?,?,?,?{',?' * len(cols)})",
            (source, d["source_ref"], key, stage, now(), now(), json.dumps(d.get("flags") or []), *[d[c] for c in cols]))
        cid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    event(cid, "found", source, (d.get("evidence") or "")[:1500])
    return cid, True


def update(cid, fields):
    fields = {k: v for k, v in fields.items() if k in FIELDS or k in ("flags", "scored", "stage")}
    if "flags" in fields and not isinstance(fields["flags"], str):
        fields["flags"] = json.dumps(fields["flags"] or [])
    if not fields:
        return
    with db() as con:
        con.execute(f"UPDATE contacts SET {', '.join(k + '=?' for k in fields)}, updated_at=? WHERE id=?",
                    (*fields.values(), now(), cid))


def event(cid, kind, channel="", text=""):
    with db() as con:
        con.execute("INSERT INTO events (contact_id, at, kind, channel, text) VALUES (?,?,?,?,?)",
                    (cid, now(), kind, channel or "", text or ""))


def events(cid):
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM events WHERE contact_id=? ORDER BY id", (cid,))]


def mark_sent(cid, channel, text):
    """You sent a message (first one or a follow-up). Sets the next follow-up date."""
    c = get(cid)
    o = cfg()["outreach"]
    days = o.get("follow_up_days") or [3, 7, 14]
    if c["stage"] in ("cold", "skip", "lost") or not c.get("last_contact_at"):
        n, kind, stage = 0, "messaged", "messaged"
        nxt = days_from_today(days[0]) if days else None
    elif c["stage"] == "messaged":
        n, kind, stage = (c["follow_ups"] or 0) + 1, "followed_up", "messaged"
        nxt = days_from_today(days[n]) if n < len(days) else None
    else:  # replied / talks / paid: keep the stage, check back soon
        n, kind, stage = c["follow_ups"] or 0, "followed_up", c["stage"]
        nxt = days_from_today(o.get("talks_follow_up_days", 2)) if c["stage"] in ("replied", "talks") else None
    # the next follow-up comes from the templates (free); "Write follow-up" asks Claude for a personal one
    fu = free.followup(c, n) if nxt and stage == "messaged" else ""
    with db() as con:
        con.execute("UPDATE contacts SET stage=?, follow_ups=?, next_follow_up=?, last_contact_at=?, followup_draft=?,"
                    " updated_at=? WHERE id=?", (stage, n, nxt, now(), fu, now(), cid))
    event(cid, kind, channel, text)


def set_stage(cid, stage, note=""):
    if stage not in STAGES:
        raise ValueError("unknown stage")
    o = cfg()["outreach"]
    nxt = days_from_today(o.get("talks_follow_up_days", 2)) if stage in ("replied", "talks") else None
    with db() as con:
        if stage in ("replied", "talks", "paid", "lost", "skip", "cold"):
            con.execute("UPDATE contacts SET stage=?, next_follow_up=?, updated_at=? WHERE id=?", (stage, nxt, now(), cid))
        else:
            con.execute("UPDATE contacts SET stage=?, updated_at=? WHERE id=?", (stage, now(), cid))
    event(cid, stage, "", note)


def listing(stage=None, service=None, due=False, limit=300):
    where, args = ["1=1"], []
    if stage == "pipeline":
        where.append("stage IN ('messaged','replied','talks','paid','lost')")
    elif stage:
        where.append("stage=?")
        args.append(stage)
    if service:
        where.append("service=?")
        args.append(service)
    if due:
        where.append("stage IN ('messaged','replied','talks') AND next_follow_up IS NOT NULL AND next_follow_up<=date('now','localtime')")
    order = "next_follow_up ASC" if due else ("score DESC, created_at DESC" if stage == "cold" else "updated_at DESC")
    with db() as con:
        return [row(r) for r in con.execute(
            f"SELECT * FROM contacts WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?", (*args, limit))]


def counts(service=None):
    sw, args = ("AND service=?", (service,)) if service else ("", ())
    with db() as con:
        by_stage = {r["stage"]: r["n"] for r in con.execute(
            f"SELECT stage, COUNT(*) n FROM contacts WHERE 1=1 {sw} GROUP BY stage", args)}
        due = con.execute(
            f"SELECT COUNT(*) FROM contacts WHERE stage IN ('messaged','replied','talks') AND next_follow_up IS NOT NULL"
            f" AND next_follow_up<=date('now','localtime') {sw}", args).fetchone()[0]
        sent_today = con.execute(
            f"SELECT COUNT(*) FROM events e JOIN contacts c ON c.id=e.contact_id WHERE e.kind IN ('messaged','followed_up')"
            f" AND date(e.at,'localtime')=date('now','localtime') {sw.replace('service', 'c.service')}", args).fetchone()[0]
        paid = con.execute(f"SELECT currency, SUM(value) v FROM contacts WHERE stage='paid' AND value IS NOT NULL {sw}"
                           f" GROUP BY currency", args).fetchall()
    return {"stages": by_stage, "due": due, "sent_today": sent_today,
            "paid_total": {(r["currency"] or "USD"): r["v"] for r in paid}}


CSV_COLS = ("id", "service", "stage", "name", "handle", "platform", "profile_url", "website", "email", "score", "why",
            "contact_via", "source", "value", "currency", "next_follow_up", "follow_ups", "last_contact_at", "note",
            "evidence_url", "created_at")


def to_csv(service=None):
    rows = listing(service=service, limit=100000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLS)
    for r in rows:
        w.writerow([r.get(k) if r.get(k) is not None else "" for k in CSV_COLS])
    return "﻿" + buf.getvalue()   # BOM so Excel reads the text as UTF-8
