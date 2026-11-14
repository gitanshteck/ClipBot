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

  function fit() {
    var k = Math.min(window.innerWidth / W, window.innerHeight / H) || 1;
    stage.style.transform = "translate(" + Math.round((window.innerWidth - W * k) / 2) + "px," + Math.round((window.innerHeight - H * k) / 2) + "px) scale(" + k + ")";
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
    if (!S || s.date !== S.date || s.v > S.v) { S = s; draw(); }
  });
  Live.onBriefChanged(function () { loadBrief(); });
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
