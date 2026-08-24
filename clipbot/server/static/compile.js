/* Compilations page: draft a segment (timeline + transcript, same technique
 * as review.js's clip editor), add it to a named compilation, save, render,
 * play back. formatClock/parseClock/toast/esc come from app.js.
 */

let SLUG = null;
let PAD_START = 1.0, PAD_END = 1.5;

let player = null;
let duration = 0;

let segments = [];      // transcript
let txView = [];
let txQuery = '';

let compilationsList = [];
let openName = null;
let openSegments = [];  // working copy of the open compilation's segments
let openOutput = null;
let dirty = false;      // unsaved local edits to openSegments

let activeCompileJobId = null;    // this workspace's in-flight compile job, if any
let renderingSegmentLabel = null; // label of the segment currently being encoded

/* --- cross-stream segments ---
 * A segment's `slug` names which workspace its footage comes from
 * (clipbot/compilations.py); missing/falsy means "this workspace" (SLUG).
 * Segments from another stream are authored via `clipbot compile` / Claude
 * Code, not this page's timeline+transcript editor (which is bound to this
 * workspace's own video) - this page only reviews, reorders-by-removal, and
 * renders them. See segSlug()/isSingleSource() below for why the client-side
 * sort has to know about this too, not just the server.
 */
function segSlug(s) { return s.slug || SLUG; }
function isSingleSource(list) {
  return new Set(list.map(segSlug)).size <= 1;
}

function initCompile(slug, padStart, padEnd) {
  SLUG = slug;
  PAD_START = padStart != null ? padStart : 1.0;
  PAD_END = padEnd != null ? padEnd : 1.5;
  player = document.getElementById('c-player');

  player.addEventListener('loadedmetadata', () => {
    duration = player.duration || 0;
    document.getElementById('c-t-dur').textContent = formatClock(duration, false);
    drawFullTimeline();
  });
  player.addEventListener('timeupdate', onTimeUpdate);
  player.addEventListener('play', () => document.getElementById('c-play-btn').textContent = '❚❚');
  player.addEventListener('pause', () => document.getElementById('c-play-btn').textContent = '▶');
  player.addEventListener('error', () => {
    player.hidden = true;
    if (!document.getElementById('c-no-video-msg')) {
      document.getElementById('stage').insertAdjacentHTML('beforeend',
        '<div class="no-video" id="c-no-video-msg">No video available — it may have been ' +
        'deleted by the cleanup stage.<br>Re-run the download stage to build compilations.</div>');
    }
  });

  wireTimeInputs();
  wireTimelines();
  wireTranscriptControls();
  document.addEventListener('keydown', onKey);

  window.addEventListener('beforeunload', (e) => {
    if (dirty) { e.preventDefault(); e.returnValue = ''; }
  });

  onEvent('compilations', loadCompilations);
  onEvent('job', onJobEvent);
  onEvent('progress', onProgressEvent);

  loadCompilations();
  loadTranscript();
}
window.initCompile = initCompile;

/* ---------------- transport ---------------- */

function cToggle() { player.paused ? player.play().catch(() => {}) : player.pause(); }
function cSeekBy(d) { player.currentTime = Math.max(0, Math.min(duration, player.currentTime + d)); }
function cSeekTo(t) { player.currentTime = Math.max(0, Math.min(duration, t)); }
window.cToggle = cToggle; window.cSeekBy = cSeekBy;

function onTimeUpdate() {
  const t = player.currentTime;
  document.getElementById('c-t-now').textContent = formatClock(t);
  positionPlayheads(t);
  highlightTranscript(t);
}

/* ---------------- timelines ---------------- */

function pct(t) { return duration ? Math.max(0, Math.min(100, (t / duration) * 100)) : 0; }

function drawFullTimeline() {
  if (!duration) return;
  drawDensity('c-tl-density', 0, duration);
  drawTicks('c-tl-ticks', 0, duration, 8);
  drawSegMarkers();
}

function drawDensity(elId, from, to, buckets) {
  const el = document.getElementById(elId);
  if (!el || !segments.length) return;
  const span = to - from;
  if (span <= 0) return;
  const N = buckets || 320;
  const filled = new Array(N).fill(0);
  const low = new Array(N).fill(0);
  for (const s of segments) {
    if (s.end < from || s.start > to) continue;
    const a = Math.max(0, Math.floor(((s.start - from) / span) * N));
    const b = Math.min(N - 1, Math.ceil(((s.end - from) / span) * N));
    const isLow = s.low_confidence || (s.flags && s.flags.length);
    for (let i = a; i <= b; i++) { filled[i] = 1; if (isLow) low[i] = 1; }
  }
  let html = '';
  for (let i = 0; i < N; i++) {
    if (!filled[i]) continue;
    html += `<i class="${low[i] ? 'low' : ''}" style="left:${(i / N) * 100}%;width:${100 / N + 0.05}%"></i>`;
  }
  el.innerHTML = html;
}

function drawTicks(elId, from, to, count) {
  const el = document.getElementById(elId);
  if (!el) return;
  const span = to - from;
  let html = '';
  for (let i = 0; i <= count; i++) {
    const t = from + (span * i) / count;
    const p = (i / count) * 100;
    html += `<i style="left:${p}%"></i>`;
    if (i > 0 && i < count) html += `<span style="left:${p}%">${formatClock(t, false)}</span>`;
  }
  el.innerHTML = html;
}

function drawSegMarkers() {
  const el = document.getElementById('c-tl-segs');
  if (!el || !duration) return;
  let html = openSegments.map(s => {
    const left = pct(s.start);
    const w = Math.max(0.15, pct(s.end) - left);
    return `<div class="tl-clip seg" style="left:${left}%;width:${w}%" title="${esc(s.label || '')}"></div>`;
  }).join('');
  const ds = cDraftStart(), de = cDraftEnd();
  if (!isNaN(ds) && !isNaN(de) && de > ds) {
    const left = pct(ds);
    const w = Math.max(0.15, pct(de) - left);
    html += `<div class="tl-clip draft" style="left:${left}%;width:${w}%"></div>`;
  }
  el.innerHTML = html;
}

function positionPlayheads(t) {
  const h = document.getElementById('c-tl-head');
  if (h) h.style.left = pct(t) + '%';
  const z = document.getElementById('c-tz-head');
  if (z) {
    const w = zoomWindow();
    const p = ((t - w.from) / (w.to - w.from)) * 100;
    z.style.left = Math.max(0, Math.min(100, p)) + '%';
    z.style.display = (p < -2 || p > 102) ? 'none' : '';
  }
}

// The zoom strip centers on the draft in/out, or a small window around the
// playhead if nothing has been drafted yet.
function zoomWindow() {
  let s = cDraftStart(), e = cDraftEnd();
  if (isNaN(s) || isNaN(e) || e <= s) {
    const t = player ? player.currentTime : 0;
    s = t; e = t + 10;
  }
  const len = Math.max(2, e - s);
  const margin = Math.max(3, len * 0.35);
  return { from: Math.max(0, s - margin), to: Math.min(duration || e + margin, e + margin) };
}

// Same rAF-coalescing as review.js's drawZoom() (trim-handle dragging and
// in/out keystrokes can call this far faster than the screen redraws, and
// drawDensity() below rescans the whole segment array every time) - kept
// here too since this file's timeline/drag code was adapted from review.js
// wholesale and shares the same hot path.
let _zoomFrameQueued = false;
function drawZoom() {
  if (_zoomFrameQueued) return;
  _zoomFrameQueued = true;
  requestAnimationFrame(() => {
    _zoomFrameQueued = false;
    drawZoomNow();
  });
}

function drawZoomNow() {
  const w = zoomWindow();
  const span = w.to - w.from;
  const s = cDraftStart(), e = cDraftEnd();
  const haveDraft = !isNaN(s) && !isNaN(e) && e > s;
  const P = t => ((t - w.from) / span) * 100;

  const win = document.getElementById('c-tz-window');
  const padIn = document.getElementById('c-tz-pad-in');
  const padOut = document.getElementById('c-tz-pad-out');
  const inH = document.getElementById('c-tz-in');
  const outH = document.getElementById('c-tz-out');

  if (haveDraft) {
    win.style.left = P(s) + '%';
    win.style.width = Math.max(0.3, P(e) - P(s)) + '%';
    win.style.display = '';
    padIn.style.left = P(Math.max(0, s - PAD_START)) + '%';
    padIn.style.width = Math.max(0, P(s) - P(Math.max(0, s - PAD_START))) + '%';
    padOut.style.left = P(e) + '%';
    padOut.style.width = Math.max(0, P(e + PAD_END) - P(e)) + '%';
    inH.style.left = P(s) + '%';
    outH.style.left = P(e) + '%';
    inH.style.display = outH.style.display = '';
  } else {
    win.style.display = 'none';
    padIn.style.width = padOut.style.width = '0';
    inH.style.display = outH.style.display = 'none';
  }

  drawDensity('c-tz-density', w.from, w.to, 180);
  drawTicks('c-tz-ticks', w.from, w.to, 6);
  positionPlayheads(player.currentTime);
}

function wireTimelines() {
  const full = document.getElementById('c-tl-full');
  full.addEventListener('click', (ev) => {
    const r = full.getBoundingClientRect();
    cSeekTo(((ev.clientX - r.left) / r.width) * duration);
  });

  const zoom = document.getElementById('c-tl-zoom');
  zoom.addEventListener('click', (ev) => {
    if (ev.target.closest('.tl-handle')) return;
    const r = zoom.getBoundingClientRect();
    const w = zoomWindow();
    cSeekTo(w.from + ((ev.clientX - r.left) / r.width) * (w.to - w.from));
  });

  makeHandleDraggable('c-tz-in', 'start');
  makeHandleDraggable('c-tz-out', 'end');
}

function makeHandleDraggable(id, field) {
  const handle = document.getElementById(id);
  const zoom = document.getElementById('c-tl-zoom');
  let dragging = false;

  const move = (ev) => {
    if (!dragging) return;
    const r = zoom.getBoundingClientRect();
    const w = zoomWindow();
    let t = w.from + ((ev.clientX - r.left) / r.width) * (w.to - w.from);
    t = Math.max(0, Math.min(duration || t, t));
    if (field === 'start') t = Math.min(t, cDraftEnd() - 0.5);
    else t = Math.max(t, cDraftStart() + 0.5);
    setField(field, t);
    drawZoom();
    drawSegMarkers();
  };

  handle.addEventListener('pointerdown', (ev) => {
    ev.preventDefault();
    dragging = true;
    handle.classList.add('drag');
    handle.setPointerCapture(ev.pointerId);
  });
  handle.addEventListener('pointermove', move);
  handle.addEventListener('pointerup', (ev) => {
    dragging = false;
    handle.classList.remove('drag');
    try { handle.releasePointerCapture(ev.pointerId); } catch (e) {}
  });
}

/* ---------------- draft in/out editor ---------------- */

function cDraftStart() { return parseClock(document.getElementById('c-in-val').value); }
function cDraftEnd() { return parseClock(document.getElementById('c-out-val').value); }

function setField(field, seconds) {
  const id = field === 'start' ? 'c-in-val' : 'c-out-val';
  document.getElementById(id).value = formatClock(seconds);
  updateDurationPill();
}

function updateDurationPill() {
  const pill = document.getElementById('c-dur-pill');
  if (!pill) return;
  const s = cDraftStart(), e = cDraftEnd();
  if (isNaN(s) || isNaN(e) || e <= s) { pill.textContent = '—'; pill.className = 'dur-pill warn'; return; }
  pill.textContent = (e - s).toFixed(1) + 's';
  pill.className = 'dur-pill';
}

function wireTimeInputs() {
  ['c-in-val', 'c-out-val'].forEach(id => {
    document.getElementById(id).addEventListener('input', () => {
      updateDurationPill();
      drawZoom();
      drawSegMarkers();
    });
  });
}

function cUseSelection() {
  const t = player.currentTime;
  document.getElementById('c-in-val').value = formatClock(t);
  document.getElementById('c-out-val').value = formatClock(Math.min(duration || t + 15, t + 15));
  updateDurationPill();
  drawZoom();
  drawSegMarkers();
}
window.cUseSelection = cUseSelection;

function cNudge(field, delta) {
  const cur = field === 'start' ? cDraftStart() : cDraftEnd();
  if (isNaN(cur)) return;
  setField(field, cur + delta);
  drawZoom();
  drawSegMarkers();
}

function cMarkHere(field) {
  setField(field, player.currentTime);
  drawZoom();
  drawSegMarkers();
}

function showCKeys() { document.getElementById('c-keys-dialog').showModal(); }
window.showCKeys = showCKeys;

// Mirrors review.js's onKey, adapted to this page's draft-segment model
// (no selected clip to approve/reject/step between - just a draft in/out).
function onKey(e) {
  const tag = e.target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) {
    if (e.key === 'Escape') e.target.blur();
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  switch (e.key) {
    case ' ': e.preventDefault(); cToggle(); break;
    case 'ArrowLeft': e.preventDefault(); cSeekBy(e.shiftKey ? -10 : -1); break;
    case 'ArrowRight': e.preventDefault(); cSeekBy(e.shiftKey ? 10 : 1); break;
    case '[': cNudge('start', e.shiftKey ? -1 : -0.1); break;
    case ']': cNudge('end', e.shiftKey ? 1 : 0.1); break;
    case 'i': case 'I': cMarkHere('start'); break;
    case 'o': case 'O': cMarkHere('end'); break;
    case 'Enter': cAddDraftSegment(); break;
    case '/': e.preventDefault(); document.getElementById('c-tx-search').focus(); break;
    case '?': showCKeys(); break;
    default: return;
  }
}

let editingIndex = null;  // index into openSegments currently being edited, or null

function cAddDraftSegment() {
  if (!openName) { toast('Open or create a compilation first', true); return; }
  const start = cDraftStart(), end = cDraftEnd();
  if (isNaN(start) || isNaN(end) || end <= start) {
    toast('Enter a valid in and out, e.g. 41:00 and 43:00', true);
    return;
  }
  const label = document.getElementById('c-label-val').value.trim();
  const wasEditing = editingIndex != null;
  if (wasEditing) {
    // Preserve the edited segment's own slug (it's always this workspace's
    // own here - see cEditSegment's foreign-row guard - but keep the field
    // rather than dropping it, in case that guard is ever relaxed).
    openSegments[editingIndex] = { ...openSegments[editingIndex], start, end, label };
    cCancelEditSegment();  // clears editingIndex and resets the button label
  } else {
    openSegments.push({ start, end, label });
  }
  // Chronological sort only makes sense when every segment shares one
  // source - across streams it'd scramble the authored play order into a
  // meaningless mix of independent clocks (see clipbot/compilations.py's
  // _normalize_segments for the server-side version of this same rule).
  if (isSingleSource(openSegments)) {
    openSegments.sort((a, b) => a.start - b.start);
  }
  dirty = true;

  document.getElementById('c-in-val').value = '';
  document.getElementById('c-out-val').value = '';
  document.getElementById('c-label-val').value = '';
  updateDurationPill();
  drawZoom();
  renderSegList();
  drawSegMarkers();
  toast((wasEditing ? 'Updated' : 'Added') + ' — not saved yet');
}
window.cAddDraftSegment = cAddDraftSegment;

// Loads a saved segment back into the draft fields so it can be adjusted -
// previously the only way to change a segment's times was delete-and-re-add,
// which lost its position in a longer list and its label had to be retyped.
function cEditSegment(i) {
  const s = openSegments[i];
  if (segSlug(s) !== SLUG) {
    // This page's draft editor (timeline + transcript) is bound to this
    // workspace's own video, so its times would be meaningless against a
    // foreign segment's source. Adjust those via `clipbot compile` instead.
    toast('This segment is from ' + segSlug(s) + ' - edit it with clipbot compile, not here', true);
    return;
  }
  document.getElementById('c-in-val').value = formatClock(s.start);
  document.getElementById('c-out-val').value = formatClock(s.end);
  document.getElementById('c-label-val').value = s.label || '';
  editingIndex = i;
  updateDurationPill();
  drawZoom();
  drawSegMarkers();
  renderSegList();
  document.getElementById('c-add-btn').textContent = 'Update segment';
  document.getElementById('c-cancel-edit-btn').hidden = false;
  document.getElementById('c-in-val').focus();
}
window.cEditSegment = cEditSegment;

function cCancelEditSegment() {
  editingIndex = null;
  document.getElementById('c-add-btn').textContent = '+ Add segment';
  document.getElementById('c-cancel-edit-btn').hidden = true;
  renderSegList();
}
window.cCancelEditSegment = cCancelEditSegment;

/* ---------------- transcript (virtualised, same approach as review.js) ---------------- */

const ROW_H = 46;
let captionsIndex = null;                 // Map(segment id -> Hinglish text), or null if none
let txScript = localStorage.getItem('clipbot.txScript') || 'dev';   // 'dev' | 'hin'

async function loadTranscript() {
  const data = await api(`/api/workspaces/${SLUG}/transcript`);
  segments = data.segments || [];
  applyTranscriptFilter();
  if (duration) drawFullTimeline();

  captionsIndex = await loadCaptionsIndex(SLUG);
  const toggle = document.getElementById('c-tx-script-toggle');
  if (toggle) toggle.hidden = !captionsIndex;
  if (captionsIndex) cSetTxScript(txScript);
}

// Mirrors review.js's displayText(): Hinglish text if that pass ran and the
// toggle is set to it, Devanagari otherwise (captions.json already carries
// the Devanagari fallback for any segment a failed transliteration batch
// left untranslated).
function displayText(s) {
  if (txScript === 'hin' && captionsIndex && captionsIndex.has(s.id)) {
    return captionsIndex.get(s.id);
  }
  return s.text || '';
}

function cSetTxScript(mode) {
  txScript = mode;
  localStorage.setItem('clipbot.txScript', mode);
  document.querySelectorAll('#c-tx-script-toggle button').forEach(b =>
    b.classList.toggle('on', b.dataset.script === mode));
  document.getElementById('c-tx-search').placeholder = mode === 'hin'
    ? 'Search transcript (Hinglish)…'
    : 'Search transcript (Devanagari — देवनागरी)…';
  applyTranscriptFilter();
}
window.cSetTxScript = cSetTxScript;

function applyTranscriptFilter() {
  const hideLow = document.getElementById('c-tx-hide-low').checked;
  const q = txQuery.trim().toLowerCase();
  txView = [];
  for (let i = 0; i < segments.length; i++) {
    const s = segments[i];
    const low = s.low_confidence || (s.flags && s.flags.length);
    if (hideLow && low) continue;
    if (q && !displayText(s).toLowerCase().includes(q)) continue;
    txView.push(i);
  }
  document.getElementById('c-tx-count').textContent =
    q || hideLow ? `${txView.length} / ${segments.length}` : `${segments.length} lines`;
  const inner = document.getElementById('c-tx-inner');
  inner.style.height = (txView.length * ROW_H) + 'px';
  renderTxWindow(true);
}

function renderTxWindow(force) {
  const scroll = document.getElementById('c-tx-scroll');
  const inner = document.getElementById('c-tx-inner');
  const top = scroll.scrollTop;
  const h = scroll.clientHeight;
  const first = Math.max(0, Math.floor(top / ROW_H) - 6);
  const last = Math.min(txView.length, Math.ceil((top + h) / ROW_H) + 6);
  if (!force && inner._first === first && inner._last === last) return;
  inner._first = first; inner._last = last;

  const t = player ? player.currentTime : 0;
  const ds = cDraftStart(), de = cDraftEnd();
  let html = '';
  for (let vi = first; vi < last; vi++) {
    const i = txView[vi];
    const s = segments[i];
    const low = s.low_confidence || (s.flags && s.flags.length);
    const active = s.start <= t && t <= s.end;
    const inDraft = !isNaN(ds) && !isNaN(de) && de > ds && s.end > ds && s.start < de;
    html += `<div class="tx-row${low ? ' lowconf' : ''}${active ? ' active' : ''}${inDraft ? ' inclip' : ''}"
                  style="top:${vi * ROW_H}px;height:${ROW_H}px"
                  data-start="${s.start}">
        <span class="t">${formatClock(s.start, false)}</span>
        <span class="x">${highlight(displayText(s))}</span>
      </div>`;
  }
  inner.innerHTML = html;
}

function highlight(text) {
  const q = txQuery.trim();
  if (!q) return esc(text);
  const i = text.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return esc(text);
  return esc(text.slice(0, i)) + '<mark>' + esc(text.slice(i, i + q.length)) +
         '</mark>' + esc(text.slice(i + q.length));
}

function highlightTranscript(t) {
  renderTxWindow(false);
}

function wireTranscriptControls() {
  const scroll = document.getElementById('c-tx-scroll');
  scroll.addEventListener('scroll', () => renderTxWindow(false));

  scroll.addEventListener('click', (ev) => {
    const row = ev.target.closest('.tx-row');
    if (!row) return;
    const start = parseFloat(row.dataset.start);

    // Shift-click sets the draft's out point, same convention review.js uses
    // for a clip's out point.
    if (ev.shiftKey) {
      setField('end', start);
      drawZoom();
      drawSegMarkers();
      renderTxWindow(true);
      toast('Out point set from transcript');
      return;
    }
    cSeekTo(start);
    player.play().catch(() => {});
  });

  const search = document.getElementById('c-tx-search');
  let debounce;
  search.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => { txQuery = search.value; applyTranscriptFilter(); }, 120);
  });
  document.getElementById('c-tx-hide-low').addEventListener('change', applyTranscriptFilter);
}

/* ---------------- compilations ---------------- */

async function loadCompilations() {
  const data = await api(`/api/workspaces/${SLUG}/compilations`);
  compilationsList = data.compilations || [];
  renderCompList();

  if (openName) {
    const fresh = compilationsList.find(c => c.name === openName);
    if (fresh) {
      openOutput = fresh.output;
      if (!dirty) openSegments = fresh.segments.slice();
      renderSegList();
      renderOutput();
      drawSegMarkers();
    } else {
      // Deleted elsewhere (another tab, or the CLI).
      openName = null;
      document.getElementById('c-open-panel').hidden = true;
      drawSegMarkers();
    }
  }
}

function renderCompList() {
  const el = document.getElementById('c-complist');
  if (!compilationsList.length) {
    el.innerHTML = '<p class="empty">No compilations yet.</p>';
    return;
  }
  el.innerHTML = compilationsList.map(c => {
    const sel = c.name === openName ? ' sel' : '';
    const rendered = c.output && c.output.file;
    return `<div class="comp-row${sel}" onclick="cSelectCompilation('${esc(c.name)}')">
      <div class="name">${esc(c.name)}</div>
      <div class="meta">${c.segments.length} segment${c.segments.length === 1 ? '' : 's'}
        ${rendered ? '<span class="badge">rendered</span>' : ''}
      </div>
    </div>`;
  }).join('');
}

function cSelectCompilation(name) {
  if (dirty && !confirm('You have unsaved segment changes. Discard them?')) return;
  const comp = compilationsList.find(c => c.name === name);
  if (!comp) return;
  openName = name;
  openSegments = comp.segments.slice();
  openOutput = comp.output;
  dirty = false;

  document.getElementById('c-open-panel').hidden = false;
  document.getElementById('c-open-title').textContent = name;
  document.getElementById('c-open-meta').textContent =
    `created ${new Date(comp.created_at * 1000).toLocaleString()}`;

  renderCompList();
  renderSegList();
  renderOutput();
  drawSegMarkers();
}
window.cSelectCompilation = cSelectCompilation;

function cNewCompilation() {
  const input = document.getElementById('c-new-name');
  const name = input.value.trim();
  if (!name) { toast('Enter a name', true); return; }
  if (dirty && !confirm('You have unsaved segment changes. Discard them?')) return;

  const existing = compilationsList.find(c => c.name === name);
  input.value = '';
  if (existing) { cSelectCompilation(name); return; }

  openName = name;
  openSegments = [];
  openOutput = null;
  dirty = true;

  document.getElementById('c-open-panel').hidden = false;
  document.getElementById('c-open-title').textContent = name;
  document.getElementById('c-open-meta').textContent = 'not saved yet';
  renderCompList();
  renderSegList();
  renderOutput();
  drawSegMarkers();
  toast('Add at least one segment, then Save');
}
window.cNewCompilation = cNewCompilation;

function renderSegList() {
  const el = document.getElementById('c-seglist');
  if (!openSegments.length) {
    el.innerHTML = '<p class="empty">No segments yet.</p>';
    return;
  }
  el.innerHTML = openSegments.map((s, i) => {
    const editing = i === editingIndex;
    const rendering = renderingSegmentLabel != null &&
      renderingSegmentLabel === (s.label || 'segment ' + (i + 1));
    const foreign = segSlug(s) !== SLUG;
    return `
    <div class="seg-row${editing ? ' editing' : ''}${rendering ? ' rendering' : ''}">
      <span class="rng">${formatClock(s.start, false)}–${formatClock(s.end, false)}</span>
      ${foreign ? `<span class="badge" title="Footage from a different workspace">from ${esc(segSlug(s))}</span>` : ''}
      <span class="label" title="${esc(s.label || '')}">${esc(s.label || '')}</span>
      ${rendering ? '<span class="badge">rendering…</span>' : ''}
      <button onclick="cEditSegment(${i})" title="${foreign ? 'Edit with clipbot compile, not here' : 'Edit'}"${foreign ? ' disabled' : ''}>✎</button>
      <button onclick="cRemoveSegment(${i})" title="Remove">✕</button>
    </div>`;
  }).join('');
}

function cRemoveSegment(i) {
  openSegments.splice(i, 1);
  dirty = true;
  // The index that was being edited may now point at a different segment
  // (or nothing) - safest to just drop out of edit mode rather than risk
  // silently editing the wrong row after a removal shifts indices.
  if (editingIndex != null) cCancelEditSegment();
  renderSegList();
  drawSegMarkers();
}
window.cRemoveSegment = cRemoveSegment;

async function cSaveCompilation() {
  if (!openName) return;
  if (!openSegments.length) { toast('Add at least one segment first', true); return; }
  const btn = document.getElementById('c-save-btn');
  btn.disabled = true;
  try {
    await api(`/api/workspaces/${SLUG}/compilations`, 'POST', {
      name: openName,
      segments: openSegments.map(s => ({ start: s.start, end: s.end, label: s.label, slug: s.slug || null })),
    });
    dirty = false;
    await loadCompilations();
    toast('Saved');
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}
window.cSaveCompilation = cSaveCompilation;

async function cRenderCompilation() {
  if (!openName) return;
  if (dirty) { toast('Save your changes first', true); return; }
  try {
    await api(`/api/workspaces/${SLUG}/jobs`, 'POST', { kind: 'compile', name: openName });
    toast('Rendering — watch the job panel');
  } catch (e) { toast(e.message, true); }
}
window.cRenderCompilation = cRenderCompilation;

async function cDeleteCompilation() {
  if (!openName) return;
  if (!confirm(`Delete "${openName}"? This also deletes its rendered video.`)) return;
  try {
    await api(`/api/workspaces/${SLUG}/compilations/${encodeURIComponent(openName)}`, 'DELETE');
    openName = null;
    dirty = false;
    document.getElementById('c-open-panel').hidden = true;
    await loadCompilations();
    drawSegMarkers();
    toast('Deleted');
  } catch (e) { toast(e.message, true); }
}
window.cDeleteCompilation = cDeleteCompilation;

function renderOutput() {
  const el = document.getElementById('c-output');
  if (!el) return;
  if (!openOutput || !openOutput.file) {
    el.innerHTML = '<span class="meta">Not rendered yet.</span>';
    return;
  }
  const url = `/media/${SLUG}/compilation/${encodeURIComponent(openName)}`;
  el.innerHTML = `
    <video src="${url}" controls preload="none"></video>
    <div class="meta" style="margin-top:4px">
      ${(openOutput.bytes / 1048576).toFixed(1)} MB · ${openOutput.duration.toFixed(1)}s
      <br><a href="${url}?download=1">download</a>
    </div>`;
}

const JOB_TERMINAL = ['succeeded', 'failed', 'cancelled', 'interrupted'];

function onJobEvent(payload) {
  if (payload.kind !== 'compile' || payload.slug !== SLUG) return;
  const name = payload.options && payload.options.name;
  const terminal = JOB_TERMINAL.includes(payload.status);
  // Tracked so onProgressEvent (a separate SSE event kind with no `kind`
  // field of its own to filter on) knows which progress events are this
  // workspace's compile job and not some other job kind or workspace.
  activeCompileJobId = terminal ? null : payload.id;
  if (terminal) renderingSegmentLabel = null;

  if (payload.status === 'succeeded') {
    loadCompilations();
    if (name === openName) toast('Render finished');
  } else if (payload.status === 'failed' && name === openName) {
    toast(payload.error || 'Render failed', true);
  }
  if (name === openName) renderSegList();
}

function onProgressEvent(payload) {
  if (!activeCompileJobId || payload.job_id !== activeCompileJobId) return;
  if (renderingSegmentLabel === payload.label) return;
  renderingSegmentLabel = payload.label || null;
  renderSegList();
}
