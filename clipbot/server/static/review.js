/* Clip review workspace.
 *
 * Built around one constraint: a 4.5-hour VOD with 1200+ transcript segments.
 * That rules out rendering the transcript as plain DOM (53,000px of scroll) and
 * makes a spatial view of the stream essential - without it there's no way to
 * tell where 20 clips sit across four and a half hours.
 */

let SLUG = null;
let PAD_START = 1.0, PAD_END = 1.5;

let clips = [];
let segments = [];
let selected = null;
let player = null;
let duration = 0;
let filter = 'all';
let clipQuery = '';
let dirty = false;
let previewUntil = null;   // when set, playback stops here (preview mode)

let compilationNames = [];
let compilationPickerClipId = null;
// renderClips() fully rebuilds the list on every `clips`/`workspace` SSE
// event (from ANY client, not just this one) - without this, expanding one
// clip's "why" and then approving a different clip would silently re-collapse
// the first the instant that event landed.
let expandedWhy = new Set();

/* formatClock/parseClock/toast live in app.js now - shared with compile.js,
 * loaded before this file via base.html. */

/* ---------------- init ---------------- */

function initReview(slug, padStart, padEnd) {
  SLUG = slug;
  PAD_START = padStart != null ? padStart : 1.0;
  PAD_END = padEnd != null ? padEnd : 1.5;
  player = document.getElementById('player');

  player.addEventListener('loadedmetadata', () => {
    duration = player.duration || 0;
    document.getElementById('t-dur').textContent = formatClock(duration, false);
    drawFullTimeline();
  });
  player.addEventListener('timeupdate', onTimeUpdate);
  player.addEventListener('play', () => document.getElementById('play-btn').textContent = '❚❚');
  player.addEventListener('pause', () => document.getElementById('play-btn').textContent = '▶');
  player.addEventListener('error', () => {
    // Hide the player rather than wiping #stage: the crop overlay lives in
    // there, and innerHTML would destroy it along with its listeners.
    player.hidden = true;
    const layer = document.getElementById('crop-layer');
    if (layer) layer.innerHTML = '';
    if (!document.getElementById('no-video-msg')) {
      document.getElementById('stage').insertAdjacentHTML('beforeend',
        '<div class="no-video" id="no-video-msg">No video available — it may have been ' +
        'deleted by the cleanup stage.<br>Re-run the download stage to review clips visually.</div>');
    }
  });

  wireTimeInputs();
  wireTimelines();
  wireTranscriptControls();
  document.addEventListener('keydown', onKey);

  // Don't lose in/out edits by navigating away mid-edit.
  window.addEventListener('beforeunload', (e) => {
    if (dirty) { e.preventDefault(); e.returnValue = ''; }
  });

  onEvent('clips', loadClips);
  onEvent('workspace', loadClips);
  onEvent('compilations', loadCompilationNames);
  loadAll();
}
window.initReview = initReview;

async function loadAll() {
  await Promise.all([loadClips(), loadTranscript(), loadSpeakers(), loadCompilationNames()]);
}
window.loadAll = loadAll;

/* ---------------- add to compilation ---------------- */

async function loadCompilationNames() {
  try {
    const data = await api(`/api/workspaces/${SLUG}/compilations`);
    compilationNames = (data.compilations || []).map(c => c.name);
    const dl = document.getElementById('comp-names-list');
    if (dl) dl.innerHTML = compilationNames.map(n => `<option value="${esc(n)}">`).join('');
  } catch (e) { /* best-effort - the picker still works by typing a new name */ }
}

function openCompilationPicker(clipId) {
  compilationPickerClipId = clipId;
  document.getElementById('comp-picker-name').value = '';
  document.getElementById('comp-picker').showModal();
}
window.openCompilationPicker = openCompilationPicker;

async function confirmAddToCompilation() {
  const name = document.getElementById('comp-picker-name').value.trim();
  if (!name) { toast('Enter a compilation name', true); return; }
  const clip = clips.find(c => c.id === compilationPickerClipId);
  if (!clip) return;
  // If this is the clip currently open in the editor with unsaved in/out
  // edits, clip.start/end here is the last-SAVED range, not what's on
  // screen - use the live editor fields instead so the compilation gets the
  // range the user actually sees.
  const useLive = dirty && selected && selected.id === clip.id;
  const start = useLive ? editedStart() : clip.start;
  const end = useLive ? editedEnd() : clip.end;
  if (isNaN(start) || isNaN(end) || end <= start) {
    toast('Fix the in/out fields first', true);
    return;
  }
  const btn = document.getElementById('comp-picker-add-btn');
  if (btn) btn.disabled = true;
  try {
    await api(`/api/workspaces/${SLUG}/compilations/${encodeURIComponent(name)}/segments`, 'POST', {
      start, end, label: clip.title || clip.description || '',
    });
    document.getElementById('comp-picker').close();
    toast(`Added to "${name}"`);
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}
window.confirmAddToCompilation = confirmAddToCompilation;

/* ---------------- transport ---------------- */

function togglePlay() { player.paused ? player.play().catch(() => {}) : player.pause(); }
function step(d) { player.pause(); player.currentTime = Math.max(0, player.currentTime + d); }
function seekBy(d) { player.currentTime = Math.max(0, Math.min(duration, player.currentTime + d)); }
function seekTo(t) { player.currentTime = Math.max(0, Math.min(duration, t)); }
window.togglePlay = togglePlay; window.step = step; window.seekBy = seekBy;

function onTimeUpdate() {
  const t = player.currentTime;
  document.getElementById('t-now').textContent = formatClock(t);
  positionPlayheads(t);
  highlightTranscript(t);

  if (previewUntil != null && t >= previewUntil) {
    if (document.getElementById('loop').checked && selected) {
      seekTo(editedStart());
    } else {
      player.pause();
      previewUntil = null;
    }
    return;
  }
  // Loop inside the selected clip so it's judged the way a viewer sees it.
  if (previewUntil == null && document.getElementById('loop').checked && selected && !player.paused) {
    const s = editedStart(), e = editedEnd();
    if (!isNaN(s) && !isNaN(e) && (t > e || t < s - 0.5)) seekTo(s);
  }
}

function previewClip() {
  if (!selected) return;
  previewUntil = editedEnd();
  seekTo(editedStart());
  player.play().catch(() => {});
}
window.previewClip = previewClip;

/* ---------------- timelines ---------------- */

function pct(t) { return duration ? Math.max(0, Math.min(100, (t / duration) * 100)) : 0; }

function drawFullTimeline() {
  if (!duration) return;
  drawDensity('tl-density', 0, duration);
  drawTicks('tl-ticks', 0, duration, 8);
  drawClipMarkers();
}

// Where speech actually is - makes silent stretches and low-confidence
// (music / hallucination) regions visible at a glance.
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

function drawClipMarkers() {
  const el = document.getElementById('tl-clips');
  if (!el || !duration) return;
  el.innerHTML = clips.map(c => {
    const left = pct(c.start);
    const w = Math.max(0.15, pct(c.end) - left);
    const sel = selected && selected.id === c.id ? ' sel' : '';
    return `<div class="tl-clip ${c.status}${sel}" style="left:${left}%;width:${w}%"
              title="${esc(c.title || (c.description || '').slice(0, 60))}"></div>`;
  }).join('');
}

function positionPlayheads(t) {
  const h = document.getElementById('tl-head');
  if (h) h.style.left = pct(t) + '%';
  const z = document.getElementById('tz-head');
  if (z && selected) {
    const w = zoomWindow();
    const p = ((t - w.from) / (w.to - w.from)) * 100;
    z.style.left = Math.max(0, Math.min(100, p)) + '%';
    z.style.display = (p < -2 || p > 102) ? 'none' : '';
  }
}

// The zoom strip shows the clip plus context either side, so you can see what
// you're cutting into.
function zoomWindow() {
  const s = editedStart(), e = editedEnd();
  const len = Math.max(2, e - s);
  const margin = Math.max(3, len * 0.35);
  return { from: Math.max(0, s - margin), to: Math.min(duration || e + margin, e + margin) };
}

// Trim-handle dragging and in/out keystrokes can call drawZoom() far faster
// than the screen can redraw (a 1000Hz mouse fires pointermove up to ~1000x/
// sec; drawDensity() below rescans the whole segment array - 1200+ on a long
// VOD - and rebuilds an innerHTML string every time). Coalescing to one real
// draw per animation frame keeps every caller's code unchanged - each still
// just calls drawZoom() - while capping the actual work to the display's
// refresh rate instead of the input event rate.
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
  const block = document.getElementById('zoom-block');
  if (!selected) { block.hidden = true; return; }
  block.hidden = false;

  const w = zoomWindow();
  const span = w.to - w.from;
  const s = editedStart(), e = editedEnd();
  const P = t => ((t - w.from) / span) * 100;

  document.getElementById('tz-window').style.left = P(s) + '%';
  document.getElementById('tz-window').style.width = Math.max(0.3, P(e) - P(s)) + '%';

  // Hatched areas = padding the cut stage adds automatically.
  const padIn = document.getElementById('tz-pad-in');
  padIn.style.left = P(Math.max(0, s - PAD_START)) + '%';
  padIn.style.width = Math.max(0, P(s) - P(Math.max(0, s - PAD_START))) + '%';
  const padOut = document.getElementById('tz-pad-out');
  padOut.style.left = P(e) + '%';
  padOut.style.width = Math.max(0, P(e + PAD_END) - P(e)) + '%';

  document.getElementById('tz-in').style.left = P(s) + '%';
  document.getElementById('tz-out').style.left = P(e) + '%';

  drawDensity('tz-density', w.from, w.to, 180);
  drawTicks('tz-ticks', w.from, w.to, 6);
  positionPlayheads(player.currentTime);
  // The FX lane sits under this strip and shares its window. Effect times are
  // absolute VOD seconds, exactly like everything else on here, so the lane
  // needs no conversion of its own.
  if (window.drawFxStrip) window.drawFxStrip();
}

function wireTimelines() {
  const full = document.getElementById('tl-full');
  full.addEventListener('click', (ev) => {
    const r = full.getBoundingClientRect();
    seekTo(((ev.clientX - r.left) / r.width) * duration);
  });

  const zoom = document.getElementById('tl-zoom');
  zoom.addEventListener('click', (ev) => {
    if (ev.target.closest('.tl-handle')) return;
    const r = zoom.getBoundingClientRect();
    const w = zoomWindow();
    seekTo(w.from + ((ev.clientX - r.left) / r.width) * (w.to - w.from));
  });

  makeHandleDraggable('tz-in', 'start');
  makeHandleDraggable('tz-out', 'end');
}

function makeHandleDraggable(id, field) {
  const handle = document.getElementById(id);
  const zoom = document.getElementById('tl-zoom');
  let dragging = false;

  const move = (ev) => {
    if (!dragging || !selected) return;
    const r = zoom.getBoundingClientRect();
    const w = zoomWindow();
    let t = w.from + ((ev.clientX - r.left) / r.width) * (w.to - w.from);
    t = Math.max(0, Math.min(duration || t, t));
    // Keep at least half a second between in and out.
    if (field === 'start') t = Math.min(t, editedEnd() - 0.5);
    else t = Math.max(t, editedStart() + 0.5);
    setField(field, t);
    drawZoom();
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
    seekTo(field === 'start' ? editedStart() : Math.max(0, editedEnd() - 2));
  });
}

/* ---------------- in/out editor ---------------- */

function editedStart() { return parseClock(document.getElementById('in-val').value); }
function editedEnd() { return parseClock(document.getElementById('out-val').value); }

function setField(field, seconds) {
  const id = field === 'start' ? 'in-val' : 'out-val';
  document.getElementById(id).value = formatClock(seconds);
  syncField(id);
  markDirty();
}

function syncField(id) {
  const v = parseClock(document.getElementById(id).value);
  const lbl = document.getElementById(id === 'in-val' ? 'in-secs' : 'out-secs');
  if (lbl) lbl.textContent = isNaN(v) ? '' : `${v.toFixed(1)}s`;
  updateDurationPill();
  // The chat window (start-PAD_START .. end+PAD_END) moves with in/out, so the
  // "N messages in this window" diagnostic in reel.js needs to follow every
  // path that changes them - typing, nudge buttons, and "set to playhead" all
  // funnel through here, not just the raw <input> event.
  if (window.updateChatWindowCount) updateChatWindowCount();
}

function updateDurationPill() {
  const pill = document.getElementById('dur-pill');
  if (!pill) return;
  const s = editedStart(), e = editedEnd();
  if (isNaN(s) || isNaN(e) || e <= s) { pill.textContent = '—'; pill.className = 'dur-pill warn'; return; }
  const len = e - s;
  pill.textContent = len.toFixed(1) + 's';
  // The rubric targets 20-60s for Reels and never past 90.
  pill.className = 'dur-pill' + (len > 90 || len < 5 ? ' warn' : '');
}

function markDirty() {
  if (!selected) return;
  const changed = Math.abs(editedStart() - selected.start) > 0.05 ||
                  Math.abs(editedEnd() - selected.end) > 0.05;
  dirty = changed;
  document.getElementById('editor').classList.toggle('dirty', changed);
}

function wireTimeInputs() {
  ['in-val', 'out-val'].forEach(id => {
    const el = document.getElementById(id);
    el.addEventListener('input', () => { syncField(id); markDirty(); drawZoom(); });
  });
  ['new-start', 'new-end'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', () => {});
  });
}

function nudge(field, delta) {
  if (!selected) return;
  const cur = field === 'start' ? editedStart() : editedEnd();
  if (isNaN(cur)) return;
  setField(field, cur + delta);
  drawZoom();
}
window.nudge = nudge;

function markHere(field) {
  if (!selected) return;
  setField(field, player.currentTime);
  drawZoom();
}
window.markHere = markHere;

async function saveEdits() {
  if (!selected) return;
  const start = editedStart(), end = editedEnd();
  if (isNaN(start) || isNaN(end)) { toast('Enter times like 41:00 or 1:23:45', true); return; }
  if (end <= start) { toast('Out must come after in', true); return; }
  const btn = document.getElementById('save-btn');
  btn.disabled = true;
  try {
    await api(`/api/workspaces/${SLUG}/clips/${selected.id}`, 'PATCH', { start, end });
    dirty = false;
    document.getElementById('editor').classList.remove('dirty');
    await loadClips();
    selected = clips.find(c => c.id === selected.id) || null;
    toast('Saved');
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}
window.saveEdits = saveEdits;

async function resetToModel() {
  if (!selected) return;
  const btn = document.getElementById('reset-btn');
  if (btn) btn.disabled = true;
  try {
    await api(`/api/workspaces/${SLUG}/clips/${selected.id}`, 'PATCH', {
      start: selected.source_start, end: selected.source_end
    });
    await loadClips();
    selectClip(selected.id);
    toast('Reset to the model’s range');
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}
window.resetToModel = resetToModel;

/* ---------------- clips ---------------- */

async function loadClips() {
  const data = await api(`/api/workspaces/${SLUG}/clips`);
  clips = (data.clips || []).slice().sort((a, b) => a.start - b.start);
  const c = data.counts || {};
  document.getElementById('tally').textContent =
    `${c.total || 0} candidates · ${c.approved || 0} approved · ${c.cut || 0} cut · ` +
    `${c.rejected || 0} rejected · ${c.pending || 0} pending · ${c.reeled || 0} reels rendered`;
  const cutBtn = document.getElementById('cut-btn');
  const n = (c.approved || 0);
  cutBtn.textContent = n ? `Cut ${n} approved` : 'Cut approved';
  cutBtn.disabled = !n;
  renderFilters(c);
  renderClips();
  drawClipMarkers();
  if (selected) {
    const fresh = clips.find(x => x.id === selected.id);
    if (fresh) selected = fresh;
  }
}

function renderFilters(counts) {
  const defs = [
    ['all', 'All', counts.total || 0],
    ['pending', 'Pending', counts.pending || 0],
    ['approved', 'Approved', counts.approved || 0],
    ['cut', 'Cut', counts.cut || 0],
    ['rejected', 'Rejected', counts.rejected || 0],
  ];
  document.getElementById('filters').innerHTML = defs.map(([k, label, n]) =>
    `<button class="${filter === k ? 'on' : ''}" onclick="setFilter('${k}')">${label}<span class="n">${n}</span></button>`
  ).join('');
}

function setFilter(f) { filter = f; renderFilters(lastCounts()); renderClips(); }
window.setFilter = setFilter;

function lastCounts() {
  const c = { total: clips.length, pending: 0, approved: 0, cut: 0, rejected: 0 };
  clips.forEach(x => { if (c[x.status] != null) c[x.status]++; });
  return c;
}

function visibleClips() {
  let list = filter === 'all' ? clips : clips.filter(c => c.status === filter);
  const q = clipQuery.trim().toLowerCase();
  if (q) {
    // Clip descriptions are English while the transcript is Devanagari, so
    // this is the search that actually works for words like "spider-man".
    list = list.filter(c =>
      ((c.title || '') + ' ' + (c.description || '') + ' ' + (c.why || ''))
        .toLowerCase().includes(q));
  }
  return list;
}

function renderClips() {
  const el = document.getElementById('clips');
  const list = visibleClips();
  if (!list.length) {
    el.innerHTML = `<p class="empty">No ${filter === 'all' ? '' : filter + ' '}clips.</p>`;
    return;
  }
  el.innerHTML = list.map(c => {
    const sel = selected && selected.id === c.id;
    const why = esc(c.why || '');
    return `<div class="clip2 ${c.status}${sel ? ' sel' : ''}" onclick="selectClip('${c.id}')" id="card-${c.id}">
      <div class="hd">
        <span class="ttl">${esc(c.title || (c.description || '').slice(0, 80) || c.id)}</span>
        <span class="rng">${formatClock(c.start, false)}</span>
      </div>
      <div class="meta" style="margin-top:3px">
        ${Math.round(c.end - c.start)}s
        ${c.stale ? '<span class="badge warn">moved</span>' : ''}
        ${c.orphaned ? '<span class="badge warn">orphaned</span>' : ''}
        ${c.origin === 'manual' ? '<span class="badge">manual</span>' : ''}
        ${c.output && c.output.file ? '<span class="badge">file ✓</span>' : ''}
        ${c.reel_output && c.reel_output.file ? '<span class="badge">reel ✓</span>' : ''}
      </div>
      ${why ? `<div class="why${expandedWhy.has(c.id) ? '' : ' clamp'}" id="why-${c.id}">${why}</div>
               <button class="more" onclick="event.stopPropagation();toggleWhy('${c.id}',this)">${expandedWhy.has(c.id) ? 'less' : 'more'}</button>` : ''}
      <div class="acts">
        <button class="${c.status === 'approved' ? 'on-ok' : ''}"
                onclick="event.stopPropagation();setStatus('${c.id}','approved',this)">Approve</button>
        <button class="${c.status === 'rejected' ? 'on-no' : ''}"
                onclick="event.stopPropagation();setStatus('${c.id}','rejected',this)">Reject</button>
        <button onclick="event.stopPropagation();openCompilationPicker('${c.id}')"
                title="Add this clip's range to a compilation">+ Compilation</button>
      </div>
    </div>`;
  }).join('');
}

function toggleWhy(id, btn) {
  const el = document.getElementById('why-' + id);
  const clamped = el.classList.toggle('clamp');
  btn.textContent = clamped ? 'more' : 'less';
  if (clamped) expandedWhy.delete(id); else expandedWhy.add(id);
}
window.toggleWhy = toggleWhy;

function selectClip(id) {
  if (dirty && !confirm('You have unsaved in/out changes. Discard them?')) return;
  selected = clips.find(c => c.id === id) || null;
  if (!selected) return;
  dirty = false;
  document.getElementById('editor').hidden = false;
  document.getElementById('editor').classList.remove('dirty');
  document.getElementById('ed-title').textContent =
    selected.title || (selected.description || '').slice(0, 60) || selected.id;
  document.getElementById('in-val').value = formatClock(selected.start);
  document.getElementById('out-val').value = formatClock(selected.end);
  syncField('in-val'); syncField('out-val');
  seekTo(selected.start);
  player.play().catch(() => {});
  renderClips();
  drawClipMarkers();
  drawZoom();
  if (window.loadReelForClip) loadReelForClip(selected);
  const card = document.getElementById('card-' + id);
  if (card) card.scrollIntoView({ block: 'nearest' });
}
window.selectClip = selectClip;

async function setStatus(id, status, btn) {
  // loadClips() on success fully rebuilds the clip list, so the disabled
  // flag set here only needs to survive long enough to block a second click
  // on *this* button before that happens - re-enabling in `finally` still
  // matters for the failure path, where the card (and this button) survives.
  if (btn) btn.disabled = true;
  try {
    const clip = clips.find(c => c.id === id);
    // Clicking the active status again clears it back to pending.
    const next = (clip && clip.status === status) ? 'pending' : status;
    await api(`/api/workspaces/${SLUG}/clips/${id}`, 'PATCH', { status: next });
    await loadClips();
    toast(next === 'pending' ? 'Cleared' : next.charAt(0).toUpperCase() + next.slice(1));
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}
window.setStatus = setStatus;

async function addManualClip() {
  const start = parseClock(document.getElementById('new-start').value);
  const end = parseClock(document.getElementById('new-end').value);
  if (isNaN(start) || isNaN(end) || end <= start) {
    toast('Enter a valid in and out, e.g. 41:00 and 43:00', true);
    return;
  }
  const btn = document.getElementById('add-clip-btn');
  if (btn) btn.disabled = true;
  try {
    const clip = await api(`/api/workspaces/${SLUG}/clips`, 'POST', { start, end });
    document.getElementById('new-start').value = '';
    document.getElementById('new-end').value = '';
    await loadClips();
    selectClip(clip.id);
    toast('Clip added — not cut yet');
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}
window.addManualClip = addManualClip;

function useSelection() {
  const t = player.currentTime;
  document.getElementById('new-start').value = formatClock(t);
  document.getElementById('new-end').value = formatClock(Math.min(duration || t + 30, t + 30));
}
window.useSelection = useSelection;

async function cutApproved() {
  const btn = document.getElementById('cut-btn');
  if (btn) btn.disabled = true;
  try {
    await api(`/api/workspaces/${SLUG}/jobs`, 'POST', { kind: 'cut' });
    toast('Cutting started — watch the workspace page');
  } catch (e) {
    toast(e.message, true);
  } finally {
    // loadClips() (via the clips/workspace SSE events the cut job will fire)
    // recomputes this button's disabled state from the approved count as
    // the job progresses - this just guards the POST itself against a
    // double-click, not the longer-lived "no approved clips" state.
    if (btn) btn.disabled = false;
  }
}
window.cutApproved = cutApproved;

/* ---------------- transcript (virtualised) ---------------- */

const ROW_H = 46;
let txView = [];        // indices into `segments` after search/filter
let txQuery = '';
let captionsIndex = null;                 // Map(segment id -> Hinglish text), or null if none
let txScript = localStorage.getItem('clipbot.txScript') || 'dev';   // 'dev' | 'hin'

async function loadTranscript() {
  // Fetched together and rendered once, rather than rendering Devanagari
  // first and re-rendering in Hinglish once captions arrive - that sequence
  // flashed the wrong script for anyone whose saved preference was already
  // 'hin'.
  const [data, captions] = await Promise.all([
    api(`/api/workspaces/${SLUG}/transcript`),
    loadCaptionsIndex(SLUG),
  ]);
  segments = data.segments || [];
  captionsIndex = captions;
  const toggle = document.getElementById('tx-script-toggle');
  if (toggle) toggle.hidden = !captionsIndex;
  if (duration) drawFullTimeline();
  setTxScript(txScript);
}

// What a transcript row actually shows - the Hinglish text if that pass ran
// and the toggle is set to it, Devanagari otherwise (including any segment a
// failed transliteration batch left untranslated - captions.json already
// carries the Devanagari fallback for those, see transliterate.py).
function displayText(s) {
  if (txScript === 'hin' && captionsIndex && captionsIndex.has(s.id)) {
    return captionsIndex.get(s.id);
  }
  return s.text || '';
}

function setTxScript(mode) {
  txScript = mode;
  localStorage.setItem('clipbot.txScript', mode);
  document.querySelectorAll('#tx-script-toggle button').forEach(b =>
    b.classList.toggle('on', b.dataset.script === mode));
  document.getElementById('tx-search').placeholder = mode === 'hin'
    ? 'Search transcript (Hinglish)…'
    : 'Search transcript (Devanagari — देवनागरी)…';
  applyTranscriptFilter();
}
window.setTxScript = setTxScript;

function applyTranscriptFilter() {
  const hideLow = document.getElementById('tx-hide-low').checked;
  const q = txQuery.trim().toLowerCase();
  txView = [];
  for (let i = 0; i < segments.length; i++) {
    const s = segments[i];
    const low = s.low_confidence || (s.flags && s.flags.length);
    if (hideLow && low) continue;
    if (q && !displayText(s).toLowerCase().includes(q)) continue;
    txView.push(i);
  }
  document.getElementById('tx-count').textContent =
    q || hideLow ? `${txView.length} / ${segments.length}` : `${segments.length} lines`;
  const inner = document.getElementById('tx-inner');
  inner.style.height = (txView.length * ROW_H) + 'px';
  renderTxWindow(true);
}

// Only the visible slice is in the DOM - 1200+ rows as real nodes made the
// page sluggish and produced 53,000px of scroll.
function renderTxWindow(force) {
  const scroll = document.getElementById('tx-scroll');
  const inner = document.getElementById('tx-inner');
  const top = scroll.scrollTop;
  const h = scroll.clientHeight;
  const first = Math.max(0, Math.floor(top / ROW_H) - 6);
  const last = Math.min(txView.length, Math.ceil((top + h) / ROW_H) + 6);
  if (!force && inner._first === first && inner._last === last) return;
  inner._first = first; inner._last = last;

  const t = player ? player.currentTime : 0;
  const selS = selected ? editedStart() : null;
  const selE = selected ? editedEnd() : null;
  let html = '';
  for (let vi = first; vi < last; vi++) {
    const i = txView[vi];
    const s = segments[i];
    const low = s.low_confidence || (s.flags && s.flags.length);
    const active = s.start <= t && t <= s.end;
    const inClip = selS != null && !isNaN(selS) && s.end > selS && s.start < selE;
    const spkId = segmentSpeakers[s.id];
    const spk = speakerRegistry[spkId];
    // A raw diarization cluster label with no library profile yet - give it
    // some visible marker too, rather than rendering identically to a
    // segment with no speaker assignment at all (the previous behavior:
    // `spk` was undefined either way, so an un-named cluster was invisible).
    const isRawCluster = spkId && !spk;
    const inRange = speakerMode && rangeStart != null && (
      rangeEnd != null
        ? (s.id >= Math.min(rangeStart, rangeEnd) && s.id <= Math.max(rangeStart, rangeEnd))
        : s.id === rangeStart
    );
    const border = spk ? `border-left:4px solid ${esc(spk.color || '#19A2D2')}`
                 : isRawCluster ? `border-left:4px dashed #888` : '';
    const rowTitle = spk ? esc(spk.name) : isRawCluster ? esc(spkId) + ' (unnamed cluster)' : '';
    html += `<div class="tx-row${low ? ' lowconf' : ''}${active ? ' active' : ''}${inClip ? ' inclip' : ''}${inRange ? ' spkrange' : ''}"
                  style="top:${vi * ROW_H}px;height:${ROW_H}px;${border}"
                  data-i="${i}" data-id="${s.id}" data-start="${s.start}" title="${rowTitle}">
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

function wireTranscriptControls() {
  const scroll = document.getElementById('tx-scroll');
  scroll.addEventListener('scroll', () => renderTxWindow(false));

  scroll.addEventListener('click', (ev) => {
    const row = ev.target.closest('.tx-row');
    if (!row) return;
    const start = parseFloat(row.dataset.start);

    // Speaker-assign mode repurposes clicks entirely (first click = range
    // start, second = range end) rather than overloading shift-click, which
    // already means something else here (set the clip's out point).
    if (speakerMode) {
      const segId = parseInt(row.dataset.id, 10);
      if (rangeStart == null || rangeEnd != null) {
        rangeStart = segId;
        rangeEnd = null;
      } else {
        rangeEnd = segId;
      }
      updateSpeakerRangeUI();
      renderTxWindow(true);
      return;
    }

    // Shift-click sets the out point - build a clip straight from the
    // transcript, which is where you can actually see what was said.
    if (ev.shiftKey && selected) {
      setField('end', start);
      drawZoom();
      toast('Out point set from transcript');
      return;
    }
    seekTo(start);
    player.play().catch(() => {});
  });

  const search = document.getElementById('tx-search');
  let debounce;
  search.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => { txQuery = search.value; applyTranscriptFilter(); }, 120);
  });
  document.getElementById('tx-hide-low').addEventListener('change', applyTranscriptFilter);

  const clipSearch = document.getElementById('clip-search');
  if (clipSearch) {
    let cd;
    clipSearch.addEventListener('input', () => {
      clearTimeout(cd);
      cd = setTimeout(() => { clipQuery = clipSearch.value; renderClips(); }, 120);
    });
  }
}

/* ---------------- speaker assignment ---------------- */
//
// Deliberately click-click rather than drag-select: the transcript is
// virtualised (only visible rows exist as DOM nodes - see the top of this
// section), so a pointer-drag gesture would have to reason about rows that
// aren't currently rendered. Two clicks (range start, range end) work
// against segment ids directly and don't care what's scrolled into view.

let speakerRegistry = {};   // library speaker id -> {id, name, color, ...}
let segmentSpeakers = {};   // transcript segment id -> speaker id
let speakerMode = false;
let rangeStart = null, rangeEnd = null;

async function loadSpeakers() {
  try {
    const [reg, seg] = await Promise.all([
      api('/api/library/speakers'),
      api(`/api/workspaces/${SLUG}/speakers`),
    ]);
    speakerRegistry = {};
    (reg.speakers || []).forEach(s => { speakerRegistry[s.id] = s; });
    segmentSpeakers = {};
    (seg.segments || []).forEach(s => { if (s.speaker_id) segmentSpeakers[s.id] = s.speaker_id; });
    // Without this, a workspace where diarization never ran and one where it
    // ran but nothing's been named yet look identical - both just show an
    // empty speaker list.
    const hint = document.getElementById('diarize-hint');
    if (hint) hint.style.display = seg.diarized ? 'none' : '';
    renderSpeakerSelect();
    renderTxWindow(true);
  } catch (e) { /* best effort - a workspace with no transcript yet is fine */ }
}

function renderSpeakerSelect() {
  const sel = document.getElementById('speaker-assign-select');
  if (!sel) return;
  const prev = sel.value;
  const namedIds = Object.keys(speakerRegistry);
  // segmentSpeakers can hold raw diarization cluster labels (e.g. "SPEAKER_00")
  // that never went through library.save_speaker - list those too, so
  // "✎ edit" has something to name/rename them into, not just already-named
  // speakers.
  const rawIds = Array.from(new Set(Object.values(segmentSpeakers)))
    .filter(id => id && !speakerRegistry[id]);
  let html = namedIds.map(id =>
    `<option value="${id}">${esc(speakerRegistry[id].name)}</option>`
  ).join('');
  if (rawIds.length) {
    html += `<optgroup label="Unnamed clusters">` + rawIds.map(id =>
      `<option value="${esc(id)}">${esc(id)} (unnamed)</option>`
    ).join('') + `</optgroup>`;
  }
  html += '<option value="__new__">+ New speaker…</option>';
  sel.innerHTML = html;
  if (namedIds.includes(prev) || rawIds.includes(prev)) sel.value = prev;
}

function toggleSpeakerMode() {
  speakerMode = !speakerMode;
  rangeStart = rangeEnd = null;
  document.getElementById('speaker-mode-btn').classList.toggle('on', speakerMode);
  document.getElementById('speaker-assign-controls').style.display = speakerMode ? '' : 'none';
  updateSpeakerRangeUI();
  renderTxWindow(true);
}
window.toggleSpeakerMode = toggleSpeakerMode;

function updateSpeakerRangeUI() {
  const rangeEl = document.getElementById('speaker-assign-range');
  const assignBtn = document.getElementById('speaker-assign-btn');
  const clearBtn = document.getElementById('speaker-clear-btn');
  if (!rangeEl) return;
  if (rangeStart == null) {
    rangeEl.textContent = 'Click a transcript line for the range start…';
    assignBtn.disabled = true; clearBtn.disabled = true;
  } else if (rangeEnd == null) {
    rangeEl.textContent = `Start set. Click a line for the range end…`;
    assignBtn.disabled = true; clearBtn.disabled = true;
  } else {
    const lo = Math.min(rangeStart, rangeEnd), hi = Math.max(rangeStart, rangeEnd);
    rangeEl.textContent = `${hi - lo + 1} segment(s) selected`;
    assignBtn.disabled = false; clearBtn.disabled = false;
  }
}

function cancelSpeakerRange() {
  rangeStart = rangeEnd = null;
  updateSpeakerRangeUI();
  renderTxWindow(true);
}
window.cancelSpeakerRange = cancelSpeakerRange;

async function applySpeakerAssign() {
  if (rangeStart == null || rangeEnd == null) return;
  const sel = document.getElementById('speaker-assign-select');
  let speakerId = sel.value;
  if (!speakerId || speakerId === '__new__') {
    // Opens the shared speaker dialog instead of a prompt(); on save it sets
    // sel.value to the new speaker and re-enters this function, so the rest
    // of the assignment flow (including the confirm() below) still runs.
    openSpeakerDialog('create', { afterCreate: (newId) => {
      sel.value = newId;
      applySpeakerAssign();
    }});
    return;
  }
  const lo = Math.min(rangeStart, rangeEnd), hi = Math.max(rangeStart, rangeEnd);
  const speakerName = speakerRegistry[speakerId] ? speakerRegistry[speakerId].name : speakerId;
  if (!confirm(`Assign ${hi - lo + 1} segment(s) to ${speakerName}? This overwrites any existing assignment in that range.`)) return;
  const assignBtn = document.getElementById('speaker-assign-btn');
  const clearBtn = document.getElementById('speaker-clear-btn');
  if (assignBtn) assignBtn.disabled = true;
  if (clearBtn) clearBtn.disabled = true;
  try {
    await api(`/api/workspaces/${SLUG}/speakers/assign`, 'POST',
             { start_id: lo, end_id: hi, speaker_id: speakerId });
    for (let id = lo; id <= hi; id++) segmentSpeakers[id] = speakerId;
    toast(`Assigned ${hi - lo + 1} segment(s) to ${speakerName}`);
    rangeStart = rangeEnd = null;
    renderTxWindow(true);
  } catch (e) {
    toast(e.message, true);
  } finally {
    // Re-derives disabled state from the (possibly just-cleared) range
    // instead of hardcoding false, so a failed request correctly leaves
    // both buttons enabled for a retry.
    updateSpeakerRangeUI();
  }
}
window.applySpeakerAssign = applySpeakerAssign;

function editSelectedSpeaker() {
  const sel = document.getElementById('speaker-assign-select');
  const id = sel && sel.value;
  if (!id || id === '__new__') { toast('Pick a speaker or unnamed cluster first', true); return; }
  const speaker = speakerRegistry[id];
  if (speaker) {
    openSpeakerDialog('edit', {
      sourceId: id, name: speaker.name,
      avatar_asset: speaker.avatar_asset, color: speaker.color,
    });
  } else {
    // A raw diarization cluster label (e.g. "SPEAKER_00") with no library
    // profile yet - this is the "name this diarization cluster" action:
    // saving creates a new speaker and rewrites every segment currently
    // pointing at the raw label to point at it instead.
    openSpeakerDialog('rename', { sourceId: id, name: '' });
  }
}
window.editSelectedSpeaker = editSelectedSpeaker;

/* ---------- speaker dialog (create / edit / rename-a-cluster) ---------- */
// One dialog replaces what used to be three sequential native prompt() calls
// across two functions (a new-speaker name prompt in applySpeakerAssign, and
// an avatar-id + a color prompt in editSelectedSpeaker) - unstyled, blocking,
// and with no inline validation of the avatar-id/color format. The <input
// type=color> here can't produce an invalid value at all, unlike the old
// free-text color prompt.

let speakerDialogMode = null;       // 'create' | 'edit' | 'rename'
let speakerDialogSourceId = null;   // speaker id being edited, or raw cluster id being renamed
let speakerDialogAfterSave = null;  // optional (speakerId) => void, run after a successful save
let avatarAssetsCache = null;

async function loadAvatarAssetsCache() {
  if (avatarAssetsCache) return avatarAssetsCache;
  try {
    const index = await api('/api/library');
    avatarAssetsCache = (index.assets || []).filter(a => a.kind === 'avatar');
  } catch (e) {
    avatarAssetsCache = [];
  }
  return avatarAssetsCache;
}

async function openSpeakerDialog(mode, opts) {
  opts = opts || {};
  speakerDialogMode = mode;
  speakerDialogSourceId = opts.sourceId || null;
  speakerDialogAfterSave = opts.afterCreate || null;

  document.getElementById('speaker-dialog-title').textContent =
    mode === 'edit' ? 'Edit speaker' : 'New speaker';
  document.getElementById('speaker-dialog-source-hint').textContent =
    mode === 'rename'
      ? `Naming cluster "${opts.sourceId}" — every segment currently assigned to it will point at this speaker instead.`
      : '';
  document.getElementById('speaker-dialog-name').value = opts.name || '';
  document.getElementById('speaker-dialog-color').value = opts.color || '#19a2d2';
  document.getElementById('speaker-dialog-error').textContent = '';

  const assets = await loadAvatarAssetsCache();
  const avatarSel = document.getElementById('speaker-dialog-avatar');
  avatarSel.innerHTML = '<option value="">(none)</option>' +
    assets.map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join('');
  avatarSel.value = opts.avatar_asset || '';

  document.getElementById('speaker-dialog').showModal();
  document.getElementById('speaker-dialog-name').focus();
}
window.openSpeakerDialog = openSpeakerDialog;

async function saveSpeakerDialog() {
  const name = document.getElementById('speaker-dialog-name').value.trim();
  const errEl = document.getElementById('speaker-dialog-error');
  if (!name) { errEl.textContent = 'Enter a name.'; return; }
  const avatar_asset = document.getElementById('speaker-dialog-avatar').value || null;
  const color = document.getElementById('speaker-dialog-color').value;

  const btn = document.getElementById('speaker-dialog-save-btn');
  btn.disabled = true;
  try {
    const speaker = await api('/api/library/speakers', 'POST', {
      name, avatar_asset, color,
      speaker_id: speakerDialogMode === 'edit' ? speakerDialogSourceId : null,
    });
    speakerRegistry[speaker.id] = speaker;

    if (speakerDialogMode === 'rename' && speakerDialogSourceId) {
      await api(`/api/workspaces/${SLUG}/speakers/rename`, 'POST',
               { old_id: speakerDialogSourceId, new_id: speaker.id });
      for (const segId of Object.keys(segmentSpeakers)) {
        if (segmentSpeakers[segId] === speakerDialogSourceId) segmentSpeakers[segId] = speaker.id;
      }
    }

    renderSpeakerSelect();
    renderTxWindow(true);
    document.getElementById('speaker-dialog').close();
    toast(speakerDialogMode === 'edit' ? `Updated ${speaker.name}` : `Saved ${speaker.name}`);
    if (speakerDialogAfterSave) speakerDialogAfterSave(speaker.id);
  } catch (e) {
    errEl.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}
window.saveSpeakerDialog = saveSpeakerDialog;

async function clearSpeakerAssign() {
  if (rangeStart == null || rangeEnd == null) return;
  const lo = Math.min(rangeStart, rangeEnd), hi = Math.max(rangeStart, rangeEnd);
  if (!confirm(`Clear the speaker assignment for ${hi - lo + 1} segment(s)?`)) return;
  const assignBtn = document.getElementById('speaker-assign-btn');
  const clearBtn = document.getElementById('speaker-clear-btn');
  if (assignBtn) assignBtn.disabled = true;
  if (clearBtn) clearBtn.disabled = true;
  try {
    await api(`/api/workspaces/${SLUG}/speakers/assign`, 'POST',
             { start_id: lo, end_id: hi, speaker_id: null });
    for (let id = lo; id <= hi; id++) delete segmentSpeakers[id];
    toast(`Cleared speaker for ${hi - lo + 1} segment(s)`);
    rangeStart = rangeEnd = null;
    renderTxWindow(true);
  } catch (e) {
    toast(e.message, true);
  } finally {
    updateSpeakerRangeUI();
  }
}
window.clearSpeakerAssign = clearSpeakerAssign;

let lastActiveSeg = -1;
function highlightTranscript(t) {
  if (!segments.length) return;
  let idx = -1;
  // Linear from the last hit - playback moves forward, so this is O(1) typically.
  const startFrom = (lastActiveSeg >= 0 && segments[lastActiveSeg] &&
                     segments[lastActiveSeg].start <= t) ? lastActiveSeg : 0;
  for (let i = startFrom; i < segments.length; i++) {
    if (segments[i].start <= t && t <= segments[i].end) { idx = i; break; }
    if (segments[i].start > t) break;
  }
  if (idx === lastActiveSeg) return;
  lastActiveSeg = idx;
  renderTxWindow(true);

  if (idx >= 0 && document.getElementById('tx-follow').checked) {
    const vi = txView.indexOf(idx);
    if (vi >= 0) {
      const scroll = document.getElementById('tx-scroll');
      const target = vi * ROW_H - scroll.clientHeight / 2;
      if (Math.abs(scroll.scrollTop - target) > scroll.clientHeight / 2) {
        scroll.scrollTop = Math.max(0, target);
      }
    }
  }
}

/* ---------------- keyboard ---------------- */

function showKeys() { document.getElementById('keys-dialog').showModal(); }
window.showKeys = showKeys;

function onKey(e) {
  const tag = e.target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) {
    if (e.key === 'Escape') e.target.blur();
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  const list = visibleClips();
  const idx = selected ? list.findIndex(c => c.id === selected.id) : -1;

  switch (e.key) {
    case ' ': e.preventDefault(); togglePlay(); break;
    case 'ArrowLeft': e.preventDefault(); seekBy(e.shiftKey ? -10 : -1); break;
    case 'ArrowRight': e.preventDefault(); seekBy(e.shiftKey ? 10 : 1); break;
    case ',': step(-1 / 30); break;
    case '.': step(1 / 30); break;
    case '[': nudge('start', e.shiftKey ? -1 : -0.1); break;
    case ']': nudge('end', e.shiftKey ? 1 : 0.1); break;
    case 'i': case 'I': markHere('start'); break;
    case 'o': case 'O': markHere('end'); break;
    case 'r': case 'R': previewClip(); break;
    case 'l': case 'L': {
      const c = document.getElementById('loop'); c.checked = !c.checked;
      toast(c.checked ? 'Loop on' : 'Loop off');
      break;
    }
    case 'a': case 'A': if (selected) setStatus(selected.id, 'approved'); break;
    case 'x': case 'X': if (selected) setStatus(selected.id, 'rejected'); break;
    case 'n': case 'N': if (list.length) selectClip(list[Math.min(idx + 1, list.length - 1)].id); break;
    case 'p': case 'P': if (list.length) selectClip(list[Math.max(idx - 1, 0)].id); break;
    case 'Enter': if (selected) saveEdits(); break;
    case '/': e.preventDefault(); document.getElementById('tx-search').focus(); break;
    case '?': showKeys(); break;
    default: return;
  }
}

// Exposed for fx.js: the FX lane draws into the same window as #tl-zoom, and
// new effects are placed at the playhead. Both are already in absolute VOD
// time, which is the time base effects are stored in.
window.zoomWindow = zoomWindow;
window.selectedClip = () => selected;
