/* Stream brief: scene model and on-air state, shared by the rundown page
   (inlined), the OBS overlays and the dock. No DOM work except paintStage. */
(function (root) {
  "use strict";
  var B = { run: [], docket: [] };
  var docketById = {};
  var KIND = { talk: "Talk", chat: "Chat game", react: "React", transition: "Switch", game: "Game", starting: "Pre-show" };

  function init(brief) {
    B = brief;
    docketById = {};
    (brief.docket || []).forEach(function (d) { docketById[d.id] = d; });
  }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function tc(m) { var neg = m < 0; m = Math.abs(m); var h = Math.floor(m / 60), mm = m % 60; return (neg ? "-" : "") + h + ":" + (mm < 10 ? "0" : "") + mm; }
  function mmss(s) { s = Math.max(0, Math.floor(s)); var m = Math.floor(s / 60), x = s % 60; return m + ":" + (x < 10 ? "0" : "") + x; }

  function sceneOf(s) {
    if (s.scene) return s.scene;
    if (s.kind === "starting") return "starting";
    if (s.kind === "react" && s.items && s.items.length) return "topic";
    if (s.rounds) return "guess";
    if (s.kind === "transition") return "vote";
    if (s.kind === "game") return "game";
    return "card";
  }
  function itemsOf(s) {
    if (sceneOf(s) === "guess") return s.rounds || [];
    return (s.items || []).map(function (id) { return docketById[id]; }).filter(Boolean);
  }
  // What the dock offers to open for the card on screen: the item's own links, then its source
  // article, then links on the segment itself. De-duplicated by URL, six at most.
  var LINK_ICON = { trailer: "▶", video: "▶", official: "★", article: "↗", post: "↗", source: "↗" };
  function linksOf(seg, it) {
    var out = [], seen = {};
    function add(l, fallbackKind) {
      if (!l || !l.url || seen[l.url]) return;
      seen[l.url] = true;
      var kind = LINK_ICON[l.kind] ? l.kind : (fallbackKind || "article");
      out.push({ label: l.label || l.url, url: l.url, kind: kind, icon: LINK_ICON[kind] });
    }
    if (it) {
      (it.links || []).forEach(function (l) { add(l); });
      if (it.url) add({ label: (it.source || "Source"), url: it.url, kind: "source" });
    }
    (seg && seg.links || []).forEach(function (l) { add(l); });
    return out.slice(0, 6);
  }
  function elapsedOf(st) {
    if (!st.startedAt) return 0;
    var now = st.pausedAt || Date.now();
    return Math.max(0, (now - st.startedAt - (st.pausedTotal || 0)) / 1000);
  }

  // The card shape (clean | hud | slash) and an optional game tag come from the brief, with the segment able to override.
  function lookOf(s) { return Object.assign({ theme: "clean", tag: "" }, B.look || {}, (s && s.look) || {}); }
  // "Type 1 or 2" cards show live tallies when the state carries votes (typed in chat, or counted by hand in the dock).
  function tallyOf(st, i) {
    var v = (st && st.votes) || [];
    if (!v.length) return null;
    var total = v.reduce(function (a, b) { return a + (b || 0); }, 0);
    return { n: v[i] || 0, pct: total ? Math.round((v[i] || 0) / total * 100) : 0, lead: total > 0 && (v[i] || 0) === Math.max.apply(null, v) };
  }

  function sceneHTML(s, st) {
    var lk = lookOf(s);
    var type = sceneOf(s), items = itemsOf(s), idx = Math.min(st.item || 0, Math.max(0, items.length - 1)), it = items[idx];
    var kicker = esc(KIND[s.kind] || s.kind) + (type === "topic" && s.screen ? " · " + esc(s.screen.title) : "");
    var count = items.length > 1 ? (idx + 1) + " / " + items.length : "";
    var title = s.screen.title, sub = s.screen.sub, body = "", prompt = s.screen.prompt, opts = null, credit = "";
    if (type === "starting") {
      sub = "";
      var ag = B.run.filter(function (x) { return x.kind !== "starting" && x.id !== "outro"; }).slice(0, 9);
      body = '<ul class="agenda">' + ag.map(function (x) {
        return '<li><span class="at">' + (x.kind === "game" ? "then" : tc(x.t)) + '</span><span>' + esc(x.agenda || (x.kind === "game" ? x.screen.title + " " + x.screen.sub : x.screen.title)) + '</span></li>';
      }).join("") + '</ul><div class="count">' + mmss(s.dur * 60 - elapsedOf(st)) + '<small>TILL WE START</small></div>';
    } else if (type === "topic" && it) {
      title = it.card || it.headline; sub = "";
      credit = '<div class="credit">via <b>' + esc(it.source) + '</b> · ' + esc(it.date) + '</div>';
      prompt = it.question; opts = it.poll || null;
    } else if (type === "guess" && it) {
      title = it.q; sub = "";
      body = st.reveal ? '<div class="reveal">' + esc(it.a) + (it.note ? '<small>' + esc(it.note) + '</small>' : "") + '</div>' : '<div class="locked">GUESSES OPEN · TYPE IT IN CHAT</div>';
    } else if (type === "vote") {
      body = '<div class="opts">' + (s.poll || []).map(function (o, i) {
        var t = tallyOf(st, i);
        return '<div class="opt' + (t && t.lead ? " lead" : "") + '"><span class="n">' + (i + 1) + '</span><span class="ot">' + esc(o) + '</span>' +
          (t ? '<span class="vt"><i style="width:' + t.pct + '%"></i></span><span class="vn">' + t.n + '</span>' : "") + '</div>';
      }).join("") + '</div>' +
        (s.rule ? '<div class="rule"><b>TONIGHT\'S RULE</b>' + esc(s.rule) + '</div>' : "");
      prompt = "type 1 or 2";
    } else if (type === "game") {
      body = '<div class="kd"><div class="k"><span>KILLS</span><strong>' + (st.kills || 0) + '</strong></div><div class="d"><span>DEATHS</span><strong>' + (st.deaths || 0) + '</strong></div></div>' +
        (s.predict ? '<div class="rule"><b>PREDICTION</b>' + esc(s.predict) + '</div>' : "") +
        (s.rule ? '<div class="rule"><b>TONIGHT\'S RULE</b>' + esc(s.rule) + '</div>' : "");
    } else if (s.poll) {
      opts = s.poll;
    }
    var html = '<div class="oc"><div class="bug">' + markHTML() + '<span>' + kicker + '</span>' + (lk.tag ? '<span class="gt">' + esc(lk.tag) + '</span>' : "") + (count ? '<span class="ct-n">' + count + '</span>' : "") + '</div>' +
      '<h2 class="tt">' + esc(title) + '</h2>' + (sub ? '<p class="st">' + esc(sub) + '</p>' : "") + body + credit +
      (prompt ? '<div class="pr"><b>CHAT</b>' + esc(prompt) + '</div>' : "") + '</div>';
    if (prompt) {
      html += '<div class="l3"><span class="lk">CHAT</span><span class="lt">' + esc(prompt) +
        (opts ? opts.map(function (o, i) { var t = tallyOf(st, i); return '<span class="o' + (t && t.lead ? " lead" : "") + '"><b>' + (i + 1) + '</b>' + esc(o) + (t ? ' <em>' + t.n + '</em>' : "") + '</span>'; }).join("") : "") + '</span></div>';
    }
    return { html: html, type: type };
  }

  function guidesHTML(lay) {
    var z = function (x, y, w, h, label, no) { return '<div class="gz' + (no ? " no" : "") + '" style="left:' + x + 'px;top:' + y + 'px;width:' + w + 'px;height:' + h + 'px">' + label + '</div>'; };
    if (lay === "vertical") return '<div class="guides">' + z(0, 0, 1080, 780, "CAM") + z(960, 700, 120, 1000, "SHORTS BUTTONS", true) + z(0, 1600, 1080, 320, "CAPTIONS + TITLE", true) + '</div>';
    if (lay === "full") return '<div class="guides">' + z(1444, 110, 456, 256, "CAM SLOT") + z(1320, 760, 560, 290, "CHAT SLOT") + '</div>';
    if (lay === "game") return '<div class="guides">' + z(1444, 110, 456, 256, "CAM") + z(50, 60, 290, 270, "MINIMAP", true) + z(1320, 910, 560, 160, "CHAT", true) + '</div>';
    return '<div class="guides">' + z(560, 110, 520, 620, "YOUR FACE", true) + z(1260, 570, 620, 490, "CHAT", true) + z(1444, 110, 456, 256, "GAME CAM") + '</div>';
  }

  // look: {bg: brand|green|clear, lay: h|v, frame?: chat|game|vertical|full, mock?: html, guides?: bool}
  function layoutFor(look) {
    var map = { chat: "overlay", game: "game", vertical: "vertical", full: "full" };
    if (look.frame && map[look.frame]) return map[look.frame];
    if (look.lay === "v") return "vertical";
    return look.bg === "brand" ? "full" : "overlay";
  }
  function paintStage(el, s, st, look) {
    var lay = layoutFor(look);
    var sc = sceneHTML(s, st);
    el.className = "stage" + (lay === "vertical" ? " vert" : "") + " bg-" + (look.bg || "clear") + " lay-" + lay + " th-" + lk2(s) + " s-" + sc.type +
      (st.l3only ? " l3only" : "") + (look.guides ? " show-guides" : "");
    el.innerHTML = (look.mock || "") + sc.html + guidesHTML(lay);
    return lay;
  }

  function lk2(s) { return lookOf(s).theme; }
  // Brand lockup if <brief dir>/brand/lockup.png exists (served at /brief/brand/lockup.png), else the text mark.
  function markHTML() {
    return '<span class="tk"><img class="wm" src="/brief/brand/lockup.png" alt="TECK"><span class="txt"><b>T</b>ECK</span></span>';
  }
  document.addEventListener("error", function (e) {
    var t = e.target;
    if (t && t.classList && t.classList.contains("wm") && t.parentNode) t.parentNode.classList.add("nowm");
  }, true);

  function defaultState(date) {
    return { v: 0, on: false, seg: 0, item: 0, reveal: false, kills: 0, deaths: 0, startedAt: 0, pausedAt: 0, pausedTotal: 0, bg: "brand", lay: "h", l3only: false, ask: "", askShow: false, mute: false, votes: [], pin: null, date: date || B.date };
  }

  // The live-chat answer card: the question as the kicker, the one-breath answer big,
  // up to three riff points, an honest "AI answer" tag. Types out while it streams.
  function askHTML(c) {
    var head = c.head || (c.done ? "" : (c.searching ? "searching the web…" : "thinking…"));
    var srcs = (c.sources || []).map(function (x) { return esc(x.host); }).join(" · ");
    var pts = (c.points || []).map(function (p) { return "<li>" + esc(p) + "</li>"; }).join("");
    var html = '<div class="oc oc-ask"><div class="bug">' + markHTML() + '<span>ASKED THE AI</span><span class="ai-tag">AI · CAN BE WRONG</span></div>' +
      '<p class="aq">“' + esc(c.q) + '”</p>' +
      '<h2 class="tt at">' + esc(head) + (c.done ? "" : '<span class="caret"></span>') + '</h2>' +
      (pts ? '<ul class="ap">' + pts + '</ul>' : "") +
      (srcs && c.done ? '<div class="credit">via <b>' + srcs + '</b></div>' : "") +
      '<div class="pr"><b>CHAT</b>agree? tell me in chat</div></div>';
    html += '<div class="l3 l3-ask"><span class="lk">ASKED</span><span class="lt">' + esc(c.q) + '</span></div>';
    return html;
  }
  // Fit the answer card to the room its frame leaves it (--ask-h on the card, set per layout in
  // scenes.css) instead of guessing from text length. Every size in the card is calc(Npx * var(--k)),
  // so one number scales it all: find the largest k in [ASK_KMIN, 1] whose card fits. Last resorts,
  // in order: clamp the riff points to two lines, drop riff points from the end, shrink further and
  // clamp the headline. While an answer types out the text only grows, so k never goes back up for
  // the same answer (no size flicker on stream).
  var ASK_KMIN = 0.4;
  function fitAsk(stage, id) {
    var card = stage && typeof stage.querySelector === "function" && stage.querySelector(".oc-ask");
    if (!card || !card.offsetHeight || typeof window.getComputedStyle !== "function") return;
    var lim = parseFloat(window.getComputedStyle(card).getPropertyValue("--ask-h")) || 400;
    card.style.maxHeight = "none";
    var over = function (k) { card.style.setProperty("--k", String(k)); return card.offsetHeight > lim; };
    var hi = (stage._askId === id && stage._askK) ? stage._askK : 1, lo = ASK_KMIN, k = hi;
    if (over(hi)) {
      if (!over(lo)) {
        for (var i = 0; i < 9; i++) { var mid = (lo + hi) / 2; if (over(mid)) hi = mid; else lo = mid; }
        k = lo;
      } else {
        k = lo;
        card.classList.add("tight");
        var lis = card.querySelectorAll(".ap li"), n = lis.length;
        while (over(k) && n > 0) lis[--n].style.display = "none";
        if (over(k)) { k = 0.34; card.classList.add("clip"); over(k); }
      }
    }
    card.style.setProperty("--k", String(k));
    card.style.maxHeight = "";
    stage._askId = id; stage._askK = k;
  }
  // Web fonts change the text metrics when they arrive: fit again from scratch.
  function refitAsks() {
    if (typeof document.querySelectorAll !== "function") return;
    var all = document.querySelectorAll(".stage");
    for (var i = 0; i < all.length; i++) { var st = all[i], id = st._askId; st._askK = 0; if (st.querySelector(".oc-ask")) fitAsk(st, id); }
  }
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(refitAsks);
    if (document.fonts.addEventListener) document.fonts.addEventListener("loadingdone", refitAsks);
  }
  // A chat message pinned to the screen from the dock.
  function pinHTML(p) {
    return '<div class="oc oc-pin"><div class="bug">' + markHTML() + '<span>FROM CHAT</span></div>' +
      '<p class="pm">“' + esc(p.t) + '”</p><p class="pu">' + esc(p.u) + '</p></div>';
  }
  function paintPin(el, p, look) {
    var lay = layoutFor(look);
    el.className = "stage" + (lay === "vertical" ? " vert" : "") + " bg-" + (look.bg || "clear") + " lay-" + lay + " th-" + lk2(null) + " s-pin" + (look.guides ? " show-guides" : "");
    el.innerHTML = (look.mock || "") + pinHTML(p) + guidesHTML(lay);
    return lay;
  }
  function paintAsk(el, c, look) {
    var lay = layoutFor(look);
    el.className = "stage" + (lay === "vertical" ? " vert" : "") + " bg-" + (look.bg || "clear") + " lay-" + lay + " th-" + lk2(null) + " s-ask" + (look.guides ? " show-guides" : "");
    el.innerHTML = (look.mock || "") + askHTML(c) + guidesHTML(lay);
    fitAsk(el, c.id);
    return lay;
  }
  // Apply one control to a copy of the state. Returns {state, segChanged, clip} or null when nothing changes.
  function reduce(S, a, shift) {
    var s = Object.assign({}, S), seg = B.run[s.seg] || B.run[0], n = itemsOf(seg).length, segChanged = false, now = Date.now();
    var goSeg = function (to) {
      to = Math.max(0, Math.min(B.run.length - 1, to));
      if (to === s.seg && s.startedAt) return false;
      s.seg = to; s.item = 0; s.reveal = false; s.votes = []; s.startedAt = now; s.pausedAt = 0; s.pausedTotal = 0;
      s.askShow = false;   // an AI answer belongs to the card it was shown over: a new segment takes it down
      return true;
    };
    if (a === "next") { if (s.seg >= B.run.length - 1) return null; segChanged = goSeg(s.seg + 1); }
    else if (a === "prev") { if (s.seg <= 0) return null; segChanged = goSeg(s.seg - 1); }
    else if (a.indexOf("goto:") === 0) { segChanged = goSeg(parseInt(a.slice(5), 10) || 0); if (!segChanged) return null; }
    else if (a === "start") { s.on = true; s.date = B.date; s.kills = 0; s.deaths = 0; s.startedAt = 0; segChanged = goSeg(0); }
    else if (a === "stop") { s.on = false; s.askShow = false; }
    else if (a === "item" || a === "itemup") { if (n < 2) return null; s.item = (s.item + (a === "item" ? 1 : -1) + n) % n; s.reveal = false; s.votes = []; s.askShow = false; }
    else if (a === "reveal") { s.reveal = !s.reveal; }
    else if (a === "kill") { s.kills = Math.max(0, s.kills + (shift ? -1 : 1)); }
    else if (a === "death") { s.deaths = Math.max(0, s.deaths + (shift ? -1 : 1)); }
    else if (a === "pause") { if (s.pausedAt) { s.pausedTotal += now - s.pausedAt; s.pausedAt = 0; } else { s.pausedAt = now; } }
    else if (a === "bg") { s.bg = { brand: "green", green: "clear", clear: "brand" }[s.bg] || "brand"; }
    else if (a === "lay") { s.lay = s.lay === "h" ? "v" : "h"; }
    else if (a === "l3") { s.l3only = !s.l3only; }
    else if (a === "clip") { return { state: S, segChanged: false, clip: true }; }
    else if (a.indexOf("ask:") === 0) { s.ask = a.slice(4); s.askShow = true; }
    else if (a === "askhide") { s.askShow = false; }
    else if (a === "mute") { s.mute = !s.mute; }
    else if (/^vote[1-3]$/.test(a)) { var vi = parseInt(a.slice(4), 10) - 1; s.votes = (s.votes || []).slice(); while (s.votes.length < 2) s.votes.push(0); while (s.votes.length <= vi) s.votes.push(0); s.votes[vi] = Math.max(0, s.votes[vi] + (shift ? -1 : 1)); }
    else if (a === "votesreset") { s.votes = []; }
    else return null;
    s.date = B.date;
    s.v = Math.max(now, (S.v || 0) + 1);
    return { state: s, segChanged: segChanged, clip: false };
  }
  // Which transition sounds a state change should trigger (none on first load or a new day).
  function soundEvents(prev, next) {
    var ev = [];
    if (!prev || !next || prev.date !== next.date) return ev;
    if (!prev.on && next.on) return ["start"];
    if (!next.on) return ev;
    if (prev.seg !== next.seg) {
      ev.push("segment");
      var seg = B.run[next.seg];
      if (seg && sceneOf(seg) === "vote") ev.push("vote");
    } else if (prev.item !== next.item || prev.l3only !== next.l3only) ev.push("card");
    if (!prev.reveal && next.reveal) ev.push("reveal");
    if ((!prev.askShow && next.askShow) || (next.askShow && prev.ask !== next.ask)) ev.push("ask");
    if (next.pin && (!prev.pin || prev.pin.id !== next.pin.id)) ev.push("ask");   // a pinned chat message uses the answer sound
    if ((next.kills || 0) > (prev.kills || 0)) ev.push("kill");
    if ((next.deaths || 0) > (prev.deaths || 0)) ev.push("death");
    return ev;
  }
  function markerDoc(S, kind) {
    var seg = B.run[S.seg] || B.run[0], it = itemsOf(seg)[S.item];
    return { date: B.date, edition: B.edition, kind: kind, seg_id: seg.id, seg_name: seg.name, item: it ? (it.id || it.q || "") : "", label: it ? (it.card || it.q || "") : seg.screen.title };
  }

  root.BriefScenes = {
    init: init, markHTML: markHTML, esc: esc, tc: tc, mmss: mmss, KIND: KIND,
    sceneOf: sceneOf, itemsOf: itemsOf, linksOf: linksOf, elapsedOf: elapsedOf, sceneHTML: sceneHTML, guidesHTML: guidesHTML,
    lookOf: lookOf, tallyOf: tallyOf, soundEvents: soundEvents, layoutFor: layoutFor, paintStage: paintStage, askHTML: askHTML, paintAsk: paintAsk, pinHTML: pinHTML, paintPin: paintPin, defaultState: defaultState, reduce: reduce, markerDoc: markerDoc
  };
})(window);
