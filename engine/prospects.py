"""Hunt 2: prospects, people or businesses who fit a menu but haven't posted asking for help.
Found by a daily Claude web search per menu, or from posts where someone describes a problem you solve.
Claude scores each one and drafts a DM and an email; you send them yourself."""
import threading
import urllib.parse

import ai
import crm
import free
from common import cfg, db, log, now, proof, services, set_state

busy = threading.Lock()


def email_footer():
    p = proof()
    f = (p.get("email_footer") or "").strip()
    return ("\n\n" + f) if f else ""


def _exclude(service_id):
    with db() as con:
        rows = con.execute("SELECT name, website, profile_url FROM contacts WHERE service=? ORDER BY id DESC LIMIT 300",
                           (service_id,)).fetchall()
    return [" | ".join(x for x in (r["name"], r["website"], r["profile_url"]) if x) for r in rows]


def find(service_id):
    """One web-search run for one menu. Returns how many new prospects were added."""
    n = int(cfg()["prospects"].get("per_service", 12))
    try:
        found = ai.find_prospects(service_id, n, _exclude(service_id))
    except Exception as e:
        log("prospects", f"{service_id} search failed: {e}")
        return 0
    added = 0
    for f in found:
        if not isinstance(f, dict) or not (f.get("name") or f.get("profile_url") or f.get("website")):
            continue
        d = {k: str(f.get(k) or "").strip() for k in ("name", "handle", "platform", "profile_url", "website", "email",
                                                      "evidence", "evidence_url")}
        if d["email"] and "@" not in d["email"]:
            d["email"] = ""
        d["service"] = service_id
        _, created = crm.add(d, "prospect", source_ref=f"web search {service_id}")
        added += created
    log("prospects", f"{service_id}: {len(found)} found, {added} new")
    return added


def from_post(post):
    """Someone described a problem you solve (no hiring words): they become a cold prospect, the post is the evidence."""
    author = post.get("author") or ""
    profile = ""
    if post.get("platform") == "Reddit" and author:
        profile = f"https://www.reddit.com/user/{urllib.parse.quote(author)}"
    elif post.get("platform") == "Bluesky" and author:
        profile = f"https://bsky.app/profile/{author}"
    d = {"service": post.get("service") or post.get("guess"), "name": author, "handle": author,
         "platform": post.get("platform"), "profile_url": profile, "dm_url": post.get("dm_url") or "",
         "evidence": ((post.get("title") or "") + "\n" + (post.get("text") or "")).strip()[:3000],
         "evidence_url": post.get("url") or ""}
    cid, _ = crm.add(d, "pain", source_ref=post["id"])
    return cid


def add_manual(url, text, service_id=None):
    """A person you found yourself (profile link + what you know about them)."""
    url = (url or "").strip()
    host = urllib.parse.urlparse(url).netloc.lower()
    platform = next((name for key, name in (("instagram", "Instagram"), ("tiktok", "TikTok"), ("youtube", "YouTube"),
                                             ("linkedin", "LinkedIn"), ("x.com", "X"), ("twitter", "X"),
                                             ("facebook", "Facebook"), ("reddit", "Reddit"), ("bsky", "Bluesky"),
                                             ("t.me", "Telegram")) if key in host), "Website" if host else "Other")
    d = {"service": service_id or "", "platform": platform, "evidence": text.strip()[:3000], "evidence_url": url,
         "profile_url": url if platform != "Website" else "", "website": url if platform == "Website" else "",
         "name": url.rstrip("/").rsplit("/", 1)[-1] if url else text.strip()[:40]}
    cid, created = crm.add(d, "manual", source_ref="pasted")
    if not created:
        crm.update(cid, {"scored": 0})
    score_pending(only=[cid]) if cfg().get("auto_claude") else template_pending(only=[cid])
    return cid


def score_pending(only=None, batch=10):
    """Score + draft every prospect that hasn't been scored yet (or just the ids given)."""
    done = 0
    while True:
        with db() as con:
            if only:
                rows = [dict(r) for r in con.execute(
                    f"SELECT * FROM contacts WHERE id IN ({','.join('?' * len(only))})", only)]
            else:
                rows = [dict(r) for r in con.execute(
                    "SELECT * FROM contacts WHERE scored=0 AND stage='cold' ORDER BY id LIMIT ?", (batch,))]
        if not rows:
            return done
        try:
            verdicts = ai.score_prospects(rows)
        except Exception as e:
            log("prospects", f"scoring failed, will retry later: {e}")
            return done
        by_id = {str(v.get("id")): v for v in verdicts if isinstance(v, dict)}
        valid = set(services())
        foot = email_footer()
        for r in rows:
            v = by_id.get(str(r["id"]))
            if not v:
                crm.update(r["id"], {"scored": 1, "why": "Claude didn't return a verdict; press Rewrite."})
                continue
            body = (v.get("email_body") or "").strip()
            crm.update(r["id"], {
                "scored": 1, "score": int(v.get("score") or 0),
                "service": v.get("service") if v.get("service") in valid else (r.get("service") or ""),
                "why": v.get("why") or "", "flags": v.get("flags") or [], "contact_via": v.get("contact_via") or "",
                "dm_draft": v.get("dm") or "", "email_subject": v.get("email_subject") or "",
                "email_body": (body + foot) if body else "",
            })
            done += 1
        log("prospects", f"scored {len(rows)}")
        if only:
            return done


def template_pending(only=None):
    """Free: word-rule score + template DM/email for prospects that have none yet (Rewrite asks Claude)."""
    with db() as con:
        if only:
            rows = [dict(r) for r in con.execute(
                f"SELECT * FROM contacts WHERE id IN ({','.join('?' * len(only))})", only)]
        else:
            rows = [dict(r) for r in con.execute("SELECT * FROM contacts WHERE scored=0 AND stage='cold'")]
    for r in rows:
        crm.update(r["id"], free.score_contact(r))
    if rows:
        log("prospects", f"templates for {len(rows)}")
    return len(rows)


def rescore(cid):
    crm.update(cid, {"scored": 0})
    score_pending(only=[cid])


def write_followup(cid):
    c = crm.get(cid)
    out = ai.followup(c, crm.events(cid))
    msg = (out.get("message") or "").strip()
    crm.update(cid, {"followup_draft": msg, **({"email_subject": out["email_subject"]} if out.get("email_subject") else {})})
    return crm.get(cid)


def run(service_id=None):
    """Daily prospect search for one menu or all of them, then scoring."""
    if not busy.acquire(blocking=False):
        return "busy"
    try:
        total = 0
        for sid in ([service_id] if service_id else list(services())):
            set_state("prospect_status", f"finding prospects: {services()[sid].get('name', sid)}")
            total += find(sid)
        set_state("prospect_status", "writing messages for new prospects")
        score_pending() if cfg().get("auto_claude") else template_pending()
        set_state("last_prospects", now())
        set_state("last_prospects_summary", f"{total} new prospects")
        return "ok"
    finally:
        set_state("prospect_status", "idle")
        busy.release()
