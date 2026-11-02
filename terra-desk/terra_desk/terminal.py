"""A terminal inside terra-desk: Set up, Run and the commands in test steps open here, one tab each, instead of in a
separate window. A tab closes itself when its command works; when it fails it stays open with the exit code. Programs that open their own windows (a game under Wine) still get their own window.

It's a real terminal (a pty with pyte's VT100 emulator, bundled in vendor/): colours, prompts, Ctrl+C and simple
full-screen programs work. "Open in <terminal>" runs the same command in your own terminal instead."""
import fcntl
import os
import signal
import struct
import subprocess
import sys
import termios
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRect, QSocketNotifier, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QPainter
from PySide6.QtWidgets import QHBoxLayout, QMenu, QPushButton, QTabWidget, QVBoxLayout, QWidget

from . import theme

sys.path.append(str(Path(__file__).parent / 'vendor'))     # a pyte installed on the system wins
import pyte  # noqa: E402

COLORS = {'black': theme.LINE, 'red': theme.PINK, 'green': theme.GREEN, 'brown': theme.BUTTER, 'yellow': theme.BUTTER,
          'blue': theme.BLUE, 'magenta': theme.LILAC, 'cyan': theme.MINT, 'white': theme.INK}
BRIGHT = {'brightblack': theme.FAINT, 'brightred': theme.TINTS[0], 'brightgreen': theme.TINTS[5],
          'brightbrown': theme.TINTS[2], 'brightyellow': theme.TINTS[2], 'brightblue': theme.TINTS[1],
          'brightmagenta': theme.TINTS[4], 'bfightmagenta': theme.TINTS[4], 'brightcyan': theme.TINTS[5],
          'brightwhite': '#ffffff'}
KEYS = {Qt.Key_Return: '\r', Qt.Key_Enter: '\r', Qt.Key_Backspace: '\x7f', Qt.Key_Tab: '\t', Qt.Key_Backtab: '\x1b[Z',
        Qt.Key_Escape: '\x1b', Qt.Key_Home: '\x1b[H', Qt.Key_End: '\x1b[F', Qt.Key_Insert: '\x1b[2~',
        Qt.Key_Delete: '\x1b[3~', Qt.Key_PageUp: '\x1b[5~', Qt.Key_PageDown: '\x1b[6~', Qt.Key_F1: '\x1bOP',
        Qt.Key_F2: '\x1bOQ', Qt.Key_F3: '\x1bOR', Qt.Key_F4: '\x1bOS'}
ARROWS = {Qt.Key_Up: 'A', Qt.Key_Down: 'B', Qt.Key_Right: 'C', Qt.Key_Left: 'D'}


def color(name, default):
    if name == 'default':
        return QColor(default)
    if name in COLORS or name in BRIGHT:
        return QColor(COLORS.get(name) or BRIGHT[name])
    return QColor('#' + name) if len(name) == 6 else QColor(default)


class Screen(pyte.HistoryScreen):
    """pyte's screen, answering the terminal's own queries (cursor position, device attributes) through `reply`."""

    def __init__(self, cols, rows, reply):
        super().__init__(cols, rows, history=5000, ratio=0.5)
        self.reply = reply

    def write_process_input(self, data):
        self.reply(data.encode())

    def resize(self, lines=None, columns=None):
        """Like a real terminal when it gets shorter: blank lines under the cursor go first, then the top lines move
        into the scrollback (pyte on its own just drops them from the top)."""
        lines = lines or self.lines
        k = self.lines - lines
        if k <= 0:
            super().resize(lines, columns)
            return
        rows = [self.buffer[y] for y in range(self.lines)]
        drop = 0
        while drop < k and self.lines - 1 - drop > self.cursor.y and not any(
                c.data.strip() for c in rows[self.lines - 1 - drop].values()):
            drop += 1
        self.history.top.extend(rows[:k - drop])
        rows = rows[self.lines - drop:] + rows[:self.lines - drop]      # the blank bottom lines go to the top...
        for y, row in enumerate(rows):
            self.buffer[y] = row
        y = self.cursor.y
        super().resize(lines, columns)                                  # ...where pyte drops them, with the old top
        self.cursor.y = max(0, y - (k - drop))


class Session(QObject):
    """One command in a pty, started once its view knows its size (pyte drops the top lines when a screen shrinks).
    `changed` when the screen changes, `finished(code)` when it exits."""
    changed = Signal()
    finished = Signal(int)

    def __init__(self, argv, cwd=None, env=None):
        super().__init__()
        self.argv, self.cwd, self.env, self.code = argv, cwd, env, None
        self.screen = Screen(100, 30, self.write)
        self.stream = pyte.ByteStream(self.screen)
        self.master = self.proc = None

    def start(self, cols, rows):
        self.screen.resize(rows, cols)
        self.master, slave = os.openpty()
        self.set_size(cols, rows)
        # setsid --ctty makes the pty the command's controlling terminal, so Ctrl+C reaches it
        self.proc = subprocess.Popen(['setsid', '--ctty', *self.argv], stdin=slave, stdout=slave, stderr=slave,
                                     cwd=self.cwd, close_fds=True,
                                     env={**(self.env or os.environ), 'TERM': 'xterm-256color', 'COLORTERM': 'truecolor'})
        os.close(slave)
        self.notifier = QSocketNotifier(self.master, QSocketNotifier.Read, self)
        self.notifier.activated.connect(self.read)

    def read(self):
        try:
            data = os.read(self.master, 65536)
        except OSError:
            data = b''
        if data:
            self.stream.feed(data)
            self.changed.emit()
            return
        self.notifier.setEnabled(False)
        self.code = self.proc.wait()
        os.close(self.master)
        self.master = None
        self.stream.feed(f'\r\n\x1b[2m[exited {self.code}]\x1b[0m'.encode())
        self.changed.emit()
        self.finished.emit(self.code)

    @property
    def running(self):
        return self.master is not None

    def write(self, data):
        if self.running:
            os.write(self.master, data)

    def set_size(self, cols, rows):
        if self.running:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

    def resize(self, cols, rows):
        if self.proc is None:
            self.start(cols, rows)
        elif (cols, rows) != (self.screen.columns, self.screen.lines):
            self.screen.resize(rows, cols)
            self.set_size(cols, rows)

    def stop(self):
        if self.running:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


class TerminalView(QWidget):
    """Draws a Session's screen and sends it your keys. Wheel scrolls back; drag selects; Ctrl+Shift+C/V copy and
    paste; the right-click menu has the same."""

    def __init__(self, session):
        super().__init__()
        self.session = session
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setCursor(Qt.IBeamCursor)
        self.font_ = QFont(theme.MONO)
        self.font_.setPointSizeF(10)
        self.font_.setStyleHint(QFont.Monospace)
        fm = QFontMetricsF(self.font_)
        self.cw, self.ch, self.ascent = fm.horizontalAdvance('M'), fm.height(), fm.ascent()
        self.sel = None         # ((row, col), (row, col)) while something is selected
        session.changed.connect(self.on_changed)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.menu)

    # ---- drawing ----
    def on_changed(self):
        screen = self.session.screen
        for y in screen.dirty:
            self.update(QRect(0, int(y * self.ch), self.width(), int(self.ch) + 1))
        screen.dirty.clear()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(e.rect(), QColor(theme.BG))
        screen = self.session.screen
        first = max(0, int(e.rect().top() // self.ch))
        last = min(screen.lines - 1, int(e.rect().bottom() // self.ch))
        for y in range(first, last + 1):
            self.paint_line(p, y, screen.buffer[y], screen.columns)
        c = screen.cursor
        if self.hasFocus() and not c.hidden and first <= c.y <= last and self.session.running:
            r = QRect(int(c.x * self.cw), int(c.y * self.ch), int(self.cw), int(self.ch))
            p.fillRect(r, QColor(theme.PINK))
            p.setPen(QColor(theme.BG))
            p.setFont(self.font_)
            p.drawText(int(c.x * self.cw), int(c.y * self.ch + self.ascent), screen.buffer[c.y][c.x].data)
        p.end()

    def selected(self, y, x):
        if not self.sel:
            return False
        a, b = sorted(self.sel)
        return a <= (y, x) <= b

    def paint_line(self, p, y, line, cols):
        x = 0
        top = y * self.ch
        while x < cols:
            ch = line[x]
            fg, bg = color(ch.fg, theme.INK), color(ch.bg, theme.BG)
            if ch.reverse:
                fg, bg = bg, fg
            if self.selected(y, x):
                fg, bg = QColor(theme.BG), QColor(theme.LILAC)
            run, end = [ch.data], x + 1
            if ch.data and len(ch.data) == 1 and ord(ch.data) < 0x1100:     # group narrow chars with the same look
                while end < cols:
                    nx = line[end]
                    if (nx.fg, nx.bg, nx.bold, nx.italics, nx.underscore, nx.strikethrough, nx.reverse) != \
                            (ch.fg, ch.bg, ch.bold, ch.italics, ch.underscore, ch.strikethrough, ch.reverse) \
                            or self.selected(y, end) != self.selected(y, x) or not nx.data or ord(nx.data[0]) >= 0x1100:
                        break
                    run.append(nx.data)
                    end += 1
            if bg != QColor(theme.BG):
                p.fillRect(QRect(int(x * self.cw), int(top), int((end - x) * self.cw) + 1, int(self.ch) + 1), bg)
            text = ''.join(run)
            if text.strip():
                f = QFont(self.font_)
                f.setBold(ch.bold)
                f.setItalic(ch.italics)
                f.setUnderline(ch.underscore)
                f.setStrikeOut(ch.strikethrough)
                p.setFont(f)
                p.setPen(fg)
                p.drawText(int(x * self.cw), int(top + self.ascent), text)
            x = end

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.session.resize(max(20, int(self.width() // self.cw)), max(4, int(self.height() // self.ch)))
        self.update()

    # ---- keys ----
    def event(self, e):
        if e.type() == QEvent.ShortcutOverride:      # keys go to the program, not to the app's shortcuts
            if e.key() != Qt.Key_QuoteLeft or not e.modifiers() & Qt.ControlModifier:
                e.accept()
                return True
        return super().event(e)

    def focusNextPrevChild(self, _next):
        return False        # Tab is for the program

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self.update()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.update()

    def keyPressEvent(self, e):
        key, mods = e.key(), e.modifiers()
        ctrl, shift = bool(mods & Qt.ControlModifier), bool(mods & Qt.ShiftModifier)
        if ctrl and shift and key == Qt.Key_C:
            self.copy()
        elif ctrl and shift and key == Qt.Key_V:
            self.paste()
        elif key in ARROWS:
            app_mode = pyte.modes.DECCKM in self.session.screen.mode
            self.send(('\x1bO' if app_mode else '\x1b[') + ARROWS[key])
        elif key in KEYS:
            self.send(KEYS[key])
        elif ctrl and Qt.Key_A <= key <= Qt.Key_Z:
            self.send(chr(key - Qt.Key_A + 1))
        elif e.text():
            self.send(e.text())
        else:
            super().keyPressEvent(e)

    def send(self, text):
        if self.session.screen.history.position < self.session.screen.history.size:
            self.session.screen.next_page()     # typing jumps back to the bottom, like a terminal
        self.sel = None
        self.session.write(text.encode())

    def wheelEvent(self, e):
        screen = self.session.screen
        if e.angleDelta().y() > 0:
            screen.prev_page()
        else:
            screen.next_page()
        self.update()

    # ---- selection ----
    def cell(self, pos):
        s = self.session.screen
        return (min(s.lines - 1, max(0, int(pos.y() // self.ch))), min(s.columns - 1, max(0, int(pos.x() // self.cw))))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            here = self.cell(e.position())
            self.sel = (here, here)
            self.update()
        elif e.button() == Qt.MiddleButton:
            self.paste()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self.sel and e.buttons() & Qt.LeftButton:
            self.sel = (self.sel[0], self.cell(e.position()))
            self.update()

    def mouseReleaseEvent(self, e):
        if self.sel and self.sel[0] == self.sel[1]:
            self.sel = None
            self.update()

    def selected_text(self):
        if not self.sel:
            return ''
        (y0, x0), (y1, x1) = sorted(self.sel)
        s = self.session.screen
        lines = []
        for y in range(y0, y1 + 1):
            a, b = (x0 if y == y0 else 0), (x1 if y == y1 else s.columns - 1)
            lines.append(''.join(s.buffer[y][x].data for x in range(a, b + 1)).rstrip())
        return '\n'.join(lines)

    def copy(self):
        text = self.selected_text()
        if text:
            QGuiApplication.clipboard().setText(text)

    def paste(self):
        text = QGuiApplication.clipboard().text()
        if text:
            self.send(text.replace('\r\n', '\r').replace('\n', '\r'))

    def menu(self, pos):
        m = QMenu(self)
        m.addAction('Copy', self.copy).setEnabled(bool(self.sel))
        m.addAction('Paste', self.paste)
        m.exec(self.mapToGlobal(pos))


class TerminalPanel(QWidget):
    """The terminal tabs, with Stop, Open in your terminal, and Close."""

    def __init__(self, open_outside, say):
        super().__init__()
        self.open_outside, self.say = open_outside, say
        lay = QVBoxLayout(self)
        corner = QWidget()
        buttons = QHBoxLayout(corner)
        self.stop_btn, self.outside_btn, self.close_btn = QPushButton('Stop'), QPushButton('Open outside'), QPushButton('Close')
        self.stop_btn.setToolTip('send the running command SIGTERM (Ctrl+C in the terminal works too)')
        self.outside_btn.setToolTip('run the same command again in your own terminal')
        self.stop_btn.clicked.connect(lambda: self.current() and self.current().session.stop())
        self.outside_btn.clicked.connect(self.run_outside)
        self.close_btn.clicked.connect(lambda: self.close_tab(self.tabs.currentIndex()))
        for btn in (self.stop_btn, self.outside_btn, self.close_btn):
            buttons.addWidget(btn)
        self.tabs = QTabWidget()
        self.tabs.setCornerWidget(corner, Qt.TopRightCorner)
        self.tabs.currentChanged.connect(lambda _: self.update_buttons())
        lay.addWidget(self.tabs)
        self.update_buttons()

    def current(self):
        return self.tabs.currentWidget()

    def run(self, argv, title, cwd=None, env=None, done=None):
        """Start argv in a new tab and show it. done(code) runs when it exits."""
        session = Session(argv, cwd, env)
        view = TerminalView(session)
        i = self.tabs.addTab(view, title[:28])
        self.tabs.setCurrentIndex(i)
        if done:
            session.finished.connect(done)
        session.finished.connect(lambda code: self.finished(view, title, code))
        self.show()
        if not view.isVisible():        # nowhere to measure (the window isn't on screen): start at a default size
            session.resize(100, 30)
        view.setFocus()
        self.update_buttons()
        return session

    def finished(self, view, title, code):
        """A command that worked closes its tab after a moment; one that failed stays open with its exit code."""
        i = self.tabs.indexOf(view)
        if i < 0:
            return
        if code == 0:
            QTimer.singleShot(1200, lambda: self.close_tab(self.tabs.indexOf(view)))
        else:
            self.tabs.setTabText(i, f'{title[:24]} · exit {code}')
        self.update_buttons()

    def run_outside(self):
        view = self.current()
        if view:
            self.open_outside(view.session.argv, view.session.cwd)

    def close_tab(self, i):
        view = self.tabs.widget(i) if i >= 0 else None
        if not view:
            return
        view.session.stop()
        self.tabs.removeTab(i)
        view.deleteLater()
        if not self.tabs.count():
            self.hide()
        self.update_buttons()

    def update_buttons(self):
        view = self.current()
        self.stop_btn.setEnabled(bool(view and view.session.running))
        self.outside_btn.setEnabled(bool(view))
        self.close_btn.setEnabled(bool(view))

    def stop_all(self):
        for i in range(self.tabs.count()):
            self.tabs.widget(i).session.stop()

