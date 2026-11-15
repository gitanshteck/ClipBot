/* OBS overlay: draws the current segment for one frame (chat | game | vertical | full)
   and follows the dock. Chat, game and vertical stay empty until Start is pressed;
   the full scene always shows (it is a scene of its own, like your Intro). */
(function () {
  "use strict";
  var SC = window.BriefScenes, Live = window.BriefLive;
  var body = document.body, stage = document.getElementById("stage"), note = document.getElementById("note");
  var frame = body.getAttribute("data-frame"), bg = body.getAttribute("data-bg");
  var vertical = frame === "vertical";
  var W = vertical ? 1080 : 1920, H = vertical ? 1920 : 1080;
  var B = null, S = null, lastKey = "", asks = {};
  var debug = /[?&]debug=1/.test(location.search);

  // The "sound" frame draws nothing: it plays the transition sounds for the whole stream, so
  // each sound reaches OBS once however many visual overlays are open.
  function runSound() {
    var Snd = window.BriefSounds, prev = null;
    stage.style.display = "none";
    Snd.onBlocked(function () { say("Click once to enable sound (OBS does this itself)"); note.style.display = "block"; });
    Live.sounds().then(function (r) { Snd.init(r.sounds); });
    function load() {
      Live.getBrief().then(function (b) { B = b; SC.init(b); return Live.getState(); }).then(function (s) {
        prev = s; Snd.setMuted(s.mute);
      }, function () { setTimeout(load, 15000); });
    }
    Live.onState(function (s) {
      if (!B) return;
      Snd.setMuted(s.mute);
      SC.soundEvents(prev, s).forEach(function (e) { Snd.play(e); });
      prev = s;
    });
    Live.onMarker(function (m) { if (m.kind === "clip") Snd.play("clip"); });
    Live.onPlay(function (e) { Snd.play(e, true); });          // "play on stream" from the dock, even when muted
    function ping() { Live.soundPing().catch(function () {}); }
    ping(); setInterval(ping, 10000);                          // lets the dock show "OBS sound source connected"
    Live.onConnection(function (ok) { if (ok) ping(); });
    Live.onBriefChanged(function () { Live.sounds().then(function (r) { Snd.init(r.sounds); }); load(); });
    document.addEventListener("click", function () { say(""); });
    load();
  }
  if (frame === "sound") { runSound(); return; }

  function fit() {
    var k = Math.min(window.innerWidth / W, window.innerHeight / H) || 1;
    var t = "translate(" + Math.round((window.innerWidth - W * k) / 2) + "px," + Math.round((window.innerHeight - H * k) / 2) + "px) scale(" + k + ")";
    stage.style.transform = t;
    wid.style.transform = t;
  }

  // ---- widgets beside the card: the +1 tally, an AI line, and (opt in) a vibe meter and word wall ----
  // ?w=plus,callout,mood,words picks them; the default is plus,callout. They follow the live chat feed,
  // not the show state, so a +1 count or an AI line can appear on any frame while the show is on.
  var wid = document.getElementById("wid");
  if (vertical) wid.className = "vert";
  var wanted = ((/[?&]w=([a-z,]*)/.exec(location.search) || [])[1] || "plus,callout").split(",");
  var WG = null, calloutTimer = null, lastPlus = "", calloutShown = "";
  function drawWidgets() {
    if (!WG) { wid.innerHTML = ""; return; }
    var esc = SC.esc, h = "";
    var show = !!(S && S.on && B && S.date === B.date);
    var up = (WG.plus || [0, 0])[0], down = (WG.plus || [0, 0])[1];
    if (wanted.indexOf("plus") >= 0 && show && up + down > 0) {
      var net = up - down, key = up + "/" + down;
      h += '<div class="w-plus' + (key !== lastPlus ? " bump" : "") + '"><span class="pl">+1</span><span class="who">CHAT</span><b>' + up + '</b>' + (down ? '<i>−' + down + '</i>' : "") + '</div>';
      lastPlus = key;
    } else if (!(up + down)) lastPlus = "";
    if (wanted.indexOf("mood") >= 0 && show && WG.mood > 0) h += '<div class="w-mood"><span>CHAT MOOD</span><div class="bar"><i style="width:' + Math.max(4, Math.min(100, WG.mood)) + '%"></i></div></div>';
    if (wanted.indexOf("words") >= 0 && show && (WG.words || []).length) {
      var top = WG.words[0].n || 1;
      h += '<div class="w-words">' + WG.words.slice(0, 10).map(function (w) { return '<span style="font-size:' + (18 + Math.round(w.n / top * 22)) + 'px">' + esc(w.w) + '</span>'; }).join("") + '</div>';
    }
    var c = WG.callout;
    if (wanted.indexOf("callout") >= 0 && c && c.text) {
      h += '<div class="w-call' + (calloutShown === c.id ? " noanim" : "") + '"><div class="bug">' + SC.markHTML() + '<span>CUE</span></div><p>' + esc(c.text) + '</p></div>';
      calloutShown = c.id;
    } else calloutShown = "";
    wid.innerHTML = h;
  }
  function setWidgets(w) {
    WG = w;
    drawWidgets();
    clearTimeout(calloutTimer);
    if (w && w.callout) calloutTimer = setTimeout(function () { if (WG && WG.callout) { WG = Object.assign({}, WG, { callout: null }); drawWidgets(); } }, Math.max(500, w.callout.ms_left));
  }
  function say(text) { note.textContent = text; note.style.display = text && (debug || bg !== "clear") ? "block" : "none"; }

  function draw() {
    if (!B) return;
    if (S && S.askShow && S.ask) {
      var c = asks[S.ask];
      if (!c) { fetchAsk(S.ask); return; }
      var akey = "ask|" + c.id + "|" + c.text.length + "|" + c.done;
      if (akey === lastKey) return;
      var same = lastKey.indexOf("ask|" + c.id + "|") === 0;
      SC.paintAsk(stage, c, { frame: frame, bg: bg });
      if (same) stage.classList.add("noanim");
      lastKey = akey;
      return;
    }
    if (S && S.pin && S.pin.t) {
      var pkey = "pin|" + S.pin.id + "|" + S.pin.t;
      if (pkey === lastKey) return;
      SC.paintPin(stage, S.pin, { frame: frame, bg: bg });
      lastKey = pkey;
      return;
    }
    var live = S && S.on && S.date === B.date;
    if (!live && frame !== "full") {
      if (lastKey !== "off") { stage.className = "stage" + (vertical ? " vert" : "") + " bg-clear"; stage.innerHTML = ""; lastKey = "off"; }
      return;
    }
    var st = S && S.date === B.date ? S : SC.defaultState(B.date);
    var seg = B.run[st.seg] || B.run[0];
    var ticking = SC.sceneOf(seg) === "starting" ? Math.floor(SC.elapsedOf(st)) : "";
    var key = [st.seg, st.item, st.reveal, st.kills, st.deaths, st.l3only, ticking].join("|");
    if (key === lastKey) return;
    var sameCard = lastKey && lastKey !== "off" && lastKey.split("|").slice(0, 3).join("|") === key.split("|").slice(0, 3).join("|");
    SC.paintStage(stage, seg, st, { frame: frame, bg: bg });
    if (sameCard) stage.classList.add("noanim");
    lastKey = key;
  }

  function loadBrief() {
    return Live.getBrief().then(function (b) {
      B = b; SC.init(b); lastKey = ""; say("");
      return Live.getState();
    }).then(function (s) { S = s; draw(); }, function (err) {
      say(err && err.status === 404 ? "No stream brief for today yet" : "Can't reach the ClipBot dashboard");
      setTimeout(loadBrief, 15000);
    });
  }

  Live.onState(function (s) {
    if (!S || s.date !== S.date || s.v > S.v) { S = s; draw(); drawWidgets(); }
  });
  Live.onBriefChanged(function () { loadBrief(); });
  Live.onWidgets(setWidgets);
  Live.widgets().then(setWidgets, function () {});
  function fetchAsk(id) {
    if (fetchAsk.busy === id) return;
    fetchAsk.busy = id;
    Live.api("GET", "/api/brief/chat/" + encodeURIComponent(id)).then(function (c) { asks[c.id] = c; fetchAsk.busy = ""; draw(); }, function () { fetchAsk.busy = ""; });
  }
  Live.onChat(function (c) { asks[c.id] = c; if (S && S.askShow && S.ask === c.id) draw(); });
  Live.onConnection(function (ok) { if (ok && !B) loadBrief(); });
  window.addEventListener("resize", fit);
  setInterval(draw, 1000);
  fit();
  loadBrief();
})();
