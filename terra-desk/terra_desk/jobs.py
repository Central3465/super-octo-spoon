"""Background jobs: the commands terra-desk runs for you (Set up, Run, dumps, the send runner, test-step commands),
each with its output kept so the web page can show it in the Jobs panel. This replaces the Linux-only pty terminal.
"""
import re
import subprocess
import threading
import time
from pathlib import Path

from . import system

ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(\x07|\x1b\\)|\x1b[@-_]')
MAX_LOG = 400_000           # characters kept per job (the start is dropped past this)


def clean(text):
    """Terminal output as plain text: colour codes dropped, \\r progress lines collapsed to their last state."""
    text = ANSI.sub('', text).replace('\r\n', '\n')
    return '\n'.join(line.rsplit('\r', 1)[-1] for line in text.split('\n'))


class Job:
    def __init__(self, jid, argv, title, cwd=None, env=None, done=None, stdout_file=None):
        self.id, self.argv, self.title, self.cwd, self.env = jid, [str(a) for a in argv], title, cwd, env
        self.done, self.stdout_file = done, stdout_file
        self.log, self.code, self.started, self.ended = '', None, time.time(), None
        self.lock = threading.Lock()
        self.proc = None

    @property
    def running(self):
        return self.code is None

    def add(self, text):
        with self.lock:
            self.log = (self.log + text)[-MAX_LOG:]

    def start(self):
        out = open(self.stdout_file, 'wb') if self.stdout_file else subprocess.PIPE  # noqa: SIM115 - closed in _wait
        try:
            self.proc = subprocess.Popen(self.argv, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE, stdout=out,
                                         stderr=subprocess.PIPE if self.stdout_file else subprocess.STDOUT)
        except OSError as e:
            if self.stdout_file:
                out.close()
            self.add(f'could not start {self.argv[0]}: {e}\n')
            self.finish(127)
            return self
        stream = self.proc.stderr if self.stdout_file else self.proc.stdout
        threading.Thread(target=self._read, args=(stream, out), daemon=True).start()
        return self

    def _read(self, stream, out):
        dec = __import__('codecs').getincrementaldecoder('utf-8')('replace')
        while True:
            chunk = stream.read1(4096) if hasattr(stream, 'read1') else stream.read(4096)
            if not chunk:
                break
            self.add(dec.decode(chunk))
        self.add(dec.decode(b'', final=True))
        code = self.proc.wait()
        if self.stdout_file:
            out.close()
        self.finish(code)

    def finish(self, code):
        self.code, self.ended = code, time.time()
        self.add(f'\n[exited {code}]\n')
        if self.done:
            try:
                self.done(code)
            except Exception as e:  # noqa: BLE001 - shown in the log
                self.add(f'[after it finished: {e}]\n')

    def write(self, text):
        if self.proc and self.running and self.proc.stdin:
            try:
                self.proc.stdin.write(text.encode('utf-8'))
                self.proc.stdin.flush()
                self.add(text)
            except OSError:
                pass

    def stop(self):
        if self.proc and self.running:
            self.proc.terminate()
            threading.Timer(5, lambda: self.running and self.proc.kill()).start()

    def info(self):
        return {'id': self.id, 'title': self.title, 'running': self.running, 'code': self.code,
                'started': self.started, 'ended': self.ended, 'size': len(self.log)}


class Jobs:
    def __init__(self):
        self.jobs, self.next_id, self.lock = {}, 1, threading.Lock()

    def run(self, argv, title, cwd=None, env=None, done=None, stdout_file=None):
        with self.lock:
            jid = self.next_id
            self.next_id += 1
            job = Job(jid, argv, title, Path(cwd) if cwd else None, env, done, stdout_file)
            self.jobs[jid] = job
        job.add(f'$ {subprocess.list2cmdline(job.argv) if system.WINDOWS else " ".join(job.argv)}\n')
        return job.start()

    def get(self, jid):
        return self.jobs.get(int(jid))

    def close(self, jid):
        job = self.jobs.get(int(jid))
        if job and not job.running:
            del self.jobs[int(jid)]

    def list(self):
        return [j.info() for j in self.jobs.values()]

    def stop_all(self):
        for j in list(self.jobs.values()):
            j.stop()
