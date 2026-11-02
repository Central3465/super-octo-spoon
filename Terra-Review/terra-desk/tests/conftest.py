import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SID = '11111111-2222-3333-4444-555555555555'
SID2 = '66666666-7777-8888-9999-000000000000'


def queue_item(sid=SID, title='Space Game', user='sam', week=9, platforms=('windows',), claimed=None, hours=10):
    return {'id': sid, 'title': title, 'username': user, 'weekNumber': week, 'queuedAt': '2026-10-20T10:00:00.000Z',
            'seconds': hours * 3600, 'platforms': list(platforms), 'claimedBy': claimed, 'claimedByMe': False,
            'stage': 'review'}


def page_data(sid=SID, danger=True, ai_seconds=0, declaration='none given'):
    return {
        'shipment': {'id': sid, 'title': 'Space Game', 'notes': 'shoot asteroids\nwith **lasers**', 'aiDeclaration': declaration,
                     'stage': 'review', 'status': 'in_review', 'weekNumber': 9, 'platforms': ['windows'],
                     'hackatimeSeconds': 36000, 'aiSeconds': ai_seconds, 'submittedAt': '2026-10-25T10:00:00.000Z',
                     'changesRounds': 1, 'evidenceVersion': 1},
        'project': {'title': 'Space Game', 'description': 'a game', 'codeUrl': 'https://github.com/sam/space',
                    'demoUrl': 'https://github.com/sam/space/releases/tag/v1', 'screenshotUrl': 'https://cdn.example/shot.png',
                    'hackatimeProjectNames': ['space'], 'branch': 'main', 'isUpdate': True},
        'maker': {'username': 'sam', 'displayName': 'Sam', 'githubLogin': 'sam'},
        'devlogs': [{'id': 'd1', 'title': 'lasers', 'body': 'made the **lasers** work', 'creditedSeconds': 36000,
                     'createdAt': '2026-10-24T10:00:00.000Z', 'images': [{'url': 'https://cdn.example/dl1.png'}]}],
        'votes': {'round': 1, 'averageStars': 5.5, 'suggestedMultiplier': 1.14,
                  'list': [{'stars': 6, 'verdict': 'accept', 'justification': 'fun', 'createdAt': '2026-10-26T10:00:00.000Z'}]},
        'decisions': [{'decision': 'changes', 'reviewer': 'alex', 'noteToMaker': 'add a release please',
                       'createdAt': '2026-10-21T10:00:00.000Z'}],
        'flags': [{'severity': 'danger', 'title': '2 files not in the repo'}] if danger else [],
        'fraudSuggestion': {'seconds': 7200, 'reason': 'too fast'},
        'evidence': {'data': {
            'time': {'totalSeconds': 36000, 'aiSeconds': ai_seconds},
            'commits': [{'sha': 'abcdef1234567', 'url': 'https://github.com/sam/space/commit/abcdef1', 'message': 'lasers\nmore',
                         'additions': 120, 'deletions': 3, 'authorEmail': 'sam@private.example', 'committedAt': '2026-10-24T09:00:00Z'}],
            'rows': [{'sha': 'abcdef1234567', 'windowDevlogId': 'd1', 'time': {'totalSeconds': 3600}}],
            'files': [{'path': 'main.py', 'status': 'head', 'seconds': 30000},
                      {'path': 'old.py', 'status': 'none', 'seconds': 600}],
            'unifiedProjects': [{'program': 'Siege', 'hours': 9, 'codeUrl': 'https://github.com/sam/space', 'approvedAt': '2026-09-01T00:00:00Z'}]}},
        'waterPaid': 10.0,
    }


def prep_facts(demo='https://github.com/sam/space/releases/tag/v1', assets=('space.exe',), readme_lines=20, run_section=True,
               ai_config=(), warnings=()):
    return {
        'ship': {'id': SID, 'platforms': ['windows'], 'demo_url': demo, 'submitted': '2026-10-25T10:00:00.000Z'},
        'links': {'demo': {'url': demo, 'status': 200, 'kind': 'releases page'}, 'pypi': None, 'npm': None},
        'checklist': {'web_demo': None, 'release_build': [f'v1: {a}' for a in assets if a.endswith('.exe')], 'package': []},
        'readme': {'lines': readme_lines, 'run_section': run_section, 'ai_section': []} if readme_lines else None,
        'firmware': None,
        'files': {'ai_config': list(ai_config)},
        'releases': [{'tag': 'v1', 'after_ship': False, 'assets': [
            {'name': a, 'size': 2 * 1048576, 'kind': 'executable/installer' if a.endswith('.exe') else
             'source file only, not an executable' if a.endswith('.py') else 'other'} for a in assets]}],
        'demo_release_tag': 'v1',
        'warnings': list(warnings),
    }


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    conf = tmp_path / 'config.toml'
    conf.write_text(f'workspace = "{tmp_path / "ws"}"\nhandle = "@tester"\nprompts_dir = "{tmp_path / "prompts"}"\n'
                    f'ship_globs = ["{tmp_path / "pages-*"}"]\n')
    monkeypatch.setenv('TERRA_DESK_CONFIG', str(conf))
    import importlib
    from terra_desk import core
    importlib.reload(core)
    return core.load_config()
