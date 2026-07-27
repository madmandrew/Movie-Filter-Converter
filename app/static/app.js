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

/* ------------------------------------------------ toasts + styled dialogs */

/** Transient message. Replaces alert(): non-blocking, dismissible, and styled.
 *  Errors persist until dismissed, since they usually need reading. */
function toast(message, kind = 'info', ms = null) {
  const el = document.createElement('div');
  el.className = `toast ${kind === 'error' ? 'err' : kind}`;
  el.innerHTML = `<span class="msg">${esc(message)}</span>
                  <button title="Dismiss">&times;</button>`;
  const remove = () => {
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 180);
  };
  $('button', el).addEventListener('click', remove);
  $('#toasts').appendChild(el);
  const life = ms ?? (kind === 'error' ? 0 : 4500);
  if (life) setTimeout(remove, life);
  return el;
}

/** Modal confirm/prompt. Resolves to a boolean, or the entered string, or null.
 *  Lives above the main modal so it can be raised from inside one. */
function dialog({ title, body = '', okText = 'OK', cancelText = 'Cancel',
                  input = null, danger = false }) {
  return new Promise((resolve) => {
    const box = $('#dialog');
    const field = $('#dlginput');
    $('#dlgtitle').textContent = title;
    $('#dlgbody').textContent = body;
    $('#dlgok').textContent = okText;
    $('#dlgcancel').textContent = cancelText;
    $('#dlgok').className = danger ? 'danger' : '';
    field.classList.toggle('hidden', input === null);
    if (input !== null) {
      field.value = input.value || '';
      field.placeholder = input.placeholder || '';
    }
    box.classList.remove('hidden');
    if (input !== null) setTimeout(() => field.focus(), 30);

    const done = (value) => {
      box.classList.add('hidden');
      $('#dlgok').removeEventListener('click', onOk);
      $('#dlgcancel').removeEventListener('click', onCancel);
      document.removeEventListener('keydown', onKey);
      box.removeEventListener('click', onBackdrop);
      resolve(value);
    };
    const onOk = () => done(input !== null ? field.value.trim() : true);
    const onCancel = () => done(input !== null ? null : false);
    const onKey = (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); onCancel(); }
      if (e.key === 'Enter' && input !== null) onOk();
    };
    const onBackdrop = (e) => { if (e.target === box) onCancel(); };

    $('#dlgok').addEventListener('click', onOk);
    $('#dlgcancel').addEventListener('click', onCancel);
    // Capture phase so Escape closes this dialog, not the modal underneath.
    document.addEventListener('keydown', onKey, true);
    box.addEventListener('click', onBackdrop);
  });
}

const confirmDialog = (title, body, opts = {}) =>
  dialog({ title, body, okText: opts.okText || 'Continue', ...opts });

const promptDialog = (title, body, placeholder = '') =>
  dialog({ title, body, input: { placeholder }, okText: 'OK' });

/** Timecode. Includes hours past the hour mark — a feature-length title's incidents
 *  otherwise read as "75:24" or worse, "57:00" for what is really 00:57:00. */
const tc = (s) => {
  if (s == null) return '';
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, '0');
  const ss = sec.toFixed(2).padStart(5, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
};

/* ------------------------------------------------------------------- tabs */
$$('.tab').forEach((b) => b.addEventListener('click', () => {
  $$('.tab').forEach((x) => x.classList.toggle('active', x === b));
  $$('.view').forEach((v) => v.classList.add('hidden'));
  $(`#view-${b.dataset.view}`).classList.remove('hidden');
  if (b.dataset.view === 'runs') loadRuns();
  if (b.dataset.view === 'words') loadWords();
  if (b.dataset.view === 'settings') loadSettings();
  if (b.dataset.view === 'tagsets') { loadTagsets(); loadSkipfiles(); }
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
      <td>${t.status === 'unfiltered'
            ? `<span class="pill">${esc(t.status)}</span>`
            : `<button class="secondary histbtn" data-path="${esc(t.path)}"
                 title="See what was filtered and how">${esc(t.status)}</button>`}</td>
      <td>${vidangelCell(t)}</td>
      <td><button data-path="${esc(t.path)}" class="filterbtn secondary">Filter…</button></td>
    </tr>`;
  }).join('') || '<tr><td colspan="7" class="muted">No titles. Try “Rescan library”.</td></tr>';

  $$('.filterbtn').forEach((b) =>
    b.addEventListener('click', () => openFilter(b.dataset.path)));
  $$('.afone').forEach((b) => b.addEventListener('click', async () => {
    b.disabled = true;
    b.textContent = '…';
    try {
      const r = await postJSON('/api/vidangel/autofetch',
        { path: b.dataset.path, force: true });
      if (r.status === 'fetched') {
        toast(r.detail || 'Filters found.', 'ok');
        await loadLibrary();
      } else {
        // Anything short of a confident match goes straight to manual selection,
        // pre-seeded with whatever the filename parsed to.
        openPicker(b.dataset.path);
      }
    } catch (e) {
      toast(e.message, 'error');
      b.disabled = false;
      b.textContent = 'Find';
    }
  }));
  $$('.afpick').forEach((b) =>
    b.addEventListener('click', () => openPicker(b.dataset.path)));
  $$('.histbtn').forEach((b) =>
    b.addEventListener('click', () => openHistory(b.dataset.path)));
}

/** VidAngel column: a cached tag-set, a known-negative answer, or a lookup control.
 *  Anything unresolved is clickable — an automatic answer that fell short is a starting
 *  point for a manual pick, not a dead end. */
function vidangelCell(t) {
  if (t.has_tagset) return '<span class="pill ok">yes</span>';
  const s = t.autofetch_status;
  const pick = (label, cls, title) =>
    `<button class="secondary afpick ${cls}" data-path="${esc(t.path)}"
       title="${esc(title || '')}">${label}</button>`;

  if (s === 'unfilterable') return pick('none', '', t.autofetch_detail);
  if (s === 'none') return pick('no match', '', t.autofetch_detail);
  if (s === 'suggested') return pick('check…', '', t.autofetch_detail);
  if (s === 'error') return pick('retry', '', t.autofetch_detail);
  return `<button class="secondary afone" data-path="${esc(t.path)}">Find</button>`;
}

/* ----------------------------------------------------------- filter history */
async function openHistory(path) {
  modal.classList.remove('hidden');
  $('#mtitle').textContent = 'Filter history';
  $('#mbody').innerHTML = '<p class="muted">loading…</p>';

  let h;
  try {
    h = await api(`/api/title/history?path=${encodeURIComponent(path)}`);
  } catch (e) {
    $('#mbody').innerHTML = `<span class="pill bad">${esc(e.message)}</span>`;
    return;
  }

  const when = (s) => (s ? String(s).replace('T', ' ').replace(/\+.*$/, '') : '—');

  // The archive is the only route back to the raw cut, so its absence is worth
  // shouting about before anyone re-filters.
  const archive = h.archive_path
    ? (h.archive_exists
        ? `<span class="pill ok">archived</span> <code>${esc(h.archive_path)}</code>`
        : `<span class="pill bad">archive MISSING</span> <code>${esc(h.archive_path)}</code>
           <br><span class="muted">Without it there is no way back to the unfiltered
           original.</span>`)
    : '<span class="pill warn">no archive recorded</span>';

  $('#mbody').innerHTML = `
    <p class="muted">${esc(h.name)}</p>
    <fieldset><legend>Current state</legend>
      <div>Status: <span class="pill ${h.status === 'filtered' ? 'ok' : 'bad'}"
        >${esc(h.status)}</span>
        &nbsp; Last filtered: <code>${esc(when(h.filtered_at))}</code></div>
      <div style="margin-top:.4rem">${archive}</div>
      ${h.tag_set_id ? `<div style="margin-top:.4rem">Tag-set
        <code>#${h.tag_set_id}</code></div>` : ''}
    </fieldset>
    ${h.decisions.length ? `<fieldset><legend>Your review decisions
      (${h.decisions.length})</legend>
      <div class="chips">${h.decisions.map((d) => `<span class="chip">
        ${tc(d.at_time)} ${esc(d.word === '__nudity__' ? 'scene' : d.word)}
        <span class="muted">${esc(d.action)}</span></span>`).join('')}</div>
      <p class="muted">Remembered across runs — a re-run applies them.</p>
      </fieldset>` : ''}
    <fieldset><legend>Runs (${h.runs.length})</legend>
      ${h.runs.length ? `<table><thead><tr>
        <th>#</th><th>When</th><th>Status</th><th>Mutes</th><th>Cuts</th>
        <th>Review</th><th></th></tr></thead><tbody>
        ${h.runs.map((r) => `<tr>
          <td class="num">${r.id}</td>
          <td class="muted">${esc(when(r.finished_at || r.created_at))}</td>
          <td><span class="pill ${r.status === 'done' ? 'ok'
            : r.status === 'failed' ? 'bad' : 'warn'}">${esc(r.status)}</span></td>
          <td class="num">${r.summary.muted}</td>
          <td class="num">${r.summary.video_cuts || ''}</td>
          <td class="num">${(r.summary.review + r.summary.not_found
            + r.summary.pending_review) || ''}</td>
          <td><button class="secondary histrun" data-id="${r.id}">Details</button></td>
        </tr>`).join('')}</tbody></table>`
      : '<p class="muted">No runs recorded.</p>'}
    </fieldset>
    ${h.runs.length ? `<fieldset><legend>Most recent configuration</legend>
      <div class="grid2">
        <div>
          <div>Audio quality: <code>${esc(h.runs[0].options.quality || '—')}</code></div>
          <div>Whisper model: <code>${esc(h.runs[0].options.model || '—')}</code></div>
          <div>Word-list scan: <code>${h.runs[0].options.do_scan ? 'yes' : 'no'}</code></div>
          <div>Nudity detection:
            <code>${h.runs[0].options.detect_nudity ? 'yes' : 'no'}</code></div>
        </div>
        <div>
          <div>Categories:
            <code>${esc((h.runs[0].options.categories || []).join(', ') || '—')}</code></div>
          <div>Video categories:
            <code>${esc((h.runs[0].options.video_categories || []).join(', ') || '—')}</code></div>
          <div>Manual: <code>${h.runs[0].options.manual_mutes} mute(s),
            ${h.runs[0].options.manual_cuts} cut(s)</code></div>
          ${h.runs[0].summary.offset != null
            ? `<div>Offset applied:
                 <code>${h.runs[0].summary.offset > 0 ? '+' : ''}${h.runs[0].summary.offset}s</code></div>`
            : ''}
        </div>
      </div>
      ${h.runs[0].summary.render
        ? `<div style="margin-top:.5rem" class="muted">${esc(h.runs[0].summary.render)}</div>`
        : ''}
      ${h.runs[0].summary.output
        ? `<div class="muted"><code>${esc(h.runs[0].summary.output)}</code></div>` : ''}
      </fieldset>` : ''}`;

  $$('.histrun').forEach((b) =>
    b.addEventListener('click', () => showRun(b.dataset.id)));
}

/* --------------------------------------------------- manual VidAngel match picker */
async function openPicker(path, initialQuery) {
  modal.classList.remove('hidden');
  $('#mtitle').textContent = 'Find VidAngel filters';
  $('#mbody').innerHTML = '<p class="muted">searching…</p>';

  const load = async (q) => {
    $('#pkresults').innerHTML = '<p class="muted">searching…</p>';
    try {
      const url = `/api/vidangel/candidates?path=${encodeURIComponent(path)}`
        + (q ? `&q=${encodeURIComponent(q)}` : '');
      const d = await api(url);
      $('#pkquery').value = d.query;
      renderResults(d);
    } catch (e) {
      $('#pkresults').innerHTML = `<span class="pill bad">${esc(e.message)}</span>`;
    }
  };

  const renderResults = (d) => {
    if (!d.results.length) {
      $('#pkresults').innerHTML =
        '<p class="muted">No results. Try a shorter or differently-worded title.</p>';
      return;
    }
    const ep = (d.season != null && d.episode != null)
      ? `<p class="muted">Filename names S${String(d.season).padStart(2, '0')}E${String(d.episode).padStart(2, '0')} — the matching episode is selected automatically once you pick the show.</p>`
      : '';
    $('#pkresults').innerHTML = ep + `
      <table><thead><tr>
        <th>Title</th><th>Year</th><th>Type</th><th>Tags</th><th>Match</th><th></th>
      </tr></thead><tbody>
      ${d.results.map((r) => `<tr>
        <td>${esc(r.title)}</td>
        <td class="num muted">${r.year ?? ''}</td>
        <td class="muted">${esc(r.kind)}</td>
        <td class="num">${r.tag_count || ''}</td>
        <td><span class="pill ${r.score >= 90 ? 'ok' : r.score >= 50 ? 'warn' : ''}"
              >${r.score}</span></td>
        <td>${r.filterable
              ? `<button class="pkuse" data-work="${r.work_id}"
                   data-kind="${esc(r.kind)}">Use this</button>`
              : `<span class="pill bad" title="${esc(r.reason)}">no filters</span>`}</td>
      </tr>`).join('')}</tbody></table>`;

    $$('.pkuse').forEach((b) => b.addEventListener('click', async () => {
      $$('.pkuse').forEach((x) => { x.disabled = true; });
      b.textContent = 'fetching…';
      try {
        const r = await postJSON('/api/vidangel/pick', {
          path, work_id: Number(b.dataset.work), kind: b.dataset.kind,
        });
        if (r.status === 'fetched') {
          closeModal();
          toast(r.detail || 'Filters attached.', 'ok');
          await loadLibrary();
        } else {
          toast(`${r.status}: ${r.detail}`, 'warn');
          $$('.pkuse').forEach((x) => { x.disabled = false; });
          b.textContent = 'Use this';
        }
      } catch (e) {
        toast(e.message, 'error');
        $$('.pkuse').forEach((x) => { x.disabled = false; });
        b.textContent = 'Use this';
      }
    }));
  };

  $('#mbody').innerHTML = `
    <p class="muted" id="pkfile"></p>
    <div class="toolbar">
      <input id="pkquery" placeholder="search VidAngel…" style="flex:1 1 22rem">
      <button id="pksearch">Search</button>
      <button id="pkmanual" class="secondary">Enter tag-set id…</button>
    </div>
    <div id="pkresults"></div>`;

  $('#pkfile').textContent = path.split(/[\\/]/).pop();
  $('#pksearch').addEventListener('click', () => load($('#pkquery').value.trim()));
  $('#pkquery').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') load($('#pkquery').value.trim());
  });
  $('#pkmanual').addEventListener('click', async () => {
    const id = await promptDialog(
      'Enter a tag-set id',
      'Open the title on vidangel.com with DevTools on the Network tab, and copy the '
      + 'number from the /api/bff/tag-sets/<id>/ request.',
      'e.g. 46025');
    if (id === null) return;
    if (!/^\d+$/.test(id)) { toast('That is not a numeric tag-set id.', 'warn'); return; }
    try {
      const r = await postJSON('/api/vidangel/pick',
        { path, tag_set_id: Number(id) });
      if (r.status === 'fetched') {
        closeModal();
        toast(r.detail || 'Filters attached.', 'ok');
        await loadLibrary();
      } else { toast(`${r.status}: ${r.detail}`, 'warn'); }
    } catch (e) { toast(e.message, 'error'); }
  });

  await load(initialQuery);
}

$('#q').addEventListener('input', () => {
  clearTimeout(libTimer);
  libTimer = setTimeout(loadLibrary, 250);
});
$('#lib').addEventListener('change', loadLibrary);
$('#status').addEventListener('change', loadLibrary);
/* ------------------------------------------------- auto-fetch VidAngel filters */
let afPoll;

async function pollAutofetch() {
  const st = await api('/api/vidangel/autofetch');
  const box = $('#afprogress');
  if (!st.running && !st.total) { box.classList.add('hidden'); return; }

  box.classList.remove('hidden');
  const pct = st.total ? Math.round((st.done / st.total) * 100) : 0;
  const counts = Object.entries(st.counts || {})
    .map(([k, v]) => `${v} ${k}`).join(' · ') || '—';
  box.innerHTML = `
    <div class="toolbar">
      <div class="bar" style="width:220px"><i style="width:${pct}%"></i></div>
      <span class="muted">${st.done}/${st.total} — ${esc(counts)}</span>
      <span class="muted">${esc(st.last || '')}</span>
    </div>`;

  if (!st.running) {
    clearInterval(afPoll);
    afPoll = null;
    await loadLibrary();
    box.innerHTML = `<p class="muted">Finished — ${esc(counts)}.
      “check” means a match was found but scored too low to trust; open the title to
      confirm. “none” means VidAngel has no filters for it.</p>`;
  }
}

$('#autofetch').addEventListener('click', async (e) => {
  const lib = $('#lib').value;
  const scope = lib ? `the ${lib} library` : 'all libraries';
  const go = await confirmDialog(
    `Search VidAngel across ${scope}?`,
    'Only close matches are fetched automatically; anything uncertain is listed for you '
    + 'to confirm. Requests are throttled, so a large library takes a while.',
    { okText: 'Start search' });
  if (!go) return;
  e.target.disabled = true;
  try {
    const r = await postJSON('/api/vidangel/autofetch',
      { library: lib || null, limit: 500, only_missing: true });
    if (!r.queued) {
      toast(r.detail || 'Nothing to match.', 'warn');
      return;
    }
    if (afPoll) clearInterval(afPoll);
    afPoll = setInterval(pollAutofetch, 1500);
    pollAutofetch();
  } catch (err) {
    toast(`Could not start: ${err.message}`, 'error');
  } finally {
    e.target.disabled = false;
  }
});

$('#rescan').addEventListener('click', async (e) => {
  e.target.disabled = true;
  e.target.textContent = 'Scanning…';
  try {
    const r = await api('/api/library/scan', { method: 'POST' });
    $('#libstats').textContent = Object.entries(r)
      .filter(([k]) => !k.startsWith('_')).map(([k, v]) => `${k}: ${v}`).join(' · ');
    await loadLibrary();
  } catch (err) {
    toast(`Scan failed: ${err.message}`, 'error');
  } finally {
    e.target.disabled = false;
    e.target.textContent = 'Rescan library';
  }
});

/* ------------------------------------------------------------ filter modal */
const modal = $('#modal');
const closeModal = () => modal.classList.add('hidden');
$('#modalclose').addEventListener('click', closeModal);
modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
// Always leave a keyboard escape hatch — a modal that cannot be dismissed blocks the
// whole page.
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !modal.classList.contains('hidden')) closeModal();
});

async function openFilter(path) {
  modal.classList.remove('hidden');
  $('#mtitle').textContent = path.split(/[\\/]/).pop();
  $('#mbody').innerHTML = '<p class="muted">Probing…</p>';

  const t = await api(`/api/title?path=${encodeURIComponent(path)}`);
  const lossless = /hd ma|dts:x|truehd|atmos/i.test(t.audio_profile || '');
  // The backend already parses release filenames (titleparse.py, covered by tests), so
  // use its result rather than reimplementing the stop-token logic here and letting the
  // two drift apart.
  const searchTitle = t.parsed_title || t.name || '';

  let groupsHtml = '<p class="muted">No tag-set linked. The word-list scan still runs.</p>';
  let tsSelect = '<option value="">none</option>';
  if (t.tagsets?.length) {
    tsSelect += t.tagsets.map((x) => {
      // Runtime delta is the wrong-cut pre-flight: a tag-set keyed to a different cut
      // has every timing offset. The offset estimator corrects for it, but seeing the
      // number up front explains an otherwise surprising result.
      const delta = x.runtime_delta != null
        ? ` — ${x.runtime_delta > 0 ? '+' : ''}${x.runtime_delta}s vs this file` : '';
      const tag = x.linked ? ' ✓ linked' : '';
      return `<option value="${x.tag_set_id}"${x.linked ? ' selected' : ''}
        >#${x.tag_set_id} ${esc(x.title_hint || '')}${delta}${tag}</option>`;
    }).join('');
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
      <p class="muted">
        Nothing here? Try the
        <a href="https://videoskip.org/exchange/" target="_blank"
           rel="noopener noreferrer">VideoSkip Exchange</a>
        for a community skip file — search it for
        <strong>${esc(searchTitle)}</strong>, download the file, then add it under
        “Or paste a filter file” on the Tag-sets tab.
      </p>
      <div id="mgroups">${groupsHtml}</div>
    </fieldset>
    <fieldset><legend>VideoSkip filter file</legend>
      <div class="toolbar">
        <select id="mvsk"><option value="">none</option></select>
        <span class="muted">A saved skip file can drive mutes and cuts on its own.</span>
      </div>
    </fieldset>
    <fieldset><legend>Word-list scan</legend>
      <label class="row"><input type="checkbox" id="mscan" checked>
        <span>Scan the whole track for word-list matches
          <code>— finds words VidAngel missed; hits await your review</code></span></label>
      <label class="row"><input type="checkbox" id="monlyen">
        <span>Only tags already enabled in VidAngel
          <code>— use your existing selections instead of whole categories</code></span></label>
      <label class="row">
        <span>Whisper model</span>
        <select id="mmodel">
          <option value="small.en">small.en — fast, verified</option>
          <option value="medium.en">medium.en — slower, may catch more</option>
          <option value="base.en">base.en — fastest, least accurate</option>
        </select>
      </label>
    </fieldset>
    <fieldset><legend>Manual filters</legend>
      <p class="muted">For titles VidAngel doesn't cover, or a single thing you want gone.
        A word plus a rough time is located and verified precisely; a time range is
        applied exactly as entered.</p>
      <div class="toolbar">
        <select id="mankind">
          <option value="word">Mute a word near…</option>
          <option value="range">Mute a time range</option>
          <option value="cut">Cut video</option>
        </select>
        <input id="manword" placeholder="word" style="max-width:9rem">
        <input id="manat" placeholder="mm:ss or seconds" style="max-width:11rem">
        <input id="manend" placeholder="end (mm:ss)" style="max-width:11rem" class="hidden">
        <label class="row hidden" id="mansnapwrap">
          <input type="checkbox" id="mansnap" checked><span>snap to scene cuts</span></label>
        <button id="manadd" class="secondary">Add</button>
      </div>
      <div id="manlist"></div>
    </fieldset>
    <div class="toolbar">
      <button id="mgo">Queue filter run</button>
      <span id="mmsg" class="muted"></span>
    </div>`;

  /* --- manual entry list ------------------------------------------------- */
  const manual = [];
  const parseTime = (v) => {
    const s = String(v).trim();
    if (!s) return null;
    if (s.includes(':')) {
      const parts = s.split(':').map(Number);
      if (parts.some(Number.isNaN)) return null;
      return parts.reduce((acc, p) => acc * 60 + p, 0);
    }
    const n = Number(s);
    return Number.isNaN(n) ? null : n;
  };

  const renderManual = () => {
    $('#manlist').innerHTML = manual.map((m, i) => {
      let desc;
      if (m.kind === 'word') desc = `mute “${m.word}” near ${tc(m.at)}`;
      else if (m.kind === 'range') desc = `mute ${tc(m.start)}–${tc(m.end)}`;
      else desc = `cut video ${tc(m.start)}–${tc(m.end)}${m.snap ? ' (snapped)' : ''}`;
      return `<span class="chip">${esc(desc)}
        <button data-i="${i}" title="Remove">&times;</button></span>`;
    }).join(' ');
    $$('#manlist button').forEach((b) => b.addEventListener('click', () => {
      manual.splice(Number(b.dataset.i), 1);
      renderManual();
    }));
  };

  const syncManualFields = () => {
    const k = $('#mankind').value;
    $('#manword').classList.toggle('hidden', k !== 'word');
    $('#manend').classList.toggle('hidden', k === 'word');
    $('#mansnapwrap').classList.toggle('hidden', k !== 'cut');
    $('#manat').placeholder = k === 'word' ? 'approx time (mm:ss)' : 'start (mm:ss)';
  };
  $('#mankind').addEventListener('change', syncManualFields);
  syncManualFields();

  $('#manadd').addEventListener('click', () => {
    const kind = $('#mankind').value;
    const at = parseTime($('#manat').value);
    if (at == null) { toast('Enter a time as mm:ss or seconds.', 'warn'); return; }
    if (kind === 'word') {
      const w = $('#manword').value.trim();
      if (!w) { toast('Enter the word to mute.', 'warn'); return; }
      manual.push({ kind, word: w, at });
    } else {
      const end = parseTime($('#manend').value);
      if (end == null || end <= at) { toast('End must be after start.', 'warn'); return; }
      manual.push({ kind, start: at, end, snap: $('#mansnap').checked });
    }
    $('#manword').value = ''; $('#manat').value = ''; $('#manend').value = '';
    renderManual();
  });

  const loadGroups = async (id) => {
    if (!id) { $('#mgroups').innerHTML = groupsHtml; return; }
    $('#mgroups').innerHTML = '<p class="muted">loading categories…</p>';
    const ts = await api(`/api/tagsets/${id}`);
    // Each incident is listed individually with its own description and timestamp.
    // A category checkbox alone hides the fact that "Sexually Suggestive" might be one
    // scene worth cutting and two worth keeping — the descriptions are the only way to
    // tell, and they are the whole reason for choosing per incident.
    $('#mgroups').innerHTML = ts.groups.map((g, gi) => {
      const kind = g.kind === 'audiovisual' ? 'video' : 'audio';
      const n = g.incidents.length;
      const note = g.locatable
        ? ''
        : ` <span class="pill warn">${kind === 'video'
            ? 'video cut' : 'no specific word — muted as timed'}</span>`;
      return `<details class="catgroup" ${n <= 6 ? 'open' : ''}>
        <summary>
          <label class="row" onclick="event.stopPropagation()">
            <input type="checkbox" class="catbox" data-key="${esc(g.key)}"
                   data-kind="${kind}" data-locatable="${g.locatable}"
                   data-group="${gi}">
            <span>${esc(g.title)} <code>(${n} ${kind})</code>${note}</span>
          </label>
        </summary>
        <div class="incidents">
          ${g.incidents.map((i) => `
            <label class="row incident-row">
              <input type="checkbox" class="incbox" data-group="${gi}"
                     data-ref="${esc(i.ref_id)}" data-key="${esc(g.key)}"
                     data-kind="${i.kind === 'audiovisual' ? 'video' : 'audio'}">
              <span class="tcell">${tc(i.start)}${i.end > i.start
                ? `–${tc(i.end)}` : ''}</span>
              <span>${esc(i.description)}
                ${i.enabled ? '<span class="pill ok">was on</span>' : ''}</span>
            </label>`).join('')}
        </div>
      </details>`;
    }).join('');

    // A category box selects every incident under it; unticking any one clears the
    // category box so the two never disagree.
    $$('.catbox').forEach((cb) => cb.addEventListener('change', () => {
      $$(`.incbox[data-group="${cb.dataset.group}"]`)
        .forEach((i) => { i.checked = cb.checked; });
    }));
    $$('.incbox').forEach((ib) => ib.addEventListener('change', () => {
      const peers = $$(`.incbox[data-group="${ib.dataset.group}"]`);
      const cb = $(`.catbox[data-group="${ib.dataset.group}"]`);
      if (!cb) return;
      cb.checked = peers.every((p) => p.checked);
      cb.indeterminate = !cb.checked && peers.some((p) => p.checked);
    }));
  };

  // Saved skip files, offered as an alternative source to a VidAngel tag-set. Loaded
  // after render so a failure here cannot block the rest of the dialog.
  api('/api/skipfiles').then((d) => {
    const sel = $('#mvsk');
    if (!sel) return;
    sel.innerHTML = '<option value="">none</option>' + d.skipfiles.map((s) =>
      `<option value="${s.id}">#${s.id} ${esc(s.title_hint || s.format)}
        — ${s.audio_count} audio, ${s.video_count} video</option>`).join('');
  }).catch(() => { /* leave the "none" option in place */ });

  $('#mts').addEventListener('change', (e) => loadGroups(e.target.value));
  // A preselected <option> fires no change event, so load the linked tag-set's
  // categories explicitly — otherwise the list sits empty on a manually-picked match.
  await loadGroups($('#mts').value);

  $('#mgo').addEventListener('click', async (e) => {
    e.target.disabled = true;
    // Send individual refs, so picking 2 of 6 scenes in a category means exactly those
    // two. Category keys still go along for the report and for the audio path's word
    // lookup, but the refs are what decide.
    const audioCats = $$('.catbox').filter((c) => c.checked && c.dataset.locatable === 'true')
      .map((c) => c.dataset.key);
    const videoCats = $$('.catbox').filter((c) => c.checked && c.dataset.kind === 'video')
      .map((c) => c.dataset.key);
    const chosen = $$('.incbox').filter((i) => i.checked);
    const audioRefs = chosen.filter((i) => i.dataset.kind === 'audio')
      .map((i) => i.dataset.ref);
    const videoRefs = chosen.filter((i) => i.dataset.kind === 'video')
      .map((i) => i.dataset.ref);
    try {
      const r = await postJSON('/api/runs', {
        path,
        tag_set_id: $('#mts').value ? Number($('#mts').value) : null,
        videoskip_id: $('#mvsk')?.value ? Number($('#mvsk').value) : null,
        categories: audioCats,
        video_categories: videoCats,
        audio_refs: audioRefs,
        video_refs: videoRefs,
        quality: $('input[name=q]:checked').value,
        do_scan: $('#mscan').checked,
        only_enabled: $('#monlyen').checked,
        model: $('#mmodel').value,
        manual_mutes: manual.filter((m) => m.kind !== 'cut').map((m) =>
          m.kind === 'word'
            ? { word: m.word, at: m.at }
            : { start: m.start, end: m.end }),
        manual_cuts: manual.filter((m) => m.kind === 'cut').map((m) =>
          ({ start: m.start, end: m.end, snap: m.snap })),
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
      <td><button class="secondary rundet" data-id="${r.id}">Details</button>
        ${r.status === 'queued'
          ? `<button class="secondary runcancel" data-id="${r.id}">Cancel</button>` : ''}
      </td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" class="muted">No runs yet.</td></tr>';

  $$('.rundet').forEach((b) => b.addEventListener('click', () => showRun(b.dataset.id)));
  // Only queued runs can be cancelled — a running job is mid-write in ffmpeg/Whisper.
  $$('.runcancel').forEach((b) => b.addEventListener('click', async () => {
    try {
      await postJSON(`/api/runs/${b.dataset.id}/cancel`, {});
      loadRuns();
    } catch (e) { toast(e.message, 'error'); }
  }));
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
      <div class="toolbar" style="margin-top:.6rem">
        <button id="rerun">Re-run to apply decisions</button>
        <span class="muted">A mute has to be located and rendered, so applying
          decisions needs another pass.</span>
      </div>
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

  const rerunBtn = $('#rerun');
  if (rerunBtn) {
    rerunBtn.addEventListener('click', async (e) => {
      e.target.disabled = true;
      try {
        const nr = await postJSON(`/api/runs/${r.id}/rerun`, {});
        closeModal();
        $$('.tab')[1].click();
        setTimeout(() => showRun(nr.run_id), 400);
      } catch (err) {
        toast(`Could not re-run: ${err.message}`, 'error');
        e.target.disabled = false;
      }
    });
  }
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
  } catch (e) { toast(e.message, 'error'); }
});

/* --------------------------------------------------------------- tag-sets */
async function loadTagsets() {
  const a = await api('/api/vidangel/auth');
  $('#vastatus').innerHTML = a.has_token
    ? `<span class="pill ok">token saved (${esc(a.token_hint)})</span>`
    : '<span class="pill warn">no token saved</span>';
  $('#vaapi').value = a.api_template;

  const d = await api('/api/tagsets');
  $('#tslist tbody').innerHTML = d.tagsets.map((t) => `<tr>
      <td class="num">${t.tag_set_id}</td>
      <td>${esc(t.title_hint || '—')}</td>
      <td class="num muted">${t.runtime ? tc(t.runtime) : '—'}</td>
      <td class="muted">${esc((t.added_at || '').replace('T', ' '))}</td>
      <td><button class="secondary tsdel" data-id="${t.tag_set_id}">Remove</button></td>
    </tr>`).join('')
    || '<tr><td colspan="5" class="muted">None cached yet.</td></tr>';

  $$('.tsdel').forEach((b) => b.addEventListener('click', async () => {
    await api(`/api/tagsets/${b.dataset.id}`, { method: 'DELETE' });
    loadTagsets();
  }));
}

$('#vasave').addEventListener('click', async () => {
  const token = $('#vatoken').value.trim();
  if (!token) return;
  try {
    await postJSON('/api/vidangel/auth', { token });
    $('#vatoken').value = '';
    await loadTagsets();
  } catch (e) {
    $('#vastatus').innerHTML = `<span class="pill bad">${esc(e.message)}</span>`;
  }
});

$('#vaclear').addEventListener('click', async () => {
  await api('/api/vidangel/auth', { method: 'DELETE' });
  loadTagsets();
});

$('#vaapisave').addEventListener('click', async () => {
  try {
    await postJSON('/api/vidangel/auth', { api_template: $('#vaapi').value.trim() });
    $('#vastatus').innerHTML = '<span class="pill ok">endpoint saved</span>';
  } catch (e) {
    $('#vastatus').innerHTML = `<span class="pill bad">${esc(e.message)}</span>`;
  }
});

$('#vasearch').addEventListener('click', async (e) => {
  const q = $('#vaq').value.trim();
  if (!q) return;
  e.target.disabled = true;
  $('#vares').innerHTML = '<span class="muted">searching…</span>';
  try {
    const r = await api(`/api/vidangel/search?q=${encodeURIComponent(q)}`);
    if (!r.results.length) {
      $('#vares').innerHTML = '<span class="muted">no matches</span>';
      return;
    }
    $('#vares').innerHTML = `<table><thead><tr>
        <th>Title</th><th>Year</th><th>Type</th><th>Tags</th>
        <th>Filterable</th><th>Work id</th><th></th></tr></thead><tbody>
      ${r.results.map((x) => `<tr>
        <td>${esc(x.title)}</td>
        <td class="num muted">${x.year ?? ''}</td>
        <td class="muted">${esc(x.kind)}</td>
        <td class="num">${x.tag_count || ''}</td>
        <td>${x.filterable
              ? '<span class="pill ok">yes</span>'
              : `<span class="pill bad" title="${esc(x.reason)}">no</span>`}</td>
        <td class="num muted">${x.work_id}</td>
        <td>${x.filterable
              ? `<button class="secondary varesolve" data-id="${x.work_id}"
                   data-kind="${esc(x.kind)}" data-title="${esc(x.title)}">Filters…</button>`
              : ''}</td>
      </tr>`).join('')}</tbody></table>
      <div id="varesolved"></div>`;

    $$('.varesolve').forEach((b) => b.addEventListener('click', () =>
      resolveWork(b.dataset.id, b.dataset.kind, b.dataset.title)));
  } catch (err) {
    $('#vares').innerHTML = `<span class="pill bad">${esc(err.message)}</span>`;
  } finally {
    e.target.disabled = false;
  }
});

async function resolveWork(workId, kind, title) {
  const box = $('#varesolved');
  box.innerHTML = '<span class="muted">resolving tag-sets…</span>';
  try {
    const r = await api(
      `/api/vidangel/resolve?work_id=${workId}&kind=${encodeURIComponent(kind)}`);
    if (!r.entries.length) {
      box.innerHTML = '<span class="muted">no tag-sets found</span>';
      return;
    }
    const many = r.entries.length > 1;
    box.innerHTML = `
      <p class="muted" style="margin-top:.8rem">
        ${esc(title)} — ${r.entries.length} ${many ? 'episodes' : 'entry'}.
        A title can have several tag-sets, one per streaming service, because services
        carry different cuts. Compare <em>runtime</em> against your file and pick the
        closest.</p>
      ${many ? `<input id="vafilter" placeholder="filter episodes, e.g. S01E02"
                 style="margin-bottom:.5rem">` : ''}
      <table id="vaeps"><thead><tr>
        <th>Episode</th><th>Runtime</th><th>Tags</th><th>Tag-sets</th>
      </tr></thead><tbody>
      ${r.entries.map((e) => `<tr data-label="${esc(e.label.toLowerCase())}">
        <td>${esc(e.label)}</td>
        <td class="num muted">${e.runtime ? tc(e.runtime) : '—'}</td>
        <td class="num">${e.tag_count || ''}</td>
        <td>${e.tag_sets.map((t) => `
          <button class="secondary vagrab" data-id="${t.tag_set_id}"
            data-hint="${esc(e.label)}" title="${esc(t.service)} ${esc(t.type)}">
            #${t.tag_set_id} ${esc(t.service)}${t.cached ? ' ✓' : ''}</button>`).join(' ')
          || '<span class="muted">none</span>'}</td>
      </tr>`).join('')}</tbody></table>`;

    $$('.vagrab').forEach((b) => b.addEventListener('click', async () => {
      b.disabled = true;
      const original = b.textContent;
      b.textContent = 'fetching…';
      try {
        const got = await postJSON('/api/vidangel/fetch',
          { url: String(b.dataset.id), title_hint: b.dataset.hint });
        b.textContent = `#${got.tag_set_id} ✓ ${got.incidents} tags`;
        await loadTagsets();
      } catch (err) {
        b.textContent = original;
        b.disabled = false;
        toast(`Fetch failed: ${err.message}`, 'error');
      }
    }));

    const filt = $('#vafilter');
    if (filt) {
      filt.addEventListener('input', () => {
        const needle = filt.value.trim().toLowerCase();
        $$('#vaeps tbody tr').forEach((row) => {
          row.classList.toggle('hidden',
            Boolean(needle) && !row.dataset.label.includes(needle));
        });
      });
    }
  } catch (err) {
    box.innerHTML = `<span class="pill bad">${esc(err.message)}</span>`;
  }
}

$('#vafetch').addEventListener('click', async (e) => {
  const url = $('#vaurl').value.trim();
  if (!url) return;
  e.target.disabled = true;
  $('#vafetchmsg').innerHTML = '<span class="muted">fetching…</span>';
  try {
    const r = await postJSON('/api/vidangel/fetch', {
      url, title_hint: $('#vahint').value.trim() || null,
    });
    $('#vafetchmsg').innerHTML = `<span class="pill ok">fetched #${r.tag_set_id}:
      ${r.incidents} incidents, ${r.enabled} pre-enabled, runtime ${r.runtime}s</span>`;
    $('#vaurl').value = '';
    await loadTagsets();
  } catch (err) {
    // Fetch failures are expected to be informative: expired token, no outbound
    // access, or a changed API shape all read differently.
    $('#vafetchmsg').innerHTML = `<div class="pill bad" style="white-space:normal;
      display:block;padding:.5rem">${esc(err.message)}</div>
      <p class="muted">Pasting the JSON below always works.</p>`;
  } finally {
    e.target.disabled = false;
  }
});

async function loadSkipfiles() {
  const d = await api('/api/skipfiles');
  $('#sklist tbody').innerHTML = d.skipfiles.map((s) => `<tr>
      <td class="num">${s.id}</td>
      <td>${esc(s.title_hint || '—')}</td>
      <td class="muted">${esc(s.format)}</td>
      <td class="num">${s.audio_count}</td>
      <td class="num">${s.video_count}</td>
      <td class="muted">${esc((s.added_at || '').replace('T', ' '))}</td>
      <td><button class="secondary skdel" data-id="${s.id}">Remove</button></td>
    </tr>`).join('')
    || '<tr><td colspan="7" class="muted">None saved yet.</td></tr>';

  $$('.skdel').forEach((b) => b.addEventListener('click', async () => {
    await api(`/api/skipfiles/${b.dataset.id}`, { method: 'DELETE' });
    loadSkipfiles();
  }));
}

$('#saveskip').addEventListener('click', async (e) => {
  const payload = $('#skpayload').value.trim();
  if (!payload) return;
  e.target.disabled = true;
  try {
    const r = await postJSON('/api/skipfiles',
      { payload, title_hint: $('#skhint').value.trim() || null });
    $('#skipmsg').innerHTML = `<span class="pill ok">saved #${r.id} (${esc(r.format)}):
      ${r.audio} audio, ${r.video} video</span>`;
    $('#skpayload').value = '';
    $('#skhint').value = '';
    await loadSkipfiles();
  } catch (err) {
    $('#skipmsg').innerHTML = `<span class="pill bad">${esc(err.message)}</span>`;
  } finally {
    e.target.disabled = false;
  }
});

$('#savetagset').addEventListener('click', async () => {
  const payload = $('#payload').value.trim();
  if (!payload) return;
  try {
    const r = await postJSON('/api/tagsets', { payload, title_hint: $('#hint').value.trim() || null });
    $('#tagsetmsg').innerHTML =
      `<span class="pill ok">saved #${r.tag_set_id}: ${r.incidents} incidents,
       ${r.enabled} pre-enabled, runtime ${r.runtime}s</span>`;
    $('#payload').value = '';
    await loadTagsets();
  } catch (e) {
    $('#tagsetmsg').innerHTML = `<span class="pill bad">${esc(e.message)}</span>`;
  }
});

/* --------------------------------------------------------------- settings */
let sampleTitle = null;

async function loadSettings() {
  const s = await api('/api/settings');
  $('#archtpl').value = s.archive.template;
  $('#archtree').checked = !!s.archive.keep_tree;
  $('#archph').innerHTML =
    `Placeholders: ${s.archive_placeholders.map((p) => `<code>${esc(p)}</code>`).join(' ')}
     &nbsp;·&nbsp; <code>{root}</code> resolves to <code>${esc(s.media_root || '—')}</code>`;

  $('#roots').innerHTML = Object.entries(s.library_roots).map(([k, v]) => `
    <div class="toolbar">
      <input class="rootname" value="${esc(k)}" style="max-width:12rem">
      <input class="rootpath" value="${esc(v)}" style="flex:1 1 20rem">
      <button class="secondary rootdel" title="Remove">&times;</button>
    </div>`).join('');
  $$('.rootdel').forEach((b) => b.addEventListener('click', () => {
    b.closest('.toolbar').remove();
  }));

  // Preview against a real title so the template's effect is concrete.
  if (!sampleTitle) {
    const lib = await api('/api/library?limit=1');
    sampleTitle = lib.items[0]?.path || null;
  }
  previewArchive();
}

let prevTimer;
async function previewArchive() {
  if (!sampleTitle) { $('#archprev').textContent = ''; return; }
  const qs = new URLSearchParams({
    path: sampleTitle,
    template: $('#archtpl').value,
    keep_tree: $('#archtree').checked ? 'true' : 'false',
  });
  try {
    const r = await api(`/api/settings/archive-preview?${qs}`);
    $('#archprev').innerHTML =
      `<div style="margin-top:.4rem">Example — <code>${esc(sampleTitle.split(/[\\/]/).pop())}</code>
       would archive to:<br><code>${esc(r.archive_path)}</code></div>`;
  } catch (e) {
    $('#archprev').innerHTML = `<span class="pill bad">${esc(e.message)}</span>`;
  }
}
$('#archtpl').addEventListener('input', () => {
  clearTimeout(prevTimer);
  prevTimer = setTimeout(previewArchive, 300);
});
$('#archtree').addEventListener('change', previewArchive);

$('#archsave').addEventListener('click', async () => {
  try {
    await postJSON('/api/settings', {
      archive: { template: $('#archtpl').value, keep_tree: $('#archtree').checked },
    });
    $('#archmsg').innerHTML = '<span class="pill ok">saved</span>';
  } catch (e) {
    $('#archmsg').innerHTML = `<span class="pill bad">${esc(e.message)}</span>`;
  }
});

$('#addroot').addEventListener('click', () => {
  const name = $('#newrootname').value.trim();
  const p = $('#newrootpath').value.trim();
  if (!name || !p) return;
  $('#roots').insertAdjacentHTML('beforeend', `
    <div class="toolbar">
      <input class="rootname" value="${esc(name)}" style="max-width:12rem">
      <input class="rootpath" value="${esc(p)}" style="flex:1 1 20rem">
      <button class="secondary rootdel" title="Remove">&times;</button>
    </div>`);
  $('#newrootname').value = ''; $('#newrootpath').value = '';
  $$('.rootdel').forEach((b) => b.addEventListener('click', () => {
    b.closest('.toolbar').remove();
  }));
});

$('#rootsave').addEventListener('click', async () => {
  const roots = {};
  $$('#roots .toolbar').forEach((row) => {
    const k = $('.rootname', row).value.trim();
    const v = $('.rootpath', row).value.trim();
    if (k && v) roots[k] = v;
  });
  try {
    await postJSON('/api/settings', { library_roots: roots });
    $('#rootmsg').innerHTML =
      '<span class="pill ok">saved — rescan the library to pick up changes</span>';
    await loadSettings();
  } catch (e) {
    $('#rootmsg').innerHTML = `<span class="pill bad">${esc(e.message)}</span>`;
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
