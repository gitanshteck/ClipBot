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
  function elapsedOf(st) {
    if (!st.startedAt) return 0;
    var now = st.pausedAt || Date.now();
    return Math.max(0, (now - st.startedAt - (st.pausedTotal || 0)) / 1000);
  }

  function sceneHTML(s, st) {
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
      body = '<div class="opts">' + (s.poll || []).map(function (o, i) { return '<div class="opt"><span class="n">' + (i + 1) + '</span>' + esc(o) + '</div>'; }).join("") + '</div>' +
        (s.rule ? '<div class="rule"><b>TONIGHT\'S RULE</b>' + esc(s.rule) + '</div>' : "");
      prompt = "type 1 or 2";
    } else if (type === "game") {
      body = '<div class="kd"><div class="k"><span>KILLS</span><strong>' + (st.kills || 0) + '</strong></div><div class="d"><span>DEATHS</span><strong>' + (st.deaths || 0) + '</strong></div></div>' +
        (s.predict ? '<div class="rule"><b>PREDICTION</b>' + esc(s.predict) + '</div>' : "") +
        (s.rule ? '<div class="rule"><b>TONIGHT\'S RULE</b>' + esc(s.rule) + '</div>' : "");
    } else if (s.poll) {
      opts = s.poll;
    }
    var html = '<div class="oc"><div class="bug"><span class="tk"><b>T</b>ECK</span><span>' + kicker + '</span>' + (count ? '<span class="ct-n">' + count + '</span>' : "") + '</div>' +
      '<h2 class="tt">' + esc(title) + '</h2>' + (sub ? '<p class="st">' + esc(sub) + '</p>' : "") + body + credit +
      (prompt ? '<div class="pr"><b>CHAT</b>' + esc(prompt) + '</div>' : "") + '</div>';
    if (prompt) {
      html += '<div class="l3"><span class="lk">CHAT</span><span class="lt">' + esc(prompt) +
        (opts ? opts.map(function (o, i) { return '<span class="o"><b>' + (i + 1) + '</b>' + esc(o) + '</span>'; }).join("") : "") + '</span></div>';
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
    el.className = "stage" + (lay === "vertical" ? " vert" : "") + " bg-" + (look.bg || "clear") + " lay-" + lay + " s-" + sc.type +
      (st.l3only ? " l3only" : "") + (look.guides ? " show-guides" : "");
    el.innerHTML = (look.mock || "") + sc.html + guidesHTML(lay);
    return lay;
  }

  function defaultState(date) {
    return { v: 0, on: false, seg: 0, item: 0, reveal: false, kills: 0, deaths: 0, startedAt: 0, pausedAt: 0, pausedTotal: 0, bg: "brand", lay: "h", l3only: false, ask: "", askShow: false, date: date || B.date };
  }

  // The live-chat answer card: the question as the kicker, the one-breath answer big,
  // up to three riff points, an honest "AI answer" tag. Types out while it streams.
  function askHTML(c) {
    var head = c.head || (c.done ? "" : "thinking…");
    var pts = (c.points || []).map(function (p) { return "<li>" + esc(p) + "</li>"; }).join("");
    var html = '<div class="oc oc-ask"><div class="bug"><span class="tk"><b>T</b>ECK</span><span>ASKED THE AI</span><span class="ai-tag">AI · CAN BE WRONG</span></div>' +
      '<p class="aq">“' + esc(c.q) + '”</p>' +
      '<h2 class="tt at' + (head.length > 150 ? " xlong" : head.length > 85 ? " long" : "") + '">' + esc(head) + (c.done ? "" : '<span class="caret"></span>') + '</h2>' +
      (pts ? '<ul class="ap">' + pts + '</ul>' : "") +
      '<div class="pr"><b>CHAT</b>agree? tell me in chat</div></div>';
    html += '<div class="l3"><span class="lk">ASKED</span><span class="lt">' + esc(c.q) + '</span></div>';
    return html;
  }
  function paintAsk(el, c, look) {
    var lay = layoutFor(look);
    el.className = "stage" + (lay === "vertical" ? " vert" : "") + " bg-" + (look.bg || "clear") + " lay-" + lay + " s-ask" + (look.guides ? " show-guides" : "");
    el.innerHTML = (look.mock || "") + askHTML(c) + guidesHTML(lay);
    return lay;
  }
  // Apply one control to a copy of the state. Returns {state, segChanged, clip} or null when nothing changes.
  function reduce(S, a, shift) {
    var s = Object.assign({}, S), seg = B.run[s.seg] || B.run[0], n = itemsOf(seg).length, segChanged = false, now = Date.now();
    var goSeg = function (to) {
      to = Math.max(0, Math.min(B.run.length - 1, to));
      if (to === s.seg && s.startedAt) return false;
      s.seg = to; s.item = 0; s.reveal = false; s.startedAt = now; s.pausedAt = 0; s.pausedTotal = 0;
      return true;
    };
    if (a === "next") { if (s.seg >= B.run.length - 1) return null; segChanged = goSeg(s.seg + 1); }
    else if (a === "prev") { if (s.seg <= 0) return null; segChanged = goSeg(s.seg - 1); }
    else if (a.indexOf("goto:") === 0) { segChanged = goSeg(parseInt(a.slice(5), 10) || 0); if (!segChanged) return null; }
    else if (a === "start") { s.on = true; s.date = B.date; s.kills = 0; s.deaths = 0; s.startedAt = 0; segChanged = goSeg(0); }
    else if (a === "stop") { s.on = false; }
    else if (a === "item" || a === "itemup") { if (n < 2) return null; s.item = (s.item + (a === "item" ? 1 : -1) + n) % n; s.reveal = false; }
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
    else return null;
    s.date = B.date;
    s.v = Math.max(now, (S.v || 0) + 1);
    return { state: s, segChanged: segChanged, clip: false };
  }
  function markerDoc(S, kind) {
    var seg = B.run[S.seg] || B.run[0], it = itemsOf(seg)[S.item];
    return { date: B.date, edition: B.edition, kind: kind, seg_id: seg.id, seg_name: seg.name, item: it ? (it.id || it.q || "") : "", label: it ? (it.card || it.q || "") : seg.screen.title };
  }

  root.BriefScenes = {
    init: init, esc: esc, tc: tc, mmss: mmss, KIND: KIND,
    sceneOf: sceneOf, itemsOf: itemsOf, elapsedOf: elapsedOf, sceneHTML: sceneHTML, guidesHTML: guidesHTML,
    layoutFor: layoutFor, paintStage: paintStage, askHTML: askHTML, paintAsk: paintAsk, defaultState: defaultState, reduce: reduce, markerDoc: markerDoc
  };
})(window);
