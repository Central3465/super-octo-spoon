#!/usr/bin/env python3
"""Read-only command line for Terra's reviewer API (api.terra.hackclub.com).

It only ever sends GET requests to Terra, and only to the read endpoints in ENDPOINTS, so it can't
claim, release, decide, write notes or change anything. Opening a review page in the browser is what
claims a ship; this never does. Endpoint details are in TERRA-API.md. `prep` also clones the ship's
repo with git and sends GETs to code hosts, PyPI and npm (see Web), never with the Terra token.

  ./terra-api.py endpoints                                every endpoint, and which ones this has
  ./terra-api.py queue --week "Prep Week 3" --unclaimed   the queue as a table
  ./terra-api.py ship 92                                  one ship as markdown (ship number, review URL or id)
  ./terra-api.py ship 92 --json | jq .shipment.aiDeclaration
  ./terra-api.py dump --week "Week 1"                     unclaimed ships not handled yet -> terra-queue-<stamp>.md
  ./terra-api.py get /review/counts                       any read endpoint, raw JSON
  ./terra-api.py prep 92 [--offline] [--download]         clone the repo and print the review facts (was prep.sh)

Every command takes --json. Admin and fraud endpoints answer 403 if your account can't see them.

The token is your own Terra login. In the browser console on terra.hackclub.com run
`copy(localStorage.terra_auth_token)`, then save it with `wl-paste > ~/.config/terra/token`.
It's read from TERRA_TOKEN or that file and never printed.
"""
import argparse
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import zipfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = 'https://api.terra.hackclub.com'
API_HOST = urllib.parse.urlsplit(API).netloc
SITE = 'https://terra.hackclub.com'
TOKEN_FILE = Path.home() / '.config/terra/token'
CACHE = Path.home() / '.cache/terra'
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
REVIEW_ID = re.compile(r'admin/review/([0-9a-f-]{36})')

# every endpoint in TERRA-API.md. Only the GETs with a command are callable; the rest change
# things on Terra and are listed so `endpoints` shows the whole API.
ENDPOINTS = [
    ('me', 'GET', '/auth/me', 'your account and roles'),
    ('counts', 'GET', '/review/counts', 'queue sizes (sidebar badges)'),
    ('weeks', 'GET', '/review/weeks', 'week numbers, labels and dates'),
    ('stats', 'GET', '/review/stats', "this week's reviewer leaderboard"),
    ('queue', 'GET', '/review/queue', 'the whole queue list'),
    ('ship', 'GET', '/review/shipment/{id}', 'everything a review page shows'),
    ('chain', 'GET', '/review/shipment/{id}/chain', 'every version of an update project'),
    ('parent', 'GET', '/review/shipment/{id}/parent', 'the original project of an update'),
    ('unified', 'GET', '/review/shipment/{id}/unified-projects', 'matching projects in other YSWS programs'),
    ('notes', 'GET', '/review/project/{id}/notes', 'staff notes on a project'),
    ('macros', 'GET', '/review/macros', 'canned decision notes'),
    ('tags', 'GET', '/review/tag-options', 'tag pick list and usage'),
    ('deflation-checks', 'GET', '/review/deflation-checks', 'admin: approvals waiting on a deflation check'),
    ('triage', 'GET', '/review/fraud/triage', 'fraud: triage list'),
    ('search', 'GET', '/admin/search', 'admin: search users, projects and ships'),
    (None, 'POST', '/review/queue/next', 'next ship to open after a decision'),
    (None, 'POST', '/review/project/{id}/notes', 'add a staff note'),
    (None, 'PATCH', '/review/note/{id}', 'edit a note'),
    (None, 'DELETE', '/review/note/{id}', 'delete a note'),
    (None, 'POST', '/review/shipment/{id}/claim', 'claim a ship'),
    (None, 'POST', '/review/shipment/{id}/claim/heartbeat', 'keep a claim alive'),
    (None, 'DELETE', '/review/shipment/{id}/claim', 'release a claim'),
    (None, 'POST', '/review/shipment/{id}/approve', 'approve'),
    (None, 'POST', '/review/shipment/{id}/changes', 'request changes'),
    (None, 'POST', '/review/shipment/{id}/reject', 'reject'),
    (None, 'POST', '/review/shipment/{id}/send', 'move to fraud or an admin'),
    (None, 'POST', '/review/shipment/{id}/flags/{kind}/dismiss', 'acknowledge or dismiss a flag'),
    (None, 'POST', '/review/shipment/{id}/video', 'upload a review video'),
    (None, 'POST', '/review/shipment/{id}/video-rejected', 'telemetry for a video over 100 MB'),
    (None, 'POST', '/review/decision/{id}/video', 'attach a video to an approval'),
    (None, 'POST', '/review/shipment/{id}/resync', 'rebuild the evidence'),
    (None, 'POST', '/review/shipment/{id}/tags', 'set project tags'),
    (None, 'POST', '/review/shipment/{id}/justification', 'admin: rewrite a finished approval'),
    (None, 'POST', '/review/shipment/{id}/pending-approval', 'admin: correct a pending approval'),
    (None, 'POST', '/review/shipment/{id}/deflation-check', 'admin: settle a deflation check'),
    (None, 'POST', '/review/shipment/{id}/requeue', 'admin: send back to the queue'),
    (None, 'POST', '/review/shipment/{id}/revert', 'admin: revert a decision'),
    (None, 'POST', '/review/shipment/{id}/fraud', 'fraud verdict'),
    (None, 'POST', '/review/shipment/{id}/fraud-edit', 'edit a fraud verdict'),
    (None, 'POST', '/review/shipment/{id}/triage-flag', 'set a fraud triage tier'),
    (None, 'DELETE', '/review/shipment/{id}/triage-flag', 'clear a fraud triage tier'),
    (None, 'POST', '/review/fraud/pass-bulk', 'pass many ships at once'),
]
ALLOWED = [re.compile(path.replace('{id}', UUID.pattern)) for cmd, method, path, _ in ENDPOINTS
           if cmd and method == 'GET']


def die(msg):
    raise SystemExit(f'terra-api.py: {msg}')


class ApiError(Exception):
    def __init__(self, code, path, msg):
        super().__init__(f'{code} on {path}: {msg}')
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib would resend the Authorization header to wherever a redirect points, and the new
    # path wouldn't have been checked against the allowlist, so redirects are never followed
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


class Api:
    def __init__(self, token):
        self.token = token
        self.gap = 0.7  # ~85 requests a minute, under the 100/min limit
        self.next_at = 0.0

    def get(self, path, query=None):
        if not any(p.fullmatch(path) for p in ALLOWED):
            raise SystemExit(f'refusing to call {path}: not on the read-only list (see `endpoints`)')
        q = urllib.parse.urlencode({k: v for k, v in (query or {}).items() if v not in (None, '')})
        url = f'{API}{path}{"?" + q if q else ""}'
        for attempt in range(5):
            # only wait between requests, so a single command is instant
            time.sleep(max(0.0, self.next_at - time.monotonic()))
            req = urllib.request.Request(url, method='GET', headers={
                'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'})
            try:
                with OPENER.open(req, timeout=60) as r:
                    body = json.load(r)
                    self.pace(r.headers)
                    return body
            except urllib.error.HTTPError as e:
                self.pace(e.headers)
                if e.code == 429:
                    self.wait_reset(e.headers, attempt)
                    continue
                if e.code == 401:
                    raise SystemExit('401 from the API: the token is wrong or expired, copy it again')
                try:
                    msg = json.loads(e.read()).get('error') or ''
                except Exception:
                    msg = ''
                if e.code == 403:
                    msg = msg or "your account can't see this"
                elif 300 <= e.code < 400:
                    msg = f"redirected to {e.headers.get('Location')}, not following it"
                raise ApiError(e.code, path, msg or 'request failed')
        raise SystemExit(f'still rate limited on {path}, try again later')

    def pace(self, headers):
        self.next_at = time.monotonic() + self.gap
        left = headers.get('X-RateLimit-Remaining')
        if left is not None and int(left) < 10:
            self.wait_reset(headers, 0)

    def wait_reset(self, headers, attempt):
        reset = headers.get('X-RateLimit-Reset')
        wait = max(1, int(reset) - time.time() + 1) if reset else 15 * (attempt + 1)
        print(f'  rate limit, waiting {wait:.0f}s', file=sys.stderr)
        time.sleep(wait)


def connect():
    token = os.environ.get('TERRA_TOKEN') or (TOKEN_FILE.read_text() if TOKEN_FILE.exists() else '')
    token = token.strip().strip('"')
    if not token:
        raise SystemExit(f'no token: set TERRA_TOKEN or save it to {TOKEN_FILE} (see --help)')
    return Api(token)


def get_weeks(api):
    # the week list barely changes, so keep it for 6 hours instead of spending a request every run
    f = CACHE / 'weeks.json'
    if f.exists() and time.time() - f.stat().st_mtime < 6 * 3600:
        return json.loads(f.read_text())
    w = api.get('/review/weeks')
    w = w if isinstance(w, list) else w.get('weeks', [])
    CACHE.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(w))
    return w


def resolve(ref):
    """A ship number (92), a review URL or a bare id -> the id."""
    m = UUID.search(ref)
    if m:
        return m.group(0)
    if ref.isdigit():
        for f in sorted(list(HERE.glob(f'bulk-review*/{int(ref):02d}-*.md')) + list(Path(os.environ.get('TERRA_SHIPS_DIR', HERE / 'ships')).glob(f'{int(ref)}-*.md'))):
            ids = REVIEW_ID.findall(f.read_text(errors='ignore'))
            if ids:
                return ids[0]
        # request changes live in one file per folder, under a `## NN Title` heading
        for f in sorted(HERE.glob('bulk-review*/request-changes.md')):
            m = re.search(rf'^## {int(ref):02d} [^\n]*\n+\S*admin/review/([0-9a-f-]{{36}})', f.read_text(errors='ignore'), re.M)
            if m:
                return m.group(1)
        raise SystemExit(f'no ship page numbered {int(ref)} with a review URL in it')
    raise SystemExit(f"{ref!r} isn't a ship number, review URL or id")


def known_ids():
    ids = set()
    for f in list(HERE.glob('bulk-review*/*.md')) + list((HERE / 'queue-skim').rglob('*.md')):
        if 'chunk-' not in f.name:
            ids.update(REVIEW_ID.findall(f.read_text(errors='ignore')))
    return ids


def show(data):
    print(json.dumps(data, indent=2, default=str))


# ---- formatting ----

def text(t):
    # player-written text can't start lines with # (looks like a new ship/section) or be a bare --- (splits ships)
    t = str(t or '').replace('\r', '')
    t = re.sub(r'(?m)^(\s*)#', r'\1\\#', t)
    return re.sub(r'(?m)^\s*(-{3,}|\*{3,}|_{3,})\s*$', '- - -', t)


def hours(s):
    return f'{(s or 0) / 3600:.2f}h'


def parse(iso):
    return datetime.fromisoformat(str(iso).replace('Z', '+00:00'))


def when(iso):
    return parse(iso).astimezone().strftime('%m/%d/%Y, %I:%M:%S %p') if iso else '?'


def day(iso):
    return parse(iso).astimezone().strftime('%b %d, %Y') if iso else '?'


def ago(iso):
    if not iso:
        return '?'
    d = datetime.now(timezone.utc) - parse(iso)
    return f'{d.days}d ago' if d.days else f'{d.seconds // 3600}h ago'


def week_name(weeks, n):
    w = next((w for w in weeks if w.get('number') == n), None)
    return w.get('label') if w else f'Week {n}'


def week_label(weeks, n):
    w = next((w for w in weeks if w.get('number') == n), None)
    if not w:
        return f'Week {n}'
    return f"{w.get('label')} ({day(w.get('startDate'))} to {day(w.get('endDate'))} )"


def week_short(weeks, n):
    return week_name(weeks, n).replace('Prep Week ', 'P').replace('Week ', 'W')


def file_tree(files):
    # big repos (unity) list hundreds of untouched files, so only show ones with time on them
    timed = [f for f in files if (f.get('seconds') or 0) >= 18]
    out = [f"{f.get('path')} {hours(f.get('seconds'))}" for f in sorted(timed, key=lambda f: f.get('path', ''))]
    if len(files) > len(timed):
        out.append(f'(+{len(files) - len(timed)} files with no tracked time)')
    return out


def render_chain(chain, weeks):
    L = []
    for ver in (chain.get('versions') if isinstance(chain, dict) else None) or []:
        pr = ver.get('project') or {}
        L.append(f"{pr.get('title')} ({week_name(weeks, pr.get('weekNumber'))}) code {pr.get('codeUrl')} demo {pr.get('demoUrl')}")
        for sh in ver.get('ships') or []:
            L.append(f"  ship: {sh.get('status')} by {sh.get('reviewer') or '-'} {day(sh.get('reviewedAt'))}, "
                     f"{hours(sh.get('seconds'))} shipped, {hours(sh.get('approvedSeconds'))} approved, x{sh.get('payoutMultiplier') or '-'}"
                     + (f", deflated {hours(sh.get('deflationSeconds'))}: {text(sh.get('deflationReason'))}" if sh.get('deflationSeconds') else ''))
            for k, label in (('builtWhat', 'features'), ('projectReview', 'justification'), ('updateJustification', 'update')):
                if sh.get(k):
                    L.append(f"    {label}: {text(sh[k])}")
        if ver.get('devlogs'):
            L.append('  devlogs: ' + '; '.join(f"{text(dv.get('title'))} {hours(dv.get('creditedSeconds'))}" for dv in ver['devlogs']))
    return L


def render_notes(notes):
    items = notes.get('notes', []) if isinstance(notes, dict) else notes or []
    if not items:
        return ['No notes yet.']
    return [f"{n.get('authorName') or n.get('author') or ''} ({n.get('audience') or '?'}) {ago(n.get('createdAt'))}: "
            f"{text(n.get('body'))}" for n in items if isinstance(n, dict)]


def render(d, notes, chain, weeks, waiting=None):
    s, p, m = d.get('shipment', {}), d.get('project', {}), d.get('maker', {})
    ev = (d.get('evidence') or {}).get('data') or {}
    L = [f"# {s.get('title') or p.get('title')}",
         f"by {m.get('username')}, shipped {ago(s.get('submittedAt'))}, evidence from {ago((d.get('evidence') or {}).get('builtAt'))}"
         + (f', {waiting} more waiting' if waiting is not None else ''),
         f"{SITE}/admin/review/{s.get('id')}", '']
    head = 'In review'
    if p.get('isUpdate'):
        head += ' Marked as update of'
    L.append(head)
    if p.get('parent'):
        L.append(f"{p['parent'].get('title')}: {SITE}/admin/projects/{p['parent'].get('id')}")
    L.append(week_label(weeks, s.get('weekNumber')))
    if s.get('changesRounds'):
        L.append(f"Changes requested {s['changesRounds']} times before"
                 + (" (at 3, only admins can request changes)" if s['changesRounds'] >= 3 else ''))
    if p.get('description'):
        L.append(text(p['description']))
    L.append(f"Ship notes: {text(s.get('notes'))}")
    L.append(f"AI declaration: {text(s.get('aiDeclaration')) or 'none given'}")
    L.append('Runs on ' + ' '.join(x.capitalize() if x != 'macos' else 'MacOS' for x in s.get('platforms') or []))
    L.append('Hackatime projects ' + ' '.join(p.get('hackatimeProjects') or []))
    if p.get('branch'):
        L.append(f"Branch {p['branch']}")
    L.append(f"Repo: {p.get('codeUrl')}")
    L.append(f"Demo: {p.get('demoUrl')}")

    # callouts the skim and split-queue.py look for
    claim = (d.get('claims') or {}).get('review')
    if claim:
        L.append(f"! {claim.get('username') or claim.get('holder') or 'someone'} is reviewing this ship")
    if s.get('reviewHold'):
        L.append(f"! Review hold ({s['reviewHold']}): {s.get('reviewHoldNote') or ''}")
    fs = d.get('fraudSuggestion')
    if fs:
        L.append(f"! Fraud recommends cutting {hours(fs.get('seconds'))}: \"{fs.get('reason') or ''}\"")

    L += ['', f"## Player ({m.get('username')})",
          f"Username {m.get('username')} Display name {m.get('displayName')} GitHub {m.get('githubLogin') or '-'}",
          f"Account created {day(m.get('createdAt'))}"]
    for ps in m.get('pastShips') or []:
        L.append(f"Other ship: {ps.get('title')} {week_name(weeks, ps.get('weekNumber'))} {ps.get('status')} {hours(ps.get('overrideSeconds'))}")

    t = ev.get('time') or {}
    L += ['', '## Hours',
          f"Credited {hours(t.get('creditedSeconds') or s.get('hackatimeSeconds'))} from {len(d.get('devlogs') or [])} devlogs, AI at 1/3",
          f"Tracked {hours(t.get('totalSeconds') or s.get('hackatimeSeconds'))}",
          f"AI coding {hours(t.get('aiSeconds') or s.get('aiSeconds'))}",
          f"Water paid {round(d.get('waterPaid') or 0, 2)}"]
    long = [x for x in ev.get('sessions') or [] if (x.get('seconds') or 0) > 3600]
    if long:
        L.append('Sessions over 1h without a break')
        L += [f"{when(x.get('start'))} to {when(x.get('end'))} {hours(x.get('seconds'))}" for x in long]

    par = p.get('parent')
    if par:
        L += ['', '## Previous Versions']
        L.append(f"{par.get('title')}: {par.get('what') or ''}")
        L += [f"{x.get('weekLabel')} completed {day(x.get('completedAt'))} {hours(x.get('seconds'))}" for x in par.get('ships') or []]
    if d.get('decisions'):
        L += ['', '## Previous decisions (newest first)']
    for dec in d.get('decisions') or []:
        L += ['', f"Decision: {dec.get('decision')} by {dec.get('reviewer')} {ago(dec.get('createdAt'))}",
              f"To player: {text(dec.get('noteToMaker'))}",
              f"Justification: {text(dec.get('justification') or dec.get('projectReview'))}"]
    if chain:
        L += ['', '## Update chain (every version of this project, oldest first)'] + render_chain(chain, weeks)

    v = d.get('votes') or {}
    L += ['', f"## Community votes ({v.get('averageStars')} stars average, {len(v.get('list') or [])} votes in round {v.get('round')}, suggests x{v.get('suggestedMultiplier')})"]
    for x in v.get('list') or []:
        L += [f"{x.get('verdict')} {x.get('stars')}/7 {ago(x.get('createdAt'))}", text(x.get('justification'))]

    L += ['', '## Devlogs and commits']
    commits = ev.get('commits') or []
    for dv in d.get('devlogs') or []:
        L.append(f"{dv.get('title')} {hours(dv.get('creditedSeconds'))} credited {hours(dv.get('hackatimeSeconds'))} devlogged {when(dv.get('createdAt'))}")
        if dv.get('body'):
            L.append(text(dv['body']))
        for c in [c for c in commits if c.get('devlogId') == dv.get('id')]:
            L.append(f"{(c.get('sha') or '')[:7]} {(c.get('message') or '').splitlines()[0] if c.get('message') else ''} {when(c.get('committedAt'))} by {c.get('authorLogin') or c.get('authorName')} +{c.get('additions')} -{c.get('deletions')}")

    files = ev.get('files') or []
    inrepo = [f for f in files if f.get('status') == 'head']
    out = [f for f in files if f.get('status') != 'head']
    L += ['', f"## Files in repo ({hours(sum(f.get('seconds') or 0 for f in inrepo))} total.)"] + file_tree(inrepo)
    L += ['', f"## Files not in repo ({hours(sum(f.get('seconds') or 0 for f in out))} total.)"]
    L += [f"{f.get('path')} {'no longer in repo' if f.get('status') == 'history' else 'never in repo'} {hours(f.get('seconds'))}" for f in out]

    flags = d.get('flags') or []
    danger = sum(1 for f in flags if f.get('severity') == 'danger' and not f.get('dismissedAt'))
    L += ['', '## Flags' + (f' ({danger} danger flag to acknowledge before approving.)' if danger else '')]
    if not flags:
        L.append('No flags')
    for f in flags:
        dis = f" [dismissed by {f.get('dismissedBy')}: {f.get('dismissReason') or ''}]" if f.get('dismissedAt') else ''
        L.append(f"{f.get('severity')} {f.get('title')}{dis}")
        if f.get('details'):
            L.append(json.dumps(f['details'], default=str)[:1500])

    L += ['', '## Project notes (Staff only, kept across every ship of this project.)'] + render_notes(notes)
    return '\n'.join(str(x) for x in L)


def fetch_ship(api, sid):
    """The same three calls the review page makes, minus the claim."""
    d = api.get(f'/review/shipment/{sid}')
    notes = api.get(f"/review/project/{d['shipment']['projectId']}/notes")
    chain = api.get(f'/review/shipment/{sid}/chain') if (d.get('project') or {}).get('parent') else None
    return d, notes, chain


# ---- queue filters (shared by queue and dump) ----

def add_filters(p):
    p.add_argument('--queue', default='review', choices=['review', 'fraud'])
    p.add_argument('--filter', default='waiting', choices=['waiting', 'approved', 'changes', 'rejected'])
    p.add_argument('--week', help='week label or number, e.g. "Prep Week 3", "Week 1" or 8')
    p.add_argument('--platform', action='append', help='keep ships that run on this (web, windows, linux, macos, other), can repeat')
    p.add_argument('--only-platform', action='append', help='keep ships that run on nothing but these, can repeat')
    p.add_argument('--clean', action='store_true', help='no danger or warn flags')
    p.add_argument('--title', help='regex on the ship or project title')
    p.add_argument('--user', help='regex on the username')
    p.add_argument('--reship', action='store_true', help='only ships that had changes requested before')
    p.add_argument('--new', action='store_true', help="leave out ships you already have pages for")
    p.add_argument('--sort', choices=['queued', 'hours', 'title', 'week'], default='queued')
    p.add_argument('--limit', type=int)


def filtered(api, a, weeks):
    items = api.get('/review/queue', {'queue': a.queue, 'filter': a.filter}).get('items', [])
    total = len(items)
    if getattr(a, 'unclaimed', False):
        items = [i for i in items if not i.get('claimedBy') and not i.get('claimedByMe') and not i.get('hold')]
    if a.week:
        items = [i for i in items if str(i.get('weekNumber')) == a.week
                 or week_name(weeks, i.get('weekNumber')).lower() == a.week.lower()]
    for plat in a.platform or []:
        items = [i for i in items if plat in (i.get('platforms') or [])]
    if a.only_platform:
        items = [i for i in items if set(i.get('platforms') or []) <= set(a.only_platform)]
    if a.clean:
        items = [i for i in items if not i.get('danger') and not i.get('warn')]
    if a.title:
        items = [i for i in items if re.search(a.title, f"{i.get('title')} {i.get('projectTitle')}", re.I)]
    if a.user:
        items = [i for i in items if re.search(a.user, i.get('username') or '', re.I)]
    if a.reship:
        items = [i for i in items if i.get('decision')]
    if a.new:
        known = known_ids()
        items = [i for i in items if i['id'] not in known]
    key = {'queued': lambda i: i.get('queuedAt') or '', 'hours': lambda i: -(i.get('seconds') or 0),
           'title': lambda i: (i.get('title') or '').lower(), 'week': lambda i: (i.get('weekNumber') or 0, i.get('queuedAt') or '')}
    items.sort(key=key[a.sort])
    if a.limit:
        items = items[:a.limit]
    return items, total


# ---- prep: clone a ship's repo and gather the mechanical review facts ----
#
# Read-only like the rest. The only things it does are a git clone/fetch into a folder, and GETs to
# code hosts, PyPI and npm through Web, which has its own small allowlist and never sees the Terra
# token. Nothing from the repo is ever run: git runs no hooks on clone, submodules aren't initialized,
# LFS files aren't downloaded unless --lfs, and every git call turns off hooks, credential helpers and
# every transport but https. Release downloads are saved, never run, chmodded or opened.

UA = 'terra-prep (read-only review helper)'
# the only hosts prep sends API requests or downloads to (GET only), plus the ship's own code host for
# self-hosted Gitea/Forgejo/GitLab. GITHUB_TOKEN, when set, only ever goes to api.github.com.
CODE_HOSTS = {'api.github.com', 'github.com', 'objects.githubusercontent.com',
              'release-assets.githubusercontent.com', 'codeberg.org', 'gitea.com', 'gitlab.com',
              'pypi.org', 'registry.npmjs.org'}
# code URLs on these are pages, not git repos
NOT_GIT = ('itch.io', 'github.io', 'github.dev', 'codeforces.com', 'replit.com', 'scratch.mit.edu',
           'google.com', 'youtube.com', 'youtu.be', 'vercel.app', 'netlify.app', 'hackclub.app')
GIT_SAFE = ['-c', 'core.hooksPath=' + os.devnull, '-c', 'credential.helper=', '-c', 'core.fsmonitor=false',
            '-c', 'submodule.recurse=false', '-c', 'core.quotePath=false', '-c', 'advice.detachedHead=false',
            '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always']
NO_LFS = ['-c', 'filter.lfs.smudge=', '-c', 'filter.lfs.process=', '-c', 'filter.lfs.required=false']
MB = 1024 * 1024

VENDORED = re.compile(r'(^|/)(node_modules|vendor|vendored|third_party|external|site-packages|dist|build|addons|site|_site)/'
                      r'|\.min\.(js|css)$|(^|/)(lib|libs)/', re.I)
AI_CONFIG = re.compile(r'(^|/)(CLAUDE\.md|AGENTS\.md|GEMINI\.md|\.cursorrules|\.windsurfrules|copilot-instructions\.md)$'
                       r'|(^|/)\.(cursor|claude|aider[^/]*)/', re.I)
GENERATED = re.compile(rb'generated by|auto-?generated|do not edit', re.I)
BINARY_FILE = re.compile(r'\.(exe|out|o|so|dll|class|pyc|apk|bin)$|(^|/)a\.out$', re.I)
TESTS = re.compile(r'(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]+\.py$|_test\.(go|c|py)$|\.test\.[jt]sx?$', re.I)
EXE_ASSET = re.compile(r'\.(exe|msi|dmg|pkg|deb|rpm|appimage|x86_64|out|elf|apk|aab|ipa|flatpak|snap|jar|uf2|hex|whl|8xp|8xk|b84)$', re.I)
ARCHIVE_ASSET = re.compile(r'\.(zip|tar\.gz|tgz|tar\.xz|7z|rar)$', re.I)
SOURCE_ASSET = re.compile(r'\.(py|js|ts|c|cpp|ino|java|rs|go|lua|sh)$', re.I)
RUN_SECTION = re.compile(r'install|usage|how to (run|play|use)|getting started|run |play|download|setup', re.I)
AI_HEADING = re.compile(r'^#+ .*\b(ai|llm|llms|claude|copilot|chatgpt|gpt|cursor|agents?)\b', re.I | re.M)
DISALLOWED = re.compile(r'streamlit\.app|drive\.google\.com|docs\.google\.com/file', re.I)
VIDEO = re.compile(r'youtube\.com/(watch|shorts|embed)|youtu\.be/|vimeo\.com/|\.(mp4|webm|mov)([?#]|$)', re.I)
MAYBE_VIDEO = re.compile(r'github\.com/user-attachments/assets/', re.I)
HOSTING = re.compile(r'itch\.io|vercel\.app|netlify\.app|github\.io|wokwi\.com|onrender\.com|railway\.app|pages\.dev'
                     r'|hackclub\.app|codeberg\.page|pypi\.org|npmjs\.com|/releases', re.I)
FIRMWARE = [(re.compile(r'(^|/)platformio\.ini$', re.I), 'PlatformIO'),
            (re.compile(r'\.ino$', re.I), 'Arduino .ino'),
            (re.compile(r'(^|/)(sdkconfig(\.[\w-]+)?|idf_component\.yml)$', re.I), 'ESP-IDF'),
            (re.compile(r'\.uf2$', re.I), '.uf2'),
            (re.compile(r'(^|/)project\.pros$|\.v5(code|python|blocks|cpp)$', re.I), 'VEX')]


def note(msg):
    print(f'  {msg}', file=sys.stderr)


def local(iso, fmt='%Y-%m-%d %H:%M'):
    return parse(iso).astimezone().strftime(fmt) if iso else '?'


def local_ts(ts, fmt='%m-%d %H:%M'):
    return datetime.fromtimestamp(ts).strftime(fmt)


def size_str(n):
    if n is None:
        return '?'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


def max_size(s):
    m = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([kmg]?)b?\s*', s, re.I)
    if not m:
        raise argparse.ArgumentTypeError(f'not a size like 500M or 1G: {s!r}')
    return int(float(m.group(1)) * {'k': 1024, 'm': MB, 'g': 1024 * MB, '': MB}[m.group(2).lower()])


class GitError(Exception):
    pass


class Git:
    """git through subprocess, with hooks, credential helpers, LFS and non-https transports off."""

    def __init__(self, path, lfs=False):
        self.path = Path(path)
        self.base = ['git'] + GIT_SAFE + ([] if lfs else NO_LFS)
        self.env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never', LC_ALL='C')
        if not lfs:
            self.env['GIT_LFS_SKIP_SMUDGE'] = '1'

    def cmd(self, *args):
        return self.base + ['-C', str(self.path)] + [str(x) for x in args]

    def run(self, *args, check=True, input=None):
        r = subprocess.run(self.cmd(*args), input=input.encode() if input is not None else None,
                           stdin=None if input is not None else subprocess.DEVNULL, capture_output=True, env=self.env)
        if check and r.returncode:
            err = r.stderr.decode('utf-8', 'replace').strip().splitlines()
            raise GitError(f"git {args[0]} failed: {err[-1] if err else 'exit code ' + str(r.returncode)}")
        return r.stdout.decode('utf-8', 'replace')

    def ok(self, *args):
        return subprocess.run(self.cmd(*args), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=self.env).returncode == 0

    def blob(self, rev, path, limit=MB):
        r = subprocess.run(self.cmd('cat-file', 'blob', f'{rev}:{path}'), stdin=subprocess.DEVNULL,
                           capture_output=True, env=self.env)
        return r.stdout[:limit].decode('utf-8', 'replace') if r.returncode == 0 else None

    def blob_stats(self, shas):
        """{sha: (is_text, lines, first 400 bytes)} for each blob, read by one `git cat-file --batch`."""
        shas = list(dict.fromkeys(shas))
        out = {}
        if not shas:
            return out
        proc = subprocess.Popen(self.cmd('cat-file', '--batch'), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=self.env)

        def feed():
            try:
                proc.stdin.write(''.join(s + '\n' for s in shas).encode())
                proc.stdin.close()
            except OSError:
                pass
        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        for s in shas:
            head = proc.stdout.readline().split()
            if not head:
                break
            if len(head) < 3:
                continue  # "<sha> missing"
            data = proc.stdout.read(int(head[2]))
            proc.stdout.read(1)
            is_text = b'\0' not in data[:8000]
            out[s] = (is_text, data.count(b'\n') if is_text else 0, data[:400])
        writer.join()
        proc.wait()
        return out


class WebError(Exception):
    pass


class Web:
    """GETs to code hosts, PyPI and npm: CODE_HOSTS plus the ship's own code host. Kept apart from the
    Terra client, so it never has the Terra token. Redirects are followed by hand so every hop is
    checked against the allowlist, and GITHUB_TOKEN is only sent to api.github.com."""

    def __init__(self, code_host=None):
        self.allowed = CODE_HOSTS | ({code_host} if code_host else set())
        self.github_token = os.environ.get('GITHUB_TOKEN', '').strip()
        self.calls = {}
        self.github_left = None
        self.github_limited = None

    def open(self, url, accept='application/json'):
        for _ in range(6):
            u = urllib.parse.urlsplit(url)
            host = (u.hostname or '').lower()
            if u.scheme != 'https' or host not in self.allowed:
                raise WebError(f'not fetching {url}: prep only talks https to code hosts, PyPI and npm')
            headers = {'User-Agent': UA, 'Accept': accept}
            if host == 'api.github.com':
                headers['X-GitHub-Api-Version'] = '2022-11-28'
                if self.github_token:
                    headers['Authorization'] = f'Bearer {self.github_token}'
            self.calls[host] = self.calls.get(host, 0) + 1
            try:
                return OPENER.open(urllib.request.Request(url, method='GET', headers=headers), timeout=60)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308) and e.headers.get('Location'):
                    url = urllib.parse.urljoin(url, e.headers['Location'])
                    continue
                raise
        raise WebError(f'too many redirects from {url}')

    def github_seen(self, headers, host):
        if host == 'api.github.com' and headers and headers.get('X-RateLimit-Remaining') is not None:
            self.github_left = int(headers['X-RateLimit-Remaining'])

    def json(self, url):
        """The parsed JSON, or None on a 404."""
        host = urllib.parse.urlsplit(url).hostname
        if host == 'api.github.com' and self.github_limited:
            raise WebError(f'GitHub rate limit (resets {self.github_limited})')
        try:
            with self.open(url) as r:
                self.github_seen(r.headers, host)
                return json.load(r)
        except urllib.error.HTTPError as e:
            self.github_seen(e.headers, host)
            if host == 'api.github.com' and (e.code == 429 or (e.code == 403 and e.headers.get('X-RateLimit-Remaining') == '0')):
                reset = e.headers.get('X-RateLimit-Reset')
                self.github_limited = datetime.fromtimestamp(int(reset)).strftime('%H:%M') if reset else 'within the hour'
                raise WebError(f'GitHub rate limit (resets {self.github_limited})')
            if e.code in (404, 410):
                return None
            raise WebError(f'{e.code} from {url}')
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise WebError(f"couldn't read {url}: {getattr(e, 'reason', e)}")

    def download(self, url, dest, cap):
        """Save url to dest, giving up past cap bytes. Returns the size."""
        got = 0
        try:
            with self.open(url, accept='application/octet-stream') as r, open(dest, 'xb') as f:
                while True:
                    chunk = r.read(MB)
                    if not chunk:
                        return got
                    got += len(chunk)
                    if got > cap:
                        raise WebError(f'bigger than the {size_str(cap)} left under --max-size')
                    f.write(chunk)
        except Exception as e:
            if dest.exists() and got:  # don't leave half a file behind
                dest.unlink()
            if isinstance(e, WebError):
                raise
            if isinstance(e, urllib.error.HTTPError):
                raise WebError(f'{e.code} from {url}')
            if isinstance(e, (urllib.error.URLError, OSError)):
                raise WebError(f"couldn't download {url}: {getattr(e, 'reason', e)}")
            raise


def probe(url):
    """The status code of a demo link: one GET with no tokens or cookies, redirects followed, body not read."""
    hops = []
    for _ in range(6):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ('http', 'https') or not u.hostname:
            return {'url': url, 'error': 'not a web link'}
        try:
            if u.hostname == 'localhost' or not ipaddress.ip_address(u.hostname).is_global:
                return {'url': url, 'error': 'a local address, not checked'}
        except ValueError:
            pass  # a name, not an IP
        req = urllib.request.Request(url, method='GET', headers={
            'User-Agent': f'Mozilla/5.0 (compatible; {UA})', 'Accept': 'text/html,*/*'})
        try:
            with OPENER.open(req, timeout=20) as r:
                return {'url': url, 'status': r.status, 'redirects': hops}
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get('Location'):
                url = urllib.parse.urljoin(url, e.headers['Location'])
                hops.append(url)
                continue
            return {'url': url, 'status': e.code, 'redirects': hops}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {'url': url, 'error': str(getattr(e, 'reason', e)), 'redirects': hops}
    return {'url': url, 'error': 'too many redirects', 'redirects': hops}


def repo_target(p, ev):
    """Where the code lives, from project.codeUrl / repoFullName / branch."""
    url = (p.get('codeUrl') or '').strip()
    if p.get('repoFullName') and (not url or 'github.com' in url.lower()):
        url = f"https://github.com/{p['repoFullName']}"
    if not url:
        return None, 'the project has no code URL'
    u = urllib.parse.urlsplit(url if '://' in url else 'https://' + url)
    host = (u.hostname or '').lower()
    host = host[4:] if host.startswith('www.') else host
    segs = [x for x in urllib.parse.unquote(u.path).split('/') if x]
    if not host or len(segs) < 2 or any(host == h or host.endswith('.' + h) for h in NOT_GIT):
        return None, f"the code URL {url} doesn't look like a git repo"
    branch = None
    if '-' in segs[2:]:  # GitLab: /group/sub/project/-/tree/branch
        i = segs.index('-', 2)
        rest, segs = segs[i + 1:], segs[:i]
        branch = rest[1] if len(rest) >= 2 and rest[0] == 'tree' else None
    elif 'gitlab' in host:
        cut = next((i for i, x in enumerate(segs) if i >= 2 and x in ('tree', 'blob')), len(segs))
        rest, segs = segs[cut:], segs[:cut]
        branch = rest[1] if len(rest) >= 2 else None
    else:
        rest, segs = segs[2:], segs[:2]
        if len(rest) >= 2 and rest[0] == 'tree':  # GitHub
            branch = rest[1]
        elif len(rest) >= 3 and rest[:2] == ['src', 'branch']:  # Gitea, Forgejo
            branch = rest[2]
    if segs[-1].endswith('.git'):
        segs[-1] = segs[-1][:-4]
    path = '/'.join(segs)
    known = host in ('github.com', 'codeberg.org', 'gitea.com', 'gitlab.com')
    return {'host': host, 'path': path, 'url': f'https://{host}/{path}',
            'clone_url': f'https://{host}/{path}' + ('.git' if known else ''),
            'name': re.sub(r'[^A-Za-z0-9._-]', '_', segs[-1]).strip('.') or 'repo',
            'branch': p.get('branch') or (ev.get('repo') or {}).get('branch') or branch}, None


def norm_releases(rels, gitlab=False):
    out = []
    for r in rels if isinstance(rels, list) else []:
        if r.get('draft'):
            continue
        if gitlab:
            assets = [{'name': x.get('name'), 'size': None, 'url': x.get('direct_asset_url') or x.get('url')}
                      for x in (r.get('assets') or {}).get('links') or []]
        else:
            assets = [{'name': x.get('name'), 'size': x.get('size'), 'url': x.get('browser_download_url')}
                      for x in r.get('assets') or []]
        out.append({'tag': r.get('tag_name'), 'name': r.get('name'), 'prerelease': bool(r.get('prerelease')),
                    'published': r.get('published_at') or r.get('released_at') or r.get('created_at'),
                    'url': r.get('html_url') or (r.get('_links') or {}).get('self'), 'assets': assets})
    return out


def host_info(web, t):
    """Repo facts and releases from the code host's API: {'kind', 'meta', 'releases', 'errors'}."""
    host, path = t['host'], t['path']
    info = {'kind': 'other', 'meta': None, 'releases': None, 'errors': []}

    def get(url, what):
        try:
            return web.json(url)
        except WebError as e:
            info['errors'].append(f'{what}: {e}')
            return None

    if host == 'github.com':
        info['kind'] = 'github'
        m = get(f'https://api.github.com/repos/{path}', 'GitHub repo')
        if isinstance(m, dict) and m.get('full_name'):
            owner = m.get('owner') or {}
            info['meta'] = {'full_name': m['full_name'], 'owner': owner.get('login'),
                            'org': owner.get('type') == 'Organization', 'fork': bool(m.get('fork')),
                            'parent': (m.get('parent') or {}).get('full_name'), 'created': m.get('created_at'),
                            'pushed': m.get('pushed_at'), 'pushed_what': 'pushed', 'default_branch': m.get('default_branch'),
                            'homepage': m.get('homepage') or None, 'archived': bool(m.get('archived'))}
            rels = get(f"https://api.github.com/repos/{m['full_name']}/releases?per_page=20", 'GitHub releases')
            info['releases'] = None if rels is None and info['errors'] else norm_releases(rels or [])
        return info

    def gitea():
        m = get(f'https://{host}/api/v1/repos/{path}', 'Gitea/Forgejo repo')
        if not isinstance(m, dict) or 'full_name' not in m:
            return False
        info['kind'] = 'gitea'
        owner = (m.get('owner') or {}).get('login')
        org = get(f'https://{host}/api/v1/orgs/{owner}', 'Gitea/Forgejo org') if owner else None
        info['meta'] = {'full_name': m['full_name'], 'owner': owner, 'org': isinstance(org, dict),
                        'fork': bool(m.get('fork')), 'parent': (m.get('parent') or {}).get('full_name'),
                        'mirror_of': m.get('original_url') if m.get('mirror') else None,
                        'created': m.get('created_at'), 'pushed': m.get('updated_at'), 'pushed_what': 'updated',
                        'default_branch': m.get('default_branch'), 'homepage': m.get('website') or None,
                        'archived': bool(m.get('archived'))}
        rels = get(f'https://{host}/api/v1/repos/{path}/releases?limit=20', 'releases')
        info['releases'] = norm_releases(rels or [])
        return True

    def gitlab():
        m = get(f"https://{host}/api/v4/projects/{urllib.parse.quote(path, safe='')}", 'GitLab project')
        if not isinstance(m, dict) or 'path_with_namespace' not in m:
            return False
        info['kind'] = 'gitlab'
        ns = m.get('namespace') or {}
        fork = m.get('forked_from_project') or {}
        info['meta'] = {'full_name': m['path_with_namespace'], 'owner': ns.get('full_path'),
                        'org': ns.get('kind') == 'group', 'fork': bool(fork), 'parent': fork.get('path_with_namespace'),
                        'created': m.get('created_at'), 'pushed': m.get('last_activity_at'), 'pushed_what': 'last activity',
                        'default_branch': m.get('default_branch'), 'homepage': None, 'archived': bool(m.get('archived'))}
        rels = get(f"https://{host}/api/v4/projects/{m['id']}/releases", 'releases')
        info['releases'] = norm_releases(rels or [], gitlab=True)
        return True

    if 'gitlab' in host:
        gitlab() or gitea()
    else:
        gitea() or gitlab()
    if info['kind'] == 'other':
        info['errors'] = []  # an unknown host answering neither API is expected, not an error
    return info


def asset_kind(name):
    n = (name or '').lower()
    if EXE_ASSET.search(n):
        return 'executable/installer'
    if ARCHIVE_ASSET.search(n):
        if re.search(r'win|linux|mac|darwin|osx|x86|x64|amd64|arm64|aarch64|i686|universal', n):
            return "archive, probably a build (check what's inside)"
        return "archive, check what's inside"
    if SOURCE_ASSET.search(n):
        return 'source file only, not an executable'
    # a real extension, not the ".2-linux-amd64" of a version number
    return 'other' if re.search(r'\.(?=[a-z0-9]{1,8}$)[0-9]*[a-z]', n) else 'no extension (maybe a Linux binary)'


def same_remote(a, b):
    def n(u):
        u = re.sub(r'^[a-z+]+://([^@/]*@)?', '', (u or '').strip().lower()).rstrip('/')
        return u[:-4] if u.endswith('.git') else u
    return n(a) == n(b)


def pick_clone(base, t, lfs):
    """<base>/<name>/, or <name>-2/ and so on when the folder holds something else."""
    for n in range(1, 50):
        d = base / (t['name'] if n == 1 else f"{t['name']}-{n}")
        if not d.exists():
            return d, False
        if (d / '.git').exists() and same_remote(Git(d, lfs).run('remote', 'get-url', 'origin', check=False), t['clone_url']):
            return d, True
    die(f"too many {t['name']}-N folders in {base}")


def free_dir(base, name):
    """<base>/<name>/ if it doesn't exist or is empty, else <name>-2/ and so on."""
    for n in range(1, 50):
        d = base / (name if n == 1 else f'{name}-{n}')
        if not d.exists() or (d.is_dir() and not any(d.iterdir())):
            return d
    die(f'too many {name}-N folders in {base}')


def ship_worktree(g, clone, sha, warn):
    """A worktree <clone>-ship/ checked out at the ship commit."""
    for n in range(1, 50):
        wt = clone.parent / (f'{clone.name}-ship' + ('' if n == 1 else f'-{n}'))
        if not wt.exists():
            g.run('worktree', 'prune')
            g.run('worktree', 'add', '--detach', '--quiet', wt, sha)
            return wt
        w = Git(wt, False)
        common = w.run('rev-parse', '--git-common-dir', check=False).strip()
        if common and (wt / common).resolve() == (clone / '.git').resolve() and wt.resolve() != clone.resolve():
            if w.run('rev-parse', 'HEAD', check=False).strip() != sha and not w.ok('checkout', '--quiet', '--detach', sha):
                warn(f"couldn't move {wt} to the ship commit {sha[:7]} (local changes in it?)")
            return wt
    die(f'too many {clone.name}-ship-N folders')


def unzip(path, dest, budget):
    """Extract a zip into dest with a zip-slip check. Never sets permissions, so nothing becomes executable."""
    skipped = []
    with zipfile.ZipFile(path) as z:
        total = sum(i.file_size for i in z.infolist())
        if total > budget:
            return f'not unzipped: {size_str(total)} unpacked is over --max-size', skipped
        dest.mkdir(parents=True)
        root = dest.resolve()
        for i in z.infolist():
            name = i.filename.replace('\\', '/')
            parts = [x for x in name.split('/') if x not in ('', '.')]
            unsafe = (name.startswith('/') or '..' in parts or (parts and re.match(r'^[A-Za-z]:', parts[0]))
                      or stat.S_ISLNK(i.external_attr >> 16))
            target = root.joinpath(*parts) if parts else root
            if unsafe or (parts and not str(target.resolve()).startswith(str(root) + os.sep)):
                skipped.append(i.filename)
                continue
            if i.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(i) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out, MB)
    return f'unzipped {size_str(total)} into {dest.name}/', skipped


def pick_release(releases, ship_at, tag=None):
    if tag:
        r = next((r for r in releases if r['tag'] == tag), None)
        return r, f'--release {tag}' if r else None
    dated = sorted(((parse(r['published']), r) for r in releases or [] if r.get('published')), key=lambda x: x[0])
    after = [r for at, r in dated if at >= ship_at]
    if after:
        return after[0], 'the first release after the ship'
    if dated:
        return dated[-1][1], 'no release after the ship, so the newest one before it'
    return None, None


def download_release(web, rel, base, name, cap):
    folder = free_dir(base, f'{name}-release')
    folder.mkdir(parents=True, exist_ok=True)
    res = {'tag': rel['tag'], 'dir': str(folder), 'files': [], 'skipped': []}
    left = cap
    for x in rel['assets']:
        fname = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', x.get('name') or 'asset').strip(' .') or 'asset'
        if x.get('size') is not None and x['size'] > left:
            res['skipped'].append(f"{fname} ({size_str(x['size'])}, over --max-size)")
            continue
        dest = folder / fname
        note(f'downloading {fname}')
        try:
            got = web.download(x['url'], dest, left)
        except WebError as e:
            res['skipped'].append(f'{fname}: {e}')
            continue
        left -= got
        entry = {'name': fname, 'size': got}
        if fname.lower().endswith('.zip'):
            out = folder / (fname[:-4] + ('-unzipped' if (folder / fname[:-4]).exists() else ''))
            try:
                entry['unzip'], bad = unzip(dest, out, cap)  # cap per zip, so a zip bomb stops there
                if bad:
                    entry['unsafe_paths_skipped'] = bad[:20]
            except (zipfile.BadZipFile, OSError, ValueError) as e:
                entry['unzip'] = f"couldn't unzip: {e}"
        res['files'].append(entry)
    return res


def norm_link(u):
    return re.sub(r'^https?://(www\.)?', '', u.strip().lower()).split('#')[0].rstrip('/')


def path_index(paths):
    by = {}
    for p in paths:
        by.setdefault(p.rsplit('/', 1)[-1], []).append(p)
    return by


def find_path(e, index):
    """The repo path a Hackatime file path refers to (Hackatime paths can carry a local folder prefix)."""
    for p in index.get(e.rsplit('/', 1)[-1], []):
        if p == e or e.endswith('/' + p) or p.endswith('/' + e):
            return p
    return None


def prep(d, base, a, source, notes=None):
    """Clone or fetch the ship's repo into base and collect the facts. Returns a JSON-able dict."""
    s, p, m = d.get('shipment') or {}, d.get('project') or {}, d.get('maker') or {}
    ev = (d.get('evidence') or {}).get('data') or {}
    ship_at = parse(s['submittedAt']) if s.get('submittedAt') else datetime.now(timezone.utc)
    devlogs = {dv.get('id'): dv.get('title') for dv in d.get('devlogs') or []}
    F = {'ship': {'id': s.get('id'), 'title': s.get('title') or p.get('title'), 'maker': m.get('username'),
                  'review_url': f"{SITE}/admin/review/{s.get('id')}", 'submitted': s.get('submittedAt'),
                  'week': s.get('weekNumber'), 'platforms': s.get('platforms') or [], 'demo_url': p.get('demoUrl'),
                  'code_url': p.get('codeUrl'), 'source': source, 'devlogs': len(devlogs),
                  'staff_notes': len((notes.get('notes') if isinstance(notes, dict) else notes) or []) if notes is not None else None},
         'warnings': []}
    warn = F['warnings'].append

    t, why = repo_target(p, ev)
    if not t:
        die(f'{why}; nothing to clone. Review it by hand from {F["ship"]["review_url"]}')
    web = Web(t['host'])
    base.mkdir(parents=True, exist_ok=True)

    # ---- the code host's API (2 GitHub requests, 3 on Gitea/Forgejo, 2 on GitLab) ----
    hi = host_info(web, t)
    meta = hi['meta']
    for e in hi['errors']:
        warn(e + (' (60 requests an hour without GITHUB_TOKEN; set it for 5,000)' if 'rate limit' in e and not web.github_token else ''))
    if t['host'] == 'github.com' and not meta and not hi['errors']:
        warn(f"GitHub says {t['path']} doesn't exist (private, renamed or deleted?)")
    if meta and meta['full_name'].lower() != t['path'].lower() and hi['kind'] == 'github':
        t['path'] = meta['full_name']  # renamed or transferred: GitHub redirected us
        t['url'], t['clone_url'] = f"https://github.com/{t['path']}", f"https://github.com/{t['path']}.git"

    # ---- clone or fetch ----
    clone, existed = pick_clone(base, t, a.lfs)
    g = Git(clone, a.lfs)
    try:
        if existed:
            note(f'fetching into {clone}')
            g.run('fetch', '--quiet', '--prune', '--tags', 'origin')
        else:
            note(f"cloning {t['clone_url']} into {clone}")
            Git(base, a.lfs).run('clone', '--quiet', '--no-recurse-submodules', t['clone_url'], clone)
    except GitError as e:
        die(f"{e}\n(private repo, wrong code URL, or the host isn't a git server?)")
    default = (meta or {}).get('default_branch') or g.run('symbolic-ref', '--short', 'refs/remotes/origin/HEAD',
                                                           check=False).strip().replace('origin/', '', 1)
    branch = t['branch'] if t['branch'] and g.ok('rev-parse', '--verify', '--quiet', f"origin/{t['branch']}") else None
    if t['branch'] and not branch:
        warn(f"Terra says branch {t['branch']}, but the repo has no such branch; used {default or 'the default'}")
    branch = branch or default
    head = f'origin/{branch}' if branch and g.ok('rev-parse', '--verify', '--quiet', f'origin/{branch}') else 'HEAD'
    if existed:
        g.ok('merge', '--ff-only', '--quiet', '@{u}')  # bring their checkout up to date if it's clean
    elif branch and branch != default:
        g.ok('checkout', '--quiet', branch)
    if a.lfs and g.ok('lfs', 'version'):
        g.ok('lfs', 'pull')
    head_sha = g.run('rev-parse', head).strip()
    F['repo'] = {'host': t['host'], 'kind': hi['kind'], 'url': t['url'], 'branch': branch, 'dir': str(clone),
                 'fetched': existed, 'head': head_sha, **(meta or {})}

    # ---- the ship commit ----
    ev_commits = [c for c in ev.get('commits') or [] if c.get('sha')]
    known = set()
    if ev_commits:
        out = g.run('cat-file', '--batch-check', input=''.join(c['sha'] + '\n' for c in ev_commits), check=False)
        known = {line.split()[0] for line in out.splitlines() if ' commit ' in line}
    on_devlog = [c for c in ev_commits if c.get('devlogId') and c['sha'] in known]
    before = [c for c in ev_commits if c['sha'] in known and c.get('committedAt') and parse(c['committedAt']) <= ship_at]
    if on_devlog:
        ship_sha, how = max(on_devlog, key=lambda c: parse(c['committedAt']))['sha'], 'the newest commit attached to a devlog'
    elif before:
        ship_sha, how = max(before, key=lambda c: parse(c['committedAt']))['sha'], "the newest of Terra's commits before the ship"
    else:
        ship_sha = g.run('rev-list', '-1', f'--before=@{int(ship_at.timestamp())}', head, check=False).strip()
        how = 'the newest commit before the ship time (none attached to a devlog)'
    missing = [c for c in ev_commits if c['sha'] not in known]
    if missing:
        warn(f"{len(missing)} of Terra's {len(ev_commits)} commits aren't in the repo any more (force-pushed or rewritten history?)")
    if not ship_sha:
        warn(f'no commits before the ship time ({local(s.get("submittedAt"))}); was anything pushed? Using {branch or "HEAD"}')
        ship_sha, how = head_sha, 'no commit before the ship, so the newest one'

    commits = []
    cur = None
    for line in g.run('log', '--reverse', '--format=@@%H%x09%at%x09%ct%x09%s', '--shortstat', head).splitlines():
        if line.startswith('@@'):
            sha, ts, cts, subj = (line[2:].split('\t', 3) + ['', '', ''])[:4]
            cur = {'sha': sha, 'time': int(ts or 0), 'committed': int(cts or 0), 'subject': subj,
                   'added': 0, 'deleted': 0, 'files': 0}
            commits.append(cur)
        elif cur and 'changed' in line:
            for key, word in (('files', 'file'), ('added', 'insertion'), ('deleted', 'deletion')):
                mm = re.search(rf'(\d+) {word}', line)
                if mm:
                    cur[key] = int(mm.group(1))
    after = set(g.run('rev-list', f'{ship_sha}..{head}').split())
    ev_by_sha = {c['sha']: c for c in ev_commits}
    for c in commits:
        c['after_ship'] = c['sha'] in after
        # after the ship commit but still before the ship: work no devlog covers
        c['unlogged_before_ship'] = c['after_ship'] and c['committed'] <= ship_at.timestamp()
        c['devlog'] = devlogs.get((ev_by_sha.get(c['sha']) or {}).get('devlogId'))
        c['big'] = c['added'] >= 300
    on_head = {c['sha'] for c in commits}
    ship_c = next((c for c in commits if c['sha'] == ship_sha), None)
    if not ship_c:
        line = g.run('log', '-1', '--format=%H%x09%at%x09%s', ship_sha).strip().split('\t', 2) + ['', '']
        ship_c = {'sha': line[0], 'time': int(line[1] or 0), 'subject': line[2]}
        warn(f'the ship commit {ship_sha[:7]} is not on {branch} (another branch, or history rewritten after the ship)')
    F['ship_commit'] = {'sha': ship_sha, 'short': ship_sha[:7], 'subject': ship_c['subject'], 'time': ship_c['time'],
                        'how': how, 'after': len(after), 'worktree': None}
    if ship_sha != head_sha:
        wt = ship_worktree(g, clone, ship_sha, warn)
        F['ship_commit']['worktree'] = str(wt)
        if after:
            warn(f'{len(after)} commit(s) after the ship commit; review the code as shipped in {wt}')
    F['commits'] = commits
    unlogged = [c for c in commits if c['unlogged_before_ship']]
    if unlogged and on_devlog:
        warn(f"{len(unlogged)} commit(s) after the ship commit were made before the ship but aren't attached to a devlog "
             f"(+{sum(c['added'] for c in unlogged)} lines); the code at ship time is {unlogged[-1]['sha'][:7]} "
             f"(git checkout {unlogged[-1]['sha'][:7]} in the -ship worktree)")
    F['terra_commits'] = {'total': len(ev_commits), 'in_repo': len(known), 'on_other_branches': len(known - on_head),
                          'truncated': bool(ev.get('commitsTruncated'))}

    # ---- repo facts ----
    if meta:
        if meta.get('org'):
            warn(f"repo is under an org ({meta['owner']}), so check it's the submitter's own work and not team/school work")
        if meta.get('fork'):
            warn(f"repo is a fork of {meta.get('parent') or '?'}; compare against the parent to see what's theirs")
        if meta.get('mirror_of'):
            warn(f"repo is a mirror of {meta['mirror_of']}")
        if meta.get('pushed'):
            F['repo']['pushed_after_ship'] = parse(meta['pushed']) > ship_at

    # ---- identities and branches (counts only: the output gets pasted into chats) ----
    ids = {}
    names = set()
    for line in g.run('log', '--all', '--format=%aN%x00%aE').splitlines():
        name, _, email = line.partition('\0')
        ids[email.strip().lower()] = ids.get(email.strip().lower(), 0) + 1
        names.add(name.strip().lower())
    F['identities'] = {'emails': len(ids), 'names': len(names), 'commits_each': sorted(ids.values(), reverse=True)}
    if len(ids) > 1:
        warn(f'{len(ids)} different commit emails ({len(names)} names): another person, or a school/work account?')
    branches = []
    for br in g.run('for-each-ref', '--format=%(refname:short)', 'refs/remotes/origin').split():
        if br in ('origin', 'origin/HEAD', f'origin/{branch}'):
            continue
        branches.append({'name': br, 'not_on_branch': int(g.run('rev-list', '--count', f'{head}..{br}').strip() or 0)})
    F['branches'] = branches

    # ---- commit oddities ----
    big = [c for c in commits if c['big']]
    if big:
        warn(f'{len(big)} commit(s) with 300+ added lines: copied/vendored code, or a big paste? (git show --stat <hash>)')
    ooo, prev = [], None
    for line in g.run('log', '--reverse', '--topo-order', '--no-merges', '--format=%h%x09%at%x09%s', head).splitlines():
        sha, ts, subj = (line.split('\t', 2) + ['', ''])[:3]
        if prev is not None and int(ts) < prev:
            ooo.append({'sha': sha, 'time': int(ts), 'subject': subj})
        prev = int(ts)
    F['out_of_order'] = ooo
    if ooo:
        warn(f'{len(ooo)} commit(s) dated earlier than the one before them (parallel branches/agent worktrees?), '
             'compare with the AI/LLM section')

    # ---- files at the ship commit ----
    tree, submodules = {}, []
    for entry in g.run('ls-tree', '-r', '-l', '-z', ship_sha).split('\0'):
        if '\t' not in entry:
            continue
        info, path = entry.split('\t', 1)
        mode, kind, sha, size = info.split()
        if kind == 'commit':
            submodules.append(path)
        elif kind == 'blob' and mode != '120000':
            tree[path] = (sha, int(size) if size.isdigit() else 0)
    stats = g.blob_stats([sha for sha, size in tree.values() if size <= 2 * MB])
    by_ext, text_paths, lfs_files = {}, {}, []
    for path, (sha, size) in tree.items():
        st = stats.get(sha)
        if not st or not st[0]:
            continue
        if st[2].startswith(b'version https://git-lfs.github.com/spec/'):
            lfs_files.append(path)
            continue
        text_paths[path] = st
        b = path.rsplit('/', 1)[-1]
        ext = b[b.rindex('.'):] if '.' in b else '(no ext)'
        e = by_ext.setdefault(ext, [0, 0])
        e[0] += st[1]
        e[1] += 1
    F['files'] = {'at': ship_sha[:7], 'count': len(tree), 'text_lines': sum(v[1] for v in text_paths.values()),
                  'by_ext': [{'ext': k, 'lines': v[0], 'files': v[1]} for k, v in sorted(by_ext.items(), key=lambda x: -x[1][0])],
                  'submodules': submodules, 'lfs': lfs_files}
    vend = {}
    for path in tree:
        if VENDORED.search(path):
            parts = path.split('/')
            key = '/'.join(parts[:2]) if len(parts) > 2 else parts[0]
            v = vend.setdefault(key, [0, 0])
            v[0] += 1
            v[1] += text_paths.get(path, (0, 0))[1]
    F['files']['vendored'] = [{'dir': k, 'files': v[0], 'lines': v[1]} for k, v in sorted(vend.items(), key=lambda x: -x[1][0])]
    if vend:
        warn(f"possible vendored/plugin/generated code, ~{sum(v[1] for v in vend.values())} lines in "
             f"{', '.join(list(vend)[:4])} (don't count it as theirs)")
    gen = [{'path': pth, 'lines': st[1]} for pth, st in text_paths.items() if GENERATED.search(st[2])]
    F['files']['generated'] = gen
    if gen:
        warn(f"{len(gen)} generated file(s), not their code: {', '.join(x['path'] for x in gen[:4])}")
    ai = [pth for pth in tree if AI_CONFIG.search(pth)]
    F['files']['ai_config'] = ai
    if ai:
        warn(f"AI tool config in repo (check the AI declaration matches): {', '.join(ai[:5])}")
    F['files']['binaries'] = [pth for pth in tree if BINARY_FILE.search(pth)][:10]
    F['files']['tests'] = [pth for pth in tree if TESTS.search(pth)][:10]
    if submodules:
        warn(f"git submodules (not initialized, their code isn't in the clone): {', '.join(submodules[:4])}")

    # ---- renames and deletions, against Terra's "not in repo" files ----
    moves = []
    c = None
    for line in g.run('log', '--all', '-M', '--diff-filter=RD', '--name-status', '--format=@@%h %at').splitlines():
        if line.startswith('@@'):
            c = line[2:].split()
        elif line[:1] in ('R', 'D') and c:
            parts = line.split('\t')
            moves.append({'sha': c[0], 'time': int(c[1]), 'kind': 'renamed' if parts[0][0] == 'R' else 'deleted',
                          'path': parts[1], 'to': parts[2] if parts[0][0] == 'R' and len(parts) > 2 else None})
    F['moves'] = moves
    out_files = sorted((f for f in ev.get('files') or [] if f.get('status') != 'head'), key=lambda f: -(f.get('seconds') or 0))
    if out_files:
        ever = set(g.run('log', '--all', '--format=', '--name-only').split('\n')) - {''}
        idx_ship, idx_ever = path_index(tree), path_index(ever)
        moved = {}
        for mv in reversed(moves):  # newest last, so the latest move of a path wins
            moved[mv['path']] = mv
        idx_moved = path_index(moved)
        checked = []
        for f in out_files:
            e = re.sub(r'^(\./)+', '', f.get('path') or '')
            hit = find_path(e, idx_ship)
            if hit:
                why = f'in the repo at the ship commit as {hit}' if hit != e else 'in the repo at the ship commit'
            elif find_path(e, idx_moved):
                mv = moved[find_path(e, idx_moved)]
                why = (f"renamed to {mv['to']} in {mv['sha']} ({local_ts(mv['time'])})" if mv['kind'] == 'renamed'
                       else f"deleted in {mv['sha']} ({local_ts(mv['time'])})")
            elif find_path(e, idx_ever):
                why = 'only in git history (another branch or an older commit)'
            else:
                why = 'never committed to this repo'
            checked.append({'path': f.get('path'), 'terra': f.get('status'), 'seconds': f.get('seconds') or 0, 'git': why})
        never = sum(x['seconds'] for x in checked if x['git'] == 'never committed to this repo')
        tracked = sum(f.get('seconds') or 0 for f in ev.get('files') or [])
        F['not_in_repo'] = {'external_host': (ev.get('repo') or {}).get('host') == 'external', 'files': checked,
                            'never_committed_seconds': never}
        if never >= 3600 or (tracked and never / tracked >= 0.2 and never >= 600):
            warn(f'{hours(never)} of tracked time is on files never committed to this repo (local-only, ignored, or another project?)')

    # ---- README ----
    root_readmes = [pth for pth in tree if '/' not in pth and re.fullmatch(r'readme(\.[a-z0-9]+)?', pth, re.I)]
    sub_readme = (ev.get('repo') or {}).get('readmePath')
    rpath = (root_readmes or [sub_readme if sub_readme in tree else None])[0] or next(
        (pth for pth in sorted(tree, key=lambda x: x.count('/')) if re.search(r'(^|/)readme(\.[a-z0-9]+)?$', pth, re.I)), None)
    readme = g.blob(ship_sha, rpath) if rpath else None
    demo = (p.get('demoUrl') or '').strip()
    links = []
    R = {'path': rpath, 'lines': None}
    F['readme'] = R
    if readme is None:
        warn('no README')
    else:
        R['lines'] = readme.count('\n')
        if '/' in rpath:
            warn(f'README is in a subfolder ({rpath}), not at the top of the repo')
        found = re.findall(r'https?://[^\])\s>"`<\']+', readme)
        links = sorted({u.rstrip('.,;:') for u in found
                        if not re.search(r'shields\.io|badge|img\.|\.(png|jpe?g|gif|svg|webp)([?#]|$)', u, re.I)})
        R['links'] = links
        R['headings'] = [h.strip() for h in re.findall(r'^#{1,3} .*$', readme, re.M)][:30]
        R['run_section'] = bool(RUN_SECTION.search(readme))
        R['ai_section'] = [ln.strip() for ln in readme.splitlines() if AI_HEADING.match(ln)][:3]
        if not R['run_section']:
            warn('README has no obvious install/run/usage section')
        text_norm = re.sub(r'https?://(www\.)?', '', readme.lower())
        repo_page = norm_link(t['url'])
        if demo:
            dn = norm_link(demo)
            if dn == repo_page or (dn.startswith(repo_page + '/') and 'release' not in dn):
                R['demo_linked'] = None  # the demo is the repo itself, or a file in it
            elif dn.startswith(repo_page + '/releases') or dn.startswith(repo_page + '/-/releases'):
                R['demo_linked'] = '/releases' in text_norm or 'release' in text_norm
            else:
                R['demo_linked'] = dn in text_norm or dn.split('?')[0] in text_norm
            if R['demo_linked'] is False:
                warn(f"the demo link {demo} isn't in the README; add it so people can find the demo")
        hp = (meta or {}).get('homepage')
        if hp and norm_link(hp) not in text_norm:
            warn(f"repo homepage {hp} isn't linked in the README")
        R['hosting_links'] = [u for u in links if HOSTING.search(u)]
    bad = [u for u in links + ([demo] if demo else []) if DISALLOWED.search(u)]
    if bad:
        warn(f"disallowed host (Streamlit / Google Drive): {', '.join(sorted(set(bad))[:3])}")

    # ---- links and packages ----
    L = {'demo': None, 'pypi': None, 'npm': None}
    F['links'] = L
    demo_pkg = re.search(r'(pypi\.org/project|npmjs\.com/package)/((?:@[^/?#]+/)?[^/?#]+)', demo or '')
    if demo:
        dn = norm_link(demo)
        if not demo_pkg:
            note(f'checking the demo link {demo}')
        # npmjs.com and pypi.org turn scripts away (403), so a package page is checked through the registry below
        L['demo'] = {'url': demo, 'via': 'registry'} if demo_pkg else probe(demo)
        L['demo']['kind'] = ('repo page' if dn == norm_link(t['url']) else
                             'releases page' if dn.startswith(norm_link(t['url'])) and 'release' in dn else
                             'repo file' if dn.startswith(norm_link(t['url'])) else
                             'package page' if re.search(r'pypi\.org|npmjs\.com', dn) else
                             'video' if VIDEO.search(demo) else
                             'code host' if urllib.parse.urlsplit(demo).hostname in ('github.com', 'codeberg.org', 'gitlab.com') else
                             'web')
        code = L['demo'].get('status')
        if L['demo'].get('error'):
            warn(f"demo link {demo}: {L['demo']['error']}")
        elif code and code >= 400:
            warn(f'demo link {demo} answers {code}' + (' (may be bot protection; open it in the browser)' if code in (401, 403, 429, 503) else ''))
    repo_l = t['path'].lower()

    def pkg(kind, name, url, page):
        try:
            j = web.json(url)
        except WebError as e:
            return {'name': name, 'error': str(e)}
        if j is None:
            return {'name': name, 'found': False}
        info = j.get('info') if kind == 'pypi' else j
        version = info.get('version')
        uploaded = next((u.get('upload_time_iso_8601') for u in j.get('urls') or []), None) if kind == 'pypi' else None
        return {'name': name, 'found': True, 'url': page, 'version': version, 'uploaded': uploaded,
                'links_back': repo_l in json.dumps(info, default=str).lower()}
    pyproject = g.blob(ship_sha, 'pyproject.toml') if 'pyproject.toml' in tree else None
    pyname = None
    if pyproject:
        mm = re.search(r'^\s*name\s*=\s*["\']([^"\']+)', pyproject, re.M)
        pyname = mm and mm.group(1)
    for fname, rx in (('setup.cfg', r'^name\s*=\s*([^\s#]+)'), ('setup.py', r'name\s*=\s*["\']([^"\']+)')):
        if not pyname and fname in tree:
            mm = re.search(rx, g.blob(ship_sha, fname) or '', re.M)
            pyname = mm and mm.group(1)
    if pyname:
        L['pypi'] = pkg('pypi', pyname, f"https://pypi.org/pypi/{urllib.parse.quote(pyname)}/json",
                        f'https://pypi.org/project/{pyname}/')
    if 'package.json' in tree:
        try:
            pj = json.loads(g.blob(ship_sha, 'package.json') or '{}')
        except ValueError:
            pj = {}
        if isinstance(pj, dict) and pj.get('name') and not pj.get('private'):
            L['npm'] = pkg('npm', pj['name'], f"https://registry.npmjs.org/{urllib.parse.quote(pj['name'], safe='@')}/latest",
                           f"https://www.npmjs.com/package/{pj['name']}")
        elif isinstance(pj, dict) and pj.get('private'):
            L['npm'] = {'name': pj.get('name'), 'private': True}
    if demo_pkg:
        k, name = ('pypi' if 'pypi' in demo_pkg.group(1) else 'npm'), urllib.parse.unquote(demo_pkg.group(2))
        if not L[k] or L[k].get('name') != name:
            L[k] = (pkg('pypi', name, f'https://pypi.org/pypi/{urllib.parse.quote(name)}/json', f'https://pypi.org/project/{name}/')
                    if k == 'pypi' else
                    pkg('npm', name, f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}/latest",
                        f'https://www.npmjs.com/package/{name}'))
        L[k]['from_demo'] = True
        L['demo']['status'] = 200 if L[k].get('found') else 404 if L[k].get('found') is False else None
        if L[k].get('error'):
            L['demo']['error'] = L[k]['error']
        elif not L[k].get('found'):
            warn(f'the demo link {demo} is a package page, but {name} is not published there')
    for k in ('pypi', 'npm'):
        x = L[k]
        if x and x.get('found') and not x.get('links_back'):
            warn(f"the {'PyPI' if k == 'pypi' else 'npm'} package {x['name']} doesn't link back to this repo; "
                 + ("check it's theirs" if x.get('from_demo') else "someone else's package with the same name?"))

    # ---- releases ----
    rels = hi['releases']
    F['releases'] = rels
    exe = []
    for r in rels or []:
        r['after_ship'] = bool(r.get('published')) and parse(r['published']) > ship_at
        for x in r['assets']:
            x['kind'] = asset_kind(x['name'])
            if x['kind'] in ('executable/installer', 'no extension (maybe a Linux binary)'):
                exe.append(f"{r['tag']}: {x['name']}")
            elif x['kind'].startswith('archive, probably a build'):
                exe.append(f"{r['tag']}: {x['name']} (archive)")
    tag = re.search(r'/releases/tag/([^/?#]+)', demo or '')
    F['demo_release_tag'] = urllib.parse.unquote(tag.group(1)) if tag else None

    # ---- firmware (hardware isn't a Terra ship, but firmware is: the demo is a video or a sim) ----
    fw = sorted({label for rx, label in FIRMWARE for pth in tree if rx.search(pth)}
                | {'.uf2 release' for r in rels or [] for x in r['assets'] if (x['name'] or '').lower().endswith('.uf2')})
    videos = sorted({u for u in links + ([demo] if demo else []) if VIDEO.search(u)})
    maybe = sorted({u for u in links + ([demo] if demo else []) if MAYBE_VIDEO.search(u)})
    sims = sorted({u for u in links + ([demo] if demo else []) if 'wokwi.com' in u.lower()})
    F['firmware'] = {'signs': fw, 'videos': videos, 'maybe_videos': maybe, 'sims': sims,
                     'wokwi_files': [pth for pth in tree if re.search(r'(^|/)(diagram\.json|wokwi\.toml)$', pth)]} if fw else None
    if fw:
        warn('firmware: needs a video of it running on the hardware (or a web sim like Wokwi) as the demo; '
             + (f'video link found: {videos[0]}' if videos else f'sim link found: {sims[0]}' if sims else
                f'no YouTube/Vimeo/.mp4 link, but maybe a GitHub video: {maybe[0]}' if maybe else
                'no video link in the README or the demo URL'))

    # ---- the ship checklist line ----
    dm = L['demo'] or {}
    web_demo = demo if dm.get('kind') == 'web' and dm.get('status') and dm['status'] < 400 else None
    package = [f"{'PyPI' if k == 'pypi' else 'npm'} {L[k]['name']}" for k in ('pypi', 'npm')
               if L[k] and L[k].get('found') and (L[k].get('links_back') or L[k].get('from_demo'))]
    F['checklist'] = {'web_demo': web_demo, 'release_build': exe[:5], 'package': package,
                      'releases_unknown': rels is None}
    if not (web_demo or exe or package or fw):  # firmware has its own warning above
        warn('no web demo, executable release, or package found. Desktop apps/CLIs/games need one of these to count as shipped'
             + (' (releases unknown: the API call failed)' if rels is None else ''))

    # ---- --download ----
    if a.download:
        rel, why = pick_release(rels or [], ship_at, a.release)
        if not a.release and F.get('demo_release_tag'):      # the release the player points reviewers at wins
            by_demo, _ = pick_release(rels or [], ship_at, F['demo_release_tag'])
            if by_demo:
                rel, why = by_demo, f"the release the demo link points at ({F['demo_release_tag']})"
        if rels is None:
            warn("--download: couldn't list releases")
        elif not rel:
            warn(f'--download: no release {a.release}' if a.release else '--download: no releases to download')
        elif not rel['assets']:
            warn(f"--download: release {rel['tag']} has no files attached")
        else:
            F['download'] = {'why': why, **download_release(web, rel, base, clone.name, a.max_size)}
            for x in F['download']['files']:
                if x.get('unsafe_paths_skipped'):
                    warn(f"{x['name']} has unsafe paths (zip-slip or symlinks), skipped: {', '.join(x['unsafe_paths_skipped'][:3])}")

    F['requests'] = {'code_hosts': web.calls, 'github_left': web.github_left, 'github_token': bool(web.github_token)}
    if web.github_left is not None and web.github_left < 10 and not web.github_token:
        warn(f'only {web.github_left} GitHub API requests left this hour; set GITHUB_TOKEN for 5,000')
    return F


def prep_text(F):
    L = []
    S, R, SC = F['ship'], F['repo'], F['ship_commit']

    def h(title):
        L.extend(['', f'== {title} =='])

    def commit_line(c):
        marks = (f"  [devlog: {c['devlog'][:40]}]" if c.get('devlog') else '') \
            + ('  [no devlog, but before the ship]' if c.get('unlogged_before_ship') else '  [after ship]' if c['after_ship'] else '') \
            + ('  [big, check what is in it]' if c['big'] else '')
        return (f"  {c['sha'][:7]}  {local_ts(c['time'])}  +{c['added']:<5} -{c['deleted']:<5} {c['files']:>3} files  "
                f"{c['subject'][:60]}{marks}")

    L.append(f"{S['title']} by {S['maker']}, shipped {local(S['submitted'])}")
    L.append(f"{S['review_url']}  ({S['source']})")
    L.append(f"repo     {R['url']}" + (f" (branch {R['branch']})" if R.get('branch') else '') + f"  ->  {R['dir']}"
             + (' (fetched)' if R['fetched'] else ' (cloned)'))
    L.append(f"ship     {SC['short']} {SC['subject'][:60]} ({local_ts(SC['time'])}), {SC['how']}")
    if SC['worktree']:
        L.append(f"         {SC['after']} commit(s) after it; the code as shipped is in {SC['worktree']}")
    else:
        L.append('         nothing committed after it, the clone is the code as shipped')
    L.append(f"demo     {S['demo_url'] or '-'}")

    h('repo')
    if R.get('full_name'):
        L.append(f"host     {R['kind']} ({R['owner']}, {'an org' if R.get('org') else 'a user'})"
                 + (', archived' if R.get('archived') else ''))
        L.append(f"created  {local(R.get('created'))}")
        if R.get('pushed'):
            L.append(f"{R['pushed_what']:<8} {local(R['pushed'])}"
                     + (' (after the ship)' if R.get('pushed_after_ship') else ' (before the ship, nothing new since)'))
        L.append(f"default  {R.get('default_branch')}   fork: {'yes, of ' + (R.get('parent') or '?') if R.get('fork') else 'no'}"
                 + (f"   mirror of {R['mirror_of']}" if R.get('mirror_of') else ''))
        if R.get('homepage'):
            L.append(f"homepage {R['homepage']}")
    else:
        L.append(f"host     {R['host']} ({'no API facts' if R['kind'] == 'other' else 'API call failed'}; git only)")
    T = F['terra_commits']
    if T['total']:
        L.append(f"Terra    {T['in_repo']} of {T['total']} commits found in the clone"
                 + (f", {T['on_other_branches']} on other branches" if T['on_other_branches'] else '')
                 + (' (Terra truncated the list)' if T['truncated'] else ''))
    else:
        L.append('Terra    no commits in the evidence (Terra only reads GitHub), ship commit found by time')

    commits = F['commits']
    h(f"commits ({len(commits)}, oldest first, local time)")
    before = [c for c in commits if not c['after_ship']]
    after = [c for c in commits if c['after_ship']]
    L.append(f'up to the ship commit ({len(before)}):')
    if len(before) > 60:
        L.append(f'  ... {len(before) - 60} older commits not shown')
    L += [commit_line(c) for c in before[-60:]]
    if after:
        L.append(f'after the ship commit ({len(after)}):')
        L += [commit_line(c) for c in after[:60]]
        if len(after) > 60:
            L.append(f'  ... {len(after) - 60} more')
    L.append(f"{len(commits)} commits, +{sum(c['added'] for c in commits)} lines total")

    h('identities and branches')
    I = F['identities']
    L.append(f"{I['emails']} commit email(s), {I['names']} name(s); commits each: "
             + ', '.join(str(x) for x in I['commits_each'][:8]) + ' (emails not shown)')
    if F['branches']:
        L += [f"  {b['name']:<30} {b['not_on_branch']} commits not on {R['branch']}" for b in F['branches'][:15]]
    else:
        L.append(f"no other branches than {R['branch']}")

    h('commit oddities')
    big = [c for c in commits if c['big']]
    L += [f"  big: {c['sha'][:7]} +{c['added']} {c['subject'][:60]}" for c in big[:10]]
    L += [f"  out of order: {c['sha']} {local_ts(c['time'])} {c['subject'][:60]}" for c in F['out_of_order'][:8]]
    if not big and not F['out_of_order']:
        L.append('none')

    h('files')
    if F['moves']:
        L.append(f"renames and deletions ({len(F['moves'])}, often explains Terra's 'not in repo' files):")
        L += [f"  {mv['sha']} {local_ts(mv['time'])}  {mv['kind']}  {mv['path']}" + (f" -> {mv['to']}" if mv['to'] else '')
              for mv in F['moves'][:20]]
        if len(F['moves']) > 20:
            L.append(f"  ... {len(F['moves']) - 20} more")
    NR = F.get('not_in_repo')
    if NR:
        files = NR['files']
        L.append(f"Terra's 'not in repo' files ({len(files)}, {hours(sum(x['seconds'] for x in files))})"
                 + (", every file shows like this because Terra can't read this host" if NR['external_host'] else '') + ':')
        counts = {}
        for x in files:
            k = re.sub(r' (as|to|in) .*', '', x['git'])
            counts[k] = counts.get(k, 0) + 1
        L.append('  ' + ', '.join(f'{n} {k}' for k, n in counts.items()))
        shown = [x for x in files if x['seconds'] >= 60 and not (NR['external_host'] and x['git'].startswith('in the repo'))]
        L += [f"  {x['path']} ({hours(x['seconds'])}): {x['git']}" for x in shown[:20]]
        if len(shown) > 20:
            L.append(f'  ... {len(shown) - 20} more with time on them')
    FL = F['files']
    L.append(f"lines by extension at {FL['at']} ({FL['count']} files, {FL['text_lines']} lines of text):")
    L += [f"  {e['lines']:>7} lines  {e['files']:>4} files  {e['ext']}" for e in FL['by_ext'][:15]]
    for key, label in (('vendored', 'vendored/plugin/build folders'), ('generated', 'generated files'),
                       ('ai_config', 'AI tool config'), ('binaries', 'committed binaries'), ('tests', 'tests, try running them'),
                       ('submodules', 'submodules (not initialized)'),
                       ('lfs', 'Git LFS files (pointers only, --lfs downloads them)')):
        v = FL.get(key)
        if v:
            items = [f"{x['dir']} ({x['files']} files, {x['lines']} lines)" if key == 'vendored' else
                     f"{x['path']} ({x['lines']} lines)" if key == 'generated' else x for x in v[:6]]
            L.append(f'{label}: ' + ', '.join(items))

    RM = F['readme']
    h(f"README ({RM['path']} at {FL['at']}, {RM['lines']} lines)" if RM['path'] else 'README')
    if not RM['path']:
        L.append('none')
    else:
        L.append('run section: ' + ('yes' if RM['run_section'] else 'no'))
        if RM.get('demo_linked') is not None:
            L.append('demo link in it: ' + ('yes' if RM['demo_linked'] else 'no'))
        L.append('AI/LLM section: ' + ('; '.join(RM['ai_section']) if RM['ai_section'] else 'none (check the AI declaration on Terra)'))
        if RM['headings']:
            L.append('headings: ' + ' | '.join(x.lstrip('# ') for x in RM['headings'][:20]))
        if RM['links']:
            L.append(f"links ({len(RM['links'])}):")
            L += [f'  {u}' for u in RM['links'][:25]]

    h('links and packages')
    LK = F['links']
    dm = LK['demo']
    if dm:
        res = dm.get('error') or dm.get('status')
        L.append(f"demo   {F['ship']['demo_url']} ({dm['kind']}{', checked through the registry' if dm.get('via') else ''}): {res}"
                 + (f" after redirects to {dm['redirects'][-1]}" if dm.get('redirects') else ''))
    else:
        L.append('demo   none given')
    for k, label in (('pypi', 'PyPI'), ('npm', 'npm')):
        x = LK[k]
        if not x:
            continue
        if x.get('private'):
            L.append(f"{label:<6} package.json is private, not published")
        elif x.get('error'):
            L.append(f"{label:<6} {x['name']}: {x['error']}")
        elif x.get('found'):
            L.append(f"{label:<6} {x['url']} v{x['version']}" + (f", uploaded {local(x['uploaded'])}" if x.get('uploaded') else '')
                     + ('' if x['links_back'] else ", doesn't link to this repo"))
        else:
            L.append(f"{label:<6} not published as {x['name']}")
    if not LK['pypi'] and not LK['npm']:
        L.append('no pyproject.toml/setup.py or package.json name at the top of the repo')

    h('releases')
    rels = F['releases']
    if rels is None:
        L.append('unknown (the API call failed)' if R['kind'] != 'other' else f"not checked ({R['host']} has no API this knows)")
    elif not rels:
        L.append('none')
    for r in (rels or [])[:8]:
        L.append(f"{r['tag']}  (published {local(r.get('published'))}{', after the ship' if r['after_ship'] else ''})"
                 + (', pre-release' if r['prerelease'] else ''))
        L += [f"  {(x['name'] or '?')[:44]:<44} {size_str(x['size']):>9}  {x['kind']}" for x in r['assets']]
        if not r['assets']:
            L.append('  no files attached (source code only)')
    if rels and len(rels) > 8:
        L.append(f'... {len(rels) - 8} older releases')
    if F['demo_release_tag']:
        L.append(f"the demo URL points at release {F['demo_release_tag']} (--download --release {F['demo_release_tag']})")

    if F['firmware']:
        FW = F['firmware']
        h('firmware')
        L.append(f"signs: {', '.join(FW['signs'])}")
        L.append('video link: ' + (', '.join(FW['videos'][:3]) if FW['videos'] else 'none (YouTube, Vimeo or .mp4)')
                 + (f"; maybe a video: {FW['maybe_videos'][0]}" if FW['maybe_videos'] else ''))
        L.append('web sim: ' + (', '.join(FW['sims'][:2]) if FW['sims'] else 'no Wokwi link')
                 + (f" (wokwi files in repo: {', '.join(FW['wokwi_files'][:2])})" if FW['wokwi_files'] else ''))

    if F.get('download'):
        D = F['download']
        h(f"download ({D['tag']}, {D['why']})")
        L.append(f"into {D['dir']} (nothing was run or opened)")
        for x in D['files']:
            L.append(f"  {x['name']}  {size_str(x['size'])}" + (f"  {x['unzip']}" if x.get('unzip') else ''))
        L += [f'  skipped {x}' for x in D['skipped']]

    h('ship checklist')
    C = F['checklist']
    L.append('web demo: ' + (C['web_demo'] or 'no')
             + ' | release build: ' + (', '.join(C['release_build'][:3]) if C['release_build'] else 'unknown' if C['releases_unknown'] else 'no')
             + ' | package: ' + (', '.join(C['package']) if C['package'] else 'no'))

    h('look at')
    L += [f'- {w}' for w in F['warnings']] or ['nothing flagged']
    q = F['requests']
    L += ['', f"(requests: {', '.join(f'{k} {v}' for k, v in q['code_hosts'].items()) or 'none'}"
          + (f"; {q['github_left']} GitHub API requests left this hour" if q['github_left'] is not None else '') + ')']
    return '\n'.join(L)


# ---- commands ----

def cmd_endpoints(a):
    rows = [{'command': c, 'method': m, 'path': p, 'what': w} for c, m, p, w in ENDPOINTS]
    if a.json:
        return show(rows)
    print('Read endpoints (each has a command):')
    for r in rows:
        if r['command']:
            print(f"  {r['command']:<17} GET    {r['path']:<42} {r['what']}")
    print("\nWrite endpoints (not in this tool, they change things on Terra):")
    for r in rows:
        if not r['command']:
            print(f"  {r['method']:<6} {r['path']:<46} {r['what']}")


def cmd_me(a, api):
    d = api.get('/auth/me')
    if a.json:
        return show(d)
    u = d.get('user', d)
    print(f"{u.get('username')} ({u.get('displayName')}), roles: {', '.join(u.get('roles') or []) or '-'}"
          + (', admin' if u.get('isAdmin') else ''))


def cmd_counts(a, api):
    d = api.get('/review/counts')
    if a.json:
        return show(d)
    print(f"review {d.get('review')}" + (f", fraud {d['fraud']}" if d.get('fraud') is not None else ''))


def cmd_weeks(a, api):
    w = api.get('/review/weeks')
    w = w if isinstance(w, list) else w.get('weeks', [])
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / 'weeks.json').write_text(json.dumps(w))
    if a.json:
        return show(w)
    for x in w:
        print(f"{x.get('number'):>3}  {x.get('label'):<14} {day(x.get('startDate'))} to {day(x.get('endDate'))}"
              + ('  rent' if x.get('rentRequired') else ''))


def cmd_stats(a, api):
    d = api.get('/review/stats')
    if a.json:
        return show(d)
    wk = d.get('week') or {}
    print(f"{wk.get('key')}: {day(wk.get('startDate'))} to {day(wk.get('endDate'))}")
    for i, r in enumerate(sorted(d.get('reviewers') or [], key=lambda r: -(r.get('total') or 0)), 1):
        c = r.get('counts') or {}
        print(f"{i:>3}. {r.get('username'):<22} {r.get('total'):>4}  ({c.get('approved', 0)} approved, {c.get('changes', 0)} changes)")


def cmd_queue(a, api):
    weeks = get_weeks(api)
    items, total = filtered(api, a, weeks)
    if a.json:
        return show(items)
    if a.ids:
        print('\n'.join(i['id'] for i in items))
        return
    short = {'windows': 'win', 'linux': 'lin', 'macos': 'mac'}
    for i in items:
        flags = ' '.join(f'{n}{k[0]}' for k, n in (('danger', i.get('danger')), ('warn', i.get('warn'))) if n)
        plats = ','.join(short.get(x, x) for x in i.get('platforms') or [])
        claim = 'me' if i.get('claimedByMe') else (i.get('claimedBy') or ('hold:' + i['hold'] if i.get('hold') else ''))
        title = (i.get('title') or '')[:32]
        print(f"{week_short(weeks, i.get('weekNumber')):<4}{hours(i.get('seconds')):>7}  {flags:<6} {plats:<15} "
              f"{claim[:12]:<12} {i.get('decision') or '':<8} {title:<32} {(i.get('username') or '')[:18]:<18} "
              f"{SITE}/admin/review/{i['id']}")
    print(f'{len(items)} shown of {total} in the {a.queue} queue ({a.filter})', file=sys.stderr)


def cmd_ship(a, api):
    sid = resolve(a.ref)
    d, notes, chain = fetch_ship(api, sid)
    if a.json:
        # the shipment response with the notes and chain added, so `jq .shipment.title` works
        return show({**d, 'notes': notes, 'chain': chain})
    print(render(d, notes, chain, get_weeks(api)))


def cmd_chain(a, api):
    d = api.get(f'/review/shipment/{resolve(a.ref)}/chain')
    if a.json:
        return show(d)
    print('\n'.join(render_chain(d, get_weeks(api))) or 'not an update, no chain')


def cmd_notes(a, api):
    if a.project:
        pid = UUID.search(a.ref).group(0) if UUID.search(a.ref) else a.ref
    else:
        pid = api.get(f'/review/shipment/{resolve(a.ref)}')['shipment']['projectId']
    d = api.get(f'/review/project/{pid}/notes')
    if a.json:
        return show(d)
    print('\n'.join(render_notes(d)))


def cmd_macros(a, api):
    d = api.get('/review/macros')
    if a.json:
        return show(d)
    for m in d if isinstance(d, list) else d.get('macros', []):
        print(f"## {m.get('title')} ({', '.join(m.get('verdicts') or [])})\n{m.get('body')}\n")


def cmd_tags(a, api):
    d = api.get('/review/tag-options')
    if a.json:
        return show(d)
    print(', '.join(t.get('name') for t in d.get('techs') or []))


def simple(path, query=None):
    """Commands with no human format yet: print the JSON."""
    def run(a, api):
        p = path.replace('{id}', resolve(a.ref)) if '{id}' in path else path
        show(api.get(p, {k: getattr(a, k) for k in query or []}))
    return run


def cmd_get(a, api):
    u = urllib.parse.urlsplit(a.path)
    if u.netloc and u.netloc != API_HOST:
        die(f'refusing to call {u.netloc}: this only talks to {API_HOST}')
    path = u.path if u.path.startswith('/') else '/' + u.path
    show(api.get(path, dict(urllib.parse.parse_qsl(u.query))))


def cmd_dump(a, api):
    weeks = get_weeks(api)
    if a.refs:
        todo = [{'id': resolve(r), 'title': r} for r in a.refs]
        waiting = 0
    else:
        a.unclaimed = True
        a.new = not a.all
        todo, _ = filtered(api, a, weeks)
        waiting = len(todo)
    print(f'dumping {len(todo)} ships (~{len(todo) * 2.5 * api.gap / 60:.0f} min)', file=sys.stderr)

    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d-%H-%M')
    raw = HERE / 'queue-json' / stamp
    raw.mkdir(parents=True, exist_ok=True)
    dump = Path(a.out) if a.out else HERE / f'terra-queue-{stamp}.md'
    parts = []
    for n, item in enumerate(todo, 1):
        sid = item['id']
        try:
            d, notes, chain = fetch_ship(api, sid)
        except ApiError as e:
            print(f'  {n}/{len(todo)} skipped, {e}', file=sys.stderr)
            continue
        (raw / f'{sid}.json').write_text(json.dumps({'shipment': d, 'notes': notes, 'chain': chain}, default=str))
        parts.append(render(d, notes, chain, weeks, waiting - n if not a.refs else None))
        # write as we go so a stop halfway still leaves a usable dump
        dump.write_text('\n\n---\n\n'.join(parts) + '\n')
        print(f'  {n}/{len(todo)} {d["shipment"].get("title") or item.get("title")}', file=sys.stderr)
    print(f'wrote {dump} ({len(parts)} ships), raw json in {raw}', file=sys.stderr)


def cmd_prep(a, api):
    sid = resolve(a.ref)
    if a.offline:
        dumps = sorted(HERE.glob(f'queue-json/*/{sid}.json'))
        if not dumps:
            die(f'no queue-json/*/{sid}.json dump of this ship; run it without --offline')
        raw = json.loads(dumps[-1].read_text())
        d, notes, source = raw['shipment'], raw.get('notes'), f'offline: {dumps[-1].relative_to(HERE)}'
    else:
        d, notes, _ = fetch_ship(api, sid)
        source = 'live from Terra'
    F = prep(d, Path(a.dir).expanduser().resolve(), a, source, notes)
    if a.json:
        return show(F)
    print(prep_text(F))


def main(argv=None):
    top = argparse.ArgumentParser(prog='terra-api.py', description=__doc__.split('\n\n')[0],
                                  epilog='Run `terra-api.py <command> -h` for a command\'s options.')
    sub = top.add_subparsers(dest='cmd', required=True, metavar='command')
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--json', action='store_true', help='print the raw JSON')

    def cmd(name, fn, help, ref=False, json=True):
        p = sub.add_parser(name, help=help, description=help, parents=[common] if json else [])
        if ref:
            p.add_argument('ref', help='ship number, review URL or shipment id')
        p.set_defaults(fn=fn)
        return p

    cmd('endpoints', None, 'list every endpoint, read and write').set_defaults(fn=None, offline=True)
    cmd('me', cmd_me, 'your account and roles')
    cmd('counts', cmd_counts, 'queue sizes')
    cmd('weeks', cmd_weeks, 'week numbers, labels and dates (refreshes the cache)')
    cmd('stats', cmd_stats, "this week's reviewer leaderboard")
    q = cmd('queue', cmd_queue, 'the queue as a table, with filters')
    add_filters(q)
    q.add_argument('--unclaimed', action='store_true', help='leave out claimed and held ships')
    q.add_argument('--ids', action='store_true', help='print only the shipment ids')
    cmd('ship', cmd_ship, 'one ship as markdown, like a dump entry (with notes and the update chain)', ref=True)
    cmd('chain', cmd_chain, 'every version of an update project', ref=True)
    cmd('parent', simple('/review/shipment/{id}/parent'), 'the original project of an update', ref=True)
    cmd('unified', simple('/review/shipment/{id}/unified-projects'), 'matching projects in other YSWS programs', ref=True)
    n = cmd('notes', cmd_notes, 'staff notes on the project of a ship')
    n.add_argument('ref', help='ship number, review URL or shipment id (a project id with --project)')
    n.add_argument('--project', action='store_true', help='ref is a project id')
    cmd('macros', cmd_macros, 'canned decision notes')
    cmd('tags', cmd_tags, 'tag pick list')
    cmd('deflation-checks', simple('/review/deflation-checks'), 'admin: approvals waiting on a deflation check')
    cmd('triage', simple('/review/fraud/triage', ['group']), 'fraud: triage list').add_argument('--group')
    cmd('search', simple('/admin/search', ['q']), 'admin: search users, projects and ships').add_argument('q')
    g = cmd('get', cmd_get, 'any read endpoint, raw JSON (e.g. "/review/queue?queue=fraud")')
    g.add_argument('path')
    d = cmd('dump', cmd_dump, 'write unclaimed ships to terra-queue-<stamp>.md plus raw JSON in queue-json/',
            json=False)
    d.add_argument('refs', nargs='*', help='only these ships (ship numbers, URLs or ids), claimed or not')
    add_filters(d)
    d.add_argument('--all', action='store_true', help='include ships you already have pages for')
    d.add_argument('-o', '--out', help='output file instead of terra-queue-<stamp>.md')
    p = cmd('prep', cmd_prep, "clone or fetch a ship's repo and print the mechanical review facts (replaces prep.sh)",
            ref=True)
    p.add_argument('--dir', default=str(HERE), help='where clones, -ship worktrees and -release folders go (default: this folder)')
    p.add_argument('--offline', action='store_true', help='read the ship from the newest queue-json/*/<id>.json dump, no Terra request')
    p.add_argument('--download', action='store_true',
                   help='download the assets of the first release after the ship into <repo>-release/ (never run)')
    p.add_argument('--release', metavar='TAG', help='with --download: this release instead')
    p.add_argument('--max-size', type=max_size, default=500 * MB, metavar='SIZE',
                   help='with --download: total size cap, e.g. 200M or 1G (default 500M)')
    p.add_argument('--lfs', action='store_true', help='also download Git LFS files (off by default)')

    a = top.parse_args(argv)
    if a.cmd == 'endpoints':
        return cmd_endpoints(a)
    try:
        a.fn(a, None if getattr(a, 'offline', False) else connect())
    except ApiError as e:
        raise SystemExit(str(e))
    except BrokenPipeError:  # piped into head: drop the rest quietly
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


if __name__ == '__main__':
    main()
