/* Vertical reel authoring: crop rects over the video + live canvas preview.
 *
 * The browser does no layout maths. Every geometry question goes to
 * POST /reel/plan, which runs the same reelspec.resolve() the renderer uses -
 * so the preview cannot drift from the output.
 */

let reelSpec = null;      // the spec being edited
let reelPlan = null;      // last resolved plan from the server
let activeRect = null;    // 'game' | 'cam' | null
let reelDirty = false;
let planTimer = null;

// Cached once per workspace load (chat.json doesn't change per clip), used to
// tell a genuinely empty chat window apart from the feature not working at
// all - on this channel roughly half of all 45s windows have zero messages,
// which looks identical to "broken" unless the count is shown.
let chatAllMessages = null;   // workspace-corrected offsets, from /chat/messages
let chatWsOffset = 0;
let chatWindowCount = null;   // messages in the current clip's padded window, or null if unknown

const PRESET_LABELS = {
  cam_top: 'Cam top',
  cam_bottom: 'Cam bottom',
  blur_fill: 'Blurred bg',
  pip: 'PiP',
  game_only: 'Game only',
};

function defaultSpec() {
  return {
    preset: 'cam_top',
    canvas: '1080x1920',
    src: { cam: { x: 0.025, y: 0.30, w: 0.25, h: 0.25 } },
  };
}

/* ---------- coordinate mapping ---------- */

// The video is object-fit:contain inside its box, so the painted frame is
// letterboxed. Every mapping goes through here; nowhere else touches pixels.
function paintRect() {
  const v = document.getElementById('player');
  if (!v) return null;
  const r = v.getBoundingClientRect();
  const vw = v.videoWidth || (reelPlan && reelPlan.source[0]) || 0;
  const vh = v.videoHeight || (reelPlan && reelPlan.source[1]) || 0;
  if (!vw || !vh || !r.width) return null;
  const scale = Math.min(r.width / vw, r.height / vh);
  const w = vw * scale, h = vh * scale;
  return {
    left: r.left + (r.width - w) / 2,
    top: r.top + (r.height - h) / 2,
    width: w, height: h, vw, vh,
  };
}

function wrapOffset() {
  const wrap = document.getElementById('stage');
  return wrap ? wrap.getBoundingClientRect() : { left: 0, top: 0 };
}

/* ---------- spec plumbing ---------- */

function currentSpec() {
  if (!reelSpec) reelSpec = defaultSpec();
  return reelSpec;
}

function specRect(which) {
  const s = currentSpec();
  if (which === 'cam') return (s.src && s.src.cam) || defaultSpec().src.cam;
  // The game rect is normally derived server-side; once dragged it's explicit.
  if (s.src && s.src.game) return s.src.game;
  if (reelPlan) {
    const [sw, sh] = reelPlan.source;
    const g = reelPlan.game.src;
    return { x: g[0] / sw, y: g[1] / sh, w: g[2] / sw, h: g[3] / sh };
  }
  return { x: 0.25, y: 0, w: 0.5, h: 1 };
}

function setSpecRect(which, rect) {
  const s = currentSpec();
  s.src = s.src || {};
  s.src[which] = {
    x: Math.max(0, Math.min(1, +rect.x.toFixed(4))),
    y: Math.max(0, Math.min(1, +rect.y.toFixed(4))),
    w: Math.max(0.01, Math.min(1, +rect.w.toFixed(4))),
    h: Math.max(0.01, Math.min(1, +rect.h.toFixed(4))),
  };
  markReelDirty();
}

function markReelDirty() {
  reelDirty = true;
  const el = document.getElementById('reel-panel');
  if (el) el.classList.add('dirty');
}

/* ---------- planning + preview ---------- */

async function replan(immediate) {
  clearTimeout(planTimer);
  const run = async () => {
    try {
      reelPlan = await api(`/api/workspaces/${SLUG}/reel/plan`, 'POST',
                           { spec: currentSpec(),
                             clip_id: selected ? selected.id : null });
      drawReelPreview();
      drawCropRects();
      updateUpscaleBadge();
      // Still pure maths on the server - the plan now also carries the resolved
      // effect warnings, so the FX lane stays instant and ffmpeg-free.
      if (window.fxOnPlan) window.fxOnPlan(reelPlan);
    } catch (e) {
      const b = document.getElementById('reel-badge');
      if (b) { b.textContent = e.message; b.className = 'badge bad'; }
    }
  };
  if (immediate) return run();
  planTimer = setTimeout(run, 130);
}

function updateUpscaleBadge() {
  const b = document.getElementById('reel-badge');
  if (!b || !reelPlan) return;
  const u = reelPlan.upscale;
  const warn = reelPlan.warn_above || 1.5;
  const worst = Math.max(u.game || 0, u.cam || 0);
  const parts = [`gameplay ${u.game.toFixed(2)}×`];
  if (u.cam) parts.push(`webcam ${u.cam.toFixed(2)}×`);
  b.textContent = parts.join(' · ');
  b.className = 'badge' + (worst > 2.5 ? ' bad' : worst > warn ? ' warn' : '');
  b.title = worst > warn
    ? `Upscaled from a ${reelPlan.source[1]}p source — will look soft. ` +
      `Options: use PiP or Blurred bg (webcam stays ~1.2×), drop the canvas to 720x1280, ` +
      `or re-download the VOD at 1080p60.`
    : 'No significant upscaling.';
}

function drawReelPreview() {
  const cv = document.getElementById('reel-canvas');
  const v = document.getElementById('player');
  if (!cv || !reelPlan || !v || !v.videoWidth) return;
  const ctx = cv.getContext('2d');
  const [cw, ch] = reelPlan.canvas;
  const k = cv.width / cw;

  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, cv.width, cv.height);

  const draw = (r) => {
    const [sx, sy, sw, sh] = r.src, [dx, dy, dw, dh] = r.dst;
    try { ctx.drawImage(v, sx, sy, sw, sh, dx * k, dy * k, dw * k, dh * k); }
    catch (e) { /* frame not ready */ }
  };

  if (reelPlan.mode === 'blur') {
    ctx.save();
    ctx.filter = 'blur(6px) brightness(0.9)';
    ctx.drawImage(v, 0, 0, v.videoWidth, v.videoHeight, 0, 0, cv.width, cv.height);
    ctx.restore();
  }
  draw(reelPlan.game);
  if (reelPlan.cam) draw(reelPlan.cam);

  // The chat panel's geometry (reelPlan.chat.rect) always resolves when chat
  // is enabled, whether or not this specific clip's window has any messages
  // in it - actual message content only exists at render time, rendered by
  // ffmpeg from PNGs this canvas never sees. So this draws the RESERVED SPACE
  // and a live count, not a facsimile of the real overlay; that's enough to
  // confirm the feature registered and where it'll sit, without pretending to
  // preview text this canvas has no way to produce.
  if (reelPlan.chat) {
    const [rx, ry, rw, rh] = reelPlan.chat.rect;
    ctx.save();
    ctx.fillStyle = 'rgba(25,162,210,.20)';
    ctx.fillRect(rx * k, ry * k, rw * k, rh * k);
    ctx.strokeStyle = 'rgba(25,162,210,.9)';
    ctx.lineWidth = 2;
    ctx.strokeRect(rx * k + 1, ry * k + 1, rw * k - 2, rh * k - 2);
    ctx.fillStyle = '#fff';
    ctx.font = '600 12px system-ui, sans-serif';
    ctx.textBaseline = 'top';
    const label = 'CHAT' + (chatWindowCount != null ? ` · ${chatWindowCount} msg` : '');
    ctx.fillText(label, rx * k + 6, ry * k + 6);
    ctx.restore();
  }

  // Same reserved-space treatment as chat above: caption text only exists at
  // render time (rendered by ffmpeg from captionrender.py's PNGs), so this
  // draws where the layer will sit, not a facsimile of the words.
  if (reelPlan.captions) {
    const [rx, ry, rw, rh] = reelPlan.captions.rect;
    ctx.save();
    ctx.fillStyle = 'rgba(224,164,25,.18)';
    ctx.fillRect(rx * k, ry * k, rw * k, rh * k);
    ctx.strokeStyle = 'rgba(224,164,25,.9)';
    ctx.lineWidth = 2;
    ctx.strokeRect(rx * k + 1, ry * k + 1, rw * k - 2, rh * k - 2);
    ctx.fillStyle = '#fff';
    ctx.font = '600 12px system-ui, sans-serif';
    ctx.textBaseline = 'top';
    ctx.fillText('CAPTIONS', rx * k + 6, ry * k + 6);
    ctx.restore();
  }

  // Instagram's chrome: caption block along the bottom, action column right.
  // Preview only - never baked into the render.
  if (document.getElementById('reel-safe') &&
      document.getElementById('reel-safe').checked) {
    ctx.fillStyle = 'rgba(224,90,90,.16)';
    ctx.fillRect(0, cv.height - (380 * k), cv.width, 380 * k);
    ctx.fillRect(cv.width - (160 * k), 0, 160 * k, cv.height);
  }
}

/* ---------- draggable crop rects over the video ---------- */

// Split from drawCropRects on purpose: that function replaces #crop-layer's
// innerHTML wholesale, which destroys and recreates #crop-rect. Calling it on
// every pointermove (as this used to) meant every drag tick tore out the very
// element holding pointer capture - the browser drops capture the instant a
// captured element leaves the DOM, and the freshly rebound bindRectDrag() got
// a brand-new closure with mode reset to null. The visible result was a drag
// that "moved very slightly on each click but didn't sustain" - each move
// landed once, then the element was replaced out from under the gesture.
// This function only ever touches style on the existing nodes, so capture and
// in-progress drag state both survive a move event.
function positionCropRect() {
  const host = document.getElementById('crop-layer');
  const rectEl = document.getElementById('crop-rect');
  const p = paintRect();
  if (!host || !rectEl || !p || !activeRect) return false;
  const off = wrapOffset();
  const r = specRect(activeRect);
  const left = (p.left - off.left) + r.x * p.width;
  const top = (p.top - off.top) + r.y * p.height;
  const w = r.w * p.width, h = r.h * p.height;

  rectEl.style.left = left + 'px';
  rectEl.style.top = top + 'px';
  rectEl.style.width = w + 'px';
  rectEl.style.height = h + 'px';

  const shades = host.querySelectorAll('.crop-shade');
  if (shades.length === 4) {
    shades[0].style.height = top + 'px';
    shades[1].style.top = (top + h) + 'px';
    shades[2].style.top = top + 'px'; shades[2].style.width = left + 'px'; shades[2].style.height = h + 'px';
    shades[3].style.left = (left + w) + 'px'; shades[3].style.top = top + 'px'; shades[3].style.height = h + 'px';
  }
  return true;
}

function drawCropRects() {
  const host = document.getElementById('crop-layer');
  if (!host) return;
  const p = paintRect();
  if (!p || !activeRect) { host.innerHTML = ''; return; }
  const off = wrapOffset();
  const r = specRect(activeRect);
  const left = (p.left - off.left) + r.x * p.width;
  const top = (p.top - off.top) + r.y * p.height;
  const w = r.w * p.width, h = r.h * p.height;

  host.innerHTML =
    `<div class="crop-shade" style="left:0;top:0;right:0;height:${top}px"></div>` +
    `<div class="crop-shade" style="left:0;top:${top + h}px;right:0;bottom:0"></div>` +
    `<div class="crop-shade" style="left:0;top:${top}px;width:${left}px;height:${h}px"></div>` +
    `<div class="crop-shade" style="left:${left + w}px;top:${top}px;right:0;height:${h}px"></div>` +
    `<div class="crop-rect ${activeRect}" id="crop-rect"
          style="left:${left}px;top:${top}px;width:${w}px;height:${h}px">
       <span class="crop-tag">${activeRect === 'cam' ? 'webcam' : 'gameplay'}</span>
       ${['nw','n','ne','e','se','s','sw','w'].map(d =>
          `<i class="h ${d}" data-dir="${d}"></i>`).join('')}
     </div>`;
  bindRectDrag();
}

function bindRectDrag() {
  const rectEl = document.getElementById('crop-rect');
  if (!rectEl) return;
  let mode = null, dir = null, startPt = null, startRect = null;

  const onDown = (ev) => {
    const p = paintRect();
    if (!p) return;
    ev.preventDefault();
    dir = ev.target.dataset ? ev.target.dataset.dir : null;
    mode = dir ? 'resize' : 'move';
    startPt = { x: ev.clientX, y: ev.clientY };
    startRect = Object.assign({}, specRect(activeRect));
    rectEl.setPointerCapture(ev.pointerId);
    rectEl.classList.add('drag');
  };

  const onMove = (ev) => {
    if (!mode) return;
    const p = paintRect();
    if (!p) return;
    const dx = (ev.clientX - startPt.x) / p.width;
    const dy = (ev.clientY - startPt.y) / p.height;
    let { x, y, w, h } = startRect;

    if (mode === 'move') {
      x = Math.max(0, Math.min(1 - w, x + dx));
      y = Math.max(0, Math.min(1 - h, y + dy));
    } else {
      if (dir.includes('w')) { const nx = Math.min(x + dx, x + w - 0.02); w += x - nx; x = nx; }
      if (dir.includes('e')) { w = Math.max(0.02, w + dx); }
      if (dir.includes('n')) { const ny = Math.min(y + dy, y + h - 0.02); h += y - ny; y = ny; }
      if (dir.includes('s')) { h = Math.max(0.02, h + dy); }
      x = Math.max(0, x); y = Math.max(0, y);
      w = Math.min(w, 1 - x); h = Math.min(h, 1 - y);
    }
    setSpecRect(activeRect, { x, y, w, h });
    if (!positionCropRect()) drawCropRects();
    replan(false);
  };

  const onUp = (ev) => {
    if (!mode) return;
    mode = null;
    rectEl.classList.remove('drag');
    try { rectEl.releasePointerCapture(ev.pointerId); } catch (e) {}
    replan(true);
  };

  rectEl.addEventListener('pointerdown', onDown);
  rectEl.addEventListener('pointermove', onMove);
  rectEl.addEventListener('pointerup', onUp);
  rectEl.addEventListener('pointercancel', onUp);
}

/* ---------- chat overlay ---------- */

function setChatEnabled(on) {
  const s = currentSpec();
  if (on) {
    s.chat = s.chat || { mode: 'overlay', side: 'bottom', size: 0.34, offset: null };
  } else {
    delete s.chat;
  }
  document.getElementById('chat-controls').style.display = on ? '' : 'none';
  markReelDirty();
  replan(true);
  updateChatWindowCount();
}
window.setChatEnabled = setChatEnabled;

function setChatField(field, value) {
  const s = currentSpec();
  s.chat = s.chat || { mode: 'overlay', side: 'bottom', size: 0.34, offset: null };
  s.chat[field] = value;
  if (field === 'size') {
    document.getElementById('chat-size-label').textContent = Math.round(value * 100) + '%';
  }
  markReelDirty();
  replan(true);
  if (field === 'offset') updateChatWindowCount();
}
window.setChatField = setChatField;

/* ---------- chat message-count diagnostic ---------- */
//
// Chat overlay geometry always resolves when enabled (drawn in
// drawReelPreview), but whether anything actually appears in the RENDER
// depends on whether the clip's own time window has any messages in it - on
// this channel roughly half of all 45s windows don't. Without this, an
// empty-window clip and a genuinely broken feature look identical to a user
// staring at an unchanged preview. Fetched once per workspace load (the
// message list doesn't change per clip) and recomputed locally per clip.

async function loadChatMessagesCache() {
  chatAllMessages = null;
  chatWsOffset = 0;
  try {
    const r = await api(`/api/workspaces/${SLUG}/chat/messages`);
    chatAllMessages = r.messages || [];
    chatWsOffset = r.chat_offset_seconds || 0;
  } catch (e) {
    chatAllMessages = [];
  }
  updateChatWindowCount();
}

function updateChatWindowCount() {
  const el = document.getElementById('chat-window-count');
  const chat = currentSpec().chat;
  if (!chat) {
    chatWindowCount = null;
    if (el) el.textContent = '';
    return;
  }
  if (!chatAllMessages) {
    if (el) el.textContent = 'checking…';
    return;
  }
  const s = editedStart(), e = editedEnd();
  if (isNaN(s) || isNaN(e)) { chatWindowCount = null; if (el) el.textContent = ''; return; }

  // /chat/messages already applies the workspace's chat_offset_seconds. A
  // per-clip override needs a *different* correction, and re-deriving it from
  // raw timestamps here would mean re-fetching - cheaper to shift the window
  // by the delta instead: at_clip = at_ws - (offset_clip - offset_ws), so
  // counting at_clip in [s,e] is the same as counting at_ws in [s+delta,e+delta].
  const delta = chat.offset != null ? (chat.offset - chatWsOffset) : 0;
  const lo = (s - PAD_START) + delta, hi = (e + PAD_END) + delta;
  chatWindowCount = chatAllMessages.filter(m => m.offset >= lo && m.offset <= hi).length;

  if (el) {
    el.textContent = chatWindowCount === 0
      ? '0 messages in this window — chat is sparse on this channel, nothing will render for this clip'
      : `${chatWindowCount} message${chatWindowCount === 1 ? '' : 's'} in this window`;
    el.style.color = chatWindowCount === 0 ? 'var(--warn)' : '';
  }
  drawReelPreview();
}
window.updateChatWindowCount = updateChatWindowCount;

function renderChatControls() {
  const s = currentSpec();
  const chat = s.chat;
  const enabled = document.getElementById('chat-enabled');
  const controls = document.getElementById('chat-controls');
  if (!enabled || !controls) return;
  enabled.checked = !!chat;
  controls.style.display = chat ? '' : 'none';
  if (chat) {
    document.getElementById('chat-mode').value = chat.mode || 'overlay';
    document.getElementById('chat-side').value = chat.side || 'bottom';
    const size = chat.size != null ? chat.size : 0.34;
    document.getElementById('chat-size').value = size;
    document.getElementById('chat-size-label').textContent = Math.round(size * 100) + '%';
    document.getElementById('chat-clip-offset').value = chat.offset != null ? chat.offset : '';
  }
}

async function loadChatSyncPanel() {
  try {
    const { chat_offset_seconds } = await api(`/api/workspaces/${SLUG}/chat/offset`);
    const el = document.getElementById('chat-ws-offset');
    if (el) el.value = chat_offset_seconds;
  } catch (e) { /* workspace not ready yet */ }
}

async function setWorkspaceChatOffset(value) {
  const v = parseFloat(value);
  if (Number.isNaN(v)) return;
  try {
    await api(`/api/workspaces/${SLUG}/chat/offset`, 'PATCH', { chat_offset_seconds: v });
    toast(`Workspace chat offset set to ${v}s`);
  } catch (e) { toast(e.message, true); }
}
window.setWorkspaceChatOffset = setWorkspaceChatOffset;

async function estimateChatOffset() {
  const out = document.getElementById('chat-sync-result');
  if (out) out.textContent = 'Correlating chat against audio…';
  try {
    const r = await api(`/api/workspaces/${SLUG}/chat/sync`, 'POST', {});
    const bits = [`correlation: ${r.offset_seconds >= 0 ? '+' : ''}${r.offset_seconds}s (z=${r.z_score})`];
    if (!r.confident) bits.push('LOW CONFIDENCE');
    if (r.boundary_offset_seconds != null) {
      bits.push(`boundary check: ${r.boundary_offset_seconds >= 0 ? '+' : ''}${r.boundary_offset_seconds}s`);
    }
    if (r.warning) bits.push(r.warning);
    if (out) out.textContent = bits.join(' · ');

    const suggested = (!r.confident && r.boundary_offset_seconds != null)
      ? r.boundary_offset_seconds : r.offset_seconds;
    const el = document.getElementById('chat-ws-offset');
    if (el) el.value = suggested;
    toast(`Suggested offset: ${suggested}s — click the field and save if it looks right`);
  } catch (e) {
    if (out) out.textContent = '';
    toast(e.message, true);
  }
}
window.estimateChatOffset = estimateChatOffset;

/* ---------- Hinglish captions overlay ---------- */

function setCaptionsEnabled(on) {
  const s = currentSpec();
  if (on) {
    s.captions = s.captions || { position: 'bottom', size: 0.18, max_lines: 2, offset: null };
  } else {
    delete s.captions;
  }
  document.getElementById('captions-controls').style.display = on ? '' : 'none';
  markReelDirty();
  replan(true);
}
window.setCaptionsEnabled = setCaptionsEnabled;

function setCaptionsField(field, value) {
  const s = currentSpec();
  s.captions = s.captions || { position: 'bottom', size: 0.18, max_lines: 2, offset: null };
  s.captions[field] = value;
  if (field === 'size') {
    document.getElementById('captions-size-label').textContent = Math.round(value * 100) + '%';
  }
  markReelDirty();
  replan(true);
}
window.setCaptionsField = setCaptionsField;

function renderCaptionsControls() {
  const s = currentSpec();
  const captions = s.captions;
  const enabled = document.getElementById('captions-enabled');
  const controls = document.getElementById('captions-controls');
  if (!enabled || !controls) return;
  enabled.checked = !!captions;
  controls.style.display = captions ? '' : 'none';
  if (captions) {
    document.getElementById('captions-position').value = captions.position || 'bottom';
    document.getElementById('captions-max-lines').value = captions.max_lines != null ? captions.max_lines : 2;
    const size = captions.size != null ? captions.size : 0.18;
    document.getElementById('captions-size').value = size;
    document.getElementById('captions-size-label').textContent = Math.round(size * 100) + '%';
    document.getElementById('captions-offset').value = captions.offset != null ? captions.offset : '';
  }
}

/* ---------- speaker avatar overlay ---------- */

function setSpeakersEnabled(on) {
  const s = currentSpec();
  if (on) {
    s.speakers = s.speakers || {
      mode: 'appear', edge: 'bottom', avatar_size: 0.16, gap: 0.02, ring_width_px: 6,
    };
  } else {
    delete s.speakers;
  }
  document.getElementById('speakers-controls').style.display = on ? '' : 'none';
  markReelDirty();
  replan(true);
}
window.setSpeakersEnabled = setSpeakersEnabled;

function setSpeakersField(field, value) {
  const s = currentSpec();
  s.speakers = s.speakers || {
    mode: 'appear', edge: 'bottom', avatar_size: 0.16, gap: 0.02, ring_width_px: 6,
  };
  s.speakers[field] = value;
  if (field === 'avatar_size') {
    document.getElementById('speakers-size-label').textContent = Math.round(value * 100) + '%';
  }
  markReelDirty();
  replan(true);
}
window.setSpeakersField = setSpeakersField;

function renderSpeakersControls() {
  const s = currentSpec();
  const speakers = s.speakers;
  const enabled = document.getElementById('speakers-enabled');
  const controls = document.getElementById('speakers-controls');
  if (!enabled || !controls) return;
  enabled.checked = !!speakers;
  controls.style.display = speakers ? '' : 'none';
  if (speakers) {
    document.getElementById('speakers-mode').value = speakers.mode || 'appear';
    document.getElementById('speakers-edge').value = speakers.edge || 'bottom';
    const size = speakers.avatar_size != null ? speakers.avatar_size : 0.16;
    document.getElementById('speakers-size').value = size;
    document.getElementById('speakers-size-label').textContent = Math.round(size * 100) + '%';
    document.getElementById('speakers-ring-width').value =
      speakers.ring_width_px != null ? speakers.ring_width_px : 6;
  }
}

/* ---------- controls ---------- */

function setReelPreset(preset) {
  currentSpec().preset = preset;
  // Preset implies geometry; drop stored overrides that would contradict it.
  delete currentSpec().layout;
  delete currentSpec().cam_edge;
  delete currentSpec().cam_enabled;
  // The auto game crop depends on the pane aspect, so let the server redo it
  // unless the user has explicitly dragged one.
  if (!reelSpec._gameDragged && reelSpec.src) delete reelSpec.src.game;
  markReelDirty();
  renderReelControls();
  replan(true);
}
window.setReelPreset = setReelPreset;

function editRect(which) {
  activeRect = activeRect === which ? null : which;
  if (which === 'game' && activeRect) {
    // Freeze the derived crop so dragging starts from what's on screen.
    const r = specRect('game');
    setSpecRect('game', r);
    reelSpec._gameDragged = true;
  }
  renderReelControls();
  drawCropRects();
}
window.editRect = editRect;

function resetReel() {
  reelSpec = defaultSpec();
  activeRect = null;
  markReelDirty();
  renderReelControls();
  drawCropRects();
  replan(true);
}
window.resetReel = resetReel;

function renderReelControls() {
  const host = document.getElementById('reel-presets');
  if (!host) return;
  const cur = currentSpec().preset;
  host.innerHTML = Object.keys(PRESET_LABELS).map(k =>
    `<button class="${k === cur ? 'on' : ''}" onclick="setReelPreset('${k}')">${PRESET_LABELS[k]}</button>`
  ).join('');
  ['game', 'cam'].forEach(w => {
    const b = document.getElementById('edit-' + w);
    if (b) b.classList.toggle('on', activeRect === w);
  });
}

async function saveReel() {
  if (!selected) { toast('Select a clip first', true); return; }
  try {
    await api(`/api/workspaces/${SLUG}/clips/${selected.id}`, 'PATCH',
              { reel: currentSpec() });
    reelDirty = false;
    document.getElementById('reel-panel').classList.remove('dirty');
    await loadClips();
    toast('Reel settings saved');
  } catch (e) { toast(e.message, true); }
}
window.saveReel = saveReel;

async function applyReelToAll() {
  if (!confirm('Apply these crop settings to every approved clip?')) return;
  try {
    const r = await api(`/api/workspaces/${SLUG}/reel/apply`, 'POST',
                        { spec: currentSpec(), scope: 'approved' });
    reelDirty = false;
    document.getElementById('reel-panel').classList.remove('dirty');
    await loadClips();
    toast(`Applied to ${r.updated} clip(s)`);
  } catch (e) { toast(e.message, true); }
}
window.applyReelToAll = applyReelToAll;

async function renderReel(all) {
  const body = { kind: 'reel' };
  if (!all) {
    if (!selected) { toast('Select a clip first', true); return; }
    body.clip_ids = [selected.id];
  }
  try {
    if (reelDirty && selected && !all) await saveReel();
    await api(`/api/workspaces/${SLUG}/jobs`, 'POST', body);
    toast(all ? 'Rendering all approved reels' : 'Rendering reel — takes ~1 min');
  } catch (e) { toast(e.message, true); }
}
window.renderReel = renderReel;

function copyFfmpeg() {
  if (!reelPlan) return;
  navigator.clipboard.writeText(reelPlan.filter_complex)
    .then(() => toast('filter_complex copied'))
    .catch(() => toast('Could not copy', true));
}
window.copyFfmpeg = copyFfmpeg;

/* ---------- lifecycle ---------- */

function loadReelForClip(clip) {
  reelSpec = clip && clip.reel ? JSON.parse(JSON.stringify(clip.reel)) : defaultSpec();
  if (reelSpec.src && reelSpec.src.game) reelSpec._gameDragged = true;
  reelDirty = false;
  const panel = document.getElementById('reel-panel');
  if (panel) panel.classList.remove('dirty');
  renderReelControls();
  renderChatControls();
  renderCaptionsControls();
  renderSpeakersControls();
  updateChatWindowCount();
  if (window.fxOnClipChange) window.fxOnClipChange();
  replan(true);
  renderReelOutput(clip);
}

function renderReelOutput(clip) {
  const el = document.getElementById('reel-output');
  if (!el) return;
  const out = clip && clip.reel_output;
  if (!out || !out.file) {
    el.innerHTML = '<span class="meta">No reel rendered yet.</span>';
    return;
  }
  const name = out.file.split('/').pop();
  el.innerHTML =
    `<video src="/media/${SLUG}/reel/${encodeURIComponent(name)}" controls preload="none"
            style="width:120px;border-radius:6px;background:#000"></video>
     <div class="meta" style="margin-left:8px">
       ${out.width}×${out.height} · ${(out.bytes / 1048576).toFixed(1)} MB · ${out.preset}
       <br><a href="/media/${SLUG}/reel/${encodeURIComponent(name)}?download=1">download</a>
     </div>`;
}

function initReel() {
  reelSpec = defaultSpec();
  renderReelControls();
  loadChatSyncPanel();
  loadChatMessagesCache();
  const v = document.getElementById('player');
  if (v) {
    v.addEventListener('loadedmetadata', () => { replan(true); });
    // Keep the preview live while scrubbing/playing, but cheaply.
    setInterval(() => {
      if (!document.getElementById('reel-panel')) return;
      if (document.getElementById('reel-panel').hidden) return;
      drawReelPreview();
    }, 250);
  }
  window.addEventListener('resize', drawCropRects);
  const stage = document.getElementById('stage');
  if (stage && window.ResizeObserver) {
    new ResizeObserver(() => drawCropRects()).observe(stage);
  }
  replan(true);
}

window.initReel = initReel;
window.loadReelForClip = loadReelForClip;
window.drawCropRects = drawCropRects;

// The effects panel edits the same spec object this file owns, so saving,
// "apply to all" and the unsaved badge all keep working with no changes here.
window.reelFx = {
  spec: currentSpec,
  dirty: markReelDirty,
  replan: replan,
  plan: () => reelPlan,
};
