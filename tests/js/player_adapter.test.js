'use strict';
/* Tests for clipbot/server/static/player.js, run under plain Node (no
 * framework, no browser) against a fake YT.Player. tests/test_player_js.py runs
 * this file as part of `python -m unittest discover -s tests`.
 *
 * The fake models the YouTube behaviours the adapter exists to work around,
 * each of which was measured against the real player (not assumed):
 *   - before the first play, getCurrentTime() is stale and only the latest
 *     seekTo() is applied at start;
 *   - seekTo() from an ENDED video restarts it;
 *   - there is no timeupdate event.
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SOURCE = fs.readFileSync(
  path.join(__dirname, '..', '..', 'clipbot', 'server', 'static', 'player.js'), 'utf8');

const STATE = { UNSTARTED: -1, ENDED: 0, PLAYING: 1, PAUSED: 2, BUFFERING: 3, CUED: 5 };

function makeEnv(opts) {
  opts = opts || {};
  const env = { now: 1000000, timeouts: [], intervals: [], nextId: 1, scripts: [], created: [], yt: null };

  function element(tag) {
    const el = {
      tag, style: {}, attrs: {}, children: [], className: '', title: '', listeners: {},
      setAttribute(k, v) { this.attrs[k] = v; },
      appendChild(c) { this.children.push(c); return c; },
      addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    };
    env.created.push(el);
    return el;
  }

  class FakeYT {
    constructor(iframe, options) {
      this.iframe = iframe; this.events = options.events; this.calls = [];
      this.state = STATE.UNSTARTED; this.time = 0; this.duration = 0; this.rate = 1;
      env.yt = this;
    }
    seekTo(t) { this.calls.push(['seekTo', t]); }
    playVideo() { this.calls.push(['playVideo']); }
    pauseVideo() { this.calls.push(['pauseVideo']); }
    setPlaybackRate(r) { this.calls.push(['setPlaybackRate', r]); this.rate = r; }
    getPlaybackRate() { return this.rate; }
    getAvailablePlaybackRates() { return [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2]; }
    getCurrentTime() { return this.time; }
    getDuration() { return this.duration; }
    getPlayerState() { return this.state; }
    // ---- test controls ----
    fireReady() { this.events.onReady({}); }
    setState(code) { this.state = code; this.events.onStateChange({ data: code }); }
    fireError(code) { this.events.onError({ data: code }); }
    callNames() { return this.calls.map(c => c[0]); }
  }

  const sandbox = {
    console, Promise, URLSearchParams, encodeURIComponent, Math, Number, Array, Object,
    location: { origin: 'http://127.0.0.1:8765' },
    document: {
      createElement: element,
      head: { appendChild(el) { env.scripts.push(el); } },
    },
    Date: { now: () => env.now },
    setTimeout(fn, ms) { const id = env.nextId++; env.timeouts.push({ id, fn, ms }); return id; },
    clearTimeout(id) { env.timeouts = env.timeouts.filter(t => t.id !== id); },
    setInterval(fn, ms) { const id = env.nextId++; env.intervals.push({ id, fn, ms }); return id; },
  };
  sandbox.window = sandbox;
  if (opts.withYT !== false) sandbox.YT = { Player: FakeYT };
  vm.runInNewContext(SOURCE, sandbox);

  env.createPlayer = sandbox.createPlayer;
  env.window = sandbox;
  env.tick = () => env.intervals.forEach(i => i.fn());
  env.advance = (ms) => { env.now += ms; };
  env.runTimeouts = () => { const due = env.timeouts.splice(0); due.forEach(t => t.fn()); };
  // Let the `loadYouTubeApi().then(...)` microtasks run.
  env.settle = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
  return env;
}

/** A ready YouTube player (API present, onReady fired). */
async function readyPlayer(duration, extra) {
  const env = makeEnv(extra);
  const mount = env.window.document.createElement('div');
  const player = env.createPlayer(mount, { kind: 'youtube', video_id: 'M7lc1UVf-VE', duration });
  await env.settle();
  env.yt.fireReady();
  return { env, player, mount };
}

const tests = [];
function test(name, fn) { tests.push([name, fn]); }

// --------------------------------------------------------------------------

test('local source returns the element untouched and loads no YouTube script', async () => {
  const env = makeEnv({ withYT: false });
  const video = { fake: 'video element' };
  assert.strictEqual(env.createPlayer(video, { kind: 'local' }), video);
  assert.strictEqual(env.createPlayer(video, undefined), video);
  assert.strictEqual(env.scripts.length, 0, 'the third-party API script must never load for local video');
});

test('a YouTube source builds a referrer-safe iframe and a click shield', async () => {
  const { player, mount, env } = await readyPlayer(1344);
  const iframe = mount.children.find(c => c.tag === 'iframe');
  const shield = mount.children.find(c => c.className === 'yt-shield');
  assert.ok(iframe && shield, 'iframe and shield are added to the mount');
  assert.strictEqual(iframe.attrs.referrerpolicy, 'strict-origin-when-cross-origin');
  const url = new URL(iframe.src);
  assert.strictEqual(url.pathname, '/embed/M7lc1UVf-VE');
  assert.strictEqual(url.searchParams.get('enablejsapi'), '1');
  assert.strictEqual(url.searchParams.get('origin'), 'http://127.0.0.1:8765');
  assert.strictEqual(url.searchParams.get('controls'), '0');
  assert.strictEqual(url.searchParams.get('disablekb'), '1');
  assert.strictEqual(player.kind, 'youtube');
  assert.strictEqual(env.yt.iframe, iframe, 'YT.Player binds to OUR iframe rather than replacing it');
});

test('the API script is loaded only when YouTube is needed, and only once', async () => {
  const env = makeEnv({ withYT: false });
  const mount = env.window.document.createElement('div');
  env.createPlayer(mount, { kind: 'youtube', video_id: 'abcdefghijk', duration: 10 });
  env.createPlayer(env.window.document.createElement('div'), { kind: 'youtube', video_id: 'abcdefghijk', duration: 10 });
  assert.strictEqual(env.scripts.length, 1);
  assert.strictEqual(env.scripts[0].src, 'https://www.youtube.com/iframe_api');
});

test('loadedmetadata fires once, before the player is even ready, when the duration is known', async () => {
  const env = makeEnv({ withYT: false });   // player never becomes ready (offline)
  const player = env.createPlayer(env.window.document.createElement('div'),
    { kind: 'youtube', video_id: 'abcdefghijk', duration: 1344 });
  let fired = 0;
  player.addEventListener('loadedmetadata', () => fired++);
  env.runTimeouts();
  env.runTimeouts();
  assert.strictEqual(fired, 1);
  assert.strictEqual(player.duration, 1344);
});

test('duration falls back to YouTube\'s own figure, and loadedmetadata waits for it', async () => {
  const { env, player } = await readyPlayer(0);
  let fired = 0;
  player.addEventListener('loadedmetadata', () => fired++);
  env.tick();
  assert.strictEqual(fired, 0);
  env.yt.duration = 812;
  env.tick();
  assert.strictEqual(fired, 1);
  assert.strictEqual(player.duration, 812);
});

test('before the first play, seeks are remembered, not sent, and not overwritten by the stale player time', async () => {
  const { env, player } = await readyPlayer(1344);
  player.currentTime = 100;
  player.currentTime = 600;
  assert.strictEqual(player.currentTime, 600, 'reads back what was set, like a <video>');
  assert.deepStrictEqual(env.yt.calls, [], 'nothing is sent to YouTube yet');
  env.advance(5000);
  env.yt.time = 0;                      // YouTube still reports where it started
  env.tick();
  assert.strictEqual(player.currentTime, 600, 'the stale time must not clobber the caller\'s position');
});

test('play() from a never-started player applies only the latest remembered seek, then plays', async () => {
  const { env, player } = await readyPlayer(1344);
  player.currentTime = 100;
  player.currentTime = 600;
  player.play();
  assert.deepStrictEqual(env.yt.calls, [['seekTo', 600], ['playVideo']]);
  player.play();
  assert.deepStrictEqual(env.yt.callNames().slice(2), ['playVideo'], 'the remembered seek is applied once');
});

test('calls made before onReady are queued and run in order once it fires', async () => {
  const env = makeEnv();
  const player = env.createPlayer(env.window.document.createElement('div'),
    { kind: 'youtube', video_id: 'abcdefghijk', duration: 100 });
  await env.settle();
  player.currentTime = 40;
  player.play();
  assert.deepStrictEqual(env.yt.calls, []);
  env.yt.fireReady();
  assert.deepStrictEqual(env.yt.calls, [['seekTo', 40], ['playVideo']]);
});

test('once playing, the poll adopts the player time and emits timeupdate', async () => {
  const { env, player } = await readyPlayer(1344);
  env.yt.setState(STATE.PLAYING);
  let updates = 0;
  player.addEventListener('timeupdate', () => updates++);
  env.yt.time = 12.5;
  env.tick();
  assert.strictEqual(player.currentTime, 12.5);
  assert.ok(updates >= 1);
  env.yt.time = 13.5;
  env.tick();
  assert.strictEqual(player.currentTime, 13.5);
});

test('while paused with an unchanged time the poll stays quiet', async () => {
  const { env, player } = await readyPlayer(1344);
  env.yt.setState(STATE.PLAYING);
  env.yt.time = 50; env.tick();
  env.yt.setState(STATE.PAUSED);
  let updates = 0;
  player.addEventListener('timeupdate', () => updates++);
  env.tick(); env.tick();
  assert.strictEqual(updates, 0);
});

test('play and pause events follow the real state changes', async () => {
  const { env, player } = await readyPlayer(1344);
  const seen = [];
  player.addEventListener('play', () => seen.push('play'));
  player.addEventListener('pause', () => seen.push('pause'));
  env.yt.setState(STATE.BUFFERING);
  assert.strictEqual(player.paused, false, 'buffering counts as playing (the user pressed play)');
  env.yt.setState(STATE.PLAYING);
  env.yt.setState(STATE.PAUSED);
  env.yt.setState(STATE.ENDED);          // paused -> ended: no second 'pause'
  assert.deepStrictEqual(seen, ['play', 'pause']);
  assert.strictEqual(player.paused, true);
});

test('a seek while playing is just seekTo; while paused or ended it is followed by pauseVideo', async () => {
  const { env, player } = await readyPlayer(1344);
  env.yt.setState(STATE.PLAYING);
  player.currentTime = 300;
  assert.deepStrictEqual(env.yt.callNames(), ['seekTo']);

  env.yt.calls.length = 0;
  env.yt.setState(STATE.PAUSED);
  player.currentTime = 301;
  assert.deepStrictEqual(env.yt.callNames(), ['seekTo', 'pauseVideo']);

  env.yt.calls.length = 0;
  env.yt.setState(STATE.ENDED);          // seekTo from ENDED would restart playback
  player.currentTime = 10;
  assert.deepStrictEqual(env.yt.callNames(), ['seekTo', 'pauseVideo']);
});

test('a just-issued seek is trusted over the player\'s position until it settles', async () => {
  const { env, player } = await readyPlayer(1344);
  env.yt.setState(STATE.PAUSED);
  env.yt.time = 100;
  player.currentTime = 500;
  env.yt.time = 100;                     // player hasn't moved yet
  env.advance(200); env.tick();
  assert.strictEqual(player.currentTime, 500, 'still inside the hold window');
  env.advance(1000);
  env.yt.time = 500;
  env.tick();
  assert.strictEqual(player.currentTime, 500);
});

test('seeks are clamped to the duration and to zero', async () => {
  const { player } = await readyPlayer(1344);
  player.currentTime = 99999;
  assert.strictEqual(player.currentTime, 1344);
  player.currentTime = -5;
  assert.strictEqual(player.currentTime, 0);
  player.currentTime = 'garbage';
  assert.strictEqual(player.currentTime, 0);
});

test('currentTime changes emit timeupdate immediately, like a <video>', async () => {
  const { player } = await readyPlayer(1344);
  let updates = 0;
  player.addEventListener('timeupdate', () => updates++);
  player.currentTime = 42;
  assert.strictEqual(updates, 1);
});

test('the shield toggles play/pause on click', async () => {
  const { env, player, mount } = await readyPlayer(1344);
  const shield = mount.children.find(c => c.className === 'yt-shield');
  shield.listeners.click[0]();           // paused -> play
  assert.ok(env.yt.callNames().includes('playVideo'));
  env.yt.setState(STATE.PLAYING);
  env.yt.calls.length = 0;
  shield.listeners.click[0]();           // playing -> pause
  assert.deepStrictEqual(env.yt.callNames(), ['pauseVideo']);
});

test('embedding-disabled errors explain the fix and free the shield so YouTube\'s own UI is clickable', async () => {
  for (const code of [101, 150]) {
    const { env, player, mount } = await readyPlayer(1344);
    let errors = 0;
    player.addEventListener('error', () => errors++);
    env.yt.fireError(code);
    assert.strictEqual(errors, 1);
    assert.match(player.errorMessage, /disabled embedding/);
    assert.match(player.errorMessage, /Studio/);
    assert.strictEqual(mount.children.find(c => c.className === 'yt-shield').style.pointerEvents, 'none');
  }
});

test('error 153 blames the missing referrer; unknown codes are reported, not swallowed', async () => {
  const a = await readyPlayer(1344);
  a.env.yt.fireError(153);
  assert.match(a.player.errorMessage, /referrer/);
  const b = await readyPlayer(1344);
  b.env.yt.fireError(999);
  assert.match(b.player.errorMessage, /999/);
});

test('a player that never becomes ready fails with an actionable message', async () => {
  const env = makeEnv({ withYT: false });
  const player = env.createPlayer(env.window.document.createElement('div'),
    { kind: 'youtube', video_id: 'abcdefghijk', duration: 100 });
  let errors = 0;
  player.addEventListener('error', () => errors++);
  const timer = env.timeouts.find(t => t.ms === 15000);
  assert.ok(timer, 'a ready-timeout is armed');
  timer.fn();
  assert.strictEqual(errors, 1);
  assert.match(player.errorMessage, /embedding/);
});

test('playbackRate snaps to the nearest step YouTube offers', async () => {
  const { env, player } = await readyPlayer(1344);
  player.playbackRate = 1.4;
  player.playbackRate = 2;
  player.playbackRate = 0.5;
  assert.deepStrictEqual(env.yt.calls, [['setPlaybackRate', 1.5], ['setPlaybackRate', 2], ['setPlaybackRate', 0.5]]);
});

test('watchUrl links to the playhead, and drops the timestamp at the very start', async () => {
  const { player } = await readyPlayer(1344);
  assert.strictEqual(player.watchUrl(403.7), 'https://www.youtube.com/watch?v=M7lc1UVf-VE&t=403s');
  assert.strictEqual(player.watchUrl(0.5), 'https://www.youtube.com/watch?v=M7lc1UVf-VE');
});

test('hidden toggles the mount', async () => {
  const { player, mount } = await readyPlayer(1344);
  player.hidden = true;
  assert.strictEqual(mount.style.display, 'none');
  assert.strictEqual(player.hidden, true);
  player.hidden = false;
  assert.strictEqual(mount.style.display, '');
});

test('a throwing listener does not stop the others', async () => {
  const { player } = await readyPlayer(1344);
  const quiet = console.error; console.error = () => {};
  let reached = false;
  player.addEventListener('timeupdate', () => { throw new Error('boom'); });
  player.addEventListener('timeupdate', () => { reached = true; });
  player.currentTime = 5;
  console.error = quiet;
  assert.ok(reached);
});

// --------------------------------------------------------------------------

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log('ok   - ' + name);
    } catch (err) {
      failed++;
      console.log('FAIL - ' + name + '\n       ' + (err && err.stack || err).toString().split('\n').join('\n       '));
    }
  }
  console.log('\n' + (tests.length - failed) + '/' + tests.length + ' passed');
  process.exit(failed ? 1 : 0);
})();
