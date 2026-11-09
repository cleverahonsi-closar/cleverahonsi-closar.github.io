"""The only AI steps: headless `claude -p` on the user's subscription.
Scoring and drafting run with NO tools at all, so text inside a post or a web page can never make Claude do anything.
Only the two search steps (sweep, find_prospects) get web tools, and they only return data."""
import json
import re
import shutil
import subprocess

from common import PROMPTS, SYSTEM, cfg, proof_text, services

CLAUDE = shutil.which("claude.cmd") or shutil.which("claude") or "claude"


def ask_claude(prompt, tools="", timeout=1800):
    cmd = [CLAUDE, "-p", "--output-format", "json", "--tools", tools]
    model = cfg().get("claude_model")   # a cheaper model than the default uses far less of the subscription
    if model:
        cmd += ["--model", str(model)]
    if tools:  # --tools only makes a tool available; headless mode also needs it allowed
        cmd += ["--allowedTools", tools]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                       timeout=timeout, cwd=SYSTEM)
    if r.returncode != 0:
        raise RuntimeError(f"claude failed ({r.returncode}): {r.stderr[-800:]}")
    out = json.loads(r.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"claude error: {out.get('result')}")
    return out.get("result", "")


def _parse_json(text, opener):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    closer = "]" if opener == "[" else "}"
    start, end = text.find(opener), text.rfind(closer)
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        dec = json.JSONDecoder()
        pattern = r"\[\s*[\{\]]" if opener == "[" else r"\{\s*\""
        for i in [m.start() for m in re.finditer(pattern, text)] or [start]:
            try:
                val, _ = dec.raw_decode(text, i)
                if isinstance(val, list if opener == "[" else dict):
                    return val
            except json.JSONDecodeError:
                continue
        raise


def parse_json_array(text):
    return _parse_json(text, "[")


def parse_json_object(text):
    return _parse_json(text, "{")


def services_text():
    """The three menus as a list for prompts: id: what it is."""
    return "\n".join(f'- "{sid}": {s.get("about") or s.get("name")}' for sid, s in services().items())


def _fill(name, **kw):
    return (PROMPTS / name).read_text(encoding="utf-8").format(services=services_text(), proof=proof_text(), **kw)


def _slim_posts(posts):
    slim = [{k: p.get(k) or "" for k in ("id", "platform", "source", "author", "title", "text", "posted_at", "reply_hint")}
            for p in posts]
    for s in slim:
        s["text"] = s["text"][:2500]
    return json.dumps(slim, ensure_ascii=False, indent=1)


def score(posts):
    """Hunt 1: "need someone" posts -> verdict, score, menu, reply draft."""
    return parse_json_array(ask_claude(_fill("score.md", posts=_slim_posts(posts))))


def answer(posts):
    """Hunt 3: Reddit questions -> answer draft (+ an optional line about your work)."""
    rules = (cfg().get("reddit") or {}).get("promo_rules") or {}
    return parse_json_array(ask_claude(_fill("answer.md", posts=_slim_posts(posts),
                                             promo_rules=json.dumps(rules, ensure_ascii=False))))


def find_prospects(service_id, n, exclude):
    """Hunt 2: web search for people/businesses who fit a menu but haven't posted. Returns raw prospect dicts."""
    s = services()[service_id]
    who = "\n".join(f"- {w}" for w in s.get("prospects") or [])
    prompt = (PROMPTS / "prospect_find.md").read_text(encoding="utf-8").format(
        service=s.get("about") or s.get("name"), who=who, n=n, exclude="\n".join(exclude[:150]) or "(none yet)")
    return parse_json_array(ask_claude(prompt, tools="WebSearch,WebFetch", timeout=2700))


def _slim_contacts(contacts):
    keys = ("id", "service", "source", "name", "handle", "platform", "profile_url", "website", "email", "evidence",
            "evidence_url")
    slim = [{k: c.get(k) or "" for k in keys} for c in contacts]
    for s in slim:
        s["evidence"] = str(s["evidence"])[:2500]
    return json.dumps(slim, ensure_ascii=False, indent=1)


def score_prospects(contacts):
    """Hunt 2: prospects -> fit score, best channel, DM draft, email draft."""
    return parse_json_array(ask_claude(_fill("prospect_score.md", prospects=_slim_contacts(contacts))))


def followup(contact, events):
    """A follow-up message that fits what has happened so far."""
    history = [{"at": e.get("at"), "what": e.get("kind"), "channel": e.get("channel"), "text": (e.get("text") or "")[:1500]}
               for e in events]
    person = json.loads(_slim_contacts([contact]))[0]
    person.update({"stage": contact.get("stage"), "follow_ups_sent": contact.get("follow_ups") or 0,
                   "note": contact.get("note") or ""})
    out = ask_claude(_fill("followup.md", person=json.dumps(person, ensure_ascii=False, indent=1),
                           history=json.dumps(history, ensure_ascii=False, indent=1)))
    return parse_json_object(out)


def sweep():
    """LinkedIn / X / Facebook "need someone" posts through web search (no logins, no scraping)."""
    prompt = (PROMPTS / "sweep.md").read_text(encoding="utf-8").format(
        days=cfg().get("sweep_days", 21), services=services_text(),
        phrases=", ".join(q for s in services().values() for q in (s.get("quick_searches") or [])))
    return parse_json_array(ask_claude(prompt, tools="WebSearch", timeout=2400))
