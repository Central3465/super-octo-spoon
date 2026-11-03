#!/usr/bin/env python3
"""Submit a Terra review decision from a draft block in a ship page.

Dry run by default: it reads the draft blocks, checks each the way the review page does, and prints
every request it would send, in order, with the exact JSON. With --send it goes through the file one
ship at a time: it shows what the player will read and waits for you to type "send" (or s to skip,
q to quit) before sending that one ship. With --queue the approved ones go into one
shared send queue (send-queue/) instead, from any number of decide runs (request changes, approves,
moves). `run --every MIN` sends that queue: send a, claim b, wait, send b, claim c, ... (Terra allows one
claim at a time; --hold 0 claims each one only as it's sent instead), sends at most one every MIN minutes (oldest approval first), and re-checks each ship
right before it goes out. `queue` lists it, `queue --remove ID` takes one out. Ships already sent in an earlier run (writes.jsonl) are
skipped. It needs a terminal. See PROPOSAL.md next to this file.

  ./terra-write.py decide ~/terra-desk/ships/12-some-ship.md --offline --no-video
  ./terra-write.py decide ~/terra-desk/ships/request-changes.md --only 105 --offline
  ./terra-write.py decide 93-coden.md --api http://127.0.0.1:18766 --send --video run.mp4
  ./terra-write.py note 93 "text" [--fraud]          add a staff note
  ./terra-write.py release 93                        drop a claim (yours, e.g. left by the iframe dumper)

--offline reads the ship from the newest queue-json/ dump instead of the API (no token, no network).
Every write that was attempted is appended to writes.jsonl (method, path, body, status, answer;
never the token).
"""
import argparse
import json
import os
import re
import runpy
import select
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get('TERRA_DATA_DIR') or HERE)      # the send queue and writes.jsonl (terra-desk: its workspace)
ROOT = Path(os.environ.get('TERRA_TOOLS_DIR') or Path(__file__).resolve().parent)
T = runpy.run_path(str(ROOT / 'terra-api.py'), run_name='terra_api')
Api, connect, ApiError, resolve = T['Api'], T['connect'], T['ApiError'], T['resolve']
UUID, REVIEW_ID, SITE, REAL_API = T['UUID'], T['REVIEW_ID'], T['SITE'], T['API']
LOCAL_API = re.compile(r'http://127\.0\.0\.1:\d+')
LOG = Path(os.environ.get('TERRA_WRITE_LOG') or DATA / 'writes.jsonl')

# the only non-GET requests this can make. Anything else is refused before a socket opens.
WRITES = {
    'claim': ('POST', '/review/shipment/{id}/claim'),
    'release': ('DELETE', '/review/shipment/{id}/claim'),
    'heartbeat': ('POST', '/review/shipment/{id}/claim/heartbeat'),
    'dismiss': ('POST', '/review/shipment/{id}/flags/{kind}/dismiss'),
    'video': ('POST', '/review/shipment/{id}/video'),
    'approve': ('POST', '/review/shipment/{id}/approve'),
    'changes': ('POST', '/review/shipment/{id}/changes'),
    'reject': ('POST', '/review/shipment/{id}/reject'),
    'move': ('POST', '/review/shipment/{id}/send'),
    'note': ('POST', '/review/project/{id}/notes'),
}
WRITE_RE = {k: (m, re.compile(p.replace('{id}', UUID.pattern).replace('{kind}', r'[a-z0-9_%-]+')))
            for k, (m, p) in WRITES.items()}

# limits from the review page (Decision-*.js): features 30..1000, justification 80+, update 40+,
# multiplier 0.8..1.5, danger acknowledgement 10+, video 100 MiB, mp4/webm/mov
MIN_FEATURES, MAX_FEATURES, MIN_JUSTIFICATION, MIN_UPDATE = 30, 1000, 80, 40
MIN_MULT, MAX_MULT, MIN_ACK = 0.8, 1.5, 10
MAX_VIDEO = 104857600
VIDEO_TYPES = {'.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime'}


class Refuse(Exception):
    """The draft or the ship isn't in a state to send. Nothing was sent."""


class Stop(Exception):
    """A write failed. Stop everything, never retry."""


# ---- draft parsing: a port of parseDrafts() in terra-review-copy.user.js ----

def norm(s):
    s = re.sub(r'\(.*?\)', '', s.lower())
    s = re.sub(r'[^a-z ]', '', s)
    return re.sub(r'\s+', ' ', s).strip()


FIELD_NAMES = {
    'features': ['specific technical features', 'technical features'],
    'airtableDeflation': ['airtable deflation'],
    'cropDeflation': ['crop deflation'],
    'deflationReason': ['deflation reason'],
    'multiplier': ['multiplier'],
    'justification': ['airtable justification'],
    'update': ['update justification'],
    'note': ['note to player', 'what to change', 'why its rejected', 'reject reason', 'rejection reason'],
    'internal': ['internal note'],
    'ack': ['danger flag acknowledgement', 'danger flag acknowledgment', 'acknowledgement', 'acknowledgment'],
    'verdict': ['verdict', 'your call'],
    # not in the userscript (paste drafts can't fill Move); the userscript ignores this heading
    'moveNote': ['move note'],
}


def field_for(name):
    n = norm(name)
    return next((k for k, names in FIELD_NAMES.items() if n in names), None)


def parse_drafts(text):
    """Same state machine as the userscript, plus what the CLI needs to be strict:
    the raw verdict text, the note heading used, and labels that matched without a ### heading."""
    fields, acks, loose, unknown = {}, [], [], []
    verdict = verdict_raw = note_name = None
    current, lines = None, []
    fenced = had_fence = wrapper = False

    def save(key, value):
        nonlocal verdict, verdict_raw
        value = re.sub(r'^\n+|\s+$', '', value)
        if not value or re.match(r'leave (it )?blank\b', value, re.I):
            return
        if key == 'ack':
            acks.append(value)
        elif key == 'verdict':
            verdict_raw = value
            v = value.lower()
            verdict = 'changes' if 'change' in v else 'reject' if 'reject' in v else 'approve' if 'approve' in v else verdict
        else:
            fields[key] = value

    def close():
        nonlocal current, lines, had_fence
        if current:
            save(current, '\n'.join(lines))
        current, lines, had_fence = None, [], False

    for n, raw in enumerate(text.replace('\r', '').split('\n'), 1):
        if re.match(r'^\s*(```|~~~)', raw):
            if wrapper or (not fenced and not current):
                if wrapper:
                    close()
                wrapper = not wrapper
                continue
            fenced = not fenced
            if fenced and current:
                had_fence = True
            elif not fenced and current and had_fence:
                close()
            continue
        if fenced:
            if current:
                lines.append(raw)
            continue
        head = re.sub(r'^\s*#+\s*', '', raw)
        bold = re.match(r'^\*\*(.+?)\*\*\s*(.*)$', head)
        name, rest = (bold.group(1), bold.group(2)) if bold else (head, '')
        if ':' in name:
            i = name.index(':')
            name, rest = name[:i], f'{name[i + 1:]} {rest}'
        rest = re.sub(r'^\s*:?\s*', '', rest).strip()
        key = field_for(name)
        if key:
            if not re.match(r'^###\s', raw):
                loose.append((n, raw))
            close()
            if key == 'note':
                note_name = norm(name)
                if note_name == 'what to change':
                    verdict = verdict or 'changes'
                elif note_name != 'note to player':
                    verdict = verdict or 'reject'
            if rest:
                save(key, rest)
            else:
                current = key
        elif re.match(r'^\s*#', raw):
            unknown.append((n, raw))
            close()
        elif current and not had_fence:
            lines.append(raw)
    close()
    return {'fields': fields, 'acks': acks, 'verdict': verdict, 'verdict_raw': verdict_raw,
            'note_name': note_name, 'loose': loose, 'unknown': unknown}


# ## headings that are sections, not ships (bulk files and request-changes files)
SECTIONS = {'key facts', 'test steps', 'gotchas', 'drafts', 'ready', 'check first', 'not drafted (your call)',
            'not drafted', 'request changes'}


def draft_blocks(path):
    """Plain code blocks holding ### field headings, each with the review id and heading above it."""
    out, sid, title, fence, buf, start, context = [], None, None, None, [], 0, []
    one_ship = False
    lines = path.read_text(encoding='utf-8', errors='ignore').replace('\r', '').split('\n')
    for n, line in enumerate(lines, 1):
        m = re.match(r'^\s*(```|~~~)(.*)$', line)
        if fence is None:
            if m:
                fence, buf, start = m.group(1), [], n
                continue
            # a ship starts at the file's # title or a `## 105 Title` heading (request-changes.md);
            # other ## headings (Key facts, Drafts) stay with the ship above them
            # in a bulk file (one ship, `# NN Title` on top) every ## is a section of that ship; in a
            # request-changes file each `## Title` is a ship unless it's a known section name
            if re.match(r'^# \d+ ', line):
                one_ship = True
            if re.match(r'^# ', line) or re.match(r'^## \d+ ', line) or (
                    not one_ship and re.match(r'^## ', line) and line[3:].strip().lower() not in SECTIONS):
                title, sid, context = line.lstrip('#').strip(), None, []
                continue
            ids = REVIEW_ID.findall(line)
            if ids:
                sid = ids[0]
            elif line.strip() and title:
                context.append(line.strip())
        elif m and m.group(1) == fence and not m.group(2).strip():
            block = '\n'.join(buf)
            if any(re.match(r'^###\s', b) and field_for(b.lstrip('#')) for b in buf):
                num = re.match(r'(\d+)\b', title or '') or re.match(r'(\d+)-', path.name)
                out.append({'sid': sid, 'title': title, 'num': int(num.group(1)) if num else None,
                            'text': block, 'line': start, 'context': list(context)})
            elif buf and title and not re.search(r'^## Drafts', '\n'.join(lines[:n]), re.M):
                context.append('check: ' + ' / '.join(x.strip() for x in buf if x.strip()))
            elif buf and title and re.search(r'^## Drafts', '\n'.join(lines[:n]), re.M):
                out.append({'sid': sid, 'title': title, 'num': None, 'text': block, 'line': start, 'bare': True})
            fence = None
        else:
            buf.append(line)
    # report a ship with no ### block once (a bare move note, or the older bold-label format), not per block
    real = {b['sid'] for b in out if not b.get('bare')}
    bare = {}
    for b in out:
        if b.get('bare') and b['sid'] not in real:
            bare[b['sid']] = b
    return [b for b in out if not b.get('bare')] + list(bare.values())


# ---- the ship ----

def newest_dump(sid):
    files = sorted((ROOT / 'queue-json').glob(f'*/{sid}.json'))
    if not files:
        return None, None
    return json.loads(files[-1].read_text(encoding='utf-8'))['shipment'], files[-1].parent.name


def hours(s):
    return f'{(s or 0) / 3600:.2f}h'


def captured_seconds(d):
    ev = ((d.get('evidence') or {}).get('data')) or {}
    if ev.get('devloggedSeconds') is not None:
        return round(ev['devloggedSeconds'])
    return round(sum(((r.get('time') or {}).get('creditedSeconds') or 0) for r in ev.get('rows') or []))


def crop_hours(captured, crop_seconds):
    # z() in the page bundle: whole hours of crops left after the crop deflation
    return max(0, (captured - max(0, min(captured, crop_seconds)) + 5) // 3600)


def danger_to_ack(d):
    # undismissed danger flags; double_dip has no acknowledge box on the page
    return [f for f in d.get('flags') or [] if f.get('severity') == 'danger' and not f.get('dismissedAt')
            and f.get('kind') != 'double_dip']


def ship_problems(d, verdict, move_to=None):
    """Why the page wouldn't let you decide this ship right now (J() and Q() in ReviewPage-*.js)."""
    s, v = d['shipment'], d.get('viewer') or {}
    p = []
    if not v.get('canReview'):
        p.append("your account can't review")
    if v.get('isOwner') and not v.get('isAdmin'):
        p.append("it's your own ship")
    if s.get('stage') != 'review':
        p.append(f"stage is {s.get('stage')!r}, not 'review'")
    if s.get('reviewHold'):
        p.append(f"it's on hold ({s['reviewHold']})")
    ev = d.get('evidence') or {}
    if not ev.get('data') or ev.get('version') != s.get('evidenceVersion'):
        p.append('evidence is still being built')
    claim = (d.get('claims') or {}).get('review')
    if claim and not claim.get('mine') and not claim.get('stale'):
        p.append(f"{claim.get('username') or 'someone'} has it claimed")
    if verdict == 'changes' and s.get('changesRounds', 0) >= 3 and not v.get('isAdmin'):
        p.append('it already had 3 rounds of changes, so approve or reject it')
    if verdict == 'move':
        if move_to == 'fraud' and not s.get('fraudPending'):
            p.append("fraud has already given a verdict (shipment.fraudPending is false), so it can only move to an admin")
        if move_to == 'admin' and v.get('isAdmin'):
            p.append("admins can't send a ship to an admin")
    return p


# ---- turning a draft into requests ----

PLACEHOLDER = re.compile(r'\bTODO\b|\bTBD\b|\bFIXME\b|\?\?\?|\(check\b|pick one|\[[A-Z][a-z]+( [a-z]+)*\]')


def number(fields, key, label, errs):
    if key not in fields:
        return None
    try:
        x = float(fields[key])
    except ValueError:
        errs.append(f'{label} is {fields[key]!r}, not a number')
        return None
    if x < 0:
        errs.append(f'{label} is negative')
    return x


def build(draft, d, a):
    """-> (verdict, steps, summary lines, warnings). Raises Refuse with every problem found."""
    f, errs, warns = draft['fields'], [], []
    s = d['shipment']
    sid = s['id']
    raw = (draft['verdict_raw'] or '').strip().lower()
    move_to = None
    if raw in ('approve', 'request changes', 'changes', 'reject'):
        verdict = 'changes' if 'change' in raw else raw
    elif re.fullmatch(r'move( review)? to (fraud|an admin|admin)', raw):
        verdict, move_to = 'move', 'fraud' if raw.endswith('fraud') else 'admin'
    else:
        raise Refuse(f"### Verdict must be exactly approve, request changes, reject, move to fraud or move to admin "
                     f"(got {draft['verdict_raw']!r})")
    if draft['verdict'] and verdict != 'move' and draft['verdict'] != verdict:
        errs.append(f"the note heading ({draft['note_name']}) means {draft['verdict']}, but ### Verdict says {verdict}")
    want_note = {'approve': 'note to player', 'changes': 'what to change'}.get(verdict)
    if want_note and draft['note_name'] and draft['note_name'] != want_note:
        errs.append(f"{verdict} takes ### {want_note.capitalize()}, not ### {draft['note_name']}")
    for n, line in draft['loose']:
        errs.append(f'line {n} of the block ({line.strip()[:40]!r}) reads as a field label but is not a ### heading')
    for n, line in draft['unknown']:
        errs.append(f'line {n} of the block ({line.strip()[:40]!r}) is a heading the form has no field for '
                    f'(the text under it would be dropped)')
    for k, v in f.items():
        if PLACEHOLDER.search(v):
            errs.append(f'{k} still has a placeholder or TODO: {PLACEHOLDER.search(v).group(0)!r}')
    errs += ship_problems(d, verdict, move_to)

    claim = [('claim', 'POST', f'/review/shipment/{sid}/claim', {'queue': 'review', 'takeover': False})]
    note = f.get('note', '')
    handle = os.environ.get('TERRA_HANDLE')
    if handle and verdict != 'move' and note and not note.rstrip().endswith(f'if you have any questions DM {handle}'):
        warns.append(f"the note doesn't end with 'if you have any questions DM {handle}'")
    summary = []

    if verdict == 'move':
        allowed = {'features', 'airtableDeflation', 'cropDeflation', 'deflationReason', 'multiplier', 'justification', 'update', 'note', 'internal'}
        if allowed & set(f):
            errs.append(f"a move block only takes ### Verdict and ### Move note (also has {', '.join(sorted(allowed & set(f)))})")
        if not f.get('moveNote', '').strip():
            errs.append(f"### Move note is empty (explain why it needs {'fraud review' if move_to == 'fraud' else 'an admin'})")
        steps = claim + [('move', 'POST', f'/review/shipment/{sid}/send', {'hold': move_to, 'note': f.get('moveNote', '')})]
        summary.append(f'move to {move_to}')

    elif verdict in ('changes', 'reject'):
        extra = {'features', 'airtableDeflation', 'cropDeflation', 'deflationReason', 'multiplier', 'justification', 'update', 'moveNote'} & set(f)
        if extra:
            errs.append(f"{verdict} only sends the note and internal note, but the block also has {', '.join(sorted(extra))}")
        if not note.strip():
            errs.append('### What to change is empty' if verdict == 'changes' else "write a note saying why it's rejected")
        if draft['acks']:
            warns.append('acknowledgements are ignored for changes and reject')
        body = {'evidenceVersion': s['evidenceVersion'], 'noteToMaker': note, 'videoUrl': None,
                'internalNote': f.get('internal', '')}
        steps = claim + [(verdict, 'POST', f'/review/shipment/{sid}/{verdict}', body)]
        summary.append('request changes' if verdict == 'changes' else 'REJECT (for good, the player cannot reship)')

    else:  # approve
        if 'moveNote' in f or 'internal' in f:
            errs.append('approve has no internal note or move note field')
        captured = captured_seconds(d)
        fs = d.get('fraudSuggestion')
        # the form starts from fraud's suggestion (both deflations and the reason); a draft field replaces it.
        # without a suggestion both deflations must be in the draft, so the page's "crop follows Airtable" never applies
        air = fs['seconds'] / 3600 if fs else 0.0
        crop = fs['seconds'] / 3600 if fs else 0.0
        reason = f"Fraud review: {fs['reason']}" if fs and fs.get('reason') else ''
        a_air = number(f, 'airtableDeflation', 'Airtable deflation', errs)
        a_crop = number(f, 'cropDeflation', 'Crop deflation', errs)
        if not fs and ('airtableDeflation' not in f or 'cropDeflation' not in f):
            errs.append('write both ### Airtable deflation (hours) and ### Crop deflation (hours), 0 if none '
                        '(only a fraud-suggested deflation may be left out)')
        air = air if a_air is None else a_air
        crop = crop if a_crop is None else a_crop
        if 'deflationReason' in f:
            reason = f['deflationReason']
        if (air > 0 or crop > 0) and not reason.strip():
            errs.append('a deflation above 0 needs ### Deflation reason')
        mult = number(f, 'multiplier', 'Multiplier', errs)
        if mult is None and 'multiplier' not in f:
            errs.append('### Multiplier is missing')
        elif mult is not None and not MIN_MULT <= mult <= MAX_MULT:
            errs.append(f'multiplier must be {MIN_MULT} to {MAX_MULT} (got {mult})')
        feat, just, upd = f.get('features', ''), f.get('justification', ''), f.get('update', '')
        if len(feat.strip()) < MIN_FEATURES:
            errs.append(f'### Specific technical features is {len(feat.strip())}/{MIN_FEATURES} characters')
        if len(feat.strip()) > MAX_FEATURES:
            errs.append(f'### Specific technical features is over {MAX_FEATURES} characters')
        if len(just.strip()) < MIN_JUSTIFICATION:
            errs.append(f'### Airtable justification is {len(just.strip())}/{MIN_JUSTIFICATION} characters')
        url_matches = (((d.get('evidence') or {}).get('data')) or {}).get('unifiedUrlMatches') or []
        if url_matches and len(upd.strip()) < MIN_UPDATE:
            errs.append(f'same repo was submitted to another program, so ### Update justification needs {MIN_UPDATE}+ characters')
        if not note.strip():
            errs.append('### Note to player is empty')

        flags = danger_to_ack(d)
        if len(draft['acks']) != len(flags):
            errs.append(f"{len(flags)} danger flag(s) to acknowledge but the block has {len(draft['acks'])} acknowledgement(s)")
        acks = list(zip(flags, draft['acks']))
        for fl, text in acks:
            if len(text.strip()) < MIN_ACK:
                errs.append(f"acknowledgement for {fl.get('kind')} is under {MIN_ACK} characters")

        video_step = []
        if a.video:
            vp = Path(a.video).expanduser()
            mime = VIDEO_TYPES.get(vp.suffix.lower())
            if not vp.is_file():
                errs.append(f'no video at {vp}')
            elif not mime:
                errs.append('the video has to be .mp4, .webm or .mov')
            elif vp.stat().st_size > MAX_VIDEO:
                errs.append(f'the video is {vp.stat().st_size / 1048576:.0f}MB, over the 100MB limit: '
                            f'shrink it first, e.g. ffmpeg -i in.mp4 -vf scale=-2:720 -crf 30 out.mp4')
            else:
                video_step = [('video', 'POST', f'/review/shipment/{sid}/video',
                               {'outcome': 'small', 'detail': 'terra-write.py, not compressed', 'compressMs': '0',
                                'sourceBytes': str(vp.stat().st_size), 'sourceType': mime, 'file': str(vp)})]
        elif not (d.get('viewer') or {}).get('videoOptional'):
            errs.append('approve needs a video (--video FILE); your account has to attach one')
        elif not a.no_video:
            errs.append('no --video given: approving without one works on your account (videoOptional), '
                        'but the review guidelines ask for one, so pass --no-video to mean it')

        air_s, crop_s = round(air * 3600), round(crop * 3600)
        body = {'evidenceVersion': s['evidenceVersion'], 'deflationSeconds': air_s, 'cropDeflationSeconds': crop_s,
                'deflationReason': reason, 'payoutMultiplier': mult, 'noteToMaker': note,
                'videoUrl': '<url from the video upload>' if video_step else None,
                'builtWhat': feat, 'projectReview': just, 'updateJustification': upd}
        approved = captured - max(0, min(captured, air_s))
        steps = (claim + [('dismiss', 'POST', f"/review/shipment/{sid}/flags/{urllib.parse.quote(fl['kind'], safe='')}/dismiss",
                           {'reason': text}) for fl, text in acks]
                 + video_step + [('approve', 'POST', f'/review/shipment/{sid}/approve', body)])
        summary.append(f"APPROVE {hours(approved)} for Airtable and {crop_hours(captured, crop_s)}h of crops at x{(mult or 0):.2f}")
        summary.append(f"credited {hours(captured)}, Airtable deflation {hours(air_s)}, crop deflation {hours(crop_s)}"
                       + (f", reason: {reason}" if reason else '') + (' (from fraud)' if fs and 'deflationReason' not in f else ''))
        for fl, text in acks:
            summary.append(f"acknowledge {fl.get('kind')} ({fl.get('title')}): {text}")
        summary.append(f'video: {a.video}' if video_step else 'video: none')

    if errs:
        raise Refuse('\n'.join(f'  - {e}' for e in errs))
    return verdict, steps, summary, warns


# ---- sending ----

class Writer:
    def __init__(self, base, token, live):
        self.base, self.token, self.live = base, token, live

    def send(self, kind, method, path, body, query=None):
        m, pattern = WRITE_RE[kind]
        if method != m or not pattern.fullmatch(path):
            raise SystemExit(f'refusing {method} {path}: not on the write list')
        if not self.live:
            raise SystemExit('internal error: send() called in a dry run')
        url = f'{self.base}{path}' + (f'?{urllib.parse.urlencode(query)}' if query else '')
        headers = {'Authorization': f'Bearer {self.token}'}
        if kind == 'video':
            data, ctype = multipart(body)
            headers['Content-Type'] = ctype
        else:
            data = json.dumps(body).encode() if body is not None else None
            headers['Content-Type'] = 'application/json'
        entry = {'at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'base': self.base,
                 'method': method, 'path': path, 'query': query, 'body': body}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        # exactly one attempt. A decision that times out may still have landed, so never resend it.
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                answer = json.loads(r.read() or b'null')
                log({**entry, 'status': r.status, 'answer': answer})
                return answer
        except urllib.error.HTTPError as e:
            try:
                answer = json.loads(e.read() or b'null')
            except Exception:
                answer = None
            log({**entry, 'status': e.code, 'answer': answer})
            msg = (answer or {}).get('error') if isinstance(answer, dict) else None
            if e.code == 429:
                raise Stop(f'429 rate limited on {method} {path}. Not retrying and not sending anything else.')
            if e.code == 401:
                raise Stop('401: the token is wrong or expired')
            holder = ((answer or {}).get('holder') or {}).get('username') if isinstance(answer, dict) else None
            raise Stop(f"{e.code} on {method} {path}: {msg or 'request failed'}" + (f' (held by {holder})' if holder else ''))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log({**entry, 'status': None, 'answer': f'no answer: {e}'})
            raise Stop(f'no answer from {method} {path} ({e}). It may or may not have gone through: '
                       f'check with terra-api.py ship before doing anything else.')


def multipart(fields):
    # same parts the page's XHR sends: outcome, detail, compressMs, sourceBytes, sourceType, then file
    b = uuid.uuid4().hex
    out = []
    for k in ('outcome', 'detail', 'compressMs', 'sourceBytes', 'sourceType'):
        out.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{fields[k]}\r\n'.encode())
    p = Path(fields['file'])
    out.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{p.name}"\r\n'
               f"Content-Type: {fields['sourceType']}\r\n\r\n".encode() + p.read_bytes() + b'\r\n')
    out.append(f'--{b}--\r\n'.encode())
    return b''.join(out), f'multipart/form-data; boundary={b}'


def log(entry):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(entry) + '\n')


def show_steps(steps, base):
    for i, (kind, method, path, body) in enumerate(steps, 1):
        print(f'  {i}. {method} {base}{path}')
        if kind == 'video':
            print(f"     multipart: file={body['file']} ({int(body['sourceBytes']) / 1048576:.1f}MB), "
                  + ', '.join(f'{k}={body[k]!r}' for k in ('outcome', 'detail', 'compressMs', 'sourceBytes', 'sourceType')))
        elif body is not None:
            print('     ' + json.dumps(body, indent=2, ensure_ascii=False).replace('\n', '\n     '))


class Skip(Exception):
    """You said no to this ship. Nothing was sent; go on to the next one."""


class Quit(Exception):
    """You stopped the run."""


def approve_prompt(verdict):
    """One ship, one answer. Only the full word sends."""
    if not sys.stdin.isatty():
        raise SystemExit('refusing to send: --send needs you at a terminal to approve each ship')
    while True:
        try:
            got = input(f'\n  send this {verdict}?  [send] it / [s]kip / [q]uit: ').strip().lower()
        except EOFError:
            raise Quit()
        if got == 'send':
            return
        if got in ('s', 'skip', 'n', 'no'):
            raise Skip()
        if got in ('q', 'quit', 'exit'):
            raise Quit()
        print('  type the whole word "send" to send it, "s" to skip, "q" to quit')


def already_sent(sid, base):
    """A decision for this ship that Terra accepted (2xx) in an earlier run, from writes.jsonl."""
    if not LOG.exists():
        return None
    pat = re.compile(rf'^/review/shipment/{sid}/(approve|changes|reject|send)$')
    for line in LOG.read_text(encoding='utf-8').splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get('base') == base and pat.match(e.get('path') or '') and 200 <= (e.get('status') or 0) < 300:
            return e.get('at')
    return None


def show_preview(b, verdict, steps):
    """What the player will read, as plain text, plus the notes written above the block."""
    for c in b.get('context') or []:
        print(f'  note     {c}')
    body = steps[-1][3] if steps and isinstance(steps[-1][3], dict) else {}
    for key, label in (('noteToMaker', 'to the player'), ('note', 'move note'), ('internalNote', 'internal note')):
        text = (body.get(key) or '').strip()
        if text:
            print(f'  --- {label} ---')
            for para in text.split('\n'):
                print('  ' + para)


def confirm(word):
    if not sys.stdin.isatty():
        raise SystemExit('refusing to send: --send needs you at a terminal to type the confirmation')
    got = input(f'type "{word}" to send, anything else stops: ').strip().lower()
    if got != word:
        raise SystemExit('stopped, nothing sent')


# ---- commands ----

def setup(a, from_desk=False):
    base = a.api or REAL_API
    if base != REAL_API and not LOCAL_API.fullmatch(base):
        raise SystemExit(f'--api must be {REAL_API} or http://127.0.0.1:<port>')
    if a.send and not sys.stdin.isatty() and not from_desk:
        raise SystemExit('refusing to send: --send needs you at a terminal to approve each ship')
    if a.offline:
        if a.send:
            raise SystemExit('--offline is for dry runs; sending needs the live ship')
        return base, None, None
    api = connect()
    Api.get.__globals__['API'] = base  # terra-api.py's GETs go to the same place as the writes
    return base, api, Writer(base, api.token, a.send)


def fetch(api, sid):
    if api is None:
        d, stamp = newest_dump(sid)
        if not d:
            raise Refuse(f'--offline but no queue-json dump has {sid}')
        return d
    return api.get(f'/review/shipment/{sid}')


# ---- the send queue (one shared queue; `run` sends it) ----

QUEUE = Path(os.environ.get('TERRA_WRITE_QUEUE') or DATA / 'send-queue')


def queued_file(sid):
    return next(iter(QUEUE.glob(f'*-{sid}.json')), None)


def enqueue(job, priority=False):
    """priority: goes ahead of everything that isn't priority (file names starting 0- sort before 2026...)."""
    QUEUE.mkdir(exist_ok=True)
    stamp = ('0-' if priority else '') + datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    data = {'sid': job['sid'], 'title': job['b']['title'], 'verdict': job['verdict'], 'steps': job['steps'],
            'd': job['d'], 'approvedAt': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    tmp = QUEUE / f'.{stamp}.tmp'
    tmp.write_text(json.dumps(data), encoding='utf-8')
    tmp.rename(QUEUE / f'{stamp}-{job["sid"]}.json')  # atomic, so the runner never reads half a file
    n = len(list(QUEUE.glob('*.json')))
    print(f'  queued ({n} in the send queue{"" if runner_alive() else ", no runner going yet"})')
    return 'queued'


def queue_files():
    return sorted(QUEUE.glob('*.json'))


def pid_alive(pid):
    """Is that process still running? (On Windows os.kill(pid, 0) would send it Ctrl+C, so ask the kernel instead.)"""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def runner_alive():
    f = QUEUE / 'runner.pid'
    try:
        return pid_alive(int(f.read_text(encoding='utf-8').strip()))
    except (OSError, ValueError):
        return False


def cmd_queue(a):
    files = queue_files()
    if a.remove:
        f = next((x for x in files if a.remove in x.name), None)
        if not f:
            raise SystemExit(f'nothing in the queue matches {a.remove}')
        f.unlink()
        print(f'removed {f.name}. If a runner holds its claim it releases it on its next pass.')
        return
    print(f'{len(files)} in the send queue' + (' (runner going)' if runner_alive() else ' (no runner)'))
    for f in files:
        j = json.loads(f.read_text(encoding='utf-8'))
        print(f"  {'!' if f.name.startswith('0-') else ' '} {j['approvedAt'][:16]}  {j['verdict']:<8} {j['title']}  {j['sid'][:8]}")
    for name in ('failed', 'sent'):
        n = len(list((QUEUE / name).glob('*.json'))) if (QUEUE / name).exists() else 0
        if n:
            print(f'{n} in {QUEUE / name}')


def cmd_every(a):
    """Change a running runner's gap (or pause/resume it) from another terminal."""
    if not runner_alive():
        raise SystemExit('no runner is going')
    v = a.value.strip().lower()
    if v not in ('pause', 'resume', 'p', 'stop'):
        try:
            if float(v) <= 0:
                raise ValueError
        except ValueError:
            raise SystemExit('give minutes above 0, or pause / resume / stop')
    (QUEUE / 'every').write_text(v, encoding='utf-8')
    print(f'sent {v!r} to the runner; it picks it up within a second')


def cmd_run(a):
    """Send the queue: claim the next few ships and keep those claims alive while they wait,
    then send at most one every --every minutes, oldest approval first."""
    if a.every <= 0:
        raise SystemExit('--every needs a number of minutes above 0')
    if a.hold not in (0, 1):
        raise SystemExit('--hold is 0 or 1: Terra only lets you hold one claim at a time')
    if not sys.stdin.isatty() and not a.from_desk:
        raise SystemExit('refusing: run it in a terminal you keep open (or start it from terra-desk)')
    interactive = sys.stdin.isatty() and os.name != 'nt'      # select() on stdin only works on Unix terminals
    QUEUE.mkdir(parents=True, exist_ok=True)
    for name in ('sent', 'failed'):
        (QUEUE / name).mkdir(exist_ok=True)
    if runner_alive():
        raise SystemExit('a runner is already going (send-queue/runner.pid). One at a time.')
    (QUEUE / 'runner.pid').write_text(str(os.getpid()), encoding='utf-8')
    a.send, a.offline = True, False
    base, api, writer = setup(a, from_desk=a.from_desk)
    held = {}            # sid -> time we claimed it
    beat_fails = {}      # sid -> heartbeats in a row that failed
    paused = False
    last_send = None
    last_beat = 0.0

    def say(msg):
        print(f'\r{" " * 70}\r{time.strftime("%H:%M:%S")}  {msg}', flush=True)

    def release(sid, why=''):
        try:
            writer.send('release', 'DELETE', f'/review/shipment/{sid}/claim', None, {'queue': 'review'})
            say(f'released {sid[:8]}{why}')
        except Stop as e:
            say(f'could not release {sid[:8]}: {e}')
        held.pop(sid, None)

    def park(f, why):
        f.replace(QUEUE / 'failed' / f.name)
        (QUEUE / 'failed' / (f.stem + '.why.txt')).write_text(why + '\n', encoding='utf-8')

    say(f'runner going: one send every {a.every:g} min, '
        + ('claiming the next ship right after each send and holding it through the wait' if a.hold else
           'claiming each ship right when it goes out')
        + ('. Stop it from terra-desk ' if a.from_desk else '. Ctrl+C stops it ')
        + '(releases the claims; the queue stays for next time).')
    try:
        while True:
            files = queue_files()
            sids = {json.loads(f.read_text(encoding='utf-8'))['sid']: f for f in files}
            # a ship removed from the queue: give its claim back
            for sid in [x for x in held if x not in sids]:
                release(sid, ' (removed from the queue)')
            # claim the next ship (with --hold 1): right after a send, the next one is claimed and held through the wait
            for f in files[:a.hold]:
                j = json.loads(f.read_text(encoding='utf-8'))
                if j['sid'] in held:
                    continue
                try:
                    with API_LOCK:
                        writer.send('claim', 'POST', f"/review/shipment/{j['sid']}/claim", {'queue': 'review', 'takeover': False})
                    held[j['sid']] = time.time()
                    say(f"claimed {j['title']}")
                except Stop as e:
                    if '429' in str(e) or '401' in str(e):
                        raise
                    park(f, f'could not claim: {e}')
                    say(f"NOT QUEUED ANY MORE, {j['title']}: could not claim it ({e}). Moved to send-queue/failed/")
            # keep the claims alive, like the review page does every 60 s
            if time.time() - last_beat >= 55:
                for sid in list(held):
                    # a heartbeat is safe to repeat, so a passing API hiccup (502/522/timeout) only skips this beat.
                    # Three failed beats in a row on the same claim still stop the runner.
                    try:
                        ans = writer.send('heartbeat', 'POST', f'/review/shipment/{sid}/claim/heartbeat', {'queue': 'review'})
                        beat_fails.pop(sid, None)
                    except Stop as e:
                        if '429' in str(e) or '401' in str(e):
                            raise
                        beat_fails[sid] = beat_fails.get(sid, 0) + 1
                        if beat_fails[sid] >= 3:
                            raise
                        say(f'heartbeat for {sid[:8]} failed ({e}), trying again in a minute ({beat_fails[sid]}/3)')
                        continue
                    if isinstance(ans, dict) and ans.get('held') is False:
                        say(f'lost the claim on {sid[:8]} (taken over or expired), will try to claim it again')
                        held.pop(sid, None)
                last_beat = time.time()
            # send the oldest one when it's time
            due = not paused and (last_send is None or time.time() - last_send >= a.every * 60)
            if files and due and (a.hold == 0 or json.loads(files[0].read_text(encoding='utf-8'))['sid'] in held):
                f = files[0]
                j = json.loads(f.read_text(encoding='utf-8'))
                job = {'sid': j['sid'], 'verdict': j['verdict'], 'steps': j['steps'], 'd': j['d']}
                if already_sent(j['sid'], base):
                    f.replace(QUEUE / 'sent' / f.name)
                    held.pop(j['sid'], None)
                    say(f"{j['title']}: already sent (writes.jsonl), dropped from the queue")
                    continue
                try:
                    send_job(a, job, api, writer, quiet=True, holding=j['sid'] in held)
                    f.replace(QUEUE / 'sent' / f.name)
                    held.pop(j['sid'], None)
                    say(f"SENT {j['verdict']}: {j['title']}")
                except Refuse as e:
                    park(f, str(e))
                    if j['sid'] in held:
                        release(j['sid'])
                    say(f"NOT SENT, {j['title']} changed since you approved it:\n{e}\n  moved to send-queue/failed/")
                last_send = time.time()
                continue
            left = 0 if last_send is None else max(0, a.every * 60 - (time.time() - last_send))
            state = 'PAUSED' if paused else f'next send in {int(left // 60)}:{int(left % 60):02d}'
            if interactive:
                print(f'\r  {len(files)} queued, {len(held)} claimed, every {a.every:g} min, {state}  '
                      f'(type minutes or p + Enter)   ', end='', flush=True)
            elif int(time.time()) % 30 == 0:
                print(f'  {len(files)} queued, {len(held)} claimed, every {a.every:g} min, {state}', flush=True)
            # live controls: type a number + Enter to change the gap, p + Enter to pause or resume.
            # `terra-write.py every MIN` from another terminal does the same through send-queue/every
            change = None
            if interactive:
                if select.select([sys.stdin], [], [], 1)[0]:
                    change = sys.stdin.readline().strip().lower()
            else:
                time.sleep(1)
            ctl = QUEUE / 'every'
            if ctl.exists():
                change = ctl.read_text(encoding='utf-8').strip().lower()
                ctl.unlink()
            if change == 'stop':
                raise KeyboardInterrupt
            if change in ('p', 'pause', 'resume'):
                paused = not paused if change == 'p' else change == 'pause'
                say('paused, nothing will be sent until you resume (p + Enter)' if paused else 'resumed')
            elif change:
                try:
                    minutes = float(change)
                    if minutes <= 0:
                        raise ValueError
                    a.every = minutes
                    say(f'now sending one every {a.every:g} min'
                        + ('' if last_send is None else f', next one at {time.strftime("%H:%M:%S", time.localtime(last_send + a.every * 60))}'))
                except ValueError:
                    say(f'{change!r}: type a number of minutes above 0, or p to pause/resume')
    except KeyboardInterrupt:
        print()
        say('stopping')
    except Stop as e:
        print()
        say(f'STOPPED: {e}\n  details are in {LOG}. Nothing else will be sent.')
    finally:
        for sid in list(held):
            release(sid)
        (QUEUE / 'runner.pid').unlink(missing_ok=True)
        n = len(queue_files())
        say(f'{n} still in the queue' + (', run `terra-write.py run --every ...` again to send them' if n else ''))


def cmd_decide(a):
    path = Path(a.file).expanduser()
    if not path.exists() and (ROOT / a.file).exists():
        path = ROOT / a.file
    blocks = draft_blocks(path)
    if a.only:
        blocks = [b for b in blocks if str(b['num']) == str(a.only) or (b['sid'] or '').startswith(a.only)]
    if not blocks:
        raise SystemExit(f'no draft block in {path}' + (f' for {a.only}' if a.only else ''))
    if a.queue and a.send:
        raise SystemExit('pick one: --send sends each ship as you approve it, --queue hands it to `terra-write.py run`')
    if a.queue and not sys.stdin.isatty():
        raise SystemExit('refusing: --queue needs you at a terminal to approve each ship')
    base, api, writer = setup(a)
    failed, sent, skipped, queued = 0, 0, 0, 0
    for i, b in enumerate(blocks, 1):
        print(f"\n== [{i}/{len(blocks)}] {b['title']} ({path.name} line {b['line']})")
        try:
            when = b['sid'] and (a.send or a.queue) and already_sent(b['sid'], base)
            if when:
                print(f'already sent on {when} (writes.jsonl), skipping')
                skipped += 1
                continue
            if b['sid'] and a.queue and queued_file(b['sid']):
                print('already in the send queue, skipping')
                skipped += 1
                continue
            if b.get('bare'):
                raise Refuse('  - no draft block with ### headings for this ship (a bare move note, or the older\n'
                             '    bold-label format). For a move write ### Verdict / move to fraud (or move to admin)\n'
                             '    / ### Move note / the note, all in one plain code block')
            if not b['sid']:
                raise Refuse('  - no review URL above the block')
            done = decide_one(a, b, base, api, writer)
            if done == 'queued':
                queued += 1
            elif done:
                sent += 1
        except Refuse as e:
            failed += 1
            print(f'NOT SENDING, fix these first:\n{e}')
        except Skip:
            skipped += 1
            print('  skipped, nothing sent')
        except Quit:
            print('  stopped, nothing sent for this one')
            break
        except KeyboardInterrupt:
            print('\n  stopped approving')
            break
        except Stop as e:
            print(f'STOPPED: {e}')
            print(f'details are in {LOG}')
            sys.exit(2)
    if a.queue:
        print(f'\n{queued} queued, {skipped} skipped, {failed} refused, {len(blocks) - queued - skipped - failed} not reached')
        if queued and not runner_alive():
            print('no runner is going right now. Start one in another terminal:\n'
                  f'  cd {HERE} && python3 terra-write.py run --every 4')
    elif a.send:
        print(f'\n{sent} sent, {skipped} skipped, {failed} refused, {len(blocks) - sent - skipped - failed} not sent')
    sys.exit(1 if failed else 0)


def decide_one(a, b, base, api, writer):
    sid = b['sid']
    draft = parse_drafts(b['text'])
    with API_LOCK:
        d = fetch(api, sid)
    s = d['shipment']
    verdict, steps, summary, warns = build(draft, d, a)

    dump, stamp = newest_dump(sid)
    if dump and dump['shipment'].get('evidenceVersion') != s.get('evidenceVersion'):
        raise Refuse(f"  - evidence is v{s.get('evidenceVersion')} now but v{dump['shipment'].get('evidenceVersion')} in "
                     f"the {stamp} dump: the player pushed or it was resynced since, so re-check before deciding")
    if dump:
        # a flag that showed up after the dump wasn't in what you reviewed; the acknowledgement count
        # already has to match, so this is a heads-up to read the flag/acknowledgement pairing below
        old = {(f.get('kind'), f.get('severity')) for f in dump.get('flags') or [] if not f.get('dismissedAt')}
        new = [f for f in d.get('flags') or [] if not f.get('dismissedAt') and (f.get('kind'), f.get('severity')) not in old]
        for f in new:
            warns.append(f"new {f.get('severity')} flag since the {stamp} dump: {f.get('kind')} ({f.get('title')})")

    m = d.get('maker') or {}
    print(f"ship     {s.get('title')} by {m.get('username')}  {SITE}/admin/review/{sid}")
    print(f"evidence v{s.get('evidenceVersion')}" + (f' (same as the {stamp} dump)' if dump else ' (no dump to compare with)')
          + ('  [offline: from the dump, not live]' if api is None else ''))
    for line in summary:
        print(f'verdict  {line}' if line is summary[0] else f'         {line}')
    for w in warns:
        print(f'warning  {w}')
    show_preview(b, verdict, steps)
    if a.json or not (a.send or a.queue):
        print(f"requests, in order{' (DRY RUN, nothing sent)' if not (a.send or a.queue) else ''}:")
        show_steps(steps, base)
    else:
        print('  requests ' + ' -> '.join(f'{m} {p.rsplit("/", 1)[-1]}' for _, m, p, _ in steps) + '  (--json to see the bodies)')
    if not (a.send or a.queue):
        return False

    approve_prompt(verdict)
    job = {'b': b, 'sid': sid, 'verdict': verdict, 'steps': steps, 'd': d}
    if a.queue:
        return enqueue(job, getattr(a, 'priority', False))
    return send_job(a, job, api, writer)


API_LOCK = threading.Lock()


def send_job(a, job, api, writer, quiet=False, holding=False):
    with API_LOCK:
        return _send_job(a, job, api, writer, quiet, holding)


def _send_job(a, job, api, writer, quiet=False, holding=False):
    """holding: the runner already holds the claim, so skip the claim step and leave releasing to it."""
    say = (lambda *x, **k: None) if quiet else print
    sid, verdict, steps, d = job['sid'], job['verdict'], job['steps'], job['d']
    if holding:
        steps = [st for st in steps if st[0] != 'claim']
    s = d['shipment']
    # the confirmation (or the --every wait) can sit for minutes, so look again right before the first write
    d2 = api.get(f'/review/shipment/{sid}')
    again = ship_problems(d2, verdict, steps[-1][3].get('hold') if verdict == 'move' else None)
    if d2['shipment'].get('evidenceVersion') != s.get('evidenceVersion'):
        again.append('evidence version changed since you approved it')
    if [f['kind'] for f in danger_to_ack(d2)] != [f['kind'] for f in danger_to_ack(d)]:
        again.append('danger flags changed since you approved it')
    if again:
        raise Refuse('\n'.join(f'  - {x}' for x in again))

    claimed = False
    video_url = None
    try:
        for kind, method, path, body in steps:
            if kind == 'approve':
                if any(k == 'dismiss' for k, *_ in steps):
                    d3 = api.get(f'/review/shipment/{sid}')
                    if danger_to_ack(d3):
                        raise Stop('danger flags still not acknowledged after dismissing them')
                    if d3['shipment'].get('evidenceVersion') != s.get('evidenceVersion'):
                        raise Stop('evidence version changed after the acknowledgements, run it again')
                body = {**body, 'videoUrl': video_url}
            answer = writer.send(kind, method, path, body)
            say(f'  sent {method} {path} -> {json.dumps(answer)}')
            if kind == 'claim':
                claimed = True
            if kind == 'video':
                video_url = (answer or {}).get('url')
                if not video_url:
                    raise Stop('video upload gave no url')
            if kind == 'approve' and isinstance(answer, dict) and answer.get('approvedSeconds') is not None:
                want = captured_seconds(d) - body['deflationSeconds']
                if answer['approvedSeconds'] != want:
                    say(f"  warning: Terra approved {hours(answer['approvedSeconds'])}, expected {hours(want)}")
        say(f'done: {verdict}')
        return True
    except Stop as e:
        # give the claim back unless we were rate limited (then send nothing more; it expires on its own)
        if claimed and '429' not in str(e):
            try:
                writer.send('release', 'DELETE', f'/review/shipment/{sid}/claim', None, {'queue': 'review'})
                say('  released the claim')
            except Stop as e2:
                say(f'  could not release the claim: {e2}')
        raise


def cmd_note(a):
    base, api, writer = setup(a)
    d = fetch(api, resolve(a.ref))
    pid = d['shipment']['projectId']
    body = {'body': a.text.strip(), 'audience': 'fraud' if a.fraud else 'reviewer'}
    if not body['body']:
        raise SystemExit('empty note')
    print(f"ship     {d['shipment'].get('title')}  (project {pid})")
    show_steps([('note', 'POST', f'/review/project/{pid}/notes', body)], base)
    if a.send:
        confirm('note')
        print(json.dumps(writer.send('note', 'POST', f'/review/project/{pid}/notes', body)))


def cmd_release(a):
    base, api, writer = setup(a)
    sid = resolve(a.ref)
    print(f'  1. DELETE {base}/review/shipment/{sid}/claim?queue=review')
    if a.send:
        confirm('release')
        print(json.dumps(writer.send('release', 'DELETE', f'/review/shipment/{sid}/claim', None, {'queue': 'review'})))


def main(argv=None):
    top = argparse.ArgumentParser(prog='terra-write.py', description=__doc__.split('\n\n')[0])
    sub = top.add_subparsers(dest='cmd', required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--send', action='store_true', help='really send: goes through the file one ship at a time and asks before each')
    common.add_argument('--api', help=f'API base, default {REAL_API}; http://127.0.0.1:<port> for a fake server')
    common.add_argument('--offline', action='store_true', help='dry run from the newest queue-json dump, no network')
    p = sub.add_parser('decide', parents=[common], help='submit the decision in a ship page')
    p.add_argument('file', help='ship page, e.g. ships/12-some-ship.md')
    p.add_argument('--only', help='bulk number or shipment id, for files with several ships')
    p.add_argument('--video', help='approve: the review video to upload (mp4, webm or mov, under 100MB)')
    p.add_argument('--no-video', action='store_true', help='approve without a video (only if your account allows it)')
    p.add_argument('--json', action='store_true', help='also print the exact request bodies when sending')
    p.add_argument('--priority', action='store_true',
                   help='with --queue: put these ahead of everything already queued without --priority')
    p.add_argument('--queue', action='store_true',
                   help='approve each ship and add it to the send queue instead of sending it (see `run`)')
    p.set_defaults(fn=cmd_decide)
    rn = sub.add_parser('run', help='send the queue, one ship every --every minutes')
    rn.add_argument('--every', type=float, required=True, metavar='MIN', help='minutes between sends')
    rn.add_argument('--hold', type=int, default=1, choices=[0, 1],
                    help='1 (default): send a, claim b, wait, send b, claim c, ... so the next ship is yours during the '
                         'wait (Terra allows one claim at a time, so you can\'t review in the browser meanwhile). '
                         '0: claim each ship only right when it is sent, which keeps your claim free while waiting')
    rn.add_argument('--api', help=f'API base, default {REAL_API}; http://127.0.0.1:<port> for a fake server')
    rn.add_argument('--from-desk', action='store_true',
                    help='started from terra-desk (no terminal needed; stop it with `every stop` or the Stop button)')
    rn.set_defaults(fn=cmd_run, json=False, video=None, no_video=False)
    ev = sub.add_parser('every', help='change a running runner\'s minutes between sends, or pause / resume it')
    ev.add_argument('value', help='minutes (e.g. 2 or 0.5), or pause / resume / stop')
    ev.set_defaults(fn=cmd_every)
    q = sub.add_parser('queue', help='show the send queue, or take a ship out of it')
    q.add_argument('--remove', metavar='ID', help='shipment id (or the start of it) to take out')
    q.set_defaults(fn=cmd_queue)
    n = sub.add_parser('note', parents=[common], help='add a staff note to the project of a ship')
    n.add_argument('ref')
    n.add_argument('text')
    n.add_argument('--fraud', action='store_true', help='fraud-only note')
    n.set_defaults(fn=cmd_note)
    r = sub.add_parser('release', parents=[common], help='release your claim on a ship')
    r.add_argument('ref')
    r.set_defaults(fn=cmd_release)
    a = top.parse_args(argv)
    try:
        a.fn(a)
    except Refuse as e:
        raise SystemExit(f'NOT SENDING:\n{e}')
    except ApiError as e:
        raise SystemExit(str(e))


if __name__ == '__main__':
    main()
