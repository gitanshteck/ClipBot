'use strict';
/* Tests for clipbot/server/static/brief/scenes.js (the stream brief's scene model
 * and on-air state), run under plain Node with no framework and no browser.
 * tests/test_scenes_js.py runs this file as part of
 * `python -m unittest discover -s tests`.
 *
 * scenes.js attaches BriefScenes to `window` and registers one listener on
 * `document` at load, so the script runs in a vm context whose window is the
 * global object and whose document is a stub.
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SOURCE = fs.readFileSync(
  path.join(__dirname, '..', '..', 'clipbot', 'server', 'static', 'brief', 'scenes.js'), 'utf8');

const ctx = { document: { addEventListener() {} } };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(SOURCE, ctx);
const S = ctx.BriefScenes;

// Values built inside the vm context have that realm's prototypes; compare through JSON.
const plain = (x) => JSON.parse(JSON.stringify(x));

const DATE = '2026-11-14';
const BRIEF = {
  date: DATE,
  run: [
    { id: 'starting', t: -5, dur: 5, name: 'Starting soon', kind: 'starting', scene: 'starting', screen: { title: 'tonight', sub: '', prompt: 'say hi' } },
    { id: 'react', t: 0, dur: 10, name: 'React', kind: 'react', scene: 'topic', items: ['d1', 'd2'], screen: { title: 'news', sub: '', prompt: 'thoughts?' } },
    { id: 'pick', t: 10, dur: 5, name: 'Pick', kind: 'transition', scene: 'vote', poll: ['alpha', 'beta'], screen: { title: 'pick one', sub: '', prompt: '1 or 2' } },
    { id: 'chat', t: 15, dur: 5, name: 'Chat', kind: 'talk', scene: 'card', screen: { title: 'chat', sub: '', prompt: 'hi' } },
    { id: 'game', t: 20, dur: 60, name: 'Game', kind: 'game', scene: 'game', screen: { title: 'warzone', sub: '', prompt: 'o/u' } },
  ],
  docket: [
    { id: 'd1', card: 'first card', headline: 'First', source: 'src', date: '14 Nov', question: 'q1' },
    { id: 'd2', card: 'second card', headline: 'Second', source: 'src', date: '14 Nov', question: 'q2' },
  ],
};

let passed = 0;
const failures = [];
function test(name, fn) {
  S.init(BRIEF);
  try { fn(); passed++; } catch (err) { failures.push(name + '\n    ' + (err && err.stack || err)); }
}

// a state that is on, at a segment, started
function on(over) {
  return Object.assign(S.defaultState(DATE), { on: true, seg: 1, startedAt: 1000 }, over || {});
}

test('soundEvents: nothing on first load or when the day changes', () => {
  assert.deepStrictEqual(plain(S.soundEvents(null, on())), []);
  assert.deepStrictEqual(plain(S.soundEvents(on(), null)), []);
  const next = on({ seg: 2, date: '2026-11-15' });
  assert.deepStrictEqual(plain(S.soundEvents(on(), next)), []);
});

test('soundEvents: going on plays start only', () => {
  const prev = on({ on: false });
  assert.deepStrictEqual(plain(S.soundEvents(prev, on({ seg: 0, kills: 4 }))), ['start']);
});

test('soundEvents: nothing when the show is not on', () => {
  const prev = on({ on: false });
  const next = on({ on: false, seg: 3, item: 1, reveal: true, kills: 2, deaths: 1, askShow: true, ask: 'x' });
  assert.deepStrictEqual(plain(S.soundEvents(prev, next)), []);
  assert.deepStrictEqual(plain(S.soundEvents(on(), on({ on: false, seg: 3 }))), []);
});

test('soundEvents: nothing when nothing changed', () => {
  assert.deepStrictEqual(plain(S.soundEvents(on(), on())), []);
});

test('soundEvents: a segment change plays segment, plus vote when the new scene is a vote', () => {
  assert.deepStrictEqual(plain(S.soundEvents(on({ seg: 0 }), on({ seg: 1 }))), ['segment']);
  assert.deepStrictEqual(plain(S.soundEvents(on({ seg: 1 }), on({ seg: 2 }))), ['segment', 'vote']);
  assert.deepStrictEqual(plain(S.soundEvents(on({ seg: 2 }), on({ seg: 3 }))), ['segment']);
});

test('soundEvents: an item change plays card, not segment', () => {
  assert.deepStrictEqual(plain(S.soundEvents(on({ item: 0 }), on({ item: 1 }))), ['card']);
});

test('soundEvents: the lower-third toggle also plays card', () => {
  assert.deepStrictEqual(plain(S.soundEvents(on(), on({ l3only: true }))), ['card']);
});

test('soundEvents: reveal plays only when it turns on', () => {
  assert.deepStrictEqual(plain(S.soundEvents(on({ reveal: false }), on({ reveal: true }))), ['reveal']);
  assert.deepStrictEqual(plain(S.soundEvents(on({ reveal: true }), on({ reveal: false }))), []);
});

test('soundEvents: ask plays when the card turns on or its question changes', () => {
  assert.deepStrictEqual(plain(S.soundEvents(on(), on({ askShow: true, ask: 'a1' }))), ['ask']);
  assert.deepStrictEqual(plain(S.soundEvents(on({ askShow: true, ask: 'a1' }), on({ askShow: true, ask: 'a2' }))), ['ask']);
  assert.deepStrictEqual(plain(S.soundEvents(on({ askShow: true, ask: 'a1' }), on({ askShow: true, ask: 'a1' }))), []);
  assert.deepStrictEqual(plain(S.soundEvents(on({ askShow: true, ask: 'a1' }), on({ askShow: false, ask: 'a1' }))), []);
});

test('soundEvents: kills and deaths play only when they go up', () => {
  assert.deepStrictEqual(plain(S.soundEvents(on({ kills: 1 }), on({ kills: 2 }))), ['kill']);
  assert.deepStrictEqual(plain(S.soundEvents(on({ deaths: 0 }), on({ deaths: 1 }))), ['death']);
  assert.deepStrictEqual(plain(S.soundEvents(on({ kills: 2, deaths: 2 }), on({ kills: 1, deaths: 1 }))), []);
  assert.deepStrictEqual(plain(S.soundEvents(on({ kills: 0, deaths: 0 }), on({ kills: 1, deaths: 1 }))), ['kill', 'death']);
});

test('reduce: vote buttons count up, and shift takes one back', () => {
  let st = on({ seg: 2 });
  st = S.reduce(st, 'vote1', false).state;
  st = S.reduce(st, 'vote1', false).state;
  assert.deepStrictEqual(plain(st.votes), [2, 0]);
  st = S.reduce(st, 'vote1', true).state;
  assert.deepStrictEqual(plain(st.votes), [1, 0]);
  st = S.reduce(st, 'vote2', false).state;
  assert.deepStrictEqual(plain(st.votes), [1, 1]);
  st = S.reduce(st, 'vote3', false).state;
  assert.deepStrictEqual(plain(st.votes), [1, 1, 1]);
});

test('reduce: a vote never goes below zero', () => {
  const st = S.reduce(on({ seg: 2 }), 'vote1', true).state;
  assert.deepStrictEqual(plain(st.votes), [0, 0]);
});

test('reduce: votes reset when the segment changes', () => {
  const r = S.reduce(on({ seg: 2, votes: [3, 4] }), 'next', false);
  assert.strictEqual(r.state.seg, 3);
  assert.deepStrictEqual(plain(r.state.votes), []);
  assert.strictEqual(r.segChanged, true);
});

test('reduce: votes reset when the item changes', () => {
  const r = S.reduce(on({ seg: 1, item: 0, votes: [3, 4] }), 'item', false);
  assert.strictEqual(r.state.item, 1);
  assert.deepStrictEqual(plain(r.state.votes), []);
});

test('reduce: votesreset clears the tallies without moving anything else', () => {
  const r = S.reduce(on({ seg: 2, votes: [3, 4] }), 'votesreset', false);
  assert.deepStrictEqual(plain(r.state.votes), []);
  assert.strictEqual(r.state.seg, 2);
});

test('reduce: mute toggles', () => {
  const a = S.reduce(on(), 'mute', false).state;
  assert.strictEqual(a.mute, true);
  const b = S.reduce(a, 'mute', false).state;
  assert.strictEqual(b.mute, false);
});

test('reduce: does not change the state it was given', () => {
  const st = on({ seg: 2, votes: [1, 1] });
  S.reduce(st, 'vote1', false);
  assert.deepStrictEqual(plain(st.votes), [1, 1]);
});

test('reduce: an unknown control changes nothing', () => {
  assert.strictEqual(S.reduce(on(), 'dance', false), null);
});

// The AI answer card belongs to the card it was shown over.
const shown = (over) => on(Object.assign({ askShow: true, ask: 'a1' }, over || {}));
['next', 'prev', 'goto:3', 'item', 'itemup'].forEach((ctl) => {
  test('reduce: ' + ctl + ' takes the AI answer card down', () => {
    const r = S.reduce(shown({ seg: ctl === 'next' || ctl === 'goto:3' ? 1 : (ctl === 'prev' ? 2 : 1) }), ctl, false);
    assert.ok(r, ctl + ' should change something');
    assert.strictEqual(r.state.askShow, false);
    assert.strictEqual(r.state.ask, 'a1');   // the answer itself is kept, so it can be shown again
  });
});
test('reduce: stop and start take the AI answer card down', () => {
  assert.strictEqual(S.reduce(shown(), 'stop', false).state.askShow, false);
  assert.strictEqual(S.reduce(shown(), 'start', false).state.askShow, false);
});
['reveal', 'kill', 'death', 'pause', 'l3', 'vote1', 'votesreset', 'mute', 'bg'].forEach((ctl) => {
  test('reduce: ' + ctl + ' leaves the AI answer card up', () => {
    const r = S.reduce(shown({ seg: 2 }), ctl, false);
    assert.ok(r, ctl + ' should change something');
    assert.strictEqual(r.state.askShow, true);
    assert.strictEqual(r.state.ask, 'a1');
  });
});
test('reduce: a no-op segment change keeps the AI answer card (goto the current segment, item on a one-item card)', () => {
  assert.strictEqual(S.reduce(shown({ seg: 2 }), 'goto:2', false), null);
  assert.strictEqual(S.reduce(shown({ seg: 2 }), 'item', false), null);
  assert.strictEqual(S.reduce(shown({ seg: 4 }), 'next', false), null);
});
test('reduce: showing an answer after a segment change turns it back on, with the ask sound', () => {
  const hidden = S.reduce(shown(), 'next', false).state;
  const again = S.reduce(hidden, 'ask:a1', false).state;
  assert.strictEqual(again.askShow, true);
  assert.deepStrictEqual(plain(S.soundEvents(hidden, again)), ['ask']);
  assert.deepStrictEqual(plain(S.soundEvents(shown(), hidden)).indexOf('ask'), -1);
});

// fitAsk: a fake card whose height is base * k, to check the scaling search and its last resorts.
function fakeStage(base, limit, lis) {
  const props = {}, classes = new Set(), items = [];
  for (let i = 0; i < (lis || 3); i++) items.push({ style: { display: '' } });
  const card = {
    style: { maxHeight: '', setProperty(k, v) { props[k] = v; } },
    classList: { add(c) { classes.add(c); } },
    get offsetHeight() {
      const k = parseFloat(props['--k'] || '1');
      const hidden = items.filter((x) => x.style.display === 'none').length;
      const fixed = classes.has('tight') ? 0.8 : 1;
      return base * k * fixed * (1 - hidden * 0.2);
    },
    querySelectorAll() { return items; },
  };
  ctx.getComputedStyle = () => ({ getPropertyValue: () => String(limit) + 'px' });
  const stage = { className: '', innerHTML: '', querySelector: (sel) => (sel === '.oc-ask' ? card : null) };
  return { stage, props, classes, items };
}
test('fitAsk: a card that fits keeps full size', () => {
  const f = fakeStage(300, 400);
  S.paintAsk(f.stage, { id: 'a1', q: 'q', head: 'h', points: [], done: true }, { frame: 'chat', bg: 'clear' });
  assert.strictEqual(f.props['--k'], '1');
});
test('fitAsk: a too-tall card scales down until it fits, and not further', () => {
  const f = fakeStage(800, 400);
  S.paintAsk(f.stage, { id: 'a1', q: 'q', head: 'h', points: ['x'], done: true }, { frame: 'chat', bg: 'clear' });
  const k = parseFloat(f.props['--k']);
  assert.ok(k >= 0.5 && k <= 0.5 + 1e-9 || (k > 0.49 && k < 0.51), 'k=' + k);
  const g = fakeStage(600, 400);
  S.paintAsk(g.stage, { id: 'a1', q: 'q', head: 'h', points: ['x'], done: true }, { frame: 'chat', bg: 'clear' });
  const k2 = parseFloat(g.props['--k']);
  assert.ok(k2 <= 400 / 600 + 1e-9 && k2 > 0.64, 'k=' + k2);
});
test('fitAsk: while an answer streams the size only goes down', () => {
  const f = fakeStage(600, 400);
  S.paintAsk(f.stage, { id: 'a1', q: 'q', head: 'h', done: false }, { frame: 'chat', bg: 'clear' });
  const k1 = parseFloat(f.props['--k']);
  const h = fakeStage(300, 400);
  h.stage._askId = 'a1'; h.stage._askK = k1;
  S.paintAsk(h.stage, { id: 'a1', q: 'q', head: 'h2', done: false }, { frame: 'chat', bg: 'clear' });
  assert.ok(parseFloat(h.props['--k']) <= k1 + 1e-9);
  const other = fakeStage(300, 400);
  other.stage._askId = 'old'; other.stage._askK = 0.6;
  S.paintAsk(other.stage, { id: 'a2', q: 'q', head: 'h', done: true }, { frame: 'chat', bg: 'clear' });
  assert.strictEqual(other.props['--k'], '1');   // a new answer starts from full size
});
test('fitAsk: last resorts clamp, then drop riff points, never leave it overflowing silently', () => {
  const f = fakeStage(2000, 400, 3);
  S.paintAsk(f.stage, { id: 'a1', q: 'q', head: 'h', points: ['a', 'b', 'c'], done: true }, { frame: 'chat', bg: 'clear' });
  assert.ok(f.classes.has('tight'));
  assert.ok(f.items.some((x) => x.style.display === 'none'));
});

test('tallyOf: percentages and the leader', () => {
  const st = { votes: [2, 1] };
  assert.deepStrictEqual(plain(S.tallyOf(st, 0)), { n: 2, pct: 67, lead: true });
  assert.deepStrictEqual(plain(S.tallyOf(st, 1)), { n: 1, pct: 33, lead: false });
  assert.deepStrictEqual(plain(S.tallyOf(st, 2)), { n: 0, pct: 0, lead: false });
});

test('tallyOf: no votes means no tally, and an empty count has no leader', () => {
  assert.strictEqual(S.tallyOf({ votes: [] }, 0), null);
  assert.strictEqual(S.tallyOf({}, 0), null);
  assert.strictEqual(S.tallyOf(null, 0), null);
  assert.deepStrictEqual(plain(S.tallyOf({ votes: [0, 0] }, 0)), { n: 0, pct: 0, lead: false });
});

test('tallyOf: a tie leads for both', () => {
  const st = { votes: [1, 1] };
  assert.strictEqual(S.tallyOf(st, 0).lead, true);
  assert.strictEqual(S.tallyOf(st, 1).lead, true);
});

test('sceneHTML: a vote segment draws tally bars and marks the leader', () => {
  const st = on({ seg: 2, votes: [2, 1] });
  const html = S.sceneHTML(BRIEF.run[2], st).html;
  assert.ok(html.includes('<i style="width:67%"></i>'), html);
  assert.ok(html.includes('<i style="width:33%"></i>'), html);
  const first = html.indexOf('class="opt lead"');
  const second = html.indexOf('class="opt"');
  assert.ok(first !== -1, 'first option should carry lead');
  assert.ok(second !== -1 && first < second, 'only the first option should carry lead');
  assert.ok(html.includes('<span class="vn">2</span>'));
});

test('sceneHTML: poll chips on a topic card carry the live counts', () => {
  const brief = Object.assign({}, BRIEF, {
    docket: [Object.assign({}, BRIEF.docket[0], { poll: ['yes', 'no'] }), BRIEF.docket[1]],
  });
  S.init(brief);
  const html = S.sceneHTML(brief.run[1], on({ seg: 1, item: 0, votes: [1, 3] })).html;
  assert.ok(html.includes('<em>1</em>') && html.includes('<em>3</em>'), html);
  assert.ok(html.includes('<span class="o"><b>1</b>yes'), 'only the second chip leads');
  assert.ok(html.includes('<span class="o lead"><b>2</b>no'));
});

test('sceneHTML: a vote segment without votes draws no tally', () => {
  const html = S.sceneHTML(BRIEF.run[2], on({ seg: 2 })).html;
  assert.ok(!html.includes('class="vt"'));
  assert.ok(!html.includes('lead'));
});

test('lookOf: segment look overrides the brief look, tag survives', () => {
  S.init(Object.assign({}, BRIEF, { look: { theme: 'hud', tag: 'WARZONE' } }));
  assert.deepStrictEqual(plain(S.lookOf({})), { theme: 'hud', tag: 'WARZONE' });
  assert.deepStrictEqual(plain(S.lookOf({ look: { theme: 'slash' } })), { theme: 'slash', tag: 'WARZONE' });
  S.init(BRIEF);
  assert.deepStrictEqual(plain(S.lookOf(null)), { theme: 'clean', tag: '' });
});

test('linksOf: item links first, then its source, then segment links, de-duplicated and capped', () => {
  S.init(BRIEF);
  const it = { url: 'https://a.example/src', source: 'Dexerto', links: [
    { label: 'Trailer', url: 'https://y.example/t', kind: 'trailer' },
    { label: 'Same again', url: 'https://a.example/src', kind: 'article' } ] };
  const seg = { links: [ { label: 'Seg link', url: 'https://s.example/x', kind: 'weird' } ] };
  const got = plain(S.linksOf(seg, it));
  assert.deepStrictEqual(got.map((l) => l.url), ['https://y.example/t', 'https://a.example/src', 'https://s.example/x']);
  assert.strictEqual(got[0].icon, '▶');
  assert.strictEqual(got[1].label, 'Same again');      // first mention wins
  assert.strictEqual(got[2].kind, 'article');          // unknown kinds fall back
  assert.deepStrictEqual(plain(S.linksOf(null, null)), []);
  const many = { links: Array.from({ length: 9 }, (_, i) => ({ label: 'l' + i, url: 'https://m.example/' + i })) };
  assert.strictEqual(S.linksOf(many, null).length, 6);
});

if (failures.length) {
  console.error(failures.length + ' failed, ' + passed + ' passed');
  failures.forEach((f) => console.error('  FAIL ' + f));
  process.exit(1);
}
console.log(passed + ' passed');
