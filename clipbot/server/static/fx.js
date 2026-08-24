/* Clip effects: the FX lane, the library drawer, presets, and proxy preview.
 *
 * Effects live inside the reel spec (`spec.fx`), which reel.js already owns and
 * already saves. So this file never talks to the clip PATCH route itself - it
 * mutates the same object and calls window.reelFx.dirty(), and Save / Apply to
 * all / the unsaved badge all keep working untouched.
 *
 * Times are absolute VOD seconds - the same clock as the video element and both
 * timelines - so placing an effect at the playhead, and drawing it on the lane
 * under #tl-zoom, both need no conversion at all.
 */

let fxIndex = { assets: [], missing: [], counts: {} };
let fxPresets = [];
let fxSelected = null;      // id of the effect being inspected
let fxDefaults = {};        // reel.fx.* echoed by /reel/plan
let fxWarnings = [];
let fxPreview = { key: null, busy: false, started: 0, timer: null };
let fxDrawerKind = 'sfx';
let fxDrawerQuery = '';
let textStylesCache = null;  // saved library text styles, for the text.style picker

const FX_LABELS = {
  punch: '⤢ Punch', shake: '≋ Shake', flash: '✦ Flash', freeze: '❚❚ Freeze end',
  speed: '⏩ Speed', sfx: '🔊 Sound', music: '♫ Music bed',
  sticker: '🖼 Sticker', text: 'T Text',
};

// Effects with no place on the timeline: they apply to the whole clip.
const FX_WHOLE = ['freeze', 'speed', 'music'];

/* ---------------- helpers ---------------- */

function fxSpec() { return window.reelFx.spec(); }
function fxAll() { const s = fxSpec(); return s.fx || (s.fx = []); }
function fxById(id) { return fxAll().find(e => e.id === id) || null; }
function fxAsset(id) { return fxIndex.assets.find(a => a.id === id) || null; }

function fxNewId() {
  // Short, stable and unique within a clip; it survives a save so the inspector
  // can reselect the same pill afterwards.
  let id;
  do { id = 'f' + Math.random().toString(36).slice(2, 8); } while (fxById(id));
  return id;
}

function fxTouched() {
  window.reelFx.dirty();
  window.reelFx.replan(true);
  drawFxStrip();
  renderFxInspector();
}

/* ---------------- adding and editing ---------------- */

function addFx(type) {
  const list = fxAll();
  const limit = (fxDefaults.max_effects || 24);
  if (list.length >= limit) { toast(`Limit is ${limit} effects per clip`, true); return; }
  if (FX_WHOLE.includes(type) && type !== 'sfx' && list.some(e => e.type === type)
      && (type === 'speed' || type === 'music')) {
    toast(`This clip already has a ${type} effect`, true);
    return;
  }
  if (type === 'sfx' || type === 'music' || type === 'sticker') {
    openFxLibrary(type);   // pick the asset first; the effect is made on click
    return;
  }

  const at = window.player ? window.player.currentTime : 0;
  const d = fxDefaults[type] || {};
  const eff = { id: fxNewId(), type };
  if (!FX_WHOLE.includes(type)) eff.at = +at.toFixed(3);

  if (type === 'punch') { eff.dur = d.duration || 0.45; eff.amount = d.amount || 1.18; }
  if (type === 'shake') {
    eff.dur = d.duration || 0.6; eff.amount = d.amount_px || 8;
    eff.freq = d.freq_hz || 12; eff.decay = d.decay !== false;
  }
  if (type === 'flash') {
    eff.dur = d.duration || 0.12; eff.amount = d.amount || 0.55;
    eff.style = d.style || 'exposure';
  }
  if (type === 'freeze') eff.dur = d.duration || 0.8;
  if (type === 'speed') eff.rate = 1.25;
  if (type === 'text') {
    eff.dur = 3.0; eff.x = 0.5; eff.y = 0.14; eff.text = 'HOOK';
  }

  fxAll().push(eff);
  fxSelected = eff.id;
  fxTouched();
}
window.addFx = addFx;

function addAssetFx(type, assetId) {
  const at = window.player ? window.player.currentTime : 0;
  const d = fxDefaults[type] || {};
  const eff = { id: fxNewId(), type, asset: assetId };
  if (type === 'sfx') { eff.at = +at.toFixed(3); eff.gain_db = d.gain_db != null ? d.gain_db : -3; }
  if (type === 'music') { eff.gain_db = d.gain_db != null ? d.gain_db : -18; }
  if (type === 'sticker') {
    eff.at = +at.toFixed(3);
    eff.dur = 2.0; eff.w = d.width || 0.3; eff.x = 0.62; eff.y = 0.15;
    eff.fade = d.fade != null ? d.fade : 0.15;
  }
  if (type === 'music' && fxAll().some(e => e.type === 'music')) {
    toast('This clip already has a music bed', true);
    return;
  }
  fxAll().push(eff);
  fxSelected = eff.id;
  closeFxLibrary();
  fxTouched();
}
window.addAssetFx = addAssetFx;

function setFxField(id, field, value) {
  const eff = fxById(id);
  if (!eff) return;
  eff[field] = value;
  fxTouched();
}
window.setFxField = setFxField;

// duck is a sub-object (fxspec.py's DEFAULT_DUCK), not a flat field - setFxField
// can't reach into it, and this is the only nested-field case in the whole
// inspector, so a small dedicated setter is simpler than teaching setFxField
// dot-paths for one caller.
function setFxDuckField(id, field, value) {
  const eff = fxById(id);
  if (!eff) return;
  eff.duck = eff.duck || { enabled: true, threshold: 0.05, ratio: 8.0, attack: 20.0, release: 300.0 };
  eff.duck[field] = value;
  fxTouched();
}
window.setFxDuckField = setFxDuckField;

async function loadTextStylesCache() {
  if (textStylesCache) return textStylesCache;
  try {
    // load_text_styles returns {styles: {style_id: {...}}} - a dict, not a
    // list - text.style only ever references one by name (string), so the
    // ids are all this picker needs.
    const data = await api('/api/library/textstyles');
    textStylesCache = Object.keys(data.styles || {});
  } catch (e) {
    textStylesCache = [];
  }
  return textStylesCache;
}

function removeFx(id) {
  const s = fxSpec();
  s.fx = fxAll().filter(e => e.id !== id);
  if (!s.fx.length) delete s.fx;
  if (fxSelected === id) fxSelected = null;
  fxTouched();
}
window.removeFx = removeFx;

function toggleFx(id) {
  const eff = fxById(id);
  if (!eff) return;
  if (eff.enabled === false) delete eff.enabled; else eff.enabled = false;
  fxTouched();
}
window.toggleFx = toggleFx;

function selectFx(id) {
  fxSelected = fxSelected === id ? null : id;
  drawFxStrip();
  renderFxInspector();
}
window.selectFx = selectFx;

/* ---------------- the lane ---------------- */

// Rebuilds the lane's DOM. Never call this from a pointermove: it replaces the
// pills wholesale, and the browser drops pointer capture the instant a captured
// element leaves the DOM - the same trap documented in reel.js's crop drag.
function drawFxStrip() {
  const lane = document.getElementById('fx-lane');
  const block = document.getElementById('fx-lane-block');
  if (!lane || !block) return;
  const clip = window.selectedClip ? window.selectedClip() : null;
  if (!clip || !window.zoomWindow) { block.hidden = true; return; }
  block.hidden = false;

  const w = window.zoomWindow();
  const span = Math.max(0.001, w.to - w.from);
  const timed = fxAll().filter(e => !FX_WHOLE.includes(e.type));
  const whole = fxAll().filter(e => FX_WHOLE.includes(e.type));

  lane.innerHTML = timed.map(eff => {
    const left = ((eff.at - w.from) / span) * 100;
    const width = Math.max(1.2, ((eff.dur || 0.25) / span) * 100);
    const off = eff.enabled === false ? ' off' : '';
    const sel = fxSelected === eff.id ? ' sel' : '';
    const bad = fxWarnings.some(x => x.startsWith(eff.id)) ? ' bad' : '';
    return `<div class="fx-pill ${eff.type}${off}${sel}${bad}" data-id="${eff.id}"
                 style="left:${left}%;width:${width}%"
                 title="${esc(fxTitle(eff))}">
              <span class="fx-cap">${FX_LABELS[eff.type] || eff.type}</span>
              <i class="fx-grip r"></i>
            </div>`;
  }).join('');

  document.getElementById('fx-whole').innerHTML = whole.map(eff =>
    `<button class="fx-chip ${eff.enabled === false ? 'off' : ''}${fxSelected === eff.id ? ' sel' : ''}"
             onclick="selectFx('${eff.id}')">${FX_LABELS[eff.type]}${fxChipDetail(eff)}</button>`
  ).join('') || '<span class="meta">none</span>';

  const warn = document.getElementById('fx-warnings');
  if (warn) {
    warn.innerHTML = fxWarnings.map(w2 => `<div class="fx-warn">⚠ ${esc(w2)}</div>`).join('');
  }
  bindFxDrag();
}
window.drawFxStrip = drawFxStrip;

function fxChipDetail(eff) {
  if (eff.type === 'speed') return ` ${eff.rate}×`;
  if (eff.type === 'freeze') return ` ${eff.dur}s`;
  if (eff.type === 'music') {
    const a = fxAsset(eff.asset);
    return ' · ' + (a ? esc(a.name) : 'missing');
  }
  return '';
}

function fxTitle(eff) {
  const bits = [FX_LABELS[eff.type] || eff.type, formatClock(eff.at)];
  if (eff.asset) { const a = fxAsset(eff.asset); bits.push(a ? a.name : 'MISSING ASSET'); }
  if (eff.text) bits.push(eff.text);
  return bits.join(' · ');
}

// Move (drag the body) or retime (drag the right grip). Only styles are touched
// while a gesture is live, so pointer capture survives it - see drawFxStrip.
function bindFxDrag() {
  const lane = document.getElementById('fx-lane');
  if (!lane) return;
  lane.querySelectorAll('.fx-pill').forEach(el => {
    let mode = null, startX = 0, startAt = 0, startDur = 0, eff = null, span = 1, laneW = 1;

    el.addEventListener('pointerdown', (ev) => {
      eff = fxById(el.dataset.id);
      if (!eff) return;
      ev.preventDefault();
      selectFx(eff.id);
      // selectFx rebuilds the lane, so re-find the live node before capturing.
      const live = document.querySelector(`.fx-pill[data-id="${eff.id}"]`);
      const target = live || el;
      mode = ev.target.classList.contains('fx-grip') ? 'resize' : 'move';
      startX = ev.clientX; startAt = eff.at; startDur = eff.dur || 0.25;
      const w = window.zoomWindow();
      span = Math.max(0.001, w.to - w.from);
      laneW = lane.getBoundingClientRect().width || 1;
      try { target.setPointerCapture(ev.pointerId); } catch (e) {}
      target.classList.add('drag');
      target._fx = { mode, startX, startAt, startDur, span, laneW, eff };
    });

    const move = (ev) => {
      const st = ev.currentTarget._fx;
      if (!st || !st.mode) return;
      const dt = ((ev.clientX - st.startX) / st.laneW) * st.span;
      if (st.mode === 'move') {
        st.eff.at = +Math.max(0, st.startAt + dt).toFixed(3);
      } else {
        st.eff.dur = +Math.max(0.05, st.startDur + dt).toFixed(3);
      }
      const w = window.zoomWindow();
      ev.currentTarget.style.left = (((st.eff.at - w.from) / st.span) * 100) + '%';
      ev.currentTarget.style.width =
        Math.max(1.2, ((st.eff.dur || 0.25) / st.span) * 100) + '%';
    };

    const up = (ev) => {
      const st = ev.currentTarget._fx;
      if (!st || !st.mode) return;
      st.mode = null;
      ev.currentTarget.classList.remove('drag');
      try { ev.currentTarget.releasePointerCapture(ev.pointerId); } catch (e) {}
      fxTouched();
    };

    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
  });
}

/* ---------------- inspector ---------------- */

function num(id, field, value, min, max, step) {
  return `<label class="fx-field"><span>${field}</span>
    <input type="number" value="${value}" min="${min}" max="${max}" step="${step}"
           onchange="setFxField('${id}','${field}',+this.value)"></label>`;
}

// fxspec.py's RANGES bounds "dur" per-type, not with one shared range - a
// punch/shake/flash/freeze value the server would reject at 400 could
// previously look perfectly in-range in this UI (0.02-60 for everything).
const DUR_RANGES = {
  punch: [0.05, 5.0],
  shake: [0.05, 5.0],
  flash: [0.02, 2.0],
  freeze: [0.05, 3.0],
  sticker: [0.05, 60.0],
  text: [0.05, 60.0],
};

function renderFxInspector() {
  const host = document.getElementById('fx-inspector');
  if (!host) return;
  const eff = fxSelected ? fxById(fxSelected) : null;
  if (!eff) {
    host.innerHTML = '<span class="meta">Select an effect to edit it.</span>';
    return;
  }
  const id = eff.id;
  let body = '';

  if (eff.at != null) body += num(id, 'at', eff.at, 0, 99999, 0.05);
  if (eff.dur != null) {
    const [durMin, durMax] = DUR_RANGES[eff.type] || [0.02, 60];
    body += num(id, 'dur', eff.dur, durMin, durMax, 0.05);
  }

  if (eff.type === 'punch') {
    body += num(id, 'amount', eff.amount, 1.02, 2, 0.01);
    body += `<label class="fx-field"><span>ease</span>
      <select onchange="setFxField('${id}','ease',this.value)">
        <option value="step" ${eff.ease !== 'smooth' ? 'selected' : ''}>step</option>
        <option value="smooth" ${eff.ease === 'smooth' ? 'selected' : ''}>smooth</option>
      </select></label>`;
  }
  if (eff.type === 'shake') {
    body += num(id, 'amount', eff.amount, 1, 40, 1) + num(id, 'freq', eff.freq, 1, 30, 1);
    body += `<label class="fx-field"><span>decay</span>
      <input type="checkbox" ${eff.decay !== false ? 'checked' : ''}
             onchange="setFxField('${id}','decay',this.checked)"></label>`;
  }
  if (eff.type === 'flash') {
    body += num(id, 'amount', eff.amount, 0.05, 1, 0.05);
    body += `<label class="fx-field"><span>style</span>
      <select onchange="setFxField('${id}','style',this.value)">
        <option value="exposure" ${eff.style === 'exposure' ? 'selected' : ''}>exposure (fades)</option>
        <option value="white" ${eff.style === 'white' ? 'selected' : ''}>white (hard cut)</option>
      </select></label>`;
  }
  if (eff.type === 'speed') body += num(id, 'rate', eff.rate, 0.5, 2, 0.05);
  if (eff.type === 'sfx' || eff.type === 'music') {
    body += num(id, 'gain_db', eff.gain_db, -40, 12, 0.5);
  }
  if (eff.type === 'music') {
    const duck = eff.duck || { enabled: true, threshold: 0.05, ratio: 8.0, attack: 20.0, release: 300.0 };
    body += `<div class="hint" style="margin-top:2px">ducking — lowers this track while there's speech</div>`;
    body += `<label class="fx-field"><span>duck</span>
      <input type="checkbox" ${duck.enabled !== false ? 'checked' : ''}
             onchange="setFxDuckField('${id}','enabled',this.checked)"></label>`;
    body += `<label class="fx-field"><span>threshold</span>
      <input type="number" value="${duck.threshold}" min="0.001" max="1" step="0.01"
             onchange="setFxDuckField('${id}','threshold',+this.value)"></label>`;
    body += `<label class="fx-field"><span>ratio</span>
      <input type="number" value="${duck.ratio}" min="1" max="20" step="0.5"
             onchange="setFxDuckField('${id}','ratio',+this.value)"></label>`;
    body += `<label class="fx-field"><span>attack (ms)</span>
      <input type="number" value="${duck.attack}" min="0.01" max="2000" step="1"
             onchange="setFxDuckField('${id}','attack',+this.value)"></label>`;
    body += `<label class="fx-field"><span>release (ms)</span>
      <input type="number" value="${duck.release}" min="0.01" max="9000" step="1"
             onchange="setFxDuckField('${id}','release',+this.value)"></label>`;
  }
  if (eff.type === 'sticker') {
    body += num(id, 'w', eff.w, 0.02, 1, 0.01) + num(id, 'x', eff.x, 0, 1, 0.01)
          + num(id, 'y', eff.y, 0, 1, 0.01) + num(id, 'fade', eff.fade, 0, 2, 0.05)
          + num(id, 'opacity', eff.opacity != null ? eff.opacity : 1.0, 0.05, 1, 0.05);
  }
  if (eff.type === 'text') {
    body += `<label class="fx-field wide"><span>text</span>
      <input type="text" value="${esc(eff.text || '')}" maxlength="120"
             onchange="setFxField('${id}','text',this.value)"></label>`;
    body += num(id, 'x', eff.x, 0, 1, 0.01) + num(id, 'y', eff.y, 0, 1, 0.01);
    // text.style can be an inline object too (fxspec.py's normalize_text_style),
    // but that's a much bigger editor (font/size/color/border/shadow/uppercase) -
    // this covers the common case of picking a name already saved from the
    // Library page's style editor. A style set as an inline object elsewhere
    // shows as "(custom)" here rather than silently losing it.
    const styleIsString = typeof eff.style === 'string' || eff.style == null;
    const styleOptions = (textStylesCache || []).map(s =>
      `<option value="${esc(s)}" ${eff.style === s ? 'selected' : ''}>${esc(s)}</option>`
    ).join('');
    body += `<label class="fx-field"><span>style</span>
      <select onchange="setFxField('${id}','style',this.value || null)" ${styleIsString ? '' : 'disabled'}>
        <option value="">(default)</option>
        ${styleOptions}
        ${styleIsString ? '' : '<option value="" selected>(custom, edit via clips.json)</option>'}
      </select></label>`;
    body += `<div class="hint">Straight apostrophes are rejected by ffmpeg —
             use ’. Colour emoji can't be drawn as text; use a sticker.</div>`;
  }
  if (eff.asset) {
    const a = fxAsset(eff.asset);
    body += `<div class="hint">asset: ${a ? esc(a.name) : '<b>missing from the library</b>'}</div>`;
  }

  host.innerHTML = `
    <div class="fx-inspector-head">
      <b>${FX_LABELS[eff.type] || eff.type}</b>
      <span class="spacer" style="flex:1"></span>
      <button onclick="toggleFx('${id}')">${eff.enabled === false ? 'enable' : 'disable'}</button>
      <button onclick="removeFx('${id}')">remove</button>
    </div>
    <div class="fx-fields">${body}</div>`;
}

/* ---------------- library drawer ---------------- */

async function loadFxLibrary(rescan) {
  try {
    fxIndex = rescan
      ? await api('/api/library/rescan', 'POST', {})
      : await api('/api/library');
    if (rescan) toast(`Library: ${fxSummary()}`);
  } catch (e) { toast(e.message, true); }
  renderFxDrawer();
  drawFxStrip();
}
window.loadFxLibrary = loadFxLibrary;

function fxSummary() {
  const c = fxIndex.counts || {};
  return Object.keys(c).map(k => `${c[k]} ${k}`).join(', ') || 'empty';
}

function openFxLibrary(kind) {
  fxDrawerKind = kind === 'sticker' ? 'sticker' : (kind || 'sfx');
  renderFxDrawer();
  const dlg = document.getElementById('fx-library');
  if (dlg && !dlg.open) dlg.showModal();
}
window.openFxLibrary = openFxLibrary;

function closeFxLibrary() {
  const dlg = document.getElementById('fx-library');
  if (dlg && dlg.open) dlg.close();
}
window.closeFxLibrary = closeFxLibrary;

function setFxDrawerKind(kind) { fxDrawerKind = kind; renderFxDrawer(); }
window.setFxDrawerKind = setFxDrawerKind;

function setFxDrawerQuery(q) { fxDrawerQuery = (q || '').toLowerCase(); renderFxDrawer(); }
window.setFxDrawerQuery = setFxDrawerQuery;

// Where to go looking, by kind. Kept short and vetted rather than
// exhaustive - a link that turns out to require an account or a paid
// tier is worse than no link, since it wastes the click it saved.
const FX_SOURCES = {
  sfx: [
    { name: 'Pixabay Sound Effects', url: 'https://pixabay.com/sound-effects/search/airhorn%20boom%20whoosh/',
      note: 'CC0, no login. Search: airhorn, vine boom, bruh, bonk, record scratch, sad trombone.' },
    { name: 'Freesound.org', url: 'https://freesound.org/search/?q=impact&f=license%3A%22Creative+Commons+0%22',
      note: 'Filter results to License: Creative Commons 0 before downloading.' },
    { name: 'Mixkit Sound Effects', url: 'https://mixkit.co/free-sound-effects/',
      note: 'No attribution required. Sound Effects section only - music/video differ.' },
  ],
  music: [
    { name: 'Pixabay Music', url: 'https://pixabay.com/music/search/lofi%20beat/',
      note: 'CC0 beds; search "lofi", "hype", "background" for loopable tracks.' },
    { name: 'Freesound.org', url: 'https://freesound.org/search/?q=loop&f=license%3A%22Creative+Commons+0%22',
      note: 'Filter to CC0. Search "loop" for anything designed to repeat cleanly.' },
  ],
  sticker: [
    { name: 'OpenMoji', url: 'https://openmoji.org/library/',
      note: 'CC-BY-SA, transparent PNG/SVG. Not memey, but genuinely free.' },
    { name: 'Twemoji', url: 'https://twemoji.twitter.com/',
      note: 'CC-BY, transparent PNG. Same caveat as OpenMoji.' },
  ],
  font: [
    { name: 'Google Fonts', url: 'https://fonts.google.com/?category=Display&sort=popularity',
      note: 'OFL-licensed .ttf, safe to redistribute. Filter to Display for hook-text weight.' },
  ],
};

function renderFxSources() {
  const host = document.getElementById('fx-sources');
  if (!host) return;
  const list = FX_SOURCES[fxDrawerKind] || [];
  host.innerHTML = `<div class="fx-sources-label">Find more ${fxDrawerKind}:</div>` +
    list.map(s => `<a href="${s.url}" target="_blank" rel="noopener noreferrer"
                       class="fx-source" title="${esc(s.note)}">${esc(s.name)} ↗</a>`).join('');
}

function renderFxDrawer() {
  const host = document.getElementById('fx-assets');
  if (!host) return;
  renderFxSources();
  const kinds = document.getElementById('fx-kinds');
  if (kinds) {
    kinds.innerHTML = ['sfx', 'music', 'sticker', 'font'].map(k =>
      `<button class="${k === fxDrawerKind ? 'on' : ''}" onclick="setFxDrawerKind('${k}')">
         ${k} <span class="meta">${(fxIndex.counts || {})[k] || 0}</span></button>`).join('');
  }

  const missing = (fxIndex.missing || []).length;
  const badge = document.getElementById('fx-missing');
  if (badge) {
    badge.textContent = missing ? `${missing} missing` : '';
    badge.className = missing ? 'badge warn' : 'badge';
  }

  const rows = (fxIndex.assets || []).filter(a =>
    a.kind === fxDrawerKind &&
    (!fxDrawerQuery || a.name.toLowerCase().includes(fxDrawerQuery) ||
     (a.tags || []).some(t => t.includes(fxDrawerQuery))));

  if (!rows.length) {
    host.innerHTML = `<div class="hint">Nothing here yet. Drop files into
      <span class="mono">work/_library/${fxDrawerKind === 'sticker' ? 'stickers' : fxDrawerKind}/</span>
      and hit Rescan.</div>`;
    return;
  }

  host.innerHTML = rows.map(a => {
    const url = `/media/library/${a.kind}/${encodeURIComponent(a.rel.split('/').slice(1).join('/'))}`;
    const audition = (a.kind === 'sfx' || a.kind === 'music')
      ? `<audio controls preload="none" src="${url}"></audio>`
      : (a.kind === 'sticker' ? `<img src="${url}" alt="">` : '<span class="meta">font</span>');
    const detail = a.duration ? `${a.duration.toFixed(2)}s`
      : (a.width ? `${a.width}×${a.height}${a.animated ? ' animated' : ''}` : '');
    const target = a.kind === 'font' ? null
      : (a.kind === 'music' ? 'music' : (a.kind === 'sticker' ? 'sticker' : 'sfx'));
    return `<div class="fx-asset">
      <div class="fx-asset-main">
        <b>${esc(a.name)}</b>
        <span class="meta">${detail} ${(a.tags || []).map(esc).join(' ')}</span>
      </div>
      <div class="fx-asset-play">${audition}</div>
      ${target ? `<button onclick="addAssetFx('${target}','${a.id}')">+ add</button>` : ''}
    </div>`;
  }).join('');
}

/* ---------------- presets ---------------- */

async function loadFxPresets() {
  try {
    const r = await api('/api/library/presets');
    fxPresets = r.presets || [];
  } catch (e) { fxPresets = []; }
  renderFxPresets();
}

function renderFxPresets() {
  const host = document.getElementById('fx-presets');
  if (!host) return;
  host.innerHTML = fxPresets.map(p =>
    `<button class="fx-chip" onclick="applyFxPreset('${p.id}')"
             title="${p.effects.length} effect(s), anchored at ${p.anchor}">${esc(p.name)}</button>`
  ).join('') || '<span class="meta">no saved chains yet</span>';
}

function applyFxPreset(presetId) {
  const preset = fxPresets.find(p => p.id === presetId);
  if (!preset) return;
  const clip = window.selectedClip ? window.selectedClip() : null;
  if (!clip) { toast('Select a clip first', true); return; }

  // A preset stores offsets from an anchor, never absolute times - that single
  // substitution is what makes it portable between clips and between streams.
  let anchor = window.player ? window.player.currentTime : 0;
  if (preset.anchor === 'clip_start') anchor = clip.start;
  if (preset.anchor === 'clip_end') anchor = clip.end;

  let missing = 0;
  preset.effects.forEach(src => {
    const eff = Object.assign({}, src);
    const offset = eff.offset || 0;
    delete eff.offset;
    eff.id = fxNewId();
    if (!FX_WHOLE.includes(eff.type)) eff.at = +(anchor + offset).toFixed(3);
    if (eff.asset && !fxAsset(eff.asset)) {
      // Land it disabled rather than dropping it or crashing: the user can see
      // exactly what the preset wanted and fix the library.
      eff.enabled = false;
      missing++;
    }
    fxAll().push(eff);
  });
  fxTouched();
  toast(missing
    ? `Applied "${preset.name}" — ${missing} effect(s) disabled, assets missing`
    : `Applied "${preset.name}"`);
}
window.applyFxPreset = applyFxPreset;

async function saveFxPreset() {
  const list = fxAll();
  if (!list.length) { toast('No effects to save', true); return; }
  const name = prompt('Name this effect chain (reusable across every stream):');
  if (!name) return;

  const timed = list.filter(e => e.at != null).map(e => e.at);
  const anchorTime = timed.length ? Math.min(...timed)
    : (window.player ? window.player.currentTime : 0);
  const effects = list.map(e => {
    const out = Object.assign({}, e);
    delete out.id;
    out.offset = out.at != null ? +(out.at - anchorTime).toFixed(3) : 0;
    delete out.at;
    return out;
  });

  try {
    await api('/api/library/presets', 'POST', { name, effects, anchor: 'at', overwrite: true });
    await loadFxPresets();
    toast(`Saved "${name}" — apply it on any clip in any stream`);
  } catch (e) { toast(e.message, true); }
}
window.saveFxPreset = saveFxPreset;

/* ---------------- proxy preview ---------------- */

function setFxPreviewMode(mode) {
  document.getElementById('reel-canvas').hidden = mode !== 'geometry';
  document.getElementById('fx-preview').hidden = mode === 'geometry';
  document.querySelectorAll('#fx-preview-modes button').forEach(b =>
    b.classList.toggle('on', b.dataset.mode === mode));
  if (mode === 'effects' && !fxPreview.key) renderFxPreview();
}
window.setFxPreviewMode = setFxPreviewMode;

async function renderFxPreview() {
  const clip = window.selectedClip ? window.selectedClip() : null;
  if (!clip) { toast('Select a clip first', true); return; }
  const status = document.getElementById('fx-preview-status');
  try {
    const r = await api(`/api/workspaces/${SLUG}/reel/preview`, 'POST',
                        { clip_id: clip.id, spec: fxSpec() });
    fxPreview.key = r.key;
    if (r.status === 'ready') { showFxPreview(r.url, r.cached ? 'cached' : 'done'); return; }
    fxPreview.busy = true;
    fxPreview.started = Date.now();
    clearInterval(fxPreview.timer);
    fxPreview.timer = setInterval(() => {
      if (!fxPreview.busy) return clearInterval(fxPreview.timer);
      const secs = (Date.now() - fxPreview.started) / 1000;
      status.textContent = `rendering ${secs.toFixed(1)}s…`;
      // The finish notification is a push over SSE; if that message ever gets
      // lost (a reconnect gap, a missed event), this re-asks the same
      // question directly rather than counting up forever. The request is
      // idempotent - same clip+spec is the same key - so it costs nothing
      // when the render really is still running.
      if (secs > 8 && Date.now() - (fxPreview.lastCheck || 0) > 4000) recheckFxPreview();
    }, 200);
    status.textContent = 'rendering…';
  } catch (e) {
    status.textContent = '';
    toast(e.message, true);
  }
}
window.renderFxPreview = renderFxPreview;

async function recheckFxPreview() {
  const key = fxPreview.key;
  fxPreview.lastCheck = Date.now();
  const clip = window.selectedClip ? window.selectedClip() : null;
  if (!clip || !fxPreview.busy || fxPreview.checking) return;
  fxPreview.checking = true;
  try {
    const r = await api(`/api/workspaces/${SLUG}/reel/preview`, 'POST',
                        { clip_id: clip.id, spec: fxSpec() });
    // The clip or spec may have moved on while this was in flight.
    if (r.key !== key || !fxPreview.busy) return;
    if (r.status === 'ready') {
      fxPreview.busy = false;
      clearInterval(fxPreview.timer);
      showFxPreview(r.url, r.cached ? 'cached' : 'done');
    }
  } catch (e) {
    // A transient failure here should not interrupt the countdown; the next
    // tick tries again.
  } finally {
    fxPreview.checking = false;
  }
}

function showFxPreview(url, note) {
  const video = document.getElementById('fx-preview');
  video.src = url + '?t=' + Date.now();
  video.hidden = false;
  document.getElementById('reel-canvas').hidden = true;
  document.getElementById('fx-preview-status').textContent = note || '';
  video.play().catch(() => {});
}

async function cancelFxPreview() {
  try { await api(`/api/workspaces/${SLUG}/reel/preview/cancel`, 'POST', {}); } catch (e) {}
  fxPreview.busy = false;
  clearInterval(fxPreview.timer);
  document.getElementById('fx-preview-status').textContent = 'cancelled';
}
window.cancelFxPreview = cancelFxPreview;

/* ---------------- lifecycle ---------------- */

function initFx() {
  loadFxLibrary(false);
  loadFxPresets();
  loadTextStylesCache().then(() => { if (fxSelected) renderFxInspector(); });

  // Pushed, not polled: the server publishes when the proxy finishes.
  onEvent('preview', (data) => {
    if (!data || data.key !== fxPreview.key) return;
    fxPreview.busy = false;
    clearInterval(fxPreview.timer);
    if (data.ok) showFxPreview(data.url, `${(data.ms / 1000).toFixed(1)}s`);
    else document.getElementById('fx-preview-status').textContent =
      data.error === 'cancelled' ? 'superseded' : (data.error || 'failed');
  });
  onEvent('library', () => loadFxLibrary(false));

  drawFxStrip();
  renderFxInspector();
}
window.initFx = initFx;

// reel.js calls this when a clip is selected; the plan carries the resolved
// effect warnings and the settings defaults, both computed server-side.
function fxOnPlan(plan) {
  if (!plan) return;
  if (plan.fx_defaults) fxDefaults = plan.fx_defaults;
  fxWarnings = plan.fx_warnings || [];
  // A new clip means the old proxy is for something else entirely.
  drawFxStrip();
}
window.fxOnPlan = fxOnPlan;

function fxOnClipChange() {
  fxSelected = null;
  fxPreview.key = null;
  const video = document.getElementById('fx-preview');
  if (video) { video.removeAttribute('src'); video.hidden = true; }
  const canvas = document.getElementById('reel-canvas');
  if (canvas) canvas.hidden = false;
  const status = document.getElementById('fx-preview-status');
  if (status) status.textContent = '';
  drawFxStrip();
  renderFxInspector();
}
window.fxOnClipChange = fxOnClipChange;
