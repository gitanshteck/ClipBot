/* The OBS dock: the one place you drive the stream brief from. Every press
   becomes a new state (BriefScenes.reduce), saved to the dashboard, which
   pushes it to every overlay. Segment changes and clip presses are logged as
   markers for tomorrow's chapters and the editors' clip list. */
(function () {
  "use strict";
  var SC = window.BriefScenes, Live = window.BriefLive;
  var dock = document.getElementById("dock"), conn = document.getElementById("conn");
  var B = null, S = null, toastT = null;

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
    var hasItems = items.length > 1;
    html += '<div class="pad">' +
      '<button type="button" data-act="prev"' + (S.seg <= 0 ? " disabled" : "") + '>◀ Back</button><button type="button" class="big" data-act="next"' + (S.seg >= B.run.length - 1 ? " disabled" : "") + '>Next ▶</button>' +
      '<button type="button" data-act="itemup"' + (hasItems ? "" : " disabled") + '>▲ Item</button><button type="button" data-act="item"' + (hasItems ? "" : " disabled") + '>Item ▼</button>' +
      '<button type="button" data-act="reveal"' + (type === "guess" ? "" : " disabled") + '>' + (S.reveal ? "Hide answer" : "Reveal answer") + '</button>' +
      '<button type="button" data-act="l3" class="' + (S.l3only ? "on" : "") + '">' + (S.l3only ? "Show card" : "Lower third only") + '</button>' +
      '<button type="button" class="wide clip" data-act="clip">● CLIP THAT</button>' +
      '<button type="button" class="wide" data-act="pause">' + (S.pausedAt ? "Resume timer" : "Pause timer") + '</button>' +
      '</div>';
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
  var asks = [];   // newest first
  function renderAsks() {
    var esc = SC.esc;
    askList.innerHTML = asks.map(function (c) {
      var onair = S && S.askShow && S.ask === c.id;
      var head = c.head || (c.done ? "" : "thinking…");
      return '<li class="ask-item' + (onair ? " onair" : "") + '"><div class="q">“' + esc(c.q) + '”</div>' +
        (c.error ? '<div class="err">' + esc(c.error) + '</div>' : '<div class="a">' + esc(head) + (c.done ? "" : '<span class="caret"></span>') + '</div>') +
        ((c.points || []).length ? "<ul>" + c.points.map(function (p) { return "<li>" + esc(p) + "</li>"; }).join("") + "</ul>" : "") +
        '<div class="row">' + (c.error ? "" : (onair ? '<button type="button" data-askhide>Hide from stream</button>' : '<button type="button" class="show" data-askshow="' + esc(c.id) + '">Show on stream</button>')) +
        '<span class="meta">' + esc(c.provider || "") + '</span></div></li>';
    }).join("");
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
    Live.ask(q, history(), "OBS dock" + (S && B && S.on ? ", during '" + (B.run[S.seg] || {}).name + "'" : "")).then(function (c) {
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
  Live.onState(function (s) { if (B && (!S || s.v > S.v || s.date !== S.date)) { S = s; render(); renderAsks(); } });
  Live.onBriefChanged(function () { toast("Tonight's brief was updated"); load(); });
  Live.onConnection(function (ok) { setConn(ok ? "on" : "off", ok ? "connected" : "reconnecting"); });
  setInterval(tick, 1000);
  load();
})();
