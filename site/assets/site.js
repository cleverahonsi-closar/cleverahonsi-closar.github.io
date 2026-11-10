(function(){
  // The 14 areas, fixes and price ranges from the Closar service plan (9 Nov 2026).
  var AREAS = [
    ["Content", "Long gaps between posts, events or launches with little content, only flyers or reposts, dead on other platforms, no captions.", "Repurposing pipeline: one recording becomes 10+ clips, captioned and scheduled.", "$600–1,200", "$300–600"],
    ["Lead capture", "Bio link goes to the homepage, no form or free offer, “DM for info”, no newsletter signup, ads sending people to a plain page.", "Free offer, opt-in page, contact saved in the CRM, welcome email.", "$500–900", "$150–300"],
    ["DMs + comments", "“Price?” comments left unanswered, late replies, “check DM” replies that never come.", "Keyword auto-reply that sends a link and tags the person in the CRM.", "$400–800", "$150–300"],
    ["Response speed", "Forms answered in 24+ hours or never, no auto-reply, a plain Gmail address with no system, “sorry for the late reply” in public.", "Instant AI reply, an alert to you, enquiries routed to the right person.", "$500–1,000", "$150–400"],
    ["Booking", "“Call to book” only, no online calendar, reviews about no-shows or double bookings, Google Form bookings.", "Online calendar with automatic reminders.", "$400–900", "$150–300"],
    ["Follow-up", "Sign up and get nothing, or one email. Newsletter not sent for months. Nothing after a webinar or event.", "Automatic email sequence (5–10 emails).", "$600–1,200", "$200–400"],
    ["Sales", "Prices hidden behind “contact for a quote”, no cart-abandon email, slow quotes written by hand, checkout through DMs or bank transfer.", "Quote and cart follow-up, plus upsell automation.", "$700–1,500", "$200–500"],
    ["Reviews", "Few reviews for the size of the business, newest review months old, bad reviews with no reply.", "Automatic review requests and AI-drafted replies.", "$400–700", "$150–250"],
    ["Support", "“No response” reviews, the same questions asked again and again, no FAQ page, public complaints.", "AI support bot trained on your FAQs.", "$800–2,000", "$200–500"],
    ["Onboarding", "“Didn't get access” reviews, products delivered by hand, no welcome email, contracts sent as PDFs.", "Payment, contract, welcome and access, all automatic.", "$800–1,800", "$200–400"],
    ["CRM + data", "Repeat customers treated as new, the same promo sent twice, “Hi there” mass emails, wrong names.", "CRM setup or clean-up: tags, segments, pipeline.", "$800–2,500", "$300–800"],
    ["Hiring pains", "Hiring an admin, VA, social media assistant or customer service rep. “Overwhelmed” posts. The owner doing everything.", "Automate that role's tasks.", "$1,000–2,500", "$300–800"],
    ["Events", "Tickets sold through DMs or bank transfer, no reminders, no recap content, no list kept for next time, low turnout.", "Tickets into the CRM, reminders, recap content, a list for the next event.", "$700–1,500", "$250–500 per event"],
    ["Win-back", "Customers never hear from you after buying. No loyalty or referral offer. No “we miss you” message.", "Win-back campaign at 60–90 days, plus a referral offer.", "$400–800", "$150–300"]
  ];
  // Stable link for each fix, e.g. services.html#fix-response. Outreach messages link straight to these.
  var SLUGS = ["content","leads","dms","response","booking","followup","sales","reviews","support","onboarding","crm","hiring","events","winback"];
  var CREATOR_PICKS = ["Creator plan: Starter","Creator plan: Consistent","Creator plan: Pro","Creator: one-off 30 clips"];
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  function $(id){ return document.getElementById(id); }

  // Services page: one card per fix, plus the jump list.
  var grid = $("svc-grid"), jump = $("jump");
  if (grid){
    AREAS.forEach(function(a, i){
      var id = "fix-" + SLUGS[i];
      var el = document.createElement("article");
      el.className = "svc"; el.id = id;
      el.innerHTML =
        '<div class="svc-top"><h3></h3><span class="n"></span></div>' +
        '<p class="signs"><b>Signs</b></p>' +
        '<p class="fix"><b>The fix</b></p>' +
        '<div class="price"><span>Setup <strong></strong></span><span>Monthly <strong></strong></span></div>' +
        '<a class="btn btn-ghost btn-sm pick" href="contact.html">Get this fixed</a>';
      el.querySelector("h3").textContent = a[0];
      el.querySelector(".n").textContent = (i + 1 < 10 ? "0" : "") + (i + 1);
      el.querySelector(".signs").appendChild(document.createTextNode(a[1]));
      el.querySelector(".fix").appendChild(document.createTextNode(a[2]));
      var st = el.querySelectorAll(".price strong");
      st[0].textContent = a[3]; st[1].textContent = a[4];
      el.querySelector(".pick").setAttribute("data-pick", a[0]);
      grid.appendChild(el);
      if (jump){ var j = document.createElement("a"); j.href = "#" + id; j.textContent = a[0]; jump.appendChild(j); }
    });
    // A fix opened from an outreach link gets scrolled to once the cards exist.
    if (/^#fix-/.test(location.hash)){ var tgt = $(location.hash.slice(1)); if (tgt) setTimeout(function(){ tgt.scrollIntoView(); }, 50); }
  }

  // "Get this fixed" / plan buttons open the audit page with that choice filled in.
  Array.prototype.forEach.call(document.querySelectorAll(".pick[data-pick]"), function(p){
    p.href = "contact.html?fix=" + encodeURIComponent(p.getAttribute("data-pick"));
  });

  // Audit form.
  var form = $("audit");
  if (form){
    var fixSel = $("f-fix");
    AREAS.map(function(a){ return a[0]; }).concat(CREATOR_PICKS).forEach(function(v){
      var o = document.createElement("option"); o.value = v; o.textContent = v; fixSel.appendChild(o);
    });
    var want = new URLSearchParams(location.search).get("fix");
    if (want && Array.prototype.some.call(fixSel.options, function(o){ return o.value === want; })) fixSel.value = want;
    var tz = $("f-tz");
    function guessTz(){ try { tz.value = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (err) {} }
    guessTz();
    var status = $("status"), send = $("send");
    function say(cls, msg){ status.className = "status " + cls; status.textContent = msg; }
    form.addEventListener("submit", function(e){
      e.preventDefault();
      if (form._honey && form._honey.value) return;
      if (!form.checkValidity()){
        var bad = form.querySelector(":invalid"); if (bad) bad.focus();
        say("err", "Please add your name, a valid email and your website or profile link.");
        return;
      }
      send.disabled = true; send.textContent = "Sending…";
      var data = {};
      new FormData(form).forEach(function(v, k){ data[k] = v; });
      data["Page"] = location.href;
      fetch("https://formsubmit.co/ajax/ahonsiclever@gmail.com", {method:"POST", headers:{"Content-Type":"application/json", "Accept":"application/json"}, body:JSON.stringify(data)})
        .then(function(r){ return r.json().catch(function(){ return {}; }).then(function(j){ if (!r.ok || String(j.success) === "false") throw new Error(j.message || "send failed"); }); })
        .then(function(){
          form.reset(); guessTz();
          say("ok", "Got it. A confirmation email is on its way, and your audit follows within one working day.");
          send.textContent = "Sent";
        })
        .catch(function(){
          var body = "Name: " + data.Name + "\nWebsite: " + data.Website + "\nWants fixed: " + data["Wants fixed"] + "\nTime zone: " + data["Time zone"] + "\nBest time: " + data["Best time"] + "\n\n" + (data.Message || "");
          say("err", "That didn't send. Your email app will open with everything filled in instead.");
          location.href = "mailto:ahonsiclever@gmail.com?subject=" + encodeURIComponent("Free audit request") + "&body=" + encodeURIComponent(body);
          send.disabled = false; send.textContent = "Send my free audit request";
        });
    });
  }

  // Intro video: plays with sound on click.
  var reel = $("reel"), rv = $("reel-video"), rp = $("reel-play");
  if (reel && rv && rp){
    rp.addEventListener("click", function(){ rv.controls = true; reel.classList.add("playing"); rv.play(); });
  }

  // Lightbox for the build files.
  var lb = $("lb");
  var thumbs = Array.prototype.slice.call(document.querySelectorAll(".thumb[data-full]"));
  if (lb && thumbs.length){
    var lbImg = $("lb-img"), lbCap = $("lb-cap"), cur = 0;
    var show = function(k){
      cur = (k + thumbs.length) % thumbs.length;
      var t = thumbs[cur];
      lbImg.src = t.getAttribute("data-full"); lbImg.alt = t.querySelector("img").alt; lbCap.textContent = t.getAttribute("data-cap") || "";
    };
    thumbs.forEach(function(t, k){ t.addEventListener("click", function(){ show(k); if (lb.showModal) lb.showModal(); else window.open(t.getAttribute("data-full")); }); });
    $("lb-prev").addEventListener("click", function(){ show(cur - 1); });
    $("lb-next").addEventListener("click", function(){ show(cur + 1); });
    $("lb-close").addEventListener("click", function(){ lb.close(); });
    lb.addEventListener("click", function(e){ if (e.target === lb) lb.close(); });
    lb.addEventListener("keydown", function(e){ if (e.key === "ArrowLeft") show(cur - 1); if (e.key === "ArrowRight") show(cur + 1); });
  }

  // Media Chic clips: play muted while on screen, tap to hear.
  var vids = Array.prototype.slice.call(document.querySelectorAll("video.auto"));
  vids.forEach(function(v){ v.addEventListener("click", function(){ v.muted = !v.muted; if (v.paused) v.play(); }); });
  if (vids.length){
    if ("IntersectionObserver" in window && !reduce){
      var vio = new IntersectionObserver(function(entries){
        entries.forEach(function(e){ if (e.isIntersecting) { var p = e.target.play(); if (p && p.catch) p.catch(function(){}); } else e.target.pause(); });
      }, { threshold: .4 });
      vids.forEach(function(v){ vio.observe(v); });
    } else vids.forEach(function(v){ v.controls = true; });
  }

  var hdr = $("hdr");
  if (hdr){
    var onScroll = function(){ hdr.classList.toggle("scrolled", window.scrollY > 4); };
    window.addEventListener("scroll", onScroll, { passive: true }); onScroll();
  }

  // Example system card: the enquiry lights up one step at a time, then starts over.
  var steps = Array.prototype.slice.call(document.querySelectorAll("#steps .step"));
  var clock = $("sys-clock");
  if (steps.length){
    if (reduce){ steps.forEach(function(s){ s.classList.add("on"); }); }
    else {
      var i = -1, timer = null;
      var tick = function(){
        i = (i + 1) % (steps.length + 2);
        steps.forEach(function(s, k){ s.classList.toggle("on", k <= i && i < steps.length); s.classList.toggle("now", k === i); });
        if (i < steps.length && clock) clock.textContent = steps[i].querySelector(".t").textContent;
      };
      var start = function(){ if (!timer) { tick(); timer = setInterval(tick, 1100); } };
      var stop = function(){ clearInterval(timer); timer = null; };
      document.addEventListener("visibilitychange", function(){ document.hidden ? stop() : start(); });
      start();
    }
  }
})();
