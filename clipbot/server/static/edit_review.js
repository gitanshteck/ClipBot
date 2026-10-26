/* The edit review page: watch a render, comment at a time, see what changed between versions.
 * Talks to /api/edits/<name>/... (comments live in work/_edits/<name>/review.json, the same file `clipbot edit review`
 * reads and writes). Uses esc/api/toast from app.js. */

(function () {
  const NAME = window.EDIT_NAME;
  const enc = encodeURIComponent(NAME);
  const $ = id => document.getElementById(id);
  const video = $('er-video');

  let data = null;          // the last /review payload
  let which = '';           // the render being watched: preview | final | v<N>
  let tab = 'comments';
  let filter = 'open';      // comments tab: open | all
  let selected = null;      // a comment id
  let pendingSeek = null;   // seek to this once the next render has loaded
  let loadedStamp = '';

  /* ---------- formatting ---------- */

  function fmtT(t) {
    t = Math.max(0, t || 0);
    if (t >= 3600) return `${Math.floor(t / 3600)}:${String(Math.floor(t % 3600 / 60)).padStart(2, '0')}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
    return `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, '0')}`;
  }
  const dur = () => (video.duration && isFinite(video.duration)) ? video.duration : (data && data.duration) || 0;
  const verLabel = v => v.which === 'preview' ? 'preview (quick look)' : v.which === 'final' ? `v${v.n} — final (live)` : `v${v.n || v.which.slice(1)}`;

  /* ---------- loading ---------- */

  async function load(w) {
    try {
      data = await api(`/api/edits/${enc}/review?which=${encodeURIComponent(w || '')}`);
    } catch (err) { toast(err.message, true); return; }
    const changed = data.which !== which;
    which = data.which;
    renderHead();
    if (changed || !video.src) setVideo();
    renderAll();
  }

  async function refresh() {            // comments written by the CLI or by Claude while the page is open
    if (document.hidden || !data) return;
    try {
      const d = await api(`/api/edits/${enc}/review?which=${encodeURIComponent(which)}`);
      data.comments = d.comments; data.taste = d.taste; data.versions = d.versions;
      renderComments(); renderMarks(); renderCounts();
      if (tab === 'taste' || tab === 'versions') renderPanel();
    } catch (err) { /* the next poll will do */ }
  }

  function setVideo() {
    const v = data.versions.find(x => x.which === which);
    const stamp = which + ':' + (v ? v.rendered_at : '');
    if (stamp === loadedStamp) return;
    loadedStamp = stamp;
    video.src = `/media/edit/${enc}/${which}?v=${encodeURIComponent(v ? v.rendered_at : '')}`;
    video.load();
  }

  /* ---------- header ---------- */

  function renderHead() {
    const sel = $('er-which');
    const vs = data.versions.slice().sort((a, b) => {
      const rank = x => x.which === 'preview' ? 0 : x.which === 'final' ? 1 : 2;
      return rank(a) - rank(b) || (b.n || 0) - (a.n || 0);
    });
    sel.innerHTML = vs.map(v => `<option value="${esc(v.which)}"${v.which === which ? ' selected' : ''}>${esc(verLabel(v))}</option>`).join('');
    const cur = data.versions.find(v => v.which === which);
    const known = cur && (cur.duration || (video.duration && isFinite(video.duration) ? video.duration : 0));
    $('er-meta').textContent = cur ? `${known ? fmtT(known) : '?'} · ${(cur.bytes / 1048576).toFixed(0)} MB · rendered ${cur.rendered_at.replace('T', ' ').slice(0, 16)}` : 'no render yet';
    $('er-verify').innerHTML = data.verify.length
      ? data.verify.map(c => `<span class="badge ${c.status === 'fail' ? 'fail' : 'warn'}" title="${esc(c.detail)}">${esc(c.name)}: ${esc(c.status)}</span>`).join('')
      : (which === 'preview' || which === 'final' ? '<span class="meta">verify: nothing flagged (or not run)</span>' : '');
  }

  /* ---------- the timeline ---------- */

  function renderTicks() {
    const d = dur();
    $('er-ticks').innerHTML = !d ? '' : data.chapters.map(c => `<div class="tick" style="left:${(100 * c.t / d).toFixed(3)}%" data-title="${esc(c.title)}"></div>`).join('');
  }

  function renderMarks() {
    const d = dur();
    $('er-marks').innerHTML = !d ? '' : data.comments.filter(c => c.on === which).map(c =>
      `<div class="mark k-${esc(c.kind)} ${esc(c.status)}${c.id === selected ? ' sel' : ''}" style="left:${(100 * c.t / d).toFixed(3)}%" data-id="${esc(c.id)}" title="${esc(fmtT(c.t) + '  ' + c.text)}"></div>`).join('');
  }

  function movePlayhead() {
    const d = dur();
    $('er-playhead').style.left = d ? (100 * video.currentTime / d).toFixed(3) + '%' : '0';
    $('er-now').textContent = $('er-at').textContent = fmtT(video.currentTime);
  }

  /* ---------- what is on screen now ---------- */

  function clipAt(t) {
    let hit = null;
    for (const c of data.clips) if (c.start - 1e-6 <= t && t < c.start + c.dur - 1e-6) hit = c;
    return hit || (data.clips.length && t >= data.clips[data.clips.length - 1].start ? data.clips[data.clips.length - 1] : null);
  }

  function renderNow() {
    if (!data) return;
    const t = video.currentTime, c = clipAt(t);
    if (!c) { $('er-nowclip').innerHTML = which.startsWith('v') ? 'This old version kept no timeline, so clips are not shown.' : ''; return; }
    let ch = '';
    for (const x of data.chapters) if (x.t <= t + 1e-6) ch = x.title;
    $('er-nowclip').innerHTML = `<b>${esc(c.id)}</b> · ${esc(c.label || '')} · +${(t - c.start).toFixed(1)}s of ${c.dur.toFixed(1)}s`
      + (c.src ? ` · source ${esc(c.src)} ${Math.round(c.in)}–${Math.round(c.out)}s` : '') + (ch ? ` · chapter “${esc(ch)}”` : '')
      + (c.captions ? `<span class="cap">“${esc(c.captions.slice(0, 160))}”</span>` : '');
    document.querySelectorAll('.er-clip').forEach(el => el.classList.toggle('now', el.dataset.id === c.id));
  }

  /* ---------- the side panel ---------- */

  function renderCounts() {
    const open = data.comments.filter(c => c.status === 'open').length;
    $('er-n-comments').textContent = open;
  }

  function commentHtml(c) {
    const where = c.clip ? `${esc(c.clip)}${c.offset != null ? ' +' + c.offset.toFixed(1) + 's' : ''}${c.chapter ? ' · ' + esc(c.chapter) : ''}` : '';
    const done = c.status !== 'open' ? `<div class="done-note">${c.status === 'wontfix' ? 'closed without a change' : 'done'}${c.done && c.done.in ? ' in ' + esc(c.done.in) : ''}${c.done && c.done.how ? ': ' + esc(c.done.how) : ''}</div>` : '';
    const acts = c.status === 'open'
      ? `<button data-act="done" data-id="${esc(c.id)}">✓ done</button>`
        + (c.kind === 'taste' || c.kind === 'change' ? `<button data-act="taste" data-id="${esc(c.id)}" title="Keep this as a standing rule for every edit">→ taste rule</button>` : '')
      : `<button data-act="reopen" data-id="${esc(c.id)}">↺ reopen</button>`;
    return `<div class="er-item k-${esc(c.kind)} ${esc(c.status)}${c.id === selected ? ' sel' : ''}" data-id="${esc(c.id)}">
      <div class="top"><span class="t" data-seek-to="${c.t}" data-on="${esc(c.on)}">${fmtT(c.t)}</span><span class="badge">${esc(c.kind)}</span>
        <span class="where">${esc(c.id)} · on ${esc(c.on)}${where ? ' · ' + where : ''}</span></div>
      <div class="txt">${esc(c.text)}</div>
      ${c.reply ? `<div class="reply">${esc(c.reply)}</div>` : ''}${done}
      <div class="acts">${acts}<button data-act="edit" data-id="${esc(c.id)}">✎ edit</button><button data-act="delete" data-id="${esc(c.id)}">🗑</button></div>
    </div>`;
  }

  function renderComments() {
    if (tab !== 'comments') return;
    let list = data.comments.slice();
    if (filter === 'open') list = list.filter(c => c.status === 'open');
    list.sort((a, b) => (a.on === which ? 0 : 1) - (b.on === which ? 0 : 1) || a.t - b.t);
    $('er-panel').innerHTML = `<div class="er-filter"><button data-filter="open" class="${filter === 'open' ? 'on' : ''}">Open</button>`
      + `<button data-filter="all" class="${filter === 'all' ? 'on' : ''}">All (${data.comments.length})</button></div>`
      + (list.length ? list.map(commentHtml).join('') : '<div class="empty">No comments yet. Pause where something should change and press C.</div>');
  }

  function renderPanel() {
    renderCounts();
    document.querySelectorAll('#er-tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
    if (tab === 'comments') return renderComments();
    const p = $('er-panel');
    if (tab === 'chapters') {
      p.innerHTML = data.chapters.length ? data.chapters.map(c => `<div class="er-item er-clip" data-seek-to="${c.t}"><span class="t">${fmtT(c.t)}</span> ${esc(c.title)}</div>`).join('')
        : '<div class="empty">No chapters in this render.</div>';
    } else if (tab === 'clips') {
      p.innerHTML = data.clips.length ? data.clips.map(c => `<div class="er-item er-clip" data-id="${esc(c.id)}" data-seek-to="${c.start}">
        <div class="top"><span class="t">${fmtT(c.start)}</span><b>${esc(c.id)}</b><span class="where">${c.dur.toFixed(1)}s${c.src ? ' · ' + esc(c.src) + ' ' + Math.round(c.in) + '–' + Math.round(c.out) : ''}${c.markers ? ' · ' + c.markers + ' marker' + (c.markers > 1 ? 's' : '') : ''}</span></div>
        <div class="txt">${esc(c.label || '')}</div>${c.captions ? `<div class="reply">“${esc(c.captions.slice(0, 140))}”</div>` : ''}</div>`).join('')
        : '<div class="empty">This version kept no timeline, so there is no clip list.</div>';
    } else if (tab === 'versions') {
      const vs = data.versions.slice().sort((a, b) => (b.n || 0) - (a.n || 0));
      p.innerHTML = vs.map(v => `<div class="er-item er-ver${v.which === which ? ' cur' : ''}"><div class="top"><b>${esc(verLabel(v))}</b>
        <span class="where">${esc(v.rendered_at.replace('T', ' ').slice(0, 16))} · ${v.duration ? fmtT(v.duration) : '?'}</span>
        <button data-watch="${esc(v.which)}" style="margin-left:auto;font-size:11px;padding:2px 8px">watch</button></div>
        <div class="txt">${esc(v.summary || '')}</div>
        ${v.changes && v.changes.length ? `<ul class="changes">${v.changes.map(c => `<li>${esc(c)}</li>`).join('')}</ul>` : ''}</div>`).join('') || '<div class="empty">No renders yet.</div>';
    } else if (tab === 'taste') {
      p.innerHTML = data.taste.length ? data.taste.map(r => `<div class="er-item k-taste"><span class="where">${esc(r.id)}</span><div class="txt">${esc(r.text)}</div></div>`).join('')
        + '<p class="hint">Standing preferences, applied to every edit by whoever writes the spec. Retire one with <code>clipbot edit taste retire ID</code>.</p>'
        : '<div class="empty">No taste rules yet. Mark a comment “taste” and press “→ taste rule”.</div>';
    }
    renderNow();
  }

  function renderAll() {
    renderTicks(); renderMarks(); renderPanel(); movePlayhead(); renderNow();
  }

  /* ---------- actions ---------- */

  function seek(t, on) {
    if (on && on !== which && data.versions.some(v => v.which === on)) {
      pendingSeek = t;
      load(on);
      return;
    }
    video.currentTime = Math.min(Math.max(0, t), dur() || t);
  }

  async function addComment(ev) {
    ev.preventDefault();
    const text = $('er-text').value.trim();
    if (!text) { toast('Write the comment first', true); return; }
    try {
      const c = await api(`/api/edits/${enc}/comments`, 'POST', { t: Math.round(video.currentTime * 100) / 100, text, kind: $('er-kind').value, on: which });
      data.comments.push(c);
      $('er-text').value = '';
      selected = c.id;
      renderMarks(); renderPanel();
      toast('Comment added at ' + fmtT(c.t));
    } catch (err) { toast(err.message, true); }
  }

  async function patch(id, body, msg) {
    try {
      const c = await api(`/api/edits/${enc}/comments/${encodeURIComponent(id)}`, 'PATCH', body);
      const i = data.comments.findIndex(x => x.id === id);
      if (i >= 0) data.comments[i] = c;
      renderMarks(); renderPanel();
      if (msg) toast(msg);
    } catch (err) { toast(err.message, true); }
  }

  async function onPanelClick(ev) {
    const t = ev.target.closest('[data-seek-to],[data-act],[data-filter],[data-watch]');
    if (!t) { const item = ev.target.closest('.er-item[data-id]'); if (item && tab === 'comments') { selected = item.dataset.id; renderMarks(); } return; }
    if (t.dataset.filter) { filter = t.dataset.filter; renderComments(); return; }
    if (t.dataset.watch) { load(t.dataset.watch); return; }
    if (t.dataset.act) {
      const id = t.dataset.id, act = t.dataset.act;
      if (act === 'done') {
        const how = window.prompt('What was changed? (optional — it is kept with the comment)', '');
        if (how === null) return;
        await patch(id, { status: 'done', how, in: which }, 'Marked done');
      } else if (act === 'reopen') await patch(id, { status: 'open' }, 'Reopened');
      else if (act === 'edit') {
        const cur = data.comments.find(c => c.id === id);
        const text = window.prompt('Edit the comment', cur ? cur.text : '');
        if (text !== null && text.trim()) await patch(id, { text }, 'Saved');
      } else if (act === 'delete') {
        if (!window.confirm('Delete this comment?')) return;
        try { await api(`/api/edits/${enc}/comments/${encodeURIComponent(id)}`, 'DELETE'); data.comments = data.comments.filter(c => c.id !== id); renderMarks(); renderPanel(); } catch (err) { toast(err.message, true); }
      } else if (act === 'taste') {
        const cur = data.comments.find(c => c.id === id);
        const text = window.prompt('The standing rule (applies to every edit):', cur ? cur.text : '');
        if (text === null || !text.trim()) return;
        try {
          const r = await api(`/api/edits/${enc}/comments/${encodeURIComponent(id)}/taste`, 'POST', { text });
          data.comments[data.comments.findIndex(c => c.id === id)] = r.comment;
          await refresh(); toast('Saved as taste rule ' + r.rule.id);
        } catch (err) { toast(err.message, true); }
      }
      return;
    }
    if (t.dataset.seekTo !== undefined) seek(parseFloat(t.dataset.seekTo), t.dataset.on);
  }

  /* ---------- wiring ---------- */

  $('er-form').addEventListener('submit', addComment);
  $('er-text').addEventListener('keydown', ev => { if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) $('er-form').requestSubmit(); });
  $('er-panel').addEventListener('click', onPanelClick);
  $('er-tabs').addEventListener('click', ev => { const b = ev.target.closest('button[data-tab]'); if (b) { tab = b.dataset.tab; renderPanel(); } });
  $('er-which').addEventListener('change', ev => { pendingSeek = null; load(ev.target.value); });
  $('er-rate').addEventListener('change', ev => { video.playbackRate = parseFloat(ev.target.value); });
  $('er-play').addEventListener('click', () => (video.paused ? video.play() : video.pause()));
  document.querySelectorAll('[data-seek]').forEach(b => b.addEventListener('click', () => seek(video.currentTime + parseFloat(b.dataset.seek))));
  $('er-tl').addEventListener('click', ev => {
    const m = ev.target.closest('.mark');
    if (m) { selected = m.dataset.id; tab = 'comments'; const c = data.comments.find(x => x.id === selected); if (c) video.currentTime = c.t; renderPanel(); renderMarks(); return; }
    const r = $('er-tl').getBoundingClientRect();
    video.currentTime = dur() * Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
  });

  video.addEventListener('loadedmetadata', () => {
    $('er-dur').textContent = fmtT(dur());
    if (data) renderHead();                       // an old version kept no timeline: the file knows its own length
    video.playbackRate = parseFloat($('er-rate').value);
    if (pendingSeek != null) { video.currentTime = Math.min(pendingSeek, dur()); pendingSeek = null; }
    renderTicks(); renderMarks(); movePlayhead();
  });
  video.addEventListener('timeupdate', () => { movePlayhead(); renderNow(); });
  video.addEventListener('play', () => { $('er-play').textContent = '⏸'; });
  video.addEventListener('pause', () => { $('er-play').textContent = '▶'; });
  video.addEventListener('error', () => toast('This render could not be played', true));

  document.addEventListener('keydown', ev => {
    if (/^(TEXTAREA|INPUT|SELECT)$/.test(ev.target.tagName) || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (ev.key === ' ') { ev.preventDefault(); video.paused ? video.play() : video.pause(); }
    else if (ev.key === 'ArrowLeft') { ev.preventDefault(); seek(video.currentTime - (ev.shiftKey ? 1 : 5)); }
    else if (ev.key === 'ArrowRight') { ev.preventDefault(); seek(video.currentTime + (ev.shiftKey ? 1 : 5)); }
    else if (ev.key === 'c' || ev.key === 'C') { ev.preventDefault(); video.pause(); $('er-text').focus(); }
  });

  setInterval(refresh, 8000);
  document.addEventListener('visibilitychange', refresh);
  load('');
})();
