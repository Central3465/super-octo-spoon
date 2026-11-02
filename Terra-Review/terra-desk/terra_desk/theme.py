"""The look, borrowed from Fluxion's web page: black, soft pastels, Archivo with IBM Plex Mono for labels, and cut
corners instead of round ones.

Lining up: every button, input, drop-down and tab is H tall, layouts space things GAP apart (Style), and frames keep
PAD between their edge and what's in them. Frames are painted here (Frames), snapped to whole device pixels, so their
lines stay sharp at any screen scale; the style sheet only sets colours, fonts and sizes."""
import tempfile
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import (QAbstractSpinBox, QComboBox, QFrame, QLabel, QLineEdit, QListWidget, QPlainTextEdit,
                               QProxyStyle,
                               QPushButton, QStyle, QStyledItemDelegate, QStyleFactory, QStyleOptionViewItem, QTableWidget,
                               QTabWidget, QToolButton, QWidget)

BG = '#000000'
INK = '#e6e2ee'
MUTED = '#9a96a8'
FAINT = '#87839a'
LINE = '#4a4757'
EDGE = '#615d73'        # outlines of things you can click or type in: 3:1 on black, LINE is only 2.3
HOVER = '#1d1c24'
BLUE = '#9cc7f2'
BLUE_LIGHT = '#b3d4f5'
GREEN = '#c6e38f'
PINK = '#f5a9c9'
BUTTER = '#f3d98b'
PEACH = '#febe92'
MINT = '#a3e4cb'
LILAC = '#c9b8f2'
TINTS = ['#ffafce', '#8ed5fe', '#f3e387', '#febe92', '#c9acff', '#93f3c1']
TINT = {'green': GREEN, 'butter': BUTTER, 'pink': PINK, 'blue': BLUE, 'lilac': LILAC}
SANS = 'Archivo'
MONO = 'IBM Plex Mono'

H = 30          # height of every button, input, drop-down and tab
GAP = 8         # space between things in a layout
PAD = 8         # space inside a frame, between its edge and what's in it
CUT = 6         # how far corners are cut
PANE_CUT = 14   # the big panel's corners

FONTS = Path(__file__).parent / 'fonts'
CACHE = Path(tempfile.gettempdir()) / 'terra-desk-theme'


def shade(color, amount=0.14):
    """The colour mixed into black, for a dark wash of a pastel."""
    c = QColor(color)
    return QColor(int(c.red() * amount), int(c.green() * amount), int(c.blue() * amount))


def load_fonts():
    for f in FONTS.glob('*.ttf'):
        QFontDatabase.addApplicationFont(str(f))


def label_font(size=8.5):
    """Small uppercase mono, like the headings on Fluxion's cards."""
    f = QFont(MONO)
    f.setPointSizeF(size)
    f.setWeight(QFont.Medium)
    f.setCapitalization(QFont.AllUppercase)
    f.setLetterSpacing(QFont.PercentageSpacing, 108)
    return f


def heading(text, color):
    """A small uppercase mono label, coloured, for the top of a panel."""
    lab = QLabel(text)
    lab.setFont(label_font())
    lab.setStyleSheet(f'color:{color}')
    return lab


def mono():
    f = QFont(MONO)
    f.setPointSizeF(10)
    return f


def palette():
    p = QPalette()
    for role, color in ((QPalette.Window, BG), (QPalette.Base, BG), (QPalette.AlternateBase, BG), (QPalette.Button, BG),
                        (QPalette.WindowText, INK), (QPalette.Text, INK), (QPalette.ButtonText, INK),
                        (QPalette.ToolTipBase, BG), (QPalette.ToolTipText, INK), (QPalette.PlaceholderText, MUTED),
                        (QPalette.Highlight, PINK), (QPalette.HighlightedText, BG), (QPalette.Link, BLUE)):
        p.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(FAINT))
    return p


def snapped(p, points, width=0):
    """A polygon in p's coordinates with every corner on a whole device pixel, so lines stay sharp at any scale.

    points: (x, y, ix, iy, dx, dy). (x, y) is a logical corner; ix/iy (-1, 0, 1) move it half a line width inwards so
    a line of `width` logical px covers whole pixels; dx/dy are logical offsets (corner cuts) added after rounding.
    Returns (polygon, line width)."""
    t = p.deviceTransform()
    inv = t.inverted()[0]
    scale = t.m11()
    w = max(1, round(width * scale)) if width else 0
    h = w / 2
    out = []
    for x, y, ix, iy, dx, dy in points:
        d = t.map(QPointF(x, y))
        out.append(inv.map(QPointF(round(d.x()) + ix * h + round(dx * scale), round(d.y()) + iy * h + round(dy * scale))))
    return QPolygonF(out), w / scale


def cut_shape(p, rect, cut=CUT, width=0):
    """The cut-corner outline of rect. Returns (polygon, line width)."""
    x0, y0, x1, y1 = rect.x(), rect.y(), rect.x() + rect.width(), rect.y() + rect.height()
    c = cut
    return snapped(p, [(x0, y0, 1, 1, c, 0), (x1, y0, -1, 1, -c, 0), (x1, y0, -1, 1, 0, c), (x1, y1, -1, -1, 0, -c),
                       (x1, y1, -1, -1, -c, 0), (x0, y1, 1, -1, c, 0), (x0, y1, 1, -1, 0, -c), (x0, y0, 1, 1, 0, c)],
                   width)


def paint_tab_pane(p, w):
    """The panel as a closed cut-corner box, with the selected tab sitting on its top line like a folder tab, in a
    pastel of its own."""
    bar = w.tabBar()
    g = bar.geometry()
    r = w.rect()
    top = g.y() + g.height()
    paint_frame(p, QRect(r.x(), top, r.width(), r.height() - top), BLUE, BG, PANE_CUT)
    if bar.currentIndex() < 0:
        return
    tab = bar.tabRect(bar.currentIndex()).translated(g.topLeft())
    tl, tr, tt, k = tab.x(), tab.x() + tab.width(), tab.y(), 5
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    shape, lw = snapped(p, [(tl, top, 1, 0, 0, 0), (tl, tt, 1, 1, 0, k), (tl, tt, 1, 1, k, 0), (tr, tt, -1, 1, -k, 0),
                            (tr, tt, -1, 1, 0, k), (tr, top, -1, 0, 0, 0)], 1)
    p.setPen(Qt.NoPen)
    p.setBrush(shade(PINK, 0.16))
    p.drawPolygon(shape)
    p.setPen(QPen(QColor(PINK), lw, Qt.SolidLine, Qt.FlatCap, Qt.MiterJoin))
    p.setBrush(Qt.NoBrush)
    p.drawPolyline(shape)
    p.restore()


def paint_frame(p, rect, outline=None, fill=None, cut=CUT, width=1):
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    shape, w = cut_shape(p, rect, cut, width if outline else 0)
    p.setPen(QPen(QColor(outline), w, Qt.SolidLine, Qt.FlatCap, Qt.MiterJoin) if outline else Qt.NoPen)
    p.setBrush(QColor(fill) if fill else Qt.NoBrush)
    p.drawPolygon(shape)
    p.restore()


def button_frame(b):
    """(outline, fill, line width) for a button in its current state."""
    hover, down = b.underMouse(), b.isDown()
    if b.objectName() == 'primary':
        return (INK, BLUE, 2) if down else (BLUE_LIGHT, BLUE_LIGHT, 1) if hover else (BLUE, BLUE, 1)
    tint = TINT.get(b.property('tint'))
    if tint:
        if b.property('on') or down:
            return (INK, tint, 2) if hover and not down else (tint, tint, 1)
        return (tint, HOVER, 2) if hover else (tint, BG, 1)
    if down or (isinstance(b, QToolButton) and b.menu() and b.menu().isVisible()):
        return BLUE, BLUE, 1
    return (BLUE, HOVER, 2) if hover else (EDGE, BG, 1)


def frames_for(w):
    """What to paint under widget w: [(rect, outline, fill, cut, line width)], or None."""
    if isinstance(w, QPushButton) and w.objectName() == 'pick':       # a PickList looks like a drop-down
        open_ = w.menu() and w.menu().isVisible()
        return [(w.rect(), PINK if open_ else MUTED if w.underMouse() else EDGE, BG, CUT, 1)]
    if isinstance(w, (QPushButton, QToolButton)):
        return [(w.rect(), *button_frame(w)[:2], CUT, button_frame(w)[2])]
    if isinstance(w, QLineEdit) and isinstance(w.parent(), (QAbstractSpinBox, QComboBox)):
        return None
    if isinstance(w, (QLineEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox)):
        outline = PINK if w.hasFocus() else MUTED if w.underMouse() else EDGE
        return [(w.rect(), outline, BG, CUT, 1)]
    if isinstance(w, (QTableWidget, QListWidget)) or (isinstance(w, QFrame) and w.objectName() == 'code'):
        return [(w.rect(), LINE, BG, CUT, 1)]
    return None


class ListRows(QStyledItemDelegate):
    """Lists and drop-downs highlight like the queue: a cut-corner lilac shape with black text, a dark wash on hover."""

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        selected = bool(opt.state & QStyle.State_Selected)
        if selected or opt.state & QStyle.State_MouseOver:
            paint_frame(painter, opt.rect.adjusted(0, 1, 0, -1), fill=LILAC if selected else shade(LILAC, 0.25), cut=4)
        if selected:
            opt.palette.setColor(QPalette.Text, QColor(BG))
        opt.state &= ~(QStyle.State_Selected | QStyle.State_MouseOver | QStyle.State_HasFocus)
        w = opt.widget
        (w.style() if w else QStyleFactory.create('Fusion')).drawControl(QStyle.CE_ItemViewItem, opt, painter, w)


class Frames(QObject):
    """Paints the cut-corner frames under the widgets (installed on the application, so it sees every widget), and
    gives lists and drop-downs the ListRows highlight."""
    REPAINT = (QEvent.Enter, QEvent.Leave, QEvent.FocusIn, QEvent.FocusOut, QEvent.MouseButtonPress,
               QEvent.MouseButtonRelease, QEvent.DynamicPropertyChange)

    def eventFilter(self, obj, ev):
        kind = ev.type()
        if kind == QEvent.Paint and isinstance(obj, QTabWidget):
            p = QPainter(obj)
            paint_tab_pane(p, obj)
            p.end()
        elif kind == QEvent.Paint and isinstance(obj, QWidget):
            spec = frames_for(obj)
            if spec:
                p = QPainter(obj)
                for rect, outline, fill, cut, width in spec:
                    paint_frame(p, rect, outline, fill, cut, width)
                p.end()
        elif kind in self.REPAINT and isinstance(obj, QWidget) and frames_for(obj):
            obj.update()
        elif kind == QEvent.Polish and isinstance(obj, QTabWidget) and not obj.property('framed'):
            obj.setProperty('framed', True)
            obj.currentChanged.connect(obj.update)
        elif kind == QEvent.Polish and isinstance(obj, (QListWidget, QComboBox)):
            view = obj.view() if isinstance(obj, QComboBox) else obj
            if not isinstance(view.itemDelegate(), ListRows):
                view.setItemDelegate(ListRows(view))
                view.setMouseTracking(True)
        return False


class Style(QProxyStyle):
    """Fusion (the same on every desktop), with one gap size: GAP between things, no margins inside widgets (the
    frames carry their own PAD), and GAP * 2 around windows and dialogs."""

    def __init__(self):
        super().__init__(QStyleFactory.create('Fusion'))

    def pixelMetric(self, metric, option=None, widget=None):
        if metric in (QStyle.PM_LayoutHorizontalSpacing, QStyle.PM_LayoutVerticalSpacing):
            return GAP
        if metric in (QStyle.PM_LayoutLeftMargin, QStyle.PM_LayoutTopMargin, QStyle.PM_LayoutRightMargin,
                      QStyle.PM_LayoutBottomMargin):
            return GAP * 2 if widget is not None and widget.isWindow() else 0
        return super().pixelMetric(metric, option, widget)


def arrow(direction, color=MUTED):
    """A small chevron for combo boxes and spin boxes (a styled one loses Qt's own)."""
    path = CACHE / f'arrow-{direction}-{color[1:]}.png'
    if not path.exists():
        CACHE.mkdir(exist_ok=True)
        img = QImage(40, 40, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(color), 4.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        pts = [(10, 16), (20, 26), (30, 16)] if direction == 'down' else [(10, 24), (20, 14), (30, 24)]
        p.drawPolyline(QPolygonF([QPointF(x, y) for x, y in pts]))
        p.end()
        img.save(str(path))
    return f'image: url({path}); width: 10px; height: 10px'


def style_sheet():
    tints = '\n'.join(f'QPushButton[tint="{name}"] {{ color: {c}; }}\n'
                      f'QPushButton[tint="{name}"][on="true"], QPushButton[tint="{name}"]:pressed {{ color: {BG}; }}'
                      for name, c in TINT.items())
    return f"""
QMainWindow, QDialog, QMessageBox {{ background: {BG}; }}
QToolTip {{ background: {BG}; color: {INK}; border: 1px solid {LINE}; padding: 4px 8px; }}

QPushButton, QToolButton {{ border: 0; background: transparent; color: {INK}; font-weight: 600;
    padding: 0 12px; min-height: {H}px; max-height: {H}px; }}
QPushButton:hover, QToolButton:hover {{ color: {INK}; }}
QPushButton:pressed, QToolButton:pressed, QPushButton#primary {{ color: {BG}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QPushButton#pick {{ font-weight: normal; }}
QPushButton#pick::menu-indicator {{ image: none; width: 0; }}
{tints}

QLineEdit, QComboBox, QDoubleSpinBox {{ border: 0; background: transparent; padding: 0 {PAD}px;
    min-height: {H}px; max-height: {H}px; }}
QLineEdit:hover, QComboBox:hover, QDoubleSpinBox:hover {{ color: {INK}; }}
QPlainTextEdit {{ border: 0; background: transparent; padding: {PAD // 2}px; }}
QComboBox::drop-down, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ border: 0; width: 16px; background: transparent;
    margin-right: {CUT}px; }}
QComboBox::down-arrow, QDoubleSpinBox::down-arrow {{ {arrow('down')}; }}
QDoubleSpinBox::up-arrow {{ {arrow('up')}; }}
QComboBox QAbstractItemView, QMenu {{ background: {BG}; border: 1px solid {LINE}; padding: 4px; outline: 0; }}
QMenu::item:selected {{ background: {LILAC}; color: {BG}; }}
QComboBox QAbstractItemView, QListWidget {{ selection-background-color: transparent; selection-color: {BG}; }}
QComboBox QAbstractItemView::item, QListWidget::item {{ padding: 4px 8px; }}
QListWidget {{ border: 0; background: transparent; padding: {PAD // 2}px; outline: 0; }}
QMenu::item {{ padding: 4px 14px; }}
QMenu::item:disabled {{ color: {FAINT}; }}
QCheckBox {{ spacing: {GAP}px; min-height: {H}px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid {EDGE}; background: {BG}; }}
QCheckBox::indicator:hover {{ border-color: {BLUE}; }}
QCheckBox::indicator:checked {{ background: {PINK}; border-color: {PINK}; }}
QCheckBox::indicator:disabled {{ background: {LINE}; border-color: {LINE}; }}
QCheckBox:disabled {{ color: {FAINT}; }}

QTableWidget {{ border: 0; background: transparent; padding: {PAD // 2}px; outline: 0;
    selection-background-color: transparent; selection-color: {BG}; }}
QTableWidget::item {{ padding: 0 4px; }}
QHeaderView, QHeaderView::section {{ background: transparent; color: {MUTED}; border: 0; padding: 4px; font: 500 8pt '{MONO}'; }}
QFrame#code {{ border: 0; background: transparent; }}
QTextBrowser, QScrollArea {{ border: 0; background: transparent; }}

QTabWidget::pane {{ border: 0; padding: {PANE_CUT}px; }}
QTabWidget::tab-bar {{ left: {PANE_CUT + GAP}px; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 0 14px; height: {H}px; margin: 0;
    font: 500 9pt '{MONO}'; }}
QTabBar::tab:hover {{ color: {INK}; }}
QTabBar::tab:selected {{ color: {PINK}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle {{ background: {EDGE}; min-height: 24px; min-width: 24px; margin: 2px; }}
QScrollBar::handle:hover {{ background: {MUTED}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: 0; width: 0; height: 0; }}
QSplitter::handle {{ background: transparent; }}
QToolBar {{ border: 0; spacing: {GAP}px; padding: {GAP}px {GAP}px {GAP}px {GAP}px; }}
QToolBar::separator {{ width: {GAP}px; }}
QStatusBar {{ color: {MUTED}; }}
QStatusBar::item {{ border: 0; }}
"""


def apply(app):
    load_fonts()
    app.setStyle(Style())
    app.setPalette(palette())
    app.setFont(QFont(SANS, 11))
    app.setStyleSheet(style_sheet())
    app.frames = Frames(app)
    app.installEventFilter(app.frames)
