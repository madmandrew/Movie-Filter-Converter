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
/** Copy text to the clipboard.
 *
 *  `navigator.clipboard` requires a secure context, which plain http:// on a LAN IP or
 *  Unraid host is not — so it is simply absent there. Falls back to a hidden textarea
 *  and execCommand, which is deprecated but the only thing that works over http.
 */
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  // Keep it off-screen but focusable; display:none would break selection.
  ta.style.cssText = 'position:fixed;top:-1000px;left:-1000px;opacity:0';
  document.body.appendChild(ta);
  try {
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    if (!document.execCommand('copy')) {
      throw new Error('the browser refused the copy');
    }
  } finally {
    ta.remove();
  }
}

const tc = (s) => {
  if (s == null) return '';
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, '0');
  const ss = sec.toFixed(2).padStart(5, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
};

/** Escape a value used inside a CSS attribute selector. Words come from a transcript,
 *  so quotes and backslashes are possible and would break the selector. */
const cssEsc = (s) => String(s ?? '').replace(/["\\]/g, '\\$&');

/** Group review hits by the word, keeping each group in time order.
 *
 *  Reviewing 50 loose hits one at a time is the complaint this answers: grouped, the
 *  same list is a handful of decisions ("all 23 of these, mute"). `i` is the hit's index
 *  in the original array, which is how a checkbox maps back to its timestamp.
 *  Groups are ordered by size so the biggest — the one worth a single bulk click —
 *  is at the top. */
function groupHits(hits) {
  const by = new Map();
  hits.forEach((h, i) => {
    const key = String(h.word || '').toLowerCase();
    if (!by.has(key)) by.set(key, []);
    by.get(key).push({ ...h, i });
  });
  return [...by.entries()]
    .map(([word, hs]) => ({ word, hits: hs.sort((a, b) => a.at - b.at) }))
    .sort((a, b) => b.hits.length - a.hits.length || a.word.localeCompare(b.word));
}

// "yes" is misleading for a windowed run — it reads as "the whole title was checked".
const nudityScopeLabel = (o) => {
  if (!o || !o.detect_nudity) return 'no';
  const s = o.nudity_start;
  const e = o.nudity_end;
  if ((s == null || s === 0) && e == null) return 'whole film';
  return `${tc(s || 0)}–${e != null ? tc(e) : 'end'} only`;
};

/* ------------------------------------------------------------------- tabs */
$$('.tab').forEach((b) => b.addEventListener('click', () => {
  $$('.tab').forEach((x) => x.classList.toggle('active', x === b));
  $$('.view').forEach((v) => v.classList.add('hidden'));
  $(`#view-${b.dataset.view}`).classList.remove('hidden');
  if (b.dataset.view === 'live') startLive();
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
    ${h.file_exists === false ? `<fieldset><legend>File is missing</legend>
      <p class="muted">Nothing is at <code>${esc(path)}</code> any more. If you moved it,
        point this history at its new location — the runs, review decisions and
        always-mute rules below all follow it.</p>
      ${(h.move_candidates || []).length ? `
        <div class="chips">${h.move_candidates.map((c) => `
          <span class="chip">${esc(c.name)}
            <span class="muted">${esc(c.library)}${c.same_name ? '' : ' · renamed'}</span>
            <button class="relink" data-new="${esc(c.path)}"
              title="${esc(c.path)}">use this</button></span>`).join('')}</div>`
        : '<p class="muted">No matching file found in the library. If you moved it '
          + 'outside the configured roots, move it back or add that root in Settings.</p>'}
      </fieldset>` : ''}
    ${(h.word_rules || []).length ? `<fieldset><legend>Always-mute words
      (${h.word_rules.length})</legend>
      <div class="chips">${h.word_rules.map((w) => `<span class="chip">
        ${esc(w.word)} <span class="muted">${esc(w.action)} every instance</span>
        <button class="delrule" data-w="${esc(w.word)}"
          title="Back to reviewing each one">&times;</button></span>`).join('')}</div>
      <p class="muted">Applied on every run, including instances a later scan finds for
        the first time. These never come back for review.</p>
      </fieldset>` : ''}
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
          <td><button class="secondary histrun" data-id="${r.id}">Details</button>
            <button class="secondary histedit" data-id="${r.id}"
              title="Reopen these settings to adjust and re-run">Edit</button></td>
        </tr>`).join('')}</tbody></table>`
      : '<p class="muted">No runs recorded.</p>'}
    </fieldset>
    ${h.runs.length ? `<fieldset><legend>Most recent configuration</legend>
      <div class="grid2">
        <div>
          <div>Audio quality: <code>${esc(h.runs[0].options.quality || '—')}</code></div>
          <div>Whisper model: <code>${esc(h.runs[0].options.model || '—')}</code></div>
          <div>Word-list scan: <code>${h.runs[0].options.do_scan ? 'yes' : 'no'}</code></div>
          <div>Muted without review:
            <code>${esc((h.runs[0].options.auto_mute_words || []).join(', ') || '—')}</code></div>
          <div>Nudity detection:
            <code>${nudityScopeLabel(h.runs[0].options)}</code></div>
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
  $$('.histedit').forEach((b) =>
    b.addEventListener('click', () => editRun(b.dataset.id)));

  $$('.relink').forEach((b) => b.addEventListener('click', async () => {
    b.disabled = true;
    try {
      await postJSON('/api/library/moved',
        { old_path: path, new_path: b.dataset.new });
    } catch (err) {
      toast(`Could not reconnect: ${err.message}`, 'error');
      b.disabled = false;
      return;
    }
    toast('History reconnected to the new location', 'ok');
    closeModal();
    await loadLibrary();
  }));

  $$('.delrule').forEach((b) => b.addEventListener('click', async () => {
    try {
      await api(`/api/review/word?path=${encodeURIComponent(path)}`
        + `&word=${encodeURIComponent(b.dataset.w)}`, { method: 'DELETE' });
    } catch (err) {
      toast(`Could not remove the rule: ${err.message}`, 'error');
      return;
    }
    openHistory(path);
  }));
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
    // Say so when history was carried across — a silent re-key looks like nothing
    // happened, and this is the moment to notice it went to the right title.
    if (r._moved) {
      toast(`${r._moved} moved file${r._moved === 1 ? '' : 's'} reconnected to `
        + `${r._moved === 1 ? 'its' : 'their'} filtering history`, 'ok');
    }
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

/** Reopen the filter dialog seeded from a previous run, so its settings can be
 *  adjusted before re-running. A failed run re-run unchanged just fails the same way. */
async function editRun(runId) {
  try {
    const d = await api(`/api/runs/${runId}/options`);
    await openFilter(d.path, d.options);
    toast(`Loaded settings from run #${runId} — adjust and queue when ready.`, 'info');
  } catch (e) {
    toast(`Could not load run #${runId}: ${e.message}`, 'error');
  }
}

/** Open the filter dialog. `prefill` is a previous run's options, so a failed or
 *  earlier run can be reopened, adjusted and re-run rather than rebuilt by hand. */
async function openFilter(path, prefill = null) {
  modal.classList.remove('hidden');
  $('#mtitle').textContent = path.split(/[\\/]/).pop();
  $('#mbody').innerHTML = '<p class="muted">Probing…</p>';

  const t = await api(`/api/title?path=${encodeURIComponent(path)}`);
  const lossless = /hd ma|dts:x|truehd|atmos/i.test(t.audio_profile || '');
  // The backend already parses release filenames (titleparse.py, covered by tests), so
  // use its result rather than reimplementing the stop-token logic here and letting the
  // two drift apart.
  const searchTitle = t.parsed_title || t.name || '';
  // Previous run's options, with the same defaults a fresh dialog would use. Every
  // field reads from here so reopening a run reproduces it exactly before any edits.
  const pf = {
    quality: 'splice', model: 'small.en', do_scan: true, only_enabled: false,
    detect_nudity: false, nudity_start: null, nudity_end: null,
    trust_timestamps: false, auto_mute_words: [],
    categories: [], video_categories: [],
    audio_refs: [], video_refs: [], manual_mutes: [], manual_cuts: [],
    tag_set_id: null, videoskip_id: null, output_path: null, archive_path: null,
    ...(prefill || {}),
  };
  // A start of 0 with no end is the same as scanning everything, so only treat the run as
  // windowed when it actually narrowed something.
  const pfNudeWindow = (pf.nudity_start != null && pf.nudity_start > 0)
    || pf.nudity_end != null;
  const pfRefs = new Set([...(pf.audio_refs || []), ...(pf.video_refs || [])]);
  const pfCats = new Set([...(pf.categories || []), ...(pf.video_categories || [])]);
  // Reopening a run must re-tick the per-category "mute all" boxes it was launched with.
  const pfAuto = new Set((pf.auto_mute_words || [])
    .map((w) => String(w).trim().toLowerCase()).filter(Boolean));

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
      // A reopened run's own tag-set wins over the linked default, so reproducing it
      // does not silently switch source.
      const chosen = pf.tag_set_id != null
        ? x.tag_set_id === pf.tag_set_id
        : x.linked;
      return `<option value="${x.tag_set_id}"${chosen ? ' selected' : ''}
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
        <label class="row"><input type="radio" name="q" value="splice"
          ${pf.quality === 'splice' ? 'checked' : ''}>
          <span>Splice <code>— only muted frames re-encoded, size-neutral</code></span></label>
        <label class="row"><input type="radio" name="q" value="same"
          ${pf.quality === 'same' ? 'checked' : ''}>
          <span>Same codec <code>— whole track, inaudible loss</code></span></label>
        <label class="row"><input type="radio" name="q" value="lossless"
          ${pf.quality === 'lossless' ? 'checked' : ''}>
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
    <fieldset><legend>Timeline offset</legend>
      <p class="muted">If the tag-set is keyed to a different cut, every time in it is
        shifted. Words are found by listening, so audio survives that — but a video cut
        can't be located in the file, so it's refused unless the offset is known.
        Anchor it here: pick a credits marker, enter when it really starts in your file,
        and the difference is applied to every tagged range.</p>
      <div id="moffbody"><p class="muted">Pick a tag-set to set an offset.</p></div>
    </fieldset>
    <fieldset><legend>VideoSkip filter file</legend>
      <div class="toolbar">
        <select id="mvsk"><option value="">none</option></select>
        <span class="muted">A saved skip file can drive mutes and cuts on its own.</span>
      </div>
    </fieldset>
    <fieldset><legend>Word-list scan</legend>
      <label class="row"><input type="checkbox" id="mscan"
        ${pf.do_scan ? 'checked' : ''}>
        <span>Scan the whole track for word-list matches
          <code>— finds words VidAngel missed; hits await your review</code></span></label>
      <label class="row" style="align-items:flex-start">
        <span style="min-width:8.5rem">Mute without review</span>
        <span style="flex:1">
          <input type="text" id="mauto" placeholder="shit, fuck"
            value="${esc((pf.auto_mute_words || []).join(', '))}" style="width:100%">
          <code>— every match for these words is muted outright, no review queue.
            Leave empty to review each hit. Inflections are included
            (“fuck” covers “fucking”). A word you previously chose to skip stays
            skipped. The “mute all” boxes on the categories above write here.</code>
        </span></label>
      <label class="row"><input type="checkbox" id="monlyen"
        ${pf.only_enabled ? 'checked' : ''}>
        <span>Only tags already enabled in VidAngel
          <code>— use your existing selections instead of whole categories</code></span></label>
      <label class="row"><input type="checkbox" id="mtrust"
        ${pf.trust_timestamps ? 'checked' : ''}>
        <span>Trust the tag's timestamps — skip Whisper
          <code>— cuts the marked range as given; use for conversations, or when a
          word isn't being found</code></span></label>
      <label class="row"><input type="checkbox" id="mnude"
        ${pf.detect_nudity ? 'checked' : ''}>
        <span>Scan video for nudity
          <code>— candidates await your review, never cut automatically</code></span></label>
      <div id="mnudeopts" class="${pf.detect_nudity ? '' : 'hidden'}"
        style="margin:.35rem 0 .35rem 1.6rem">
        <label class="row">
          <span>Scan</span>
          <select id="mnudescope">
            <option value="all"${pfNudeWindow ? '' : ' selected'}>the whole film</option>
            <option value="window"${pfNudeWindow ? ' selected' : ''}
              >a time range only</option>
          </select>
        </label>
        <div id="mnudewin" class="hidden toolbar" style="margin-top:.35rem">
          <input id="mnudestart" placeholder="from (mm:ss)" style="max-width:11rem"
            value="${pfNudeWindow && pf.nudity_start != null ? tc(pf.nudity_start) : ''}">
          <input id="mnudeend" placeholder="to (mm:ss, blank = end)"
            style="max-width:13rem"
            value="${pf.nudity_end != null ? tc(pf.nudity_end) : ''}">
        </div>
        <p class="muted" style="margin:.3rem 0 0">Detection is the slowest part of a run.
          Scanning one range is much faster when you already know roughly where the scene
          is — anything outside it is left untouched.</p>
      </div>
      <label class="row">
        <span>Whisper model</span>
        <select id="mmodel">
          <option value="small.en"${pf.model === 'small.en' ? ' selected' : ''}
            >small.en — fast, verified</option>
          <option value="medium.en"${pf.model === 'medium.en' ? ' selected' : ''}
            >medium.en — slower, may catch more</option>
          <option value="base.en"${pf.model === 'base.en' ? ' selected' : ''}
            >base.en — fastest, least accurate</option>
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
    <fieldset><legend>Output paths</legend>
      <p class="muted">Leave blank for the defaults. The archive is written first and a
        run refuses to proceed without it — point it somewhere writable if your media
        share is read-only.</p>
      <label class="row"><span style="min-width:5.5rem">Filtered</span>
        <input id="mout" style="flex:1 1 24rem" spellcheck="false"
          value="${esc(pf.output_path || '')}" placeholder="replaces the source file (original goes to the archive)"></label>
      <label class="row"><span style="min-width:5.5rem">Archive</span>
        <input id="march" style="flex:1 1 24rem" spellcheck="false"
          value="${esc(pf.archive_path || '')}" placeholder="from the Settings template"></label>
    </fieldset>
    <div class="toolbar">
      <button id="mgo">Queue filter run</button>
      <span id="mmsg" class="muted"></span>
    </div>`;

  /* --- manual entry list ------------------------------------------------- */
  // Rehydrate a reopened run's manual entries. The wire format splits them into
  // manual_mutes/manual_cuts; the UI keeps one list tagged by kind.
  const manual = [
    ...(pf.manual_mutes || []).map((m) => (m.word != null && m.at != null
      ? { kind: 'word', word: m.word, at: m.at }
      : { kind: 'range', start: m.start, end: m.end })),
    ...(pf.manual_cuts || []).map((m) => (
      { kind: 'cut', start: m.start, end: m.end, snap: m.snap !== false })),
  ];
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

  // Nudity scope: the window inputs only mean anything when detection is on, so they
  // stay hidden until it is, and collapse again when it is switched off.
  const syncNudity = () => {
    const on = $('#mnude').checked;
    $('#mnudeopts').classList.toggle('hidden', !on);
    $('#mnudewin').classList.toggle('hidden',
      !on || $('#mnudescope').value !== 'window');
  };
  $('#mnude').addEventListener('change', syncNudity);
  $('#mnudescope').addEventListener('change', syncNudity);
  syncNudity();
  // Show any entries restored from a reopened run — renderManual() is otherwise only
  // called from the add/remove handlers, so a prefilled list would stay invisible.
  if (manual.length) renderManual();

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

  /* --- timeline offset from a credits anchor ------------------------------ */
  // The offset actually sent with the run. Null means "let the audio estimator decide",
  // which is the historical behaviour and still the default.
  let manualOffset = pf.manual_offset != null ? Number(pf.manual_offset) : null;

  // Structural markers are the anchors: opening/closing credits are hard boundaries the
  // user can read off the file directly, unlike a 6-second-bucketed word tag. The
  // tag-set endpoint already flags them (`structural`), so no new parsing is needed.
  const buildOffsetPanel = (ts, tsId) => {
    const box = $('#moffbody');
    if (!box) return;
    const anchors = ts.groups.flatMap((g) => g.incidents
      .filter((i) => i.structural)
      .map((i) => ({ ...i, title: g.title, key: g.key })));

    if (!anchors.length) {
      box.innerHTML = `<p class="muted">This tag-set has no opening/closing credits
        marker to anchor on. You can still enter an offset directly, or place video cuts
        by hand under “Manual filters”.</p>
        <label class="row"><span style="min-width:7rem">Offset</span>
          <input id="moffraw" style="max-width:11rem" placeholder="e.g. -1:33"
            value="${manualOffset != null ? tc(manualOffset) : ''}">
          <span class="muted">added to every tagged time; negative = your file runs
            earlier</span></label>
        <div id="moffout" class="muted" style="margin-top:.4rem"></div>`;
    } else {
      box.innerHTML = `
        <div class="anchorlist">
          ${anchors.map((a, i) => `
            <label class="row anchor-row">
              <input type="checkbox" class="anchorbox" data-i="${i}"
                     data-tagged="${a.start}">
              <span style="min-width:12rem">${esc(a.title)}</span>
              <span class="tcell">tagged ${tc(a.start)}</span>
              <span>→ really at
                <input class="anchorat" data-i="${i}" style="max-width:8rem"
                       placeholder="mm:ss"></span>
            </label>`).join('')}
        </div>
        <div id="moffout" class="muted" style="margin-top:.5rem"></div>`;
    }

    const out = $('#moffout');
    // Recompute locally on every keystroke. The server endpoint does the same arithmetic
    // and is what persists the value, but a round-trip per keystroke would make the
    // readout lag behind the typing that produced it.
    const recompute = () => {
      const raw = $('#moffraw');
      if (raw) {
        const v = raw.value.trim();
        manualOffset = v ? parseTime(v) : null;
        out.innerHTML = manualOffset == null
          ? 'No offset — the audio estimate will be used.'
          : `<span class="pill ok">offset ${manualOffset >= 0 ? '+' : ''}${
            manualOffset.toFixed(2)}s</span> applied to every tagged range.`;
        return;
      }
      const used = $$('.anchorbox').filter((b) => b.checked).map((b) => {
        const at = parseTime($(`.anchorat[data-i="${b.dataset.i}"]`).value);
        return at == null ? null : {
          label: anchors[Number(b.dataset.i)].title,
          tagged: Number(b.dataset.tagged),
          offset: at - Number(b.dataset.tagged),
        };
      }).filter(Boolean);

      if (!used.length) {
        manualOffset = null;
        out.textContent = 'Tick a marker and enter its real time to set the offset. '
          + 'Without one, the audio estimate is used — which is refused for video cuts '
          + 'when the tags disagree.';
        return;
      }
      const offs = used.map((u) => u.offset);
      const spread = Math.max(...offs) - Math.min(...offs);
      manualOffset = offs.reduce((a, b) => a + b, 0) / offs.length;
      const detail = used.map((u) =>
        `${esc(u.label)} ${u.offset >= 0 ? '+' : ''}${u.offset.toFixed(1)}s`).join(', ');
      // Mirrors ANCHOR_DISAGREE_TOLERANCE on the server. Two anchors this far apart mean
      // the drift is not constant, so no single offset fits the whole title — the same
      // condition that defeats the audio estimator. Say so rather than quietly averaging.
      out.innerHTML = spread > 5
        ? `<span class="pill warn">anchors disagree by ${spread.toFixed(1)}s</span>
           ${detail}. Drift isn't constant across this title, so no single offset fits.
           Using the average (${manualOffset >= 0 ? '+' : ''}${manualOffset.toFixed(1)}s);
           a cut far from either marker may be off by up to ${(spread / 2).toFixed(1)}s.
           For accuracy, anchor on the marker nearest the cut.`
        : `<span class="pill ok">offset ${manualOffset >= 0 ? '+' : ''}${
          manualOffset.toFixed(2)}s</span> from ${detail}. Applied to every tagged range,
          and video cuts will be allowed.`;
    };

    // Bound on the freshly-written subtree, not on `#moffbody` itself: that element
    // survives a tag-set switch (only its innerHTML is replaced), so listeners attached
    // to it would accumulate one copy per switch.
    box.querySelectorAll('input').forEach((el) => {
      el.addEventListener('input', recompute);
      el.addEventListener('change', recompute);
    });

    // Rehydrate a saved offset, so reopening a title shows what it is already using
    // rather than looking unset.
    if (manualOffset != null && anchors.length) {
      out.innerHTML = `<span class="pill ok">offset ${manualOffset >= 0 ? '+' : ''}${
        manualOffset.toFixed(2)}s</span> saved for this title. Tick a marker above to
        re-measure it.`;
    } else {
      recompute();
    }
  };

  const loadGroups = async (id) => {
    if (!id) {
      $('#mgroups').innerHTML = groupsHtml;
      $('#moffbody').innerHTML = '<p class="muted">Pick a tag-set to set an offset.</p>';
      return;
    }
    $('#mgroups').innerHTML = '<p class="muted">loading categories…</p>';
    const ts = await api(`/api/tagsets/${id}`);
    // A saved offset only applies to the tag-set it was measured against — switching
    // tag-sets must not silently carry a correction derived from another cut. So this
    // always resolves to a value for *this* tag-set, and clears to null when there is
    // none: an early return on a missing saved offset would leave the previous
    // tag-set's number in place, which is the exact mistake the feature exists to stop.
    if (pf.manual_offset != null && Number(id) === pf.tag_set_id) {
      // Reopening the run that set it: the run's own value wins over the stored one.
      manualOffset = Number(pf.manual_offset);
    } else {
      const saved = await api(
        `/api/titles/offset?path=${encodeURIComponent(path)}&tag_set_id=${id}`)
        .catch(() => ({ offset: null }));
      manualOffset = saved.offset != null ? Number(saved.offset) : null;
    }
    buildOffsetPanel(ts, id);
    // Each incident is listed individually with its own description and timestamp.
    // A category checkbox alone hides the fact that "Sexually Suggestive" might be one
    // scene worth cutting and two worth keeping — the descriptions are the only way to
    // tell, and they are the whole reason for choosing per incident.
    // "Mute every instance" strip, one control per distinct word rather than per
    // category. Categories are the wrong unit for this: VidAngel's "god" category
    // lists god/goddamn/jesus/christ together, and several categories name the same
    // word, so a per-category control would both over-reach and double up. The word
    // is what the scan actually matches on, so it is what the user chooses.
    //
    // Only locatable audio categories contribute — a video cut has nothing to hear and
    // an unlocatable category ("other_sexual") names an action, not a word.
    const allWords = [...new Set(ts.groups
      .filter((g) => g.kind !== 'audiovisual' && g.locatable)
      .flatMap((g) => g.incidents.flatMap((i) => i.words || []))
      .map((w) => String(w).trim().toLowerCase())
      .filter(Boolean))].sort();
    // Count distinct incidents, not mentions: one "goddamn" tag is listed under both the
    // "damn" and "god" categories, so summing across groups would report it twice and
    // overstate how much VidAngel already covers.
    const tagged = (w) => new Set(ts.groups.flatMap((g) => g.incidents
      .filter((i) => (i.words || []).some((x) => String(x).toLowerCase() === w))
      .map((i) => i.ref_id))).size;
    const muteAllHtml = allWords.length ? `
      <div class="muteall-strip">
        <div class="muteall-head">Mute every instance, no review
          <code>— catches what VidAngel did not tag. Inflections included; a hit you
          previously skipped stays skipped.</code></div>
        <div class="muteall-words">
          ${allWords.map((w) => `
            <label class="muteall" title="Mute every &quot;${esc(w)}&quot; in the episode — VidAngel tagged ${tagged(w)}">
              <input type="checkbox" class="allbox" data-words="${esc(w)}"
                     ${pfAuto.has(w) ? 'checked' : ''}>
              <span>${esc(w)} <code>${tagged(w)}</code></span>
            </label>`).join('')}
        </div>
      </div>` : '';

    $('#mgroups').innerHTML = muteAllHtml + ts.groups.map((g, gi) => {
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
                     data-kind="${i.kind === 'audiovisual' ? 'video' : 'audio'}"
                     ${pfRefs.has(String(i.ref_id))
                       || (!pfRefs.size && pfCats.has(g.key)) ? 'checked' : ''}>
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
    const syncCat = (group) => {
      const peers = $$(`.incbox[data-group="${group}"]`);
      const cb = $(`.catbox[data-group="${group}"]`);
      if (!cb || !peers.length) return;
      cb.checked = peers.every((p) => p.checked);
      cb.indeterminate = !cb.checked && peers.some((p) => p.checked);
    };
    $$('.incbox').forEach((ib) =>
      ib.addEventListener('change', () => syncCat(ib.dataset.group)));

    // Reflect any restored selection, and open the groups holding it so a reopened
    // run shows what it picked instead of hiding it behind a collapsed summary.
    $$('.catbox').forEach((cb) => {
      syncCat(cb.dataset.group);
      if (cb.checked || cb.indeterminate) {
        cb.closest('.catgroup')?.setAttribute('open', '');
      }
    });

    // "mute all" writes through to the same free-text field the run already reads, so
    // there is one source of truth at submit time. Ticking the box is a shortcut for
    // typing the word there — not a second, competing setting that could disagree.
    const wordsOf = (box) => (box.dataset.words || '').split(',').filter(Boolean);
    const syncAutoField = () => {
      const field = $('#mauto');
      if (!field) return;
      // Preserve anything typed by hand that no checkbox owns, so toggling a box never
      // silently deletes a word the user entered themselves.
      const owned = new Set($$('.allbox').flatMap(wordsOf));
      const typed = field.value.split(/[,\s]+/).map((w) => w.trim().toLowerCase())
        .filter((w) => w && !owned.has(w));
      const ticked = $$('.allbox').filter((b) => b.checked).flatMap(wordsOf);
      field.value = [...new Set([...typed, ...ticked])].join(', ');
    };
    $$('.allbox').forEach((b) => b.addEventListener('change', syncAutoField));
    // Typing in the field re-ticks any box whose words are all present, keeping the two
    // views consistent in both directions.
    $('#mauto')?.addEventListener('input', () => {
      const typed = new Set($('#mauto').value.split(/[,\s]+/)
        .map((w) => w.trim().toLowerCase()).filter(Boolean));
      $$('.allbox').forEach((b) => {
        const w = wordsOf(b);
        b.checked = w.length > 0 && w.every((x) => typed.has(x));
      });
    });
  };

  // Saved skip files, offered as an alternative source to a VidAngel tag-set. Loaded
  // after render so a failure here cannot block the rest of the dialog.
  api('/api/skipfiles').then((d) => {
    const sel = $('#mvsk');
    if (!sel) return;
    sel.innerHTML = '<option value="">none</option>' + d.skipfiles.map((s) =>
      `<option value="${s.id}"${s.id === pf.videoskip_id ? ' selected' : ''}
        >#${s.id} ${esc(s.title_hint || s.format)}
        — ${s.audio_count} audio, ${s.video_count} video</option>`).join('');
  }).catch(() => { /* leave the "none" option in place */ });

  $('#mts').addEventListener('change', (e) => loadGroups(e.target.value));
  // A preselected <option> fires no change event, so load the linked tag-set's
  // categories explicitly — otherwise the list sits empty on a manually-picked match.
  await loadGroups($('#mts').value);

  $('#mgo').addEventListener('click', async (e) => {
    // Resolve the nudity window before disabling the button, so a bad time leaves the
    // form usable instead of stranding it mid-submit.
    let nudeStart = null;
    let nudeEnd = null;
    if ($('#mnude').checked && $('#mnudescope').value === 'window') {
      const raw = $('#mnudestart').value.trim();
      nudeStart = raw ? parseTime(raw) : 0;
      if (nudeStart == null) {
        toast('Enter the scan start as mm:ss or seconds.', 'warn');
        return;
      }
      const rawEnd = $('#mnudeend').value.trim();
      if (rawEnd) {
        nudeEnd = parseTime(rawEnd);
        if (nudeEnd == null) {
          toast('Enter the scan end as mm:ss or seconds, or leave it blank.', 'warn');
          return;
        }
        if (nudeEnd <= nudeStart) {
          toast('The scan range must end after it starts.', 'warn');
          return;
        }
      }
    }
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
        // A hand-measured anchor. Null means the audio estimator decides, as before.
        manual_offset: manualOffset,
        do_scan: $('#mscan').checked,
        // Free text, so split on commas/whitespace and drop the empties a trailing
        // comma leaves behind.
        auto_mute_words: ($('#mauto')?.value || '')
          .split(/[,\s]+/).map((w) => w.trim().toLowerCase()).filter(Boolean),
        only_enabled: $('#monlyen').checked,
        detect_nudity: $('#mnude').checked,
        trust_timestamps: $('#mtrust').checked,
        nudity_start: nudeStart,
        nudity_end: nudeEnd,
        model: $('#mmodel').value,
        // Blank means "use the default", so send null rather than an empty string.
        output_path: $('#mout').value.trim() || null,
        archive_path: $('#march').value.trim() || null,
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

/* ------------------------------------------------------------------- live */
let livePoll = null;
//: Which run's log is expanded, and how far its <pre> was scrolled, so a refresh does
//: not collapse the panel or yank the view away from what is being read.
const liveOpen = new Set();

const humanAge = (s) => {
  if (s == null) return '—';
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
};

/** Cancel a run. Confirms first when it is already running, since that throws away
 *  however long it has been going — a full-film pass is over an hour. Returns whether
 *  the request went through, so callers can decide whether to refresh. */
async function cancelRun(id, status) {
  if (status === 'running') {
    const ok = await confirmDialog(
      `Stop run #${id}?`,
      'The work done so far is lost — nothing resumes, so re-running starts from the '
      + 'beginning. Your media file is not modified: the filtered copy is only swapped '
      + 'in once it is complete.',
      { okText: 'Stop the run', danger: true },
    );
    if (!ok) return false;
  }
  try {
    const r = await postJSON(`/api/runs/${id}/cancel`, {});
    toast(r.stopped ? `Run #${id} cancelled`
      : `Run #${id} is stopping — this can take a few seconds`);
    return true;
  } catch (e) {
    toast(e.message, 'error');
    return false;
  }
}

async function loadLive() {
  let d;
  try {
    d = await api('/api/runs/live');
  } catch (e) {
    $('#livestatus').innerHTML = `<span class="pill bad">${esc(e.message)}</span>`;
    return;
  }

  const bits = [];
  bits.push(d.worker_alive
    ? '<span class="pill ok">worker running</span>'
    : '<span class="pill bad">worker NOT running</span>');
  bits.push(`<span class="muted">${d.running} running · ${d.queued} queued</span>`);
  $('#livestatus').innerHTML = bits.join(' ');

  if (!d.runs.length) {
    $('#livebody').innerHTML =
      '<p class="muted">Nothing active. Finished runs stay here for 30 minutes.</p>';
    return;
  }

  // Preserve scroll position of any open log before re-rendering.
  const scrolls = {};
  $$('.livelog').forEach((el) => {
    scrolls[el.dataset.id] = {
      top: el.scrollTop,
      pinned: el.scrollHeight - el.scrollTop - el.clientHeight < 40,
    };
  });

  // Newest-first is right for finished runs but backwards for the queue: the job about
  // to run should read first. Queued runs are lifted out and shown in queue order, ahead
  // of everything else, so "what runs next" is the top of the list rather than something
  // to work out from the id order.
  const queued = d.runs.filter((r) => r.status === 'queued')
    .sort((a, b) => (a.queue_position || 0) - (b.queue_position || 0));
  const rest = d.runs.filter((r) => r.status !== 'queued');
  const ordered = [...rest.filter((r) => r.status === 'running'), ...queued,
    ...rest.filter((r) => r.status !== 'running')];

  $('#livebody').innerHTML = ordered.map((r) => {
    const pct = Math.round(r.progress);
    const cls = { done: 'ok', failed: 'bad', running: 'warn' }[r.status] || '';
    const open = liveOpen.has(String(r.id));
    // A stalled run is the case this page exists for: same stage, same percentage,
    // no heartbeat. Say so explicitly rather than leaving it to be inferred.
    const stall = r.stalled
      ? `<span class="pill bad" title="No log or stage change for over
           ${Math.round(d.stall_seconds / 60)} minutes">possibly stuck —
           quiet ${humanAge(r.seconds_since_heartbeat)}</span>`
      : (r.status === 'running'
          ? `<span class="muted">last activity ${humanAge(r.seconds_since_heartbeat)} ago</span>`
          : '');
    // Queue controls, only for runs that have not started. A running job is mid-write in
    // ffmpeg/Whisper and cannot be displaced, so it gets no buttons — the same reason it
    // cannot be cancelled.
    const q = r.status === 'queued' && r.queue_position
      ? `<span class="muted" title="Position in the queue">#${r.queue_position} of
           ${r.queue_length} in line</span>
         <button class="secondary qmove" data-id="${r.id}" data-to="front"
           title="Run this next" ${r.queue_position === 1 ? 'disabled' : ''}>⇈ Next</button>
         <button class="secondary qmove" data-id="${r.id}" data-to="up"
           title="Move up one place" ${r.queue_position === 1 ? 'disabled' : ''}>↑</button>
         <button class="secondary qmove" data-id="${r.id}" data-to="down"
           title="Move down one place"
           ${r.queue_position === r.queue_length ? 'disabled' : ''}>↓</button>`
      : '';
    return `<fieldset class="liverun">
      <legend>#${r.id} — ${esc(r.name)}</legend>
      <div class="toolbar">
        <span class="pill ${cls}">${esc(r.status)}</span>
        <span>${esc(r.stage || '')}</span>
        <div class="bar" style="width:160px"><i style="width:${pct}%"></i></div>
        <span class="muted">${pct}%</span>
        ${r.elapsed != null
          ? `<span class="muted">running ${humanAge(r.elapsed)}</span>` : ''}
        ${stall}
        ${q}
        ${['queued', 'running'].includes(r.status)
          ? `<button class="secondary livecancel" data-id="${r.id}"
               data-status="${r.status}"
               ${r.stage === 'cancelling' ? 'disabled' : ''}
               title="${r.status === 'running'
                 ? 'Stop this run — the work so far is lost, the media file is untouched'
                 : 'Remove this job from the queue'}"
               >${r.stage === 'cancelling' ? 'stopping…'
                 : r.status === 'running' ? 'Stop' : 'Cancel'}</button>`
          : ''}
        <button class="secondary livetoggle" data-id="${r.id}"
          style="margin-left:auto">${open ? 'Hide log' : 'Show log'}</button>
        <button class="secondary livecopy" data-id="${r.id}"
          title="Copy the full log to the clipboard">Copy log</button>
      </div>
      ${r.error ? `<div class="pill bad" style="display:block;white-space:normal;
        padding:.5rem;margin-top:.5rem">${esc(r.error.join(' '))}</div>` : ''}
      <pre class="log livelog ${open ? '' : 'hidden'}" data-id="${r.id}"
        >${esc(r.log_tail.join('\n')) || '(no output yet)'}</pre>
      ${open && r.log_lines > r.log_tail.length
        ? `<div class="muted">showing the last ${r.log_tail.length} of
             ${r.log_lines} lines</div>` : ''}
    </fieldset>`;
  }).join('');

  $$('.livetoggle').forEach((b) => b.addEventListener('click', () => {
    const id = String(b.dataset.id);
    if (liveOpen.has(id)) liveOpen.delete(id); else liveOpen.add(id);
    loadLive();
  }));

  $$('#livebody .livecancel').forEach((b) => b.addEventListener('click', async () => {
    b.disabled = true;
    const ok = await cancelRun(b.dataset.id, b.dataset.status);
    if (!ok) b.disabled = false;
    loadLive();
  }));

  // Scoped to the live view: the runs table renders the same buttons and binds its own
  // handler, and an unscoped selector would bind both to every button.
  $$('#livebody .qmove').forEach((b) => b.addEventListener('click', async () => {
    b.disabled = true;
    try {
      const r = await postJSON(`/api/runs/${b.dataset.id}/move`,
        { to: b.dataset.to });
      await loadLive();
      toast(`Run #${b.dataset.id} is now #${r.position} of ${r.order.length} in line`);
    } catch (e) {
      b.disabled = false;
      // A 409 here is the normal race, not a bug: the queue moved on between the page
      // rendering and the click, usually because the job started. Re-poll so the buttons
      // match reality rather than leaving a stale row on screen.
      toast(e.message, 'error');
      loadLive();
    }
  }));

  $$('.livecopy').forEach((b) => b.addEventListener('click', async () => {
    const id = b.dataset.id;
    const original = b.textContent;
    b.disabled = true;
    b.textContent = 'copying…';
    try {
      // Fetch the whole log rather than copying the visible <pre>: the live view only
      // renders the last 60 lines, so copying the element would silently truncate
      // exactly when the log is long enough to be worth sharing.
      const d = await api(`/api/runs/${id}/log`);
      const text = d.lines.join('\n');
      await copyText(text);
      b.textContent = `copied ${d.total} lines`;
      setTimeout(() => { b.textContent = original; b.disabled = false; }, 1800);
    } catch (e) {
      b.textContent = original;
      b.disabled = false;
      toast(`Could not copy log: ${e.message}`, 'error');
    }
  }));

  // Restore scroll; if the user was at the bottom, keep them pinned to the newest line.
  $$('.livelog').forEach((el) => {
    const s = scrolls[el.dataset.id];
    if (!s) { el.scrollTop = el.scrollHeight; return; }
    el.scrollTop = s.pinned ? el.scrollHeight : s.top;
  });
}

function startLive() {
  loadLive();
  if (livePoll) clearInterval(livePoll);
  livePoll = setInterval(() => {
    if ($('#view-live').classList.contains('hidden') || !$('#liveauto').checked) return;
    loadLive();
  }, 2000);
}
$('#liveauto').addEventListener('change', (e) => {
  if (e.target.checked) loadLive();
});

/* ------------------------------------------------------------------- runs */
async function loadRuns() {
  const d = await api('/api/runs');
  $('#runs tbody').innerHTML = d.runs.map((r) => {
    const cls = { done: 'ok', failed: 'bad', running: 'warn' }[r.status] || '';
    const pct = Math.round(r.progress || 0);
    return `<tr>
      <td class="num">${r.id}</td>
      <td class="name" title="${esc(r.path)}">${esc(r.path.split(/[\\/]/).pop())}</td>
      <td><span class="pill ${cls}">${esc(r.status)}</span>
        ${r.queue_position
          ? `<span class="muted" title="Position in the queue">${r.queue_position}/${r.queue_length}</span>`
          : ''}</td>
      <td class="muted">${esc(r.stage || '')}</td>
      <td><div class="bar"><i style="width:${pct}%"></i></div></td>
      <td><button class="secondary rundet" data-id="${r.id}">Details</button>
        ${r.status === 'queued'
          ? `<button class="secondary qmove" data-id="${r.id}" data-to="front"
               title="Run this next"
               ${r.queue_position === 1 ? 'disabled' : ''}>⇈ Next</button>
             <button class="secondary qmove" data-id="${r.id}" data-to="up"
               title="Move up one place"
               ${r.queue_position === 1 ? 'disabled' : ''}>↑</button>
             <button class="secondary qmove" data-id="${r.id}" data-to="down"
               title="Move down one place"
               ${r.queue_position === r.queue_length ? 'disabled' : ''}>↓</button>
             <button class="secondary runcancel" data-id="${r.id}"
               data-status="queued">Cancel</button>` : ''}
        ${r.status === 'running'
          ? `<button class="secondary runcancel" data-id="${r.id}" data-status="running"
               ${r.stage === 'cancelling' ? 'disabled' : ''}
               title="Stop this run — the work so far is lost, the media file is untouched"
               >${r.stage === 'cancelling' ? 'stopping…' : 'Stop'}</button>`
          : ''}
        ${['done', 'failed', 'cancelled'].includes(r.status)
          ? `<button class="secondary runedit" data-id="${r.id}"
               title="Reopen these settings to adjust and re-run">Edit &amp; re-run</button>`
          : ''}
      </td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" class="muted">No runs yet.</td></tr>';

  $$('.rundet').forEach((b) => b.addEventListener('click', () => showRun(b.dataset.id)));
  $$('.runedit').forEach((b) => b.addEventListener('click', () => editRun(b.dataset.id)));
  $$('#runs .runcancel').forEach((b) => b.addEventListener('click', async () => {
    b.disabled = true;
    const ok = await cancelRun(b.dataset.id, b.dataset.status);
    if (!ok) b.disabled = false;
    loadRuns();
  }));
  // Re-prioritise a pending run. Same handler shape as the live view; this table is not
  // auto-refreshed, so it reloads explicitly.
  $$('#runs .qmove').forEach((b) => b.addEventListener('click', async () => {
    b.disabled = true;
    try {
      const r = await postJSON(`/api/runs/${b.dataset.id}/move`, { to: b.dataset.to });
      toast(`Run #${b.dataset.id} is now #${r.position} of ${r.order.length} in line`);
    } catch (e) {
      toast(e.message, 'error');
    }
    loadRuns();
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
    ${r.error ? `<fieldset><legend>Failure</legend>
      <pre class="log">${esc(r.error)}</pre>
      <div class="toolbar"><button id="editrun" data-id="${r.id}">Edit settings
        &amp; re-run</button>
        <span class="muted">Re-running unchanged will fail the same way.</span></div>
      </fieldset>` : `<div class="toolbar" style="margin-bottom:.8rem">
        <button class="secondary" id="editrun" data-id="${r.id}">Edit settings
          &amp; re-run</button></div>`}
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
      <p class="muted">Words found in the audio that no tag covered. Listen, then choose.
        Shift-click a second checkbox to select everything between.</p>
      ${groupHits(pending).map((g) => `
        <div class="wordgroup" data-word="${esc(g.word)}">
          <div class="wordhead">
            <label><input type="checkbox" class="gall" data-word="${esc(g.word)}">
              <b>${esc(g.word)}</b> <span class="muted">×${g.hits.length}</span></label>
            <span class="spacer"></span>
            <button class="secondary gmute" data-word="${esc(g.word)}">Mute all
              ${g.hits.length}</button>
            <button class="secondary gskip" data-word="${esc(g.word)}">Skip all</button>
            <button class="secondary grule" data-word="${esc(g.word)}"
              title="Mute every instance of this word in this title, now and on any future run — including ones no scan has found yet">Always mute “${esc(g.word)}”</button>
          </div>
          <div class="hits">${g.hits.map((h) => `
            <div class="hit" data-i="${h.i}">
              <label class="pick"><input type="checkbox" class="hsel" data-i="${h.i}"
                data-word="${esc(g.word)}"></label>
              <div class="hbody">
                <div class="ctx">${tc(h.at)} — ${esc(h.context).replace(
                  new RegExp(`\\b(${h.word})\\b`, 'i'), '<b>$1</b>')}</div>
                <div class="actions">
                  <audio controls preload="none"
                    src="/api/clip?path=${encodeURIComponent(r.path)}&start=${h.at}&end=${h.end}"></audio>
                  <button class="dec" data-a="mute" data-t="${h.at}" data-w="${esc(h.word)}">Mute this</button>
                  <button class="dec secondary" data-a="skip" data-t="${h.at}" data-w="${esc(h.word)}">Skip</button>
                  <span class="muted">p=${h.confidence}</span>
                </div>
              </div>
            </div>`).join('')}</div>
        </div>`).join('')}
      <div class="toolbar" style="margin-top:.6rem">
        <button class="secondary" id="selmute" disabled>Mute selected
          (<span id="selcount">0</span>)</button>
        <button class="secondary" id="selskip" disabled>Skip selected</button>
      </div>
      <div class="toolbar" style="margin-top:.6rem">
        <button id="rerun">Re-run to apply decisions</button>
        <span class="muted">A mute has to be located and rendered, so applying
          decisions needs another pass.</span>
      </div>
      </fieldset>` : ''}
    ${rep.render ? `<fieldset><legend>Render</legend>
      <div>${esc(rep.render.summary || '')}</div></fieldset>` : ''}
    <fieldset><legend>Log</legend>
      <div class="toolbar">
        <button class="secondary" id="copylog" data-id="${r.id}">Copy log</button>
      </div>
      <pre class="log">${esc(r.log || '')}</pre></fieldset>`;

  $('#editrun')?.addEventListener('click', (e) => editRun(e.target.dataset.id));

  $('#copylog')?.addEventListener('click', async (e) => {
    const b = e.target;
    const original = b.textContent;
    b.disabled = true;
    try {
      await copyText(r.log || '');
      b.textContent = `copied ${(r.log || '').trim().split('\n').length} lines`;
      setTimeout(() => { b.textContent = original; b.disabled = false; }, 1800);
    } catch (err) {
      b.textContent = original;
      b.disabled = false;
      toast(`Could not copy log: ${err.message}`, 'error');
    }
  });

  // Mark a hit as decided: dim it, disable its controls, untick it so it drops out of
  // the selection count.
  const settle = (el, action) => {
    if (!el || el.dataset.done) return;
    el.dataset.done = action;
    el.style.opacity = .4;
    $$('button', el).forEach((x) => { x.disabled = true; });
    const box = $('.hsel', el);
    if (box) { box.checked = false; box.disabled = true; }
  };

  $$('.dec').forEach((b) => b.addEventListener('click', async () => {
    try {
      await postJSON('/api/review', {
        path: r.path, at_time: Number(b.dataset.t), word: b.dataset.w,
        action: b.dataset.a,
      });
    } catch (err) {
      toast(`Could not save: ${err.message}`, 'error');
      return;
    }
    const hit = b.closest('.hit');
    b.textContent = b.dataset.a === 'mute' ? 'will mute' : 'skipped';
    settle(hit, b.dataset.a);
    refreshSel();
  }));

  // ---- selection ----------------------------------------------------------
  // `pending` is the run's own array and its index is the hit id, so a selection maps
  // straight back to the timestamps the decision rows need.
  const boxes = () => $$('.hsel').filter((b) => !b.disabled);
  const selected = () => boxes().filter((b) => b.checked)
    .map((b) => pending[Number(b.dataset.i)]);

  function refreshSel() {
    const n = selected().length;
    $('#selcount').textContent = n;
    $('#selmute').disabled = !n;
    $('#selskip').disabled = !n;
    // Group headers reflect their own children, not the whole list.
    $$('.gall').forEach((g) => {
      const kids = $$(`.hsel[data-word="${cssEsc(g.dataset.word)}"]`)
        .filter((b) => !b.disabled);
      const on = kids.filter((b) => b.checked).length;
      g.checked = kids.length > 0 && on === kids.length;
      g.indeterminate = on > 0 && on < kids.length;
    });
  }

  // Shift-click extends from the last clicked box, so a run of adjacent hits — the
  // "fuck, fuck fuck" case — is three clicks rather than one per word.
  let anchor = null;
  $$('.hsel').forEach((b) => b.addEventListener('click', (e) => {
    const all = boxes();
    if (e.shiftKey && anchor !== null) {
      const a = all.indexOf(anchor);
      const z = all.indexOf(b);
      if (a > -1 && z > -1) {
        const [lo, hi] = a < z ? [a, z] : [z, a];
        for (let k = lo; k <= hi; k++) all[k].checked = b.checked;
      }
    }
    anchor = b;
    refreshSel();
  }));

  $$('.gall').forEach((g) => g.addEventListener('change', () => {
    $$(`.hsel[data-word="${cssEsc(g.dataset.word)}"]`)
      .filter((b) => !b.disabled)
      .forEach((b) => { b.checked = g.checked; });
    refreshSel();
  }));

  // Bulk-decide a set of hits in one request, then settle their rows.
  async function decideMany(hits, action) {
    if (!hits.length) return;
    try {
      await postJSON('/api/review/bulk', {
        path: r.path, action,
        hits: hits.map((h) => ({ at_time: h.at, word: h.word })),
      });
    } catch (err) {
      toast(`Could not save: ${err.message}`, 'error');
      return;
    }
    hits.forEach((h) => settle($(`.hit[data-i="${h.i}"]`), action));
    refreshSel();
    toast(`${action === 'mute' ? 'Muting' : 'Skipping'} ${hits.length} hit${
      hits.length === 1 ? '' : 's'}`);
  }

  const groupHitsOf = (w) => $$(`.hsel[data-word="${cssEsc(w)}"]`)
    .filter((b) => !b.disabled)
    .map((b) => pending[Number(b.dataset.i)]);

  $$('.gmute').forEach((b) => b.addEventListener('click',
    () => decideMany(groupHitsOf(b.dataset.word), 'mute')));
  $$('.gskip').forEach((b) => b.addEventListener('click',
    () => decideMany(groupHitsOf(b.dataset.word), 'skip')));

  $('#selmute')?.addEventListener('click', () => decideMany(selected(), 'mute'));
  $('#selskip')?.addEventListener('click', () => decideMany(selected(), 'skip'));

  // A standing rule, as opposed to a decision about the hits on screen: it also covers
  // instances this scan never found, so the word stops coming back for review at all.
  $$('.grule').forEach((b) => b.addEventListener('click', async () => {
    const w = b.dataset.word;
    try {
      await postJSON('/api/review/word', { path: r.path, word: w, action: 'mute' });
    } catch (err) {
      toast(`Could not save the rule: ${err.message}`, 'error');
      return;
    }
    groupHitsOf(w).forEach((h) => settle($(`.hit[data-i="${h.i}"]`), 'mute'));
    b.textContent = `always muting “${w}”`;
    b.disabled = true;
    refreshSel();
    toast(`Every “${w}” in this title will be muted from now on`);
  }));

  refreshSel();

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

$('#vadologin').addEventListener('click', async (e) => {
  const username = $('#valogin').value.trim();
  const password = $('#vapass').value;
  if (!username || !password) {
    toast('Enter your VidAngel email and password.', 'warn');
    return;
  }
  e.target.disabled = true;
  e.target.textContent = 'logging in…';
  try {
    await postJSON('/api/vidangel/login', { username, password });
    // Clear the password from the DOM immediately — it is not needed again, and the
    // token is now saved server-side.
    $('#vapass').value = '';
    toast('Logged in — token saved.', 'ok');
    await loadTagsets();
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    e.target.disabled = false;
    e.target.textContent = 'Log in & get token';
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
