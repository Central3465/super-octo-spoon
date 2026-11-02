"""terra-desk's store: one SQLite file in the workspace.

ships     one row per shipment id: what the queue says, the review-page data, and an imported page (if any)
work      what you've done on it: notes, the draft, the video, where Set up put things, skipped
Sent and queued state isn't stored here: it comes from terra-write's send queue and log, which are the truth.
"""
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
create table if not exists ships (
    sid text primary key,
    title text, username text, week integer, queued_at text, seconds integer, platforms text,
    queue_item text,            -- the row from the review queue (json), null once it left the queue
    in_queue integer default 0,
    page_json text,             -- GET /review/shipment/<id> (json)
    page_fetched real,
    source_path text,           -- an imported markdown page, if any
    source_mtime real,
    page_text text,             -- that page without its Drafts section (shown on the Page tab)
    test_steps text,            -- test steps from that page
    suggested text,             -- a suggested draft block from that page
    num integer                 -- the page's number, if it had one
);
create table if not exists work (
    sid text primary key,
    notes text default '',
    draft text,                 -- the draft block (### headings), null if none
    video text,                 -- path of the compressed video to upload with an approve
    release_dir text,
    repo_dir text,
    skipped integer default 0,
    updated real
);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute('pragma table_info(ships)')}
        for col, kind in (('checks', 'text'), ('checks_at', 'real')):
            if col not in cols:
                self.db.execute(f'alter table ships add column {col} {kind}')
        if 'remarks' not in {r[1] for r in self.db.execute('pragma table_info(work)')}:
            self.db.execute('alter table work add column remarks text')     # notes a plugin leaves under the draft
        self.db.execute('drop table if exists stash')     # per-ship stashes, replaced by whole-database ones
        self.db.execute('pragma journal_mode=wal')
        self.db.commit()

    # ---- the queue ----
    def sync_queue(self, items):
        """items: {sid: queue row}. Marks everything else as no longer in the queue."""
        with self.db:
            self.db.execute('update ships set in_queue = 0')
            for sid, i in items.items():
                self.db.execute(
                    """insert into ships (sid, title, username, week, queued_at, seconds, platforms, queue_item, in_queue)
                       values (?, ?, ?, ?, ?, ?, ?, ?, 1)
                       on conflict(sid) do update set title=excluded.title, username=excluded.username, week=excluded.week,
                         queued_at=excluded.queued_at, seconds=excluded.seconds, platforms=excluded.platforms,
                         queue_item=excluded.queue_item, in_queue=1""",
                    (sid, i.get('title'), i.get('username'), i.get('weekNumber'), i.get('queuedAt'), i.get('seconds'),
                     ' '.join(i.get('platforms') or []), json.dumps(i)))

    # ---- review page data ----
    def page(self, sid, max_age=None):
        r = self.db.execute('select page_json, page_fetched from ships where sid = ?', (sid,)).fetchone()
        if not r or not r['page_json']:
            return None
        if max_age is not None and time.time() - (r['page_fetched'] or 0) > max_age:
            return None
        return json.loads(r['page_json'])

    def set_page(self, sid, data):
        s, mk = data.get('shipment') or {}, data.get('maker') or {}
        with self.db:
            self.db.execute('insert into ships (sid) values (?) on conflict(sid) do nothing', (sid,))
            self.db.execute("""update ships set page_json = ?, page_fetched = ?,
                               title = coalesce(title, ?), username = coalesce(username, ?), week = coalesce(week, ?)
                               where sid = ?""",
                            (json.dumps(data), time.time(), s.get('title'), mk.get('username'), s.get('weekNumber'), sid))

    # ---- imported pages ----
    def import_page(self, sid, path, mtime, page_text, test_steps, suggested, num, title, week, notes, draft):
        """Refresh what the page says; notes and draft only come in when you haven't written your own yet."""
        with self.db:
            self.db.execute('insert into ships (sid) values (?) on conflict(sid) do nothing', (sid,))
            self.db.execute("""update ships set source_path = ?, source_mtime = ?, page_text = ?, test_steps = ?,
                               suggested = ?, num = ?, title = coalesce(title, ?), week = coalesce(week, ?) where sid = ?""",
                            (str(path), mtime, page_text, test_steps, suggested, num, title, week, sid))
            self.db.execute('insert into work (sid, updated) values (?, ?) on conflict(sid) do nothing', (sid, time.time()))
            if notes:
                self.db.execute("update work set notes = ? where sid = ? and coalesce(notes, '') = ''", (notes, sid))
            if draft:
                self.db.execute('update work set draft = ? where sid = ? and draft is null', (draft, sid))

    def set_test_steps(self, sid, text):
        with self.db:
            self.db.execute('insert into ships (sid) values (?) on conflict(sid) do nothing', (sid,))
            self.db.execute('update ships set test_steps = ? where sid = ?', (text, sid))

    def source_mtimes(self):
        return {r['source_path']: r['source_mtime'] for r in self.db.execute('select source_path, source_mtime from ships where source_path is not null')}

    # ---- your work ----
    def work(self, sid):
        r = self.db.execute('select * from work where sid = ?', (sid,)).fetchone()
        return dict(r) if r else {'sid': sid, 'notes': '', 'draft': None, 'video': None, 'release_dir': None,
                                  'repo_dir': None, 'skipped': 0}

    def set_work(self, sid, **fields):
        cols = ', '.join(f'{k} = ?' for k in fields)
        with self.db:
            self.db.execute('insert into work (sid, updated) values (?, ?) on conflict(sid) do nothing', (sid, time.time()))
            self.db.execute(f'update work set {cols}, updated = ? where sid = ?', (*fields.values(), time.time(), sid))

    # ---- automatic checks ----
    def checks(self, sid):
        r = self.db.execute('select checks, checks_at from ships where sid = ?', (sid,)).fetchone()
        return (json.loads(r['checks']), r['checks_at']) if r and r['checks'] else (None, None)

    def set_checks(self, sid, checks):
        with self.db:
            self.db.execute('insert into ships (sid) values (?) on conflict(sid) do nothing', (sid,))
            self.db.execute('update ships set checks = ?, checks_at = ? where sid = ?', (json.dumps(checks), time.time(), sid))

    # ---- stashes: the whole database put aside to start fresh, and brought back later ----
    def has_work(self):
        return self.db.execute("""select 1 from work where draft is not null or coalesce(notes, '') != ''
                                  or video is not null or release_dir is not null or skipped
                                  union all select 1 from ships where page_text is not null or test_steps is not null
                                  or suggested is not null""").fetchone() is not None

    def stash(self, folder):
        """Copy the whole database into folder, then clear your work (notes, drafts, videos, set-up folders, skips) and
        what came from imported pages (their text, test steps, suggested drafts and numbers). Which files were
        imported is kept, so they aren't imported again unless they change. Returns the stash file."""
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime('%Y-%m-%d-%H%M%S')
        path, n = folder / f'{stamp}.db', 1
        while path.exists():
            n += 1
            path = folder / f'{stamp}-{n}.db'
        dest = sqlite3.connect(str(path))
        self.db.backup(dest)
        dest.close()
        with self.db:
            self.db.execute('delete from work')
            self.db.execute('update ships set page_text = null, test_steps = null, suggested = null, num = null')
        return path

    def pop(self, path, folder):
        """Replace the database with a stash. What you have now is stashed first (if there's any work), so nothing
        is lost."""
        if self.has_work():
            self.stash(folder)
        src = sqlite3.connect(str(path))
        src.backup(self.db)
        src.close()
        Path(path).unlink()

    # ---- everything for the list ----
    def rows(self):
        return [dict(r) for r in self.db.execute(
            """select s.*, w.notes, w.draft, w.video, w.release_dir, w.repo_dir, w.remarks, coalesce(w.skipped, 0) as skipped
               from ships s left join work w on w.sid = s.sid""")]


def stashes(folder):
    """The stashes in folder, newest first: [{'path', 'created', 'drafts', 'notes', 'skipped', 'pages'}]."""
    out = []
    for f in sorted(Path(folder).glob('*.db'), key=lambda f: f.stat().st_mtime, reverse=True):
        d = sqlite3.connect(f'file:{f}?mode=ro', uri=True)
        drafts, notes, skipped = d.execute(
            "select count(draft), sum(coalesce(notes, '') != ''), sum(skipped) from work").fetchone()
        pages = d.execute('select count(page_text) from ships').fetchone()[0]
        d.close()
        out.append({'path': f, 'created': f.stat().st_mtime, 'drafts': drafts or 0, 'notes': notes or 0,
                    'skipped': skipped or 0, 'pages': pages or 0})
    return out

