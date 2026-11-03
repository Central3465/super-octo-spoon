"""terra-desk core: ships (from the queue, the store and imported pages), Terra state, test setup, video,
templates and queueing.

Everything that talks to Terra goes through terra-api.py (read-only) and terra-write.py (writes, only
when you press Queue; the runner sends). Nothing here opens Terra review pages: opening one claims the ship.
"""
import contextlib
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from . import prompts, system
from .db import Store

TOKEN_FILE = system.token_file()     # where terra-api.py looks for your Terra key
CONFIG_FILE = Path(os.environ.get('TERRA_DESK_CONFIG') or system.config_dir() / 'config.toml').expanduser()
URL = re.compile(r'https?://[^\s)>\]`"]+')
MD_CODE = re.compile(r'(`+)(.+?)\1')
MD_LINK = re.compile(r'(\[)([^\]\n]+)(\]\()([^)\s]+)(\))')
MD_BOLD = re.compile(r'(\*\*|__)(?=\S)(.+?)(?<=\S)\1')
MD_ITALIC = re.compile(r'(?<![\w*])(\*|_)(?=[^\s*_])(.+?)(?<=[^\s*_])\1(?![\w*])')
MD_STRIKE = re.compile(r'(~~)(?=\S)(.+?)(?<=\S)~~')
PLAYER_FIELDS = ('note to player', 'what to change')    # the text a player reads, in Slack
REVIEW_ID = re.compile(r'review/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})')
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
VIDEO_LIMIT = 20 * 1024 * 1024          # what we compress to (Terra itself takes up to 100 MiB)
STATE_ORDER = {'drafted': 0, 'notes': 1, 'new': 2, 'queued': 3, 'sent': 4, 'claimed': 5, 'gone': 6}
WEEK_ORDER = {'Prep Week 3': 0, 'Prep Week 2': 1, 'Week 1': 2, 'Week 2': 3, 'Week 3': 4}
QUEUE_COLUMNS = ['mine', 'checks', 'week', 'ship', 'player', 'hours', 'waiting', 'platform']
RENAMED_COLUMNS = {'runs on': 'platform'}
WEEKS = {6: 'Prep Week 1', 7: 'Prep Week 2', 8: 'Prep Week 3', 9: 'Week 1', 10: 'Week 2', 11: 'Week 3', 12: 'Week 4'}


# ---- config ----

DEFAULT_CONFIG = """# terra-desk settings
# workspace: where the database, clones, release downloads and videos go
workspace = "{workspace}"
# folders of markdown ship pages to import (NN-slug.md with a review URL), globs allowed, e.g. ["~/reviews/batch-*"]
ship_globs = []
# your Slack handle, used at the end of notes to players
handle = "@yourname"
# the two scripts this app drives (they ship with terra-desk; point elsewhere if you keep your own copies)
terra_api = ""
terra_write = ""
# where the send queue, writes.jsonl and queue dumps live (empty = "send" in the workspace)
data_dir = ""
# Linux only: the bubblewrap wrapper used to run builds, and the folder sandboxed programs may live in
sandbox_run = ""
sandbox_root = ""
# port for the web page on this computer (http://127.0.0.1:<port>)
port = 8765
# GitHub lookups: "gh" uses `gh auth token` if you're logged in (5,000 an hour), "" for none (60 an hour)
github_token = "gh"
# terminal for "Open outside" (empty = auto: Windows Terminal or cmd on Windows; $TERMINAL, kitty, foot, ... on Linux)
terminal = ""
# plugin files that add features (see README.md)
plugins = []
# your own rules / rubric / style files, editable under Settings… (edited in place)
documents = []
# queue table columns to show (Settings… > Queue); "ship" is always shown
queue_columns = ["mine", "checks", "week", "ship", "player", "hours", "waiting", "platform"]
"""


def load_config():
    if not CONFIG_FILE.exists():
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ws = str(system.default_workspace()).replace(str(Path.home()), '~', 1).replace('\\', '/')
        CONFIG_FILE.write_text(DEFAULT_CONFIG.replace('{workspace}', ws), encoding='utf-8')
    c = tomllib.loads(CONFIG_FILE.read_text(encoding='utf-8'))
    here = system.app_root()
    c.setdefault('workspace', str(system.default_workspace()))
    c['workspace'] = Path(c['workspace']).expanduser()
    for key, default in (('terra_api', here / 'tools/terra-api.py'), ('terra_write', here / 'tools/terra-write.py'),
                         ('sandbox_run', here / 'tools/sandbox-run')):
        c[key] = Path(c.get(key) or default).expanduser()
    c.setdefault('ship_globs', [])
    c.setdefault('handle', '@yourname')
    c.setdefault('terminal', '')
    c.setdefault('plugins', [])
    c['queue_columns'] = [RENAMED_COLUMNS.get(x, x) for x in c.get('queue_columns') or QUEUE_COLUMNS
                          if RENAMED_COLUMNS.get(x, x) in QUEUE_COLUMNS]
    c['sandbox_root'] = Path(c.get('sandbox_root') or c['workspace']).expanduser()
    c['data_dir'] = Path(c.get('data_dir') or c['workspace'] / 'send').expanduser()
    c['data_dir'].mkdir(parents=True, exist_ok=True)
    c['port'] = int(c.get('port') or 8765)
    os.environ['TERRA_HANDLE'] = c['handle']
    os.environ['TERRA_DATA_DIR'] = str(c['data_dir'])     # the tools keep their queue and log here
    (c['workspace'] / 'pages').mkdir(parents=True, exist_ok=True)
    return c


# ---- the two scripts ----

class Tools:
    """terra-api.py and terra-write.py loaded as modules (they're single-file CLIs)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._w = None
        self._gh = None
        self._api = None

    def _load(self, path):
        os.environ['TERRA_DATA_DIR'] = str(self.cfg['data_dir'])
        argv, cwd = sys.argv, os.getcwd()
        sys.argv = [path.name]
        os.chdir(path.parent)
        try:
            return runpy.run_path(str(path), run_name='td')
        finally:
            sys.argv = argv
            os.chdir(cwd)

    @property
    def W(self):
        if self._w is None:
            self._w = self._load(self.cfg['terra_write'])
        return self._w

    def api(self):
        if self._api is None:
            self._api = self.W['connect']()
        return self._api

    def github_env(self):
        """GITHUB_TOKEN for prep's GitHub lookups (5,000 an hour instead of 60), from the env or `gh auth token`.
        terra-api.py only ever sends it to api.github.com. It's never written anywhere."""
        if self._gh is None:
            self._gh = os.environ.get('GITHUB_TOKEN', '')
            tok = self.cfg.get('github_token', 'gh')
            if not self._gh and tok == 'gh' and shutil.which('gh'):
                r = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True)
                self._gh = r.stdout.strip() if r.returncode == 0 else ''
            elif not self._gh and tok and tok != 'gh':
                self._gh = tok
        env = system.tool_env({'TERRA_DATA_DIR': str(self.cfg['data_dir'])})
        return {**env, 'GITHUB_TOKEN': self._gh} if self._gh else env

    def api_argv(self, *args):
        return system.tool_argv(self.cfg['terra_api'], *args)

    def write_argv(self, *args):
        return system.tool_argv(self.cfg['terra_write'], *args)

    def run_api(self, *args, timeout=600):
        """terra-api.py as a subprocess (read-only commands)."""
        out = subprocess.run(self.api_argv(*args), capture_output=True, text=True, encoding='utf-8', errors='replace',
                             timeout=timeout, cwd=self.cfg['workspace'], env=self.github_env())
        if out.returncode:
            raise RuntimeError((out.stderr or out.stdout).strip()[-600:])
        return out.stdout


# ---- ships ----

@dataclass
class Ship:
    sid: str
    title: str = ''
    username: str = ''
    week: str = ''
    num: int = 0
    item: dict | None = None          # the ship's row in the review queue, while it's waiting there
    page_text: str = ''               # an imported page (without its Drafts), shown on the Page tab
    quick: str = ''                   # test steps
    suggested: str | None = None
    notes: str = ''
    draft: str | None = None
    fields: dict = field(default_factory=dict)
    video: str | None = None
    release_dir: str | None = None
    skipped: bool = False
    remarks: str = ''                 # what a plugin noticed about the draft (e.g. Claude), shown under it
    state: str = 'new'

    @property
    def key(self):
        return self.sid

    @property
    def verdict_kind(self):
        v = (self.fields.get('verdict') or '').lower()
        return ('changes' if 'change' in v else 'reject' if 'reject' in v else 'approve' if 'approve' in v
                else 'skip' if 'skip' in v else v)


def page_folders(cfg):
    out = [cfg['workspace'] / 'pages']
    for g in cfg['ship_globs']:
        g = str(Path(g).expanduser()).replace('\\', '/')
        base, pat = g.rsplit('/', 1) if '*' in g else (g, '')
        out += sorted(p for p in Path(base).glob(pat) if p.is_dir()) if pat else [Path(base)]
    return [p for p in out if p.is_dir()]


def test_steps_files(folders):
    out = {}
    for folder in folders:
        f = folder / 'test-steps.md'
        if f.exists():
            for m in re.finditer(r'^## (\d+) [^\n]*\n(.*?)(?=^## \d+ |\Z)', f.read_text(encoding='utf-8', errors='ignore'), re.S | re.M):
                out[(folder.name, int(m.group(1)))] = re.split(r'^### Notes', m.group(2), flags=re.M)[0].strip()
    return out


def import_pages(cfg, store, hooks=None):
    """Read markdown ship pages (NN-slug.md with a review URL) into the store. Only changed files are re-read."""
    folders = page_folders(cfg)
    steps = test_steps_files(folders)
    seen = store.source_mtimes()
    n = 0
    for folder in folders:
        for f in folder.glob('[0-9]*-*.md'):
            if f.name.startswith('00-'):
                continue
            mtime = f.stat().st_mtime
            if seen.get(str(f)) == mtime:
                continue
            text = f.read_text(encoding='utf-8', errors='ignore')
            m = REVIEW_ID.search(text[:1500])
            if not m:
                continue
            first = text.split('\n', 1)[0].lstrip('# ').strip()
            num = int(f.name.split('-')[0])
            head, notes, draft = split_drafts(text)
            quick = next((section(text, s) for s in [*(hooks.test_sections if hooks else []), 'Test steps', 'Quick look',
                                                     'Test'] if section(text, s)), '') or steps.get((folder.name, num), '')
            quick = re.split(r'^### Notes', quick, flags=re.M)[0].strip()
            sug = None
            if hooks and hooks.suggested_section:
                sm = re.search(rf'^## {re.escape(hooks.suggested_section)}\n.*?^```[^\n]*\n(### Verdict.*?)^```', head, re.S | re.M)
                sug = sm.group(1).rstrip('\n') if sm else None
            wk = re.search(r'(Prep Week \d|Week \d+)', first)
            week = next((k for k, v in WEEKS.items() if wk and v == wk.group(1)), None)
            title = re.sub(r',\s*(Prep Week \d|Week \d+).*$', '', re.sub(r'^\d+\s+', '', first))
            store.import_page(m.group(1), f, mtime, head, quick, sug, num, title, week, notes.strip(), draft)
            n += 1
    return n


def build_ships(cfg, store, hooks=None):
    """Every ship the store knows (the queue, imported pages, your work), with its state."""
    sent, queued = sent_sids(cfg), set(queued_sids(cfg))
    out = []
    for r in store.rows():
        item = json.loads(r['queue_item']) if r['queue_item'] and r['in_queue'] else None
        s = Ship(r['sid'], r['title'] or '', r['username'] or '', WEEKS.get(r['week'], ''), r['num'] or 0, item,
                 r['page_text'] or '', r['test_steps'] or '',
                 r['suggested'], r['notes'] or '', r['draft'], draft_fields(r['draft']), r['video'], r['release_dir'],
                 bool(r['skipped']), remarks=r['remarks'] or '')
        if not s.quick and hooks:
            s.quick = next((x for x in (fb(s) for fb in hooks.test_fallbacks) if x), '')
        if s.sid in sent:
            s.state = 'sent'
        elif s.sid in queued:
            s.state = 'queued'
        elif not item:
            s.state = 'gone'
        elif item.get('claimedBy') and not item.get('claimedByMe'):
            s.state = 'claimed'
        elif s.draft:
            s.state = 'drafted'
        elif s.notes.strip():
            s.state = 'notes'
        out.append(s)
    return out


def fetch_page(store, tools, sid, max_age=600):
    """The review-page data for a ship (GET /review/shipment/<id>, read-only), cached in the store."""
    d = store.page(sid, max_age)
    if d is None:
        d = tools.api().get(f'/review/shipment/{sid}')
        store.set_page(sid, d)
    return d


def split_drafts(text):
    """(head, notes, draft): draft is the last fenced block under ## Drafts that starts with ### Verdict."""
    head, sep, tail = text.partition('\n## Drafts')
    if not sep:
        return text, '', None
    for m in reversed(list(re.finditer(r'^```[^\n]*\n(.*?)^```[ \t]*$', tail, re.S | re.M))):
        if m.group(1).lstrip().startswith('### Verdict'):
            return head, (tail[:m.start()] + tail[m.end():]).strip('\n'), m.group(1).rstrip('\n')
    return head, tail.strip('\n'), None


def draft_fields(draft):
    out, cur = {}, None
    for line in (draft or '').split('\n'):
        m = re.match(r'^###\s+(.*)$', line)
        if m:
            cur = m.group(1).strip().lower()
            out[cur] = ''
        elif cur:
            out[cur] += line + '\n'
    return {k: v.strip() for k, v in out.items()}


def build_draft(fields):
    """fields (ordered dict of heading -> text) back into a ### block."""
    return '\n\n'.join(f'### {k}\n{v.strip()}' for k, v in fields.items() if v is not None and str(v).strip() != '')


def section(text, name):
    m = re.search(rf'^## {re.escape(name)}[^\n]*\n(.*?)(?=^## |\Z)', text, re.S | re.M)
    return m.group(1).strip() if m else ''


def slack_markup(text):
    """Markdown emphasis as Slack writes it: **bold** -> *bold*, *italic* -> _italic_, ~~strike~~ -> ~strike~.
    Code spans are left alone."""
    codes = []

    def keep(m):
        codes.append(m.group())
        return f'\x00{len(codes) - 1}\x00'
    text = MD_CODE.sub(keep, text)
    text = MD_BOLD.sub(lambda m: f'\x01{m.group(2)}\x01', text)
    text = MD_ITALIC.sub(lambda m: f'_{m.group(2)}_', text)
    text = MD_STRIKE.sub(lambda m: f'~{m.group(2)}~', text)
    text = text.replace('\x01', '*')
    return re.sub('\x00(\\d+)\x00', lambda m: codes[int(m.group(1))], text)


def slack_draft(draft):
    """The draft with its player-facing sections in Slack markup (the rest goes to Airtable and staff as written)."""
    parts = re.split(r'(?m)^(###[ \t]+.*)$', draft)
    for i in range(1, len(parts) - 1, 2):
        if parts[i][3:].strip().lower() in PLAYER_FIELDS:
            parts[i + 1] = slack_markup(parts[i + 1])
    return ''.join(parts)


def links(text):
    seen = []
    for u in URL.findall(text):
        u = u.rstrip('.,')
        if not is_review_page(u) and u not in seen:
            seen.append(u)
    return seen


def is_review_page(u):
    return 'terra.hackclub.com' in u


# ---- Terra state ----

def sent_sids(cfg):
    log = Path(os.environ.get('TERRA_WRITE_LOG') or Path(cfg['data_dir']) / 'writes.jsonl')
    sids = set()
    if log.exists():
        for line in log.read_text(encoding='utf-8', errors='replace').splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            m = re.match(r'^/review/shipment/([0-9a-f-]{36})/(approve|changes|reject|send)$', e.get('path') or '')
            if m and 200 <= int(e.get('status') or 0) < 300:
                sids.add(m.group(1))
    return sids


def queue_dir(cfg):
    return Path(os.environ.get('TERRA_WRITE_QUEUE') or Path(cfg['data_dir']) / 'send-queue')


def queued_sids(cfg):
    out = {}
    for f in queue_dir(cfg).glob('*.json'):
        m = UUID.search(f.name)
        if m:
            out[m.group(0)] = f
    return out


def runner_alive(cfg):
    try:
        return system.pid_alive((queue_dir(cfg) / 'runner.pid').read_text().strip())
    except OSError:
        return False


def runner_control(cfg, word):
    """Tell a running runner to change its gap (minutes), pause/resume ('pause', 'resume') or 'stop'."""
    queue_dir(cfg).mkdir(parents=True, exist_ok=True)
    (queue_dir(cfg) / 'every').write_text(str(word), encoding='utf-8')


def terra_waiting(tools):
    items = json.loads(tools.run_api('queue', '--filter', 'waiting', '--json', timeout=90))
    return {i['id']: i for i in items}


# ---- test setup ----

def prep_args(cfg, sid, download=True):
    """terra-api.py's arguments to clone the repo (and a worktree at the ship commit) and download the release, never
    running anything. It prints the facts as JSON on stdout and its progress on stderr."""
    builds = cfg['workspace'] / 'builds'
    builds.mkdir(parents=True, exist_ok=True)
    return ['prep', sid, '--json', '--dir', str(builds)] + (['--download'] if download else [])


def dump_folders(cfg):
    """terra-api.py dump writes each ship's review-page data to queue-json/<stamp>/<id>.json in the data folder."""
    return sorted(p for p in (Path(cfg['data_dir']) / 'queue-json').glob('*') if p.is_dir())


def import_dump(store, folder):
    """Cache the review-page data from a dump folder, so those ships' pages open even while Terra is down."""
    n = 0
    for f in Path(folder).glob('*.json'):
        try:
            data = json.loads(f.read_text(encoding='utf-8'))['shipment']
        except (OSError, ValueError, KeyError, TypeError):
            continue
        store.set_page(f.stem, data)
        n += 1
    return n


def prep(cfg, tools, sid, download=True):
    return json.loads(tools.run_api(*prep_args(cfg, sid, download), timeout=900))


def find_runnables(folder):
    """Things in a release folder that can be started, best guess first: (path, how). how is 'exe' (a Windows
    program: native on Windows, Wine on Linux), 'msi', 'bat', 'java', 'appimage' or 'native' (a Linux binary)."""
    out = []
    if not folder or not Path(folder).is_dir():
        return out
    for p in sorted(Path(folder).rglob('*')):
        if '.sandbox-home' in p.parts or not p.is_file():
            continue
        name = p.name.lower()
        if name.endswith('.exe') and 'crashhandler' not in name and 'unins' not in name:
            out.append((p, 'exe'))
        elif name.endswith('.msi') and system.WINDOWS:
            out.append((p, 'msi'))
        elif name.endswith(('.bat', '.cmd')) and system.WINDOWS:
            out.append((p, 'bat'))
        elif name.endswith('.appimage'):
            if not system.WINDOWS:
                out.append((p, 'appimage'))
        elif name.endswith('.jar'):
            out.append((p, 'java'))
        elif system.WINDOWS:
            continue                                      # Linux binaries can't start here
        elif os.access(p, os.X_OK) or p.suffix in ('', '.x86_64', '.out'):
            try:
                if p.read_bytes()[:4] == b'\x7fELF':
                    out.append((p, 'native'))
            except OSError:
                pass
    out.sort(key=lambda x: ('console' in x[0].name.lower(), len(x[0].parts), x[0].name))
    return out


def sandbox_command(cfg, folder, exe, how):
    """Linux: the sandbox-run (bubblewrap) command for a runnable (a list), run from its release folder.
    On Windows, see system.sandbox_launch (Windows Sandbox)."""
    folder = Path(folder)
    rel = Path(exe).relative_to(folder).as_posix()
    base = ['env', f"TERRA_SANDBOX_ROOT={cfg['sandbox_root']}", str(cfg['sandbox_run']), '--gui', '--audio']
    if how in ('exe', 'wine'):
        return base + ['--wine', str(folder), 'wine', rel]
    if how == 'java':
        return base + [str(folder), 'java', '-jar', rel]
    if how == 'appimage':
        return base + [str(folder), './' + rel, '--appimage-extract-and-run']
    return base + [str(folder), './' + rel]


def terminal_command(cfg, argv):
    """Run argv in a new terminal window that stays open."""
    if system.WINDOWS:
        cmd = subprocess.list2cmdline(argv)
        term = cfg.get('terminal') or ('wt' if shutil.which('wt') else '')
        if term == 'wt':
            return ['wt', 'cmd', '/k', cmd]
        return ['cmd', '/k', cmd]          # started with CREATE_NEW_CONSOLE, so it gets its own window
    import shlex
    cmd = ' '.join(shlex.quote(a) for a in argv) + '; echo; read -p "[done, Enter to close] " _'
    term = cfg['terminal'] or os.environ.get('TERMINAL') or next(
        (t for t in ('kitty', 'foot', 'alacritty', 'wezterm', 'konsole', 'gnome-terminal', 'xterm') if shutil.which(t)), 'xterm')
    if 'gnome-terminal' in term:
        return [term, '--', 'bash', '-c', cmd]
    if 'wezterm' in term:
        return [term, 'start', '--', 'bash', '-c', cmd]
    return [term, '-e', 'bash', '-c', cmd]


# ---- video ----

def ffmpeg_exe():
    """ffmpeg: the one next to terra-desk.exe or on PATH, else the copy bundled with imageio-ffmpeg."""
    found = shutil.which('ffmpeg')
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError('ffmpeg is not installed (the Windows installer includes it; on Linux install ffmpeg)') from e


def video_info(path):
    """(duration in seconds, size in bytes), read from ffmpeg's own banner so ffprobe isn't needed."""
    out = subprocess.run([ffmpeg_exe(), '-hide_banner', '-i', str(path)], capture_output=True, text=True,
                         encoding='utf-8', errors='replace')
    m = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', out.stderr)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    size = Path(path).stat().st_size if Path(path).exists() else 0
    return dur, size


def compress_video(src, dest, limit=VIDEO_LIMIT):
    """Re-encode to h264/aac mp4 under `limit` bytes (two-pass at a computed bitrate, max 1080p)."""
    dur, size = video_info(src)
    if dur <= 0:
        raise RuntimeError('ffprobe could not read the video length')
    if size and size <= limit and str(src).lower().endswith('.mp4'):
        shutil.copy(src, dest)
        return dest
    audio_k = 96
    total_k = (limit * 8 / 1024) / dur * 0.94          # 6% headroom for the container
    video_k = max(int(total_k - audio_k), 120)
    scale = 'scale=-2:min(ih\\,1080)' if video_k > 2500 else 'scale=-2:min(ih\\,720)' if video_k > 700 else 'scale=-2:min(ih\\,480)'
    log = Path(dest).with_suffix('.ffmpeg2pass')
    common = [ffmpeg_exe(), '-y', '-v', 'error', '-i', str(src), '-vf', scale, '-c:v', 'libx264', '-preset', 'medium',
              '-b:v', f'{video_k}k', '-maxrate', f'{int(video_k * 1.3)}k', '-bufsize', f'{video_k * 2}k',
              '-passlogfile', str(log)]
    for n, extra in ((1, ['-pass', '1', '-an', '-f', 'mp4', os.devnull]),
                     (2, ['-pass', '2', '-c:a', 'aac', '-b:a', f'{audio_k}k', '-movflags', '+faststart', str(dest)])):
        r = subprocess.run(common + extra, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if r.returncode:
            raise RuntimeError(r.stderr.strip()[-500:])
    for f in Path(dest).parent.glob(log.name + '*'):
        f.unlink(missing_ok=True)
    if Path(dest).stat().st_size > limit:
        raise RuntimeError(f'still {Path(dest).stat().st_size / 1048576:.1f} MiB after compressing, trim the video first')
    return dest


# ---- templates ----

TEMPLATE_KINDS = ('approve', 'changes', 'reject')


def template(kind, handle, cfg=None):
    """The draft a verdict starts from: yours from Settings if you edited it, else the built-in one."""
    text = prompts.get(cfg, f'template-{kind}', default_template(kind)) if cfg else default_template(kind)
    return prompts.fill(text, handle=handle)


def default_template(kind):
    end = 'if you have any questions DM {handle}'
    if kind == 'approve':
        return build_draft({
            'Verdict': 'approve',
            'Specific technical features': '(what they built, concretely: the systems, tools and tricks)',
            'Airtable deflation (hours)': '0',
            'Crop deflation (hours)': '0',
            'Multiplier': '1.1',
            'Airtable justification': '(80+ characters: what you tested and on what, commits and hours, AI use and '
                                      'whether it matches the declaration, beginner or not, whether the time makes sense)',
            'Note to player': f'(one thing they did well, one thing to improve; if you cut hours, say why)\n{end}',
        })
    if kind == 'changes':
        return build_draft({'Verdict': 'request changes',
                            'What to change': f'(one nice thing, then the 1-2 things that block it, each with how to fix it)\n'
                                              f"reship and we'll take another look!\n{end}"})
    if kind == 'reject':
        return build_draft({'Verdict': 'reject',
                            'Note to player': f'(why it can never count here, e.g. school or paid work, and where it could go instead)\n{end}'})
    raise ValueError(kind)


def set_config(cfg, key, value):
    """Change one `key = "value"` line in the config file (keeping its comments) and in cfg."""
    text = CONFIG_FILE.read_text(encoding='utf-8') if CONFIG_FILE.exists() else ''
    line = f'{key} = {json.dumps(value)}'
    text, n = re.subn(rf'^{re.escape(key)}\s*=.*$', lambda _m: line, text, count=1, flags=re.M)
    CONFIG_FILE.write_text(text if n else text.rstrip('\n') + '\n' + line + '\n', encoding='utf-8')
    cfg[key] = value


def state_file():
    """Things the app remembers that aren't settings (column widths), next to the config file."""
    return CONFIG_FILE.parent / 'state.json'


def load_state():
    try:
        return json.loads(state_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_state(**values):
    state = {**load_state(), **values}
    state_file().parent.mkdir(parents=True, exist_ok=True)
    state_file().write_text(json.dumps(state, indent=1), encoding='utf-8')
    return state


def column_widths():
    """The queue column widths you set by dragging, by column name."""
    return {RENAMED_COLUMNS.get(k, k): v for k, v in (load_state().get('column_widths') or {}).items()}


def token_state():
    """'env' (TERRA_TOKEN is set, it wins), 'file' (saved in TOKEN_FILE) or 'none'. Never the key itself."""
    if os.environ.get('TERRA_TOKEN'):
        return 'env'
    return 'file' if TOKEN_FILE.exists() and TOKEN_FILE.read_text(encoding='utf-8').strip() else 'none'


def save_token(key):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(key.strip().strip('"') + '\n')
    os.chmod(TOKEN_FILE, 0o600)


def remove_token():
    TOKEN_FILE.unlink(missing_ok=True)


def template_problems(draft):
    """Leftover (placeholder) text from a template."""
    return [line.strip() for line in draft.split('\n') if re.match(r'^\(.*\)$', line.strip())]


# ---- queueing ----

def queue_draft(cfg, tools, store, ship):
    """Check the draft against the live ship and put it in terra-write's send queue. Returns a message."""
    W = tools.W
    if not ship.draft:
        raise RuntimeError('no draft')
    if template_problems(ship.draft):
        raise RuntimeError('the draft still has template text in (parentheses)')
    if W['already_sent'](ship.sid, W['REAL_API']) or W['queued_file'](ship.sid):
        raise RuntimeError('already sent or queued')
    kind = ship.verdict_kind
    priority = kind == 'approve'
    video = ship.video if kind == 'approve' and ship.video and Path(ship.video).exists() else None
    a = SimpleNamespace(video=video, no_video=video is None, json=False, send=False, queue=True, offline=False,
                        api=None, priority=priority)
    d = tools.api().get(f'/review/shipment/{ship.sid}')
    store.set_page(ship.sid, d)
    draft = slack_draft(ship.draft)
    verdict, steps, _, warns = W['build'](W['parse_drafts'](draft), d, a)
    probs = W['ship_problems'](d, verdict)
    mine = re.sub(r'\s+', ' ', slack_markup(ship.fields.get('note to player') or ship.fields.get('what to change') or ''))[:120].strip()
    for x in d.get('decisions') or []:
        if mine and mine[:80] in re.sub(r'\s+', ' ', x.get('noteToMaker') or ''):
            probs.append(f"this exact note was already sent ({x.get('decision')}, {(x.get('claimedAt') or '')[:10]})")
            break
    if probs:
        raise RuntimeError('; '.join(probs))
    b = {'sid': ship.sid, 'title': f'{ship.title} ({ship.username})' if ship.username else ship.title,
         'num': ship.num or None, 'text': draft}
    with contextlib.redirect_stdout(io.StringIO()):
        W['enqueue']({'b': b, 'sid': ship.sid, 'verdict': verdict, 'steps': steps, 'd': d}, priority=priority)
    return f'queued {verdict} for {ship.title}{" (priority)" if priority else ""}{" with video" if video else ""}' + (
        f'. warnings: {"; ".join(warns)}' if warns else '')


def unqueue(cfg, ship):
    f = queued_sids(cfg).get(ship.sid)
    if f and f.exists():
        f.unlink()
        return True
    return False


def open_store(cfg):
    return Store(cfg['workspace'] / 'terra-desk.db')


# ---- automatic checks ----

def ship_repo(F):
    """The clone prep made, checked out at the ship commit when it could make that worktree."""
    return (F.get('ship_commit') or {}).get('worktree') or (F.get('repo') or {}).get('dir')


def run_checks(cfg, tools, store, sid):
    """prep (read-only clone + lookups, no download) and the review-page data -> checks, saved in the store."""
    from . import checks
    F = prep(cfg, tools, sid, download=False)
    page = fetch_page(store, tools, sid)
    demo = (F.get('ship') or {}).get('demo_url') or ((page or {}).get('project') or {}).get('demoUrl') or ''
    if 'itch.io' in demo.lower():
        try:
            F['itch'] = checks.itch_facts(fetch_html(demo))
        except (OSError, ValueError) as e:
            F['itch'] = {'error': str(e)[:120]}
    res = checks.analyze(F, page)
    repo = ship_repo(F)
    if repo and not store.work(sid).get('repo_dir'):
        store.set_work(sid, repo_dir=repo)
    rel = store.work(sid).get('release_dir')
    if rel:
        res['run'] += [{**x, 'binary': True} for x in checks.binary_problems(rel)]
        if any(x['level'] == 'bad' for x in res['run']):
            res['level'] = 'bad'
    store.set_checks(sid, res)
    return res


def add_binary_checks(store, sid, folder):
    from . import checks
    res, _ = store.checks(sid)
    if res is None:
        return None
    res['run'] = [x for x in res['run'] if not x.get('binary')] + [{**x, 'binary': True} for x in checks.binary_problems(folder)]
    if any(x['level'] == 'bad' for x in res['run']):
        res['level'] = 'bad'
    store.set_checks(sid, res)
    return res


# ---- images ----

def fetch_html(url, limit=3 * 1024 * 1024):
    """A public web page's HTML (GET, nothing sent but a User-Agent)."""
    req = urllib.request.Request(url, headers={'User-Agent': 'terra-desk (review helper)'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(limit).decode('utf-8', 'replace')


def image_urls(page):
    out = []
    p = (page or {}).get('project') or {}
    if p.get('screenshotUrl'):
        out.append(p['screenshotUrl'])
    for d in (page or {}).get('devlogs') or []:
        for img in d.get('images') or []:
            u = img.get('url')
            if u:
                out.append(u)
    return [u for u in out if u.startswith('https://')]


def image_path(cfg, url):
    import hashlib
    ext = re.search(r'\.(png|jpe?g|gif|webp)(?:$|\?)', url.lower())
    return cfg['workspace'] / 'images' / (hashlib.sha256(url.encode()).hexdigest()[:24] + '.' + (ext.group(1) if ext else 'img'))


def fetch_images(cfg, urls):
    """Download images (screenshot, devlog images) into the workspace. Returns {url: path} for the ones on disk."""
    limit = 15 * 1024 * 1024
    out = {}
    for u in urls:
        dest = image_path(cfg, u)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                req = urllib.request.Request(u, headers={'User-Agent': 'terra-desk (review helper)'})
                with urllib.request.urlopen(req, timeout=20) as r:
                    if not (r.headers.get('Content-Type') or '').startswith('image/'):
                        continue
                    data = r.read(limit + 1)
                if len(data) > limit:
                    continue
                dest.write_bytes(data)
            except Exception:  # noqa: BLE001 - a missing image just shows as a link
                continue
        out[u] = dest
    return out


# ---- claims (only when you press Claim) ----

def _writer(tools):
    W = tools.W
    return W['Writer'](W['REAL_API'], tools.api().token, True)


def claim(tools, sid):
    """Claim the ship for review, like opening its review page does. Logged to terra-write's writes.jsonl."""
    return _writer(tools).send('claim', 'POST', f'/review/shipment/{sid}/claim', {'queue': 'review', 'takeover': False})


def heartbeat(tools, sid):
    """Keep a claim alive (the review page does this every 60 s). Returns False if the claim was lost."""
    ans = _writer(tools).send('heartbeat', 'POST', f'/review/shipment/{sid}/claim/heartbeat', {'queue': 'review'})
    return (ans or {}).get('held') is not False


def release(tools, sid):
    return _writer(tools).send('release', 'DELETE', f'/review/shipment/{sid}/claim', None, {'queue': 'review'})
