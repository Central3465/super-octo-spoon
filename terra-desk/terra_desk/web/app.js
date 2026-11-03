// terra-desk's page. It talks to the server on this computer (server.py); everything it shows comes from there.
'use strict';

const KEY = document.querySelector('meta[name=desk-key]').content;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'html') el.innerHTML = v;
    else if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') for (const [sk, sv] of Object.entries(v)) { if (sk.startsWith('--')) el.style.setProperty(sk, sv); else el.style[sk] = sv; }
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid !== null && kid !== undefined && kid !== false) el.append(kid.nodeType ? kid : String(kid));
  return el;
};

const TINT = { approve: 'var(--green)', changes: 'var(--yellow)', reject: 'var(--red)', move: 'var(--purple)', skip: 'var(--muted)' };
const PLATFORMS = ['web', 'windows', 'linux', 'macos', 'other'];
const WEEK_ORDER = { 'Prep Week 3': 0, 'Prep Week 2': 1, 'Week 1': 2, 'Week 2': 3, 'Week 3': 4 };
const STATE_ORDER = { drafted: 0, notes: 1, new: 2, queued: 3, sent: 4, claimed: 5, gone: 6 };
const STEP_KEY = 'step';

const S = {
  rows: [], version: -1, cur: null, detail: null, rev: -1, since: 0, platforms: new Set(), jobs: [], job: null,
  dirty: {}, notesTimer: null, notesFor: null, draftTimer: null, step: 0, rubric: null, status: {},
};

// ---- talking to the server ----
async function api(method, path, body, raw) {
  const opts = { method, headers: { 'X-Desk-Key': KEY } };
  if (raw) { opts.body = raw.body; Object.assign(opts.headers, raw.headers); }
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers['Content-Type'] = 'application/json'; }
  let res;
  try { res = await fetch(path, opts); } catch (e) { say('terra-desk is not answering. Is it still running?'); throw e; }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { const err = new Error(data.error || res.statusText); err.data = data; throw err; }
  return data;
}
const post = (path, body = {}) => api('POST', path, body);
async function act(path, body, okMsg) {
  try { const r = await post(path, body); if (r.msg || okMsg) say(r.msg || okMsg); return r; } catch (e) { say(e.message, true); return null; }
}

let toastTimer = null;
function say(msg, important) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), important ? 9000 : 4500);
}

function ask(text, { input = null, yes = 'OK' } = {}) {
  return new Promise((resolve) => {
    const d = $('#ask');
    $('#ask-text').textContent = text;
    const inp = $('#ask-input');
    inp.hidden = input === null;
    inp.value = input || '';
    $('#ask-yes').textContent = yes;
    let done = false;
    const finish = (v) => { if (done) return; done = true; d.close(); resolve(v); };
    $('#ask-yes').onclick = () => finish(input === null ? true : inp.value);
    $('#ask-no').onclick = () => finish(input === null ? false : null);
    d.onclose = () => finish(input === null ? false : null);
    inp.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); finish(inp.value); } };
    d.showModal();
    (input === null ? $('#ask-yes') : inp).focus();
  });
}

function openDialog(title, ...body) {
  $('#dialog-title').textContent = title;
  $('#dialog-body').replaceChildren(...body.flat());
  if (!$('#dialog').open) $('#dialog').showModal();
}
$('#dialog-close').onclick = () => $('#dialog').close();

// ---- the state poll ----
async function poll() {
  try {
    const st = await api('GET', `/api/state?since=${S.since}`);
    for (const m of st.messages) { S.since = m.id; say(m.text); }
    for (const ev of st.events) {
      if (ev.copy !== undefined) navigator.clipboard?.writeText(ev.copy).catch(() => {});
      if (ev.tab !== undefined) showStep(ev.tab);
    }
    showStatus(st.status);
    showJobsList(st.jobs);
    if (st.version !== S.version) await loadShips();
    if (S.cur && st.cur_rev !== S.rev) await loadDetail(false);
  } catch (e) { /* shown by api() */ }
  setTimeout(poll, 1500);
}

function showStatus(s) {
  S.status = s;
  const pills = [
    h('span', { class: 'pill' }, `${s.drafted} drafted`),
    h('button', { class: 'pill' + (s.runner ? ' on' : ''), title: 'Send your queued reviews', onclick: openSending },
      `${s.send_queue} waiting to send · sender ${s.runner ? 'on' : 'off'}`),
  ];
  if (s.checking) pills.push(h('span', { class: 'pill' }, `checking${s.checks_waiting ? ` (+${s.checks_waiting} more)` : ''}…`));
  $('#status').replaceChildren(...pills);
  const banner = $('#banner');
  if (s.token === 'none') {
    banner.hidden = false;
    if (!banner.dataset.shown) {
      banner.dataset.shown = '1';
      banner.replaceChildren(h('div', { class: 'grow' }, h('b', {}, 'Add your Terra key to load the queue. '),
        'It takes a minute: Settings → Terra key explains how.'), h('button', { class: 'primary', onclick: () => openSettings('Terra key') }, 'Add my Terra key'));
    }
  } else { banner.hidden = true; delete banner.dataset.shown; }
}

// ---- the ship list ----
async function loadShips() {
  const r = await api('GET', '/api/ships');
  S.version = r.version;
  S.rows = r.rows;
  fillList();
}

function visible() {
  const view = $('#view').value, week = $('#week').value, q = $('#search').value.trim().toLowerCase();
  const open = ['drafted', 'notes', 'new'];
  const out = S.rows.filter((s) => {
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

const KIND_LABEL = { approve: 'Approve', changes: 'Request changes', reject: 'Reject', move: 'Move review', skip: 'Skip note' };
function stateBadge(s) {
  if (s.state === 'drafted') return h('span', { class: 'badge', style: { '--tint': TINT[s.kind] || 'var(--blue)' } }, `Draft: ${KIND_LABEL[s.kind] || 'written'}`);
  if (s.state === 'notes') return h('span', { class: 'badge', style: { '--tint': 'var(--yellow)' } }, 'Has notes');
  if (s.state === 'queued') return h('span', { class: 'badge', style: { '--tint': 'var(--purple)' } }, 'Waiting to send');
  if (s.state === 'sent') return h('span', { class: 'badge', style: { '--tint': 'var(--green)' } }, 'Sent');
  if (s.state === 'claimed') return h('span', { class: 'badge', style: { '--tint': 'var(--red)' } }, `Claimed by ${s.claimed_by || 'someone'}`);
  if (s.state === 'gone') return h('span', { class: 'badge plain' }, 'No longer in the queue');
  if (s.suggested) return h('span', { class: 'badge', style: { '--tint': 'var(--orange)' } }, 'Suggested draft');
  return null;
}
function checkBadge(level) {
  const c = { ok: ['Checks look fine', 'var(--green)'], warn: ['Checks: worth a look', 'var(--yellow)'], bad: ['Checks found problems', 'var(--red)'],
    running: ['Checking…', 'var(--muted)'] }[level];
  return c ? h('span', { class: 'badge', style: { '--tint': c[1] } }, c[0]) : null;
}

function fillList() {
  const vis = visible();
  const list = $('#ship-list');
  list.replaceChildren(...vis.map((s) => h('li', { class: 'ship-item' + (s.sid === S.cur ? ' sel' : ''), 'data-sid': s.sid },
    h('button', { onclick: () => select(s.sid) },
      h('span', { class: 'ship-title' }, (s.num ? `${s.num}. ` : '') + s.title),
      h('span', { class: 'ship-meta' }, [s.username, s.week, s.in_queue ? `${(s.seconds / 3600).toFixed(1)}h` : null,
        s.waiting ? `waiting ${s.waiting.replace(' ago', '')}` : null].filter(Boolean).join(' · ')),
      h('span', { class: 'badges' }, stateBadge(s), checkBadge(s.check), s.platforms.map((p) => h('span', { class: 'badge plain' }, p)))))));
  if (!vis.length) list.append(h('li', { class: 'empty-list' }, S.rows.length ? 'No ships match. Try "Everything in the queue" or clear the search.'
    : (S.status.token === 'none' ? 'Add your Terra key to load the queue.' : 'Loading the queue…')));
  const inQueue = S.rows.filter((s) => s.in_queue).length;
  $('#count').textContent = `${vis.length} shown · ${inQueue} in queue`;
  if (S.cur && !S.rows.some((s) => s.sid === S.cur)) clearDetail();
}

function move(d) {
  const vis = visible();
  const i = vis.findIndex((s) => s.sid === S.cur);
  const next = vis[Math.max(0, Math.min(vis.length - 1, i + d))];
  if (next) { select(next.sid); $(`.ship-item[data-sid="${next.sid}"]`)?.scrollIntoView({ block: 'nearest' }); }
}

// ---- one ship ----
async function select(sid) {
  if (sid === S.cur) return;
  await flushNotes();
  await flushDraft();
  S.cur = sid;
  S.rev = -1;
  $$('.ship-item.sel').forEach((el) => el.classList.remove('sel'));
  $(`.ship-item[data-sid="${sid}"]`)?.classList.add('sel');
  await loadDetail(true);
}

function clearDetail() {
  S.cur = null; S.detail = null;
  $('#ship').hidden = true;
  $('#empty').hidden = false;
}

async function loadDetail(fresh) {
  const sid = S.cur;
  if (!sid) return;
  let d;
  try { d = await api('GET', `/api/ship/${sid}`); } catch (e) { return; }
  if (sid !== S.cur) return;
  const ws = $('#workspace'), scroll = ws.scrollTop;
  S.detail = d;
  S.rev = d.rev;
  $('#empty').hidden = true;
  $('#ship').hidden = false;
  renderHead(d);
  $('#step-0').replaceChildren(h('div', { class: 'content', html: d.page_html }));
  // a background refresh (checks done, images loaded) never rebuilds a form you are typing in
  const active = document.activeElement;
  const busy = !fresh && active && active.matches('input, textarea, select') && active.closest('#step-1, #step-2');
  S.pendingRender = busy;
  if (!busy) {
    const typing = S.notesTimer && !fresh;
    renderTest(d, typing ? $('#notes').value : d.notes);
    renderDecide(d);
  }
  $('#step-btn-3').hidden = !d.has_page;
  $('#step-3').replaceChildren(h('div', { class: 'section content', html: d.facts_html }));
  if (S.step === 3 && !d.has_page) S.step = 0;
  showStep(S.step);
  if (fresh) ws.scrollTop = 0; else ws.scrollTop = scroll;
  S.notesFor = sid;
}
document.addEventListener('focusout', () => setTimeout(() => {
  const a = document.activeElement;
  if (S.pendingRender && !(a && a.matches('input, textarea, select') && a.closest('#step-1, #step-2'))) { S.pendingRender = false; loadDetail(false); }
}, 300));

function renderHead(d) {
  const claim = h('div', { class: 'row' });
  if (d.claim.state === 'mine') claim.append(h('span', { class: 'badge', style: { '--tint': 'var(--green)' } }, 'You have claimed it'),
    h('button', { onclick: () => act(`/api/ship/${d.sid}/release`) }, 'Release claim'));
  else if (d.claim.state === 'other') claim.append(h('span', { class: 'badge', style: { '--tint': 'var(--red)' } }, `Claimed by ${d.claim.by}`));
  else if (d.claim.state === 'free') {
    claim.append(h('button', { title: 'Claim it on Terra, like opening its review page. You keep the claim while terra-desk is open.', onclick: claimShip }, 'Claim this ship'));
    if (d.claim.terra_says_mine) claim.append(h('span', { class: 'muted small' }, 'Terra says you already hold a claim on it'));
  }
  claim.append(h('button', { onclick: skip, title: d.skipped ? 'Put it back in the list' : 'Hide it from "Waiting for review" (it stays under "Skipped")' }, d.skipped ? 'Unskip' : 'Skip'));
  const meta = [d.username && `by ${d.username}`, d.week, d.hours !== null && d.hours !== undefined ? `${d.hours}h tracked` : null,
    d.waiting && `waiting ${d.waiting.replace(' ago', '')}`, d.platforms.length ? d.platforms.join(', ') : null].filter(Boolean).join(' · ');
  $('#ship-head').replaceChildren(
    h('div', { class: 'grow' }, h('h1', {}, (d.num ? `${d.num}. ` : '') + d.title), h('div', { class: 'meta' }, meta)),
    h('div', { class: 'side' }, claim));
}

function showStep(i) {
  S.step = +i;
  try { localStorage.setItem(STEP_KEY, String(S.step)); } catch (e) { /* private window */ }
  $$('#steps button').forEach((b) => b.classList.toggle('on', +b.dataset.step === S.step));
  for (const k of [0, 1, 2, 3]) $(`#step-${k}`).hidden = k !== S.step;
}
$('#steps').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) showStep(+b.dataset.step); });

// ---- step 2: test it ----
function renderTest(d, notesText) {
  const prep = h('div', { class: 'section' },
    h('h2', {}, 'Get it ready'),
    h('p', { class: 'lead' }, 'Set up downloads the code and the release onto your computer. Nothing runs until you press Run.'),
    h('div', { class: 'row' },
      h('button', { class: 'primary', onclick: async () => { const r = await act(`/api/ship/${d.sid}/setup`); if (r?.job) openJob(r.job); } }, 'Set up (download)'),
      h('div', { class: 'menu-wrap' }, h('button', { onclick: (e) => runMenu(e, d) }, 'Run ▾'), h('div', { class: 'menu left', id: 'run-menu' })),
      d.release_dir && h('button', { onclick: () => act(`/api/ship/${d.sid}/open-folder`, { which: 'release' }) }, 'Open release folder'),
      d.repo_dir && h('button', { onclick: () => act(`/api/ship/${d.sid}/open-folder`, { which: 'repo' }) }, 'Open code folder'),
      d.test_actions.map((label, i) => h('button', { onclick: () => act(`/api/ship/${d.sid}/action`, { kind: 'test', index: i }) }, label))));
  const steps = h('div', { class: 'section content' }, h('h2', {}, 'How to test it'),
    d.test.map((seg) => seg.code !== undefined
      ? h('div', { class: 'code-row' }, h('pre', {}, seg.code),
        h('button', { onclick: () => { navigator.clipboard?.writeText(seg.code); say('Copied'); } }, 'Copy'),
        h('button', { title: 'Run this command (its output shows in Activity)', onclick: async () => { const r = await act('/api/run-code', { code: seg.code }); if (r?.job) openJob(r.job); } }, 'Run'))
      : h('div', { html: seg.html })));
  const shots = d.screenshots.length ? h('div', { class: 'section' }, h('h2', {}, 'Screenshots'),
    h('div', { class: 'shots' }, d.screenshots.map((s) => h('img', { src: s.url, title: s.name, alt: s.name, onclick: () => window.open(s.url, '_blank', 'noopener') })))) : null;
  const saved = h('span', { class: 'saved', id: 'notes-saved' }, notesText ? 'Saved' : '');
  const ta = h('textarea', { id: 'notes', placeholder: 'What did you try, on what, and what happened? Bugs you hit, what impressed you…' });
  ta.value = notesText;
  ta.addEventListener('input', () => { $('#notes-saved').textContent = 'Saving…'; clearTimeout(S.notesTimer); S.notesTimer = setTimeout(flushNotes, 800); });
  const notes = h('div', { class: 'section' }, h('h2', {}, 'Your notes', h('span', { class: 'grow' }), saved),
    h('p', { class: 'lead' }, 'Saved as you type. They show again on the Decide step while you write the review.'), ta,
    h('div', { class: 'row end' }, h('button', { class: 'primary', onclick: async () => { await flushNotes(); showStep(2); } }, 'Next: decide →')));
  $('#step-1').replaceChildren(prep, steps, shots || '', notes);
}

async function runMenu(e, d) {
  e.stopPropagation();
  const menu = $('#run-menu');
  if (menu.classList.contains('open')) { closeMenus(); return; }
  closeMenus();
  menu.replaceChildren(h('div', { class: 'note' }, 'Looking…'));
  menu.classList.add('open');
  const m = await api('GET', `/api/ship/${d.sid}/run-menu`);
  const items = [];
  for (const r of m.runnables) {
    const how = m.windows ? (['exe', 'msi', 'bat'].includes(r.how) ? (m.sandbox ? 'in Windows Sandbox' : 'no sandbox on this PC') : r.how) : `${r.how}, sandboxed`;
    items.push(h('button', { class: 'item', onclick: () => { closeMenus(); runIt(d.sid, r.index, r.label); } }, `${r.label}  (${how})`));
  }
  if (m.links.length) { if (items.length) items.push(h('div', { class: 'sep' })); }
  for (const u of m.links) items.push(h('button', { class: 'item', onclick: () => { closeMenus(); openUrl(u); } }, `Open ${u.slice(0, 70)}`));
  if (!items.length) items.push(h('div', { class: 'note' }, 'Nothing to run yet. Press Set up first.'));
  if (m.windows && !m.sandbox && m.runnables.length) items.push(h('div', { class: 'sep' }),
    h('div', { class: 'note' }, 'Windows Sandbox is off. Turn it on in "Turn Windows features on or off" (Windows Pro) to run builds safely.'));
  menu.replaceChildren(...items);
}

async function runIt(sid, index, label) {
  let r = await act(`/api/ship/${sid}/run`, { index });
  if (r?.need_direct) {
    const ok = await ask(`Windows Sandbox isn't available on this PC, so ${label} would run directly on your computer, with access to your files and the internet. Only do this for builds you trust. Run it anyway?`, { yes: 'Run without a sandbox' });
    if (!ok) return;
    r = await act(`/api/ship/${sid}/run`, { index, direct: true });
  }
  if (r?.job) openJob(r.job);
}

async function flushNotes() {
  if (!S.notesTimer) return;
  clearTimeout(S.notesTimer);
  S.notesTimer = null;
  const sid = S.notesFor, ta = $('#notes');
  if (!sid || !ta) return;
  try {
    await post(`/api/ship/${sid}/notes`, { notes: ta.value });
    if ($('#notes-saved')) $('#notes-saved').textContent = 'Saved';
    if (S.detail && S.detail.sid === sid) S.detail.notes = ta.value;
  } catch (e) { say(`Notes NOT saved: ${e.message}`, true); }
}
window.addEventListener('beforeunload', () => { flushNotes(); flushDraft(); });

// ---- step 3: decide ----
function guideFor(key) { return S.rubric?.fields?.[key]; }
function dirtyFor(sid) { return (S.dirty[sid] ||= {}); }

function fieldHelp(key) {
  const g = guideFor(key);
  if (!g) return null;
  return h('div', { class: 'help' }, g.help,
    g.points ? h('ul', {}, g.points.map((p) => h('li', {}, p))) : null,
    g.example ? h('details', { class: 'example' }, h('summary', {}, 'Show an example'), h('pre', {}, g.example)) : null);
}

function renderDecide(d) {
  const kids = [];
  const notes = (S.detail.notes || '').trim();
  kids.push(h('div', { class: 'section' }, h('h2', {}, 'Your notes', h('span', { class: 'grow' }), h('button', { class: 'link', onclick: () => showStep(1) }, 'Edit')),
    notes ? h('div', { class: 'notes-preview' }, notes) : h('p', { class: 'muted' }, 'No notes yet. Write them on the Test it step.')));

  const verdicts = (S.rubric?.verdicts || []).map((v) => h('button', {
    class: 'verdict' + (d.draft && d.kind === v.kind ? ' on' : ''), style: { '--tint': TINT[v.kind] }, onclick: () => switchVerdict(v.kind),
  }, h('b', {}, v.label), h('span', {}, v.when)));
  const pick = h('div', { class: 'section' }, h('h2', {}, 'Choose a verdict'),
    h('p', { class: 'lead' }, 'Picking one fills in its template below. You can switch later; the note text carries over.'),
    h('div', { class: 'verdicts' }, verdicts));
  kids.push(pick);

  if (!d.draft) {
    if (d.suggested_html !== undefined) kids.push(h('div', { class: 'section' }, h('h2', {}, 'There is a suggested draft'),
      h('div', { class: 'content', html: d.suggested_html }),
      h('div', { class: 'row' }, h('button', { onclick: () => act(`/api/ship/${d.sid}/use-suggested`) }, 'Use the suggested draft'))));
    if (d.decision_actions.length) kids.push(h('div', { class: 'section' }, h('div', { class: 'actionbar' }, actions(d))));
    $('#step-2').replaceChildren(...kids);
    return;
  }

  const dirty = dirtyFor(d.sid);
  const form = h('div', { class: 'section' }, h('h2', {}, 'Write the review', h('span', { class: 'badge', style: { '--tint': TINT[d.kind] } }, d.badge),
    h('span', { class: 'grow' }), h('span', { class: 'saved', id: 'draft-saved' }, 'Saved')));
  if (d.kind === 'approve') form.append(approveNumbers(d, dirty));
  if (d.kind === 'move') {
    const sel = h('select', { onchange: (e) => { dirty.move_to = e.target.value; draftChanged(); } },
      h('option', { value: 'fraud' }, 'Fraud review'), h('option', { value: 'admin' }, 'An admin'));
    sel.value = dirty.move_to ?? d.move_to;
    form.append(h('div', { class: 'form-field' }, h('label', {}, 'Move it to'), sel));
  }
  if (d.kind === 'changes') {
    form.append(h('div', { class: 'notice info' }, h('b', {}, 'Clear reasons to request changes'),
      h('ul', {}, (S.rubric?.changes_reasons || []).map((r) => h('li', {}, r))),
      h('div', { class: 'small', style: { marginTop: '8px' } }, h('b', {}, "Don't request changes for: "), (S.rubric?.changes_dont || []).join(' '))));
  }
  for (const f of d.fields) {
    if (f.key === 'deflation reason' && d.kind === 'approve') continue;       // shown next to the hours cut
    const ta = h('textarea', { 'data-key': f.key, rows: f.player ? 7 : 4, oninput: (e) => { dirty['f:' + f.key] = e.target.value; draftChanged(); } });
    ta.value = dirty['f:' + f.key] ?? f.text;
    form.append(h('div', { class: 'form-field' }, h('label', {}, f.label, f.player ? h('span', { class: 'muted small' }, '  (the player reads this)') : ''), fieldHelp(f.key), ta));
  }
  if (d.remarks_html) {
    form.append(h('div', { class: 'notice' }, h('div', { class: 'row' }, h('b', { class: 'grow' }, 'Noticed while drafting'),
      h('button', { onclick: () => act(`/api/ship/${d.sid}/clear-remarks`) }, 'Clear')), h('div', { class: 'content', html: d.remarks_html })));
  }
  if (d.kind === 'approve') {
    form.append(h('div', { class: 'form-field' }, h('label', {}, 'Test video'),
      h('div', { class: 'help' }, 'A recording of your test. It is shrunk to under 20 MB and uploaded with the approve.'),
      h('div', { class: 'row' }, h('span', { id: 'video-label' }, d.video ? `${d.video.name} (${d.video.mb} MB)` : 'No video yet'),
        h('button', { onclick: () => $('#video-file').click() }, d.video ? 'Replace video…' : 'Attach video…'),
        d.video_set ? h('button', { onclick: () => act(`/api/ship/${d.sid}/remove-video`) }, 'Remove') : null)));
  }
  kids.push(form);

  const send = h('div', { class: 'section' }, h('h2', {}, 'Send it'));
  if (d.template_left) {
    send.append(h('div', { class: 'notice' }, h('b', {}, 'Replace these reminders in (parentheses) before queueing:'),
      h('ul', { class: 'small' }, (d.template_left_all || [d.template_left]).map((x) => h('li', {}, x)))));
  } else {
    send.append(h('p', { class: 'lead' }, 'Queueing checks your draft against the live ship, then adds it to the send queue. The sender (top bar) sends it.'));
  }
  send.append(h('div', { class: 'actionbar' },
    h('button', { class: 'primary', onclick: queue, title: 'Ctrl+Enter' }, 'Queue for sending'),
    h('button', { onclick: () => saveDraft(true) }, 'Save draft'),
    h('span', { class: 'grow' }),
    h('button', { onclick: () => act(`/api/ship/${d.sid}/unqueue`) }, 'Take out of send queue'),
    h('button', { class: 'danger', onclick: deleteDraft }, 'Delete draft')));
  if (d.decision_actions.length) send.append(h('div', { class: 'actionbar' }, actions(d)));
  kids.push(send);
  $('#step-2').replaceChildren(...kids);
}

function approveNumbers(d, dirty) {
  const levels = S.rubric?.multipliers || [];
  const value = +(dirty.multiplier ?? d.multiplier);
  const known = levels.some((l) => Math.abs(l.value - value) < 1e-9);
  const sel = h('select', { id: 'mult', style: { maxWidth: '100%' } },
    levels.map((l) => h('option', { value: String(l.value) }, `x${l.value} – ${l.text}`)),
    h('option', { value: 'custom' }, 'Another number…'));
  const custom = h('input', { type: 'number', id: 'mult-custom', step: '0.05', min: '0.5', max: '2', value: String(value), hidden: known });
  sel.value = known ? String(levels.find((l) => Math.abs(l.value - value) < 1e-9).value) : 'custom';
  sel.onchange = () => { custom.hidden = sel.value !== 'custom'; dirty.multiplier = sel.value === 'custom' ? custom.value : sel.value; draftChanged(); };
  custom.oninput = () => { dirty.multiplier = custom.value; draftChanged(); };

  const cut = h('input', { type: 'number', id: 'cut', step: '0.5', min: '0', max: '50', value: String(dirty.cut ?? d.cut) });
  const reasonField = (d.fields.find((f) => f.key === 'deflation reason') || {}).text || '';
  const reason = h('textarea', { id: 'cut-reason', rows: 2, placeholder: 'One line: why you cut the hours' });
  reason.value = dirty['f:deflation reason'] ?? reasonField;
  const reasonBox = h('div', { class: 'form-field' }, h('label', {}, 'Why you cut hours'), fieldHelp('deflation reason'), reason);
  reasonBox.hidden = !(+cut.value > 0);
  cut.oninput = () => { dirty.cut = cut.value; reasonBox.hidden = !(+cut.value > 0); draftChanged(); };
  reason.oninput = () => { dirty['f:deflation reason'] = reason.value; draftChanged(); };

  const deflation = h('details', { class: 'example' }, h('summary', {}, 'When to cut hours'),
    h('ul', {}, (S.rubric?.deflation || []).map((x) => h('li', {}, h('b', {}, x.when + ': '), x.do))));
  return h('div', { class: 'two' },
    h('div', { class: 'form-field' }, h('label', { for: 'mult' }, 'Multiplier'),
      h('div', { class: 'help' }, S.rubric?.multiplier_intro || '', d.hints?.length ? h('div', {}, h('b', {}, d.hints.join(' · '))) : null,
        h('details', { class: 'example' }, h('summary', {}, 'How AI use changes it'), h('pre', {}, S.rubric?.ai_note || ''))),
      sel, custom),
    h('div', { class: 'stack' }, h('div', { class: 'form-field' }, h('label', { for: 'cut' }, 'Hours cut'),
      h('div', { class: 'help' }, '0 if you keep all the hours. Testing time never counts.'), cut, deflation), reasonBox));
}

function actions(d) {
  return d.decision_actions.map((a) => h('button', { onclick: async () => { await saveDraft(false); act(`/api/ship/${d.sid}/action`, { kind: 'decision', index: a.index }); } }, a.label));
}

function draftChanged() {
  const el = $('#draft-saved');
  if (el) el.textContent = 'Unsaved changes…';
  clearTimeout(S.draftTimer);
  S.draftTimer = setTimeout(() => saveDraft(false, true), 1500);
}
async function flushDraft() { if (S.draftTimer) { clearTimeout(S.draftTimer); S.draftTimer = null; await saveDraft(false, true); } }

async function saveDraft(loud, auto) {
  clearTimeout(S.draftTimer);
  S.draftTimer = null;
  const d = S.detail;
  if (!d || !d.draft) return true;
  const dirty = dirtyFor(d.sid);
  const fields = {};
  for (const [k, v] of Object.entries(dirty)) if (k.startsWith('f:')) fields[k.slice(2)] = v;
  const body = { fields };
  if (d.kind === 'approve') {
    body.multiplier = +(dirty.multiplier ?? d.multiplier);
    body.cut = +(dirty.cut ?? d.cut);
    if (fields['deflation reason']) body.reason = fields['deflation reason'];
  }
  if (d.kind === 'move' && dirty.move_to) body.move_to = dirty.move_to;
  if (!Object.keys(dirty).length) { if (loud) say('Nothing changed'); return true; }
  let r;
  try { r = await post(`/api/ship/${d.sid}/draft`, body); } catch (e) { say(e.message, true); return false; }
  const el = $('#draft-saved');
  if (r.need_reason) {
    if (el) el.textContent = 'Add why you cut the hours to save';
    if (!auto) { say('Write why you cut the hours first', true); $('#cut-reason')?.focus(); }
    return false;
  }
  delete S.dirty[d.sid];
  if (el) el.textContent = 'Saved';
  if (loud) say('Draft saved');
  return true;
}

async function switchVerdict(kind) {
  const d = S.detail;
  if (!d || (d.draft && d.kind === kind)) return;
  if (d.draft && !(await ask(`Switch to ${KIND_LABEL[kind]}? The draft starts again from its template (the note text carries over).`, { yes: 'Switch' }))) return;
  delete S.dirty[d.sid];
  clearTimeout(S.draftTimer); S.draftTimer = null;
  await act(`/api/ship/${d.sid}/verdict`, { kind });
  await loadDetail(false);
}

async function queue() {
  const d = S.detail;
  if (!d || !d.draft) { say('Choose a verdict first'); return; }
  if (d.kind === 'skip') { say('Skips are staff notes: post that one by hand'); return; }
  if (!(await saveDraft(false))) return;
  await loadDetail(false);
  const dd = S.detail;
  const extra = dd.kind === 'approve' ? ` at x${dd.multiplier}${dd.cut ? `, cutting ${dd.cut}h` : ''}` : '';
  if (!(await ask(`Queue "${dd.badge}"${extra} for ${dd.title}?`, { yes: 'Queue it' }))) return;
  say('Queueing…');
  await act(`/api/ship/${dd.sid}/queue`);
  await loadDetail(false);
}

async function deleteDraft() {
  const d = S.detail;
  if (d && (await ask('Delete this draft? Your notes stay.', { yes: 'Delete draft' }))) {
    delete S.dirty[d.sid];
    clearTimeout(S.draftTimer); S.draftTimer = null;
    await act(`/api/ship/${d.sid}/delete-draft`);
  }
}

async function skip() {
  const d = S.detail;
  if (!d) return;
  if (!d.skipped && !(await ask(`Hide ${d.title} from "Waiting for review"? You can find it under "Skipped".`, { yes: 'Skip' }))) return;
  await act(`/api/ship/${d.sid}/skip`, { skipped: !d.skipped });
}

async function claimShip() {
  const d = S.detail;
  if (!d) return;
  if (S.status.runner && S.status.send_queue &&
      !(await ask('The sender is working through your queue, and Terra only lets you hold one claim. Claiming now can make its next send fail. Claim anyway?', { yes: 'Claim' }))) return;
  await act(`/api/ship/${d.sid}/claim`);
}

$('#video-file').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  e.target.value = '';
  const d = S.detail;
  if (!file || !d) return;
  const label = $('#video-label');
  if (label) label.textContent = 'Uploading…';
  try {
    await api('POST', `/api/ship/${d.sid}/video`, undefined, { body: file, headers: { 'X-Filename': encodeURIComponent(file.name), 'Content-Type': 'application/octet-stream' } });
    if (label) label.textContent = 'Shrinking the video… (it shows here when ready)';
  } catch (err) { say(`Video failed: ${err.message}`, true); if (label) label.textContent = 'No video yet'; }
});

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

// ---- the review guide ----
async function loadRubric() {
  try { S.rubric = await api('GET', '/api/rubric'); } catch (e) { S.rubric = null; }
  renderGuide();
}
function renderGuide() {
  const r = S.rubric;
  if (!r) return;
  const sec = (id, title, ...body) => h('details', { id: `g-${id}`, open: id === 'verdicts' }, h('summary', {}, title), h('div', { class: 'gbody' }, body));
  $('#guide').replaceChildren(
    h('div', { class: 'row' }, h('h2', { class: 'grow' }, 'Review guide'), h('button', { onclick: toggleGuide }, 'Close')),
    h('p', { class: 'muted' }, 'The rubric to follow. Open a section to read it.'),
    sec('verdicts', 'Which verdict?', h('table', {}, r.verdicts.map((v) => h('tr', {}, h('td', { style: { color: TINT[v.kind] } }, v.label), h('td', {}, v.when))))),
    sec('changes', 'Request changes', h('b', {}, 'Clear reasons:'), h('ul', {}, r.changes_reasons.map((x) => h('li', {}, x))),
      h('b', {}, "Don't request changes for:"), h('ul', {}, r.changes_dont.map((x) => h('li', {}, x))),
      h('b', {}, 'Example response:'), h('pre', {}, r.changes_example)),
    sec('deflation', 'Approve: hours cut (deflation)', h('table', {}, r.deflation.map((x) => h('tr', {}, h('td', { style: { whiteSpace: 'normal' } }, x.when), h('td', {}, x.do))))),
    sec('multiplier', 'Approve: multiplier', h('p', {}, r.multiplier_intro),
      h('table', {}, r.multipliers.map((m) => h('tr', {}, h('td', {}, `x${m.value}${m.value === 1.3 ? '+' : ''}`), h('td', {}, m.text))))),
    sec('ai', 'AI use', h('p', {}, r.ai_note)),
    sec('features', 'Specific technical features', h('p', {}, r.fields['specific technical features'].help), h('p', { class: 'muted' }, r.fields['specific technical features'].example)),
    sec('justification', 'Airtable justification', h('p', {}, r.fields['airtable justification'].help),
      h('ul', {}, r.fields['airtable justification'].points.map((x) => h('li', {}, x))), h('b', {}, 'Example (leave out any part that doesn\'t apply):'),
      h('pre', {}, r.fields['airtable justification'].example)),
    sec('note', 'Note to player', h('p', {}, r.fields['note to player'].help), h('ul', {}, r.fields['note to player'].points.map((x) => h('li', {}, x))),
      h('b', {}, 'Example (leave out any part that doesn\'t apply):'), h('pre', {}, r.fields['note to player'].example)));
}
function toggleGuide() {
  const g = $('#guide');
  g.hidden = !g.hidden;
  $('#app').classList.toggle('with-guide', !g.hidden);
  $('#btn-guide').classList.toggle('primary', !g.hidden);
  try { localStorage.setItem('guide', g.hidden ? '0' : '1'); } catch (e) { /* ok */ }
}
$('#btn-guide').onclick = toggleGuide;

// ---- menus ----
function closeMenus() { $$('.menu.open').forEach((m) => m.classList.remove('open')); }
document.addEventListener('click', (e) => { if (!e.target.closest('.menu-wrap')) closeMenus(); });
$('#btn-more').onclick = (e) => {
  e.stopPropagation();
  const menu = $('#more-menu');
  const was = menu.classList.contains('open');
  closeMenus();
  if (was) return;
  const item = (label, fn, title) => h('button', { class: 'item', title, onclick: () => { closeMenus(); fn(); } }, label);
  menu.replaceChildren(
    item('Send queued reviews…', openSending),
    item('Check all shown ships', checkShown, 'Run the automatic checks on every ship in the list (read-only)'),
    item('Reload my markdown pages', () => act('/api/reload'), 'F5'),
    item('Save the whole queue to a file', dumpQueue, 'Writes every waiting ship to a markdown file (read-only, takes ~12 min)'),
    item('Stash my work…', openStash, 'Put all your work aside to start fresh, or bring it back'),
    h('div', { class: 'sep' }),
    item('Quit terra-desk', quit));
  menu.classList.add('open');
};

async function checkShown() {
  const sids = visible().filter((s) => s.in_queue && !s.check).map((s) => s.sid);
  if (!sids.length) { say('Every shown ship is already checked'); return; }
  if (sids.length > 25 && !(await ask(`Check ${sids.length} ships? Each one downloads its code (read-only, nothing runs), one at a time.`, { yes: 'Check them' }))) return;
  act('/api/check-shown', { sids });
}
async function dumpQueue() { const r = await act('/api/dump'); if (r?.job) openJob(r.job); }
async function quit() {
  if (!(await ask('Close terra-desk? Your claim is released. The sender keeps going if it is on.', { yes: 'Quit' }))) return;
  await flushNotes();
  await flushDraft();
  await act('/api/quit');
  document.body.replaceChildren(h('div', { class: 'card', style: { margin: '48px auto', maxWidth: '520px' } }, h('h2', {}, 'terra-desk is closed'),
    h('p', { class: 'muted', style: { marginTop: '10px' } }, 'You can close this window. Start it again from the Start menu.')));
}

// ---- sending ----
function openSending() {
  const s = S.status;
  const body = [h('p', {}, `${s.send_queue} review${s.send_queue === 1 ? '' : 's'} waiting to send.`),
    h('p', { class: 'muted' }, 'The sender sends one review every few minutes, claiming each ship right before it goes out. It keeps going if you close the window.')];
  if (!s.runner) {
    let every = '4';
    try { every = localStorage.getItem('every') || '4'; } catch (e) { /* ok */ }
    const ev = h('input', { type: 'number', value: every, min: '0.5', step: '0.5' });
    const hold = h('select', {}, h('option', { value: '1' }, 'Claim the next ship during the wait (you can\'t review in the browser meanwhile)'),
      h('option', { value: '0' }, 'Only claim each ship as it is sent'));
    body.push(h('div', { class: 'form-grid' }, 'Minutes between sends', ev, 'Claims', hold),
      h('div', { class: 'row' }, h('button', { class: 'primary', onclick: async () => {
        try { localStorage.setItem('every', ev.value); } catch (e) { /* ok */ }
        const r = await act('/api/runner/start', { every: +ev.value, hold: +hold.value }, 'Sender started');
        $('#dialog').close();
        if (r?.job) openJob(r.job);
      } }, 'Start sending')));
  } else {
    const control = (word, label) => h('button', { onclick: async () => { await act('/api/runner/control', { word }); $('#dialog').close(); } }, label);
    body.push(h('div', { class: 'row' }, control('pause', 'Pause'), control('resume', 'Resume'),
      h('button', { onclick: async () => { const m = await ask('Minutes between sends:', { input: '4' }); if (m) act('/api/runner/control', { word: m }); } }, 'Change the gap…'),
      control('stop', 'Stop (gives back its claims)'),
      s.runner_job ? h('button', { onclick: () => { $('#dialog').close(); openJob(s.runner_job); } }, 'Show what it is doing') : null));
  }
  openDialog('Send queued reviews', body);
}

// ---- stash ----
async function openStash() {
  const { stashes } = await api('GET', '/api/stashes');
  const rows = stashes.map((x) => h('div', { class: 'stash-row' },
    h('div', { class: 'grow' }, h('b', {}, x.when), h('div', { class: 'muted small' }, `${x.drafts} drafts, ${x.notes} with notes, ${x.pages} pages`)),
    h('button', { onclick: async () => { if (await ask(`Bring back the stash from ${x.when}? What you have now is stashed first, so nothing is lost.`, { yes: 'Bring it back' })) { await act('/api/stash/pop', { name: x.path }); openStash(); } } }, 'Bring back'),
    h('button', { class: 'danger', onclick: async () => { if (await ask(`Delete the stash from ${x.when} for good?`, { yes: 'Delete' })) { await act('/api/stash/drop', { name: x.path }); openStash(); } } }, 'Delete')));
  openDialog('Stash my work',
    h('p', { class: 'muted' }, 'A stash puts all your work aside (notes, drafts, videos, skips, imported pages) so you can start fresh, and brings it back later. Queued and sent reviews are not affected.'),
    h('div', { class: 'row' }, h('button', { class: 'primary', onclick: async () => {
      await flushNotes(); await flushDraft();
      if (await ask('Put all your work aside and start fresh?', { yes: 'Stash it' })) { await act('/api/stash'); openStash(); }
    } }, 'Stash everything now')),
    h('h3', {}, 'Saved stashes'), rows.length ? rows : h('p', { class: 'muted' }, 'No stashes yet.'));
}

// ---- activity (jobs) ----
function showJobsList(list) {
  S.jobs = list;
  const running = list.filter((j) => j.running).length;
  $('#activity-count').textContent = running ? String(running) : '';
  $('#job-list').replaceChildren(...(list.length ? list.slice().reverse().map((j) => h('li', {}, h('button', { class: j.id === S.job ? 'on' : '', onclick: () => openJob(j.id) },
    h('span', {}, j.title.slice(0, 40)), h('span', { class: 'state' }, j.running ? 'running…' : j.code ? `stopped with an error (${j.code})` : 'finished'))))
    : [h('li', { class: 'muted small' }, 'Nothing has run yet.')]));
}
function openJob(id) {
  S.job = id;
  $('#activity').hidden = false;
  showJobsList(S.jobs);
  refreshJob(true);
}
let jobTimer = null;
async function refreshJob(force) {
  clearTimeout(jobTimer);
  if (!S.job || $('#activity').hidden) return;
  try {
    const j = await api('GET', `/api/jobs/${S.job}`);
    const log = $('#job-log');
    const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
    $('#job-title').textContent = j.title;
    if (force || log.textContent !== j.text) { log.textContent = j.text; if (atEnd || force) log.scrollTop = log.scrollHeight; }
    $('#job-stop').disabled = !j.running;
    $('#job-close').disabled = j.running;
    $('#job-input').disabled = !j.running;
    if (j.running) jobTimer = setTimeout(refreshJob, 1000);
  } catch (e) { S.job = null; }
}
$('#job-stop').onclick = () => S.job && act(`/api/jobs/${S.job}/stop`);
$('#job-close').onclick = async () => { if (S.job) { await act(`/api/jobs/${S.job}/close`); S.job = null; $('#job-log').textContent = ''; $('#job-title').textContent = ''; } };
$('#job-outside').onclick = () => S.job && act(`/api/jobs/${S.job}/outside`);
$('#activity-close').onclick = () => { $('#activity').hidden = true; };
$('#job-input-form').addEventListener('submit', (e) => {
  e.preventDefault();
  if (S.job) { post(`/api/jobs/${S.job}/input`, { text: $('#job-input').value }); $('#job-input').value = ''; setTimeout(refreshJob, 200); }
});
function toggleActivity() {
  const el = $('#activity');
  el.hidden = !el.hidden;
  if (!el.hidden && !S.job && S.jobs.length) openJob(S.jobs[S.jobs.length - 1].id);
  else refreshJob(true);
}
$('#btn-activity').onclick = toggleActivity;
$('#btn-refresh').onclick = () => act('/api/refresh');
$('#btn-settings').onclick = () => openSettings();

// ---- filters ----
for (const id of ['view', 'week', 'sort']) $('#' + id).addEventListener('change', () => { savePrefs(); fillList(); });
$('#search').addEventListener('input', fillList);
$('#platforms').replaceChildren(...PLATFORMS.map((p) => {
  const box = h('input', { type: 'checkbox', value: p, onchange: () => { box.checked ? S.platforms.add(p) : S.platforms.delete(p); savePrefs(); fillList(); } });
  return h('label', {}, box, p);
}));
function savePrefs() {
  try { localStorage.setItem('prefs', JSON.stringify({ view: $('#view').value, week: $('#week').value, sort: $('#sort').value, platforms: [...S.platforms] })); } catch (e) { /* ok */ }
}
function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem('prefs') || '{}');
    for (const k of ['view', 'week', 'sort']) if (p[k] && [...$('#' + k).options].some((o) => o.value === p[k])) $('#' + k).value = p[k];
    S.platforms = new Set(p.platforms || []);
    $$('#platforms input').forEach((b) => { b.checked = S.platforms.has(b.value); });
    if (S.platforms.size || p.week && p.week !== 'all weeks') $('#filters').open = true;
    S.step = +(localStorage.getItem(STEP_KEY) || 0);
    if (localStorage.getItem('guide') === '1') toggleGuide();
  } catch (e) { /* ok */ }
}

// ---- keys ----
document.addEventListener('keydown', (e) => {
  const typing = e.target.matches('input, textarea, select');
  const ctrl = e.ctrlKey || e.metaKey;
  if (ctrl && e.key === 's') { e.preventDefault(); flushNotes(); saveDraft(true); }
  else if (ctrl && e.key === 'Enter') { e.preventDefault(); queue(); }
  else if (ctrl && e.key.toLowerCase() === 'r' && !e.shiftKey) { e.preventDefault(); act('/api/refresh'); }
  else if (e.key === 'F5') { e.preventDefault(); act('/api/reload'); }
  else if (ctrl && e.key === ',') { e.preventDefault(); openSettings(); }
  else if (ctrl && e.key === '`') { e.preventDefault(); toggleActivity(); }
  else if (!typing && !ctrl && (e.key === 'j' || e.key === 'J')) move(1);
  else if (!typing && !ctrl && (e.key === 'k' || e.key === 'K')) move(-1);
  else if (!typing && !ctrl && ['1', '2', '3'].includes(e.key) && S.cur) showStep(+e.key - 1);
  else if (e.key === 'Escape') closeMenus();
});

// ---- settings ----
async function openSettings(start = 'General') {
  const st = await api('GET', '/api/settings');
  const tabs = { General: general, 'Terra key': key, Templates: () => editor(st.templates) };
  if (st.prompts.length) tabs['Plugin texts'] = () => editor(st.prompts);
  const nav = h('nav', { class: 'tabs' });
  const body = h('div', { class: 'stack' });
  const show = (name) => {
    $$('button', nav).forEach((b) => b.classList.toggle('on', b.textContent === name));
    body.replaceChildren(...[tabs[name](st)].flat());
  };
  nav.append(...Object.keys(tabs).map((n) => h('button', { onclick: () => show(n) }, n)));
  openDialog('Settings', nav, body);
  show(tabs[start] ? start : 'General');
}

function general(st) {
  const handle = h('input', { value: st.handle });
  const terminal = h('input', { value: st.terminal, placeholder: st.windows ? 'automatic (Windows Terminal or cmd)' : 'automatic' });
  const win = h('select', {}, h('option', { value: 'app' }, 'Its own window'), h('option', { value: 'browser' }, 'A tab in my browser'));
  win.value = st.window;
  const extra = st.extra.map((x) => { const s = h('select', { title: x.help }, x.choices.map((c) => h('option', {}, c))); s.value = x.value; return [x, s]; });
  const save = h('button', { class: 'primary', onclick: () => act('/api/settings/general', { handle: handle.value, terminal: terminal.value, window: win.value,
    extra: Object.fromEntries(extra.map(([x, s]) => [x.key, s.value])) }) }, 'Save');
  const sandbox = st.windows ? h('div', { class: 'notice' + (st.sandbox ? ' good' : '') }, st.sandbox
    ? 'Windows Sandbox is on: Run starts builds inside it, with no internet and only their own folder.'
    : 'Windows Sandbox is off, so Run can only start builds directly on your PC (it asks first). On Windows Pro, Enterprise or Education, turn on "Windows Sandbox" in "Turn Windows features on or off" to run builds safely.') : null;
  return [h('div', { class: 'form-grid' },
    h('label', {}, 'Your Slack handle'), handle,
    h('label', {}, 'Open terra-desk in'), win,
    h('label', {}, 'Terminal for "Open in a terminal"'), terminal,
    extra.map(([x, s]) => [h('label', {}, x.label), s])),
  h('p', { class: 'muted' }, 'Your handle goes at the end of every note to a player.'), sandbox,
  h('p', { class: 'muted small' }, `Settings file: ${st.config_file}. Your work is in ${st.workspace}.`),
  h('div', { class: 'row' }, save)];
}

function key(st) {
  const state = h('div', { class: 'notice' });
  const field = h('input', { type: 'password', placeholder: 'Paste your Terra key here', autocomplete: 'off', style: { flex: '1' } });
  const remove = h('button', { class: 'danger' }, 'Remove key');
  const show = (s) => {
    state.className = 'notice' + (s === 'none' ? '' : ' good');
    state.textContent = { file: 'A key is saved.', env: 'The TERRA_TOKEN environment variable is set; it is used instead of a saved key.',
      none: "No key saved yet: the queue can't load without one." }[s];
    remove.disabled = s !== 'file';
  };
  const save = h('button', { class: 'primary', onclick: async () => { const r = await act('/api/settings/key', { key: field.value }, 'Key saved. Loading the queue…'); field.value = ''; if (r) show(r.token); } }, 'Save key');
  remove.onclick = async () => { if (await ask('Delete the saved Terra key from this computer?', { yes: 'Delete' })) { const r = await act('/api/settings/key', { remove: true }, 'Key removed'); if (r) show(r.token); } };
  field.onkeydown = (e) => { if (e.key === 'Enter') save.click(); };
  show(st.token);
  return [state, h('ol', {},
    h('li', {}, 'Open ', h('b', {}, 'terra.hackclub.com'), ' and sign in.'),
    h('li', {}, 'Press ', h('b', {}, 'F12'), ' and open the ', h('b', {}, 'Console'), ' tab.'),
    h('li', {}, 'Type ', h('code', {}, 'copy(localStorage.terra_auth_token)'), ' and press Enter. The key is now copied.'),
    h('li', {}, 'Paste it below and press Save key.')),
  h('div', { class: 'row' }, field, save, remove),
  h('p', { class: 'muted small' }, `It is saved to ${st.token_file}, which only your Windows account can read, and never shown again.`)];
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
    state.textContent = e.file ? '' : e.text !== e.default ? 'Edited by you' : 'The default';
    reset.textContent = e.file ? 'Reload from disk' : 'Reset to default';
  };
  save.onclick = async () => { if (!cur) return; const r = await act('/api/settings/text', { key: cur.key, text: ta.value }); if (r) { cur.text = ta.value; state.textContent = cur.file ? 'Saved' : ta.value !== cur.default ? 'Edited by you' : 'The default'; } };
  reset.onclick = async () => {
    if (!cur) return;
    if (cur.file) { ta.value = cur.text; return; }
    if (!(await ask(`Go back to the default ${cur.name}? Your edits are lost.`, { yes: 'Reset' }))) return;
    const r = await act('/api/settings/text', { key: cur.key, reset: true });
    if (r) { cur.text = r.text; ta.value = r.text; state.textContent = 'The default'; }
  };
  entries.forEach((e, i) => { const b = h('button', { onclick: () => pick(e, b) }, e.name); list.append(b); if (i === 0) setTimeout(() => pick(e, b)); });
  return h('div', { class: 'editor' }, list, h('div', { class: 'pane-r' }, help, ta, h('div', { class: 'row' }, state, h('span', { class: 'grow' }), reset, save)));
}

// ---- start ----
loadPrefs();
loadRubric().then(poll);
