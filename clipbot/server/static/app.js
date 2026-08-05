/* Shared shell: API helper, SSE client, job panel. */

const _handlers = {};

function onEvent(kind, fn) {
  (_handlers[kind] = _handlers[kind] || []).push(fn);
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtDur(sec) {
  if (sec == null) return '—';
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
           : `${m}:${String(s).padStart(2, '0')}`;
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
    return `<div class="job ${j.status}">
      <div><b>${j.kind}</b> <span class="meta">${j.slug}</span></div>
      <div class="meta">${j.status}${j.phase ? ' · ' + j.phase : ''}${pct != null ? ' · ' + pct + '%' : ''}${eta}</div>
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

  ['job', 'progress', 'log', 'workspace', 'clips', 'segment', 'preview', 'library'].forEach(k =>
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
         <td class="meta">${esc(c.detail)}</td></tr>`).join('') + '</table>';
    } catch (e) { body.textContent = e.message; }
  });
}

api('/api/jobs').then(({ jobs: list }) => {
  list.forEach(j => jobs.set(j.id, j));
  renderJobs();
}).catch(() => {});

connect();
