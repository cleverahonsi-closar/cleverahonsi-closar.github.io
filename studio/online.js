/* Closar studio, online edition (GitHub Pages + Supabase).
   The studio page normally talks to the Python server on the laptop (/studio/api/...). When it is opened
   anywhere else, call() hands each request to Online.call(), which does the same work straight on the
   Supabase database: view and edit leads, notes, stages, posts. Scans, Claude and new audits stay on the laptop.
   Mirrors server.py, engine/crm.py, engine/audit.py (rank/score/message/apply) and engine/free.py (followup). */
(function () {
  const LOCAL = ["127.0.0.1", "localhost", "::1", "[::1]"];
  const ONLINE = !LOCAL.includes(location.hostname);
  window.CLOSAR_ONLINE = ONLINE;
  if (!ONLINE) return;

  const LAPTOP = "This runs on your laptop studio only (http://127.0.0.1:8810/studio).";
  const STAGES = ["cold", "messaged", "replied", "talks", "paid", "lost", "skip"];
  const FIELDS = ["service", "name", "handle", "platform", "profile_url", "website", "email", "evidence", "evidence_url",
    "contact_via", "dm_url", "dm_draft", "email_subject", "email_body", "followup_draft", "value", "currency",
    "note", "next_follow_up", "score", "why"];
  let sb = null, CFG = {}, AREAS = {}, PROOF = {}, TEXT = {};

  /* ---------- small helpers (same formats as the Python side) ---------- */
  const pad = n => String(n).padStart(2, "0");
  const now = () => new Date().toISOString().slice(0, 19) + "+00:00";
  const isoDay = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const today = () => isoDay(new Date());
  const daysFromToday = n => { const d = new Date(); d.setDate(d.getDate() + Number(n)); return isoDay(d) };
  const dayStartUtc = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d.toISOString().slice(0, 19) + "+00:00" };
  const services = () => CFG.services || {};
  const svc = s => (s && services()[s]) ? s : null;
  const json = (s, d) => { try { return s ? JSON.parse(s) : d } catch { return d } };
  const pyRound = x => { const f = Math.floor(x), r = x - f; return Math.abs(r - 0.5) < 1e-9 ? (f % 2 === 0 ? f : f + 1) : Math.round(x) };
  const round1 = x => pyRound(x * 10) / 10;
  function err(message, status) { const e = new Error(message); e.status = status; return e }
  async function q(builder) { const { data, error, count } = await builder; if (error) throw err(error.message); return count ?? data }
  const fill = (text, vals) => String(text ?? "").replace(/\{\{|\}\}|\{(\w+)\}/g, (m, k) => m === "{{" ? "{" : m === "}}" ? "}" : (k in vals ? vals[k] : m));

  /* ---------- crm.py ---------- */
  function normUrl(u) {
    u = (u || "").trim().toLowerCase();
    if (!u) return "";
    try {
      const p = new URL(u.includes("://") ? u : "https://" + u);
      let host = p.host.replace(/^www\./, "").replace(/^m\./, "");
      return host + (host ? p.pathname.replace(/\/+$/, "") : "");
    } catch { return u }
  }
  function dedupeKey(d) {
    if (d.email) return "email:" + d.email.trim().toLowerCase();
    if (d.profile_url) return "url:" + normUrl(d.profile_url);
    if (d.website) return "site:" + normUrl(d.website).split("/")[0];
    if (d.handle && d.platform) return `handle:${d.platform.toLowerCase()}:${d.handle.replace(/^@+/, "").toLowerCase()}`;
    if (d.source_ref) return "ref:" + d.source_ref;
    return "name:" + (d.name || "").toLowerCase().replace(/[\W_]+/gu, "");
  }
  const row = r => r ? { ...r, flags: json(r.flags, []) } : null;
  async function get(cid) { return row((await q(sb.from("contacts").select("*").eq("id", cid).limit(1)))[0]) }
  async function update(cid, fields) {
    const f = {};
    for (const [k, v] of Object.entries(fields)) if (FIELDS.includes(k) || ["flags", "scored", "stage"].includes(k)) f[k] = v;
    if ("flags" in f && typeof f.flags !== "string") f.flags = JSON.stringify(f.flags || []);
    if (!Object.keys(f).length) return;
    f.updated_at = now();
    await q(sb.from("contacts").update(f).eq("id", cid));
  }
  const event = (cid, kind, channel = "", text = "") =>
    q(sb.from("events").insert({ contact_id: cid, at: now(), kind, channel: channel || "", text: text || "" }));
  const events = cid => q(sb.from("events").select("*").eq("contact_id", cid).order("id"));
  async function add(d, source, sourceRef = "", stage = "cold") {
    d = Object.fromEntries(Object.entries(d).filter(([, v]) => v !== null && v !== undefined && v !== ""));
    d.source_ref = sourceRef || d.source_ref || "";
    const key = dedupeKey(d);
    const old = await q(sb.from("contacts").select("id").eq("dedupe_key", key).limit(1));
    if (old.length) return [old[0].id, false];
    const rec = { source, source_ref: d.source_ref, dedupe_key: key, stage, created_at: now(), updated_at: now(), flags: JSON.stringify(d.flags || []) };
    for (const c of FIELDS) if (c in d) rec[c] = d[c];
    const made = await q(sb.from("contacts").insert(rec).select("id").single());
    await event(made.id, "found", source, (d.evidence || "").slice(0, 1500));
    return [made.id, true];
  }
  function followup(c, n) {   // free.py: template follow-up number n
    const t = PROOF.templates || {}, st = ((t.services || {})[c.service || ""]) || {};
    const links = ((PROOF[c.service || ""] || {}).links) || [];
    const link = (links.find(l => l && typeof l === "object" && l.url) || {}).url || "[ADD LINK]";
    const first = String(c.handle || c.name || "").trim().replace(/^@+/, "").split(" ")[0] || "there";
    const vals = { first, link, name: PROOF.name || "", pitch: st.pitch || "", offer: st.offer || "", topic: st.topic || "this" };
    const texts = t.followups;
    return Array.isArray(texts) && n < texts.length ? fill(texts[n], vals).trim() : "";
  }
  async function markSent(cid, channel, text) {
    const c = await get(cid), o = CFG.outreach || {}, days = o.follow_up_days || [3, 7, 14];
    let n, kind, stage, nxt;
    if (["cold", "skip", "lost"].includes(c.stage) || !c.last_contact_at) {
      n = 0; kind = "messaged"; stage = "messaged"; nxt = days.length ? daysFromToday(days[0]) : null;
    } else if (c.stage === "messaged") {
      n = (c.follow_ups || 0) + 1; kind = "followed_up"; stage = "messaged"; nxt = n < days.length ? daysFromToday(days[n]) : null;
    } else {
      n = c.follow_ups || 0; kind = "followed_up"; stage = c.stage;
      nxt = ["replied", "talks"].includes(c.stage) ? daysFromToday(o.talks_follow_up_days ?? 2) : null;
    }
    const fu = nxt && stage === "messaged" ? followup(c, n) : "";
    await q(sb.from("contacts").update({ stage, follow_ups: n, next_follow_up: nxt, last_contact_at: now(), followup_draft: fu, updated_at: now() }).eq("id", cid));
    await event(cid, kind, channel, text);
  }
  async function setStage(cid, stage, note = "") {
    if (!STAGES.includes(stage)) throw err("unknown stage", 400);
    const o = CFG.outreach || {};
    const f = { stage, updated_at: now() };
    if (["replied", "talks", "paid", "lost", "skip", "cold"].includes(stage))
      f.next_follow_up = ["replied", "talks"].includes(stage) ? daysFromToday(o.talks_follow_up_days ?? 2) : null;
    await q(sb.from("contacts").update(f).eq("id", cid));
    await event(cid, stage, "", note);
  }
  async function listing(stage = null, service = null, due = false, limit = 300) {
    let b = sb.from("contacts").select("*");
    if (stage === "pipeline") b = b.in("stage", ["messaged", "replied", "talks", "paid", "lost"]);
    else if (stage) b = b.eq("stage", stage);
    if (service) b = b.eq("service", service);
    if (due) b = b.in("stage", ["messaged", "replied", "talks"]).not("next_follow_up", "is", null).lte("next_follow_up", today());
    if (due) b = b.order("next_follow_up", { ascending: true });
    else if (stage === "cold") b = b.order("score", { ascending: false, nullsFirst: false }).order("created_at", { ascending: false });
    else b = b.order("updated_at", { ascending: false });
    return (await q(b.limit(limit))).map(row);
  }
  async function counts() {
    const all = await q(sb.from("contacts").select("id,stage,next_follow_up,currency,value"));
    const stages = {}, paid = {};
    let due = 0;
    const t = today();
    for (const c of all) {
      stages[c.stage] = (stages[c.stage] || 0) + 1;
      if (["messaged", "replied", "talks"].includes(c.stage) && c.next_follow_up && c.next_follow_up <= t) due++;
      if (c.stage === "paid" && c.value !== null) paid[c.currency || "USD"] = (paid[c.currency || "USD"] || 0) + c.value;
    }
    const ids = new Set(all.map(c => c.id));
    const sent = await q(sb.from("events").select("contact_id").in("kind", ["messaged", "followed_up"]).gte("at", dayStartUtc()));
    return { stages, due, sent_today: sent.filter(e => ids.has(e.contact_id)).length, paid_total: paid };
  }
  const CSV_COLS = ["id", "service", "stage", "name", "handle", "platform", "profile_url", "website", "email", "score", "why",
    "contact_via", "source", "value", "currency", "next_follow_up", "follow_ups", "last_contact_at", "note", "evidence_url", "created_at"];
  async function toCsv() {
    const rows = await listing(null, null, false, 100000);
    const cell = v => { const s = v === null || v === undefined ? "" : String(v); return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s };
    return "﻿" + [CSV_COLS, ...rows.map(r => CSV_COLS.map(k => r[k]))].map(r => r.map(cell).join(",")).join("\r\n") + "\r\n";
  }

  /* ---------- audit.py: rank, score, message, apply ---------- */
  const areaMap = kind => Object.fromEntries((AREAS[kind] || []).map(a => [a.id, a]));
  function rank(kind, signs) {
    const amap = areaMap(kind), by = {};
    for (const s of signs) {
      const a = amap[s.area];
      if (!a) continue;
      const strength = ((a.signs || []).find(x => x.id === s.sign) || {}).strength ?? 1;
      const r = by[a.id] ||= { area: a.id, n: a.n ?? null, name: a.name, points: 0, signs: [], fix: a.fix ?? null, setup: a.setup ?? null, monthly: a.monthly ?? null };
      r.points += strength * Number(a.weight ?? 1);
      r.signs.push(s);
    }
    const out = Object.values(by).sort((x, y) => y.points - x.points);
    for (const r of out) r.points = round1(r.points);
    return out;
  }
  function score(ranked, hasContact) {
    if (!ranked.length) return 0;
    const total = ranked.slice(0, 4).reduce((s, r) => s + r.points, 0);
    return Math.max(0, Math.min(10, pyRound(1 + total * 0.7) + (hasContact ? 1 : -1)));
  }
  function firstName(name) {
    const n = String(name || "").trim().replace(/^@+/, "").split(" ")[0];
    return n && /^\p{L}+$/u.test(n) && n[0] === n[0].toUpperCase() && n[0] !== n[0].toLowerCase() && n.length < 20 ? n : "there";
  }
  function message(c, kind, ranked) {
    if (!ranked.length) return ["", ""];
    const amap = areaMap(kind), main = ranked[0], a = amap[main.area];
    const ev = (main.signs.find(s => s.evidence) || {}).evidence || String(a.signs[0].label).toLowerCase();
    const links = ((PROOF[kind] || {}).links) || [];
    const link = (links.find(l => l && typeof l === "object" && l.url) || {}).url || "[ADD LINK]";
    const hi = (c.handle || kind === "creator") ? firstName(c.handle || c.name) : (c.name ? `${c.name} team` : "there");
    const lines = [`Hi ${hi},`, "", String(a.hook).replace(/\{evidence\}/g, ev) + " " + a.cost, "", a.fix_line + ` Here's one I've built: ${link}`];
    const also = ranked.slice(1, 4).map(r => amap[r.area].also);
    if (also.length) lines.push("", "I also noticed:", ...also.map(x => `- ${x}`));
    lines.push("", "Want me to show you how the first one would work for you?", "", "Clever");
    return [lines.join("\n"), a.subject || ""];
  }
  async function applyAudit(cid, result = null, signs = null) {
    const c = await get(cid);
    const kind = c.kind || (c.service === "creator" ? "creator" : "business");
    const data = result || json(c.audit, {}) || {};
    if (signs !== null) data.signs = signs;
    const ranked = rank(kind, data.signs || []);
    data.ranked = ranked;
    const fields = {
      scored: 1, score: score(ranked, !!(c.email || c.dm_url || c.profile_url)),
      why: ranked.length ? `Main problem: ${ranked[0].name}. ` + (ranked.length > 1 ? `Also: ${ranked.slice(1, 4).map(r => r.name).join(", ")}.` : "")
        : "No signs found yet. Check them yourself and tick what you see."
    };
    const [body, subject] = message(c, kind, ranked);
    if (body) {
      const foot = String(PROOF.email_footer || "").trim();
      Object.assign(fields, { dm_draft: body, email_subject: subject, email_body: body + (foot ? "\n\n" + foot : ""), contact_via: c.email ? "email" : "DM" });
    }
    await update(cid, fields);
    await q(sb.from("contacts").update({ audit: JSON.stringify(data), main_area: ranked.length ? ranked[0].area : null, audited_at: now(), kind }).eq("id", cid));
  }

  /* ---------- server.py endpoints ---------- */
  const POST_VIEWS = {
    leads: b => b.eq("kind", "hire").eq("stage", "scored").eq("verdict", "lead").eq("outreach", "todo"),
    maybe: b => b.eq("kind", "hire").eq("stage", "scored").eq("verdict", "maybe").eq("outreach", "todo"),
    skipped: b => b.eq("kind", "hire").or("and(stage.eq.scored,verdict.eq.skip,outreach.eq.todo),outreach.eq.dismissed"),
  };
  const postCount = view => q(POST_VIEWS[view](sb.from("posts").select("id", { count: "exact", head: true })).not("service", "is", null));
  const getState = async (k, d = null) => ((await q(sb.from("state").select("v").eq("k", k).limit(1)))[0] || {}).v ?? d;
  const minScore = () => (CFG.prospects || {}).min_score ?? 6;
  async function contact(cid) {
    const c = await get(cid);
    if (!c) throw err("Lead not found", 404);
    c.audit = json(c.audit, null);
    return c;
  }
  async function lead(cid) { const c = await contact(cid); c.events = await events(cid); return c }
  const post = r => ({ ...r, flags: json(r.flags, []) });

  async function apiState() {
    const ms = minScore();
    const [crm, leads, toCheck, posts, postsMaybe, status, pstatus, last, summary] = await Promise.all([
      counts(),
      q(sb.from("contacts").select("id", { count: "exact", head: true }).eq("stage", "cold").eq("scored", 1).gte("score", ms)),
      q(sb.from("contacts").select("id", { count: "exact", head: true }).eq("stage", "cold").or(`scored.eq.0,score.lt.${ms}`)),
      postCount("leads"), postCount("maybe"),
      getState("scan_status", "idle"), getState("prospect_status", "idle"), getState("last_scan"), getState("last_scan_summary", ""),
    ]);
    return {
      services: Object.entries(services()).map(([id, v]) => ({ id, name: v.name || id })), crm,
      counts: { leads, to_check: toCheck, posts, posts_maybe: postsMaybe },
      status, prospect_status: pstatus, last_scan: last, last_scan_summary: summary,
      min_score: ms, daily_limit: (CFG.outreach || {}).daily_limit ?? 25,
      has_link: Object.keys(services()).some(k => ((PROOF[k] || {}).links || []).length), online: true,
    };
  }
  async function apiToday() {
    const c = await counts(), o = CFG.outreach || {}, limit = o.daily_limit ?? 25;
    const left = Math.max(parseInt(limit) - c.sent_today, 0), ms = minScore();
    const cold = (await listing("cold", null, false, 300)).filter(r => r.scored && (r.score || 0) >= ms
      && ((r.dm_draft || "").trim() || (r.email_body || "").trim()));
    for (const r of cold) { r.ranked = (json(r.audit, null) || {}).ranked || []; delete r.audit }
    const due = await listing(null, null, true, 100);
    for (const r of due) delete r.audit;
    return { due, queue: cold.slice(0, left), queue_total: cold.length, sent_today: c.sent_today, daily_limit: limit, crm: c };
  }
  async function apiLeads(p) {
    const stage = p.get("stage") || "cold", q_ = (p.get("q") || "").toLowerCase();
    if (!STAGES.includes(stage) && !["pipeline", "all"].includes(stage)) throw err("unknown stage", 400);
    let rows = await listing(stage === "all" ? null : stage, svc(p.get("service")), !!Number(p.get("due") || 0), 500);
    if (q_) rows = rows.filter(r => ["name", "website", "email", "why", "evidence"].map(k => String(r[k] ?? "")).join(" ").toLowerCase().includes(q_));
    for (const r of rows) { r.ranked = (json(r.audit, null) || {}).ranked || []; delete r.audit }
    return rows;
  }
  async function apiLeadUpdate(cid, body) {
    await contact(cid);
    body = { ...body };
    if ("stage" in body) { const s = body.stage, n = body.stage_note || ""; delete body.stage; delete body.stage_note; await setStage(cid, s, n) }
    if ("value" in body) {
      const v = String(body.value ?? "").trim();
      if (v && isNaN(Number(v))) throw err("The amount should be a number.", 400);
      body.value = v ? Number(v) : null;
    }
    await update(cid, body);
    return lead(cid);
  }
  async function apiSigns(cid, body) {
    const c = await contact(cid);
    if (!Array.isArray(body.signs)) throw err("signs should be a list", 400);
    const kind = c.kind || (c.service === "creator" ? "creator" : "business");
    const valid = new Set((AREAS[kind] || []).flatMap(a => (a.signs || []).map(s => a.id + "\u0000" + s.id)));
    const clean = body.signs.filter(s => valid.has(s.area + "\u0000" + s.sign))
      .map(s => ({ area: s.area, sign: s.sign, auto: !!s.auto, evidence: String(s.evidence || "").trim().slice(0, 240) }));
    await applyAudit(cid, null, clean);
    await event(cid, "signs", "", `${clean.length} sign(s) after your check`);
    return contact(cid);
  }
  async function apiPosts(p) {
    const view = p.get("view") || "leads";
    if (!POST_VIEWS[view]) throw err("unknown view", 400);
    let b = POST_VIEWS[view](sb.from("posts").select("*")).not("service", "is", null);
    if (svc(p.get("service"))) b = b.eq("service", p.get("service"));
    b = b.order("score", { ascending: false, nullsFirst: false }).order("posted_at", { ascending: false }).limit(200);
    return (await q(b)).map(post);
  }
  async function apiPostStatus(pid, body) {
    const o = body.outreach;
    if (!["todo", "sent", "dismissed"].includes(o)) throw err("bad status", 400);
    const p = (await q(sb.from("posts").select("*").eq("id", pid).limit(1)))[0];
    if (!p) throw err("post not found", 404);
    await q(sb.from("posts").update(o === "sent" ? { outreach: o, sent_at: now() } : { outreach: o }).eq("id", pid));
    const out = { ok: true };
    if (o === "sent") {
      const d = { service: p.service, name: p.author || (p.title || "").slice(0, 60), handle: p.author, platform: p.platform,
        profile_url: p.url, dm_url: p.dm_url, evidence: ((p.title || "") + "\n" + (p.text || "")).trim().slice(0, 3000),
        evidence_url: p.url, dm_draft: body.text || p.draft, score: p.score, why: p.why };
      const [cid] = await add(d, "post", pid);
      await markSent(cid, p.reply_via || p.platform, body.text || p.draft);
      await q(sb.from("posts").update({ contact_id: cid }).eq("id", pid));
      out.contact_id = cid;
    }
    return out;
  }

  const ROUTES = [
    ["GET", /^\/state$/, () => apiState()],
    ["GET", /^\/today$/, () => apiToday()],
    ["GET", /^\/areas$/, () => AREAS],
    ["GET", /^\/leads$/, (m, p) => apiLeads(p)],
    ["GET", /^\/leads\/(\d+)$/, m => lead(+m[1])],
    ["POST", /^\/leads\/(\d+)$/, (m, p, b) => apiLeadUpdate(+m[1], b)],
    ["POST", /^\/leads\/(\d+)\/sent$/, async (m, p, b) => { const id = +m[1]; await contact(id); await markSent(id, b.channel || "", b.text || ""); return lead(id) }],
    ["POST", /^\/leads\/(\d+)\/note$/, async (m, p, b) => { const id = +m[1]; await contact(id); await event(id, "note", "", String(b.text || "").trim()); return lead(id) }],
    ["POST", /^\/leads\/(\d+)\/signs$/, (m, p, b) => apiSigns(+m[1], b)],
    ["GET", /^\/posts$/, (m, p) => apiPosts(p)],
    ["POST", /^\/posts\/(.+)\/draft$/, async (m, p, b) => { await q(sb.from("posts").update({ draft: b.draft || "" }).eq("id", decodeURIComponent(m[1]))); return { ok: true } }],
    ["POST", /^\/posts\/(.+)\/status$/, (m, p, b) => apiPostStatus(decodeURIComponent(m[1]), b)],
    ["GET", /^\/settings$/, () => ({ proof: TEXT.proof, areas: TEXT.areas,
      searches: Object.fromEntries(Object.entries(services()).map(([k, v]) => [k, { name: v.name || k, quick: v.quick_searches || [] }])) })],
  ];

  /* ---------- sign-in ---------- */
  function signInScreen() {
    return new Promise(resolve => {
      const box = document.createElement("div");
      box.id = "signin";
      box.innerHTML = `<style>
        #signin,#signin *{box-sizing:border-box}
        #signin{position:fixed;inset:0;z-index:1000;display:grid;place-items:center;padding:16px;background:var(--bg)}
        #signin form{width:min(380px,100%);padding:28px;display:grid;gap:12px}
        #signin h1{font:700 1.35rem/1.2 var(--font);letter-spacing:-.02em;margin:0 0 4px}
        #signin p{margin:0 0 6px;color:var(--muted);font-size:.88rem}
        #signin .err{color:var(--danger);min-height:1.2em;font-size:.85rem;margin:0}
        #signin input.field{font-size:16px}</style>
        <form class="card" autocomplete="on">
          <h1>Closar studio</h1><p>Sign in to see and edit your leads from anywhere.</p>
          <input class="field" type="email" name="email" placeholder="Email" autocomplete="username" required>
          <input class="field" type="password" name="password" placeholder="Password" autocomplete="current-password" required>
          <p class="err" role="alert"></p>
          <button class="btn btn-primary" type="submit">Sign in</button>
        </form>`;
      document.body.appendChild(box);
      const f = box.querySelector("form");
      f.onsubmit = async e => {
        e.preventDefault();
        const btn = f.querySelector("button"); btn.disabled = true; f.querySelector(".err").textContent = "";
        const { error } = await sb.auth.signInWithPassword({ email: f.email.value.trim(), password: f.password.value });
        btn.disabled = false;
        if (error) { f.querySelector(".err").textContent = error.message; return }
        box.remove(); resolve();
      };
      f.email.focus();
    });
  }
  function signOutButton() {
    const b = document.createElement("button");
    b.className = "btn btn-quiet btn-sm"; b.textContent = "Sign out";
    b.style.cssText = "position:fixed;right:12px;bottom:calc(12px + env(safe-area-inset-bottom));z-index:50";
    b.onclick = async () => { await sb.auth.signOut(); location.reload() };
    document.body.appendChild(b);
  }

  /* ---------- start ---------- */
  const ready = (async () => {
    const conf = await (await fetch("supabase.json", { cache: "no-store" })).json();
    sb = window.supabase.createClient(conf.url, conf.anon_key, { auth: { persistSession: true, autoRefreshToken: true } });
    const [c, a, p] = await Promise.all(["config.yaml", "areas.yaml", "proof.yaml"].map(f => fetch("data/" + f, { cache: "no-store" }).then(r => r.ok ? r.text() : "")));
    TEXT = { areas: a, proof: p };
    CFG = jsyaml.load(c) || {}; AREAS = jsyaml.load(a) || {};
    try { PROOF = jsyaml.load(p) || {} } catch { PROOF = {} }
    const { data } = await sb.auth.getSession();
    if (!data.session) await signInScreen();
    signOutButton();
  })();

  window.Online = {
    async call(path, opts = {}) {
      await ready;
      const method = opts.method || (opts.body ? "POST" : "GET");
      const u = new URL(path, "https://x"), p = u.searchParams;
      for (const [m, re, fn] of ROUTES) {
        const hit = m === method && u.pathname.match(re);
        if (hit) {
          try { return await fn(hit, p, opts.body || {}) }
          catch (e) {
            if (/JWT|not authenticated|permission denied|row-level security/i.test(e.message)) { await sb.auth.signOut(); location.reload() }
            throw e;
          }
        }
      }
      throw err(LAPTOP, 409);
    },
    async downloadCsv() {
      await ready;
      const blob = new Blob([await toCsv()], { type: "text/csv;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = "closar-leads.csv"; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    },
  };
})();
