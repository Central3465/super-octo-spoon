"""Editable prompts and templates, stored as files in ~/.config/terra-desk/prompts/<name>.md.

A file overrides the plugin's default; deleting it (Reset in the editor) goes back to the default.
"""
from pathlib import Path


def folder(cfg):
    d = Path(cfg.get('prompts_dir') or Path.home() / '.config/terra-desk/prompts').expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def get(cfg, name, default):
    f = folder(cfg) / f'{name}.md'
    return f.read_text() if f.exists() else default


def save(cfg, name, text):
    (folder(cfg) / f'{name}.md').write_text(text)


def reset(cfg, name):
    (folder(cfg) / f'{name}.md').unlink(missing_ok=True)


def fill(text, **values):
    """Replace {name} placeholders (plain replace, so other braces in the text are left alone)."""
    for k, v in values.items():
        text = text.replace('{' + k + '}', str(v))
    return text
