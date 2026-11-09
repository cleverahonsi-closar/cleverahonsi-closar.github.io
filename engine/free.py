"""Scoring and drafts with no Claude at all: word rules for the score, fill-in templates (proof.yaml › templates)
for the messages. Claude only runs when you press Rewrite, Write follow-up, Find prospects or the web search."""
import re

from common import cfg, proof, services

MONEY = re.compile(r"(?:[$£€₦]\s?\d[\d,.]*\s?k?|\b\d[\d,.]*\s?(?:usd|dollars|naira|eur|gbp)\b|\b\d+\s?(?:/hr|/hour|per hour|an hour))",
                   re.I)
UNPAID = ["unpaid", "volunteer", "for free", "no budget", "rev share", "revenue share", "exposure", "for credit",
          "portfolio piece", "collab only", "passion project", "can't pay", "cant pay", "no pay"]
FULL_TIME = ["full-time", "full time", "on-site", "onsite", "salary", "401k", "relocat", "in office", "in-office"]


def _hits(text, words):
    found = []
    for w in words or []:
        w = str(w).lower()
        short = len(w) <= 4 and w.isalnum()   # short words must be whole words ("ai" not in "said")
        if (short and re.search(rf"\b{re.escape(w)}\b", text)) or (not short and w in text):
            found.append(w)
    return found


class _Keep(dict):
    def __missing__(self, key):   # an unknown {placeholder} stays as it is instead of crashing
        return "{" + key + "}"


def _snippet(s, n=80):
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"


def _fill(service_id, kind, **extra):
    p = proof()
    t = p.get("templates") or {}
    st = (t.get("services") or {}).get(service_id) or {}
    links = (p.get(service_id) or {}).get("links") or []
    link = next((l.get("url") for l in links if isinstance(l, dict) and l.get("url")), "") or "[ADD LINK]"
    first = (extra.pop("first", "") or "").strip().lstrip("@").split(" ")[0] or "there"
    vals = _Keep(first=first, link=link, name=p.get("name") or "", pitch=st.get("pitch", ""), offer=st.get("offer", ""),
                 topic=st.get("topic", "this"), **extra)
    text = t.get(kind) if kind != "subject" else st.get("email_subject", "")
    if isinstance(text, list):
        return [str(x).format_map(vals).strip() for x in text]
    return re.sub(r"[ \t]+", " ", str(text or "").format_map(vals)).strip()


def score_post(p):
    """Hunt 1 post -> {verdict, score, budget, flags, why, reply_via, draft} from word rules."""
    c = cfg()
    text = ((p.get("title") or "") + " " + (p.get("text") or "")).lower()
    sid = p.get("service") or p.get("guess") or next(iter(services()), "")
    kw = _hits(text, (services().get(sid) or {}).get("keywords"))
    hire = _hits(((p.get("title") or "") + " " + (p.get("text") or "")[:200]).lower(), c.get("hire_words"))   # title + opening only
    money = MONEY.findall(p.get("title", "") + " " + p.get("text", ""))
    unpaid, full = _hits(text, UNPAID), _hits(text, FULL_TIME)
    score, why, flags = 5, [], []
    if hire:
        score += 1
        why.append(f'hiring words ("{hire[0]}")')
    if money:
        score += 2
        why.append(f"mentions pay ({money[0].strip()})")
    else:
        flags.append("no budget mentioned")
    if len(kw) >= 2:
        score += min(2, len(kw) - 1)
    why.append(f"{len(kw)} of your keywords ({', '.join(kw[:3])})" if kw else "matched your menu")
    if unpaid:
        score -= 4
        flags.append(f'looks unpaid ("{unpaid[0]}")')
    if full:
        score -= 2
        flags.append(f'looks like a full-time job ("{full[0]}")')
    off = _hits((p.get("title") or text[:200]).lower(), (services().get(sid) or {}).get("off_words"))
    if off:
        score -= 3
        flags.append(f'may be outside this menu ("{off[0]}")')
    if not hire:   # no one said they're hiring: worth a look at most
        score = min(score, 6)
    score = max(0, min(10, score))
    verdict = "lead" if score >= 7 else "maybe" if score >= 5 else "skip"
    return {"verdict": verdict, "score": score, "budget": money[0].strip() if money else "", "flags": flags,
            "why": "Word rules: " + ", ".join(why) + ". Press Rewrite for a Claude-written reply.",
            "reply_via": "DM" if p.get("dm_url") else "reply under the post",
            "draft": _fill(sid, "reply", first=p.get("author") or "")}


def question(p):
    """Hunt 3: answers need real thought, so no template; Rewrite writes one with Claude."""
    text = ((p.get("title") or "") + " " + (p.get("text") or "")).lower()
    kw = _hits(text, (services().get(p.get("service")) or {}).get("keywords"))
    return {"verdict": "answer", "score": min(10, 4 + len(kw)),
            "why": f"{len(kw)} of your keywords ({', '.join(kw[:3])}). Press Rewrite to have Claude write the answer."}


def score_contact(r):
    """Hunt 2 prospect -> score, why, flags, dm, email (template; Rewrite asks Claude for a personal one)."""
    sid = r.get("service") or next(iter(services()), "")
    ev = r.get("evidence") or ""
    kw = _hits(ev.lower(), (services().get(sid) or {}).get("keywords"))
    score, flags = 5 + min(2, len(kw)), []
    if r.get("source") == "pain":
        score += 1   # they described the problem themselves
        seen = f'your post: "{_snippet(ev.splitlines()[0] if ev else "")}"'
    else:
        seen = "your work"
    if r.get("dm_url") or r.get("email") or r.get("profile_url"):
        score += 1
    else:
        flags.append("no way to contact found")
    if _hits(ev.lower(), UNPAID):
        score -= 3
        flags.append("looks unpaid")
    body = _fill(sid, "dm", first=r.get("handle") or r.get("name") or "", seen=seen)
    foot = (proof().get("email_footer") or "").strip()
    return {"scored": 1, "score": max(0, min(10, score)), "service": sid, "flags": flags,
            "why": f"Word rules: {len(kw)} of your keywords. Template message; press Rewrite for a Claude-written one.",
            "contact_via": "email" if r.get("email") else "DM",
            "dm_draft": body, "email_subject": _fill(sid, "subject") if r.get("email") else "",
            "email_body": (body + ("\n\n" + foot if foot else "")) if r.get("email") else ""}


def followup(c, n):
    """Template for follow-up number n (0 = the first one after your first message), or "" when there's none left."""
    texts = _fill(c.get("service") or "", "followups", first=c.get("handle") or c.get("name") or "")
    return texts[n] if isinstance(texts, list) and n < len(texts) else ""
