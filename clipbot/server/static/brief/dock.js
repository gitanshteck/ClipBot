/* The OBS dock: the one place you drive the stream brief from. Every press
   becomes a new state (BriefScenes.reduce), saved to the dashboard, which
   pushes it to every overlay. Segment changes and clip presses are logged as
   markers for tomorrow's chapters and the editors' clip list. */
(function () {
  "use strict";
  var SC = window.BriefScenes, Live = window.BriefLive;
  var dock = document.getElementById("dock"), conn = document.getElementById("conn");
  var B = null, S = null, toastT = null, feed = null;

  function toast(msg) {
    var t = document.querySelector(".toast");
    if (!t) { t = document.createElement("div"); t.className = "toast"; t.setAttribute("role", "status"); document.body.appendChild(t); }
    t.textContent = msg; t.classList.add("on");
    clearTimeout(toastT); toastT = setTimeout(function () { t.classList.remove("on"); }, 1600);
  }

  function act(a, shift) {
    if (!B || !S) return;
    var res = SC.reduce(S, a, shift);
    if (!res) return;
    if (res.clip) {
      Live.mark(SC.markerDoc(S, "clip")).then(function () { toast("Clip marked"); }, function () { toast("Couldn't save the marker"); });
      return;
    }
    S = res.state;
    render();
    renderAsks();
    Live.putState(S);
    if (res.segChanged) Live.mark(SC.markerDoc(S, "segment")).catch(function () {});
  }

  function render() {
    if (!B || !S) return;
    var esc = SC.esc, live = S.on && S.date === B.date;
    var seg = B.run[S.seg] || B.run[0], type = SC.sceneOf(seg), items = SC.itemsOf(seg), it = items[S.item];
    var plan = seg.dur * 60;
    var next = B.run[S.seg + 1];
    var line = it ? (it.take || "") : (seg.say || "");
    var html = '<button type="button" class="onair' + (live ? " live" : "") + '" data-act="' + (live ? "stop" : "start") + '"><span class="dot"></span>' + (live ? "On air · end" : "Start the run") + '</button>';
    html += '<div class="now"><div class="k"><span>' + esc(SC.KIND[seg.kind] || seg.kind) + ' · ' + SC.tc(seg.t) + '</span><span>' + (items.length > 1 ? (S.item + 1) + " / " + items.length : "") + '</span></div>' +
      '<div class="t">' + esc(seg.name) + '</div>' +
      (it ? '<div class="i">' + esc(it.card || it.q || it.headline || "") + (type === "guess" ? (S.reveal ? ' → <b>' + esc(it.a) + '</b>' : ' · <b>' + esc(it.a) + '</b> (hidden)') : "") + '</div>' : "") +
      (line ? '<div class="line">“' + esc(line) + '”</div>' : "") +
      '<div class="clock"><span id="dkE"></span><span>' + SC.mmss(plan) + '</span></div>' +
      '<div class="prog" id="dkP"><i></i></div>' +
      '<div class="next">' + (next ? 'Next: <b>' + esc(next.name) + '</b>' : 'Last segment') + '</div></div>';
    var notes = (seg.notes || []).slice(), links = SC.linksOf(seg, it), refs = "";
    if (it && it.risk) notes.unshift("Careful: " + it.risk);
    if (notes.length || links.length) {
      // Links first (they are the action), then the notes for the card.
      refs = '<div class="refs"><h2>Links &amp; notes</h2>' +
        (links.length ? '<div class="lnks">' + links.map(function (l) {
          return '<a class="lnk ' + esc(l.kind) + '" href="' + esc(l.url) + '" data-link="' + esc(l.url) + '" target="_blank" rel="noopener" title="' + esc(l.url) + '"><span>' + l.icon + '</span>' + esc(l.label) + '</a>';
        }).join("") + '</div><p class="hint">Opens in this PC&#39;s browser.</p>' : "") +
        (notes.length ? '<ul class="dknotes">' + notes.map(function (n) { return "<li>" + esc(n) + "</li>"; }).join("") + "</ul>" : "") + '</div>';
    }
    var hasItems = items.length > 1;
    html += '<div class="pad">' +
      '<button type="button" data-act="prev"' + (S.seg <= 0 ? " disabled" : "") + '>◀ Back</button><button type="button" class="big" data-act="next"' + (S.seg >= B.run.length - 1 ? " disabled" : "") + '>Next ▶</button>' +
      '<button type="button" data-act="itemup"' + (hasItems ? "" : " disabled") + '>▲ Item</button><button type="button" data-act="item"' + (hasItems ? "" : " disabled") + '>Item ▼</button>' +
      '<button type="button" data-act="reveal"' + (type === "guess" ? "" : " disabled") + '>' + (S.reveal ? "Hide answer" : "Reveal answer") + '</button>' +
      '<button type="button" data-act="l3" class="' + (S.l3only ? "on" : "") + '">' + (S.l3only ? "Show card" : "Lower third only") + '</button>' +
      '<button type="button" class="wide clip" data-act="clip">● CLIP THAT</button>' +
      '<button type="button" data-act="pause">' + (S.pausedAt ? "Resume timer" : "Pause timer") + '</button>' +
      '<button type="button" data-act="mute" class="' + (S.mute ? "on" : "") + '">' + (S.mute ? "Sounds muted" : "Sounds on") + '</button>' +
      '</div>';
    var fromChat = !!(feed && feed.connection && feed.connection.status === "live" && feed.votesFromChat);
    var vt = S.votes || [], nOpts = (type === "vote" ? (seg.poll || []).length : (it && it.poll ? it.poll.length : (seg.poll || []).length)) || 0;
    if (nOpts >= 2) {
      html += '<h2>Chat votes (type 1 or 2)' + (fromChat ? " · counting from chat" : "") + '</h2><div class="votes' + (fromChat ? " live" : "") + '"><span>' + (fromChat ? "live" : "count") + '</span>';
      for (var vi = 0; vi < Math.min(3, nOpts); vi++) html += '<button type="button" data-act="vote' + (vi + 1) + '"' + (fromChat ? " disabled" : "") + ' title="Shift-click: -1">' + (vi + 1) + ' · ' + (vt[vi] || 0) + '</button>';
      for (var vj = nOpts; vj < 3; vj++) html += '<button type="button" disabled>–</button>';
      html += '<button type="button" class="reset" data-act="votesreset">reset</button></div>';
    }
    html += refs;
    html += '<h2>Kills ' + (S.kills || 0) + ' · deaths ' + (S.deaths || 0) + '</h2><div class="kd"><button type="button" class="kill" data-act="kill" title="Kill +1">K +1</button><button type="button" data-act="kill" data-shift="1" title="Kill -1">K −1</button>' +
      '<button type="button" class="death" data-act="death" title="Death +1">D +1</button><button type="button" data-act="death" data-shift="1" title="Death -1">D −1</button></div>';
    html += '<h2>Run of show</h2><ul class="segs">' + B.run.map(function (s, i) {
      return '<li><button type="button" class="' + (i === S.seg && live ? "cur" : "") + '" data-act="goto:' + i + '"><span class="tc">' + SC.tc(s.t) + '</span><span>' + esc(s.name) + '</span><span class="kn">' + esc(SC.KIND[s.kind] || s.kind) + '</span></button></li>';
    }).join("") + '</ul>';
    html += '<p class="keys">With the dock focused: <kbd>→</kbd> next <kbd>←</kbd> back <kbd>↓</kbd> item <kbd>R</kbd> reveal <kbd>C</kbd> clip <kbd>K</kbd>/<kbd>D</kbd> kill/death (shift −1) <kbd>L</kbd> lower third <kbd>Space</kbd> pause</p>';
    var focused = document.activeElement && document.activeElement.getAttribute("data-act");
    dock.innerHTML = html;
    if (focused) { var again = dock.querySelector('[data-act="' + focused + '"]'); if (again) again.focus(); }
    tick();
  }
  // Only the clock moves every second: rebuilding the buttons would swallow clicks.
  function tick() {
    if (!B || !S) return;
    var seg = B.run[S.seg] || B.run[0], e = SC.elapsedOf(S), plan = seg.dur * 60, over = S.on && e > plan;
    var el = document.getElementById("dkE"), pr = document.getElementById("dkP");
    if (el) { el.textContent = SC.mmss(e) + (S.pausedAt ? " paused" : ""); el.className = over ? "over" : ""; }
    if (pr) { pr.className = "prog" + (over ? " over" : ""); pr.firstElementChild.style.width = Math.min(100, plan ? e / plan * 100 : 0) + "%"; }
  }

  dock.addEventListener("click", function (e) {
    var b = e.target.closest("[data-act]");
    if (b && !b.disabled) act(b.getAttribute("data-act"), b.getAttribute("data-shift") === "1");
  });
  // Links go through the server so they open in the PC's own browser even from OBS's embedded dock.
  document.addEventListener("click", function (e) {
    var a = e.target.closest("a[data-link]");
    if (!a) return;
    e.preventDefault();
    var url = a.getAttribute("data-link");
    Live.openLink(url).then(function () { toast("Opened in your browser"); }, function () {
      if (!window.open(url, "_blank", "noopener")) toast("Couldn't open it: " + url);
    });
  });
  document.addEventListener("click", function (e) {
    var c = e.target.closest("[data-copy]");
    if (!c) return;
    var text = c.getAttribute("data-copy");
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(function () { toast("Copied"); }, function () { toast(text); });
    else toast(text);
  });
  document.addEventListener("keydown", function (e) {
    if (e.target.matches && e.target.matches("input, textarea")) return;
    var map = { ArrowRight: "next", PageDown: "next", ArrowLeft: "prev", PageUp: "prev", ArrowDown: "item", ArrowUp: "itemup", " ": "pause" };
    if (map[e.key]) { e.preventDefault(); act(map[e.key]); return; }
    var k = e.key.toLowerCase();
    if (k === "r") act("reveal");
    else if (k === "c") act("clip");
    else if (k === "k") act("kill", e.shiftKey);
    else if (k === "d") act("death", e.shiftKey);
    else if (k === "l") act("l3");
  });

  // ---------- Ask the AI (kept outside #dock so typing survives re-renders) ----------
  var askForm = document.getElementById("askForm"), askQ = document.getElementById("askQ"), askGo = document.getElementById("askGo");
  var askList = document.getElementById("askList"), askProv = document.getElementById("askProv");
  var askSearch = document.getElementById("askSearch");
  try { askSearch.checked = localStorage.getItem("teck-brief-search") !== "0"; } catch (err) { askSearch.checked = true; }
  var asks = [];   // newest first
  var askHtml = "";
  // One card per answer, newest first. The Show/Hide button leads each card, so a long answer
  // can never push it out of reach, and the on-air card carries its own tag. The list is only
  // rewritten when its markup changed, and keeps its scroll position when it is.
  function renderAsks() {
    var esc = SC.esc;
    var html = asks.map(function (c) {
      var onair = !!(S && S.askShow && S.ask === c.id);
      var head = c.head || (c.done ? "" : (c.searching ? "searching the web…" : "thinking…"));
      var btn = c.error ? "" : (onair ? '<button type="button" class="hide" data-askhide>Hide from stream</button>' : '<button type="button" class="show" data-askshow="' + esc(c.id) + '">Show on stream</button>');
      return '<li class="ask-item' + (onair ? " is-onair" : "") + '"><div class="ask-top">' + btn + (onair ? '<span class="ask-tag">ON AIR</span>' : "") + '<span class="meta">' + esc(c.provider || "") + '</span></div>' +
        '<div class="ask-q">“' + esc(c.q) + '”</div>' +
        (c.error ? '<div class="ask-err">' + esc(c.error) + '</div>' : '<div class="ask-a' + (c.searching && !c.head ? " searching" : "") + '">' + esc(head) + (c.done ? "" : '<span class="caret"></span>') + '</div>') +
        ((c.sources || []).length ? '<div class="ask-src">via ' + c.sources.map(function (x) { return esc(x.host); }).join(", ") + '</div>' : "") +
        ((c.points || []).length ? '<ul class="ask-pts">' + c.points.map(function (p) { return "<li>" + esc(p) + "</li>"; }).join("") + "</ul>" : "") +
        '</li>';
    }).join("");
    if (html === askHtml) return;
    askHtml = html;
    var top = askList.scrollTop;
    askList.innerHTML = html;
    askList.scrollTop = top;
  }
  function upsert(c) {
    for (var i = 0; i < asks.length; i++) if (asks[i].id === c.id) { asks[i] = c; renderAsks(); return; }
    asks.unshift(c); asks = asks.slice(0, 20); renderAsks();
  }
  function history() {
    return asks.filter(function (c) { return c.done && !c.error; }).slice(0, 6).reverse().map(function (c) { return { q: c.q, a: c.text }; });
  }
  askForm.addEventListener("submit", function (e) {
    e.preventDefault();
    var q = askQ.value.trim();
    if (!q) return;
    askGo.disabled = true;
    try { localStorage.setItem("teck-brief-search", askSearch.checked ? "1" : "0"); } catch (err) {}
    Live.ask(q, history(), "OBS dock" + (S && B && S.on ? ", during '" + (B.run[S.seg] || {}).name + "'" : ""), askSearch.checked).then(function (c) {
      upsert(c); askQ.value = ""; askGo.disabled = false; askQ.focus();
    }, function (err) { askGo.disabled = false; toast(err.message || "Couldn't ask"); });
  });
  askQ.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); askForm.requestSubmit ? askForm.requestSubmit() : askGo.click(); }
  });
  askList.addEventListener("click", function (e) {
    var sh = e.target.closest("[data-askshow]");
    if (sh) { act("ask:" + sh.getAttribute("data-askshow")); renderAsks(); return; }
    if (e.target.closest("[data-askhide]")) { act("askhide"); renderAsks(); }
  });
  Live.onChat(function (c) { upsert(c); });
  Live.chatStatus().then(function (st) {
    askProv.textContent = st.provider ? "via " + st.provider : "no model set up";
    askProv.className = "ask-prov" + (st.provider ? "" : " off");
    if (!st.provider) { askProv.title = st.hint; askGo.disabled = true; askQ.placeholder = st.hint; }
  }, function () { askProv.textContent = "offline"; askProv.className = "ask-prov off"; });

  // ---------- transition sounds list (audition from the dock; the sound overlay plays them on stream) ----------
  var sndList = document.getElementById("sndList");
  Live.sounds().then(function (r) {
    window.BriefSounds.init(r.sounds);
    var ev = window.BriefSounds.events();
    sndList.innerHTML = ev.length ? ev.map(function (e) { return '<li><span class="ev">' + SC.esc(e.event) + '</span><span class="fl">' + SC.esc(e.file) + '</span><button type="button" data-play="' + SC.esc(e.event) + '">▶</button></li>'; }).join("") : '<li class="muted">No sounds found in the library\'s sfx folder.</li>';
  }, function () { sndList.innerHTML = '<li class="muted">Could not load sounds.</li>'; });
  // ▶ goes through the server to the OBS sound source, so it tests the real path into the OBS mixer.
  sndList.addEventListener("click", function (e) {
    var b = e.target.closest("[data-play]");
    if (!b) return;
    Live.playSound(b.getAttribute("data-play")).then(function () {
      toast(sndConn.className.indexOf("on") >= 0 ? "Sent to OBS" : "Sent, but no OBS sound source is connected");
    }, function () { toast("Couldn't send the sound"); });
  });
  var sndConn = document.getElementById("sndConn");
  function sndStatus() {
    Live.soundStatus().then(function (st) {
      sndConn.className = "conn " + (st.connected ? "on" : "off");
      sndConn.querySelector("span").textContent = st.connected ? "sound: OBS" : "sound: not in OBS";
      sndConn.title = st.connected ? "The OBS sound source is running" : "No OBS sound source heard in the last 25 s: add or refresh the Browser Source (see Transition sounds)";
    }, function () {});
  }
  sndStatus(); setInterval(sndStatus, 5000);

  // ---------- Chat awareness (read-only Kick chat; kept outside #dock so the channel box survives re-renders) ----------
  var chatEl = document.getElementById("chat");
  var chatChannel = "teckislive";
  try { chatChannel = localStorage.getItem("teck-brief-channel") || chatChannel; } catch (err) {}
  function spark(bins) {
    var max = Math.max.apply(null, bins.concat([4])), w = 200, h = 38, step = w / Math.max(1, bins.length - 1);
    var pts = bins.map(function (v, i) { return (i * step).toFixed(1) + "," + (h - 3 - (v / max) * (h - 8)).toFixed(1); });
    return '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" role="img" aria-label="Messages per 30 seconds, last 10 minutes">' +
      '<polyline points="0,' + (h - 2) + ' ' + pts.join(" ") + ' ' + w + ',' + (h - 2) + '" fill="rgba(25,162,210,.18)" stroke="none"/>' +
      '<polyline points="' + pts.join(" ") + '" fill="none" stroke="#19A2D2" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/></svg>';
  }
  function renderChat() {
    var esc = SC.esc, c = (feed && feed.connection) || { status: "off" }, live = c.status === "live";
    var h = '<div class="chat-h"><h2>Chat</h2><span class="stat' + (c.status === "error" ? " err" : "") + '">' +
      (c.status === "off" ? "not connected" : c.status === "connecting" ? "connecting…" : c.status === "error" ? "retrying: " + esc(c.error || "") : "<b>" + esc(c.channel) + "</b> · " + (feed.chatters || 0) + " chatters") + '</span></div>';
    if (c.status === "off" || c.status === "stopped") {
      h += '<form class="chat-connect" id="chatForm"><input id="chatChannel" value="' + esc(chatChannel) + '" aria-label="Your Kick channel name" maxlength="40" spellcheck="false"><button class="btn go" type="submit">Connect</button></form>' +
        '<p class="note">Reads your Kick chat (read only, nothing is posted). It uses Kick\'s unofficial chat endpoint, so it can stop working without notice; the dock works without it.</p>';
    } else {
      h += '<div class="pulse"><div>' + spark(feed.spark || []) + '</div><div class="big">' + (feed.rate || 0) + '<small>MSGS / MIN</small></div></div>' +
        '<div class="pulse-line" id="pulseLine"></div>';
      if (feed.spike) h += '<div class="alert"><span>Chat just spiked</span><button type="button" data-chat="clip">● Clip it</button></div>';
      else if (feed.quietAlert && B) {
        var beats = B.keep_alive || [], q = Math.floor((feed.quiet || 0) / 60);
        h += '<div class="alert quiet"><span>Chat quiet for ' + q + ' min' + (beats.length ? ' · try: ' + esc(beats[q % beats.length].beat) : "") + '</span></div>';
      }
      if (S && S.pin) h += '<div class="pinned"><span>On screen: <b>' + esc(S.pin.u) + '</b>: ' + esc(S.pin.t) + '</span><button class="btn" type="button" data-chat="unpin">Take it down</button></div>';
      var qs = feed.questions || [];
      h += '<h3>Questions</h3>' + (qs.length ? '<ul>' + qs.map(function (x) {
        return '<li class="q"><span class="who">' + esc(x.user) + ' · ' + (x.age < 90 ? x.age + "s" : Math.round(x.age / 60) + "m") + '</span><span>' + esc(x.text) + '</span>' +
          '<span class="row"><button type="button" data-chat="ask" data-id="' + esc(x.id) + '">Ask AI</button><button type="button" data-chat="pin" data-id="' + esc(x.id) + '">Pin</button><button type="button" data-chat="dismiss" data-id="' + esc(x.id) + '">✕</button></span></li>';
      }).join("") + '</ul>' : '<p class="note">None waiting.</p>');
      var rp = feed.repeats || [];
      if (rp.length) h += '<h3>Chat keeps asking</h3><div class="chips">' + rp.map(function (r) { return '<span class="chip">' + esc(r.text) + ' <b>×' + r.chatters + '</b></span>'; }).join("") + '</div>';
      var nc = feed.newcomers || [];
      if (nc.length) h += '<h3>New here <small>(tap to welcome on stream)</small></h3><div class="chips">' + nc.map(function (n) { return '<button type="button" class="chip hello" data-chat="hello" data-id="' + esc(n) + '">👋 ' + esc(n) + '</button>'; }).join("") + '</div>';
      h += '<label class="opt"><input type="checkbox" id="chatVotes"' + (feed.votesFromChat ? " checked" : "") + '> Count "1" / "2" votes and +1s from chat</label>' +
        '<div><button class="btn" type="button" data-chat="stop">Disconnect</button></div>';
    }
    chatEl.innerHTML = h;
    renderPulseLine();
  }
  function chatQuestion(id) {
    var q = ((feed && feed.questions) || []).filter(function (x) { return x.id === id; })[0];
    if (!q) return;
    askQ.value = q.text;
    askForm.requestSubmit ? askForm.requestSubmit() : askGo.click();
  }
  chatEl.addEventListener("submit", function (e) {
    if (e.target.id !== "chatForm") return;
    e.preventDefault();
    var ch = document.getElementById("chatChannel").value.trim().toLowerCase();
    try { localStorage.setItem("teck-brief-channel", ch); } catch (err) {}
    chatChannel = ch;
    Live.feedStart(ch).then(function (f) { feed = f; renderChat(); render(); }, function (err) { toast(err.message || "Couldn't connect"); });
  });
  chatEl.addEventListener("change", function (e) { if (e.target.id === "chatVotes") Live.feedVotes(e.target.checked).then(function () { if (feed) feed.votesFromChat = e.target.checked; render(); }); });
  chatEl.addEventListener("click", function (e) {
    var b = e.target.closest("[data-chat]");
    if (!b) return;
    var a = b.getAttribute("data-chat"), id = b.getAttribute("data-id");
    if (a === "ask") chatQuestion(id);
    else if (a === "pin") Live.feedPin(id).then(function () { toast("On screen"); }, function (err) { toast(err.message || "Couldn't pin"); });
    else if (a === "unpin") Live.feedPin("");
    else if (a === "dismiss") Live.feedDismiss(id).then(function () { if (feed) feed.questions = (feed.questions || []).filter(function (x) { return x.id !== id; }); renderChat(); });
    else if (a === "clip") act("clip");
    else if (a === "hello") Live.shoutout(id).then(function () { toast("Welcomed " + id); }, function (err) { toast(err.message || "Couldn't"); });
    else if (a === "pulsereset") Live.pulseReset().then(function () { toast("Counts reset"); });
    else if (a === "stop") Live.feedStop().then(function (f) { feed = f; renderChat(); render(); });
  });
  Live.feed().then(function (f) { feed = f; renderChat(); render(); }, function () { renderChat(); });
  Live.onFeed(function (f) {
    var typing = document.activeElement && document.activeElement.id === "chatChannel";
    feed = f; if (!typing) renderChat(); render();
  });

  // ---------- AI jumps in (outside #dock so toggles and the history survive re-renders) ----------
  var aimEl = document.getElementById("aim");
  var mom = { status: null, moments: [], skipped: 0 }, wg = null;
  var KIND_LABEL = { spike: "chat spiked", repeat: "same message", quiet: "quiet chat", random: "random", verdict: "vote verdict", recap: "recap" };
  var STATUS_LABEL = { pending: "waiting", shown: "on stream", vetoed: "vetoed", dock: "dock only", error: "failed", skipped: "passed" };
  function hm(sec) { var d = new Date(sec * 1000); return (d.getHours() < 10 ? "0" : "") + d.getHours() + ":" + (d.getMinutes() < 10 ? "0" : "") + d.getMinutes(); }
  function renderAim() {
    var esc = SC.esc, st = mom.status, cfg = (st && st.config) || {}, now = Date.now() / 1000;
    var on = !!cfg.enabled;
    var h = '<div class="aim-h"><h2>AI jumps in</h2><label class="sw" title="Lets Cue put a short line on stream when chat does something"><input type="checkbox" data-aim="enabled"' + (on ? " checked" : "") + '><span>' + (on ? "on" : "off") + '</span></label></div>';
    if (st) {
      var bits = [st.used_today + " / " + st.budget + " calls today"];
      if (on) bits.unshift(st.wait_seconds > 0 ? "next in " + Math.ceil(st.wait_seconds / 60) + " min" : "ready");
      h += '<p class="note">' + esc(bits.join(" · ")) + '</p>';
    }
    h += '<div class="aim-row"><button class="btn" type="button" data-aim="fire">Try one now</button><button class="btn" type="button" data-aim="recap">Recap now</button>' +
      '<label class="opt"><input type="checkbox" data-aim="recap_enabled"' + (cfg.recap_enabled ? " checked" : "") + '> auto recap</label></div>';
    var recap = mom.moments.filter(function (m) { return m.kind === "recap" && m.text; })[0];
    if (recap) h += '<div class="aim-recap"><div class="k">Chat recap · ' + hm(recap.at) + ' (private)</div>' + recap.text.split("\n").map(function (l) { return "<div>" + esc(l) + "</div>"; }).join("") + '</div>';
    h += '<ul class="aim-list">' + mom.moments.filter(function (m) { return m.kind !== "recap" || m !== recap; }).slice(0, 25).map(function (m) {
      var pend = m.status === "pending", onair = m.status === "shown";
      var chat = (m.chat || []).map(function (c) { return esc(c.user) + ": " + esc(c.text); }).join("<br>");
      return '<li class="aim-item ' + esc(m.status) + '"><div class="aim-top"><span class="tm">' + hm(m.at) + '</span><span class="kd">' + esc(KIND_LABEL[m.kind] || m.kind) + '</span><span class="stt ' + esc(m.status) + '">' + esc(STATUS_LABEL[m.status] || m.status) + (pend ? ' · <span data-showat="' + m.show_at + '"></span>' : "") + '</span></div>' +
        (m.status === "error" ? '<div class="aim-err">' + esc(m.error || "failed") + '</div>' : '<div class="aim-text">' + esc(m.text).replace(/\n/g, "<br>") + '</div>') +
        '<div class="aim-why">Why: ' + esc(m.reason) + (m.seg ? " · during " + esc(m.seg) : "") + '</div>' +
        (chat ? '<details class="aim-chat"><summary>chat at the time</summary><div>' + chat + '</div></details>' : "") +
        '<div class="aim-btns">' + ((pend || onair) ? '<button type="button" class="veto" data-aim-veto="' + esc(m.id) + '">' + (pend ? "Veto" : "Take down") + '</button>' : "") +
        ((m.status === "vetoed" || m.status === "shown") && m.kind !== "recap" && m.text ? '<button type="button" data-aim-reshow="' + esc(m.id) + '">' + (m.status === "shown" ? "Show again" : "Show anyway") + '</button>' : "") + '</div></li>';
    }).join("") + '</ul>';
    if (mom.skipped) h += '<p class="note">' + mom.skipped + ' attempt' + (mom.skipped === 1 ? "" : "s") + ' passed (the model had nothing better, or the line failed a check)</p>';
    aimEl.innerHTML = h;
    tickAim();
  }
  function tickAim() {
    var els = aimEl.querySelectorAll("[data-showat]"), now = Date.now() / 1000;
    for (var i = 0; i < els.length; i++) els[i].textContent = "on stream in " + Math.max(0, Math.ceil(parseFloat(els[i].getAttribute("data-showat")) - now)) + "s";
  }
  setInterval(tickAim, 500);
  function loadMoments() {
    Live.moments().then(function (r) { mom = { status: r.status, moments: r.moments || [], skipped: r.skipped || 0 }; renderAim(); }, function () { renderAim(); });
  }
  Live.onMoment(function (m) {
    var i = mom.moments.map(function (x) { return x.id; }).indexOf(m.id);
    if (i >= 0) mom.moments[i] = m; else mom.moments.unshift(m);
    renderAim();
    if (m.status === "pending" || m.status === "skipped" || m.status === "dock") loadMoments();
    if (m.status === "pending") toast("AI line coming up: veto it in the dock");
  });
  Live.onMomentsStatus(function (s) { mom.status = s; renderAim(); });
  aimEl.addEventListener("change", function (e) {
    var k = e.target.getAttribute("data-aim");
    if (!k || (k !== "enabled" && k !== "recap_enabled")) return;
    var patch = {}; patch[k] = e.target.checked;
    Live.momentSettings(patch).then(function (r) { mom = { status: r.status, moments: r.moments || [], skipped: r.skipped || 0 }; renderAim(); }, function (err) { toast(err.message || "Couldn't save"); });
  });
  aimEl.addEventListener("click", function (e) {
    var b = e.target.closest("button");
    if (!b) return;
    var vid = b.getAttribute("data-aim-veto"), sid = b.getAttribute("data-aim-reshow"), k = b.getAttribute("data-aim");
    if (vid) Live.momentVeto(vid).then(function () { toast("Vetoed"); }, function (err) { toast(err.message || "Too late"); });
    else if (sid) Live.momentReshow(sid).then(function () { toast("Back on stream"); }, function (err) { toast(err.message || "Couldn't show it"); });
    else if (k === "fire" || k === "recap") {
      b.disabled = true;
      Live.momentFire(k === "recap" ? "recap" : "random").then(function () { b.disabled = false; loadMoments(); }, function (err) { b.disabled = false; toast(err.message || "Couldn't ask the AI"); });
    }
  });
  Live.onWidgets(function (w) { wg = w; if (feed && feed.connection && feed.connection.status === "live") renderPulseLine(); });
  Live.widgets().then(function (w) { wg = w; }, function () {});
  function renderPulseLine() {
    var el = document.getElementById("pulseLine");
    if (!el || !wg) return;
    var p = wg.plus || [0, 0];
    el.innerHTML = '<span>+1 <b>' + p[0] + '</b>' + (p[1] ? ' · −1 <b>' + p[1] + '</b>' : "") + '</span><span>mood <b>' + (wg.mood || 0) + '</b></span><button class="btn" type="button" data-chat="pulsereset" title="Count 1/2 votes and +1s again from now">reset counts</button>';
  }
  loadMoments();

  function setConn(state, text) { conn.className = "conn " + state; conn.querySelector("span").textContent = text; }
  function load() {
    Live.getBrief().then(function (b) {
      B = b; SC.init(b);
      return Live.getState();
    }).then(function (s) {
      S = (s && s.date === B.date) ? s : SC.defaultState(B.date);
      render();
    }, function (err) {
      dock.innerHTML = '<p class="muted">' + (err && err.status === 404 ? "No stream brief for today yet. The daily run writes it around 4:30 pm." : "Can't reach the ClipBot dashboard. Is it running?") + '</p>';
      setTimeout(load, 15000);
    });
  }
  Live.onState(function (s) { if (B && (!S || s.v > S.v || s.date !== S.date)) { S = s; render(); renderAsks(); if (feed) renderChat(); } });
  Live.onBriefChanged(function () { toast("Tonight's brief was updated"); load(); });
  Live.onConnection(function (ok) { setConn(ok ? "on" : "off", ok ? "connected" : "reconnecting"); });
  setInterval(tick, 1000);
  load();
})();
