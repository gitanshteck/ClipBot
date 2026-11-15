/* Stream brief: client for the local dashboard (/api/brief/*). Used by the
   rundown page when it is served at /brief, by the OBS overlays and by the dock.
   State changes travel as whole state objects with a version `v`, so the newest
   change wins whichever window made it. */
(function (root) {
  "use strict";
  var HEADERS = { "Content-Type": "application/json", "X-ClipBot": "1" };

  function api(method, url, body) {
    return fetch(url, { method: method, headers: HEADERS, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store" })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (data) {
          if (!r.ok) { var e = new Error(data.detail || data.error || ("HTTP " + r.status)); e.status = r.status; throw e; }
          return data;
        });
      });
  }

  var pending = null, inFlight = false;
  function putState(state) {
    pending = state;
    if (inFlight) return;
    inFlight = true;
    (function loop() {
      var next = pending; pending = null;
      api("PUT", "/api/brief/state", next).catch(function () {}).then(function () {
        if (pending) loop(); else inFlight = false;
      });
    })();
  }

  // One EventSource per page; listeners filter the dashboard's event stream for brief events.
  var source = null, listeners = [], statusListeners = [];
  function ensureSource() {
    if (source || typeof EventSource === "undefined") return;
    source = new EventSource("/api/events");
    source.addEventListener("brief", function (e) {
      var payload;
      try { payload = JSON.parse(e.data); } catch (err) { return; }
      listeners.forEach(function (fn) { try { fn(payload); } catch (err) {} });
    });
    source.onopen = function () { statusListeners.forEach(function (fn) { fn(true); }); };
    source.onerror = function () { statusListeners.forEach(function (fn) { fn(false); }); };
  }
  function onEvent(fn) { ensureSource(); listeners.push(fn); }

  root.BriefLive = {
    api: api,
    getBrief: function () { return api("GET", "/api/brief"); },
    getState: function () { return api("GET", "/api/brief/state"); },
    putState: putState,
    onState: function (fn) { onEvent(function (p) { if (p && p.state) fn(p.state); }); },
    onBriefChanged: function (fn) { onEvent(function (p) { if (p && p.changed) fn(p); }); },
    onMarker: function (fn) { onEvent(function (p) { if (p && p.marker) fn(p.marker); }); },
    sounds: function () { return api("GET", "/api/brief/sounds"); },
    openLink: function (url) { return api("POST", "/api/brief/open", { url: url }); },
    onFeed: function (fn) { onEvent(function (p) { if (p && p.feed) fn(p.feed); }); },
    feed: function () { return api("GET", "/api/brief/feed"); },
    feedStart: function (channel) { return api("POST", "/api/brief/feed/start", { channel: channel }); },
    feedStop: function () { return api("POST", "/api/brief/feed/stop", {}); },
    feedVotes: function (on) { return api("POST", "/api/brief/feed/votes", { on: !!on }); },
    feedDismiss: function (id) { return api("POST", "/api/brief/feed/dismiss", { id: id }); },
    feedPin: function (id) { return api("POST", "/api/brief/feed/pin", { id: id || "" }); },
    widgets: function () { return api("GET", "/api/brief/widgets"); },
    onWidgets: function (fn) { onEvent(function (p) { if (p && p.widgets) fn(p.widgets); }); },
    shoutout: function (user) { return api("POST", "/api/brief/shoutout", { user: user }); },
    pulseReset: function () { return api("POST", "/api/brief/pulse/reset", {}); },
    moments: function () { return api("GET", "/api/brief/moments"); },
    onMoment: function (fn) { onEvent(function (p) { if (p && p.moment) fn(p.moment); }); },
    onMomentsStatus: function (fn) { onEvent(function (p) { if (p && p.momentsStatus) fn(p.momentsStatus); }); },
    momentSettings: function (patch) { return api("PUT", "/api/brief/moments/settings", patch); },
    momentVeto: function (id) { return api("POST", "/api/brief/moments/" + encodeURIComponent(id) + "/veto", {}); },
    momentReshow: function (id) { return api("POST", "/api/brief/moments/" + encodeURIComponent(id) + "/reshow", {}); },
    momentFire: function (kind) { return api("POST", "/api/brief/moments/fire", { kind: kind || "random" }); },
    onPlay: function (fn) { onEvent(function (p) { if (p && p.play) fn(p.play); }); },
    soundPing: function () { return api("POST", "/api/brief/sounds/ping", {}); },
    soundStatus: function () { return api("GET", "/api/brief/sounds/status"); },
    playSound: function (event) { return api("POST", "/api/brief/sounds/play", { event: event }); },
    onChat: function (fn) { onEvent(function (p) { if (p && p.chat) fn(p.chat); }); },
    chatStatus: function () { return api("GET", "/api/brief/chat/status"); },
    ask: function (question, history, page, search) { return api("POST", "/api/brief/chat", { question: question, history: history || [], page: page || "", search: !!search }); },
    onConnection: function (fn) { ensureSource(); statusListeners.push(fn); },
    mark: function (doc) { return api("POST", "/api/brief/markers", doc); },
    getFeedback: function (date) { return api("GET", "/api/brief/feedback?date=" + encodeURIComponent(date)); },
    setFeedback: function (doc) { return api("POST", "/api/brief/feedback", doc); }
  };
})(window);
