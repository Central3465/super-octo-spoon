# PyInstaller spec: builds dist/terra-desk/ with
#   terra-desk.exe      the app (no console window; it opens the page in its own window)
#   terra-desk-cli.exe  the same program with a console, used to run the bundled tools (their output goes to Jobs)
# Run from the terra-desk folder:  pyinstaller packaging/terra-desk.spec --noconfirm
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).parent          # noqa: F821 - SPECPATH is set by PyInstaller

datas = [
    (str(ROOT / 'terra_desk' / 'web'), 'terra_desk/web'),
    (str(ROOT / 'terra_desk' / 'fonts'), 'terra_desk/fonts'),
    (str(ROOT / 'tools' / 'terra-api.py'), 'tools'),
    (str(ROOT / 'tools' / 'terra-write.py'), 'tools'),
]
datas += collect_data_files('imageio_ffmpeg')     # a Windows ffmpeg.exe, used to compress review videos

# the tools are run with runpy, so PyInstaller can't see what they import: list it
hidden = ['argparse', 'ipaddress', 'zipfile', 'uuid', 'select', 'stat', 'shlex', 'ctypes', 'ctypes.wintypes',
          'urllib.request', 'urllib.error', 'urllib.parse', 'http.client', 'email', 'email.message', 'json', 'sqlite3',
          'tomllib', 'xml.sax.saxutils', 'webbrowser', 'imageio_ffmpeg', 'terra_desk.app']

a = Analysis([str(ROOT / 'packaging' / 'launch.py')], pathex=[str(ROOT)], datas=datas, hiddenimports=hidden,
             excludes=['tkinter', 'PySide6', 'PyQt5', 'PyQt6', 'numpy'], noarchive=False)
pyz = PYZ(a.pure)
options = [('X utf8', None, 'OPTION')]             # UTF-8 mode: text files and pipes are UTF-8, not the ANSI code page
icon = str(ROOT / 'packaging' / 'terra-desk.ico')
version = os.environ.get('TERRA_DESK_VERSION_FILE')

app = EXE(pyz, a.scripts, options, exclude_binaries=True, name='terra-desk', console=False, icon=icon,
          version=version, upx=False)
cli = EXE(pyz, a.scripts, options, exclude_binaries=True, name='terra-desk-cli', console=True, icon=icon,
          version=version, upx=False)
COLLECT(app, cli, a.binaries, a.datas, name='terra-desk', upx=False)
