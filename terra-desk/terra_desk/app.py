"""terra-desk: test and review Terra ships, straight from the review queue."""
import gc
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRect, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QKeySequence, QPainter, QPalette, QPen, QPixmap,
                           QPolygonF, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QFrame,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter, QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem, QTableWidget, QTableWidgetItem, QTabWidget,
                               QTextBrowser, QToolButton, QVBoxLayout, QWidget)

from . import core, db, plugins, review_page, settings, theme
from .mdedit import MarkdownEdit
from .terminal import TerminalPanel
from .theme import BG, BUTTER, FAINT, GREEN, LILAC, MINT, MUTED, PEACH, PINK, heading, label_font, mono

STATE_COLOR = {'drafted': GREEN, 'notes': BUTTER, 'new': MUTED, 'queued': LILAC, 'sent': FAINT, 'gone': FAINT, 'claimed': PINK}
KIND_BADGE = {'approve': ('approve', GREEN), 'changes': ('request changes', BUTTER), 'reject': ('reject (for good)', PINK),
              'skip': ('skip: staff note, post by hand', MUTED)}
VIEWS = ['queue, unclaimed', 'queue, all', 'mine, open', 'mine, drafted', 'mine, request changes', 'queued and sent',
         'skipped']
COLUMNS = core.QUEUE_COLUMNS
PLATFORMS = ['web', 'windows', 'linux', 'macos', 'other']
SORTS = ['oldest first', 'newest first', 'most hours', 'fewest hours', 'by platform', 'by week']
TEXT_FIELDS = ['note to player', 'what to change', 'airtable justification', 'specific technical features',
               'deflation reason', 'danger flag acknowledgement', 'update justification', 'staff note']
PRETTY = {'verdict': 'Verdict', 'specific technical features': 'Specific technical features',
          'airtable deflation (hours)': 'Airtable deflation (hours)', 'crop deflation (hours)': 'Crop deflation (hours)',
          'deflation reason': 'Deflation reason', 'multiplier': 'Multiplier', 'airtable justification': 'Airtable justification',
          'note to player': 'Note to player', 'what to change': 'What to change',
          'danger flag acknowledgement': 'Danger flag acknowledgement', 'update justification': 'Update justification',
          'staff note': 'Staff note'}
ORDER = ['verdict', 'specific technical features', 'airtable deflation (hours)', 'crop deflation (hours)', 'deflation reason',
         'multiplier', 'airtable justification', 'note to player', 'what to change', 'danger flag acknowledgement',
         'update justification', 'staff note']


class Bus(QObject):
    done = Signal(object, object, object)   # callback, result, error


BUS = Bus()
BUS.done.connect(lambda cb, res, err: cb(res, err))


def background(fn, callback):
    """Run fn() off the UI thread, then callback(result, error) on it."""
    def run():
        try:
            BUS.done.emit(callback, fn(), None)
        except Exception as e:  # noqa: BLE001 - shown to the user
            BUS.done.emit(callback, None, e)
    threading.Thread(target=run, daemon=True).start()


def stash_when(path):
    return time.strftime('%b %d %H:%M', time.localtime(Path(path).stat().st_mtime))


def open_url(win, u):
    if core.is_review_page(u):
        win.say("not opening Terra review pages (that would claim the ship)")
        return
    QDesktopServices.openUrl(QUrl(u))


def fields_to_draft(f):
    keys = [k for k in ORDER if k in f] + [k for k in f if k not in ORDER]
    return core.build_draft({PRETTY.get(k, k): f[k] for k in keys})


class Markdown(QTextBrowser):
    def __init__(self, win, text):
        super().__init__()
        self.setOpenLinks(False)
        self.anchorClicked.connect(lambda url: open_url(win, url.toString()))
        self.setFrameShape(QFrame.NoFrame)
        self.setMarkdown(re.sub(r'(?<![\(\[<])(https?://[^\s)>\]`"]+?)([.,]?)(?=\s|$)', r'[\1](\1)\2', text, flags=re.M))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.document().documentLayout().documentSizeChanged.connect(lambda s: self.setFixedHeight(int(s.height()) + 8))


class QueueRows(QStyledItemDelegate):
    """Cells draw only their text (black on the selected row); QueueTable draws the row highlights under them."""

    def paint(self, painter, option, index):
        table = self.parent()
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        if opt.state & QStyle.State_Selected:
            opt.palette.setColor(QPalette.Text, QColor(BG))
        opt.state &= ~(QStyle.State_Selected | QStyle.State_MouseOver | QStyle.State_HasFocus)
        table.style().drawControl(QStyle.CE_ItemViewItem, opt, painter, table)


class QueueTable(QTableWidget):
    """Columns you drag keep their width (widths, by column name; widths_changed fires after a drag). The others fit
    their contents, and the ship column takes the room that's left, never less than SHIP_MIN, unless you sized it
    yourself; past that the table scrolls sideways.

    Each row has its own pastel, like the cards on Fluxion's page: a dark wash of it under the mouse, solid when
    selected, as one cut-corner shape per row so there are no seams between cells."""
    SHIP, SHIP_MIN, CUT = 3, 220, 5
    widths_changed = Signal()

    def __init__(self, *args):
        super().__init__(*args)
        self.hover_row = -1
        self.widths, self.sizing = {}, False
        self.setMouseTracking(True)
        self.setItemDelegate(QueueRows(self))
        self.horizontalHeader().sectionResized.connect(self.resized)

    def resized(self, col, _old, new):
        if not self.sizing:
            self.widths[COLUMNS[col]] = new
            self.widths_changed.emit()

    def size_columns(self, shown):
        """Show the columns in shown, each at the width you gave it or fitted to its contents."""
        self.sizing = True
        try:
            for c, name in enumerate(COLUMNS):
                self.setColumnHidden(c, name not in shown and c != self.SHIP)
                if self.isColumnHidden(c):
                    continue
                if name in self.widths:
                    self.horizontalHeader().resizeSection(c, self.widths[name])
                elif c != self.SHIP:
                    self.resizeColumnToContents(c)
            self.fit()
        finally:
            self.sizing = False

    def set_hover(self, row):
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().update()

    def mouseMoveEvent(self, e):
        self.set_hover(self.rowAt(int(e.position().y())))
        super().mouseMoveEvent(e)

    def leaveEvent(self, e):
        self.set_hover(-1)
        super().leaveEvent(e)

    def paintEvent(self, e):
        p = QPainter(self.viewport())
        x0 = -self.horizontalOffset()
        x1 = min(self.horizontalHeader().length() + x0, self.viewport().width())
        first, last = self.rowAt(0), self.rowAt(self.viewport().height() - 1)
        last = self.rowCount() - 1 if last < 0 else last
        dots = QPen(QColor(theme.LINE), 0, Qt.DotLine)
        p.setPen(dots)
        for row in range(max(first, 0), last + 1):
            y = self.rowViewportPosition(row) + self.rowHeight(row) - 1
            p.drawLine(x0, y, x1, y)
        selected = self.selectionModel().selectedRows()
        for row, wash in [(self.hover_row, True)] + [(i.row(), False) for i in selected[:1]]:
            if row >= 0:
                tint = QColor(theme.TINTS[row % len(theme.TINTS)])
                rect = QRect(x0, self.rowViewportPosition(row), x1 - x0, self.rowHeight(row))
                theme.paint_frame(p, rect, fill=theme.shade(tint) if wash else tint, cut=self.CUT)
        p.end()
        super().paintEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.fit()

    def fit(self):
        if 'ship' in self.widths:
            return
        h = self.horizontalHeader()
        rest = sum(h.sectionSize(c) for c in range(self.columnCount()) if c != self.SHIP and not self.isColumnHidden(c))
        was, self.sizing = self.sizing, True
        h.resizeSection(self.SHIP, max(self.SHIP_MIN, self.viewport().width() - rest))
        self.sizing = was


class StayOpenMenu(QMenu):
    """A menu that stays open when you tick something in it."""

    def mouseReleaseEvent(self, e):
        a = self.activeAction()
        if a and a.isCheckable() and a.isEnabled():
            a.trigger()
            return
        super().mouseReleaseEvent(e)


class PickList(QPushButton):
    """A drop-down of tick boxes, looking like the other drop-downs. picked is the set ticked (empty means all of
    them); changed fires on every tick. presets: [(label, set)] for one-click selections."""
    changed = Signal()

    def __init__(self, items, everything, presets=()):
        super().__init__()
        self.setObjectName('pick')
        self.items, self.everything, self.picked = items, everything, set()
        menu = StayOpenMenu(self)
        self.choices = [(menu.addAction(everything), set())]
        self.choices += [(menu.addAction(label), set(chosen)) for label, chosen in presets]
        for a, chosen in self.choices:
            a.triggered.connect(lambda _=False, c=chosen: self.set_picked(c))
        menu.addSeparator()
        self.ticks = {}
        for item in items:
            self.ticks[item] = menu.addAction(item)
            self.ticks[item].triggered.connect(lambda _=False, i=item: self.set_picked(self.picked ^ {i}))
        for a in [a for a, _ in self.choices] + list(self.ticks.values()):
            a.setCheckable(True)
        menu.aboutToHide.connect(self.update)
        self.setMenu(menu)
        self.show_picked()

    def set_picked(self, picked):
        self.picked = set(picked)
        self.show_picked()
        self.changed.emit()

    def show_picked(self):
        for a, chosen in self.choices:
            a.setChecked(self.picked == chosen)
        for item, a in self.ticks.items():
            a.setChecked(item in self.picked)
        names = [i for i in self.items if i in self.picked]
        preset = next((a.text() for a, chosen in self.choices if chosen and chosen == self.picked), None)
        self.setText(self.everything if not names else preset.split(' (')[0] if preset else ', '.join(names))

    def paintEvent(self, e):
        # drawn here: Qt ignores a style sheet's padding on buttons with a menu (the frame comes from theme.Frames)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        text = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.width() - 2 * theme.PAD - 24)
        p.setPen(QColor(theme.INK))
        p.drawText(self.rect().adjusted(theme.PAD, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, text)
        x, y = self.width() - theme.PAD - theme.CUT - 4, self.height() / 2
        p.setPen(QPen(QColor(theme.MUTED), 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPolyline(QPolygonF([QPointF(x - 4, y - 2), QPointF(x, y + 2), QPointF(x + 4, y - 2)]))
        p.end()


class CodeRow(QFrame):
    def __init__(self, win, code):
        super().__init__()
        self.setObjectName('code')
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(theme.PAD, theme.PAD // 2, theme.PAD // 2, theme.PAD // 2)
        lab = QLabel(code)
        lab.setFont(mono())
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(lab, 1)
        cp = QPushButton('Copy')
        cp.clicked.connect(lambda: (QApplication.clipboard().setText(code), win.say(f'copied: {code[:70]}')))
        run = QPushButton('Run')
        run.setToolTip('run it in a new terminal window')
        run.clicked.connect(lambda: win.run_in_terminal(['bash', '-c', code], code.split('\n')[0]))
        lay.addWidget(cp, 0, Qt.AlignTop)
        lay.addWidget(run, 0, Qt.AlignTop)


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = core.load_config()
        self.tools = core.Tools(self.cfg)
        self.store = core.open_store(self.cfg)
        self.hooks, plugin_errors = plugins.load(self.cfg)
        try:
            self.tools.W   # load terra-write.py now, on the main thread
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, 'terra-desk', f'could not load terra-write.py: {e}')
        self.ships, self.by_key, self.cur, self.page_json, self.edits = [], {}, None, None, {}
        self.check_queue, self.checking, self.images = [], None, {}
        self.dumping = False
        self.panel_shown = None
        self.claimed = None   # the ship id you claimed with the Claim button
        self.setWindowTitle('terra-desk')
        self.resize(1600, 950)
        self.build()
        for err in plugin_errors:
            self.say(f'plugin failed to load: {err}', 20000)
        for doc in self.cfg.get('documents') or []:
            path = Path(doc).expanduser()
            self.hooks.prompts.setdefault(path.name, {'path': path, 'help': f'Your document {path} (edited in place).'})
        core.import_pages(self.cfg, self.store, self.hooks)
        self.reload()
        self.refetch()
        for ms, fn in ((10000, self.refresh_status), (120000, self.refetch), (30000, self.reimport), (55000, self.beat)):
            t = QTimer(self)
            t.timeout.connect(fn)
            t.start(ms)

    # ---- layout ----
    def build(self):
        tb = self.addToolBar('main')
        tb.setMovable(False)
        for label, fn, key in (('Refresh queue', self.refetch, 'Ctrl+R'), ('Reload', self.reimport, 'F5'),
                               ('Check shown ships', self.check_shown, 'Ctrl+Shift+C')):
            a = QAction(label, self)
            a.triggered.connect(lambda _=False, f=fn: f())
            a.setShortcut(QKeySequence(key))
            tb.addAction(a)
        stash_btn = QToolButton(text='Stash ▾', popupMode=QToolButton.InstantPopup)
        stash_btn.setToolTip('put all your work aside to start fresh, or bring a stash back')
        sm = QMenu(stash_btn)
        sm.aboutToShow.connect(lambda m=sm: self.fill_stash_menu(m))
        stash_btn.setMenu(sm)
        tb.addWidget(stash_btn)
        dump = QAction('Dump queue', self)
        dump.setToolTip('write every waiting ship to a markdown file and cache their review pages (read-only, ~12 min)')
        dump.triggered.connect(self.dump_queue)
        tb.addAction(dump)
        term = QAction('Terminal', self)
        term.setShortcut(QKeySequence('Ctrl+`'))
        term.triggered.connect(lambda: self.terminal.setVisible(not self.terminal.isVisible()))
        tb.addAction(term)
        a = QAction('Settings…', self)
        a.triggered.connect(lambda: settings.Settings(self).exec())
        a.setShortcut(QKeySequence('Ctrl+,'))
        tb.addAction(a)
        self.status = QLabel()
        tb.addSeparator()
        tb.addWidget(self.status)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.view = QComboBox()
        self.view.addItems(VIEWS)
        self.view.currentTextChanged.connect(lambda _: self.fill())
        self.week = QComboBox()
        self.week.addItems(['all weeks', 'Prep Week 3', 'Week 1', 'Week 2', 'Week 3'])
        self.week.currentTextChanged.connect(lambda _: self.fill())
        row.addWidget(self.view)
        row.addWidget(self.week)
        ll.addLayout(row)
        row = QHBoxLayout()
        self.platform = PickList(PLATFORMS, 'all platforms', [('runs here (not macOS-only)', set(PLATFORMS) - {'macos'})])
        self.platform.setToolTip('only ships that run on any of the ticked platforms')
        self.platform.changed.connect(self.fill)
        self.sort = QComboBox()
        self.sort.addItems(SORTS)
        self.sort.currentTextChanged.connect(lambda _: self.fill())
        row.addWidget(self.platform)
        row.addWidget(self.sort)
        ll.addLayout(row)
        self.search = QLineEdit(placeholderText='search title, player or number')
        self.search.textChanged.connect(lambda _: self.fill())
        ll.addWidget(self.search)
        self.table = QueueTable(0, len(COLUMNS))
        self.table.widths = core.column_widths()
        self.widths_timer = QTimer(self, singleShot=True, interval=500)
        self.widths_timer.timeout.connect(lambda: core.save_state(column_widths=self.table.widths))
        self.table.widths_changed.connect(self.widths_timer.start)
        self.table.setHorizontalHeaderLabels([c.upper() for c in COLUMNS])
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.currentCellChanged.connect(lambda r, *_: self.show_row(r))
        self.count = QLabel()
        self.count.setStyleSheet(f'color:{MUTED}')
        ll.addWidget(self.count)
        ll.addWidget(self.table)

        self.page = QTextBrowser()
        self.page.setOpenLinks(False)
        self.page.anchorClicked.connect(lambda url: open_url(self, url.toString()))
        self.steps_area = QScrollArea()
        self.steps_area.setWidgetResizable(True)
        self.facts = QTextBrowser()
        self.facts.setOpenLinks(False)
        self.facts.anchorClicked.connect(lambda url: open_url(self, url.toString()))
        self.tabs = QTabWidget()
        self.tabs.addTab(self.page, 'REVIEW PAGE')
        self.tabs.addTab(self.steps_area, 'TEST')
        self.tabs.addTab(self.facts, 'PAGE')
        self.tabs.tabBar().setDrawBase(False)

        self.notes = MarkdownEdit(placeholderText='your test notes (saved as you type)', open_link=lambda u: open_url(self, u))
        self.notes_timer = QTimer(self, singleShot=True, interval=800)
        self.notes_timer.timeout.connect(self.save_notes)
        self._loading = False
        self.notes.textChanged.connect(lambda: self.notes_timer.start() if self.cur and not self._loading else None)
        self.draft_area = QScrollArea()
        self.draft_area.setWidgetResizable(True)
        self.draft_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        notes_box = QFrame()
        nl = QVBoxLayout(notes_box)
        nl.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        notes_head = heading('Your notes', PINK)
        notes_head.setFixedHeight(theme.H)
        head.addWidget(notes_head)
        head.addStretch()
        nl.addLayout(head)
        nl.addWidget(self.notes)
        right = QSplitter(Qt.Vertical)
        right.addWidget(notes_box)
        right.addWidget(self.box('Decision', self.draft_area))
        right.setSizes([200, 720])
        self.terminal = TerminalPanel(self.open_outside, self.say)
        self.terminal.hide()
        middle = QSplitter(Qt.Vertical)
        middle.addWidget(self.tabs)
        middle.addWidget(self.terminal)
        middle.setSizes([560, 380])
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(middle)
        split.addWidget(right)
        split.setSizes([480, 640, 480])
        for s in (split, middle, right):
            s.setHandleWidth(theme.GAP * 2)
        split.setContentsMargins(theme.GAP, 0, theme.GAP, theme.GAP)
        self.setCentralWidget(split)
        self.statusBar()
        for key, fn in (('Ctrl+S', self.save_notes), ('Ctrl+Return', self.queue), ('J', lambda: self.move(1)),
                        ('K', lambda: self.move(-1))):
            QShortcut(QKeySequence(key), self, activated=fn)

    @staticmethod
    def box(title, w):
        f = QFrame()
        lay = QVBoxLayout(f)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(heading(title, LILAC))
        lay.addWidget(w)
        return f

    def say(self, msg, ms=8000):
        self.statusBar().showMessage(msg, ms)

    # ---- data ----
    def reload(self, keep=None):
        keep = keep or (self.cur.key if self.cur else None)
        self.flush_notes()          # before reading the store, or the notes you just typed get read back as older text
        self.ships = core.build_ships(self.cfg, self.store, self.hooks)
        self.by_key = {s.key: s for s in self.ships}
        self.fill(keep)
        self.refresh_status()

    def reimport(self):
        if core.import_pages(self.cfg, self.store, self.hooks):
            self.reload()
        else:
            self.refresh_status()

    def refetch(self):
        self.say('refreshing the queue…')
        background(lambda: core.terra_waiting(self.tools), self.got_queue)

    def got_queue(self, res, err):
        if err:
            self.say(f'could not load the queue: {err}')
            return
        self.store.sync_queue(res)
        self.reload()
        self.say(f'{len(res)} ships in the queue')

    def mine_label(self, s):
        if s.state == 'drafted':
            return {'approve': 'approve', 'changes': 'changes', 'reject': 'reject', 'skip': 'skip note'}.get(s.verdict_kind, 'drafted')
        if s.state == 'new':
            return 'suggested' if s.suggested else 'page' if s.page_text else 'steps' if s.quick else ''
        return s.state

    def mine_color(self, s, mine):
        return (PINK if mine == 'reject' else BUTTER if mine == 'changes' else PEACH if mine == 'suggested'
                else MINT if mine == 'steps' else STATE_COLOR.get(s.state, MUTED))

    def visible(self):
        view, week, q = self.view.currentText(), self.week.currentText(), self.search.text().strip().lower()
        open_states = ('drafted', 'notes', 'new')
        out = []
        for s in self.ships:
            in_queue = s.item is not None
            mine = bool(s.notes.strip() or s.draft or s.page_text or s.quick)
            keep = {
                'queue, unclaimed': in_queue and s.state not in ('claimed', 'queued', 'sent') and not s.skipped,
                'queue, all': in_queue,
                'mine, open': mine and s.state in open_states and not s.skipped,
                'mine, drafted': s.state == 'drafted',
                'mine, request changes': s.state in open_states and (s.verdict_kind == 'changes' or (not s.draft and bool(s.suggested))),
                'queued and sent': s.state in ('queued', 'sent'),
                'skipped': s.skipped,
            }[view]
            if not keep or (week != 'all weeks' and s.week != week):
                continue
            plats = set((s.item or {}).get('platforms') or [])
            if self.platform.picked and not plats & self.platform.picked:
                continue
            if q and q not in s.title.lower() and q not in s.username.lower() and q != str(s.num):
                continue
            out.append(s)
        how = self.sort.currentText()
        queued = lambda s: (s.item or {}).get('queuedAt') or ''          # noqa: E731
        hours = lambda s: (s.item or {}).get('seconds') or 0             # noqa: E731
        week = lambda s: core.WEEK_ORDER.get(s.week, 9)                  # noqa: E731
        plat = lambda s: ' '.join(sorted((s.item or {}).get('platforms') or ['~']))  # noqa: E731
        if not view.startswith('queue') and how == 'oldest first':
            out.sort(key=lambda s: (core.STATE_ORDER.get(s.state, 9), week(s), s.num, s.title))
        elif how == 'oldest first':
            out.sort(key=lambda s: (week(s), queued(s)))
        elif how == 'newest first':
            out.sort(key=queued, reverse=True)
        elif how == 'most hours':
            out.sort(key=hours, reverse=True)
        elif how == 'fewest hours':
            out.sort(key=hours)
        elif how == 'by platform':
            out.sort(key=lambda s: (plat(s), week(s), queued(s)))
        elif how == 'by week':
            out.sort(key=lambda s: (week(s), queued(s)))
        return out

    def fill(self, keep=None):
        keep = keep or (self.cur.key if self.cur else None)
        vis = self.visible()
        self.table.blockSignals(True)
        self.table.setRowCount(len(vis))
        row_keep = 0
        for r, s in enumerate(vis):
            item = s.item or {}
            mine = self.mine_label(s)
            chk = self.check_cell(s)
            cells = [mine, chk, s.week.replace('Prep Week', 'PW'), (f'{s.num} ' if s.num else '') + s.title, s.username,
                     f"{(item.get('seconds') or 0) / 3600:.1f}h" if item else '',
                     review_page.ago(item.get('queuedAt')) if item else '', ' '.join(item.get('platforms') or [])]
            for c, txt in enumerate(cells):
                it = QTableWidgetItem(txt)
                it.setData(Qt.UserRole, s.key)
                if c == 0:
                    it.setForeground(QColor(self.mine_color(s, mine)))
                if c == 1:
                    it.setForeground(QColor(self.check_color(s)))
                    it.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(r, c, it)
            if s.key == keep:
                row_keep = r
        self.table.size_columns(self.cfg['queue_columns'])
        self.table.blockSignals(False)
        in_queue = sum(1 for s in self.ships if s.item)
        self.count.setText(f'{len(vis)} shown · {in_queue} in the queue')
        if vis:
            self.table.setCurrentCell(row_keep, 0)
            self.show_row(row_keep)
        else:
            self.show_ship(None)

    def refresh_status(self):
        drafted = sum(1 for s in self.ships if s.state == 'drafted')
        notes = sum(1 for s in self.ships if s.state == 'notes')
        q = len(core.queued_sids(self.cfg))
        run = f'<span style="color:{GREEN}">runner on</span>' if core.runner_alive(self.cfg) else f'<span style="color:{PINK}">runner off</span>'
        self.status.setText(f'  drafted {drafted} · notes {notes}  |  {q} in send queue, {run}')

    def move(self, d):
        if self.notes.hasFocus():
            return
        self.table.setCurrentCell(max(0, min(self.table.rowCount() - 1, self.table.currentRow() + d)), 0)

    def show_row(self, r):
        it = self.table.item(r, 0) if r >= 0 else None
        self.show_ship(self.by_key.get(it.data(Qt.UserRole)) if it else None)

    def refresh_cur(self):
        """Re-read the current ship from the store (after saving), keeping the list position."""
        if self.cur:
            self.reload(self.cur.key)

    # ---- one ship ----
    def flush_notes(self):
        if self.cur and self.notes_timer.isActive():
            self.notes_timer.stop()
            self.save_notes()

    def pending_edits(self):
        """Draft fields changed but not saved yet, so a rebuilt panel can put them back."""
        out = {}
        for k, e in self.edits.items():
            now = e.toPlainText() if isinstance(e, QPlainTextEdit) else e.value()
            if now != e.property('saved'):
                out[k] = now
        return out

    def panel_key(self, s):
        """What the Test and Decision panels show; they're only rebuilt when this changes."""
        if not s:
            return None
        item = s.item or {}
        return (s.sid, s.draft, s.remarks, s.state, s.video, s.skipped, s.suggested, s.quick, s.release_dir, self.claimed,
                item.get('claimedBy'), item.get('claimedByMe'))

    def show_ship(self, s):
        same = bool(s and self.cur and s.key == self.cur.key)
        self.flush_notes()
        pending = self.pending_edits() if same else {}
        self.cur = s
        self.page_json = self.store.page(s.sid) if s else None
        if not same or self.notes.toPlainText() != s.notes:     # a refresh of the same ship leaves your typing alone
            self._loading = True
            self.notes.setPlainText(s.notes if s else '')
            self._loading = False
        key = self.panel_key(s)
        if not same or key != self.panel_shown:
            self.panel_shown = key
            self.steps_area.setWidget(self.steps_widget(s))
            self.draft_area.setWidget(self.draft_widget(s))
            for k, v in pending.items():
                e = self.edits.get(k)
                if isinstance(e, QPlainTextEdit):
                    e.setPlainText(v)
                elif e is not None:
                    e.setValue(v)
        self.facts.setMarkdown(re.sub(r'^# .*\n', '', s.page_text) if s and s.page_text else
                               '_No imported page for this ship._')
        self.load_page(s)
        if s and s.item:
            chk, at = self.store.checks(s.sid)
            if chk is None or time.time() - (at or 0) > 12 * 3600:
                self.enqueue_check(s.sid, front=True)

    def load_page(self, s):
        if not s:
            self.page.setHtml('')
            return
        if self.page_json:
            self.render_page()
        else:
            self.page.setHtml(f'<p style="color:{MUTED}">loading {s.title} from Terra (read-only)…</p>')
        key = s.key
        background(lambda: core.fetch_page(self.store, self.tools, s.sid), lambda res, err: self.got_page(key, res, err))

    def got_page(self, key, res, err):
        if not self.cur or self.cur.key != key:
            return
        if err:
            if not self.page_json:
                self.page.setHtml(f'<p>could not load the ship: {err}</p>')
            return
        fresh = self.page_json is None
        self.page_json = res
        self.render_page()
        if fresh:
            self.draft_area.setWidget(self.draft_widget(self.cur))
        urls = [u for u in core.image_urls(res) if u not in self.images]
        if urls:
            background(lambda: core.fetch_images(self.cfg, urls), lambda got, err: self.got_images(key, got, err))

    # ---- checks and images ----
    def check_level(self, s):
        if self.checking == s.sid or s.sid in self.check_queue:
            return 'running'
        chk, _ = self.store.checks(s.sid)
        return chk.get('level') if chk else None

    def check_cell(self, s):
        lvl = self.check_level(s)
        return '…' if lvl == 'running' else review_page.ICON[lvl][0] if lvl in review_page.ICON else ''

    def check_color(self, s):
        return review_page.ICON.get(self.check_level(s), ('', MUTED))[1]

    def fraud_hours(self):
        fs = (self.page_json or {}).get('fraudSuggestion') or {}
        return round((fs.get('seconds') or 0) / 3600, 2)

    def fraud_cut(self, cut):
        """Is this cut fraud's suggested one?"""
        return bool(self.fraud_hours()) and abs(cut - self.fraud_hours()) < 0.01

    def fraud_reason(self):
        reason = ((self.page_json or {}).get('fraudSuggestion') or {}).get('reason')
        return f'Fraud review: {reason}' if reason else ''

    def demo_url(self):
        return ((self.page_json or {}).get('project') or {}).get('demoUrl')

    def render_page(self):
        s = self.cur
        if not s or not self.page_json:
            return
        chk, _ = self.store.checks(s.sid)
        bar = self.page.verticalScrollBar().value()
        self.page.setHtml(review_page.render(self.page_json, chk, {u: str(p) for u, p in self.images.items()},
                                             checking=self.check_level(s) == 'running'))
        self.page.verticalScrollBar().setValue(bar)

    def got_images(self, key, got, err):
        if got:
            self.images.update(got)
        if self.cur and self.cur.key == key:
            self.render_page()

    def enqueue_check(self, sid, front=False):
        if sid == self.checking or sid in self.check_queue:
            return
        if front:
            self.check_queue.insert(0, sid)
        else:
            self.check_queue.append(sid)
        self.update_check_cell(sid)
        self.next_check()

    def next_check(self):
        if self.checking or not self.check_queue:
            return
        sid = self.check_queue.pop(0)
        self.checking = sid
        left = len(self.check_queue)
        self.say(f'checking {self.by_key[sid].title if sid in self.by_key else sid}' + (f' ({left} more queued)' if left else ''), 60000)
        background(lambda: core.run_checks(self.cfg, self.tools, self.store, sid), lambda res, err: self.got_checks(sid, res, err))

    def got_checks(self, sid, res, err):
        self.checking = None
        if err:
            self.say(f'checks failed: {str(err)[:200]}', 12000)
        self.update_check_cell(sid)
        if self.cur and self.cur.sid == sid:
            self.render_page()
        if not self.check_queue:
            self.say('checks done', 5000)
        self.next_check()

    def update_check_cell(self, sid):
        s = self.by_key.get(sid)
        if not s:
            return
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 1)
            if it and it.data(Qt.UserRole) == sid:
                it.setText(self.check_cell(s))
                it.setForeground(QColor(self.check_color(s)))
                break

    def check_shown(self):
        vis = [s for s in self.visible() if s.item and self.store.checks(s.sid)[0] is None]
        if not vis:
            self.say('every shown ship is already checked')
            return
        if len(vis) > 25 and QMessageBox.question(self, 'Check shown ships',
                                                  f'Check {len(vis)} ships? Each one clones its repo into the workspace '
                                                  '(read-only, nothing runs), one at a time.') != QMessageBox.Yes:
            return
        for s in vis:
            self.enqueue_check(s.sid)

    def steps_widget(self, s):
        w = QWidget()
        lay = QVBoxLayout(w)
        if not s:
            lay.addWidget(QLabel('nothing selected'))
            return w
        head = QLabel(f'<b>{(str(s.num) + " ") if s.num else ""}{s.title}</b> &nbsp; <span style="color:{MUTED}">{s.week}</span>')
        head.setWordWrap(True)
        lay.addWidget(head)
        btns = QHBoxLayout()
        setup = QPushButton('Set up (clone + download)')
        setup.setToolTip('clone the repo at the ship commit and download the release into the workspace; nothing runs')
        setup.clicked.connect(self.setup_ship)
        runb = QToolButton(text='Run ▾', popupMode=QToolButton.InstantPopup)
        menu = QMenu(runb)
        menu.aboutToShow.connect(lambda m=menu: self.fill_run_menu(m))
        runb.setMenu(menu)
        btns.addWidget(setup)
        btns.addWidget(runb)
        for label, fn in self.hooks.test_actions:
            extra = QPushButton(label)
            extra.clicked.connect(lambda _=False, f=fn: f(self, self.cur))
            btns.addWidget(extra)
        if s.release_dir:
            folder = QPushButton('Open release folder')
            folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(s.release_dir)))
            btns.addWidget(folder)
        btns.addStretch()
        lay.addLayout(btns)
        demo = self.demo_url()
        text = s.quick or ('Press **Set up** to clone the repo and download the release (nothing runs), then use **Run**.'
                           + (f'\n\nDemo: {demo}' if demo else ''))
        pos = 0
        for m in re.finditer(r'^```[^\n]*\n(.*?)^```[ \t]*$', text, re.S | re.M):
            before = text[pos:m.start()].strip()
            if before:
                lay.addWidget(Markdown(self, before))
            code = m.group(1).rstrip('\n')
            lines = [l.strip() for l in code.splitlines() if l.strip()]
            lay.addWidget(Markdown(self, '  \n'.join(lines)) if lines and all(core.URL.fullmatch(l) for l in lines)
                          else CodeRow(self, code))
            pos = m.end()
        rest = text[pos:].strip()
        if rest:
            lay.addWidget(Markdown(self, rest))
        shots = self.screenshots(s)
        if shots:
            lay.addWidget(QLabel(f'<span style="color:{MUTED}">{len(shots)} screenshots (click to open)</span>'))
            strip = QHBoxLayout()
            for p in shots[:8]:
                lab = QLabel()
                lab.setPixmap(QPixmap(str(p)).scaledToHeight(110, Qt.SmoothTransformation))
                lab.setToolTip(p.name)
                lab.setCursor(Qt.PointingHandCursor)
                lab.mousePressEvent = lambda _e, p=p: QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
                strip.addWidget(lab)
            strip.addStretch()
            lay.addLayout(strip)
        lay.addStretch()
        return w

    def screenshots(self, s):
        dirs = [self.cfg['workspace'] / 'screenshots' / s.sid]
        for fn in self.hooks.screenshot_dirs:
            dirs += fn(s) or []
        return [p for d in dirs if Path(d).is_dir() for p in sorted(Path(d).glob('*.png'))]

    def draft_widget(self, s):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.edits = {}
        if not s:
            return w
        crow = QHBoxLayout()
        item = s.item or {}
        if self.claimed == s.sid:
            lab = QLabel(f'<b style="color:{GREEN}">● claimed by you</b>')
            rel = QPushButton('Release claim')
            rel.clicked.connect(self.release_claim)
            crow.addWidget(lab)
            crow.addWidget(rel)
        elif item.get('claimedBy') and not item.get('claimedByMe'):
            crow.addWidget(QLabel(f'<b style="color:{PINK}">claimed by {item.get("claimedBy")}</b>'))
        elif s.item:
            cb = QPushButton('Claim')
            cb.setToolTip('claim this ship on Terra (like opening its review page); it stays claimed while terra-desk is open')
            cb.clicked.connect(self.claim_cur)
            crow.addWidget(cb)
            if item.get('claimedByMe'):
                crow.addWidget(QLabel(f'<span style="color:{BUTTER}">Terra says you already hold a claim on it</span>'))
        crow.addStretch()
        skip = QPushButton('Unskip' if s.skipped else 'Skip')
        skip.setToolTip('put it back in the queue list' if s.skipped else 'hide it from the queue list (it stays under "skipped")')
        skip.clicked.connect(self.skip)
        crow.addWidget(skip)
        lay.addLayout(crow)
        vrow = QHBoxLayout()
        for kind, label, tint in (('approve', 'Approve', 'green'), ('changes', 'Request changes', 'butter'),
                                  ('reject', 'Reject', 'pink')):
            btn = QPushButton(label)
            btn.setProperty('tint', tint)
            btn.setProperty('on', bool(s.draft) and s.verdict_kind == kind)
            btn.clicked.connect(lambda _=False, k=kind: self.switch_verdict(k))
            vrow.addWidget(btn)
        vrow.addStretch()
        lay.addLayout(vrow)
        if not s.draft:
            if s.suggested:
                lay.addWidget(QLabel(f'<b style="color:{PEACH}">Suggested draft</b> for this ship:'))
                sf = core.draft_fields(s.suggested)
                lay.addWidget(Markdown(self, sf.get('what to change') or sf.get('note to player') or ''))
                use = QPushButton('Use the suggested draft')
                use.clicked.connect(lambda: self.set_draft(s.suggested))
                lay.addWidget(use)
            lay.addWidget(QLabel('No draft yet. Pick a verdict above to start from its template.'))
            self.add_actions(lay, ('no-draft', 'always'))
            lay.addStretch()
            return w

        f, kind = dict(s.fields), s.verdict_kind
        written = re.search(r'\d+(?:\.\d+)?', f.get('airtable deflation (hours)') or '')
        if kind == 'approve' and self.fraud_cut(float(written.group()) if written else self.fraud_hours()) \
                and not (f.get('deflation reason') or '').strip():
            f['deflation reason'] = self.fraud_reason()        # what the review form fills in for fraud's cut
        text, color = KIND_BADGE.get(kind, (f.get('verdict', '?'), MUTED))
        top = QHBoxLayout()
        hints = []
        badge = QLabel(text)
        badge.setFont(label_font(9))
        badge.setStyleSheet(f'background:{color}; color:{BG}; padding:3px 8px;')
        top.addWidget(badge, 0, Qt.AlignVCenter)
        if kind == 'approve':
            m = QDoubleSpinBox(decimals=2, minimum=0.8, maximum=1.5, singleStep=0.05)
            m.setValue(float(f.get('multiplier') or 1.1))
            cut = QDoubleSpinBox(decimals=2, minimum=0, maximum=50, singleStep=0.5, suffix=' h cut')
            written = re.search(r'\d+(?:\.\d+)?', f.get('airtable deflation (hours)') or '')
            cut.setValue(float(written.group()) if written else self.fraud_hours())   # left out: fraud's cut applies
            for spin in (m, cut):
                spin.setProperty('saved', spin.value())
            top.addWidget(QLabel('x'))
            for spin in (m, cut):
                spin.setMaximumWidth(100)
                top.addWidget(spin)
            self.edits['multiplier'], self.edits['cut'] = m, cut
            sug = ((self.page_json or {}).get('votes') or {}).get('suggestedMultiplier')
            hints = ([f'voters suggest x{sug:.2f}'] if sug else []) + \
                ([f'fraud suggests {self.fraud_hours():g}h'] if self.fraud_hours() else [])
        top.addStretch()
        lay.addLayout(top)
        if kind == 'approve' and hints:                  # its own line, so the row above never gets wider than the panel
            hint = QLabel(' · '.join(hints))
            hint.setWordWrap(True)
            hint.setStyleSheet(f'color:{MUTED}')
            lay.addWidget(hint)
        for k in TEXT_FIELDS:
            if k in f:
                lab = heading(k, MUTED)
                lab.setContentsMargins(0, 6, 0, 0)
                e = MarkdownEdit(f[k], open_link=lambda u: open_url(self, u))
                e.grow(min_lines=4 if k in core.PLAYER_FIELDS else 2)
                lay.addWidget(lab)
                lay.addWidget(e)
                e.setProperty('saved', f[k])
                self.edits[k] = e
        if s.remarks:
            head = QHBoxLayout()
            head.addWidget(heading('Noticed while drafting', BUTTER))
            head.addStretch()
            clear = QPushButton('Clear')
            clear.clicked.connect(lambda: (self.store.set_work(s.sid, remarks=None), self.refresh_cur()))
            head.addWidget(clear)
            lay.addLayout(head)
            lay.addWidget(Markdown(self, s.remarks))
        if kind == 'approve':
            vr = QHBoxLayout()
            ok = s.video and Path(s.video).exists()
            self.video_label = QLabel(f'video: {Path(s.video).name} ({Path(s.video).stat().st_size / 1048576:.1f} MiB)' if ok else 'video: none')
            add = QPushButton('Attach video…')
            add.setToolTip('pick a recording of your test; it is compressed to under 20 MiB and uploaded with the approve')
            add.clicked.connect(self.attach_video)
            vr.addWidget(self.video_label)
            vr.addWidget(add)
            if s.video:
                rm = QPushButton('Remove')
                rm.clicked.connect(lambda: (self.store.set_work(s.sid, video=None), self.refresh_cur()))
                vr.addWidget(rm)
            vr.addStretch()
            lay.addLayout(vr)
        left = core.template_problems(s.draft)
        if left:
            warn = QLabel(f'<span style="color:{BUTTER}">still has template text: {left[0][:90]}</span>')
            warn.setWordWrap(True)
            lay.addWidget(warn)
        row = QHBoxLayout()
        q = QPushButton('Queue  (Ctrl+Enter)')
        q.setObjectName('primary')
        q.clicked.connect(self.queue)
        sv = QPushButton('Save')
        sv.clicked.connect(self.save_draft_edits)
        row.addWidget(q)
        row.addWidget(sv)
        row.addStretch()
        lay.addLayout(row)
        row2 = QHBoxLayout()
        for label, fn in (('Unqueue', self.unqueue), ('Delete draft', self.delete_draft)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row2.addWidget(b)
        row2.addStretch()
        lay.addLayout(row2)
        self.add_actions(lay, ('draft', 'always'))
        lay.addStretch()
        return w

    def add_actions(self, lay, whens):
        acts = [(label, fn) for label, fn, when in self.hooks.decision_actions if when in whens]
        if not acts:
            return
        row = QHBoxLayout()
        for label, fn in acts:
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, fn=fn: fn(self, self.cur))
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)

    # ---- actions ----
    def save_notes(self):
        s = self.cur
        if not s or self.notes.toPlainText() == s.notes:
            return
        s.notes = self.notes.toPlainText()
        self.store.set_work(s.sid, notes=s.notes)
        self.say('notes saved')
        if s.state in ('new', 'notes'):         # the tag comes and goes with the text
            s.state = 'notes' if s.notes.strip() else 'new'
            mine = self.mine_label(s)
            for r in range(self.table.rowCount()):
                if self.table.item(r, 0).data(Qt.UserRole) == s.key:
                    self.table.item(r, 0).setText(mine)
                    self.table.item(r, 0).setForeground(QColor(self.mine_color(s, mine)))
                    self.table.size_columns(self.cfg['queue_columns'])

    def set_draft(self, draft):
        if self.cur:
            self.store.set_work(self.cur.sid, draft=draft)
            self.refresh_cur()

    def switch_verdict(self, kind):
        s = self.cur
        if not s or (s.draft and s.verdict_kind == kind):
            return
        if s.draft and QMessageBox.question(self, 'Switch verdict', f'Replace the {s.verdict_kind} draft with a {kind} '
                                            'template? The note text carries over.') != QMessageBox.Yes:
            return
        new = core.draft_fields(core.template(kind, self.cfg['handle'], self.cfg))
        old = s.fields.get('note to player') or s.fields.get('what to change')
        if old:
            new['what to change' if kind == 'changes' else 'note to player'] = old
        dangers = [f for f in (self.page_json or {}).get('flags') or [] if f.get('severity') == 'danger' and not f.get('dismissedAt')]
        if kind == 'approve' and dangers:
            new['danger flag acknowledgement'] = s.fields.get('danger flag acknowledgement') or \
                f'(one line, why each danger flag is fine: {"; ".join(f.get("title", "") for f in dangers)})'
        self.set_draft(fields_to_draft(new))

    def save_draft_edits(self):
        s = self.cur
        if not s or not s.draft or not self.edits:
            return
        f = dict(s.fields)
        if 'multiplier' in self.edits:
            f['multiplier'] = f'{self.edits["multiplier"].value():g}'
            cut = self.edits['cut'].value()
            f['airtable deflation (hours)'] = f['crop deflation (hours)'] = f'{cut:g}'
            if cut and not (f.get('deflation reason') or '').strip() and self.fraud_cut(cut):
                f['deflation reason'] = self.fraud_reason()
            if cut and not f.get('deflation reason'):
                text, ok = QInputDialog.getText(self, 'Deflation reason', 'one line, why the hours were cut:')
                if not ok or not text.strip():
                    return
                f['deflation reason'] = text.strip()
            if not cut:
                f.pop('deflation reason', None)
        for k, e in self.edits.items():
            if isinstance(e, QPlainTextEdit):
                f[k] = e.toPlainText()
        new = fields_to_draft(f)
        if new != s.draft:
            self.store.set_work(s.sid, draft=new)
            self.say('draft saved')
            self.refresh_cur()

    def queue(self):
        s = self.cur
        if not s or not s.draft:
            return
        if s.verdict_kind == 'skip':
            self.say('skips are staff notes, post that one by hand')
            return
        self.save_draft_edits()
        s = self.by_key.get(s.key)
        text, _ = KIND_BADGE.get(s.verdict_kind, ('?', ''))
        extra = f'  x{s.fields.get("multiplier")}' if s.verdict_kind == 'approve' else ''
        if QMessageBox.question(self, 'Queue', f'Queue {text}{extra} for {s.title}?') != QMessageBox.Yes:
            return
        self.say('queueing…')
        if self.claimed == s.sid:
            self.release_claim(quiet=True)   # the runner claims it again right before it sends
        background(lambda: core.queue_draft(self.cfg, self.tools, self.store, s),
                   lambda res, err: (self.say(f'NOT queued: {err}', 15000) if err else self.say(res), self.refresh_cur()))

    def unqueue(self):
        s = self.cur
        if s and core.unqueue(self.cfg, s):
            self.say('taken out of the send queue')
            self.refresh_cur()

    def skip(self):
        s = self.cur
        if not s:
            return
        if s.skipped:
            self.store.set_work(s.sid, skipped=0)
            self.say('back in the list')
        elif QMessageBox.question(self, 'Skip', f'Hide {s.title} from the queue list? It stays under "skipped".') == QMessageBox.Yes:
            self.store.set_work(s.sid, skipped=1)
        self.reload()

    def delete_draft(self):
        s = self.cur
        if s and QMessageBox.question(self, 'Delete draft', 'Delete the draft? Your notes stay.') == QMessageBox.Yes:
            self.store.set_work(s.sid, remarks=None)
            self.set_draft(None)

    # ---- stash: all your work put aside, to start fresh and bring back later ----
    def stash_folder(self):
        return self.cfg['workspace'] / 'stashes'

    def stash(self):
        self.save_notes()
        if not self.store.has_work():
            self.say('nothing to stash')
            return
        if QMessageBox.question(self, 'Stash', 'Put everything aside and start fresh: notes, drafts, videos, set-up '
                                'folders, skips and imported pages? Pop brings it all back. Queued and sent reviews are '
                                'not affected.') != QMessageBox.Yes:
            return
        path = self.store.stash(self.stash_folder())
        self.say(f'stashed everything in {path.name}, starting fresh')
        self.reload()

    def pop_stash(self, path):
        when = stash_when(path)
        if QMessageBox.question(self, 'Pop stash', f'Bring back the stash from {when}? What you have now gets stashed '
                                'first, so nothing is lost.') != QMessageBox.Yes:
            return
        self.save_notes()
        self.store.pop(path, self.stash_folder())
        self.say(f'brought back the stash from {when}')
        self.reload()
        self.refetch()

    def drop_stash(self, path):
        if QMessageBox.question(self, 'Delete stash', f'Delete the stash from {stash_when(path)} for good?') == QMessageBox.Yes:
            Path(path).unlink(missing_ok=True)
            self.say('stash deleted')

    def fill_stash_menu(self, menu):
        menu.clear()
        menu.addAction('Stash everything and start fresh').triggered.connect(self.stash)
        menu.addSeparator()
        found = db.stashes(self.stash_folder()) if self.stash_folder().is_dir() else []
        if not found:
            menu.addAction('no stashes yet').setEnabled(False)
        for x in found:
            sub = menu.addMenu(f"{stash_when(x['path'])} · {x['drafts']} drafts, {x['notes']} with notes, {x['pages']} pages")
            sub.addAction('Pop (bring it back)').triggered.connect(lambda _=False, f=x['path']: self.pop_stash(f))
            sub.addAction('Delete').triggered.connect(lambda _=False, f=x['path']: self.drop_stash(f))

    # ---- prompts ----
    def columns_changed(self, reset_widths=False):
        if reset_widths:
            self.table.widths.clear()
            core.save_state(column_widths={})
        self.table.size_columns(self.cfg['queue_columns'])

    def dump_queue(self):
        if self.dumping:
            self.say('a dump is already running, see the terminal')
            return
        out = self.cfg['workspace'] / 'dumps' / time.strftime('terra-queue-%Y-%m-%d-%H%M.md')
        out.parent.mkdir(parents=True, exist_ok=True)
        before = set(core.dump_folders(self.cfg))
        self.dumping = True

        def done(code):
            self.dumping = False
            new = [f for f in core.dump_folders(self.cfg) if f not in before]
            n = sum(core.import_dump(self.store, f) for f in new)
            self.say(f'dump finished: {out}, {n} review pages cached' if code == 0 else
                     f'dump stopped (exit {code}); {n} review pages cached so far', 30000)
            if self.cur:
                self.load_page(self.cur)
        self.terminal.run(['python3', '-I', str(self.cfg['terra_api']), 'dump', '--all', '-o', str(out)], 'dump queue',
                          self.cfg['workspace'], done=done)

    def key_changed(self):
        self.tools._api = None
        self.say('Terra key changed, reloading the queue')
        self.refetch()

    # ---- claims ----
    def claim_cur(self):
        s = self.cur
        if not s:
            return
        if core.runner_alive(self.cfg) and core.queued_sids(self.cfg):
            if QMessageBox.question(self, 'Claim', 'The runner is sending your queue, and Terra only lets you hold one '
                                    'claim. Claiming now can make its next send fail. Claim anyway?') != QMessageBox.Yes:
                return
        old = self.claimed

        def work():
            if old and old != s.sid:
                core.release(self.tools, old)
            return core.claim(self.tools, s.sid)

        def done(res, err):
            if err:
                self.say(f'claim failed: {err}', 15000)
                if old and old != s.sid:
                    self.claimed = None
                return
            self.claimed = s.sid
            self.say(f'claimed {s.title}' + (', released the previous one' if old and old != s.sid else ''))
            if self.cur and self.cur.key == s.key:
                self.draft_area.setWidget(self.draft_widget(self.cur))
        self.say('claiming…')
        background(work, done)

    def release_claim(self, quiet=False):
        sid = self.claimed
        if not sid:
            return
        self.claimed = None
        background(lambda: core.release(self.tools, sid),
                   lambda res, err: None if quiet else self.say(f'release failed: {err}' if err else 'claim released'))
        if self.cur and self.cur.sid == sid:
            self.draft_area.setWidget(self.draft_widget(self.cur))

    def beat(self):
        sid = self.claimed
        if not sid:
            return

        def done(held, err):
            if err:
                self.say(f'claim heartbeat failed ({err}), trying again in a minute', 15000)
            elif not held and self.claimed == sid:
                self.claimed = None
                self.say('your claim was lost (it expired or someone took it over)', 20000)
                if self.cur and self.cur.sid == sid:
                    self.draft_area.setWidget(self.draft_widget(self.cur))
        background(lambda: core.heartbeat(self.tools, sid), done)

    def closeEvent(self, event):
        self.terminal.stop_all()
        if self.claimed:
            try:
                core.release(self.tools, self.claimed)
            except Exception:  # noqa: BLE001 - closing anyway; the claim expires on its own
                pass
        super().closeEvent(event)

    def setup_ship(self):
        s = self.cur
        if not s:
            return
        self.say(f'setting up {s.title}: cloning and downloading (nothing runs)…', 120000)
        out = self.cfg['workspace'] / 'builds' / f'.prep-{s.sid}.json'

        def finished(code):
            try:
                res, err = json.loads(out.read_text()), None
            except (OSError, ValueError) as e:
                res, err = None, f'exited {code}' if code else e
            out.unlink(missing_ok=True)
            done(res, err)

        def done(res, err):
            if err:
                self.say(f'set up failed: {err}', 15000)
                return
            rel = ((res or {}).get('download') or {}).get('dir')
            repo = core.ship_repo(res or {})
            self.store.set_work(s.sid, release_dir=rel, repo_dir=repo)
            if rel:
                core.add_binary_checks(self.store, s.sid, rel)
            self.say('set up' + (f': release in {rel}' if rel else ', no release to download') + (f', repo in {repo}' if repo else ''), 20000)
            if self.cur and self.cur.key == s.key:
                self.refresh_cur()
        argv = ['bash', '-c', 'exec "$@" > "$TERRA_DESK_OUT"', 'prep', 'python3', '-I', str(self.cfg['terra_api']),
                *core.prep_args(self.cfg, s.sid)]
        self.terminal.run(argv, f'set up {s.title}', self.cfg['workspace'],
                          {**self.tools.github_env(), 'TERRA_DESK_OUT': str(out)}, finished)

    def release_dirs(self, s):
        out = [Path(s.release_dir)] if s.release_dir and Path(s.release_dir).is_dir() else []
        for fn in self.hooks.release_dirs:
            out += [Path(d) for d in fn(s) or [] if Path(d).is_dir() and Path(d) not in out]
        return out

    def fill_run_menu(self, menu):
        menu.clear()
        s = self.cur
        if not s:
            return
        found = False
        for d in self.release_dirs(s):
            for exe, how in core.find_runnables(d)[:8]:
                found = True
                a = menu.addAction(f'{exe.relative_to(d)}  ({how}, sandboxed)')
                a.triggered.connect(lambda _=False, d=d, exe=exe, how=how: self.run_in_terminal(core.sandbox_command(self.cfg, d, exe, how), exe.name, d))
        demo = self.demo_url()
        for u in ([demo] if demo else []) + [u for u in core.links(s.quick) if u != demo][:5]:
            found = True
            a = menu.addAction(f'open {u[:70]}')
            a.triggered.connect(lambda _=False, u=u: open_url(self, u))
        if not found:
            menu.addAction('nothing to run yet, press Set up first').setEnabled(False)

    def run_in_terminal(self, argv, title, cwd=None):
        self.terminal.run(argv, title, cwd or Path.home())

    def open_outside(self, argv, cwd=None):
        subprocess.Popen(core.terminal_command(self.cfg, argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, cwd=cwd)
        self.say('started in a new terminal window')

    def attach_video(self):
        s = self.cur
        if not s:
            return
        src, _ = QFileDialog.getOpenFileName(self, 'Pick the test recording', str(Path.home() / 'Videos'),
                                             'Videos (*.mp4 *.mkv *.webm *.mov *.avi)')
        if not src:
            return
        dest = self.cfg['workspace'] / 'videos' / f'{s.sid}.mp4'
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.video_label.setText('video: compressing…')

        def done(res, err):
            if err:
                self.say(f'video failed: {err}', 15000)
                return
            self.store.set_work(s.sid, video=str(res))
            self.say(f'video ready: {Path(res).stat().st_size / 1048576:.1f} MiB')
            if self.cur and self.cur.key == s.key:
                self.refresh_cur()
        background(lambda: core.compress_video(src, dest), done)


def user_bin_on_path():
    # ~/.local/bin holds per-user programs (claude, pipx tools); fish adds it to PATH but bash often doesn't, and
    # terra-desk is started from a bash launcher
    local = Path.home() / '.local/bin'
    parts = os.environ.get('PATH', '').split(os.pathsep)
    if local.is_dir() and str(local) not in parts:
        os.environ['PATH'] = os.pathsep.join([*parts, str(local)])


def main():
    user_bin_on_path()
    # Qt objects must only be destroyed on the main thread, so Python's garbage collector only runs here, on a timer
    gc.disable()
    app = QApplication(sys.argv)
    app.setApplicationName('terra-desk')
    theme.apply(app)
    collector = QTimer()
    collector.timeout.connect(lambda: gc.collect())
    collector.start(3000)
    w = Main()
    w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
