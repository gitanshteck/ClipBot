/* Transition sounds for the stream brief. One page plays them (the "sound" overlay),
   so each sound goes into OBS exactly once, with its own fader ("Control audio via OBS").
   Volumes and files come from /api/brief/sounds (settings brief.sounds, the library's sfx folder). */
(function (root) {
  "use strict";
  var map = {}, audio = {}, muted = false, onBlocked = null, last = {};
  function clamp(v) { v = Number(v); return isFinite(v) ? Math.max(0, Math.min(1, v)) : 0.6; }
  function init(m) {
    map = m || {};
    audio = {};
    Object.keys(map).forEach(function (k) {
      var a = new Audio(map[k].url);
      a.preload = "auto";
      a.volume = clamp(map[k].volume);
      audio[k] = a;
    });
  }
  function play(ev, force) {
    if ((muted && !force) || !audio[ev]) return;
    var now = Date.now();
    if (last[ev] && now - last[ev] < 80) return;   // a burst of identical events plays once
    last[ev] = now;
    var c = audio[ev].cloneNode();
    c.volume = audio[ev].volume;
    var p = c.play();
    if (p && p.catch) p.catch(function () { if (onBlocked) onBlocked(); });
  }
  root.BriefSounds = {
    init: init, play: play,
    setMuted: function (m) { muted = !!m; },
    events: function () { return Object.keys(map).map(function (k) { return { event: k, file: map[k].file, volume: map[k].volume }; }); },
    onBlocked: function (fn) { onBlocked = fn; }
  };
})(window);
