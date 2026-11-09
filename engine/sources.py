"""Free, open sources. Each fetcher returns a list of post dicts; text from posts is DATA only, never acted on."""
import html
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

from common import cfg, get_state, http_get, http_json, log, services, set_state

ATOM = {"a": "http://www.w3.org/2005/Atom"}


def clean(s, limit=4000):
    s = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(s or "")))
    return re.sub(r"\s+", " ", s).strip()[:limit]


def reddit_dm(author, title):
    if not author:
        return ""
    return "https://www.reddit.com/message/compose/?" + urllib.parse.urlencode({"to": author, "subject": ("Re: " + title)[:100]})


def reddit():
    """Reddit rate-limits anonymous RSS hard: subs are combined 10 per request, a few requests per scan with a pause
    between them, and each scan continues where the last one stopped."""
    out = []
    r = cfg()["reddit"]
    subs = r["subs"]
    chunks = [subs[i:i + 10] for i in range(0, len(subs), 10)]
    turn = int(get_state("reddit_turn", "0"))
    n = min(int(r.get("requests_per_scan", 1)), len(chunks))
    set_state("reddit_turn", str(turn + n))
    for k in range(n):
        chunk = chunks[(turn + k) % len(chunks)]
        if k:
            time.sleep(20)
        try:
            root = ET.fromstring(http_get(f"https://www.reddit.com/r/{'+'.join(chunk)}/new/.rss?limit=100"))
        except Exception as e:
            log("reddit", f"{'+'.join(chunk)}: {e}")
            if "429" in str(e):   # Reddit says slow down: stop here, the next scan picks up from this group
                set_state("reddit_turn", str(turn + k))
                break
            continue
        for e in root.findall("a:entry", ATOM):
            cat = e.find("a:category", ATOM)
            sub = cat.get("term") if cat is not None else "reddit"
            rid = (e.findtext("a:id", "", ATOM) or "").strip()
            link = e.find("a:link", ATOM)
            author = (e.findtext("a:author/a:name", "", ATOM) or "").replace("/u/", "").strip()
            title = clean(e.findtext("a:title", "", ATOM), 300)
            out.append({
                "id": f"reddit:{rid}", "source": f"r/{sub}", "platform": "Reddit",
                "url": link.get("href") if link is not None else "",
                "author": author, "title": title, "text": clean(e.findtext("a:content", "", ATOM)),
                "posted_at": e.findtext("a:published", "", ATOM) or e.findtext("a:updated", "", ATOM),
                "dm_url": reddit_dm(author, title), "reply_hint": "",
            })
    return out


def bluesky():
    out = []
    queries = [q for s in services().values() for q in (s.get("bluesky") or [])]
    for q in queries:
        url = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?" + urllib.parse.urlencode(
            {"q": q, "limit": 25, "sort": "latest"})
        try:
            data = http_json(url)
        except Exception as e:
            log("bluesky", f"{q}: {e}")
            continue
        for p in data.get("posts", []):
            handle = p["author"]["handle"]
            rkey = p["uri"].rsplit("/", 1)[-1]
            rec = p.get("record", {})
            out.append({
                "id": f"bsky:{p['uri']}", "source": "Bluesky", "platform": "Bluesky",
                "url": f"https://bsky.app/profile/{handle}/post/{rkey}",
                "author": handle, "title": "", "text": clean(rec.get("text", "")),
                "posted_at": rec.get("createdAt") or p.get("indexedAt"),
                "dm_url": "", "reply_hint": "Reply under the post, or DM if their DMs are open.",
            })
        time.sleep(1)
    return out


def telegram():
    """Public channels through their web preview (t.me/s/<channel>): no login, no API key."""
    out = []
    for ch in (cfg().get("telegram") or {}).get("channels") or []:
        ch = str(ch).lstrip("@").strip()
        if not ch:
            continue
        try:
            page = http_get(f"https://t.me/s/{urllib.parse.quote(ch)}")
        except Exception as e:
            log("telegram", f"{ch}: {e}")
            continue
        for block in page.split('<div class="tgme_widget_message_wrap')[1:]:
            post = re.search(r'data-post="([^"]+)"', block)
            body = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
            if not post or not body:
                continue
            when = re.search(r'<time datetime="([^"]+)"', block)
            text = clean(body.group(1))
            handles = re.findall(r"(?<![\w.])@([A-Za-z][\w]{4,31})", text)
            out.append({
                "id": f"tg:{post.group(1)}", "source": f"Telegram @{ch}", "platform": "Telegram",
                "url": f"https://t.me/{post.group(1)}", "author": ch, "title": "", "text": text,
                "posted_at": when.group(1) if when else "",
                "dm_url": f"https://t.me/{handles[0]}" if handles else "",
                "reply_hint": "Contact the person or link named in the post (the channel itself usually just reposts).",
            })
        time.sleep(2)
    return out


def hn():
    if not cfg().get("hn"):
        return []
    try:
        s = http_json("https://hn.algolia.com/api/v1/search_by_date?" + urllib.parse.urlencode(
            {"query": "Freelancer? Seeking freelancer?", "tags": "story,author_whoishiring", "hitsPerPage": 1}))
        story = s["hits"][0]
        item = http_json(f"https://hn.algolia.com/api/v1/items/{story['objectID']}")
    except Exception as e:
        log("hn", str(e))
        return []
    out = []
    for c in item.get("children", []):
        text = clean(c.get("text") or "")
        if not text.upper().startswith("SEEKING FREELANCER"):
            continue
        out.append({
            "id": f"hn:{c['id']}", "source": "HN Seeking Freelancer", "platform": "Hacker News",
            "url": f"https://news.ycombinator.com/item?id={c['id']}", "author": c.get("author") or "",
            "title": "", "text": text, "posted_at": c.get("created_at"),
            "dm_url": "", "reply_hint": "HN has no DMs: use the email/contact in their comment.",
        })
    return out


def discourse():
    out = []
    for f in cfg().get("discourse", []):
        base, cat = f["base"], f["category"]
        try:
            data = http_json(f"{base}/c/{cat}.json")
        except Exception as e:
            log("discourse", f"{base} c/{cat}: {e}")
            continue
        users = {u["id"]: u["username"] for u in data.get("users", [])}
        for t in data.get("topic_list", {}).get("topics", []):
            if t.get("pinned"):
                continue
            poster = next((users.get(p["user_id"]) for p in t.get("posters", [])
                           if "Original Poster" in (p.get("description") or "")), "") or ""
            dm = ""
            if poster:
                dm = f"{base}/new-message?" + urllib.parse.urlencode(
                    {"username": poster, "title": ("Re: " + t["title"])[:100]})
            out.append({
                "id": f"disc:{base}:{t['id']}", "source": f["name"], "platform": f["name"],
                "url": f"{base}/t/{t['slug']}/{t['id']}", "author": poster, "title": t["title"],
                "text": clean(t.get("excerpt") or ""), "posted_at": t.get("created_at"),
                "dm_url": dm, "reply_hint": f.get("reply_hint", ""),
            })
        time.sleep(1)
    return out


def fill_discourse_text(post):
    """Topic lists only carry a short excerpt; fetch the first post for ones that pass the gate."""
    if not post["id"].startswith("disc:") or len(post.get("text") or "") > 400:
        return post
    try:
        t = http_json(post["url"] + ".json")
        post["text"] = clean(t["post_stream"]["posts"][0]["cooked"])
    except Exception as e:
        log("discourse", f"topic {post['url']}: {e}")
    return post


ALL = {"reddit": reddit, "bluesky": bluesky, "telegram": telegram, "hn": hn, "discourse": discourse}
