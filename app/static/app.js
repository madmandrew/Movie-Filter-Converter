'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail ?? detail; } catch { /* non-JSON error */ }
    throw new Error(detail);
  }
  return r.json();
}
const postJSON = (url, body) => api(url, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

const tc = (s) => {
  if (s == null) return '';
  const m = Math.floor(s / 60), sec = (s % 60);
  return `${String(m).padStart(2, '0')}:${sec.toFixed(2).padStart(5, '0')}`;
};

/* ------------------------------------------------------------------- tabs */
$$('.tab').forEach((b) => b.addEventListener('click', () => {
  $$('.tab').forEach((x) => x.classList.toggle('active', x === b));
  $$('.view').forEach((v) => v.classList.add('hidden'));
  $(`#view-${b.dataset.view}`).classList.remove('hidden');
  if (b.dataset.view === 'runs') loadRuns();
  if (b.dataset.view === 'words') loadWords();
}));

/* ---------------------------------------------------------------- library */
let libTimer;
async function loadLibrary() {
  const q = $('#q').value.trim();
  const lib = $('#lib').value;
  const status = $('#status').value;
  const d = await api(`/api/library?q=${encodeURIComponent(q)}&lib=${lib}&status=${status}`);

  if ($('#lib').options.length <= 1) {
    for (const k of Object.keys(d.roots)) {
      $('#lib').insertAdjacentHTML('beforeend', `<option value="${esc(k)}">${esc(k)}</option>`);
    }
  }
  const s = d.stats;
  $('#libstats').textContent =
    `${s.total} titles · ${Object.entries(s.by_status).map(([k, v]) => `${v} ${k}`).join(' · ')}`;

  $('#titles tbody').innerHTML = d.items.map((t) => {
    const cls = { filtered: 'ok', failed: 'bad' }[t.status] || '';
    const audio = t.audio_codec
      ? `${esc(t.audio_codec)}${t.channels ? ` ${t.channels}ch` : ''}`
      : '<span class="muted">—</span>';
    return `<tr>
      <td class="name" title="${esc(t.path)}">${esc(t.name)}</td>
      <td class="muted">${esc(t.library)}</td>
      <td class="num">${t.size_gb} GB</td>
      <td>${audio}</td>
      <td><span class="pill ${cls}">${esc(t.status)}</span></td>
      <td>${t.has_tagset ? '<span class="pill ok">yes</span>' : '<span class="muted">—</span>'}</td>
      <td><button data-path="${esc(t.path)}" class="filterbtn secondary">Filter…</button></td>
    </tr>`;
  }).join('') || '<tr><td colspan="7" class="muted">No titles. Try “Rescan library”.</td></tr>';

  $$('.filterbtn').forEach((b) =>
    b.addEventListener('click', () => openFilter(b.dataset.path)));
}

$('#q').addEventListener('input', () => {
  clearTimeout(libTimer);
  libTimer = setTimeout(loadLibrary, 250);
});
$('#lib').addEventListener('change', loadLibrary);
$('#status').addEventListener('change', loadLibrary);
$('#rescan').addEventListener('click', async (e) => {
  e.target.disabled = true;
  e.target.textContent = 'Scanning…';
  try {
    const r = await api('/api/library/scan', { method: 'POST' });
    $('#libstats').textContent = Object.entries(r)
      .filter(([k]) => !k.startsWith('_')).map(([k, v]) => `${k}: ${v}`).join(' · ');
    await loadLibrary();
  } catch (err) {
    alert(`Scan failed: ${err.message}`);
  } finally {
    e.target.disabled = false;
    e.target.textContent = 'Rescan library';
  }
});

/* ------------------------------------------------------------ filter modal */
const modal = $('#modal');
$('#modalclose').addEventListener('click', () => modal.classList.add('hidden'));
modal.addEventListener('click', (e) => { if (e.target === modal) modal.classList.add('hidden'); });

async function openFilter(path) {
  modal.classList.remove('hidden');
  $('#mtitle').textContent = path.split(/[\\/]/).pop();
  $('#mbody').innerHTML = '<p class="muted">Probing…</p>';

  const t = await api(`/api/title?path=${encodeURIComponent(path)}`);
  const lossless = /hd ma|dts:x|truehd|atmos/i.test(t.audio_profile || '');

  let groupsHtml = '<p class="muted">No tag-set linked. The word-list scan still runs.</p>';
  let tsSelect = '<option value="">none</option>';
  if (t.tagsets?.length) {
    tsSelect += t.tagsets.map((x) =>
      `<option value="${x.tag_set_id}">#${x.tag_set_id} ${esc(x.title_hint || '')}</option>`).join('');
  }

  $('#mbody').innerHTML = `
    <div class="grid2">
      <fieldset><legend>Source</legend>
        <div class="muted">${esc(t.audio_codec || '?')} · ${esc(t.audio_profile || '?')}
          · ${t.channels || '?'}ch · ${t.duration ? tc(t.duration) : '?'}</div>
        ${lossless ? `<p class="pill warn" style="margin-top:.5rem">
          Lossless/object audio — splice is unavailable, FLAC will be used.
          Atmos/DTS:X object metadata cannot survive filtering.</p>` : ''}
      </fieldset>
      <fieldset><legend>Audio quality</legend>
        <label class="row"><input type="radio" name="q" value="splice" checked>
          <span>Splice <code>— only muted frames re-encoded, size-neutral</code></span></label>
        <label class="row"><input type="radio" name="q" value="same">
          <span>Same codec <code>— whole track, inaudible loss</code></span></label>
        <label class="row"><input type="radio" name="q" value="lossless">
          <span>Lossless FLAC <code>— bit-exact, ~1.8× audio size</code></span></label>
      </fieldset>
    </div>
    <fieldset><legend>VidAngel tag-set</legend>
      <div class="toolbar">
        <select id="mts">${tsSelect}</select>
        <span class="muted">Pick a cached tag-set to filter specific tagged incidents.</span>
      </div>
      <div id="mgroups">${groupsHtml}</div>
    </fieldset>
    <fieldset><legend>Word-list scan</legend>
      <label class="row"><input type="checkbox" id="mscan" checked>
        <span>Scan the whole track for word-list matches
          <code>— finds words VidAngel missed; hits await your review</code></span></label>
    </fieldset>
    <div class="toolbar">
      <button id="mgo">Queue filter run</button>
      <span id="mmsg" class="muted"></span>
    </div>`;

  $('#mts').addEventListener('change', async (e) => {
    const id = e.target.value;
    if (!id) { $('#mgroups').innerHTML = groupsHtml; return; }
    const ts = await api(`/api/tagsets/${id}`);
    $('#mgroups').innerHTML = ts.groups.map((g) => {
      const kind = g.kind === 'audiovisual' ? 'video' : 'audio';
      const n = g.incidents.length;
      const usable = g.locatable
        ? ''
        : ` <span class="pill warn">${kind === 'video'
            ? 'video cut' : 'no specific word — cannot word-mute'}</span>`;
      return `<label class="row">
        <input type="checkbox" class="catbox" data-key="${esc(g.key)}"
               data-kind="${kind}" data-locatable="${g.locatable}">
        <span>${esc(g.title)} <code>(${n} ${kind})</code>${usable}</span></label>`;
    }).join('');
  });

  $('#mgo').addEventListener('click', async (e) => {
    e.target.disabled = true;
    const audioCats = $$('.catbox').filter((c) => c.checked && c.dataset.locatable === 'true')
      .map((c) => c.dataset.key);
    const videoCats = $$('.catbox').filter((c) => c.checked && c.dataset.kind === 'video')
      .map((c) => c.dataset.key);
    try {
      const r = await postJSON('/api/runs', {
        path,
        tag_set_id: $('#mts').value ? Number($('#mts').value) : null,
        categories: audioCats,
        video_categories: videoCats,
        quality: $('input[name=q]:checked').value,
        do_scan: $('#mscan').checked,
      });
      $('#mmsg').textContent = `queued as run #${r.run_id}`;
      setTimeout(() => { modal.classList.add('hidden'); $$('.tab')[1].click(); }, 700);
    } catch (err) {
      $('#mmsg').textContent = `failed: ${err.message}`;
      e.target.disabled = false;
    }
  });
}

/* ------------------------------------------------------------------- runs */
async function loadRuns() {
  const d = await api('/api/runs');
  $('#runs tbody').innerHTML = d.runs.map((r) => {
    const cls = { done: 'ok', failed: 'bad', running: 'warn' }[r.status] || '';
    const pct = Math.round(r.progress || 0);
    return `<tr>
      <td class="num">${r.id}</td>
      <td class="name" title="${esc(r.path)}">${esc(r.path.split(/[\\/]/).pop())}</td>
      <td><span class="pill ${cls}">${esc(r.status)}</span></td>
      <td class="muted">${esc(r.stage || '')}</td>
      <td><div class="bar"><i style="width:${pct}%"></i></div></td>
      <td><button class="secondary rundet" data-id="${r.id}">Details</button></td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" class="muted">No runs yet.</td></tr>';

  $$('.rundet').forEach((b) => b.addEventListener('click', () => showRun(b.dataset.id)));
}

async function showRun(id) {
  const r = await api(`/api/runs/${id}`);
  const rep = r.report || {};
  const inc = rep.incidents || [];
  const pending = rep.scan?.pending_review || [];

  modal.classList.remove('hidden');
  $('#mtitle').textContent = `Run #${r.id} — ${r.status}`;
  $('#mbody').innerHTML = `
    ${r.error ? `<pre class="log">${esc(r.error)}</pre>` : ''}
    ${inc.length ? `<fieldset><legend>Incidents</legend>
      <table><thead><tr><th>Word</th><th>Bucket</th><th>Mute</th><th>Drift</th>
      <th>Status</th><th>Note</th></tr></thead><tbody>
      ${inc.map((i) => `<tr>
        <td>${esc(i.word)}</td>
        <td class="num muted">${i.bucket ?? '—'}</td>
        <td class="num">${i.start != null ? `${tc(i.start)}–${tc(i.end)}` : '—'}</td>
        <td class="num">${i.drift != null ? `${i.drift > 0 ? '+' : ''}${i.drift.toFixed(2)}s` : ''}</td>
        <td><span class="pill ${i.status.startsWith('OK') ? 'ok' : i.status === 'NOT_FOUND' ? 'bad' : 'warn'}">${esc(i.status)}</span></td>
        <td class="muted">${esc(i.note || '')}</td></tr>`).join('')}
      </tbody></table></fieldset>` : ''}
    ${rep.video_ranges?.length ? `<fieldset><legend>Video cuts</legend>
      ${rep.video_ranges.map((v) =>
        `<div>${tc(v.start)}–${tc(v.end)} <span class="muted">(${(v.end - v.start).toFixed(1)}s, ${esc(v.method)})</span></div>`).join('')}
      </fieldset>` : ''}
    ${pending.length ? `<fieldset><legend>Needs review (${pending.length})</legend>
      <p class="muted">Words found in the audio that no tag covered. Listen, then choose.</p>
      <div id="hits">${pending.map((h, k) => `
        <div class="hit" data-k="${k}">
          <div class="ctx">${tc(h.at)} — ${esc(h.context).replace(
            new RegExp(`\\b(${h.word})\\b`, 'i'), '<b>$1</b>')}</div>
          <div class="actions">
            <audio controls preload="none"
              src="/api/clip?path=${encodeURIComponent(r.path)}&start=${h.at}&end=${h.end}"></audio>
            <button class="dec" data-a="mute" data-t="${h.at}" data-w="${esc(h.word)}">Mute this</button>
            <button class="dec secondary" data-a="skip" data-t="${h.at}" data-w="${esc(h.word)}">Skip</button>
            <span class="muted">p=${h.confidence}</span>
          </div>
        </div>`).join('')}</div>
      <p class="muted">Decisions are remembered; re-run the filter to apply them.</p>
      </fieldset>` : ''}
    ${rep.render ? `<fieldset><legend>Render</legend>
      <div>${esc(rep.render.summary || '')}</div></fieldset>` : ''}
    <fieldset><legend>Log</legend><pre class="log">${esc(r.log || '')}</pre></fieldset>`;

  $$('.dec').forEach((b) => b.addEventListener('click', async () => {
    await postJSON('/api/review', {
      path: r.path, at_time: Number(b.dataset.t), word: b.dataset.w, action: b.dataset.a,
    });
    const hit = b.closest('.hit');
    hit.style.opacity = .4;
    $$('button', hit).forEach((x) => { x.disabled = true; });
    b.textContent = b.dataset.a === 'mute' ? 'will mute' : 'skipped';
  }));
}

/* -------------------------------------------------------------- word list */
async function loadWords() {
  const d = await api('/api/words');
  $('#wordlist').innerHTML = d.words.map((w) => `
    <span class="chip ${w.enabled ? '' : 'off'}">
      ${esc(w.word)} <span class="muted">${esc(w.category)}</span>
      <button data-w="${esc(w.word)}" title="Remove">&times;</button>
    </span>`).join('');
  $$('#wordlist button').forEach((b) => b.addEventListener('click', async () => {
    await api(`/api/words/${encodeURIComponent(b.dataset.w)}`, { method: 'DELETE' });
    loadWords();
  }));
}
$('#addword').addEventListener('click', async () => {
  const word = $('#newword').value.trim();
  if (!word) return;
  try {
    await postJSON('/api/words', { word, category: $('#newcat').value, enabled: true });
    $('#newword').value = '';
    loadWords();
  } catch (e) { alert(e.message); }
});

/* --------------------------------------------------------------- tag-sets */
$('#savetagset').addEventListener('click', async () => {
  const payload = $('#payload').value.trim();
  if (!payload) return;
  try {
    const r = await postJSON('/api/tagsets', { payload, title_hint: $('#hint').value.trim() || null });
    $('#tagsetmsg').innerHTML =
      `<span class="pill ok">saved #${r.tag_set_id}: ${r.incidents} incidents,
       ${r.enabled} pre-enabled, runtime ${r.runtime}s</span>`;
    $('#payload').value = '';
  } catch (e) {
    $('#tagsetmsg').innerHTML = `<span class="pill bad">${esc(e.message)}</span>`;
  }
});

/* ----------------------------------------------------------------- polling */
async function health() {
  try {
    const h = await api('/api/health');
    $('#health').className = `pill ${h.gpu_ok ? 'ok' : 'warn'}`;
    $('#health').textContent = h.gpu_ok ? 'GPU ready' : `CPU only — ${h.detail}`;
  } catch {
    $('#health').className = 'pill bad';
    $('#health').textContent = 'server unreachable';
  }
}

// Refresh the run list while anything is active, so progress ticks without a reload.
setInterval(() => {
  if (!$('#view-runs').classList.contains('hidden')) loadRuns();
}, 2000);

health();
loadLibrary();
