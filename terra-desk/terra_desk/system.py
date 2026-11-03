"""Everything that differs between Windows and Linux/macOS: where files live, how programs are found and started,
whether a process is still running, and how a player's build is run in a sandbox.

terra-desk is built for Windows first (the installer bundles Python, Git and ffmpeg), and still runs from source on
Linux and macOS.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

WINDOWS = os.name == 'nt'
FROZEN = bool(getattr(sys, 'frozen', False))          # running from the installed terra-desk.exe
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010


def app_root():
    """The folder with the bundled tools/, web/ and fonts (inside the exe's unpack folder when frozen)."""
    if FROZEN:
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def config_dir():
    """terra-desk's settings: %APPDATA%\\terra-desk on Windows, ~/.config/terra-desk elsewhere."""
    if WINDOWS:
        return Path(os.environ.get('APPDATA') or Path.home() / 'AppData/Roaming') / 'terra-desk'
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'terra-desk'


def token_file():
    """Where the Terra key is kept (shared with tools/terra-api.py)."""
    if WINDOWS:
        return Path(os.environ.get('APPDATA') or Path.home() / 'AppData/Roaming') / 'terra' / 'token'
    return Path.home() / '.config/terra/token'


def default_workspace():
    return Path.home() / ('Documents/terra-desk' if WINDOWS else 'terra-desk')


def quiet_subprocesses():
    """On Windows a windowed app that starts git, ffmpeg or Python flashes a console window for each one. Make
    every subprocess start without one unless the caller asked for something else."""
    if not WINDOWS or getattr(subprocess.Popen, '_terra_quiet', False):
        return
    original = subprocess.Popen.__init__

    def init(self, *args, **kwargs):
        if not kwargs.get('creationflags'):
            kwargs['creationflags'] = CREATE_NO_WINDOW
        original(self, *args, **kwargs)
    subprocess.Popen.__init__ = init
    subprocess.Popen._terra_quiet = True


def add_bundled_tools_to_path():
    """The installer ships MinGit and ffmpeg next to terra-desk.exe; put them first on PATH so they're used
    even when the user has nothing installed. ~/.local/bin is added on Linux (pipx tools, claude)."""
    parts = os.environ.get('PATH', '').split(os.pathsep)
    extra = []
    if FROZEN:
        here = Path(sys.executable).parent
        extra += [here / 'mingit' / 'cmd', here / 'mingit' / 'mingw64' / 'bin', here / 'ffmpeg']
    if not WINDOWS:
        extra.append(Path.home() / '.local/bin')
    for d in extra:
        if d.is_dir() and str(d) not in parts:
            parts.insert(0, str(d))
    os.environ['PATH'] = os.pathsep.join(parts)


def tool_argv(script, *args):
    """The command line that runs one of the bundled tools (terra-api.py, terra-write.py).

    From source it's this Python in UTF-8 mode; from the installed exe it's the exe itself with --tool, since there
    is no separate python.exe."""
    script = Path(script)
    if FROZEN:
        cli = Path(sys.executable).with_name('terra-desk-cli.exe')    # the console build: output goes to the pipe
        return [str(cli if cli.exists() else sys.executable), '--tool', str(script), *args]
    return [sys.executable, '-X', 'utf8', '-I', str(script), *args]


def tool_env(extra=None):
    """Environment for the tools: UTF-8 everywhere (Windows defaults to the ANSI code page otherwise)."""
    return {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8', **(extra or {})}


def pid_alive(pid):
    """Is process `pid` still running? (os.kill(pid, 0) on Windows would send it Ctrl+C, so not that.)"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if WINDOWS:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        handle = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259             # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def open_path(path):
    """Open a file or folder with the system's default program (Explorer for folders)."""
    path = str(path)
    if WINDOWS:
        os.startfile(path)  # noqa: S606 - the user's own file
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', path])
    else:
        subprocess.Popen(['xdg-open', path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def which(name):
    return shutil.which(name)


# ---- running a player's build ----

def windows_sandbox_available():
    """Windows Sandbox (Windows 10/11 Pro, Enterprise or Education, turned on under "Windows features")."""
    if not WINDOWS:
        return False
    root = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    return (root / 'System32' / 'WindowsSandbox.exe').exists()


def wsb_config(folder, command, network=False):
    """A Windows Sandbox config that maps `folder` (writable) to C:\\work, turns networking, the clipboard and the
    microphone off, and starts `command` (a path inside C:\\work) once the sandbox is up."""
    from xml.sax.saxutils import escape
    return f"""<Configuration>
  <Networking>{'Default' if network else 'Disable'}</Networking>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <AudioInput>Disable</AudioInput>
  <VideoInput>Disable</VideoInput>
  <PrinterRedirection>Disable</PrinterRedirection>
  <ProtectedClient>Enable</ProtectedClient>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>{escape(str(folder))}</HostFolder>
      <SandboxFolder>C:\\work</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>{escape(command)}</Command>
  </LogonCommand>
</Configuration>
"""


def sandbox_launch(folder, exe, how, sandbox_dir):
    """Start a runnable from a release folder. Returns (what happened, argv or None).

    Windows: .exe/.msi/.bat files go into Windows Sandbox (no network, only the release folder visible), started
    through a generated .wsb file. Without Windows Sandbox nothing is started unless `how` is 'direct'.
    Linux: tools/sandbox-run (bubblewrap) as before."""
    folder, exe = Path(folder), Path(exe)
    rel = exe.relative_to(folder)
    if WINDOWS:
        if how == 'direct':
            return 'started outside any sandbox', [str(exe)]
        inner = 'C:\\work\\' + str(rel).replace('/', '\\')
        if exe.suffix.lower() == '.msi':
            command = f'msiexec /i "{inner}"'
        elif exe.suffix.lower() in ('.bat', '.cmd'):
            command = f'cmd /c start "" /d C:\\work "{inner}"'
        elif exe.suffix.lower() == '.jar':
            command = f'cmd /c start "" /d C:\\work java -jar "{inner}"'
        else:
            command = f'cmd /c start "" /d "{str(Path(inner).parent)}" "{inner}"'
        sandbox_dir = Path(sandbox_dir)
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        wsb = sandbox_dir / f'{exe.stem}.wsb'
        wsb.write_text(wsb_config(folder, command), encoding='utf-8')
        return f'opening Windows Sandbox with {rel}', [str(wsb)]
    return None, None


def bash():
    """A real bash for test-step commands: Git's (bundled MinGit or Git for Windows) on Windows, never WSL's."""
    if not WINDOWS:
        return shutil.which('bash')
    roots = []
    if FROZEN:
        roots.append(Path(sys.executable).parent / 'mingit')
    for env in ('ProgramFiles', 'ProgramW6432', 'LOCALAPPDATA'):
        if os.environ.get(env):
            roots += [Path(os.environ[env]) / 'Git', Path(os.environ[env]) / 'Programs' / 'Git']
    for r in roots:
        for sub in ('usr/bin/bash.exe', 'bin/bash.exe'):
            if (r / sub).exists():
                return str(r / sub)
    return None


def shell_argv(code):
    """Run a test-step command: bash when there is one, else PowerShell (Windows) or sh."""
    b = bash()
    if b:
        return [b, '-c', code]
    if WINDOWS:
        return ['powershell', '-NoProfile', '-Command', code]
    return ['sh', '-c', code]


def edge():
    """Microsoft Edge, for opening terra-desk in its own app window (no tabs or address bar)."""
    if not WINDOWS:
        return None
    for env in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA'):
        base = os.environ.get(env)
        if base and (Path(base) / 'Microsoft/Edge/Application/msedge.exe').exists():
            return str(Path(base) / 'Microsoft/Edge/Application/msedge.exe')
    return shutil.which('msedge')


def open_window(url, mode='app'):
    """Show the page: an Edge/Chrome app window when mode is 'app' and one is found, else the default browser."""
    import webbrowser
    if mode == 'app':
        browser = edge() or shutil.which('google-chrome') or shutil.which('chromium') or shutil.which('chrome')
        if browser:
            try:
                subprocess.Popen([browser, f'--app={url}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                pass
    webbrowser.open(url)
