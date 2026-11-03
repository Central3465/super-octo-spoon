"""Render a shipment's API data (GET /review/shipment/<id>) as an HTML page laid out like Terra's review page.

Read-only: this only formats data that was already fetched. Commit author emails are never shown. Everything from
the API is escaped; the web page also blocks scripts in it (Content-Security-Policy) and opens links itself.
images maps an image's URL to where terra-desk serves the downloaded copy.
"""
import html
import re
from datetime import datetime, timezone

from . import look as T
from .core import WEEKS

C = {'bg': T.BG, 'code': '#1d1c24', 'line': T.LINE, 'text': T.INK, 'dim': T.MUTED, 'accent': T.LILAC, 'green': T.GREEN,
     'yellow': T.BUTTER, 'red': T.PINK, 'blue': T.BLUE}
PINK, BLUE, BUTTER, PEACH, LILAC, MINT = T.TINTS
LABEL = f"font-family:'{T.MONO}'; font-size:8.5pt; font-weight:500; letter-spacing:1px; text-transform:uppercase"


def e(x):
    return html.escape(str(x if x is not None else ''))


def hrs(sec):
    return f'{(sec or 0) / 3600:.2f}h'


def ago(iso):
    if not iso:
        return ''
    try:
        t = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except ValueError:
        return iso
    d = datetime.now(timezone.utc) - t
    if d.days >= 1:
        return f'{d.days}d ago'
    h = d.seconds // 3600
    return f'{h}h ago' if h else f'{d.seconds // 60}m ago'


def when(iso):
    try:
        return datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone().strftime('%b %d %H:%M')
    except (ValueError, AttributeError):
        return iso or ''


def md(text):
    """A small markdown subset for devlog bodies and notes: headings, bold, inline code, links, lists, paragraphs."""
    out, para, lst = [], [], []

    def flush():
        if para:
            out.append('<p>' + '<br>'.join(para) + '</p>')
            para.clear()
        if lst:
            out.append('<ul>' + ''.join(f'<li>{x}</li>' for x in lst) + '</ul>')
            lst.clear()

    def inline(s):
        s = e(s)
        s = re.sub(r'`([^`]+)`', rf'<code style="background:{C["code"]}">\1</code>', s)
        s = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', s)
        s = re.sub(r'\[([^\]]+)\]\((https?://[^)]+)\)', r'<a href="\2">\1</a>', s)
        s = re.sub(r'(?<!["=>])(https?://[^\s<]+)', r'<a href="\1">\1</a>', s)
        return s

    in_code = False
    code = []
    for line in (text or '').replace('\r', '').split('\n'):
        if line.strip().startswith('```'):
            if in_code:
                out.append(f'<pre style="background:{C["code"]}">{e(chr(10).join(code))}</pre>')
                code = []
            else:
                flush()
            in_code = not in_code
            continue
        if in_code:
            code.append(line)
            continue
        line = re.sub(r'^\\#', '#', line)
        if not line.strip():
            flush()
        elif m := re.match(r'^(#{1,6})\s+(.*)', line):
            flush()
            out.append(f'<p><b>{inline(m.group(2))}</b></p>')
        elif m := re.match(r'^\s*[-*]\s+(.*)', line):
            if para:
                flush()
            lst.append(inline(m.group(1)))
        else:
            if lst:
                flush()
            para.append(inline(line))
    flush()
    return ''.join(out)


def card(title, body, right='', tint=BLUE):
    """A box with its title as a solid tab, like the feature cards on Fluxion's page."""
    return (f'<table width="100%" border="1" cellspacing="0" cellpadding="12" style="border-color:{C["line"]}; '
            f'border-style:solid; border-collapse:collapse; margin-bottom:14px"><tr><td>'
            f'<span style="background:{tint}; color:{C["bg"]}; {LABEL}">&nbsp;{e(title)}&nbsp;</span>'
            + (f'<span style="color:{C["dim"]}"> &nbsp; {right}</span>' if right else '')
            + f'<br>{body}</td></tr></table>')


def chip(text, color):
    return f'<span style="background:{color}; color:{C["bg"]}; {LABEL}">&nbsp;{e(text)}&nbsp;</span>'


def render(d, checks=None, images=None, checking=False):
    images = images or {}
    s, p, mk = d.get('shipment') or {}, d.get('project') or {}, d.get('maker') or {}
    ev = (d.get('evidence') or {}).get('data') or {}
    week = WEEKS.get(s.get('weekNumber'), f"week {s.get('weekNumber')}")
    parts = [f'<div class="review-page" style="color:{C["text"]}; font-family:\'{T.SANS}\'">']

    # header
    stage = s.get('stage') or s.get('status') or ''
    stage_color = {'review': C['blue'], 'changes_requested': C['yellow'], 'completed': C['green'],
                   'rejected': C['red'], 'fraud_pending': C['red']}.get(stage, C['dim'])
    chips = [chip(stage.replace('_', ' '), stage_color), chip(week, C['accent'])]
    if s.get('changesRounds'):
        chips.append(chip(f"changes requested {s['changesRounds']}x before", C['yellow']))
    if p.get('isUpdate'):
        chips.append(chip('update', C['blue']))
    if s.get('fraudPending'):
        chips.append(chip('fraud check pending', C['dim']))
    parts.append(f'<h1 style="margin:0; font-size:26pt; font-weight:800">{e(s.get("title") or p.get("title"))}</h1>'
                 f'<p style="color:{C["dim"]}">by <b style="color:{C["text"]}">{e(mk.get("username"))}</b>'
                 f'{" (" + e(mk.get("displayName")) + ")" if mk.get("displayName") else ""}'
                 f'{" · GitHub " + e(mk.get("githubLogin")) if mk.get("githubLogin") else ""}'
                 f' · shipped {ago(s.get("submittedAt"))} · {" ".join(e(x) for x in s.get("platforms") or [])}</p>'
                 f'<p>{" ".join(chips)}</p>')

    parts.append(checks_card(checks, checking))

    # project + ship notes
    links = []
    for label, u in (('Repo', p.get('codeUrl')), ('Demo', p.get('demoUrl'))):
        if u:
            links.append(f'<a href="{e(u)}">{label}: {e(u)}</a>')
    body = ''
    shot = images.get(p.get('screenshotUrl') or '')
    if shot:
        body += f'<p><a href="{e(shot)}"><img src="{e(shot)}" width="560"></a></p>'
    body += (f'<p>{md(p.get("description"))}</p>' if p.get('description') else '')
    body += f'<p><b>Ship notes</b></p>{md(s.get("notes"))}'
    body += (f'<p><b>AI declaration:</b> {e(s.get("aiDeclaration") or "none given")}</p>')
    if p.get('madeWith') or p.get('what'):
        body += f'<p style="color:{C["dim"]}">{e(p.get("what") or "")} {("· made with " + e(p.get("madeWith"))) if p.get("madeWith") else ""}</p>'
    body += '<p>' + '<br>'.join(links) + '</p>'
    names = p.get('hackatimeProjectNames') or []
    if names:
        body += f'<p style="color:{C["dim"]}">Hackatime: {e(", ".join(names))}' + (f' · branch {e(p.get("branch"))}' if p.get('branch') else '') + '</p>'
    parts.append(card('Project', body, tint=PINK))

    # hours + flags
    credited = sum(x.get('creditedSeconds') or 0 for x in d.get('devlogs') or [])
    t = ev.get('time') or {}
    rows = [('Credited', hrs(credited) + f' from {len(d.get("devlogs") or [])} devlogs'),
            ('Tracked', hrs(s.get('hackatimeSeconds') or t.get('totalSeconds'))),
            ('AI coding', hrs(s.get('aiSeconds') or t.get('aiSeconds')))]
    if d.get('waterPaid') is not None:
        rows.append(('Water paid', f'{d["waterPaid"]:.2f}'))
    body = '<table cellpadding="3">' + ''.join(f'<tr><td style="color:{C["dim"]}">{a}</td><td><b>{b}</b></td></tr>' for a, b in rows) + '</table>'
    fs = d.get('fraudSuggestion')
    if fs:
        body += f'<p style="color:{C["red"]}"><b>Fraud recommends cutting {hrs(fs.get("seconds"))}</b>: {e(fs.get("reason"))}</p>'
    for f in d.get('flags') or []:
        col = C['red'] if f.get('severity') == 'danger' else C['yellow']
        dismissed = f' <span style="color:{C["dim"]}">(dismissed)</span>' if f.get('dismissedAt') else ''
        body += f'<p><span style="color:{col}"><b>{e(f.get("severity"))}</b></span> {e(f.get("title"))}{dismissed}</p>'
    if not d.get('flags'):
        body += f'<p style="color:{C["dim"]}">No flags</p>'
    parts.append(card('Hours', body, tint=BUTTER))

    # votes
    v = d.get('votes') or {}
    if v.get('list'):
        body = ''
        for x in v['list']:
            body += (f'<p><b>{e(x.get("verdict"))}</b> {e(x.get("stars"))}/7 <span style="color:{C["dim"]}">{ago(x.get("createdAt"))}</span><br>'
                     f'{md(x.get("justification"))}</p>')
        right = f'{v.get("averageStars") or 0:.1f} stars avg' + (f', suggests x{v["suggestedMultiplier"]:.2f}' if v.get('suggestedMultiplier') else '')
        parts.append(card(f'Community votes (round {v.get("round")})', body, right, LILAC))

    # previous decisions
    decs = sorted(d.get('decisions') or [], key=lambda x: x.get('createdAt') or x.get('claimedAt') or '', reverse=True)
    if decs:
        body = ''
        for x in decs:
            extra = f' x{x["payoutMultiplier"]}' if x.get('payoutMultiplier') else ''
            body += (f'<p><b>{e(x.get("decision"))}</b>{extra} by {e(x.get("reviewer") or "?")} '
                     f'<span style="color:{C["dim"]}">{ago(x.get("createdAt") or x.get("claimedAt"))}</span><br>'
                     f'{md(x.get("noteToMaker") or "")}</p>')
        parts.append(card('Previous decisions', body, tint=PEACH))

    # devlogs + commits
    commits = {c['sha']: c for c in ev.get('commits') or []}
    rows_by_devlog = {}
    for r in ev.get('rows') or []:
        rows_by_devlog.setdefault(r.get('windowDevlogId'), []).append(r)
    body = ''
    for dl in sorted(d.get('devlogs') or [], key=lambda x: x.get('createdAt') or ''):
        body += (f'<p><b style="font-size:11pt">{e(dl.get("title") or "devlog")}</b> '
                 f'<span style="color:{C["dim"]}">{hrs(dl.get("creditedSeconds"))} credited · {when(dl.get("createdAt"))}</span></p>'
                 f'{md(dl.get("body"))}')
        for img in dl.get('images') or []:
            u = img.get('url')
            if u and images.get(u):
                body += f'<p><a href="{e(images[u])}"><img src="{e(images[u])}" width="520"></a></p>'
            elif u:
                body += f'<p><a href="{e(u)}">image (loading or unavailable)</a></p>'
        crow = ''
        for r in rows_by_devlog.get(dl.get('id'), []):
            c = commits.get(r.get('sha')) or {}
            if not c:
                continue
            crow += (f'<tr><td><a href="{e(c.get("url"))}"><code>{e(c["sha"][:7])}</code></a></td>'
                     f'<td>{e((c.get("message") or "").splitlines()[0][:90])}</td>'
                     f'<td style="color:{C["green"]}">+{c.get("additions", "?")}</td><td style="color:{C["red"]}">-{c.get("deletions", "?")}</td>'
                     f'<td style="color:{C["dim"]}">{hrs((r.get("time") or {}).get("totalSeconds"))} · {when(c.get("committedAt"))}</td></tr>')
        if crow:
            body += f'<table cellpadding="2" style="margin-left:12px">{crow}</table>'
        body += f'<hr style="color:{C["line"]}">'
    parts.append(card('Devlogs and commits', body or f'<span style="color:{C["dim"]}">no devlogs</span>', tint=MINT))

    # files
    files = sorted(ev.get('files') or [], key=lambda f: -(f.get('seconds') or 0))
    if files:
        inrepo = [f for f in files if f.get('status') == 'head' and f.get('seconds')]
        out = [f for f in files if f.get('status') != 'head' and f.get('seconds')]
        body = ''.join(f'<tr><td>{e(f["path"])}</td><td style="color:{C["dim"]}">{hrs(f["seconds"])}</td></tr>' for f in inrepo[:20])
        html_ = f'<table cellpadding="2">{body}</table>'
        if out:
            html_ += '<p><b>Not in the repo</b></p><table cellpadding="2">' + ''.join(
                f'<tr><td>{e(f["path"])}</td><td style="color:{C["dim"]}">{e(f.get("status"))} · {hrs(f["seconds"])}</td></tr>' for f in out[:12]) + '</table>'
        parts.append(card('Files', html_, f'{hrs(sum(f.get("seconds") or 0 for f in inrepo))} in the repo'))

    # other programs
    up = ev.get('unifiedProjects') or []
    if up:
        body = ''.join(f'<p>{e(x.get("program"))}: {e(x.get("hours"))}h · <a href="{e(x.get("codeUrl"))}">{e(x.get("codeUrl"))}</a>'
                       f' <span style="color:{C["dim"]}">{when(x.get("approvedAt"))}</span></p>' for x in up)
        parts.append(card('Same project in other programs', body, tint=LILAC))
    parts.append('</div>')
    return ''.join(parts)


ICON = {'ok': ('✓', C['green']), 'warn': ('⚠', C['yellow']), 'bad': ('✗', C['red'])}


def checks_card(checks, checking=False):
    if not checks:
        msg = 'running the checks (read-only clone and lookups)…' if checking else 'not checked yet'
        return card('Checks', f'<span style="color:{C["dim"]}">{msg}</span>')
    def rows(items):
        return ''.join(f'<tr><td style="color:{ICON[x["level"]][1]}"><b>{ICON[x["level"]][0]}</b></td><td>{e(x["text"])}</td></tr>'
                       for x in items)
    body = ''
    for title, key in (('Shipped properly?', 'rules'), ('How to run it', 'run'), ('AI signs', 'ai')):
        if checks.get(key):
            body += f'<p><b>{title}</b></p><table cellpadding="2">{rows(checks[key])}</table>'
    if checks.get('other'):
        body += '<p><b>Other notes</b></p><table cellpadding="2">' + ''.join(
            f'<tr><td style="color:{C["dim"]}">·</td><td style="color:{C["dim"]}">{e(w)}</td></tr>' for w in checks['other'][:10]) + '</table>'
    level = checks.get('level', 'ok')
    return card('Checks', body, f'<span style="color:{ICON[level][1]}">{ {"ok": "looks shippable", "warn": "worth a look", "bad": "problems found"}[level] }</span>')
