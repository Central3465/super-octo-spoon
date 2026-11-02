"""Automatic checks for a ship: ship-rule problems, how to run it, and AI signs.

The facts come from `terra-api.py prep` (a read-only clone plus GitHub, demo and package lookups) and the
review-page data. Nothing from the player's repo is run. After Set up downloads a release, `binary_problems`
looks at the files statically (objdump, no running).
"""
import re
import shutil
from html import unescape
import subprocess
from datetime import datetime, timezone
from pathlib import Path

RENDER_CUTOFF = datetime(2026, 10, 30, tzinfo=timezone.utc)   # Render demos are only allowed for ships made before this
DESKTOP = {'windows', 'linux', 'macos'}
# Windows DLLs that every PC has (or that come with the usual redistributables), so they aren't "missing"
SYSTEM_DLLS = re.compile(
    r'^(api-ms-win-.*|ext-ms-win-.*|kernel32|kernelbase|user32|gdi32|gdiplus|advapi32|shell32|ole32|oleaut32|ws2_32|'
    r'comdlg32|comctl32|winmm|imm32|version|shlwapi|msvcrt|ucrtbase|vcruntime140(_1)?|msvcp140(_1|_2|_atomic_wait|_codecvt_ids)?|'
    r'vcomp140|concrt140|opengl32|glu32|d3d\d+|d3dcompiler_\d+|dxgi|dwrite|d2d1|bcrypt|crypt32|setupapi|dwmapi|uxtheme|'
    r'winhttp|wininet|ntdll|hid|dinput8|xinput1_\d|xinput9_1_0|dsound|iphlpapi|secur32|normaliz|wldap32|mswsock|psapi|'
    r'powrprof|dbghelp|cfgmgr32|rpcrt4|userenv|shcore|propsys|avrt|mfplat|mfreadwrite|mf|mfuuid|ksuser|winusb|wtsapi32|'
    r'netapi32|oleacc|msimg32|usp10|wintrust|ncrypt|mpr|credui|dnsapi|winspool\.drv|winspool|msacm32|mmdevapi|'
    r'xaudio2_\d|x3daudio1_\d|vulkan-1|mscoree|mscoree|wsock32|imagehlp|sechost|combase|windowscodecs|urlmon|'
    r'shfolder|oledlg|msvfw32|avifil32|esent|cryptbase|bthprops\.cpl|bluetoothapis|setupapi|devobj|resutils|'
    r'd3d9|d3d10|d3d10_1|d3d11|d3d12|ddraw|uiautomationcore)\.dll$', re.I)
DEBUG_DLLS = re.compile(r'^(msvcp140d|vcruntime140d|vcruntime140_1d|ucrtbased|concrt140d)\.dll$', re.I)


def item(level, text):
    return {'level': level, 'text': text}


def analyze(F, page=None):
    """F: terra-api.py prep --json output. page: the review-page data. Returns {'rules', 'run', 'ai', 'level'}."""
    page = page or {}
    s, p = page.get('shipment') or {}, page.get('project') or {}
    ship = F.get('ship') or {}
    plats = set(ship.get('platforms') or s.get('platforms') or [])
    demo = ship.get('demo_url') or p.get('demoUrl') or ''
    links = F.get('links') or {}
    dm = links.get('demo') or {}
    cl = F.get('checklist') or {}
    readme = F.get('readme')
    fw = F.get('firmware')
    rels = F.get('releases') or []
    rules, ai = [], []

    # ---- shipped properly? ----
    submitted = ship.get('submitted') or s.get('submittedAt')
    try:
        made = datetime.fromisoformat(submitted.replace('Z', '+00:00')) if submitted else None
    except ValueError:
        made = None
    low = demo.lower()
    if not demo:
        rules.append(item('bad', 'no demo link'))
    elif re.search(r'localhost|127\.0\.0\.1|0\.0\.0\.0|192\.168\.|\.local\b', low):
        rules.append(item('bad', f'the demo link is a local address: {demo}'))
    elif re.search(r'github\.dev|app\.github\.dev|codespaces', low):
        rules.append(item('bad', 'the demo link is a Codespace, only the owner can open it'))
    elif 'streamlit.app' in low:
        rules.append(item('bad', 'Streamlit isn\'t allowed as a demo host'))
    elif re.search(r'drive\.google\.com|docs\.google\.com/file', low):
        rules.append(item('bad', 'Google Drive isn\'t allowed for demos or demo videos'))
    elif 'onrender.com' in low:
        rules.append(item('bad' if made and made >= RENDER_CUTOFF else 'ok',
                          'Render demo: not allowed for ships made after Oct 30' if made and made >= RENDER_CUTOFF
                          else 'Render demo (fine, the ship is from before the Oct 30 rule)'))
    status = dm.get('status')
    if demo and status and status >= 400:
        rules.append(item('bad', f'the demo link returns {status}'))
    elif demo and status is None and dm.get('error'):
        rules.append(item('warn', f"couldn't reach the demo: {dm['error']}"))

    web_ok = bool(cl.get('web_demo'))
    builds = cl.get('release_build') or []
    packages = cl.get('package') or []
    if fw:
        if fw.get('videos') or fw.get('sims'):
            rules.append(item('ok', 'firmware with a video or sim: ' + ((fw.get('videos') or fw.get('sims'))[0])))
        else:
            rules.append(item('bad', 'firmware (' + ', '.join(fw.get('signs') or []) + ') with no video of it running and no web sim'))
    elif web_ok:
        rules.append(item('ok', f'web demo answers ({demo})'))
    elif builds or packages:
        rules.append(item('ok', 'release build: ' + ', '.join((builds + packages)[:3])))
    elif plats & DESKTOP or not plats:
        rules.append(item('bad', 'no web demo, release build or package (desktop apps, CLIs and games need one)'))
    only_source = rels and all(all((a.get('kind') or '').startswith('source file') for a in r.get('assets') or [])
                               for r in rels if r.get('assets'))
    if only_source and any(r.get('assets') for r in rels):
        rules.append(item('bad', 'the release only has source files, not a build'))

    if readme is None:
        rules.append(item('bad', 'no README'))
    else:
        if not readme.get('run_section'):
            rules.append(item('warn', 'the README has no install / run / how to play section'))
        if (readme.get('lines') or 0) < 4:
            rules.append(item('warn', f"the README is only {readme.get('lines')} lines"))
    for w in F.get('warnings') or []:
        if re.search(r'not (been )?pushed|never committed|no commits|404|private|empty repo|not found', w, re.I):
            rules.append(item('warn', w))

    # ---- AI signs ----
    files = F.get('files') or {}
    if files.get('ai_config'):
        ai.append(item('warn', 'AI tool files in the repo: ' + ', '.join(files['ai_config'][:5])))
    if readme and readme.get('ai_section'):
        ai.append(item('ok', 'the README has an AI section: ' + ', '.join(readme['ai_section'][:2])))
    decl = (s.get('aiDeclaration') or '').strip().lower()
    ai_h = (s.get('aiSeconds') or 0) / 3600
    total_h = (s.get('hackatimeSeconds') or 0) / 3600
    if ai_h >= 0.1 and decl in ('', 'none', 'no', 'n/a', 'none given', 'nothing', 'no ai', 'not used'):
        ai.append(item('warn', f'{ai_h:.2f}h of AI time tracked, but the declaration says "{s.get("aiDeclaration") or "none"}"'))
    elif total_h and ai_h / total_h >= 0.5:
        ai.append(item('warn', f'{ai_h / total_h:.0%} of the time is AI-tracked (higher bar)'))
    elif ai_h:
        ai.append(item('ok', f'{ai_h:.2f}h AI tracked ({ai_h / total_h:.0%} of {total_h:.1f}h)' if total_h else f'{ai_h:.2f}h AI tracked'))
    for w in F.get('warnings') or []:
        if re.search(r'300\+ added lines|big paste|copied/vendored', w, re.I):
            ai.append(item('warn', w))

    level = 'bad' if any(x['level'] == 'bad' for x in rules) else 'warn' if any(x['level'] == 'warn' for x in rules + ai) else 'ok'
    return {'rules': rules, 'run': run_plan(F, demo, web_ok, plats), 'ai': ai, 'level': level,
            'other': [w for w in F.get('warnings') or []
                      if not any(w == x['text'] for x in rules + ai)]}


ITCH_ICONS = {'windows8': 'windows', 'tux': 'linux', 'apple': 'macos', 'android': 'android'}


def itch_facts(html):
    """What an itch.io game page offers: {'browser', 'downloads': [{'name', 'size', 'platforms'}], 'password',
    'price'} (price is None when it's free or "name your own price")."""
    downloads = []
    for m in re.finditer(r'<div class="upload_name"><strong title="([^"]*)" class="name">.*?</strong>(.*?)</div>', html, re.S):
        size = re.search(r'class="file_size"><span>([^<]+)</span>', m.group(2))
        platforms = [ITCH_ICONS.get(x, x) for x in re.findall(r'class="icon icon-([\w-]+)"', m.group(2))]
        downloads.append({'name': unescape(m.group(1)), 'size': size.group(1) if size else '',
                          'platforms': platforms})
    price = re.search(r'class="dollars[^"]*"[^>]*>([^<]+)<', html)
    free = 'Name your own price' in html or 'class="download_btn' in html
    return {'browser': bool(re.search(r'html(?:-classic)?\.itch\.zone/html/\d+', html)), 'downloads': downloads,
            'password': bool(re.search(r'password_form|This page is protected|class="password', html)),
            'price': None if free or not price else price.group(1).strip()}


def itch_plan(facts):
    """Checks for an itch.io demo page."""
    if not facts:
        return [item('warn', 'itch.io: the page wasn\'t checked')]
    if facts.get('error'):
        return [item('warn', f"itch.io: couldn't load the page ({facts['error']})")]
    if facts.get('password'):
        return [item('bad', 'itch.io: the page is password-protected, players and reviewers can\'t get in')]
    out = []
    if facts.get('price'):
        out.append(item('warn', f"itch.io: the game costs {facts['price']}"))
    downs = facts.get('downloads') or []
    listed = '; '.join(f"{d['name']} ({', '.join(d['platforms']) or 'no platform marked'}{', ' + d['size'] if d['size'] else ''})"
                       for d in downs[:4]) + ('; …' if len(downs) > 4 else '')
    if facts.get('browser'):
        out.append(item('ok', 'itch.io: plays in the browser' + (f', and has downloads: {listed}' if downs else '')))
    elif downs:
        runnable = any(set(d['platforms']) & {'windows', 'linux'} or not d['platforms'] for d in downs)
        out.append(item('ok' if runnable else 'warn',
                        f'itch.io: download only (no browser version): {listed}' if runnable else
                        f'itch.io: downloads only for {", ".join(sorted({p for d in downs for p in d["platforms"]}))}, '
                        f'nothing for Windows or Linux: {listed}'))
    else:
        out.append(item('bad', 'itch.io: the page has no browser game and no downloads'))
    return out


def run_plan(F, demo, web_ok, plats):
    """What the ship's build is and how terra-desk would run it."""
    rels = F.get('releases') or []
    tag = F.get('demo_release_tag')
    rel = next((r for r in rels if r.get('tag') == tag), None) or next((r for r in rels if not r.get('after_ship')), None) \
        or (rels[0] if rels else None)
    out = []
    if web_ok:
        out.append(item('ok', f'web: open {demo}'))
    if 'itch.io' in (demo or '').lower():
        out += itch_plan(F.get('itch'))
    if rel:
        for a in rel.get('assets') or []:
            n = (a.get('name') or '').lower()
            how = ('Windows: run it in Wine (sandboxed)' if n.endswith(('.exe', '.msi')) else
                   'Android: install it in Waydroid (adb install)' if n.endswith(('.apk', '.aab')) else
                   'Java: java -jar (sandboxed)' if n.endswith('.jar') else
                   'Linux AppImage (sandboxed)' if n.endswith('.appimage') else
                   'Debian package: extract it, don\'t install' if n.endswith('.deb') else
                   'macOS only, can\'t run here' if n.endswith(('.dmg', '.pkg', '.app.zip')) else
                   'iOS, can\'t run here' if n.endswith('.ipa') else
                   'Godot data file (needs the matching exe)' if n.endswith('.pck') else
                   'archive: Set up unzips it, then pick what to run' if n.endswith(('.zip', '.tar.gz', '.tgz', '.tar.xz', '.7z')) else
                   'firmware image, flash it to the board' if n.endswith(('.uf2', '.hex', '.bin')) else
                   'source file only, not a build' if (a.get('kind') or '').startswith('source') else
                   'macOS build, can\'t run here' if re.search(r'mac|darwin|osx', n) and 'no extension' in (a.get('kind') or '') else
                   'probably a Linux binary (sandboxed)' if 'no extension' in (a.get('kind') or '') else 'other file')
            out.append(item('ok' if 'can\'t' not in how and 'source' not in how else 'warn',
                            f"{rel.get('tag')}: {a.get('name')} ({_size(a.get('size'))}) → {how}"))
        if rel.get('after_ship'):
            out.append(item('warn', f"release {rel.get('tag')} was published after the ship"))
    for k in ('pypi', 'npm'):
        pk = (F.get('links') or {}).get(k)
        if pk and pk.get('found'):
            cmd = f"pipx run {pk['name']}" if k == 'pypi' else f"npx {pk['name']}"
            out.append(item('ok', f"{'PyPI' if k == 'pypi' else 'npm'} package {pk['name']} → {cmd} (in the sandbox)"))
    if not out:
        out.append(item('warn', 'no release, package or web demo to run'))
    return out


def _size(n):
    if not n:
        return '?'
    return f'{n / 1048576:.1f} MB' if n >= 1048576 else f'{n / 1024:.0f} KB'


def binary_problems(folder):
    """After a download: Windows exes that need DLLs which aren't next to them, Linux binaries missing libraries.
    Only reads the files (objdump), never runs them."""
    out = []
    folder = Path(folder)
    if not folder.is_dir() or not shutil.which('objdump'):
        return out
    present = {p.name.lower() for p in folder.rglob('*.dll')}
    sys_libs = None
    for p in folder.rglob('*'):
        if not p.is_file() or '.sandbox-home' in p.parts or p.stat().st_size < 1024:
            continue
        try:
            head = p.open('rb').read(4)
        except OSError:
            continue
        if p.suffix.lower() == '.exe' and head[:2] == b'MZ':
            r = subprocess.run(['objdump', '-p', str(p)], capture_output=True, text=True)
            dlls = re.findall(r'DLL Name:\s*(\S+)', r.stdout)
            debug = [d for d in dlls if DEBUG_DLLS.match(d)]
            missing = [d for d in dlls if not SYSTEM_DLLS.match(d) and not DEBUG_DLLS.match(d) and d.lower() not in present]
            if debug:
                out.append(item('bad', f'{p.name} is a Debug build: needs {", ".join(debug)}, which only exist where Visual Studio is installed'))
            if missing:
                out.append(item('bad', f'{p.name} needs {", ".join(missing)}, not in the download'))
            elif not debug:
                out.append(item('ok', f'{p.name}: every DLL it needs is a system one or included'))
        elif head == b'\x7fELF':
            r = subprocess.run(['objdump', '-p', str(p)], capture_output=True, text=True)
            needed = re.findall(r'NEEDED\s+(\S+)', r.stdout)
            if not needed:
                continue
            if sys_libs is None:
                lc = subprocess.run(['ldconfig', '-p'], capture_output=True, text=True).stdout
                sys_libs = set(re.findall(r'^\s*(\S+)\s', lc, re.M))
            local = {q.name for q in p.parent.glob('*.so*')}
            missing = [n for n in needed if n not in sys_libs and n not in local]
            interp = re.search(r'/nix/store/', p.read_bytes()[:4096].decode('latin1'))
            if interp:
                out.append(item('bad', f'{p.name} is built for NixOS (its loader is in /nix/store), it won\'t start on other Linux'))
            if missing:
                out.append(item('warn', f'{p.name} needs {", ".join(missing)}, not installed here and not in the download'))
    return out
