/* Shared shell: API helper, SSE client, job panel. */

const _handlers = {};

function onEvent(kind, fn) {
  (_handlers[kind] = _handlers[kind] || []).push(fn);
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* --- download quality picker ---
 * Shared by workspace.html (per-stage download/re-download/run-all) and
 * library.html (new VOD form + channel browser) so the preset list only
 * lives in one place. Keys/order must match clipbot/stages/download.py's
 * QUALITY_CHOICES; there's no route exposing that list, so it's mirrored
 * here by hand - it's a short, rarely-changed constant, not worth a round
 * trip for. Empty value means "omit `quality`", i.e. fall back to
 * config/settings.json's `download.format`.
 */
function qualityPickerHtml(id) {
  return `<select id="${id}" title="Download quality">
    <option value="">Default (≤720p)</option>
    <option value="best">Best available</option>
    <option value="1080">1080p</option>
    <option value="720">720p</option>
    <option value="480">480p</option>
    <option value="360">360p</option>
  </select>`;
}

function fmtDur(sec) {
  if (sec == null) return '—';
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
           : `${m}:${String(s).padStart(2, '0')}`;
}

/* --- library cards ---
 * Shared by the Kick and YouTube library pages (library.html,
 * library_youtube.html) so a workspace looks the same on either tab. `w` is
 * one entry of GET /api/workspaces. */

function stageDots(stages) {
  return stages.map(s => `<span class="dot ${s.done ? 'done' : (s.ready ? 'ready' : 'blocked')}" title="${esc(s.label)}${s.blocked ? ' — ' + esc(s.blocked) : ''}"></span>`).join('');
}

function workspaceCardHtml(w) {
  // A YouTube workspace normally has no video on disk: the dashboard plays the
  // embedded YouTube player instead, so say so rather than showing a blank size.
  const embed = w.source_mode === 'embed';
  return `
    <article class="card">
      <h2><a href="/w/${w.slug}">${esc(w.title)}</a></h2>
      <div class="meta">
        ${w.duration ? fmtDur(w.duration) : '—'} ·
        ${w.disk_human}
        ${w.video_height ? ` · ${w.video_width}×${w.video_height}` : ''}
        ${embed ? '<span class="badge" title="No video on disk — review plays it from YouTube">audio only</span>' : ''}
        ${w.low_res ? '<span class="badge warn" title="Too low to review clips visually">low-res</span>' : ''}
        ${w.video_deleted ? '<span class="badge">VOD deleted</span>' : ''}
      </div>
      <div class="dots">${stageDots(w.stages)}</div>
      ${w.counts ? `<div class="meta">${w.counts.total} candidates · ${w.counts.approved} approved · ${w.counts.cut} cut</div>` : ''}
      ${w.active_job ? `<div class="meta running">▶ ${w.active_job.kind} ${w.active_job.status}</div>` : ''}
      <div class="row">
        <a class="btn" href="/w/${w.slug}">Open</a>
        ${w.counts && w.counts.total ? `<a class="btn" href="/w/${w.slug}/review">Review clips</a>` : ''}
      </div>
    </article>`;
}

/* --- byte/rate formatting --- (job panel: download speed + remaining size) */
function fmtBytes(n) {
  if (n == null) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = Math.max(0, n);
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtRate(bytesPerSec) {
  if (bytesPerSec == null) return null;
  return `${fmtBytes(bytesPerSec)}/s`;
}

/* --- time helpers ---
 * Promoted out of review.js so both review.js and compile.js get them from
 * this shared load instead of duplicating them - purely a move, behavior
 * unchanged.
 */

function formatClock(total, withTenths) {
  if (total == null || isNaN(total)) return '';
  total = Math.max(0, total);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total - h * 3600) / 60);
  const s = total - h * 3600 - m * 60;
  const ss = withTenths === false
    ? String(Math.floor(s)).padStart(2, '0')
    : s.toFixed(1).padStart(4, '0');
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

function parseClock(str) {
  if (str == null) return NaN;
  str = String(str).trim();
  if (str === '') return NaN;
  if (/^-?\d+(\.\d+)?$/.test(str)) return parseFloat(str);
  const parts = str.split(':').map(p => p.trim());
  if (parts.length < 2 || parts.length > 3) return NaN;
  if (parts.some(p => p === '' || isNaN(parseFloat(p)))) return NaN;
  const n = parts.map(parseFloat);
  return parts.length === 3 ? n[0] * 3600 + n[1] * 60 + n[2] : n[0] * 60 + n[1];
}

/* --- captions (Hinglish transliteration) ---
 * Shared by review.js and compile.js, both of which show a transcript panel
 * and want an optional Devanagari/Hinglish toggle on it. captions.json's
 * segments share transcript.json's segment ids exactly (see
 * stages/transliterate.py), so this just needs to build an id -> text
 * lookup - each page keeps its own toggle state and re-render logic.
 * Resolves to null (not present) rather than throwing, so a workspace with
 * no Hinglish pass, or an older server build that predates this route,
 * just leaves the toggle hidden instead of breaking the page.
 */
async function loadCaptionsIndex(slug) {
  try {
    const data = await api(`/api/workspaces/${slug}/captions`);
    if (!data.present) return null;
    const index = new Map();
    for (const s of data.segments || []) index.set(s.id, s.text);
    return index;
  } catch (e) {
    return null;
  }
}

/* --- toast --- (any page that calls this needs a <div class="toast" id="toast"></div>) */

function toast(msg, bad) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.className = 'toast show' + (bad ? ' bad' : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast'; }, 2200);
}

async function api(path, method = 'GET', body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['content-type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  // Mutating requests carry this header; a cross-origin page can't set it
  // without a preflight, and we send no CORS headers.
  if (method !== 'GET') opts.headers['x-clipbot'] = '1';
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

/* --- job panel --- */

const jobs = new Map();

function renderJobs() {
  const el = document.getElementById('joblist');
  if (!el) return;
  const list = [...jobs.values()]
    .sort((a, b) => (b.created_at || 0) - (a.created_at || 0))
    .slice(0, 12);
  if (!list.length) { el.innerHTML = '<p class="meta">Nothing running.</p>'; return; }
  el.innerHTML = list.map(j => {
    const pct = j.fraction != null ? Math.round(j.fraction * 100) : null;
    const eta = j.eta_seconds ? ` · ETA ${fmtDur(j.eta_seconds)}` : '';
    // Byte-unit jobs (currently just downloads) additionally show speed and
    // bytes-done/total - other stages report progress in units like
    // "segments" and don't have a meaningful transfer rate to show.
    const bytesLine = j.unit === 'bytes'
      ? `<div class="meta">${fmtBytes(j.current)} / ${fmtBytes(j.total)}${j.rate ? ' · ' + fmtRate(j.rate) : ''}</div>`
      : '';
    return `<div class="job ${j.status}">
      <div><b>${j.kind}</b> <span class="meta">${j.slug}</span></div>
      <div class="meta">${j.status}${j.phase ? ' · ' + j.phase : ''}${pct != null ? ' · ' + pct + '%' : ''}${eta}</div>
      ${bytesLine}
      ${j.label ? `<div class="meta">${esc(j.label)}</div>` : ''}
      ${j.error ? `<div class="meta" style="color:var(--bad)">${esc(j.error)}</div>` : ''}
      ${pct != null && j.status === 'running' ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ''}
      ${(j.status === 'running' || j.status === 'queued')
        ? `<div class="row" style="margin-top:6px"><button onclick="cancelJob('${j.id}')">Cancel</button></div>` : ''}
    </div>`;
  }).join('');
}

async function cancelJob(id) {
  try { await api(`/api/jobs/${id}/cancel`, 'POST'); } catch (e) { alert(e.message); }
}
window.cancelJob = cancelJob;

function appendLog(p) {
  const time = new Date((p.time || Date.now() / 1000) * 1000).toLocaleTimeString();
  document.querySelectorAll('.logtail').forEach(el => {
    const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent += `${time} ${p.message}\n`;
    const lines = el.textContent.split('\n');
    if (lines.length > 400) el.textContent = lines.slice(-400).join('\n');
    if (stick) el.scrollTop = el.scrollHeight;
  });
}

/* --- SSE --- */

function connect() {
  const src = new EventSource('/api/events');
  const conn = document.getElementById('conn');

  src.onopen = () => conn && conn.classList.add('live');
  src.onerror = () => conn && conn.classList.remove('live');

  const dispatch = (kind) => (e) => {
    let payload;
    try { payload = JSON.parse(e.data); } catch (err) { return; }
    if (kind === 'job') { jobs.set(payload.id, payload); renderJobs(); }
    if (kind === 'progress') {
      const j = jobs.get(payload.job_id);
      if (j) { Object.assign(j, payload); renderJobs(); }
    }
    if (kind === 'log') appendLog(payload);
    (_handlers[kind] || []).forEach(fn => fn(payload));
  };

  ['job', 'progress', 'log', 'workspace', 'clips', 'compilations', 'segment', 'preview', 'library'].forEach(k =>
    src.addEventListener(k, dispatch(k)));
}

/* --- doctor --- */

const doctorBtn = document.getElementById('doctor-btn');
if (doctorBtn) {
  doctorBtn.addEventListener('click', async () => {
    const dlg = document.getElementById('doctor-dialog');
    const body = document.getElementById('doctor-body');
    dlg.showModal();
    body.textContent = 'Checking…';
    try {
      const { checks } = await api('/api/doctor');
      body.innerHTML = '<table class="doctor">' + checks.map(c =>
        `<tr><td>${c.ok ? '✅' : '❌'}</td><td><b>${esc(c.name)}</b></td>
         <td class="meta">${esc(c.detail)}</td></tr>`).join('') + '</table>' +
        // The YouTube checks above are offline (version, JS runtime). Only this
        // proves extraction actually works, and it asks YouTube, so it's on request.
        '<div class="row" style="margin-top:10px">' +
        '<button id="doctor-yt-test" title="Asks YouTube for a public video\'s format list">Test YouTube</button>' +
        '<span class="meta" id="doctor-yt-result"></span></div>';
      document.getElementById('doctor-yt-test').addEventListener('click', testYoutube);
    } catch (e) { body.textContent = e.message; }
  });
}

async function testYoutube() {
  const btn = document.getElementById('doctor-yt-test');
  const out = document.getElementById('doctor-yt-result');
  btn.disabled = true;
  out.textContent = 'Asking YouTube… (a few seconds)';
  try {
    const r = await api('/api/doctor/youtube', 'POST', {});
    out.innerHTML = `${r.ok ? '✅' : '❌'} ${esc(r.detail)}`;
  } catch (e) {
    out.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

api('/api/jobs').then(({ jobs: list }) => {
  list.forEach(j => jobs.set(j.id, j));
  renderJobs();
}).catch(() => {});

connect();
