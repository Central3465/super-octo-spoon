"""terra-desk's web app: a small server on this computer (127.0.0.1 only) that does the work, and a page in your
browser (terra_desk/web/) that shows it.

Only the Python standard library is used. Every /api request must carry the per-run key that is written into the
page, and the Host header must be this computer, so other websites you have open can't drive it.
"""
import collections
import hashlib
import json
import mimetypes
import os
import re
import secrets
import subprocess
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from . import core, db, jobs, plugins, prompts, review_page, rubric, system

WEB = Path(__file__).resolve().parent / 'web'
KIND_BADGE = {'approve': 'Approve', 'changes': 'Request changes', 'reject': 'Reject (for good)', 'move': 'Move review',
              'skip': 'Skip: staff note, post by hand'}
TEXT_FIELDS = ['note to player', 'what to change', 'airtable justification', 'specific technical features',
               'deflation reason', 'danger flag acknowledgement', 'update justification', 'staff note', 'move note']
PRETTY = {'verdict': 'Verdict', 'specific technical features': 'Specific technical features',
          'airtable deflation (hours)': 'Airtable deflation (hours)', 'crop deflation (hours)': 'Crop deflation (hours)',
          'deflation reason': 'Deflation reason', 'multiplier': 'Multiplier', 'airtable justification': 'Airtable justification',
          'note to player': 'Note to player', 'what to change': 'What to change',
          'danger flag acknowledgement': 'Danger flag acknowledgement', 'update justification': 'Update justification',
          'staff note': 'Staff note', 'move note': 'Move note'}
ORDER = ['verdict', 'specific technical features', 'airtable deflation (hours)', 'crop deflation (hours)', 'deflation reason',
         'multiplier', 'airtable justification', 'note to player', 'what to change', 'danger flag acknowledgement',
         'update justification', 'staff note', 'move note']
TEMPLATE_NAMES = {'approve': 'Approve', 'changes': 'Request changes', 'reject': 'Reject', 'move': 'Move review'}
VIDEO_TYPES = ('.mp4', '.mkv', '.webm', '.mov', '.avi')
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; media-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def fields_to_draft(f):
    keys = [k for k in ORDER if k in f] + [k for k in f if k not in ORDER]
    return core.build_draft({PRETTY.get(k, k): f[k] for k in keys})


class Fail(Exception):
    """An error to show the user (status 400)."""


class Tabs:
    """For plugins written for the desktop window: tabs.setCurrentIndex(n) switches the middle tab."""

    def __init__(self, desk):
        self.desk = desk

    def setCurrentIndex(self, n):  # noqa: N802 - the old Qt name
        self.desk.ui_events.append({'tab': int(n)})


class Desk:
    """Everything the page can ask for. Also what plugins get as `window` (cfg, store, hooks, page_json, say,
    reload, save_notes, tabs, copy)."""

    def __init__(self, cfg=None):
        self.cfg = cfg or core.load_config()
        self.tools = core.Tools(self.cfg)
        self.store = core.open_store(self.cfg)
        self.hooks, self.plugin_errors = plugins.load(self.cfg)
        for doc in self.cfg.get('documents') or []:
            path = Path(doc).expanduser()
            self.hooks.prompts.setdefault(path.name, {'path': path, 'help': f'Your document {path} (edited in place).'})
        self.jobs = jobs.Jobs()
        self.lock = threading.RLock()
        self.ships, self.by_key = [], {}
        self.version = 0                       # bumps when the ship list changes
        self.rev = collections.Counter()       # bumps when one ship's details change
        self.messages = collections.deque(maxlen=200)
        self.msg_id = 0
        self.ui_events = collections.deque(maxlen=50)
        self.claimed = None
        self.cur_sid = None
        self.check_queue, self.checking = [], None
        self.check_wake = threading.Condition()
        self.images = {}                       # image url -> local path
        self.files = {}                        # token -> local path the page may load
        self.fetching = set()
        self.run_menus = {}
        self.dumping = False
        self.runner_job = None
        self.tabs = Tabs(self)
        self.stopping = False
        try:
            self.tools.W                       # load terra-write.py now
        except Exception as e:  # noqa: BLE001
            self.say(f'could not load terra-write.py: {e}')
        for err in self.plugin_errors:
            self.say(f'plugin failed to load: {err}')
        core.import_pages(self.cfg, self.store, self.hooks)
        self.reload()

    # ---- plumbing ----
    def say(self, msg, *_):
        with self.lock:
            self.msg_id += 1
            self.messages.append({'id': self.msg_id, 'at': time.time(), 'text': str(msg)})

    def copy(self, text):
        """Put text on the clipboard (the page does it)."""
        self.ui_events.append({'copy': str(text)})

    def background(self, fn, done=None):
        def run():
            try:
                res, err = fn(), None
            except Exception as e:  # noqa: BLE001 - shown to the user
                res, err = None, e
            if done:
                try:
                    done(res, err)
                except Exception as e:  # noqa: BLE001
                    self.say(f'error: {e}')
        threading.Thread(target=run, daemon=True).start()

    def serve_file(self, path):
        path = str(path)
        token = hashlib.sha256(path.encode()).hexdigest()[:20]
        self.files[token] = path
        return f'/file/{token}/{urllib.parse.quote(Path(path).name)}'

    def bump(self, sid=None):
        with self.lock:
            if sid:
                self.rev[sid] += 1

    # ---- the list ----
    def reload(self, keep=None):
        with self.lock:
            self.ships = core.build_ships(self.cfg, self.store, self.hooks)
            self.by_key = {s.sid: s for s in self.ships}
            self.version += 1
            for s in self.ships:
                self.rev[s.sid] += 0

    def ship(self, sid):
        s = self.by_key.get(sid)
        if not s:
            raise Fail('no such ship')
        return s

    def refresh_one(self, sid):
        self.reload()
        self.rev[sid] += 1

    def refetch(self):
        def done(res, err):
            if err:
                self.say(f'could not load the queue: {err}')
                return
            self.store.sync_queue(res)
            self.reload()
            self.say(f'{len(res)} ships in the queue')
        self.background(lambda: core.terra_waiting(self.tools), done)

    def reimport(self):
        if core.import_pages(self.cfg, self.store, self.hooks):
            self.reload()
            return True
        return False

    def check_level(self, s):
        if self.checking == s.sid or s.sid in self.check_queue:
            return 'running'
        chk, _ = self.store.checks(s.sid)
        return chk.get('level') if chk else None

    @staticmethod
    def mine_label(s):
        if s.state == 'drafted':
            return {'approve': 'approve', 'changes': 'changes', 'reject': 'reject', 'move': 'move',
                    'skip': 'skip note'}.get(s.verdict_kind, 'drafted')
        if s.state == 'new':
            return 'suggested' if s.suggested else 'page' if s.page_text else 'steps' if s.quick else ''
        return s.state

    def rows(self):
        out = []
        for s in self.ships:
            item = s.item or {}
            out.append({'sid': s.sid, 'num': s.num, 'title': s.title, 'username': s.username, 'week': s.week,
                        'state': s.state, 'skipped': s.skipped, 'kind': s.verdict_kind if s.draft else '',
                        'draft': bool(s.draft), 'notes': bool(s.notes.strip()), 'suggested': bool(s.suggested),
                        'page': bool(s.page_text), 'quick': bool(s.quick), 'in_queue': s.item is not None,
                        'queuedAt': item.get('queuedAt') or '', 'seconds': item.get('seconds') or 0,
                        'platforms': item.get('platforms') or [], 'mine': self.mine_label(s),
                        'check': self.check_level(s), 'waiting': review_page.ago(item.get('queuedAt')) if item else '',
                        'claimed_by': item.get('claimedBy') if item.get('claimedBy') and not item.get('claimedByMe') else None})
        return out

    def status(self):
        return {'drafted': sum(1 for s in self.ships if s.state == 'drafted'),
                'notes': sum(1 for s in self.ships if s.state == 'notes'),
                'send_queue': len(core.queued_sids(self.cfg)), 'runner': core.runner_alive(self.cfg),
                'failed': len(core.failed_sends(self.cfg)),
                'runner_job': self.runner_job.id if self.runner_job and self.runner_job.running else None,
                'in_queue': sum(1 for s in self.ships if s.item), 'checking': self.checking,
                'checks_waiting': len(self.check_queue), 'claimed': self.claimed, 'token': core.token_state(),
                'dumping': self.dumping}

    def state(self, since=0):
        with self.lock:
            msgs = [m for m in self.messages if m['id'] > since]
            events = list(self.ui_events)
            self.ui_events.clear()
        cur = self.cur_sid
        return {'version': self.version, 'status': self.status(), 'messages': msgs, 'events': events,
                'jobs': self.jobs.list(), 'cur_rev': self.rev.get(cur, 0) if cur else 0}

    # ---- one ship ----
    @property
    def page_json(self):
        return self.store.page(self.cur_sid) if self.cur_sid else None

    def fraud_hours(self, page):
        fs = (page or {}).get('fraudSuggestion') or {}
        return round((fs.get('seconds') or 0) / 3600, 2)

    def fraud_reason(self, page):
        reason = ((page or {}).get('fraudSuggestion') or {}).get('reason')
        return f'Fraud review: {reason}' if reason else ''

    def fraud_cut(self, page, cut):
        return bool(self.fraud_hours(page)) and abs(cut - self.fraud_hours(page)) < 0.01

    def load_page(self, s):
        """Fetch the review-page data in the background when it isn't cached (or is older than 10 minutes)."""
        if s.sid in self.fetching or self.store.page(s.sid, max_age=600) is not None:
            return
        self.fetching.add(s.sid)

        def done(res, err):
            self.fetching.discard(s.sid)
            if err:
                if not self.store.page(s.sid):
                    self.say(f'could not load {s.title}: {err}')
                return
            self.rev[s.sid] += 1
            urls = [u for u in core.image_urls(res) if u not in self.images]
            if urls:
                def got(res2, err2):
                    if res2:
                        self.images.update(res2)
                        self.rev[s.sid] += 1
                self.background(lambda: core.fetch_images(self.cfg, urls), got)
        self.background(lambda: core.fetch_page(self.store, self.tools, s.sid, max_age=600), done)

    def page_html(self, s, page):
        if not page:
            return f'<p class="muted">loading {review_page.e(s.title)} from Terra (read-only)…</p>'
        for u in core.image_urls(page):
            p = core.image_path(self.cfg, u)
            if p.exists():
                self.images[u] = p
        chk, _ = self.store.checks(s.sid)
        imgs = {u: self.serve_file(p) for u, p in self.images.items() if Path(p).exists()}
        return review_page.render(page, chk, imgs, checking=self.check_level(s) == 'running')

    def test_segments(self, s, page):
        demo = ((page or {}).get('project') or {}).get('demoUrl')
        text = s.quick or ('Press **Set up** to clone the repo and download the release (nothing runs), then use **Run**.'
                           + (f'\n\nDemo: {demo}' if demo else ''))
        out, pos = [], 0
        for m in re.finditer(r'^```[^\n]*\n(.*?)^```[ \t]*$', text, re.S | re.M):
            before = text[pos:m.start()].strip()
            if before:
                out.append({'html': review_page.md(before)})
            code = m.group(1).rstrip('\n')
            lines = [ln.strip() for ln in code.splitlines() if ln.strip()]
            if lines and all(core.URL.fullmatch(ln) for ln in lines):
                out.append({'html': review_page.md('\n\n'.join(lines))})
            else:
                out.append({'code': code})
            pos = m.end()
        rest = text[pos:].strip()
        if rest:
            out.append({'html': review_page.md(rest)})
        return out

    def screenshots(self, s):
        dirs = [self.cfg['workspace'] / 'screenshots' / s.sid]
        for fn in self.hooks.screenshot_dirs:
            try:
                dirs += fn(s) or []
            except Exception as e:  # noqa: BLE001
                self.say(f'plugin screenshot_dirs failed: {e}')
        return [p for d in dirs if Path(d).is_dir() for p in sorted(Path(d).glob('*.png'))]

    def detail(self, sid):
        s = self.ship(sid)
        self.cur_sid = sid
        page = self.store.page(sid)
        self.load_page(s)
        if s.item:
            chk, at = self.store.checks(sid)
            if chk is None or time.time() - (at or 0) > 12 * 3600:
                self.enqueue_check(sid, front=True)
        item = s.item or {}
        if self.claimed == sid:
            claim = {'state': 'mine'}
        elif item.get('claimedBy') and not item.get('claimedByMe'):
            claim = {'state': 'other', 'by': item.get('claimedBy')}
        elif s.item:
            claim = {'state': 'free', 'terra_says_mine': bool(item.get('claimedByMe'))}
        else:
            claim = {'state': 'none'}
        d = {'sid': sid, 'num': s.num, 'title': s.title, 'username': s.username, 'week': s.week, 'state': s.state,
             'skipped': s.skipped, 'notes': s.notes, 'draft': bool(s.draft), 'kind': s.verdict_kind if s.draft else '',
             'claim': claim, 'rev': self.rev.get(sid, 0), 'page_html': self.page_html(s, page),
             'hours': round((item.get('seconds') or 0) / 3600, 1) if s.item else None,
             'waiting': review_page.ago(item.get('queuedAt')) if s.item else '', 'platforms': item.get('platforms') or [],
             'has_page': bool(s.page_text),
             'facts_html': review_page.md(re.sub(r'^# .*\n', '', s.page_text)) if s.page_text else
             '<p class="muted"><i>No imported page for this ship.</i></p>',
             'test': self.test_segments(s, page), 'demo': ((page or {}).get('project') or {}).get('demoUrl'),
             'repo_url': ((page or {}).get('project') or {}).get('codeUrl'),
             'release_dir': s.release_dir, 'repo_dir': self.store.work(sid).get('repo_dir'),
             'screenshots': [{'url': self.serve_file(p), 'name': p.name} for p in self.screenshots(s)[:8]],
             'test_actions': [label for label, _ in self.hooks.test_actions],
             'decision_actions': [{'label': label, 'index': i} for i, (label, _, when) in enumerate(self.hooks.decision_actions)
                                  if when in (('draft', 'always') if s.draft else ('no-draft', 'always'))]}
        if not s.draft:
            if s.suggested:
                sf = core.draft_fields(s.suggested)
                d['suggested_html'] = review_page.md(sf.get('what to change') or sf.get('note to player') or '')
            return d
        f = dict(s.fields)
        kind = s.verdict_kind
        written = re.search(r'\d+(?:\.\d+)?', f.get('airtable deflation (hours)') or '')
        cut = float(written.group()) if written else self.fraud_hours(page)
        if kind == 'approve' and self.fraud_cut(page, cut) and not (f.get('deflation reason') or '').strip():
            f['deflation reason'] = self.fraud_reason(page)        # what the review form fills in for fraud's cut
        d['badge'] = KIND_BADGE.get(kind, f.get('verdict', '?'))
        d['fields'] = [{'key': k, 'label': PRETTY.get(k, k), 'text': f[k], 'player': k in core.PLAYER_FIELDS}
                       for k in TEXT_FIELDS if k in f]
        if kind == 'move':
            d['move_to'] = 'admin' if 'admin' in (f.get('verdict') or '') else 'fraud'
        if kind == 'approve':
            try:
                d['multiplier'] = float(f.get('multiplier') or 1.1)
            except ValueError:
                d['multiplier'] = 1.1
            d['cut'] = cut
            sug = ((page or {}).get('votes') or {}).get('suggestedMultiplier')
            d['hints'] = ([f'voters suggest x{sug:.2f}'] if sug else []) + \
                ([f'fraud suggests {self.fraud_hours(page):g}h'] if self.fraud_hours(page) else [])
            ok = s.video and Path(s.video).exists()
            d['video'] = {'name': Path(s.video).name, 'mb': round(Path(s.video).stat().st_size / 1048576, 1)} if ok else None
            d['video_set'] = bool(s.video)
        if s.remarks:
            d['remarks_html'] = review_page.md(s.remarks)
        left = core.template_problems(s.draft)
        d['template_left'] = left[0][:90] if left else ''
        d['template_left_all'] = [x if len(x) <= 160 else x[:157] + '…' for x in left[:8]]
        return d

    def set_notes(self, sid, notes):
        s = self.ship(sid)
        if notes == s.notes:
            return
        s.notes = notes
        self.store.set_work(sid, notes=notes)
        if s.state in ('new', 'notes'):
            s.state = 'notes' if notes.strip() else 'new'
            with self.lock:
                self.version += 1

    def save_notes(self):          # plugins call this; the page saves notes as you type
        return None

    def set_draft(self, sid, draft):
        self.store.set_work(sid, draft=draft)
        self.refresh_one(sid)

    def switch_verdict(self, sid, kind):
        s = self.ship(sid)
        if kind not in core.TEMPLATE_KINDS:
            raise Fail('unknown verdict')
        if s.draft and s.verdict_kind == kind:
            return
        new = core.draft_fields(core.template(kind, self.cfg['handle'], self.cfg))
        old = s.fields.get('note to player') or s.fields.get('what to change')
        if old and kind != 'move':
            new['what to change' if kind == 'changes' else 'note to player'] = old
        page = self.store.page(sid)
        dangers = [f for f in (page or {}).get('flags') or [] if f.get('severity') == 'danger' and not f.get('dismissedAt')]
        if kind == 'approve' and dangers:
            new['danger flag acknowledgement'] = s.fields.get('danger flag acknowledgement') or \
                f'(one line, why each danger flag is fine: {"; ".join(f.get("title", "") for f in dangers)})'
        self.set_draft(sid, fields_to_draft(new))

    def save_draft(self, sid, body):
        s = self.ship(sid)
        if not s.draft:
            raise Fail('no draft to save')
        f = dict(s.fields)
        page = self.store.page(sid)
        if s.verdict_kind == 'move' and body.get('move_to') in ('fraud', 'admin'):
            f['verdict'] = f"move to {body['move_to']}"
        if s.verdict_kind == 'approve' and 'multiplier' in body:
            f['multiplier'] = f'{float(body["multiplier"]):g}'
            cut = float(body.get('cut') or 0)
            f['airtable deflation (hours)'] = f['crop deflation (hours)'] = f'{cut:g}'
            if cut and not (f.get('deflation reason') or '').strip() and self.fraud_cut(page, cut):
                f['deflation reason'] = self.fraud_reason(page)
            if (body.get('fields') or {}).get('deflation reason', '').strip():
                f['deflation reason'] = body['fields']['deflation reason']
            if cut and body.get('reason'):
                f['deflation reason'] = body['reason'].strip()
            if cut and not (f.get('deflation reason') or '').strip():
                return {'need_reason': True}
            if not cut:
                f.pop('deflation reason', None)
        for k, v in (body.get('fields') or {}).items():
            if k in f or k in TEXT_FIELDS:
                if k == 'deflation reason' and 'deflation reason' not in f:
                    continue
                f[k] = v
        new = fields_to_draft(f)
        if new != s.draft:
            self.store.set_work(sid, draft=new)
            self.refresh_one(sid)
            return {'saved': True}
        return {'saved': False}

    def queue(self, sid):
        s = self.ship(sid)
        if not s.draft:
            raise Fail('no draft')
        if s.verdict_kind == 'skip':
            raise Fail('skips are staff notes, post that one by hand')
        if self.claimed == sid:
            self.release_claim(quiet=True)    # the runner claims it again right before it sends
        try:
            msg = core.queue_draft(self.cfg, self.tools, self.store, s)
        except Exception as e:  # noqa: BLE001
            raise Fail(f'NOT queued: {e}') from e
        finally:
            self.refresh_one(sid)
        self.say(msg)
        return msg

    def unqueue(self, sid):
        if core.unqueue(self.cfg, self.ship(sid)):
            self.refresh_one(sid)
            return 'taken out of the send queue'
        return 'it was not in the send queue'

    # ---- checks ----
    def enqueue_check(self, sid, front=False):
        with self.check_wake:
            if sid == self.checking or sid in self.check_queue:
                return
            self.check_queue.insert(0, sid) if front else self.check_queue.append(sid)
            self.version += 1
            self.check_wake.notify()

    def check_worker(self):
        while not self.stopping:
            with self.check_wake:
                while not self.check_queue and not self.stopping:
                    self.check_wake.wait(5)
                if self.stopping:
                    return
                sid = self.check_queue.pop(0)
                self.checking = sid
            s = self.by_key.get(sid)
            left = len(self.check_queue)
            self.say(f'checking {s.title if s else sid}' + (f' ({left} more queued)' if left else ''))
            try:
                core.run_checks(self.cfg, self.tools, self.store, sid)
            except Exception as e:  # noqa: BLE001
                self.say(f'checks failed: {str(e)[:200]}')
            self.checking = None
            self.rev[sid] += 1
            with self.lock:
                self.version += 1
            if not self.check_queue:
                self.say('checks done')

    def check_shown(self, sids):
        todo = [sid for sid in sids if sid in self.by_key and self.by_key[sid].item and self.store.checks(sid)[0] is None]
        for sid in todo:
            self.enqueue_check(sid)
        return f'checking {len(todo)} ships' if todo else 'every shown ship is already checked'

    # ---- claims ----
    def claim(self, sid):
        s = self.ship(sid)
        old = self.claimed
        if old and old != sid:
            try:
                core.release(self.tools, old)
            except Exception:  # noqa: BLE001
                pass
            self.claimed = None
        try:
            core.claim(self.tools, sid)
        except Exception as e:  # noqa: BLE001
            raise Fail(f'claim failed: {e}') from e
        self.claimed = sid
        self.rev[sid] += 1
        if old:
            self.rev[old] += 1
        return f'claimed {s.title}' + (', released the previous one' if old and old != sid else '')

    def release_claim(self, quiet=False):
        sid = self.claimed
        if not sid:
            return 'nothing claimed'
        self.claimed = None
        self.rev[sid] += 1

        def done(res, err):
            if not quiet:
                self.say(f'release failed: {err}' if err else 'claim released')
        self.background(lambda: core.release(self.tools, sid), done)
        return 'releasing the claim'

    def beat(self):
        sid = self.claimed
        if not sid:
            return

        def done(held, err):
            if err:
                self.say(f'claim heartbeat failed ({err}), trying again in a minute')
            elif not held and self.claimed == sid:
                self.claimed = None
                self.rev[sid] += 1
                self.say('your claim was lost (it expired or someone took it over)')
        self.background(lambda: core.heartbeat(self.tools, sid), done)

    # ---- set up, run, video ----
    def setup(self, sid):
        s = self.ship(sid)
        out = self.cfg['workspace'] / 'builds' / f'.prep-{sid}.json'
        out.parent.mkdir(parents=True, exist_ok=True)

        def finished(code):
            try:
                res = json.loads(out.read_text(encoding='utf-8'))
            except (OSError, ValueError) as e:
                self.say(f'set up failed: {f"exited {code}" if code else e}')
                return
            finally:
                out.unlink(missing_ok=True)
            rel = (res.get('download') or {}).get('dir')
            repo = core.ship_repo(res)
            self.store.set_work(sid, release_dir=rel, repo_dir=repo)
            if rel:
                core.add_binary_checks(self.store, sid, rel)
            self.say('set up' + (f': release in {rel}' if rel else ', no release to download') + (f', repo in {repo}' if repo else ''))
            self.refresh_one(sid)
        job = self.jobs.run(self.tools.api_argv(*core.prep_args(self.cfg, sid)), f'set up {s.title}', self.cfg['workspace'],
                            self.tools.github_env(), finished, stdout_file=out)
        return {'job': job.id, 'msg': f'setting up {s.title}: cloning and downloading (nothing runs)…'}

    def release_dirs(self, s):
        out = [Path(s.release_dir)] if s.release_dir and Path(s.release_dir).is_dir() else []
        for fn in self.hooks.release_dirs:
            out += [Path(d) for d in fn(s) or [] if Path(d).is_dir() and Path(d) not in out]
        return out

    def run_menu(self, sid):
        s = self.ship(sid)
        entries = []
        for d in self.release_dirs(s):
            for exe, how in core.find_runnables(d)[:8]:
                entries.append((d, exe, how))
        self.run_menus[sid] = entries
        page = self.store.page(sid)
        demo = ((page or {}).get('project') or {}).get('demoUrl')
        links = ([demo] if demo else []) + [u for u in core.links(s.quick) if u != demo][:5]
        sandbox = system.windows_sandbox_available() if system.WINDOWS else True
        return {'runnables': [{'index': i, 'label': str(exe.relative_to(d)), 'how': how} for i, (d, exe, how) in enumerate(entries)],
                'links': [u for u in links if not core.is_review_page(u)], 'sandbox': sandbox, 'windows': system.WINDOWS}

    def run(self, sid, index, direct=False):
        self.ship(sid)
        entries = self.run_menus.get(sid)
        if entries is None:
            self.run_menu(sid)
            entries = self.run_menus[sid]
        if not 0 <= int(index) < len(entries):
            raise Fail('pick it from the Run menu again')
        d, exe, how = entries[int(index)]
        if system.WINDOWS:
            if how == 'java':
                if not system.which('java'):
                    raise Fail('Java is not installed, so the .jar can\'t start')
                job = self.jobs.run(['java', '-jar', str(exe)], exe.name, exe.parent)
                return {'job': job.id, 'msg': f'started {exe.name} (not sandboxed)'}
            if not direct:
                if not system.windows_sandbox_available():
                    return {'need_direct': True}
                msg, argv = system.sandbox_launch(d, exe, how, self.cfg['workspace'] / 'sandbox')
                system.open_path(argv[0])
                return {'msg': msg}
            if how == 'msi':
                job = self.jobs.run(['msiexec', '/i', str(exe)], exe.name, exe.parent)
            elif how == 'bat':
                job = self.jobs.run(['cmd', '/c', str(exe)], exe.name, exe.parent)
            else:
                job = self.jobs.run([str(exe)], exe.name, exe.parent)
            return {'job': job.id, 'msg': f'started {exe.name} without a sandbox'}
        argv = core.sandbox_command(self.cfg, d, exe, how)
        job = self.jobs.run(argv, exe.name, d)
        return {'job': job.id, 'msg': f'started {exe.name} in the sandbox'}

    def run_code(self, code):
        job = self.jobs.run(system.shell_argv(code), code.split('\n')[0][:60], Path.home())
        return {'job': job.id}

    def open_outside(self, jid):
        job = self.jobs.get(jid)
        if not job:
            raise Fail('no such job')
        kw = {'creationflags': system.CREATE_NEW_CONSOLE} if system.WINDOWS else {'start_new_session': True}
        subprocess.Popen(core.terminal_command(self.cfg, job.argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, cwd=job.cwd, **kw)
        return 'started in a new terminal window'

    def attach_video(self, sid, src, keep_source=False):
        """Shrink a video in the background and attach it. keep_source: it's the reviewer's own file (from the
        clipboard or their recordings), so it's never deleted."""
        s = self.ship(sid)
        dest = self.cfg['workspace'] / 'videos' / f'{sid}.mp4'
        dest.parent.mkdir(parents=True, exist_ok=True)

        def done(res, err):
            try:
                if not keep_source and Path(src) != dest:
                    Path(src).unlink(missing_ok=True)
            except OSError:
                pass
            if err:
                self.say(f'video failed: {err}')
                return
            self.store.set_work(sid, video=str(res))
            self.say(f'video ready for {s.title}: {Path(res).stat().st_size / 1048576:.1f} MiB')
            self.refresh_one(sid)
        tmp = dest.with_name(f'{sid}.compressing.mp4')
        self.background(lambda: Path(core.compress_video(src, tmp)).replace(dest) or dest, done)

    def video_from_clipboard(self, sid):
        """Attach the video file you copied (Ctrl+C on it in File Explorer, or "Copy as path")."""
        found = [Path(p) for p in system.clipboard_paths()]
        videos = [p for p in found if p.suffix.lower() in VIDEO_TYPES and p.is_file()]
        if not videos:
            if found:
                raise Fail(f'{found[0].name} is not a video ({", ".join(VIDEO_TYPES)})')
            raise Fail('No video file on the clipboard. In File Explorer, right-click the video and choose Copy, then try again.')
        self.attach_video(sid, videos[0], keep_source=True)
        return f'shrinking {videos[0].name}…'

    def latest_recording(self, sid):
        """Attach the newest video in your Videos folder (where Snipping Tool and Xbox Game Bar save recordings)."""
        newest = system.newest_recording(VIDEO_TYPES)
        if not newest:
            raise Fail('No recent recording found in your Videos folder')
        self.attach_video(sid, newest, keep_source=True)
        return f'shrinking {newest.name} ({time.strftime("%b %d %H:%M", time.localtime(newest.stat().st_mtime))})…'

    # ---- the queue and stashes ----
    def dump_queue(self):
        if self.dumping:
            raise Fail('a dump is already running, see Jobs')
        out = self.cfg['workspace'] / 'dumps' / time.strftime('terra-queue-%Y-%m-%d-%H%M.md')
        out.parent.mkdir(parents=True, exist_ok=True)
        before = set(core.dump_folders(self.cfg))
        self.dumping = True

        def done(code):
            try:
                new = [f for f in core.dump_folders(self.cfg) if f not in before]
                n = sum(core.import_dump(self.store, f) for f in new)
            finally:
                self.dumping = False          # only once the pages are cached, so nothing reads them half-imported
            self.say(f'dump finished: {out}, {n} review pages cached' if code == 0 else
                     f'dump stopped (exit {code}); {n} review pages cached so far')
            if self.cur_sid:
                self.rev[self.cur_sid] += 1
        job = self.jobs.run(self.tools.api_argv('dump', '--all', '-o', str(out)), 'dump queue', self.cfg['workspace'],
                            self.tools.github_env(), done)
        return {'job': job.id}

    def stash_folder(self):
        return self.cfg['workspace'] / 'stashes'

    def stashes(self):
        found = db.stashes(self.stash_folder()) if self.stash_folder().is_dir() else []
        return [{**x, 'path': x['path'].name, 'when': time.strftime('%b %d %H:%M', time.localtime(x['created']))} for x in found]

    def stash_path(self, name):
        p = self.stash_folder() / Path(name).name
        if p.suffix != '.db' or not p.exists():
            raise Fail('no such stash')
        return p

    # ---- the runner ----
    def start_runner(self, every, hold):
        if core.runner_alive(self.cfg):
            raise Fail('a runner is already going')
        if float(every) <= 0:
            raise Fail('minutes between sends must be above 0')
        argv = self.tools.write_argv('run', '--every', f'{float(every):g}', '--hold', str(int(hold)), '--from-desk')
        (core.queue_dir(self.cfg) / 'every').unlink(missing_ok=True)     # a stop left over would end it at once

        def done(code):
            self.say(f'the runner stopped (exit {code})')
            self.reload()
        self.runner_job = self.jobs.run(argv, 'send runner', self.cfg['workspace'], self.tools.github_env(), done)
        return {'job': self.runner_job.id}

    # ---- settings ----
    def settings(self):
        templates = [{'name': TEMPLATE_NAMES[k], 'key': f'template-{k}', 'default': core.default_template(k),
                      'text': prompts.get(self.cfg, f'template-{k}', core.default_template(k)),
                      'help': 'What a new draft starts from when you pick this verdict. {handle} becomes your handle. '
                              'Lines in (parentheses) are reminders: the draft can\'t be queued until they are replaced.'}
                     for k in core.TEMPLATE_KINDS]
        texts = []
        for name, spec in sorted(self.hooks.prompts.items()):
            if spec.get('path'):
                path = Path(spec['path'])
                texts.append({'name': name, 'key': name, 'file': str(path), 'help': spec.get('help', ''),
                              'text': path.read_text(encoding='utf-8') if path.exists() else ''})
            else:
                texts.append({'name': name, 'key': spec.get('key', name), 'default': spec['default'], 'help': spec.get('help', ''),
                              'text': prompts.get(self.cfg, spec.get('key', name), spec['default'])})
        extra = [{'key': x['key'], 'label': x['label'], 'choices': [str(c) for c in x['choices']], 'help': x.get('help', ''),
                  'value': str(self.cfg.get(x['key'], x.get('default', x['choices'][0])))} for x in self.hooks.settings]
        return {'handle': self.cfg.get('handle', ''), 'terminal': self.cfg.get('terminal', ''), 'extra': extra,
                'window': self.cfg.get('window', 'app'),
                'columns': core.QUEUE_COLUMNS, 'shown': self.cfg['queue_columns'], 'token': core.token_state(),
                'token_file': str(core.TOKEN_FILE), 'config_file': str(core.CONFIG_FILE), 'workspace': str(self.cfg['workspace']),
                'templates': templates, 'prompts': texts, 'windows': system.WINDOWS,
                'sandbox': system.windows_sandbox_available() if system.WINDOWS else None}

    def find_text(self, key):
        for k in core.TEMPLATE_KINDS:
            if key == f'template-{k}':
                return {'key': key, 'default': core.default_template(k)}
        for name, spec in self.hooks.prompts.items():
            if spec.get('key', name) == key:
                return {**spec, 'key': spec.get('key', name)}
        raise Fail('no such text')

    def shutdown(self):
        self.stopping = True
        with self.check_wake:
            self.check_wake.notify_all()
        if self.claimed:
            try:
                core.release(self.tools, self.claimed)
            except Exception:  # noqa: BLE001 - closing anyway; the claim expires on its own
                pass
        for j in list(self.jobs.jobs.values()):
            if j is not self.runner_job:
                j.stop()

    def timers(self):
        last = {'refetch': time.time(), 'reimport': time.time(), 'beat': time.time(), 'sends': time.time()}
        marks = core.send_marks(self.cfg)
        while not self.stopping:
            time.sleep(1)
            now = time.time()
            if now - last['sends'] >= 3:          # the sender works in its own process: notice what it sent
                last['sends'] = now
                try:
                    new = core.send_marks(self.cfg)
                    if new != marks:
                        marks = new
                        self.reload()
                        if self.cur_sid:
                            self.rev[self.cur_sid] += 1
                except OSError:
                    pass
            if now - last['refetch'] >= 120:
                last['refetch'] = now
                self.refetch()
            if now - last['reimport'] >= 30:
                last['reimport'] = now
                try:
                    self.reimport()
                except Exception as e:  # noqa: BLE001
                    self.say(f'reimport failed: {e}')
            if now - last['beat'] >= 55:
                last['beat'] = now
                self.beat()


# ---- HTTP ----

class Handler(BaseHTTPRequestHandler):
    desk = None
    key = ''
    server_version = 'terra-desk'

    def log_message(self, fmt, *args):  # quiet
        pass

    def host_ok(self):
        host = (self.headers.get('Host') or '').split(':')[0].strip('[]').lower()
        return host in ('127.0.0.1', 'localhost', '::1')

    def send(self, code, body, ctype='application/json; charset=utf-8', headers=None):
        data = body if isinstance(body, bytes) else (json.dumps(body, default=str) if not isinstance(body, str) else body).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        if ctype.startswith('text/html'):
            self.send_header('Content-Security-Policy', CSP)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def static(self, path):
        if path in ('/', '/index.html'):
            html = (WEB / 'index.html').read_text(encoding='utf-8').replace('{{KEY}}', self.key)
            return self.send(200, html, 'text/html; charset=utf-8')
        if path.startswith('/static/'):
            name = path[len('/static/'):]
            f = (WEB / name).resolve()
            if f.is_file() and WEB.resolve() in f.parents:
                ctype = mimetypes.guess_type(f.name)[0] or 'application/octet-stream'
                if f.suffix == '.js':
                    ctype = 'text/javascript; charset=utf-8'
                elif f.suffix == '.css':
                    ctype = 'text/css; charset=utf-8'
                return self.send(200, f.read_bytes(), ctype)
        if path.startswith('/file/'):
            token = path.split('/')[2]
            f = self.desk.files.get(token)
            if f and Path(f).is_file():
                ctype = mimetypes.guess_type(f)[0] or 'application/octet-stream'
                if not ctype.startswith(('image/', 'video/')):
                    ctype = 'application/octet-stream'
                return self.send(200, Path(f).read_bytes(), ctype)
        self.send(404, {'error': 'not found'})

    def do_GET(self):  # noqa: N802
        if not self.host_ok():
            return self.send(403, {'error': 'bad host'})
        url = urllib.parse.urlsplit(self.path)
        if url.path == '/api/ping':
            return self.send(200, {'app': 'terra-desk'})
        if url.path.startswith('/api/'):
            return self.api('GET', url, None)
        return self.static(url.path)

    def do_POST(self):  # noqa: N802
        if not self.host_ok():
            return self.send(403, {'error': 'bad host'})
        url = urllib.parse.urlsplit(self.path)
        if not url.path.startswith('/api/'):
            return self.send(404, {'error': 'not found'})
        n = int(self.headers.get('Content-Length') or 0)
        if url.path.endswith('/video'):
            return self.api('POST', url, None, upload=n)
        body = json.loads(self.rfile.read(n) or b'{}') if n else {}
        return self.api('POST', url, body)

    def api(self, method, url, body, upload=0):
        if not secrets.compare_digest(self.headers.get('X-Desk-Key') or '', self.key):
            return self.send(403, {'error': 'wrong key: reload the page'})
        q = dict(urllib.parse.parse_qsl(url.query))
        try:
            res = route(self.desk, method, url.path, body or {}, q, self, upload)
            if res is not None:
                self.send(200, res)
        except Fail as e:
            self.send(400, {'error': str(e)})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.send(500, {'error': f'{type(e).__name__}: {e}'})


def route(desk, method, path, b, q, handler, upload):
    parts = path.strip('/').split('/')[1:]       # after "api"
    head = parts[0] if parts else ''
    if method == 'GET':
        if head == 'state':
            return desk.state(int(q.get('since') or 0))
        if head == 'ships':
            return {'version': desk.version, 'rows': desk.rows(), 'columns': desk.cfg['queue_columns'],
                    'widths': core.column_widths()}
        if head == 'ship' and len(parts) == 2:
            return desk.detail(parts[1])
        if head == 'ship' and len(parts) == 3 and parts[2] == 'run-menu':
            return desk.run_menu(parts[1])
        if head == 'jobs' and len(parts) == 2:
            job = desk.jobs.get(parts[1])
            if not job:
                raise Fail('no such job')
            with job.lock:
                text = job.log
            return {**job.info(), 'text': jobs.clean(text)}
        if head == 'stashes':
            return {'stashes': desk.stashes()}
        if head == 'rubric':
            return rubric.guide()
        if head == 'failed':
            return {'failed': core.failed_sends(desk.cfg)}
        if head == 'settings':
            return desk.settings()
        raise Fail('unknown request')

    # POST
    if head == 'ship':
        sid, act = parts[1], parts[2] if len(parts) > 2 else ''
        s = desk.ship(sid)
        if act == 'notes':
            desk.set_notes(sid, str(b.get('notes') or ''))
            return {'ok': True}
        if act == 'verdict':
            desk.switch_verdict(sid, b.get('kind'))
            return {'ok': True}
        if act == 'use-suggested':
            desk.set_draft(sid, s.suggested)
            return {'ok': True}
        if act == 'draft':
            return desk.save_draft(sid, b)
        if act == 'delete-draft':
            desk.store.set_work(sid, remarks=None)
            desk.set_draft(sid, None)
            return {'ok': True}
        if act == 'queue':
            return {'msg': desk.queue(sid)}
        if act == 'unqueue':
            return {'msg': desk.unqueue(sid)}
        if act == 'skip':
            desk.store.set_work(sid, skipped=1 if b.get('skipped') else 0)
            desk.refresh_one(sid)
            return {'msg': 'skipped' if b.get('skipped') else 'back in the list'}
        if act == 'claim':
            return {'msg': desk.claim(sid)}
        if act == 'release':
            return {'msg': desk.release_claim()}
        if act == 'check':
            desk.enqueue_check(sid, front=True)
            return {'msg': 'checking'}
        if act == 'setup':
            return desk.setup(sid)
        if act == 'run':
            return desk.run(sid, b.get('index', -1), bool(b.get('direct')))
        if act == 'video':
            name = urllib.parse.unquote(handler.headers.get('X-Filename') or 'video.mp4')
            ext = Path(name).suffix.lower()
            if ext not in VIDEO_TYPES:
                raise Fail('pick a video: ' + ', '.join(VIDEO_TYPES))
            src = desk.cfg['workspace'] / 'videos' / f'{sid}-upload{ext}'
            src.parent.mkdir(parents=True, exist_ok=True)
            left = upload
            with src.open('wb') as fh:
                while left > 0:
                    chunk = handler.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    fh.write(chunk)
                    left -= len(chunk)
            desk.attach_video(sid, src)
            return {'msg': 'compressing the video…'}
        if act == 'video-clipboard':
            return {'msg': desk.video_from_clipboard(sid)}
        if act == 'video-latest':
            return {'msg': desk.latest_recording(sid)}
        if act == 'remove-video':
            desk.store.set_work(sid, video=None)
            desk.refresh_one(sid)
            return {'ok': True}
        if act == 'clear-remarks':
            desk.store.set_work(sid, remarks=None)
            desk.refresh_one(sid)
            return {'ok': True}
        if act == 'open-folder':
            folder = s.release_dir if b.get('which') != 'repo' else desk.store.work(sid).get('repo_dir')
            if not folder or not Path(folder).exists():
                raise Fail('that folder is not there (press Set up first)')
            system.open_path(folder)
            return {'ok': True}
        if act == 'action':
            kind, i = b.get('kind'), int(b.get('index', -1))
            hooks = desk.hooks.decision_actions if kind == 'decision' else desk.hooks.test_actions
            if not 0 <= i < len(hooks):
                raise Fail('no such action')
            fn = hooks[i][1]
            desk.cur_sid = sid
            res = fn(desk, s)
            desk.refresh_one(sid)
            return {'msg': res if isinstance(res, str) else ''}
        raise Fail('unknown request')
    if head == 'refresh':
        desk.refetch()
        return {'msg': 'refreshing the queue…'}
    if head == 'reload':
        desk.reimport()
        desk.reload()
        return {'msg': 'reloaded the pages'}
    if head == 'check-shown':
        return {'msg': desk.check_shown(b.get('sids') or [])}
    if head == 'dump':
        return desk.dump_queue()
    if head == 'run-code':
        return desk.run_code(str(b.get('code') or ''))
    if head == 'open-url':
        u = str(b.get('url') or '')
        if core.is_review_page(u):
            raise Fail('not opening Terra review pages (that would claim the ship)')
        if not re.match(r'^https?://', u):
            raise Fail('only web links open')
        import webbrowser
        webbrowser.open(u)
        return {'ok': True}
    if head == 'jobs':
        job = desk.jobs.get(parts[1])
        if not job:
            raise Fail('no such job')
        act = parts[2] if len(parts) > 2 else ''
        if act == 'stop':
            if job is desk.runner_job and job.running:
                core.runner_control(desk.cfg, 'stop')
                threading.Timer(15, lambda: job.running and job.stop()).start()
                return {'msg': 'asking the runner to stop (it releases its claims first)'}
            job.stop()
            return {'msg': 'stopping'}
        if act == 'input':
            job.write(str(b.get('text') or '') + '\n')
            return {'ok': True}
        if act == 'outside':
            return {'msg': desk.open_outside(job.id)}
        if act == 'close':
            desk.jobs.close(job.id)
            return {'ok': True}
        raise Fail('unknown request')
    if head == 'failed':
        try:
            core.failed_send(desk.cfg, str(b.get('name') or ''), bool(b.get('retry')))
        except RuntimeError as e:
            raise Fail(str(e)) from e
        desk.reload()
        return {'msg': 'back in the send queue' if b.get('retry') else 'removed'}
    if head == 'runner':
        act = parts[1] if len(parts) > 1 else ''
        if act == 'start':
            return desk.start_runner(b.get('every') or 4, b.get('hold', 1))
        if act == 'control':
            word = str(b.get('word') or '').strip().lower()
            if not core.runner_alive(desk.cfg):
                raise Fail('no runner is going')
            if word not in ('pause', 'resume', 'stop'):
                try:
                    if float(word) <= 0:
                        raise ValueError
                except ValueError:
                    raise Fail('minutes above 0, or pause / resume / stop') from None
            core.runner_control(desk.cfg, word)
            return {'msg': f'sent {word!r} to the runner'}
        raise Fail('unknown request')
    if head == 'stash':
        act = parts[1] if len(parts) > 1 else ''
        if act == '':
            if not desk.store.has_work():
                raise Fail('nothing to stash')
            path = desk.store.stash(desk.stash_folder())
            desk.reload()
            return {'msg': f'stashed everything in {path.name}, starting fresh'}
        if act == 'pop':
            p = desk.stash_path(b.get('name', ''))
            desk.store.pop(p, desk.stash_folder())
            desk.reload()
            desk.refetch()
            return {'msg': 'brought the stash back'}
        if act == 'drop':
            desk.stash_path(b.get('name', '')).unlink(missing_ok=True)
            return {'msg': 'stash deleted'}
        raise Fail('unknown request')
    if head == 'settings':
        act = parts[1] if len(parts) > 1 else ''
        cfg = desk.cfg
        if act == 'general':
            core.set_config(cfg, 'handle', str(b.get('handle', '')).strip())
            os.environ['TERRA_HANDLE'] = cfg['handle']
            core.set_config(cfg, 'terminal', str(b.get('terminal', '')).strip())
            if b.get('window') in ('app', 'browser'):
                core.set_config(cfg, 'window', b['window'])
            for k, v in (b.get('extra') or {}).items():
                if any(x['key'] == k for x in desk.hooks.settings):
                    core.set_config(cfg, k, str(v))
            return {'msg': 'settings saved'}
        if act == 'columns':
            cols = [c for c in b.get('columns') or [] if c in core.QUEUE_COLUMNS]
            if 'ship' not in cols:
                cols.append('ship')
            cfg['queue_columns'] = [c for c in core.QUEUE_COLUMNS if c in cols]
            core.set_config(cfg, 'queue_columns', cfg['queue_columns'])
            desk.version += 1
            return {'ok': True}
        if act == 'widths':
            core.save_state(column_widths={k: int(v) for k, v in (b.get('widths') or {}).items()})
            return {'ok': True}
        if act == 'key':
            if b.get('remove'):
                core.remove_token()
            elif str(b.get('key') or '').strip():
                core.save_token(str(b['key']))
            else:
                raise Fail('paste the key first')
            desk.tools._api = None
            desk.refetch()
            return {'token': core.token_state()}
        if act == 'text':
            spec = desk.find_text(b.get('key'))
            text = str(b.get('text') or '')
            if spec.get('path'):
                Path(spec['path']).write_text(text, encoding='utf-8')
                return {'msg': f'saved {spec["path"]}'}
            if b.get('reset'):
                prompts.reset(cfg, spec['key'])
                return {'msg': 'back to the default', 'text': spec['default']}
            prompts.save(cfg, spec['key'], text)
            return {'msg': 'saved'}
        raise Fail('unknown request')
    if head == 'quit':
        threading.Thread(target=lambda: (time.sleep(0.3), handler.server.shutdown()), daemon=True).start()
        return {'msg': 'terra-desk is closing'}
    raise Fail('unknown request')


def running_file():
    return system.config_dir() / 'running.json'


def already_running():
    """The URL of a terra-desk that is already running for this user, if any."""
    try:
        info = json.loads(running_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not system.pid_alive(info.get('pid')):
        return None
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{info['port']}/api/ping", timeout=2) as r:
            if json.loads(r.read()).get('app') == 'terra-desk':
                return info.get('url')
    except Exception:  # noqa: BLE001
        return None
    return None


def make_server(desk, port):
    key = secrets.token_urlsafe(24)
    handler = type('DeskHandler', (Handler,), {'desk': desk, 'key': key})
    try:
        httpd = ThreadingHTTPServer(('127.0.0.1', port), handler)
    except OSError:
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)      # that port is taken: any free one
    httpd.daemon_threads = True
    return httpd, key


def serve(open_browser=True, port=None):
    existing = already_running()
    if existing:
        system.open_window(existing, core.load_config().get('window', 'app'))
        return 0
    desk = Desk()
    httpd, _ = make_server(desk, port or desk.cfg['port'])
    url = f'http://127.0.0.1:{httpd.server_address[1]}/'
    running_file().parent.mkdir(parents=True, exist_ok=True)
    running_file().write_text(json.dumps({'pid': os.getpid(), 'port': httpd.server_address[1], 'url': url}), encoding='utf-8')
    threading.Thread(target=desk.check_worker, daemon=True).start()
    threading.Thread(target=desk.timers, daemon=True).start()
    desk.refetch()
    print(f'terra-desk is running at {url} (close it with Quit in the page, or Ctrl+C here)', flush=True)
    if open_browser:
        system.open_window(url, desk.cfg.get('window', 'app'))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        desk.shutdown()
        httpd.server_close()
        running_file().unlink(missing_ok=True)
    return 0


def test_client(desk):
    """For tests: call the API without a socket. Returns call(method, path, body=None) -> (status, json)."""
    key = 'test-key'

    def call(method, path, body=None):
        url = urllib.parse.urlsplit(path)
        out = {}

        class Fake:
            headers = {'X-Desk-Key': key, 'Host': '127.0.0.1'}
            server = SimpleNamespace(shutdown=lambda: None)

            def send(self, code, res):
                out['code'], out['body'] = code, res

        fake = Fake()
        try:
            res = route(desk, method, url.path, body or {}, dict(urllib.parse.parse_qsl(url.query)), fake, 0)
            return 200, json.loads(json.dumps(res, default=str))
        except Fail as e:
            return 400, {'error': str(e)}
    return call
