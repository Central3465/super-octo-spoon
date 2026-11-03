"""terra-desk: test and review Terra ships, straight from the review queue.

  terra-desk                  start it and open it in a window (or bring the running one forward)
  terra-desk --no-browser     start it without opening a window
  terra-desk --port 8765      use this port on 127.0.0.1
  terra-desk --tool tools/terra-write.py run --every 4    run one of the bundled tools (used by the app itself)
"""
import os
import runpy
import sys
from pathlib import Path


def utf8_mode():
    """Windows reads and writes text in the ANSI code page unless Python is in UTF-8 mode. From source, restart in it;
    the installed exe is built with it on."""
    if sys.flags.utf8_mode or getattr(sys, 'frozen', False) or os.environ.get('TERRA_DESK_NO_REEXEC'):
        return
    os.environ['TERRA_DESK_NO_REEXEC'] = '1'
    args = [sys.executable, '-X', 'utf8', '-m', 'terra_desk', *sys.argv[1:]]
    if os.name == 'nt':
        import subprocess
        raise SystemExit(subprocess.call(args))
    os.execv(sys.executable, args)


def windowed_output():
    """The windowed exe has no console: send output to a log file next to the settings instead of nowhere."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from terra_desk import system
    system.config_dir().mkdir(parents=True, exist_ok=True)
    log = open(system.config_dir() / 'terra-desk.log', 'a', encoding='utf-8', buffering=1)  # noqa: SIM115
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log


def run_tool(argv):
    from terra_desk import system
    script = Path(argv[0])
    if not script.is_absolute() and not script.exists():
        script = system.app_root() / 'tools' / script.name
    sys.argv = [str(script), *argv[1:]]
    sys.path.insert(0, str(script.parent))
    runpy.run_path(str(script), run_name='__main__')


def main():
    utf8_mode()
    windowed_output()
    from terra_desk import system
    system.quiet_subprocesses()
    system.add_bundled_tools_to_path()
    args = sys.argv[1:]
    if args[:1] == ['--tool']:
        return run_tool(args[1:])
    if args[:1] in (['-h'], ['--help']):
        print(__doc__)
        return 0
    port = None
    if '--port' in args:
        port = int(args[args.index('--port') + 1])
    from terra_desk import server
    return server.serve(open_browser='--no-browser' not in args, port=port)


if __name__ == '__main__':
    sys.exit(main())
