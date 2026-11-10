"""The free audit: look at a business or creator from the outside, find signs of a failing system, rank the problems
and write the problem-led message. No Claude, no logins. Page text is DATA only: nothing on a page is ever acted on.

Signs it checks on its own are marked auto in areas.yaml. Everything else you tick in the studio after checking."""
import datetime as dt
import html
import json
import re
import statistics
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import crm
from common import areas, cfg, db, http_get, http_json, log, now, proof

SOCIAL = {
    "instagram": r"instagram\.com/([A-Za-z0-9_.]+)",
    "tiktok": r"tiktok\.com/@([A-Za-z0-9_.]+)",
    "youtube": r"youtube\.com/(@[A-Za-z0-9_.-]+|channel/UC[\w-]{22}|c/[\w-]+|user/[\w-]+)",
    "x": r"(?:twitter|x)\.com/([A-Za-z0-9_]{2,15})(?![\w/])",
    "facebook": r"facebook\.com/([A-Za-z0-9_.-]+)",
    "linkedin": r"linkedin\.com/(company/[\w-]+|in/[\w-]+)",
    "bluesky": r"bsky\.app/profile/([\w.-]+)",
}
SOCIAL_SKIP = {"sharer", "share", "intent", "tr", "plugins", "dialog", "home", "watch", "embed", "p", "reel", "explore"}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
FREE_MAIL = ("gmail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "live.com")
SUBPAGES = ("contact", "book", "booking", "appointment", "faq", "help", "careers", "jobs", "join", "events", "tickets", "shop")

# What each automatic sign says in the first line of a message ("I noticed {evidence}").
EVIDENCE = {
    "no_form": "there's no form or email signup anywhere on your site",
    "no_newsletter": "there's no newsletter or free offer to collect emails on your site",
    "dm_for_info": "the main next step on your site is to message you directly",
    "no_chat": "there's no instant reply or chat on your site",
    "free_email": "enquiries go to a {mail} address",
    "quote_only": "prices on your site are behind 'contact us for a quote'",
    "store_no_cart": "your store doesn't seem to have abandoned-cart emails set up",
    "pay_transfer": "payment is by bank transfer or DM",
    "no_faq": "there's no FAQ page on your site",
    "hiring_admin": "you're hiring for {role} role",
    "event_transfer": "event tickets are sold by bank transfer or DM",
    "event_no_tool": "your events don't use a ticketing system",
    "call_to_book": "booking means calling or messaging you, with no online calendar",
    "no_calendar": "you take appointments but there's no booking link on your site",
    "post_gap": "your last {platform} post was {days} days ago",
    "no_socials": "your site doesn't link to any social media",
    "no_testimonials": "there are no reviews or testimonials on your site",
    "no_referral": "there's no referral or loyalty offer",
    "gap7": "your last {platform} post was {days} days ago",
    "bursty": "your {platform} posts come in bursts with long quiet spells between them",
    "few_posts": "you posted {n} times on {platform} in the last 30 days",
    "views_falling": "views on your recent {platform} videos have been dropping",
}


# ---------- fetching ----------

def _get(url):
    try:
        return http_get(url, timeout=cfg().get("audit", {}).get("timeout", 20))
    except Exception as e:
        log("audit", f"{url}: {e}")
        return ""


def _text(h):
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", h)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h))).strip()


def _norm(url):
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    return url


def _site_pages(url):
    """Homepage plus a few internal pages that matter (contact, booking, FAQ, careers, events, shop)."""
    home = _get(url)
    pages = {url: home}
    if not home:
        return pages
    host = urllib.parse.urlparse(url).netloc
    seen, todo = set(), []
    for href in re.findall(r'href=["\']([^"\'#]+)', home, re.I):
        full = urllib.parse.urljoin(url, href)
        p = urllib.parse.urlparse(full)
        if p.netloc != host or full in pages:
            continue
        slug = p.path.lower().strip("/")
        last = slug.split("/")[-1]
        hit = next((k for k in SUBPAGES if k in last), None) if last else None
        if hit and hit not in seen and len(seen) < 5:
            seen.add(hit)
            todo.append(full)
    with ThreadPoolExecutor(max_workers=5) as pool:   # fetch the sub-pages at the same time
        for full, body in zip(todo, pool.map(_get, todo)):
            pages[full] = body
    return pages


def socials_in(h):
    out = {}
    for name, pat in SOCIAL.items():
        for m in re.finditer(pat, h, re.I):
            handle = m.group(1).rstrip("/")
            if handle.lower().split("/")[0] in SOCIAL_SKIP:
                continue
            out.setdefault(name, m.group(0) if m.group(0).startswith("http") else "https://" + m.group(0))
            break
    return out


# ---------- channels (free public feeds) ----------

def youtube_feed(url):
    """Upload dates + views from the public channel feed (last 15 uploads)."""
    m = re.search(r"channel/(UC[\w-]{22})", url)
    cid = m.group(1) if m else None
    if not cid:
        page = _get(url if "://" in url else "https://" + url)
        m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', page)
             or re.search(r'itemprop="identifier" content="(UC[\w-]{22})"', page)
             or re.search(r'"externalId":"(UC[\w-]{22})"', page))
        cid = m.group(1) if m else None
    if not cid:
        return None
    xml = _get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
    if not xml:
        return None
    ns = {"a": "http://www.w3.org/2005/Atom", "m": "http://search.yahoo.com/mrss/"}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    posts = []
    for e in root.findall("a:entry", ns):
        st = e.find("m:group/m:community/m:statistics", ns)
        posts.append({"at": e.findtext("a:published", "", ns), "views": int(st.get("views", 0)) if st is not None else None})
    return {"platform": "YouTube", "name": root.findtext("a:title", "", ns), "posts": posts}


def bluesky_feed(handle):
    try:
        d = http_json("https://api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?" +
                      urllib.parse.urlencode({"actor": handle, "limit": 50, "filter": "posts_no_replies"}))
    except Exception as e:
        log("audit", f"bluesky {handle}: {e}")
        return None
    posts = [{"at": f["post"]["record"].get("createdAt", ""), "views": None} for f in d.get("feed", [])
             if f["post"]["author"]["handle"] == handle]
    return {"platform": "Bluesky", "name": handle, "posts": posts}


def _dates(posts):
    out = []
    for p in posts:
        try:
            out.append(dt.datetime.fromisoformat(p["at"].replace("Z", "+00:00")))
        except (ValueError, AttributeError):
            pass
    return sorted(out, reverse=True)


def channel_signs(feed, kind):
    """Posting gaps, bursts, volume and falling views from a channel feed."""
    found = []
    if not feed or not feed["posts"]:
        return found, {}
    d = _dates(feed["posts"])
    if not d:
        return found, {}
    today = dt.datetime.now(dt.timezone.utc)
    last = (today - d[0]).days
    month = sum(1 for x in d if (today - x).days <= 30)
    gaps = [(d[i] - d[i + 1]).total_seconds() / 86400 for i in range(len(d) - 1)]
    stats = {"platform": feed["platform"], "last_days": last, "posts_30d": month,
             "median_gap": round(statistics.median(gaps), 1) if gaps else None, "sample": len(d)}
    pf = feed["platform"]
    if kind == "creator":
        if last >= 7:
            found.append(("consistency", "gap7", {"platform": pf, "days": last}))
        if len(gaps) >= 5 and max(gaps) >= 14 and min(gaps) <= 1:
            found.append(("consistency", "bursty", {"platform": pf}))
        if month < 8 and len(d) >= 3:
            found.append(("consistency", "few_posts", {"platform": pf, "n": month}))
        views = [p["views"] for p in feed["posts"] if p.get("views") is not None]
        if len(views) >= 8:
            recent, older = views[:len(views) // 2], views[len(views) // 2:]
            if statistics.median(recent) < 0.6 * statistics.median(older):
                found.append(("consistency", "views_falling", {"platform": pf}))
    elif last >= 21:
        found.append(("content", "post_gap", {"platform": pf, "days": last}))
    return found, stats


# ---------- business site checks ----------

def site_signs(pages):
    allh = " ".join(pages.values())
    low = allh.lower()
    txt = _text(allh).lower()
    found = []

    def has(*words):
        return any(w in low for w in words)

    # Many sites load their forms with JavaScript (form builders, iframes), so "<form" alone misses them.
    has_form = (bool(re.search(r'<input[^>]+type=["\']?email', low)) or "<form" in low
                or has("gform", "wpforms", "wpcf7", "ninja-forms", "elementor-form", "hsforms", "hbspt.forms", "typeform",
                       "jotform", "formstack", "cognitoforms", "formspree", "fluentform", "forminator", "calendly",
                       "acuityscheduling", "msgsndr", "leadconnector", "request a call", "call back", "callback",
                       "contact form", "book now", "book online", "schedule an appointment", "request an appointment"))
    if not has_form:
        found.append(("leads", "no_form", {}))
    if not has("newsletter", "subscribe", "mailchimp", "convertkit", "klaviyo", "mailerlite", "beehiiv", "substack",
               "free guide", "free download", "lead magnet", "join our list", "sign up for", "flodesk"):
        found.append(("leads", "no_newsletter", {}))
    if re.search(r"(dm|message|whatsapp|text|call) (us|me) (for|to get) (more )?(info|details|prices?|pricing)", txt) and not has_form:
        found.append(("leads", "dm_for_info", {}))
    if not has("intercom", "tawk.to", "crisp.chat", "drift.com", "livechat", "tidio", "hs-scripts", "zendesk", "freshchat",
               "chatbot", "msgsndr", "leadconnector", "manychat", "botpress", "voiceflow"):
        found.append(("response", "no_chat", {}))
    mails = sorted({m.lower() for m in EMAIL.findall(_text(allh)) if not m.lower().endswith((".png", ".jpg", ".webp", ".gif", ".svg"))})
    free = next((m for m in mails if m.split("@")[1] in FREE_MAIL), None)
    if free:
        found.append(("response", "free_email", {"mail": free.split("@")[1].split(".")[0].title()}))
    if re.search(r"(contact us for|request a|get a free|ask for a|call for a?) ?(quote|pricing|price)|price on request|pricing on request", txt):
        found.append(("sales", "quote_only", {}))
    store = has("cdn.shopify.com", "woocommerce", "add to cart", "add-to-cart")
    if store and not has("klaviyo", "omnisend", "mailchimp", "drip.com", "privy", "attentive"):
        found.append(("sales", "store_no_cart", {}))
    # Only a sign when transfer is THE way to pay, not one option next to card / payment links.
    other_pay = re.search(r"(credit card|debit card|card on file|pay by card|payment link|stripe|paypal|apple pay|"
                          r"google pay|checkout|square)", txt) or has("cdn.shopify.com", "js.stripe.com", "paypal.com/sdk")
    if re.search(r"(bank transfer|pay (in)?to (this|our|the) account|account number|send proof of payment)", txt) and not other_pay:
        found.append(("sales", "pay_transfer", {}))
    if not re.search(r"\bfaqs?\b|frequently asked|help cent(er|re)", txt):
        found.append(("support", "no_faq", {}))
    role = re.search(r"(virtual assistant|personal assistant|admin(istrative)? assistant|administrator|receptionist|data entry|"
                     r"customer service|customer support|social media (assistant|manager)|office manager)", txt)
    if role and re.search(r"(we'?re hiring|now hiring|join (our|the) team|careers|vacanc|job opening|apply now|position)", txt):
        r = role.group(1)
        found.append(("hiring", "hiring_admin", {"role": ("an " if r[0] in "aeiou" else "a ") + r}))
    no_support = re.sub(r"(support|help|log an?|submit an?|open an?|raise an?|service) tickets?", " ", txt)   # helpdesk, not events
    events = re.search(r"\b(event tickets?|tickets? (on sale|available)|buy tickets?|get (your )?tickets?|rsvp|upcoming events?|"
                       r"register now|early bird)\b", no_support)
    ticket_tool = has("eventbrite", "tix.africa", "ticketmaster", "tito.io", "luma.com", "lu.ma", "dice.fm", "universe.com",
                      "ticketleap", "ticketsource", "zeffy", "humanitix", "paystack.com/pay", "flutterwave", "stripe")
    if events:
        if re.search(r"(bank transfer|send proof of payment|dm (us|me) (for|to buy) tickets?|whatsapp .{0,20}tickets?)", txt):
            found.append(("events", "event_transfer", {}))
        elif not ticket_tool:
            found.append(("events", "event_no_tool", {}))
    calendar = has("calendly", "acuityscheduling", "setmore", "simplybook", "booksy", "fresha", "square.site/book",
                   "squareup.com/appointments", "vagaro", "mindbodyonline", "zcal", "cal.com", "tidycal", "msgsndr",
                   "leadconnector", "/booking", "book online", "book now", "book-now", "/book\"", "/book/", "halaxy",
                   "cliniko", "coreplus", "powerdiary", "nookal", "janeapp", "jane.app", "opentable", "resy.com",
                   "exploretock", "simplepractice", "therapyportal", "zocdoc", "doctolib", "healthengine", "hotdoc")
    appts = re.search(r"\b(appointments?|consultations?|book (a|your) (session|call|visit|table)|reservations?)\b", txt)
    if re.search(r"(call|whatsapp|text|dm) (us )?to (book|reserve|schedule)", txt) and not calendar:
        found.append(("booking", "call_to_book", {}))
    elif appts and not calendar:
        found.append(("booking", "no_calendar", {}))
    soc = socials_in(allh)
    if not soc:
        found.append(("content", "no_socials", {}))
    if not re.search(r"testimonial|reviews?\b|what (our )?(clients|customers) say|trustpilot|google reviews|★|stars", txt):
        found.append(("reviews", "no_testimonials", {}))
    if not re.search(r"referr|refer a friend|loyalty|rewards? (program|club)", txt):
        found.append(("winback", "no_referral", {}))
    return found, {"emails": mails[:5], "socials": soc}


def _name_of(h, url):
    m = re.search(r'property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)', h, re.I) or \
        re.search(r"<title[^>]*>([^<]+)", h, re.I)
    name = html.unescape(m.group(1)).strip() if m else ""
    name = re.split(r"\s[|\-–—:]\s", name)[0].strip() if name else ""
    return name[:80] or urllib.parse.urlparse(url).netloc.removeprefix("www.")


# ---------- the audit ----------

CHANNEL_HOSTS = ("youtube.com", "tiktok.com", "instagram.com", "bsky.app", "twitch.tv")
# Profiles on these sites are not the lead's own website: auditing them scores the platform's page, not the lead.
NOT_SITE_HOSTS = ("reddit.com", "redd.it", "t.me", "telegram.", "x.com", "twitter.com", "facebook.com", "fb.com",
                  "linkedin.com", "threads.net", "medium.com", "substack.com", "github.com", "imgur.com", "discord.",
                  "upwork.com", "fiverr.com", "freelancer.com", "onlinejobs.ph", "jobberman.com", "indeed.com",
                  "glassdoor.", "psychologytoday.com", "yelp.com", "google.com", "goo.gl", "linktr.ee", "wa.me",
                  "whatsapp.com", "eventbrite.", "wikipedia.org")


def _host(url):
    return urllib.parse.urlparse(_norm(url)).netloc.lower().removeprefix("www.")


def _on(host, hosts):
    return any(host == h or host.endswith("." + h) or (h.endswith(".") and h in host) for h in hosts)


def is_channel(url):
    return bool(url) and _on(_host(url), CHANNEL_HOSTS)


def is_own_site(url):
    """True for a lead's own website (not a social/forum/job-board/directory profile)."""
    return bool(url) and bool(_host(url)) and not _on(_host(url), CHANNEL_HOSTS + NOT_SITE_HOSTS)


def tld_ok(url):
    """False when the site sits on a country domain outside the target countries (e.g. .ng, .in, .com.ph)."""
    last = _host(url).rsplit(".", 1)[-1]
    if len(last) != 2:   # .com, .org, .io ... say nothing about country
        return True
    return last in (cfg().get("country_tlds") or ["us", "uk", "ca", "au"]) or last in ("io", "co", "ai", "me", "tv")


def auditable(url):
    return is_own_site(url) or is_channel(url)


def kind_of(url):
    return "creator" if is_channel(url) else "business"


def run(url, kind=None):
    """Audit a website or channel. Returns {kind, name, website, socials, emails, signs, stats, notes}."""
    url = _norm(url)
    kind = kind or kind_of(url)
    found, stats, notes, info, name, closed = [], [], [], {"emails": [], "socials": {}}, "", []
    host = urllib.parse.urlparse(url).netloc.lower()
    if not auditable(url):
        return {"kind": kind, "name": "", "website": "", "profile_url": "", "socials": {}, "emails": [], "signs": [],
                "stats": [], "notes": [f"{_host(url)} is a profile on someone else's platform, not their own site or"
                                       " channel, so there is nothing to audit. Add their real website."]}
    if kind == "business" and not is_channel(url):
        pages = _site_pages(url)
        home = pages.get(url, "")
        if not home:
            notes.append("The site didn't load (blocked or offline). Check it yourself and tick what you see.")
        else:
            name = _name_of(home, url)
            f, info = site_signs(pages)
            found += f
            notes.append(f"Checked {len([p for p in pages.values() if p])} page(s) on the site.")
        channels = info["socials"]
    else:
        channels = {next((k for k in SOCIAL if re.search(SOCIAL[k], url, re.I)), "web"): url}
    for platform, link in channels.items():
        feed = None
        if platform == "youtube":
            feed = youtube_feed(link)
        elif platform == "bluesky":
            feed = bluesky_feed(re.search(SOCIAL["bluesky"], link).group(1))
        elif platform in ("instagram", "tiktok", "facebook", "x", "linkedin"):
            closed.append({"x": "X", "tiktok": "TikTok", "linkedin": "LinkedIn"}.get(platform, platform.title()))
        if feed:
            f, st = channel_signs(feed, kind)
            found += f
            stats.append(st)
            name = name or feed.get("name", "")
    if closed:
        names = ", ".join(closed[:-1]) + (" and " if len(closed) > 1 else "") + closed[-1]
        notes.append(f"{names} can't be read without logging in. Open {'them' if len(closed) > 1 else 'it'} from the links above and tick what you see.")
    if kind == "creator" and not stats:
        notes.append("No public feed to measure posting from. Open the profile and tick the signs you see.")
    signs = [{"area": a, "sign": s, "auto": True, "evidence": EVIDENCE.get(s, "").format_map(_Keep(v))} for a, s, v in found]
    return {"kind": kind, "name": name, "website": url if kind == "business" else "", "profile_url": url if kind == "creator" else "",
            "socials": channels, "emails": info.get("emails", []), "signs": signs, "stats": stats, "notes": notes}


class _Keep(dict):
    def __missing__(self, k):
        return "{" + k + "}"


# ---------- ranking + message ----------

def _area_map(kind):
    return {a["id"]: a for a in (areas().get(kind) or [])}


def rank(kind, signs):
    """Areas with at least one sign, worst (most costly) first: [{area, name, points, signs, setup, monthly, fix}]."""
    amap = _area_map(kind)
    by = {}
    for s in signs:
        a = amap.get(s["area"])
        if not a:
            continue
        strength = next((x.get("strength", 1) for x in a["signs"] if x["id"] == s["sign"]), 1)
        r = by.setdefault(a["id"], {"area": a["id"], "n": a.get("n"), "name": a["name"], "points": 0.0, "signs": [],
                                    "fix": a.get("fix"), "setup": a.get("setup"), "monthly": a.get("monthly")})
        r["points"] += strength * float(a.get("weight", 1))
        r["signs"].append(s)
    out = sorted(by.values(), key=lambda r: -r["points"])
    for r in out:
        r["points"] = round(r["points"], 1)
    return out


def score(ranked, has_contact):
    total = sum(r["points"] for r in ranked[:4])
    return max(0, min(10, round(1 + total * 0.7) + (1 if has_contact else -1))) if ranked else 0


def _first(name):
    n = (name or "").strip().lstrip("@").split(" ")[0]
    return n if n and n.isalpha() and n[0].isupper() and len(n) < 20 else "there"


def message(c, kind, ranked):
    """Main problem first (hook, cost, fix, proof link), smaller ones under 'I also noticed', then one question."""
    if not ranked:
        return "", ""
    amap = _area_map(kind)
    main = ranked[0]
    a = amap[main["area"]]
    ev = next((s.get("evidence") for s in main["signs"] if s.get("evidence")), "") or a["signs"][0]["label"].lower()
    links = (proof().get(kind) or {}).get("links") or []
    link = next((l.get("url") for l in links if isinstance(l, dict) and l.get("url")), "") or "[ADD LINK]"
    if "cleverahonsi-closar.github.io" in link and "#" not in link:   # landing page; ?fix= pre-picks their problem in the audit form
        link = link.rstrip("/") + "/" + ("" if kind == "creator" else f"?fix={main['area']}")
    if c.get("handle") or kind == "creator":
        hi = _first(c.get("handle") or c.get("name"))
    else:   # a business found by its site: greet the team, not the brand as if it were a person
        hi = f"{c['name']} team" if c.get("name") else "there"
    lines = [f"Hi {hi},", "",
             a["hook"].format(evidence=ev) + " " + a["cost"], "",
             a["fix_line"] + f" Here's one I've built: {link}"]
    # Say what was actually seen (the sign's evidence), never the area's generic line, which can claim unseen things.
    also = [next((s["evidence"] for s in r["signs"] if s.get("evidence")), "") or amap[r["area"]]["also"]
            for r in ranked[1:4]]
    if also:
        lines += ["", "I also noticed:"] + [f"- {x}" for x in also]
    lines += ["", "Want me to show you how the first one would work for you?", "", "Clever"]
    return "\n".join(lines), a.get("subject", "")


def apply(cid, result=None, signs=None):
    """Save an audit (or an edited set of signs) on a contact: score, main problem, drafts."""
    c = crm.get(cid)
    kind = c.get("kind") or ("creator" if c.get("service") == "creator" else "business")
    old = json.loads(c.get("audit") or "{}")
    data = result or old
    if signs is not None:
        data["signs"] = signs
    ranked = rank(kind, data.get("signs") or [])
    data["ranked"] = ranked
    fields = {"scored": 1, "score": score(ranked, bool(c.get("email") or c.get("dm_url") or c.get("profile_url"))),
              "why": (f"Main problem: {ranked[0]['name']}. " + (f"Also: {', '.join(r['name'] for r in ranked[1:4])}." if len(ranked) > 1 else ""))
              if ranked else "No signs found yet. Check them yourself and tick what you see."}
    body, subject = message(c, kind, ranked)
    if body:
        foot = (proof().get("email_footer") or "").strip()
        fields.update({"dm_draft": body, "email_subject": subject,
                       "email_body": body + ("\n\n" + foot if foot else ""),
                       "contact_via": "email" if c.get("email") else "DM"})
    crm.update(cid, fields)
    with db() as con:
        con.execute("UPDATE contacts SET audit=?, main_area=?, audited_at=?, kind=? WHERE id=?",
                    (json.dumps(data), ranked[0]["area"] if ranked else None, now(), kind, cid))
    return crm.get(cid)


def add_and_audit(url, kind=None, name="", email=""):
    """New lead from a URL: audit it, create the contact (or update the existing one) and score it."""
    r = run(url, kind)
    d = {"service": r["kind"], "name": name or r["name"], "website": r["website"], "profile_url": r["profile_url"],
         "email": email or (r["emails"][0] if r["emails"] else ""),
         "evidence": "; ".join(s["evidence"] for s in r["signs"][:6]), "evidence_url": url}
    cid, created = crm.add(d, "audit", url)
    with db() as con:
        con.execute("UPDATE contacts SET kind=? WHERE id=?", (r["kind"], cid))
    crm.event(cid, "audited", "", f"{len(r['signs'])} sign(s) found automatically")
    return apply(cid, r), created


def audit_pending(limit=None):
    """Background: audit contacts that have a website or channel but no audit yet (free)."""
    limit = limit or cfg().get("audit", {}).get("per_run", 15)
    with db() as con:
        rows = con.execute("SELECT id, website, profile_url, service FROM contacts WHERE audit IS NULL AND stage='cold'"
                           " AND (COALESCE(website,'')<>'' OR COALESCE(profile_url,'')<>'') LIMIT ?", (limit,)).fetchall()
    for r in rows:
        url = next((u for u in (r["website"], r["profile_url"]) if auditable(u or "")), None)
        if not url:   # only a forum/social/job-board profile: no own site to judge, so no score
            apply(r["id"], run(r["website"] or r["profile_url"]))
            continue
        try:
            apply(r["id"], run(url, "creator" if r["service"] == "creator" else None))
        except Exception as e:
            log("audit", f"contact {r['id']}: {e}")
            with db() as con:
                con.execute("UPDATE contacts SET audit=? WHERE id=?", (json.dumps({"signs": [], "notes": [f"Audit failed: {e}"]}), r["id"]))
    return len(rows)
