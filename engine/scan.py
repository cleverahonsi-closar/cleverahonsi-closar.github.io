"""Fetch -> sort -> save -> Claude. Every post becomes one of three kinds:
  hire     someone wants to pay for help        -> Hunt 1: scored, reply draft
  question a Reddit question in your areas      -> Hunt 3: answer draft
  pain     someone describes a problem you solve -> Hunt 2: becomes a prospect (DM / email draft)
Run directly for one scan: python scan.py [--sweep | --prospects]"""
import datetime as dt
import hashlib
import json
import re
import sys
import threading
import urllib.parse

import ai
import free
import prospects
import sources
from common import cfg, db, local_day_start_utc, log, now, services, set_state

busy = threading.Lock()


def _has(text, words):
    for w in words:
        w = w.lower()
        if len(w) <= 4 and w.isalnum():
            if re.search(rf"\b{re.escape(w)}\b", text):
                return w
        elif w in text:
            return w
    return None


def _age_days(posted):
    try:
        t = dt.datetime.fromisoformat((posted or "").replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 86400
    except ValueError:
        return None


def best_service(text):
    hits = {sid: sum(1 for k in s.get("keywords") or [] if _has(text, [k])) for sid, s in services().items()}
    if not hits:
        return None
    sid = max(hits, key=hits.get)
    return sid if hits[sid] else None


def gate(p):
    """Returns (keep, kind, service, reason). Cheap word checks, so Claude only sees likely ones."""
    c = cfg()
    title = (p.get("title") or "").lower()
    body = (title + " " + (p.get("text") or "")).lower()
    age = _age_days(p.get("posted_at"))
    max_age = c.get("sweep_days", 21) if p["id"].startswith("web:") else c["max_age_days"]
    if age is not None and age > max_age:
        return False, None, None, f"older than {max_age} days"
    w = _has(title or body[:160], c["skip_words"])
    if w:
        return False, None, None, f"seller post ({w})"
    service = best_service(body)
    if not service:
        return False, None, None, "not in any of your menus"
    is_reddit = p["id"].startswith("reddit:")
    if p["id"].startswith(("hn:", "disc:")) or _has(body, c["hire_words"]):
        return True, "hire", service, ""
    if is_reddit and ("?" in title or _has(title + " " + body[:300], c["question_words"])):
        if age is not None and age > (c.get("answers") or {}).get("max_age_days", 2):
            return False, None, service, "question too old to answer"
        return True, "question", service, ""
    if _has(body, c["need_words"]):
        return True, "hire", service, ""
    if _has(body, c["pain_words"]):
        return True, "pain", service, ""
    return False, None, service, "no hiring, question or problem words"


def save(posts):
    """Insert new posts (dedupe by id and url). Returns {kind: number kept}."""
    kept = {"hire": 0, "question": 0, "pain": 0}
    with db() as con:
        for p in posts:
            if con.execute("SELECT 1 FROM posts WHERE id=? OR (url=? AND url!='')", (p["id"], p["url"])).fetchone():
                continue
            keep, kind, service, reason = gate(p)
            if keep:
                p = sources.fill_discourse_text(p)
                kept[kind] += 1
            con.execute(
                "INSERT INTO posts (id,source,platform,url,author,title,text,posted_at,found_at,dm_url,reply_hint,stage,"
                "guess,service,kind,why) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["id"], p["source"], p["platform"], p["url"], p["author"], p["title"], p["text"], p["posted_at"],
                 now(), p.get("dm_url", ""), p.get("reply_hint", ""), "queued" if keep else "filtered", service,
                 service, kind or "hire", reason))
    return kept


def _apply_scores(rows, verdicts):
    by_id = {v.get("id"): v for v in verdicts if isinstance(v, dict)}
    valid = set(services())
    done = 0
    with db() as con:
        for r in rows:
            v = by_id.get(r["id"])
            if not v:
                continue
            svc = v.get("service") if v.get("service") in valid else r["service"]
            con.execute(
                "UPDATE posts SET stage='scored', verdict=?, score=?, service=?, category=?, budget=?, flags=?, why=?,"
                " reply_via=?, draft=? WHERE id=?",
                (v.get("verdict", "skip"), int(v.get("score") or 0), svc, svc, v.get("budget") or "",
                 json.dumps(v.get("flags") or []), v.get("why") or "", v.get("reply_via") or "", v.get("draft") or "",
                 r["id"]))
            done += 1
    return done


def score_queue(limit=None):
    """Hunt 1: score queued "need someone" posts."""
    limit = limit or cfg()["score_batch"]
    with db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM posts WHERE stage='queued' AND kind='hire' ORDER BY posted_at DESC LIMIT ?", (limit,))]
    if not rows:
        return 0
    if not cfg().get("auto_claude"):   # free: word rules + template reply; Rewrite asks Claude
        done = _apply_scores(rows, [dict(free.score_post(r), id=r["id"]) for r in rows])
        log("score", f"word rules scored {done}")
        return done
    try:
        done = _apply_scores(rows, ai.score(rows))
    except Exception as e:
        log("score", f"claude scoring failed, will retry next scan: {e}")
        return 0
    log("score", f"scored {done}/{len(rows)}")
    return done


def _today_count(sql):
    with db() as con:
        return con.execute(sql + " AND found_at>=?", (local_day_start_utc(),)).fetchone()[0]


def answer_queue(limit=10):
    """Hunt 3: draft answers for Reddit questions, newest first, up to the daily cap."""
    a = cfg().get("answers") or {}
    with db() as con:  # threads that went stale while waiting aren't worth answering
        con.execute("UPDATE posts SET stage='filtered', why='question too old to answer' WHERE stage='queued'"
                    " AND kind='question' AND posted_at<?",
                    ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=a.get("max_age_days", 2))).isoformat(),))
    if not cfg().get("auto_claude"):   # free: listed for you; Rewrite on a card asks Claude for the answer
        with db() as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM posts WHERE stage='queued' AND kind='question'")]
            for r in rows:
                v = free.question(r)
                con.execute("UPDATE posts SET stage='scored', verdict=?, score=?, why=?, draft='', mention='', flags='[]'"
                            " WHERE id=?", (v["verdict"], v["score"], v["why"], r["id"]))
        return 0
    left = int(a.get("max_per_day", 30)) - _today_count(
        "SELECT COUNT(*) FROM posts WHERE kind='question' AND stage='scored'")
    if left <= 0:
        return 0
    with db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM posts WHERE stage='queued' AND kind='question' ORDER BY posted_at DESC LIMIT ?",
            (min(limit, left),))]
    if not rows:
        return 0
    try:
        verdicts = ai.answer(rows)
    except Exception as e:
        log("answer", f"claude failed, will retry next scan: {e}")
        return 0
    by_id = {v.get("id"): v for v in verdicts if isinstance(v, dict)}
    valid = set(services())
    done = 0
    with db() as con:
        for r in rows:
            v = by_id.get(r["id"])
            if not v:
                continue
            svc = v.get("service") if v.get("service") in valid else r["service"]
            con.execute("UPDATE posts SET stage='scored', verdict=?, score=?, service=?, category=?, why=?, draft=?,"
                        " mention=?, flags='[]' WHERE id=?",
                        (v.get("verdict", "skip"), int(v.get("score") or 0), svc, svc, v.get("why") or "",
                         v.get("answer") or "", v.get("mention") or "", r["id"]))
            done += 1
    log("answer", f"answered {done}/{len(rows)}")
    return done


def pain_queue():
    """Hunt 2: posts where someone describes a problem you solve become prospects (up to the daily cap)."""
    left = int(cfg()["prospects"].get("pain_per_day", 30)) - _today_count(
        "SELECT COUNT(*) FROM posts WHERE kind='pain' AND stage='scored'")
    with db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM posts WHERE stage='queued' AND kind='pain' ORDER BY posted_at DESC LIMIT ?", (max(left, 0),))]
    for r in rows:
        cid = prospects.from_post(r)
        with db() as con:
            con.execute("UPDATE posts SET stage='scored', verdict='prospect', contact_id=? WHERE id=?", (cid, r["id"]))
    return len(rows)


def process_queues():
    set_state("scan_status", "scoring posts")
    while score_queue():
        pass
    set_state("scan_status", "writing Reddit answers")
    while answer_queue():
        pass
    set_state("scan_status", "writing messages for new prospects")
    if pain_queue():
        prospects.score_pending() if cfg().get("auto_claude") else prospects.template_pending()


def run_scan():
    if not busy.acquire(blocking=False):
        return "busy"
    try:
        set_state("scan_status", "scanning")
        found, kept = 0, {"hire": 0, "question": 0, "pain": 0}
        for name, fn in sources.ALL.items():
            try:
                posts = fn()
            except Exception as e:
                log("scan", f"{name} failed: {e}")
                continue
            found += len(posts)
            k = save(posts)
            for kind in kept:
                kept[kind] += k[kind]
            log("scan", f"{name}: {len(posts)} posts, new: {k['hire']} need-someone, {k['question']} questions,"
                        f" {k['pain']} problems")
        process_queues()
        set_state("last_scan", now())
        set_state("last_scan_summary", f"{found} posts checked: {kept['hire']} new need-someone posts,"
                                       f" {kept['question']} questions, {kept['pain']} people with a problem you solve")
        return "ok"
    finally:
        set_state("scan_status", "idle")
        busy.release()


def _platform_from_url(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    for key, name in (("linkedin", "LinkedIn"), ("x.com", "X"), ("twitter", "X"), ("facebook", "Facebook"),
                      ("instagram", "Instagram"), ("discord", "Discord"), ("reddit", "Reddit"), ("bsky", "Bluesky"),
                      ("threads", "Threads"), ("t.me", "Telegram"), ("tiktok", "TikTok")):
        if key in host:
            return name
    return host or "Pasted"


def run_sweep():
    """LinkedIn / X / Facebook / Instagram "need someone" posts via Claude's web search (same sorting + scoring)."""
    if not busy.acquire(blocking=False):
        return "busy"
    try:
        set_state("scan_status", "searching LinkedIn / X / Facebook / Instagram")
        try:
            items = ai.sweep()
        except Exception as e:
            log("sweep", f"failed: {e}")
            return "error"
        posts = []
        for it in items:
            url = (it.get("url") or "").strip()
            if not url.startswith("http"):
                continue
            posts.append({
                "id": "web:" + hashlib.sha1(url.encode()).hexdigest()[:16], "source": "Web search",
                "platform": it.get("platform") or _platform_from_url(url), "url": url,
                "author": it.get("author") or "", "title": "", "text": it.get("text") or "",
                "posted_at": it.get("posted") or "", "dm_url": "",
                "reply_hint": "Found through search: open it logged in and reply or DM there.",
            })
        kept = save(posts)
        process_queues()
        set_state("last_sweep", now())
        set_state("last_sweep_summary", f"{len(posts)} search results, {kept['hire']} new need-someone posts")
        log("sweep", f"{len(posts)} results, kept {kept}")
        return "ok"
    finally:
        set_state("scan_status", "idle")
        busy.release()


def add_pasted(url, text, kind="hire", service=None):
    """A post you saw yourself (Discord, Instagram, a Facebook group, WhatsApp...). Skips the word checks."""
    url = (url or "").strip()
    pid = "paste:" + hashlib.sha1((url or text).encode()).hexdigest()[:16]
    service = service if service in services() else best_service(text.lower()) or next(iter(services()), "")
    with db() as con:
        con.execute("DELETE FROM posts WHERE id=?", (pid,))   # same as SQLite's INSERT OR REPLACE, works on Postgres too
        con.execute(
            "INSERT INTO posts (id,source,platform,url,author,title,text,posted_at,found_at,stage,reply_hint,"
            "kind,service,guess) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, "Pasted", _platform_from_url(url) if url else "Pasted", url, "", "", text[:4000], now(), now(),
             "queued", "You found this yourself: reply where you saw it.", kind, service, service))
    if cfg().get("auto_claude"):
        rescore(pid)
    else:
        free_score(pid)
    return pid


def free_score(pid):
    """Word rules + template for one post (no Claude)."""
    with db() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM posts WHERE id=?", (pid,))]
    if not rows:
        return
    if rows[0]["kind"] == "question":
        v = free.question(rows[0])
        with db() as con:
            con.execute("UPDATE posts SET stage='scored', verdict=?, score=?, why=?, draft='' WHERE id=?",
                        (v["verdict"], v["score"], v["why"], pid))
    else:
        _apply_scores(rows, [dict(free.score_post(rows[0]), id=pid)])


def rescore(pid):
    """Score / answer one post again (Rewrite button)."""
    with db() as con:
        con.execute("UPDATE posts SET stage='queued' WHERE id=?", (pid,))
        rows = [dict(r) for r in con.execute("SELECT * FROM posts WHERE id=?", (pid,))]
    if not rows:
        return
    if rows[0]["kind"] == "question":
        v = (ai.answer(rows) or [{}])[0]
        svc = v.get("service") if v.get("service") in services() else rows[0]["service"]
        with db() as con:
            con.execute("UPDATE posts SET stage='scored', verdict=?, score=?, service=?, why=?, draft=?, mention=?"
                        " WHERE id=?", (v.get("verdict", "answer"), int(v.get("score") or 0), svc, v.get("why") or "",
                                        v.get("answer") or "", v.get("mention") or "", pid))
    else:
        if not _apply_scores(rows, ai.score(rows)):
            with db() as con:
                con.execute("UPDATE posts SET stage='scored', verdict='maybe', why='Claude gave no verdict' WHERE id=?",
                            (pid,))


redraft = rescore


if __name__ == "__main__":
    if "--sweep" in sys.argv:
        print(run_sweep())
    elif "--prospects" in sys.argv:
        print(prospects.run(sys.argv[sys.argv.index("--prospects") + 1] if len(sys.argv) > sys.argv.index("--prospects") + 1 else None))
    else:
        print(run_scan())
