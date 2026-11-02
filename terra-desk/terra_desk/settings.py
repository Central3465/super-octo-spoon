"""Settings: your handle and terminal, the queue table's columns, the Terra key, the verdict templates, and the prompts
and documents."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox,
                               QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget)

from . import core, prompts
from .theme import GREEN, MUTED, PINK, mono

KEY_HELP = ('Your Terra key is your own login. On terra.hackclub.com, open the browser console and run '
            '<code>copy(localStorage.terra_auth_token)</code>, then paste it here. It is saved to '
            f'<code>{str(core.TOKEN_FILE).replace(str(Path.home()), "~", 1)}</code> (only you can read it) and never '
            'shown again.')
TEMPLATE_NAMES = {'approve': 'Approve', 'changes': 'Request changes', 'reject': 'Reject'}


def muted(text):
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet(f'color:{MUTED}')
    return lab


class Editor(QWidget):
    """A list of texts on the left and the selected one on the right, with Save and Reset.

    entries: {name: {'help': str, 'default': str}} for texts stored with prompts.save, or {'path': Path} for files
    edited in place."""

    def __init__(self, win, entries):
        super().__init__()
        self.win, self.entries = win, entries
        lay = QHBoxLayout(self)
        self.list = QListWidget()
        self.list.addItems(list(entries))
        self.list.setFixedWidth(220)
        lay.addWidget(self.list)
        right = QVBoxLayout()
        self.help = muted('')
        self.edit = QPlainTextEdit()
        self.edit.setFont(mono())
        self.state = muted('')
        self.save_btn, self.reset_btn = QPushButton('Save'), QPushButton('Reset to default')
        self.save_btn.setObjectName('primary')
        row = QHBoxLayout()
        row.addWidget(self.state)
        row.addStretch()
        row.addWidget(self.reset_btn)
        row.addWidget(self.save_btn)
        right.addWidget(self.help)
        right.addWidget(self.edit, 1)
        right.addLayout(row)
        lay.addLayout(right, 1)
        self.list.currentTextChanged.connect(lambda n: self.show(n) if n else None)
        self.save_btn.clicked.connect(self.save)
        self.reset_btn.clicked.connect(self.reset)
        if entries:
            self.list.setCurrentRow(0)

    def current(self):
        item = self.list.currentItem()
        return (item.text(), self.entries[item.text()]) if item else (None, None)

    def show(self, name):
        spec = self.entries[name]
        if spec.get('path'):
            path = Path(spec['path'])
            self.help.setText((spec.get('help', '') + '\n' if spec.get('help') else '') + f'File: {path}')
            self.edit.setPlainText(path.read_text() if path.exists() else '')
            self.state.setText('file')
            self.reset_btn.setText('Reload from disk')
            return
        self.reset_btn.setText('Reset to default')
        self.help.setText(spec.get('help', ''))
        cur = prompts.get(self.win.cfg, spec.get('key', name), spec['default'])
        self.edit.setPlainText(cur)
        self.state.setText('edited' if cur != spec['default'] else 'default')

    def save(self):
        name, spec = self.current()
        if not spec:
            return
        if spec.get('path'):
            Path(spec['path']).write_text(self.edit.toPlainText())
            self.state.setText('saved')
            self.win.say(f'saved {spec["path"]}')
            return
        prompts.save(self.win.cfg, spec.get('key', name), self.edit.toPlainText())
        self.state.setText('saved' if self.edit.toPlainText() != spec['default'] else 'default')
        self.win.say(f'saved {name}')

    def reset(self):
        name, spec = self.current()
        if not spec:
            return
        if spec.get('path'):
            self.show(name)
            return
        if QMessageBox.question(self, 'Reset', f'Go back to the default {name}? Your edits are lost.') == QMessageBox.Yes:
            prompts.reset(self.win.cfg, spec.get('key', name))
            self.show(name)


class Settings(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle('Settings')
        self.resize(1100, 760)
        tabs = QTabWidget()
        tabs.addTab(self.general(), 'GENERAL')
        tabs.addTab(self.queue(), 'QUEUE')
        tabs.addTab(self.key(), 'TERRA KEY')
        templates = {TEMPLATE_NAMES[k]: {'key': f'template-{k}', 'default': core.default_template(k),
                                         'help': 'What a new draft starts from when you pick this verdict. '
                                                 '{handle} becomes your handle. Lines in (parentheses) are reminders: '
                                                 'the draft can\'t be queued until they are replaced.'}
                     for k in core.TEMPLATE_KINDS}
        tabs.addTab(Editor(win, templates), 'TEMPLATES')
        if win.hooks.prompts:
            tabs.addTab(Editor(win, dict(sorted(win.hooks.prompts.items()))), 'PROMPTS')
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)

    def general(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        cfg = self.win.cfg
        handle = QLineEdit(cfg.get('handle', ''))
        terminal = QLineEdit(cfg.get('terminal', ''), placeholderText='auto: $TERMINAL, kitty, foot, alacritty, ...')
        form.addRow('Your Slack handle', handle)
        form.addRow('Terminal for Run', terminal)
        extra = {}
        for spec in self.win.hooks.settings:
            box = QComboBox()
            box.addItems([str(c) for c in spec['choices']])
            box.setCurrentText(str(cfg.get(spec['key'], spec.get('default', spec['choices'][0]))))
            box.setToolTip(spec.get('help', ''))
            form.addRow(spec['label'], box)
            extra[spec['key']] = box
        lay.addLayout(form)
        lay.addWidget(muted(f'Saved to {core.CONFIG_FILE}. The handle goes at the end of every note to a player.'))
        save = QPushButton('Save')
        save.setObjectName('primary')
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(save)
        lay.addLayout(row)
        lay.addStretch()

        def do_save():
            core.set_config(cfg, 'handle', handle.text().strip())
            core.set_config(cfg, 'terminal', terminal.text().strip())
            for key, box in extra.items():
                core.set_config(cfg, key, box.currentText())
            self.win.say('settings saved')
        save.clicked.connect(do_save)
        return w

    def queue(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(muted('Columns in the queue table. Changes show straight away. Drag a column\'s edge in the table '
                            'to resize it; it keeps that width until you reset.'))
        cfg = self.win.cfg

        def toggle():
            cfg['queue_columns'] = [c for c, box in boxes.items() if box.isChecked()]
            core.set_config(cfg, 'queue_columns', cfg['queue_columns'])
            self.win.columns_changed()

        boxes = {}
        for name in core.QUEUE_COLUMNS:
            box = QCheckBox('ship (always shown)' if name == 'ship' else name)
            box.setChecked(name in cfg['queue_columns'] or name == 'ship')
            box.setEnabled(name != 'ship')
            box.toggled.connect(toggle)
            boxes[name] = box
            lay.addWidget(box)
        reset = QPushButton('Reset column widths')
        reset.clicked.connect(lambda: (self.win.columns_changed(reset_widths=True), self.win.say('column widths reset')))
        row = QHBoxLayout()
        row.addWidget(reset)
        row.addStretch()
        lay.addLayout(row)
        lay.addStretch()
        return w

    def key(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        state = QLabel()
        lay.addWidget(state)
        lay.addWidget(muted(KEY_HELP))
        field = QLineEdit(placeholderText='paste your Terra key')
        field.setEchoMode(QLineEdit.Password)
        save, remove = QPushButton('Save key'), QPushButton('Remove key')
        save.setObjectName('primary')
        row = QHBoxLayout()
        row.addWidget(field, 1)
        row.addWidget(save)
        row.addWidget(remove)
        lay.addLayout(row)
        lay.addStretch()

        def show_state():
            s = core.token_state()
            text = {'file': (GREEN, 'A key is saved.'),
                    'env': (GREEN, 'The TERRA_TOKEN environment variable is set; it is used instead of the saved key.'),
                    'none': (PINK, 'No key saved yet: the queue and review pages can\'t load without one.')}[s]
            state.setText(f'<b style="color:{text[0]}">{text[1]}</b>')
            remove.setEnabled(s == 'file')

        def do_save():
            if not field.text().strip():
                return
            core.save_token(field.text())
            field.clear()
            show_state()
            self.win.key_changed()

        def do_remove():
            if QMessageBox.question(self, 'Remove key', 'Delete the saved Terra key from this computer?') == QMessageBox.Yes:
                core.remove_token()
                show_state()
                self.win.key_changed()

        save.clicked.connect(do_save)
        field.returnPressed.connect(do_save)
        remove.clicked.connect(do_remove)
        show_state()
        return w
