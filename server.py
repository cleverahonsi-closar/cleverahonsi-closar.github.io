"""Closar: the public site and the private studio, on one local server (http://127.0.0.1:8810).

  /           the public site (site/index.html): what visitors see. Only the site/ folder is ever deployed.
  /studio     the private studio: lead hunt, audit + scoring, outreach, pipeline. Served to this computer only.

The studio never sends anything. You copy the message, your email or the post opens in your browser, you press send.
Engine adapted from Gig Radar (posts hunt, CRM, follow-ups) plus the free audit (engine/audit.py)."""
import json
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "engine"))
if sys.stdout is None:  # started with pythonw (no console)
    (ROOT / "data").mkdir(exist_ok=True)
    sys.stdout = sys.stderr = open(ROOT / "data" / "server.log", "a", encoding="utf-8", buffering=1)

import uvicorn  # noqa: E402
import yaml  # noqa: E402
from fastapi import Body, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import audit  # noqa: E402
import crm  # noqa: E402
import prospects  # noqa: E402
import scan  # noqa: E402
from common import STAGES, SYSTEM, areas, cfg, db, due_slot, get_state, log, now, proof, proof_text, services, set_state  # noqa: E402

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
LOCAL = {"127.0.0.1", "::1", "localhost"}


@app.middleware("http")
async def studio_is_private(request: Request, call_next):
    """Everything under /studio answers this computer only, whatever network the server is put on."""
    if request.url.path.startswith("/studio") and (request.client.host if request.client else "") not in LOCAL:
        return Response("Not found", status_code=404)
    return await call_next(request)


# ---------- public site ----------

@app.get("/")
def site():
    return FileResponse(ROOT / "site" / "index.html")


# ---------- studio ----------

@app.get("/studio")
def studio():
    return FileResponse(ROOT / "studio" / "index.html", headers={"Cache-Control": "no-store"})   # always the latest


api = "/studio/api"

POST_VIEWS = {
    "leads": "kind='hire' AND stage='scored' AND verdict='lead' AND outreach='todo'",
    "maybe": "kind='hire' AND stage='scored' AND verdict='maybe' AND outreach='todo'",
    "skipped": "kind='hire' AND ((stage='scored' AND verdict='skip' AND outreach='todo') OR outreach='dismissed')",
}


def _svc(s):
    return s if s in services() else None


def _count(table, where, args=()):
    with db() as con:
        return con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]


def _post(r):
    r = dict(r)
    r["flags"] = json.loads(r["flags"] or "[]")
    return r


def _contact(cid):
    c = crm.get(cid)
    if not c:
        raise HTTPException(404, "Lead not found")
    c["audit"] = json.loads(c.get("audit") or "null")
    return c


@app.get(api + "/state")
def state():
    c = crm.counts()
    min_score = cfg()["prospects"].get("min_score", 6)
    return {
        "services": [{"id": k, "name": v.get("name", k)} for k, v in services().items()],
        "crm": c,
        "counts": {
            "leads": _count("contacts", "stage='cold' AND scored=1 AND score>=?", (min_score,)),
            "to_check": _count("contacts", "stage='cold' AND (scored=0 OR score<?)", (min_score,)),
            "posts": _count("posts", f"({POST_VIEWS['leads']}) AND service IS NOT NULL"),
            "posts_maybe": _count("posts", f"({POST_VIEWS['maybe']}) AND service IS NOT NULL"),
        },
        "status": get_state("scan_status", "idle"), "prospect_status": get_state("prospect_status", "idle"),
        "last_scan": get_state("last_scan"), "last_scan_summary": get_state("last_scan_summary", ""),
        "min_score": min_score, "daily_limit": cfg()["outreach"].get("daily_limit", 25),
        "has_link": any((proof().get(k) or {}).get("links") for k in services()),
    }


@app.get(api + "/areas")
def get_areas():
    return areas()


# ----- audit -----

@app.post(api + "/audit")
def audit_new(body: dict = Body(...)):
    """Audit one or more URLs (one per line). Each becomes a lead with its problems ranked."""
    urls = [u.strip() for u in (body.get("urls") or body.get("url") or "").splitlines() if u.strip()]
    if not urls:
        raise HTTPException(400, "Paste a website or channel link.")
    kind = body.get("kind") if body.get("kind") in ("business", "creator") else None
    def one(u):
        try:
            c, created = audit.add_and_audit(u, kind, body.get("name", "") if len(urls) == 1 else "",
                                             body.get("email", "") if len(urls) == 1 else "")
            c["audit"] = json.loads(c.get("audit") or "null")
            return {"url": u, "created": created, "item": c}
        except Exception as e:
            log("audit", f"{u}: {e}")
            return {"url": u, "error": str(e)}

    with ThreadPoolExecutor(max_workers=4) as pool:   # several links are checked at the same time
        return list(pool.map(one, urls[:20]))


@app.post(api + "/leads/{cid}/signs")
def set_signs(cid: int, body: dict = Body(...)):
    """You ticked or unticked signs (and wrote what you saw). Re-ranks the problems and rewrites the message."""
    c = _contact(cid)
    signs = body.get("signs")
    if not isinstance(signs, list):
        raise HTTPException(400, "signs should be a list")
    kind = c.get("kind") or ("creator" if c.get("service") == "creator" else "business")
    valid = {(a["id"], s["id"]) for a in areas().get(kind, []) for s in a["signs"]}
    clean = [{"area": s["area"], "sign": s["sign"], "auto": bool(s.get("auto")), "evidence": (s.get("evidence") or "").strip()[:240]}
             for s in signs if (s.get("area"), s.get("sign")) in valid]
    audit.apply(cid, signs=clean)
    crm.event(cid, "signs", "", f"{len(clean)} sign(s) after your check")
    return _contact(cid)


@app.post(api + "/leads/{cid}/reaudit")
def reaudit(cid: int):
    c = _contact(cid)
    url = c.get("website") or c.get("profile_url") or c.get("evidence_url")
    if not url:
        raise HTTPException(400, "This lead has no website or channel to check.")
    r = audit.run(url, c.get("kind"))
    manual = [s for s in ((c.get("audit") or {}).get("signs") or []) if not s.get("auto")]
    r["signs"] += manual   # keep what you ticked yourself
    audit.apply(cid, r)
    return _contact(cid)


# ----- leads (the CRM) -----

@app.get(api + "/leads")
def leads(stage: str = "cold", service: str = "", due: int = 0, q: str = ""):
    if stage not in STAGES and stage not in ("pipeline", "all"):
        raise HTTPException(400, "unknown stage")
    rows = crm.listing(None if stage == "all" else stage, _svc(service), bool(due), limit=500)
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in " ".join(str(r.get(k) or "") for k in ("name", "website", "email", "why", "evidence")).lower()]
    for r in rows:
        a = json.loads(r.get("audit") or "null")
        r["ranked"] = (a or {}).get("ranked") or []
        r.pop("audit", None)
    return rows


@app.get(api + "/leads.csv")
def leads_csv():
    return Response(crm.to_csv(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="closar-leads.csv"'})


@app.get(api + "/leads/{cid}")
def lead(cid: int):
    c = _contact(cid)
    c["events"] = crm.events(cid)
    return c


@app.post(api + "/leads/{cid}")
def lead_update(cid: int, body: dict = Body(...)):
    _contact(cid)
    if "stage" in body:
        try:
            crm.set_stage(cid, body.pop("stage"), body.pop("stage_note", ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
    if "value" in body:
        try:
            body["value"] = float(body["value"]) if str(body["value"]).strip() else None
        except ValueError:
            raise HTTPException(400, "The amount should be a number.")
    crm.update(cid, body)
    return lead(cid)


@app.post(api + "/leads/{cid}/sent")
def lead_sent(cid: int, body: dict = Body(...)):
    _contact(cid)
    crm.mark_sent(cid, body.get("channel", ""), body.get("text", ""))
    return lead(cid)


@app.post(api + "/leads/{cid}/note")
def lead_note(cid: int, body: dict = Body(...)):
    _contact(cid)
    crm.event(cid, "note", "", (body.get("text") or "").strip())
    return lead(cid)


@app.post(api + "/leads/{cid}/rewrite")
def lead_rewrite(cid: int):
    _contact(cid)
    try:
        prospects.rescore(cid)
    except Exception as e:
        raise HTTPException(500, f"Claude failed: {e}")
    return lead(cid)


# ----- today -----

@app.get(api + "/today")
def today():
    c = crm.counts()
    o = cfg()["outreach"]
    left = max(int(o.get("daily_limit", 25)) - c["sent_today"], 0)
    min_score = cfg()["prospects"].get("min_score", 6)
    cold = [r for r in crm.listing("cold", limit=300) if r["scored"] and (r["score"] or 0) >= min_score
            and ((r.get("dm_draft") or "").strip() or (r.get("email_body") or "").strip())]
    for r in cold + []:
        r["ranked"] = (json.loads(r.get("audit") or "null") or {}).get("ranked") or []
        r.pop("audit", None)
    due = crm.listing(None, None, due=True, limit=100)
    for r in due:
        r.pop("audit", None)
    return {"due": due, "queue": cold[:left], "queue_total": len(cold), "sent_today": c["sent_today"],
            "daily_limit": o.get("daily_limit", 25), "crm": c}


# ----- posts hunt (people asking for help) -----

@app.get(api + "/posts")
def posts(view: str = "leads", service: str = ""):
    if view not in POST_VIEWS:
        raise HTTPException(400, "unknown view")
    where, args = f"({POST_VIEWS[view]}) AND service IS NOT NULL", ()
    if _svc(service):
        where, args = where + " AND service=?", (service,)
    with db() as con:
        return [_post(r) for r in con.execute(f"SELECT * FROM posts WHERE {where} ORDER BY score DESC NULLS LAST, posted_at DESC LIMIT 200", args)]


def _get_post(pid):
    with db() as con:
        r = con.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404, "post not found")
    return _post(r)


@app.post(api + "/posts/{pid:path}/draft")
def post_draft(pid: str, body: dict = Body(...)):
    with db() as con:
        con.execute("UPDATE posts SET draft=? WHERE id=?", (body.get("draft", ""), pid))
    return {"ok": True}


@app.post(api + "/posts/{pid:path}/status")
def post_status(pid: str, body: dict = Body(...)):
    o = body.get("outreach")
    if o not in ("todo", "sent", "dismissed"):
        raise HTTPException(400, "bad status")
    p = _get_post(pid)
    with db() as con:
        con.execute("UPDATE posts SET outreach=?, sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END WHERE id=?", (o, o, now(), pid))
    out = {"ok": True}
    if o == "sent":
        d = {"service": p.get("service"), "name": p.get("author") or (p.get("title") or "")[:60], "handle": p.get("author"),
             "platform": p.get("platform"), "profile_url": p.get("url"), "dm_url": p.get("dm_url"),
             "evidence": ((p.get("title") or "") + "\n" + (p.get("text") or "")).strip()[:3000], "evidence_url": p.get("url"),
             "dm_draft": body.get("text") or p.get("draft"), "score": p.get("score"), "why": p.get("why")}
        cid, _ = crm.add(d, "post", source_ref=pid)
        crm.mark_sent(cid, p.get("reply_via") or p.get("platform"), body.get("text") or p.get("draft"))
        with db() as con:
            con.execute("UPDATE posts SET contact_id=? WHERE id=?", (cid, pid))
        out["contact_id"] = cid
    return out


@app.post(api + "/posts/{pid:path}/rewrite")
def post_rewrite(pid: str):
    try:
        scan.rescore(pid)
    except Exception as e:
        raise HTTPException(500, f"Claude failed: {e}")
    return _get_post(pid)


# ----- running things -----

@app.post(api + "/scan")
def scan_now():
    threading.Thread(target=scan.run_scan, daemon=True).start()
    return {"ok": True}


@app.post(api + "/prospects")
def prospects_now(body: dict = Body(default={})):
    """Claude web search for new businesses or creators (uses your Claude subscription: a button, never a timer)."""
    if prospects.busy.locked():
        raise HTTPException(409, "A search is already running.")
    threading.Thread(target=prospects.run, args=(_svc(body.get("service", "")),), daemon=True).start()
    return {"ok": True}


@app.get(api + "/settings")
def settings():
    return {"proof": proof_text(), "areas": (SYSTEM / "areas.yaml").read_text(encoding="utf-8"),
            "searches": {k: {"name": v.get("name", k), "quick": v.get("quick_searches") or []} for k, v in services().items()}}


@app.post(api + "/settings/{which}")
def save_settings(which: str, body: dict = Body(...)):
    if which not in ("proof", "areas"):
        raise HTTPException(404, "unknown file")
    text = body.get("text", "")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise HTTPException(400, f"Not saved, there's a formatting mistake: {e}")
    if which == "areas" and not (data.get("business") and data.get("creator")):
        raise HTTPException(400, "Not saved: the business and creator sections are both needed.")
    (SYSTEM / f"{which}.yaml").write_text(text, encoding="utf-8")
    return {"ok": True}


app.mount("/studio/assets", StaticFiles(directory=ROOT / "studio"), name="studio-assets")


def scheduler():
    """Free post scan every N minutes and free audits of new leads. Claude never runs on a timer."""
    time.sleep(5)
    last_scan = last_audit = 0.0
    while True:
        try:
            c = cfg()
            if time.time() - last_scan >= c["scan_every_min"] * 60:
                last_scan = time.time()
                scan.run_scan()
            if time.time() - last_audit >= c.get("audit", {}).get("every_min", 30) * 60:
                last_audit = time.time()
                audit.audit_pending()
        except Exception as e:
            log("scheduler", str(e))
        time.sleep(30)


PORT = int(os.environ.get("CLOSAR_PORT") or cfg()["port"])

if __name__ == "__main__":
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            log("server", f"port {PORT} is already taken (is Closar already running?)")
            sys.exit(0)
    if not os.environ.get("CLOSAR_NO_SCHEDULER"):
        threading.Thread(target=scheduler, daemon=True).start()
    log("server", f"site http://127.0.0.1:{PORT}  ·  studio http://127.0.0.1:{PORT}/studio")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
