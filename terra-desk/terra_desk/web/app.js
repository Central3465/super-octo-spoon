// terra-desk's page. It talks to the server on this computer (server.py); everything it shows comes from there.
'use strict';

const KEY = document.querySelector('meta[name=desk-key]').content;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (x) => String(x ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'html') el.innerHTML = v;
    else if (k === 'class') el.className = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid.nodeType ? kid : String(kid));
  return el;
};

const COLORS = { drafted: 'var(--green)', notes: 'var(--butter)', new: 'var(--muted)', queued: 'var(--lilac)', sent: 'var(--faint)',
  gone: 'var(--faint)', claimed: 'var(--pink)' };
const CHECK = { ok: ['✓', 'var(--green)'], warn: ['⚠', 'var(--butter)'], bad: ['✗', 'var(--pink)'], running: ['…', 'var(--muted)'] };
const TINTS = ['var(--t0)', 'var(--t1)', 'var(--t2)', 'var(--t3)', 'var(--t4)', 'var(--t5)'];
const PLATFORMS = ['web', 'windows', 'linux', 'macos', 'other'];
const WEEK_ORDER = { 'Prep Week 3': 0, 'Prep Week 2': 1, 'Week 1': 2, 'Week 2': 3, 'Week 3': 4 };
const STATE_ORDER = { drafted: 0, notes: 1, new: 2, queued: 3, sent: 4, claimed: 5, gone: 6 };

const S = {
  rows: [], columns: [], version: -1, cur: null, detail: null, rev: -1, since: 0, platforms: new Set(),
  jobs: [], job: null, dirty: {}, notesTimer: null, notesFor: null, tab: 0, lastJobsSeen: 0,
};

// ---- talking to the server ----
async function api(method, path, body, raw) {
  const opts = { method, headers: { 'X-Desk-Key': KEY } };
  if (raw) { opts.body = raw.body; Object.assign(opts.headers, raw.headers); }
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers['Content-Type'] = 'application/json'; }
  let res;
  try { res = await fetch(path, opts); } catch (e) { say('terra-desk is not answering (was it closed?)'); throw e; }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { const err = new Error(data.error || res.statusText); err.data = data; throw err; }
  return data;
}
const post = (path, body = {}) => api('POST', path, body);
async function act(path, body, okMsg) {
  try { const r = await post(path, body); if (r.msg || okMsg) say(r.msg || okMsg); return r; } catch (e) { say(e.message, true); return null; }
}

let sayTimer = null;
function say(msg, important) {
  const el = $('#say');
  el.textContent = msg;
  el.title = msg;
  el.classList.add('fresh');
  clearTimeout(sayTimer);
  sayTimer = setTimeout(() => el.classList.remove('fresh'), important ? 15000 : 8000);
}

function ask(text, { input = null, yes = 'OK' } = {}) {
  return new Promise((resolve) => {
    const d = $('#ask');
    $('#ask-text').textContent = text;
    const inp = $('#ask-input');
    inp.hidden = input === null;
    inp.value = input || '';
    $('#ask-yes').textContent = yes;
    const done = (v) => { d.close(); $('#ask-yes').onclick = $('#ask-no').onclick = null; resolve(v); };
    $('#ask-yes').onclick = () => done(input === null ? true : inp.value);
    $('#ask-no').onclick = () => done(input === null ? false : null);
    d.onclose = () => resolve(input === null ? false : null);
    inp.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); done(inp.value); } };
    d.showModal();
    (input === null ? $('#ask-yes') : inp).focus();
  });
}

// ---- the state poll ----
async function poll() {
  try {
    const st = await api('GET', `/api/state?since=${S.since}`);
    for (const m of st.messages) { S.since = m.id; say(m.text); }
    for (const ev of st.events) {
      if (ev.copy !== undefined) navigator.clipboard?.writeText(ev.copy).catch(() => {});
      if (ev.tab !== undefined) showTab(ev.tab);
    }
    showStatus(st.status);
    showJobsList(st.jobs);
    if (st.version !== S.version) await loadShips();
    if (S.cur && st.cur_rev !== S.rev) await loadDetail(false);
  } catch (e) { /* shown by api() */ }
  setTimeout(poll, 1500);
}

function showStatus(s) {
  $('#status').innerHTML = `drafted ${s.drafted} · notes ${s.notes} &nbsp;|&nbsp; ${s.send_queue} in send queue, ` +
    (s.runner ? '<span class="on">runner on</span>' : '<span class="off">runner off</span>') +
    (s.token === 'none' ? ' &nbsp;|&nbsp; <span class="off">no Terra key: open Settings</span>' : '') +
    (s.checking ? ` &nbsp;|&nbsp; checking${s.checks_waiting ? ` (+${s.checks_waiting})` : ''}` : '');
  S.status = s;
}

// ---- the queue list ----
async function loadShips() {
  const r = await api('GET', '/api/ships');
  S.version = r.version;
  S.rows = r.rows;
  S.columns = r.columns;
  S.widths = r.widths || {};
  fillTable();
}

function visible() {
  const view = $('#view').value, week = $('#week').value, q = $('#search').value.trim().toLowerCase();
  const open = ['drafted', 'notes', 'new'];
  let out = S.rows.filter((s) => {
    const mine = s.notes || s.draft || s.page || s.quick;
    const keep = {
      'queue, unclaimed': s.in_queue && !['claimed', 'queued', 'sent'].includes(s.state) && !s.skipped,
      'queue, all': s.in_queue,
      'mine, open': mine && open.includes(s.state) && !s.skipped,
      'mine, drafted': s.state === 'drafted',
      'mine, request changes': open.includes(s.state) && (s.kind === 'changes' || (!s.draft && s.suggested)),
      'queued and sent': ['queued', 'sent'].includes(s.state),
      skipped: s.skipped,
    }[view];
    if (!keep || (week !== 'all weeks' && s.week !== week)) return false;
    if (S.platforms.size && !s.platforms.some((p) => S.platforms.has(p))) return false;
    if (q && !s.title.toLowerCase().includes(q) && !s.username.toLowerCase().includes(q) && q !== String(s.num)) return false;
    return true;
  });
  const how = $('#sort').value;
  const wk = (s) => WEEK_ORDER[s.week] ?? 9;
  const cmp = (...keys) => (a, b) => { for (const k of keys) { const x = k(a), y = k(b); if (x < y) return -1; if (x > y) return 1; } return 0; };
  const plat = (s) => [...(s.platforms.length ? s.platforms : ['~'])].sort().join(' ');
  if (!view.startsWith('queue') && how === 'oldest first') out.sort(cmp((s) => STATE_ORDER[s.state] ?? 9, wk, (s) => s.num, (s) => s.title));
  else if (how === 'oldest first') out.sort(cmp(wk, (s) => s.queuedAt));
  else if (how === 'newest first') out.sort(cmp((s) => s.queuedAt)).reverse();
  else if (how === 'most hours') out.sort(cmp((s) => -s.seconds));
  else if (how === 'fewest hours') out.sort(cmp((s) => s.seconds));
  else if (how === 'by platform') out.sort(cmp(plat, wk, (s) => s.queuedAt));
  else if (how === 'by week') out.sort(cmp(wk, (s) => s.queuedAt));
  return out;
}

function mineColor(s) {
  return { reject: 'var(--pink)', changes: 'var(--butter)', suggested: 'var(--peach)', steps: 'var(--mint)' }[s.mine] || COLORS[s.state] || 'var(--muted)';
}

function cell(name, s) {
  switch (name) {
    case 'mine': return h('td', { style: `color:${mineColor(s)}` }, s.mine);
    case 'checks': { const c = CHECK[s.check]; return h('td', { class: 'chk', style: c ? `color:${c[1]}` : '' }, c ? c[0] : ''); }
    case 'week': return h('td', {}, s.week.replace('Prep Week', 'PW'));
    case 'ship': return h('td', { class: 'ship' }, (s.num ? `${s.num} ` : '') + s.title);
    case 'player': return h('td', {}, s.username);
    case 'hours': return h('td', {}, s.in_queue ? `${(s.seconds / 3600).toFixed(1)}h` : '');
    case 'waiting': return h('td', {}, s.waiting);
    case 'platform': return h('td', {}, s.platforms.join(' '));
    default: return h('td');
  }
}

function fillTable() {
  const cols = S.columns.length ? S.columns : ['ship'];
  const head = $('#queue thead');
  head.replaceChildren(h('tr', {}, cols.map((c) => {
    const th = h('th', { 'data-col': c, style: S.widths[c] ? `width:${S.widths[c]}px` : (c === 'ship' ? '' : 'width:1%') }, c);
    return th;
  })));
  const vis = visible();
  const body = $('#queue tbody');
  body.replaceChildren(...vis.map((s, i) => {
    const tr = h('tr', { 'data-sid': s.sid, style: `--tint:${TINTS[i % TINTS.length]}`, onclick: () => select(s.sid) }, cols.map((c) => cell(c, s)));
    if (s.sid === S.cur) tr.classList.add('sel');
    return tr;
  }));
  const inQueue = S.rows.filter((s) => s.in_queue).length;
  $('#count').textContent = `${vis.length} shown · ${inQueue} in the queue`;
  if (!vis.length) { if (S.cur) clearDetail(); return; }
  if (!vis.some((s) => s.sid === S.cur)) select(vis[0].sid);
}

function move(d) {
  const vis = visible();
  const i = vis.findIndex((s) => s.sid === S.cur);
  const next = vis[Math.max(0, Math.min(vis.length - 1, i + d))];
  if (next) { select(next.sid); $(`#queue tr[data-sid="${next.sid}"]`)?.scrollIntoView({ block: 'nearest' }); }
}

// widths you drag stay (columns are resizable through their header's corner)
let widthTimer = null;
document.addEventListener('mouseup', () => {
  const ths = $$('#queue th');
  if (!ths.length) return;
  const widths = {};
  let changed = false;
  for (const th of ths) {
    const c = th.dataset.col, w = Math.round(th.getBoundingClientRect().width);
    if (th.style.width && th.style.width !== '1%' && parseInt(th.style.width, 10) !== w) changed = true;
    if (th.dataset.w && +th.dataset.w !== w) changed = true;
    th.dataset.w = w;
    widths[c] = w;
  }
  if (changed) { clearTimeout(widthTimer); widthTimer = setTimeout(() => { S.widths = widths; post('/api/settings/widths', { widths }); }, 500); }
});

// ---- one ship ----
async function select(sid) {
  if (sid === S.cur) return;
  await flushNotes();
  S.cur = sid;
  S.rev = -1;
  $$('#queue tr.sel').forEach((tr) => tr.classList.remove('sel'));
  $(`#queue tr[data-sid="${sid}"]`)?.classList.add('sel');
  await loadDetail(true);
}

function clearDetail() {
  S.cur = null; S.detail = null;
  for (const i of [0, 1, 2]) $(`#tab-${i}`).replaceChildren(h('p', { class: 'muted' }, 'nothing selected'));
  $('#decision').replaceChildren();
  $('#notes').value = '';
}

async function loadDetail(fresh) {
  const sid = S.cur;
  if (!sid) return;
  let d;
  try { d = await api('GET', `/api/ship/${sid}`); } catch (e) { return; }
  if (sid !== S.cur) return;
  const scroll = $('#tab-0').scrollTop, scrollD = $('#decision').scrollTop;
  S.detail = d;
  S.rev = d.rev;
  $('#tab-0').innerHTML = d.page_html;
  if (!fresh) $('#tab-0').scrollTop = scroll;
  renderTest(d);
  $('#tab-2').innerHTML = d.facts_html;
  const notes = $('#notes');
  if (fresh || (document.activeElement !== notes && !S.notesTimer)) { notes.value = d.notes; S.notesFor = sid; }
  renderDecision(d);
  if (!fresh) $('#decision').scrollTop = scrollD;
}

function renderTest(d) {
  const el = $('#tab-1');
  const head = h('p', {}, h('b', {}, `${d.num ? d.num + ' ' : ''}${d.title}`), ' ', h('span', { class: 'muted' }, d.week));
  const btns = h('div', { class: 'row' },
    h('button', { title: 'clone the repo at the ship commit and download the release into the workspace; nothing runs',
      onclick: async () => { const r = await act(`/api/ship/${d.sid}/setup`); if (r?.job) openJob(r.job); } }, 'Set up (clone + download)'),
    h('div', { class: 'menu-wrap' }, h('button', { onclick: (e) => runMenu(e, d) }, 'Run ▾'), h('div', { class: 'menu', id: 'run-menu' })),
    d.test_actions.map((label, i) => h('button', { onclick: () => act(`/api/ship/${d.sid}/action`, { kind: 'test', index: i }) }, label)),
    d.release_dir && h('button', { onclick: () => act(`/api/ship/${d.sid}/open-folder`, { which: 'release' }) }, 'Open release folder'),
    d.repo_dir && h('button', { onclick: () => act(`/api/ship/${d.sid}/open-folder`, { which: 'repo' }) }, 'Open repo folder'));
  const parts = d.test.map((seg) => seg.code !== undefined
    ? h('div', { class: 'code-row' }, h('pre', {}, seg.code),
      h('button', { onclick: () => { navigator.clipboard?.writeText(seg.code); say(`copied: ${seg.code.slice(0, 70)}`); } }, 'Copy'),
      h('button', { title: 'run it (output in Jobs)', onclick: async () => { const r = await act('/api/run-code', { code: seg.code }); if (r?.job) openJob(r.job); } }, 'Run'))
    : h('div', { html: seg.html }));
  const shots = d.screenshots.length ? [h('p', { class: 'muted' }, `${d.screenshots.length} screenshots (click to open)`),
    h('div', { class: 'shots' }, d.screenshots.map((s) => h('img', { src: s.url, title: s.name, onclick: () => window.open(s.url, '_blank', 'noopener') })))] : [];
  el.replaceChildren(head, btns, ...parts, ...shots);
}

async function runMenu(e, d) {
  e.stopPropagation();
  const menu = $('#run-menu');
  if (menu.classList.contains('open')) { closeMenus(); return; }
  closeMenus();
  menu.replaceChildren(h('div', { class: 'note' }, 'looking…'));
  menu.classList.add('open');
  const m = await api('GET', `/api/ship/${d.sid}/run-menu`);
  const items = [];
  for (const r of m.runnables) {
    const how = m.windows ? (['exe', 'msi', 'bat'].includes(r.how) ? (m.sandbox ? 'Windows Sandbox' : 'no sandbox available') : r.how) : `${r.how}, sandboxed`;
    items.push(h('button', { class: 'item', onclick: () => { closeMenus(); runIt(d.sid, r.index, r.label); } }, `${r.label}  (${how})`));
  }
  for (const u of m.links) items.push(h('button', { class: 'item', onclick: () => { closeMenus(); openUrl(u); } }, `open ${u.slice(0, 70)}`));
  if (!items.length) items.push(h('div', { class: 'note' }, 'nothing to run yet, press Set up first'));
  if (m.windows && !m.sandbox && m.runnables.length) {
    items.push(h('div', { class: 'sep' }), h('div', { class: 'note' }, 'Windows Sandbox is off. Turn it on under "Turn Windows features on or off" (Windows Pro/Enterprise) to run builds isolated.'));
  }
  menu.replaceChildren(...items);
}

async function runIt(sid, index, label) {
  let r = await act(`/api/ship/${sid}/run`, { index });
  if (r?.need_direct) {
    const ok = await ask(`Windows Sandbox isn't available on this PC, so ${label} would run directly on your computer, ` +
      'with access to your files and the network. Only do this for builds you trust. Run it anyway?', { yes: 'Run without a sandbox' });
    if (!ok) return;
    r = await act(`/api/ship/${sid}/run`, { index, direct: true });
  }
  if (r?.job) openJob(r.job);
}

function dirtyFor(sid) { return (S.dirty[sid] ||= {}); }

function renderDecision(d) {
  const el = $('#decision');
  const dirty = dirtyFor(d.sid);
  const kids = [];
  const crow = h('div', { class: 'row' });
  if (d.claim.state === 'mine') {
    crow.append(h('b', { class: 'good' }, '● claimed by you'), h('button', { onclick: () => act(`/api/ship/${d.sid}/release`) }, 'Release claim'));
  } else if (d.claim.state === 'other') {
    crow.append(h('b', { class: 'bad' }, `claimed by ${d.claim.by}`));
  } else if (d.claim.state === 'free') {
    crow.append(h('button', { title: 'claim this ship on Terra (like opening its review page); it stays claimed while terra-desk is open', onclick: claim }, 'Claim'));
    if (d.claim.terra_says_mine) crow.append(h('span', { class: 'warn' }, 'Terra says you already hold a claim on it'));
  }
  crow.append(h('span', { class: 'grow' }),
    h('button', { title: d.skipped ? 'put it back in the queue list' : 'hide it from the queue list (it stays under "skipped")', onclick: skip }, d.skipped ? 'Unskip' : 'Skip'));
  kids.push(crow);
  kids.push(h('div', { class: 'row' }, [['approve', 'Approve', 'green'], ['changes', 'Request changes', 'butter'], ['reject', 'Reject', 'pink']].map(([k, label, tint]) =>
    h('button', { 'data-tint': tint, class: d.draft && d.kind === k ? 'on' : '', onclick: () => switchVerdict(k) }, label))));
  if (!d.draft) {
    if (d.suggested_html !== undefined) {
      kids.push(h('div', {}, h('b', { style: 'color:var(--peach)' }, 'Suggested draft'), ' for this ship:'), h('div', { html: d.suggested_html }),
        h('div', { class: 'row' }, h('button', { onclick: () => act(`/api/ship/${d.sid}/use-suggested`) }, 'Use the suggested draft')));
    }
    kids.push(h('p', { class: 'muted' }, 'No draft yet. Pick a verdict above to start from its template.'));
    kids.push(actions(d));
    el.replaceChildren(...kids);
    return;
  }
  const top = h('div', { class: 'row' }, h('span', { class: `badge ${d.kind}` }, d.badge));
  if (d.kind === 'approve') {
    const mult = h('input', { type: 'number', id: 'mult', step: '0.05', min: '0.8', max: '1.5', value: dirty.multiplier ?? d.multiplier,
      oninput: (e) => { dirty.multiplier = e.target.value; } });
    const cut = h('input', { type: 'number', id: 'cut', step: '0.5', min: '0', max: '50', value: dirty.cut ?? d.cut,
      oninput: (e) => { dirty.cut = e.target.value; } });
    top.append(h('span', {}, 'x'), mult, cut, h('span', { class: 'muted' }, 'h cut'));
  }
  kids.push(top);
  if (d.kind === 'approve' && d.hints?.length) kids.push(h('div', { class: 'muted' }, d.hints.join(' · ')));
  for (const f of d.fields) {
    const ta = h('textarea', { class: f.player ? 'player' : '', 'data-key': f.key, oninput: (e) => { dirty['f:' + f.key] = e.target.value; } });
    ta.value = dirty['f:' + f.key] ?? f.text;
    kids.push(h('div', { class: 'field-label' }, f.key), ta);
  }
  if (d.remarks_html) {
    kids.push(h('div', { class: 'row' }, h('h2', { class: 'label butter' }, 'Noticed while drafting'), h('span', { class: 'grow' }),
      h('button', { onclick: () => act(`/api/ship/${d.sid}/clear-remarks`) }, 'Clear')), h('div', { html: d.remarks_html }));
  }
  if (d.kind === 'approve') {
    const vr = h('div', { class: 'row' }, h('span', { id: 'video-label' }, d.video ? `video: ${d.video.name} (${d.video.mb} MiB)` : 'video: none'),
      h('button', { title: 'pick a recording of your test; it is compressed to under 20 MiB and uploaded with the approve', onclick: () => $('#video-file').click() }, 'Attach video…'));
    if (d.video_set) vr.append(h('button', { onclick: () => act(`/api/ship/${d.sid}/remove-video`) }, 'Remove'));
    kids.push(vr);
  }
  if (d.template_left) kids.push(h('div', { class: 'warn' }, `still has template text: ${d.template_left}`));
  kids.push(h('div', { class: 'row' }, h('button', { class: 'primary', onclick: queue }, 'Queue  (Ctrl+Enter)'), h('button', { onclick: () => saveDraft(true) }, 'Save')));
  kids.push(h('div', { class: 'row' }, h('button', { onclick: () => act(`/api/ship/${d.sid}/unqueue`) }, 'Unqueue'), h('button', { onclick: deleteDraft }, 'Delete draft')));
  kids.push(actions(d));
  el.replaceChildren(...kids);
}

function actions(d) {
  return h('div', { class: 'row' }, d.decision_actions.map((a) =>
    h('button', { onclick: async () => { await saveDraft(false); act(`/api/ship/${d.sid}/action`, { kind: 'decision', index: a.index }); } }, a.label)));
}

async function saveDraft(loud) {
  const d = S.detail;
  if (!d || !d.draft) return true;
  const dirty = dirtyFor(d.sid);
  const fields = {};
  for (const [k, v] of Object.entries(dirty)) if (k.startsWith('f:')) fields[k.slice(2)] = v;
  const body = { fields };
  if (d.kind === 'approve') { body.multiplier = +($('#mult')?.value ?? d.multiplier); body.cut = +($('#cut')?.value ?? d.cut); }
  if (!Object.keys(fields).length && (d.kind !== 'approve' || (body.multiplier === d.multiplier && body.cut === d.cut))) {
    if (loud) say('nothing changed');
    return true;
  }
  let r;
  try { r = await post(`/api/ship/${d.sid}/draft`, body); } catch (e) { say(e.message, true); return false; }
  if (r.need_reason) {
    const reason = await ask('One line, why the hours were cut:', { input: '' });
    if (!reason || !reason.trim()) return false;
    try { r = await post(`/api/ship/${d.sid}/draft`, { ...body, reason }); } catch (e) { say(e.message, true); return false; }
  }
  delete S.dirty[d.sid];
  if (r.saved) say('draft saved');
  await loadDetail(false);
  return true;
}

async function switchVerdict(kind) {
  const d = S.detail;
  if (!d || (d.draft && d.kind === kind)) return;
  if (d.draft && !(await ask(`Replace the ${d.kind} draft with a ${kind} template? The note text carries over.`, { yes: 'Replace' }))) return;
  delete S.dirty[d.sid];
  await act(`/api/ship/${d.sid}/verdict`, { kind });
  await loadDetail(false);
}

async function queue() {
  const d = S.detail;
  if (!d || !d.draft) return;
  if (d.kind === 'skip') { say('skips are staff notes, post that one by hand'); return; }
  if (!(await saveDraft(false))) return;
  const dd = S.detail;
  const extra = dd.kind === 'approve' ? `  x${dd.multiplier}` : '';
  if (!(await ask(`Queue ${dd.badge}${extra} for ${dd.title}?`, { yes: 'Queue' }))) return;
  say('queueing…');
  await act(`/api/ship/${dd.sid}/queue`);
  await loadDetail(false);
}

async function deleteDraft() {
  const d = S.detail;
  if (d && (await ask('Delete the draft? Your notes stay.', { yes: 'Delete' }))) {
    delete S.dirty[d.sid];
    await act(`/api/ship/${d.sid}/delete-draft`);
  }
}

async function skip() {
  const d = S.detail;
  if (!d) return;
  if (!d.skipped && !(await ask(`Hide ${d.title} from the queue list? It stays under "skipped".`, { yes: 'Skip' }))) return;
  await act(`/api/ship/${d.sid}/skip`, { skipped: !d.skipped });
}

async function claim() {
  const d = S.detail;
  if (!d) return;
  if (S.status?.runner && S.status.send_queue &&
      !(await ask('The runner is sending your queue, and Terra only lets you hold one claim. Claiming now can make its next send fail. Claim anyway?', { yes: 'Claim' }))) return;
  say('claiming…');
  await act(`/api/ship/${d.sid}/claim`);
}

$('#video-file').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  e.target.value = '';
  const d = S.detail;
  if (!file || !d) return;
  const label = $('#video-label');
  if (label) label.textContent = 'video: uploading…';
  try {
    await api('POST', `/api/ship/${d.sid}/video`, undefined, { body: file, headers: { 'X-Filename': encodeURIComponent(file.name), 'Content-Type': 'application/octet-stream' } });
    if (label) label.textContent = 'video: compressing…';
    say('compressing the video (it shows here when it is ready)…');
  } catch (err) { say(`video failed: ${err.message}`, true); if (label) label.textContent = 'video: none'; }
});

// ---- notes ----
$('#notes').addEventListener('input', () => {
  clearTimeout(S.notesTimer);
  S.notesTimer = setTimeout(flushNotes, 800);
});
async function flushNotes() {
  if (!S.notesTimer) return;
  clearTimeout(S.notesTimer);
  S.notesTimer = null;
  const sid = S.notesFor;
  if (!sid) return;
  try { await post(`/api/ship/${sid}/notes`, { notes: $('#notes').value }); say('notes saved'); } catch (e) { say(`notes NOT saved: ${e.message}`, true); }
}
window.addEventListener('beforeunload', () => { if (S.notesTimer) flushNotes(); });

// ---- links: web links open in your browser; Terra review pages never (opening one claims the ship) ----
function openUrl(u) { act('/api/open-url', { url: u }); }
document.addEventListener('click', (e) => {
  const a = e.target.closest('a[href]');
  if (!a) return;
  e.preventDefault();
  const href = a.getAttribute('href');
  if (href.startsWith('/file/')) window.open(href, '_blank', 'noopener');
  else if (/^https?:\/\//i.test(href)) openUrl(href);
});

// ---- tabs ----
function showTab(i) {
  S.tab = i;
  $$('#tabs button').forEach((b) => b.classList.toggle('on', +b.dataset.tab === i));
  for (const k of [0, 1, 2]) $(`#tab-${k}`).hidden = k !== i;
}
$('#tabs').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) showTab(+b.dataset.tab); });

// ---- menus ----
function closeMenus() { $$('.menu.open').forEach((m) => m.classList.remove('open')); }
document.addEventListener('click', (e) => { if (!e.target.closest('.menu-wrap')) closeMenus(); });
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-menu]');
  if (!b) return;
  const menu = $('#' + b.dataset.menu);
  const was = menu.classList.contains('open');
  closeMenus();
  if (was) return;
  ({ 'stash-menu': fillStash, 'runner-menu': fillRunner, 'platform-menu': fillPlatforms }[b.dataset.menu])(menu);
  menu.classList.add('open');
});

function fillPlatforms(menu) {
  const presets = [['all platforms', []], ['runs here (not macOS-only)', PLATFORMS.filter((p) => p !== 'macos')]];
  const same = (arr) => arr.length === S.platforms.size && arr.every((p) => S.platforms.has(p));
  const set = (arr) => { S.platforms = new Set(arr); showPlatforms(); fillTable(); fillPlatforms(menu); };
  menu.replaceChildren(
    ...presets.map(([label, arr]) => h('button', { class: 'item' + (same(arr) ? ' on' : ''), onclick: (e) => { e.stopPropagation(); set(arr); } }, label)),
    h('div', { class: 'sep' }),
    ...PLATFORMS.map((p) => h('button', { class: 'item' + (S.platforms.has(p) ? ' on' : ''), onclick: (e) => {
      e.stopPropagation(); const n = new Set(S.platforms); n.has(p) ? n.delete(p) : n.add(p); set([...n]);
    } }, p)));
}
function showPlatforms() {
  const names = PLATFORMS.filter((p) => S.platforms.has(p));
  const notMac = PLATFORMS.filter((p) => p !== 'macos');
  $('#platform-btn').textContent = !names.length ? 'all platforms'
    : names.length === notMac.length && notMac.every((p) => S.platforms.has(p)) ? 'runs here' : names.join(', ');
}

async function fillStash(menu) {
  const items = [h('button', { class: 'item', onclick: async () => {
    closeMenus();
    await flushNotes();
    if (await ask('Put everything aside and start fresh: notes, drafts, videos, set-up folders, skips and imported pages? Pop brings it all back. Queued and sent reviews are not affected.', { yes: 'Stash' })) act('/api/stash');
  } }, 'Stash everything and start fresh'), h('div', { class: 'sep' })];
  menu.replaceChildren(...items, h('div', { class: 'note' }, 'loading…'));
  const { stashes } = await api('GET', '/api/stashes');
  if (!stashes.length) items.push(h('div', { class: 'note' }, 'no stashes yet'));
  for (const x of stashes) {
    items.push(h('div', { class: 'note' }, `${x.when} · ${x.drafts} drafts, ${x.notes} with notes, ${x.pages} pages`),
      h('button', { class: 'item sub', onclick: async () => { closeMenus(); if (await ask(`Bring back the stash from ${x.when}? What you have now gets stashed first, so nothing is lost.`, { yes: 'Pop' })) act('/api/stash/pop', { name: x.path }); } }, 'Pop (bring it back)'),
      h('button', { class: 'item sub', onclick: async () => { closeMenus(); if (await ask(`Delete the stash from ${x.when} for good?`, { yes: 'Delete' })) act('/api/stash/drop', { name: x.path }); } }, 'Delete'));
  }
  menu.replaceChildren(...items);
}

function fillRunner(menu) {
  const on = S.status?.runner;
  const items = [h('div', { class: 'note' }, on ? 'The runner is sending your send queue.' : 'The runner sends your queued reviews one at a time, claiming each ship right before it goes out.')];
  if (!on) {
    const every = h('input', { type: 'number', value: localStorage.getItem('every') || '4', min: '0.5', step: '0.5', style: 'width:80px' });
    const hold = h('select', {}, h('option', { value: '1' }, 'hold the next claim during the wait'), h('option', { value: '0' }, 'claim each only as it is sent'));
    items.push(h('div', { class: 'row', style: 'padding:4px 10px' }, 'every', every, 'min'), h('div', { style: 'padding:4px 10px' }, hold),
      h('button', { class: 'item', onclick: async () => {
        closeMenus();
        try { localStorage.setItem('every', every.value); } catch (e) { /* private window */ }
        const r = await act('/api/runner/start', { every: +every.value, hold: +hold.value }, 'runner started');
        if (r?.job) openJob(r.job);
      } }, 'Start the runner'));
  } else {
    items.push(h('button', { class: 'item', onclick: () => { closeMenus(); act('/api/runner/control', { word: 'pause' }); } }, 'Pause'),
      h('button', { class: 'item', onclick: () => { closeMenus(); act('/api/runner/control', { word: 'resume' }); } }, 'Resume'),
      h('button', { class: 'item', onclick: async () => { closeMenus(); const m = await ask('Minutes between sends:', { input: '4' }); if (m) act('/api/runner/control', { word: m }); } }, 'Change the gap…'),
      h('button', { class: 'item', onclick: () => { closeMenus(); act('/api/runner/control', { word: 'stop' }); } }, 'Stop (releases its claims)'));
    if (S.status.runner_job) items.push(h('button', { class: 'item', onclick: () => { closeMenus(); openJob(S.status.runner_job); } }, 'Show its output'));
  }
  menu.replaceChildren(...items);
}

// ---- jobs ----
function showJobsList(list) {
  S.jobs = list;
  const running = list.filter((j) => j.running).length;
  $('#jobs-count').textContent = list.length ? `(${running ? running + ' running' : list.length})` : '';
  const tabs = $('#job-tabs');
  tabs.replaceChildren(...list.map((j) => h('button', { class: j.id === S.job ? 'on' : '', onclick: () => openJob(j.id) },
    `${j.title.slice(0, 28)}${j.running ? ' …' : j.code ? ` · exit ${j.code}` : ' ✓'}`)));
}
function openJob(id) {
  S.job = id;
  $('#jobs').hidden = false;
  showJobsList(S.jobs);
  refreshJob(true);
}
let jobTimer = null;
async function refreshJob(force) {
  clearTimeout(jobTimer);
  if (!S.job || $('#jobs').hidden) return;
  try {
    const j = await api('GET', `/api/jobs/${S.job}`);
    const log = $('#job-log');
    const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
    if (force || log.textContent.length !== j.text.length) {
      log.textContent = j.text;
      if (atEnd || force) log.scrollTop = log.scrollHeight;
    }
    $('#job-stop').disabled = !j.running;
    $('#job-close').disabled = j.running;
    $('#job-input').disabled = !j.running;
    if (j.running) jobTimer = setTimeout(refreshJob, 1000);
  } catch (e) { S.job = null; }
}
$('#job-stop').onclick = () => S.job && act(`/api/jobs/${S.job}/stop`);
$('#job-close').onclick = async () => { if (S.job) { await act(`/api/jobs/${S.job}/close`); S.job = null; $('#job-log').textContent = ''; } };
$('#job-outside').onclick = () => S.job && act(`/api/jobs/${S.job}/outside`);
$('#jobs-hide').onclick = () => { $('#jobs').hidden = true; };
$('#job-input-form').addEventListener('submit', (e) => {
  e.preventDefault();
  if (S.job) { post(`/api/jobs/${S.job}/input`, { text: $('#job-input').value }); $('#job-input').value = ''; setTimeout(refreshJob, 200); }
});
function toggleJobs() {
  const el = $('#jobs');
  el.hidden = !el.hidden;
  if (!el.hidden && !S.job && S.jobs.length) openJob(S.jobs[S.jobs.length - 1].id);
  else refreshJob(true);
}

// ---- toolbar ----
const DO = {
  refresh: () => act('/api/refresh'),
  reload: () => act('/api/reload'),
  'check-shown': async () => {
    const sids = visible().filter((s) => s.in_queue && !s.check).map((s) => s.sid);
    if (!sids.length) { say('every shown ship is already checked'); return; }
    if (sids.length > 25 && !(await ask(`Check ${sids.length} ships? Each one clones its repo into the workspace (read-only, nothing runs), one at a time.`, { yes: 'Check' }))) return;
    act('/api/check-shown', { sids });
  },
  dump: async () => { const r = await act('/api/dump'); if (r?.job) openJob(r.job); },
  jobs: toggleJobs,
  settings: openSettings,
  quit: async () => {
    if (!(await ask('Close terra-desk? Your claim is released; a running send runner keeps going.', { yes: 'Quit' }))) return;
    await flushNotes();
    await act('/api/quit');
    document.body.replaceChildren(h('p', { style: 'padding:40px' }, 'terra-desk is closed. You can close this window.'));
  },
};
document.addEventListener('click', (e) => { const b = e.target.closest('[data-do]'); if (b) DO[b.dataset.do](); });
for (const id of ['view', 'week', 'sort']) $('#' + id).addEventListener('change', () => { savePrefs(); fillTable(); });
$('#search').addEventListener('input', fillTable);

function savePrefs() {
  try { localStorage.setItem('prefs', JSON.stringify({ view: $('#view').value, week: $('#week').value, sort: $('#sort').value, platforms: [...S.platforms] })); } catch (e) { /* ok */ }
}
function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem('prefs') || '{}');
    for (const k of ['view', 'week', 'sort']) if (p[k]) $('#' + k).value = p[k];
    S.platforms = new Set(p.platforms || []);
  } catch (e) { /* ok */ }
  showPlatforms();
}

// ---- keys ----
document.addEventListener('keydown', (e) => {
  const typing = e.target.matches('input, textarea, select');
  const ctrl = e.ctrlKey || e.metaKey;
  if (ctrl && e.key === 's') { e.preventDefault(); flushNotes(); saveDraft(true); }
  else if (ctrl && e.key === 'Enter') { e.preventDefault(); queue(); }
  else if (ctrl && e.key.toLowerCase() === 'r' && !e.shiftKey) { e.preventDefault(); DO.refresh(); }
  else if (e.key === 'F5') { e.preventDefault(); DO.reload(); }
  else if (ctrl && e.key === ',') { e.preventDefault(); openSettings(); }
  else if (ctrl && e.key === '`') { e.preventDefault(); toggleJobs(); }
  else if (ctrl && e.shiftKey && e.key.toLowerCase() === 'c') { e.preventDefault(); DO['check-shown'](); }
  else if (!typing && !ctrl && (e.key === 'j' || e.key === 'J')) move(1);
  else if (!typing && !ctrl && (e.key === 'k' || e.key === 'K')) move(-1);
  else if (e.key === 'Escape') closeMenus();
});

// ---- settings ----
async function openSettings() {
  const st = await api('GET', '/api/settings');
  const tabs = { General: general, Queue: columns, 'Terra key': key, Templates: () => editor(st.templates) };
  if (st.prompts.length) tabs.Prompts = () => editor(st.prompts);
  const nav = $('#settings-tabs');
  const body = $('#settings-body');
  const show = (name) => {
    $$('button', nav).forEach((b) => b.classList.toggle('on', b.textContent === name));
    body.replaceChildren(...[tabs[name](st)].flat());
  };
  nav.replaceChildren(...Object.keys(tabs).map((n) => h('button', { onclick: () => show(n) }, n)));
  show('General');
  $('#settings').showModal();
}
$('#settings-close').onclick = () => $('#settings').close();

function general(st) {
  const handle = h('input', { value: st.handle, style: 'width:260px' });
  const terminal = h('input', { value: st.terminal, placeholder: st.windows ? 'auto: Windows Terminal or cmd' : 'auto: $TERMINAL, kitty, foot, alacritty, ...', style: 'width:260px' });
  const win = h('select', {}, h('option', { value: 'app' }, 'its own window (Edge or Chrome app window)'), h('option', { value: 'browser' }, 'a tab in your default browser'));
  win.value = st.window;
  const extra = st.extra.map((x) => { const s = h('select', { title: x.help }, x.choices.map((c) => h('option', {}, c))); s.value = x.value; return [x, s]; });
  const grid = h('div', { class: 'form-grid' }, 'Your Slack handle', handle, 'Terminal for "Open outside"', terminal, 'Open terra-desk in', win,
    extra.map(([x, s]) => [x.label, s]));
  const save = h('button', { class: 'primary', onclick: () => act('/api/settings/general', { handle: handle.value, terminal: terminal.value, window: win.value,
    extra: Object.fromEntries(extra.map(([x, s]) => [x.key, s.value])) }) }, 'Save');
  const sandbox = st.windows ? h('p', { class: st.sandbox ? 'good' : 'warn' }, st.sandbox
    ? 'Windows Sandbox is on: Run starts .exe builds inside it, with no network and only their own folder.'
    : 'Windows Sandbox is off, so Run can only start builds directly (it asks first). On Windows 10/11 Pro, Enterprise or Education, turn on "Windows Sandbox" under "Turn Windows features on or off" to run builds isolated.') : null;
  return [grid, h('p', { class: 'muted' }, `Saved to ${st.config_file}. The handle goes at the end of every note to a player. Workspace: ${st.workspace}`), sandbox, h('div', { class: 'row' }, save)];
}

function columns(st) {
  const boxes = st.columns.map((c) => {
    const box = h('input', { type: 'checkbox', checked: st.shown.includes(c) || c === 'ship', disabled: c === 'ship' });
    box.onchange = async () => {
      const cols = st.columns.filter((_, i) => boxes[i][1].checked);
      st.shown = cols;
      await post('/api/settings/columns', { columns: cols });
      await loadShips();
    };
    return [h('label', { class: 'check' }, box, c === 'ship' ? 'ship (always shown)' : c), box];
  });
  return [h('p', { class: 'muted' }, 'Columns in the queue table. Drag the bottom-right corner of a column heading to resize it.'),
    ...boxes.map((b) => b[0]),
    h('div', { class: 'row' }, h('button', { onclick: async () => { await post('/api/settings/widths', { widths: {} }); await loadShips(); say('column widths reset'); } }, 'Reset column widths'))];
}

function key(st) {
  const state = h('b');
  const show = (s) => {
    const [cls, text] = { file: ['good', 'A key is saved.'], env: ['good', 'The TERRA_TOKEN environment variable is set; it is used instead of the saved key.'],
      none: ['bad', 'No key saved yet: the queue and review pages can\'t load without one.'] }[s];
    state.className = cls; state.textContent = text;
    remove.disabled = s !== 'file';
  };
  const field = h('input', { type: 'password', placeholder: 'paste your Terra key', style: 'flex:1', autocomplete: 'off' });
  const save = h('button', { class: 'primary', onclick: async () => { const r = await act('/api/settings/key', { key: field.value }, 'key saved, reloading the queue'); field.value = ''; if (r) show(r.token); } }, 'Save key');
  const remove = h('button', { onclick: async () => { if (await ask('Delete the saved Terra key from this computer?', { yes: 'Delete' })) { const r = await act('/api/settings/key', { remove: true }, 'key removed'); if (r) show(r.token); } } }, 'Remove key');
  field.onkeydown = (e) => { if (e.key === 'Enter') save.click(); };
  show(st.token);
  return [state, h('p', { class: 'muted', html: 'Your Terra key is your own login. On terra.hackclub.com, open the browser console (F12) and run ' +
    `<code>copy(localStorage.terra_auth_token)</code>, then paste it here. It is saved to <code>${esc(st.token_file)}</code> (only your Windows account can read it) and never shown again.` }),
    h('div', { class: 'row' }, field, save, remove)];
}

function editor(entries) {
  const list = h('div', { class: 'list' });
  const help = h('p', { class: 'muted' });
  const ta = h('textarea', { spellcheck: 'false' });
  const state = h('span', { class: 'muted' });
  const save = h('button', { class: 'primary' }, 'Save');
  const reset = h('button');
  let cur = null;
  const pick = (e, btn) => {
    cur = e;
    $$('button', list).forEach((b) => b.classList.toggle('on', b === btn));
    help.textContent = (e.help || '') + (e.file ? `  File: ${e.file}` : '');
    ta.value = e.text;
    state.textContent = e.file ? 'file' : e.text !== e.default ? 'edited' : 'default';
    reset.textContent = e.file ? 'Reload from disk' : 'Reset to default';
  };
  save.onclick = async () => { if (!cur) return; const r = await act('/api/settings/text', { key: cur.key, text: ta.value }); if (r) { cur.text = ta.value; state.textContent = cur.file ? 'saved' : ta.value !== cur.default ? 'saved' : 'default'; } };
  reset.onclick = async () => {
    if (!cur) return;
    if (cur.file) { ta.value = cur.text; return; }
    if (!(await ask(`Go back to the default ${cur.name}? Your edits are lost.`, { yes: 'Reset' }))) return;
    const r = await act('/api/settings/text', { key: cur.key, reset: true });
    if (r) { cur.text = r.text; ta.value = r.text; state.textContent = 'default'; }
  };
  entries.forEach((e, i) => { const b = h('button', { onclick: () => pick(e, b) }, e.name); list.append(b); if (i === 0) setTimeout(() => pick(e, b)); });
  return h('div', { class: 'editor' }, list, h('div', { class: 'pane-r' }, help, ta, h('div', { class: 'row' }, state, h('span', { class: 'grow' }), reset, save)));
}

// ---- start ----
loadPrefs();
clearDetail();
poll();
