"""A text box that shows markdown the way Obsidian's live preview does: styled as you type, with the markup (**, `,
[](), #) hidden unless the cursor is touching or inside it. Only the drawing changes; the text is exactly what you
typed."""
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextLayout, QTextOption
from PySide6.QtWidgets import QPlainTextEdit

from . import core, theme

CODE, LINK, URL, BOLD, ITALIC, STRIKE = core.MD_CODE, core.MD_LINK, core.URL, core.MD_BOLD, core.MD_ITALIC, core.MD_STRIKE
HEADING = re.compile(r'^(#{1,6} )')
LIST = re.compile(r'^(\s*)([-*+]|\d+[.)])( )')
QUOTE = re.compile(r'^(> ?)')
FENCE = re.compile(r'^\s*```')
IN_FENCE = 1


def char_format(color=None, bold=False, italic=False, mono=False, size=None, underline=False, strike=False, bg=None):
    f = QTextCharFormat()
    if color:
        f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Bold)
    if italic:
        f.setFontItalic(True)
    if mono:
        f.setFontFamilies([theme.MONO])
    if size:
        f.setFontPointSize(size)
    if underline:
        f.setFontUnderline(True)
    if strike:
        f.setFontStrikeOut(True)
    if bg:
        f.setBackground(QColor(bg))
    return f


def hidden_format():
    """Markup off the cursor's line: no colour and almost no width (a 1pt character is about a pixel)."""
    f = QTextCharFormat()
    f.setForeground(Qt.transparent)
    f.setFontPointSize(1)
    return f


class LiveMarkdown(QSyntaxHighlighter):
    def __init__(self, edit):
        super().__init__(edit.document())
        self.edit = edit
        self.cursor = None          # (start, end) of the cursor or selection while the box has focus, else None
        self.markup = char_format(theme.FAINT)
        self.hidden = hidden_format()
        self.code = char_format(theme.INK, mono=True, bg=theme.HOVER)
        self.link = char_format(theme.BLUE, underline=True)

    def blocks(self, span):
        if not span:
            return set()
        doc = self.document()
        return set(range(doc.findBlock(span[0]).blockNumber(), doc.findBlock(span[1]).blockNumber() + 1))

    def follow(self, span):
        """Move the reveal to span (absolute positions) or None, restyling only the lines that change."""
        old, self.cursor = self.cursor, span
        if old == span:
            return
        doc = self.document()
        was = self.edit.blockSignals(True)      # restyling isn't an edit: no textChanged, so nothing autosaves
        try:
            for n in self.blocks(old) | self.blocks(span):
                block = doc.findBlockByNumber(n)
                if block.isValid():
                    self.rehighlightBlock(block)
        finally:
            self.edit.blockSignals(was)

    def touched(self, a, b):
        """Is the cursor (or selection) touching or inside a..b of the current line?"""
        if not self.cursor:
            return False
        base = self.currentBlock().position()
        return self.cursor[0] - base <= b and self.cursor[1] - base >= a

    def mark(self, span, start, length):
        self.setFormat(start, length, self.markup if self.touched(*span) else self.hidden)

    def highlightBlock(self, text):
        in_fence = self.previousBlockState() == IN_FENCE
        if FENCE.match(text):
            self.setCurrentBlockState(0 if in_fence else IN_FENCE)
            self.setFormat(0, len(text), self.markup)
            return
        if in_fence:
            self.setCurrentBlockState(IN_FENCE)
            self.setFormat(0, len(text), self.code)
            return
        self.setCurrentBlockState(0)
        taken = []      # spans already styled as code or links, so ** inside them stays literal

        def free(a, b):
            return all(b <= x or a >= y for x, y in taken)

        line = 0, len(text)
        if m := HEADING.match(text):
            size = {1: 15, 2: 13.5, 3: 12.5}.get(len(m.group(1)) - 1, 11.5)
            self.setFormat(m.end(), len(text) - m.end(), char_format(theme.INK, bold=True, size=size))
            self.mark(line, 0, m.end())
        elif m := LIST.match(text):
            self.setFormat(m.start(2), len(m.group(2)), char_format(theme.PINK))
        elif m := QUOTE.match(text):
            self.setFormat(m.end(), len(text) - m.end(), char_format(theme.MUTED, italic=True))
            self.mark(line, 0, m.end())
        for m in CODE.finditer(text):
            self.setFormat(m.start(2), len(m.group(2)), self.code)
            self.mark(m.span(), m.start(), len(m.group(1)))
            self.mark(m.span(), m.end(2), len(m.group(1)))
            taken.append(m.span())
        for m in LINK.finditer(text):
            if free(*m.span()):
                self.setFormat(m.start(2), len(m.group(2)), self.link)
                self.mark(m.span(), m.start(1), 1)
                self.mark(m.span(), m.start(3), m.end(5) - m.start(3))
                taken.append(m.span())
        for m in URL.finditer(text):
            if free(*m.span()):
                self.setFormat(m.start(), len(m.group()), self.link)
                taken.append(m.span())
        for rx, fmt in ((BOLD, char_format(bold=True)), (ITALIC, char_format(italic=True)),
                        (STRIKE, char_format(theme.MUTED, strike=True))):
            for m in rx.finditer(text):
                if free(*m.span()):
                    mk = len(m.group(1))
                    inner = m.start(2), len(m.group(2))
                    self.setFormat(*inner, merged(self.format(inner[0]), fmt))
                    self.mark(m.span(), m.start(), mk)
                    self.mark(m.span(), m.end() - mk, mk)


def merged(base, extra):
    f = QTextCharFormat(base)
    f.merge(extra)
    return f


class MarkdownEdit(QPlainTextEdit):
    """QPlainTextEdit with LiveMarkdown. Ctrl+click opens a link through open_link(url)."""

    def __init__(self, text='', open_link=None, **kw):
        super().__init__(text, **kw)
        self.open_link = open_link
        self.live = LiveMarkdown(self)
        self.cursorPositionChanged.connect(self.follow_cursor)
        self.selectionChanged.connect(self.follow_cursor)

    def grow(self, min_lines=2, max_lines=40):
        """Fit the box's height to its text (wrapped lines too), between min_lines and max_lines."""
        self.lines = (min_lines, max_lines)
        self.document().contentsChanged.connect(self.fit_height)
        self.fit_height()

    def visual_lines(self):
        """The height of each line the text takes at the box's width, laid out the way it's drawn (with the markdown
        styling, so bold lines count as a little taller). Qt's own count only covers lines it has laid out so far."""
        width = self.viewport().width() - 2 * self.document().documentMargin()
        option = QTextOption()
        option.setWrapMode(self.wordWrapMode())
        heights, block = [], self.document().firstBlock()
        while block.isValid():
            layout = QTextLayout(block.text(), self.font())
            layout.setTextOption(option)
            layout.setFormats(block.layout().formats())
            layout.beginLayout()
            lines = []
            while (line := layout.createLine()).isValid():
                line.setLineWidth(width)
                lines.append(line.height())
            layout.endLayout()
            heights += lines or [self.fontMetrics().height()]
            block = block.next()
        return heights

    def fit_height(self):
        if not getattr(self, 'lines', None) or self.viewport().width() < 40:
            return
        lo, hi = self.lines
        heights = self.visual_lines()
        line = self.fontMetrics().height()
        text = sum(heights[:hi]) + line * max(0, lo - len(heights))
        cm, vm = self.contentsMargins(), self.viewportMargins()     # fixed, unlike the viewport while it's laid out
        chrome = cm.top() + cm.bottom() + vm.top() + vm.bottom()
        height = int(text + 2 * self.document().documentMargin() + chrome + 1)
        if height != self.height():
            self.setFixedHeight(height)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded if len(heights) > hi else Qt.ScrollBarAlwaysOff)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if e.oldSize().width() != e.size().width() or e.oldSize().height() != e.size().height():
            self.fit_height()

    def follow_cursor(self):
        if self.hasFocus():
            c = self.textCursor()
            self.live.follow((c.selectionStart(), c.selectionEnd()))

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self.follow_cursor()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.live.follow(None)

    def link_at(self, pos):
        c = self.cursorForPosition(pos)
        text, at = c.block().text(), c.positionInBlock()
        for m in LINK.finditer(text):
            if m.start() <= at <= m.end():
                return m.group(4)
        for m in URL.finditer(text):
            if m.start() <= at <= m.end():
                return m.group()
        return None

    def mousePressEvent(self, e):
        url = self.link_at(e.position().toPoint()) if e.modifiers() & Qt.ControlModifier else None
        if url and self.open_link:
            self.open_link(url)
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        over = bool(e.modifiers() & Qt.ControlModifier and self.link_at(e.position().toPoint()))
        self.viewport().setCursor(Qt.PointingHandCursor if over else Qt.IBeamCursor)
        super().mouseMoveEvent(e)
