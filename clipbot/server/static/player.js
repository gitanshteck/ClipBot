/* Player adapter: one interface for "the thing that shows the video".
 *
 * review.js and compile.js only ever touch their player through this surface:
 *   currentTime (get/set), duration, paused, playbackRate (set), hidden (set),
 *   play() -> Promise, pause(), addEventListener(type, fn) for
 *   'loadedmetadata' | 'timeupdate' | 'play' | 'pause' | 'error'.
 * A native <video> already has all of it, so for a workspace whose video is on
 * disk createPlayer() returns the element itself and nothing changes.
 *
 * For a YouTube workspace (no video downloaded) it returns a YouTubePlayer that
 * offers the same surface on top of YouTube's IFrame Player API. What differs,
 * and why it's handled here rather than in the pages:
 *
 *  - The API has no 'timeupdate' event, so we poll getCurrentTime().
 *  - A cross-origin iframe swallows keyboard focus, which would kill the review
 *    shortcuts (space, I, O, [ ]) the moment you clicked the video. A click
 *    shield sits over the iframe so focus stays in the page; clicking it toggles
 *    play/pause, which is what clicking a video does anyway.
 *  - seekTo() on a video that has ENDED restarts it, so a seek made while not
 *    playing is followed by pauseVideo(). (Measured: from plain "paused" it
 *    doesn't restart; from "ended" it does.)
 *  - Until the video has been played once, YouTube remembers the latest seek and
 *    applies it at start, but getCurrentTime() keeps reporting the old position
 *    and later seeks don't show up in it. So before the first play we keep the
 *    caller's position ourselves (currentTime reads it back correctly) and
 *    hand it to YouTube when play() is called.
 *  - Embeds need a Referer or YouTube answers "error 153"; the iframe carries an
 *    explicit referrerpolicy so a stricter page-wide policy can't strip it.
 *  - The API script is third-party code, so it is only ever loaded for a
 *    YouTube workspace - never on a Kick/local page.
 *
 * What an embed cannot do: frame-exact stepping (a seek is approximate), playback
 * rates outside YouTube's fixed steps, and drawing frames to a canvas (which is
 * why the vertical-reel editor isn't offered for YouTube workspaces).
 */
(function () {
  'use strict';

  const STATE = { UNSTARTED: -1, ENDED: 0, PLAYING: 1, PAUSED: 2, BUFFERING: 3, CUED: 5 };
  const POLL_MS = 200;          // ~ the 4-5 Hz a native <video> fires timeupdate at
  const SEEK_HOLD_MS = 500;     // after a seek, trust our own value over the player's
  const READY_TIMEOUT_MS = 15000;

  const ERROR_TEXT = {
    2: "YouTube rejected the video id.",
    5: "YouTube's player hit an internal error. Reload the page; if it keeps happening, open the video on YouTube.",
    100: "This video was removed or is private, so it can't be played here.",
    101: "The video owner has disabled embedding for this video. In YouTube Studio: Content → the video → Show more → allow embedding.",
    150: "The video owner has disabled embedding for this video. In YouTube Studio: Content → the video → Show more → allow embedding.",
    153: "YouTube refused the embed because the request carried no referrer (error 153). " +
         "A privacy extension or browser setting may be stripping it - open the dashboard at " +
         "http://localhost:8765 or http://127.0.0.1:8765 and check it isn't blocked.",
  };

  let apiPromise = null;

  function loadYouTubeApi() {
    if (window.YT && window.YT.Player) return Promise.resolve();
    if (!apiPromise) {
      apiPromise = new Promise((resolve, reject) => {
        const previous = window.onYouTubeIframeAPIReady;
        window.onYouTubeIframeAPIReady = () => { if (previous) previous(); resolve(); };
        const script = document.createElement('script');
        script.src = 'https://www.youtube.com/iframe_api';
        script.onerror = () => {
          apiPromise = null;  // let a later attempt retry
          reject(new Error("Couldn't load YouTube's player script - are you offline, or is youtube.com blocked?"));
        };
        document.head.appendChild(script);
      });
    }
    return apiPromise;
  }

  class YouTubePlayer {
    constructor(mount, opts) {
      this.kind = 'youtube';
      this.videoId = opts.videoId;
      this.errorMessage = '';

      this._mount = mount;
      this._listeners = {};
      this._t = 0;
      // The workspace's audio-probed duration: the same clock the transcript
      // and clip times are on. YouTube's own figure is only a fallback.
      this._duration = Number(opts.duration) > 0 ? Number(opts.duration) : 0;
      this._state = STATE.UNSTARTED;
      this._yt = null;
      this._ready = false;
      this._queue = [];
      this._holdUntil = 0;
      this._pendingSeek = null;    // a seek made before the first play (see header)
      this._loadedFired = false;

      this._build();
      this._start();

      // With a known duration the timelines and transcript can draw right away,
      // so a broken player (offline, embedding disabled) still leaves the page
      // usable for reviewing candidates - you just can't play them.
      if (this._duration > 0) setTimeout(() => this._fireLoaded(), 0);
    }

    /* ---- the <video>-like surface ---- */

    get currentTime() { return this._t; }

    set currentTime(seconds) {
      let t = Math.max(0, Number(seconds) || 0);
      const dur = this.duration;
      if (dur) t = Math.min(t, dur);
      this._t = t;                                  // readable straight away, like a <video>
      this._holdUntil = Date.now() + SEEK_HOLD_MS;
      if (this._notStarted()) {
        // Never played yet: remember it, apply it when play() is called.
        this._pendingSeek = t;
      } else {
        const wasPaused = this.paused;
        this._whenReady(() => {
          this._yt.seekTo(t, true);
          if (wasPaused) this._yt.pauseVideo();     // an ENDED video would otherwise restart
        });
      }
      this._emit('timeupdate');
    }

    get duration() {
      if (this._duration) return this._duration;
      try { return (this._ready && this._yt.getDuration()) || 0; } catch (e) { return 0; }
    }

    get paused() {
      return !(this._state === STATE.PLAYING || this._state === STATE.BUFFERING);
    }

    play() {
      this._whenReady(() => {
        if (this._pendingSeek != null) {
          this._yt.seekTo(this._pendingSeek, true);
          this._pendingSeek = null;
        }
        this._yt.playVideo();
      });
      return Promise.resolve();
    }

    pause() {
      this._whenReady(() => this._yt.pauseVideo());
    }

    set playbackRate(rate) {
      this._whenReady(() => {
        // YouTube only offers fixed steps (0.25 ... 2); pick the nearest.
        const rates = this._yt.getAvailablePlaybackRates() || [rate];
        this._yt.setPlaybackRate(
          rates.reduce((a, b) => (Math.abs(b - rate) < Math.abs(a - rate) ? b : a)));
      });
    }

    get playbackRate() {
      try { return this._ready ? this._yt.getPlaybackRate() : 1; } catch (e) { return 1; }
    }

    set hidden(value) { this._mount.style.display = value ? 'none' : ''; }
    get hidden() { return this._mount.style.display === 'none'; }

    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    }

    /* ---- extras the pages use ---- */

    /** youtube.com URL at `seconds`, for the "Open on YouTube" link. */
    watchUrl(seconds) {
      return 'https://www.youtube.com/watch?v=' + encodeURIComponent(this.videoId) +
        (seconds > 1 ? '&t=' + Math.floor(seconds) + 's' : '');
    }

    /* ---- internals ---- */

    _emit(type) {
      for (const fn of (this._listeners[type] || [])) {
        try { fn({ type: type, target: this }); } catch (e) { console.error(e); }
      }
    }

    _fireLoaded() {
      if (this._loadedFired || !(this.duration > 0)) return;
      this._loadedFired = true;
      this._emit('loadedmetadata');
    }

    _whenReady(fn) {
      if (this._ready) { try { fn(); } catch (e) { console.error(e); } }
      else this._queue.push(fn);
    }

    _build() {
      const iframe = document.createElement('iframe');
      iframe.className = 'yt-iframe';
      iframe.title = 'YouTube video player';
      const params = new URLSearchParams({
        enablejsapi: '1',
        origin: location.origin,
        controls: '0',        // ClipBot's own transport replaces YouTube's
        disablekb: '1',
        fs: '0',
        rel: '0',
        playsinline: '1',
        iv_load_policy: '3',
        modestbranding: '1',
      });
      iframe.src = 'https://www.youtube.com/embed/' + encodeURIComponent(this.videoId) + '?' + params;
      // Explicit, so a stricter page-wide Referrer-Policy can't strip the Referer
      // YouTube requires (error 153).
      iframe.setAttribute('referrerpolicy', 'strict-origin-when-cross-origin');
      iframe.setAttribute('allow', 'autoplay; encrypted-media; picture-in-picture');
      this._iframe = iframe;

      const shield = document.createElement('div');
      shield.className = 'yt-shield';
      shield.title = 'Click to play / pause';
      shield.addEventListener('click', () => { this.paused ? this.play() : this.pause(); });
      this._shield = shield;

      this._mount.appendChild(iframe);
      this._mount.appendChild(shield);
    }

    _start() {
      this._readyTimer = setTimeout(() => {
        if (!this._ready) {
          this._fail("YouTube's player didn't respond. The video may not allow embedding, " +
                     "or the request was blocked (error 153 = no referrer). " +
                     "Check the video's embedding setting in YouTube Studio.");
        }
      }, READY_TIMEOUT_MS);

      loadYouTubeApi().then(() => {
        this._yt = new window.YT.Player(this._iframe, {
          events: {
            onReady: () => this._onReady(),
            onStateChange: (e) => this._onState(e.data),
            onError: (e) => this._onError(e.data),
          },
        });
      }).catch((err) => this._fail(err.message));
    }

    _onReady() {
      this._ready = true;
      clearTimeout(this._readyTimer);
      this._fireLoaded();
      for (const fn of this._queue.splice(0)) {
        try { fn(); } catch (e) { console.error(e); }
      }
      // The API has no timeupdate event, so poll.
      setInterval(() => this._tick(), POLL_MS);
    }

    _notStarted() {
      return this._state === STATE.UNSTARTED || this._state === STATE.CUED;
    }

    _tick() {
      if (!this._ready) return;
      this._fireLoaded();                            // duration may only appear after first play
      // Before the first play getCurrentTime() is stale (see the header), so the
      // caller's own position stays authoritative.
      if (this._notStarted()) return;
      if (Date.now() < this._holdUntil) return;      // let a just-issued seek settle
      let t;
      try { t = this._yt.getCurrentTime(); } catch (e) { return; }
      if (typeof t !== 'number' || isNaN(t)) return;
      if (Math.abs(t - this._t) > 0.001 || this._state === STATE.PLAYING) {
        this._t = t;
        this._emit('timeupdate');
      }
    }

    _onState(code) {
      const previous = this._state;
      this._state = code;
      if (code === STATE.PLAYING && previous !== STATE.PLAYING) {
        this._emit('play');
      } else if ((code === STATE.PAUSED || code === STATE.ENDED) &&
                 (previous === STATE.PLAYING || previous === STATE.BUFFERING)) {
        this._emit('pause');
      }
    }

    _onError(code) {
      this._fail(ERROR_TEXT[code] || ('YouTube reported error ' + code + '.'));
    }

    _fail(message) {
      this.errorMessage = message;
      // Let people click YouTube's own error UI (sign-in prompts, "watch on
      // YouTube" links) instead of having the shield swallow it.
      if (this._shield) this._shield.style.pointerEvents = 'none';
      this._emit('error');
    }
  }

  /** `el` is the page's #player element: a <video> for a local workspace, an
   *  empty <div class="yt-frame"> for a YouTube one. `source` comes from the
   *  server (`{kind: 'youtube', video_id, duration}` or `{kind: 'local'}`). */
  function createPlayer(el, source) {
    if (source && source.kind === 'youtube') {
      return new YouTubePlayer(el, { videoId: source.video_id, duration: source.duration });
    }
    return el;
  }

  window.createPlayer = createPlayer;
  window.YouTubePlayer = YouTubePlayer;   // exposed for tests / console poking
})();
