"""The Terra review guide: shown in the Guide panel, and as hints next to each part of the decision form."""

VERDICTS = [
    {'kind': 'approve', 'label': 'Approve', 'when': 'It works and meets the bar. Set the multiplier and any hours cut.'},
    {'kind': 'changes', 'label': 'Request changes', 'when': 'Something fixable stops it from working (see the clear reasons).'},
    {'kind': 'reject', 'label': 'Reject', 'when': "Not fixable: school or paid work, not the player's own project, "
                                                  'or high AI use (~80%+) and unpolished / slop.'},
    {'kind': 'move', 'label': 'Move review', 'when': "To fraud or an admin: if it's sus ╮(─ω─)╭, or it ran out of "
                                                     'request-changes rounds.'},
]

CHANGES_REASONS = [
    'Nothing runs from what was shipped: missing libraries or DLLs, a debug build, assets loaded from the wrong '
    "place, a build that can't find its own files.",
    "The demo link doesn't work: private, a placeholder, points at a different project, or uses a disallowed host.",
    "The code isn't published or isn't pushed: the repo or account 404s, or the claimed features aren't in it.",
    'A desktop app or CLI has no release build: only source, a source zip, or "install Python and run this file".',
    "The ship notes admit there's nothing to try yet.",
    'The README gives no way to run the project or find the demo at all.',
    "A reship didn't fix what the last reviewer asked for.",
]
CHANGES_DONT = [
    'Semi-incorrect platform selection: some platform-bound projects (Roblox games, Minecraft mods, browser '
    'extensions) are ambiguous about what they run on.',
]
CHANGES_EXAMPLE = ("specific thing you liked is really cool (^-^) projects don't need to be finished to ship, but what "
                   'you ship has to work, and right now problem:\n\n1. exact problem. exact fix\n2. exact problem. exact fix\n'
                   "3. etc.\n\nthen reship and we'll take another look!")

DEFLATION = [
    ('Fraud suggested a deflation', "Use fraud's number unless you find something else."),
    ("Code that isn't in the shipped version", 'Cut it: dropped features, lost files, work from after the ship window.'),
    ('Testing time', "Doesn't count."),
    ('Hours clearly above the visible effort, with nothing else to explain them', 'A light cut, scaled to the gap.'),
    ("AI that's disclosed but not tracked", 'Cut if significant or if it affects quality (is slop).'),
    ('Writing docs', 'Most of the time should be under ~1h, but look at what the docs contain.'),
]

MULTIPLIER_INTRO = ('Mostly based on effort: how much they clearly put in (polish, going past the basics, fixing their own '
                    'bugs, figuring out something hard), not just how cool the idea is. A simple project with a ton of care '
                    'can beat a big idea that is barely held together.')
MULTIPLIERS = [
    (0.8, 'barely any effort, just meets the bar'),
    (0.9, 'works, but very little effort shows (mostly AI, or super thin)'),
    (1.0, 'meh: the minimum to work, rough, not much past the basics'),
    (1.05, 'some effort past the basics, but rough or buggy'),
    (1.1, 'average: solid effort, works, some bugs'),
    (1.15, 'clearly more effort than average (polish, extra features, care)'),
    (1.2, 'a lot of effort shows, impressive'),
    (1.3, 'tons of effort and it shows everywhere, genuinely blows you away'),
    (1.5, '"I genuinely had no idea that was possible"'),
]
AI_NOTE = ('The more AI there is (tracked, or obvious AI work the declaration doesn\'t mention), the more signs of human '
           'effort and improvement it needs to get the same multiplier: their own fixes, design choices, things that are '
           "clearly not just the AI's first try. A little AI barely matters, but ~30%+ needs really clear human effort to "
           'even get average. AI images are allowed, but make the project lower quality.')

FIELDS = {
    'specific technical features': {
        'help': 'What it is, what it\'s made with, then a list of what it actually does. Only list things that are in the ship.',
        'example': 'e.g. "multiplayer with rollback, procedural maps, cloud saves"',
    },
    'airtable justification': {
        'help': 'If another reviewer read it, they should land on the same hours as you.',
        'points': ['what you tested, on what, and how it went (bugs you hit too)',
                   'commits/devlogs vs the hours, and why the hours make sense (or what you cut and why)',
                   'one short bit for every flag or "suspicious" warning, explaining it',
                   'AI use vs what they declared',
                   'their experience (beginner or not) if it matters for the hours'],
        'example': 'tested features on platform/method, what worked, bugs you hit. N commits match the N devlogs. flag is '
                   'what it actually is, e.g. an addon/template/web uploads. cut Xh for reason, e.g. dropped feature / docs '
                   'over 1h. ai note, beginner/experienced, hours make sense',
    },
    'note to player': {
        'help': 'The player sees this: be nice and specific.',
        'points': ['start with something specific you liked (quote their own wording if it\'s fun)',
                   'then the 2-3 things that matter most to improve, not a list of every nitpick',
                   'if you deflated, say why',
                   'end with some encouragement'],
        'example': 'specific thing you liked is really cool ( ≧ᗜ≦) improvement 1, and improvement 2 (maybe the fix if you '
                   'know it). i took Xh off since reason in plain words. keep it up!',
    },
    'what to change': {
        'help': 'The player sees this. One nice thing, then each problem with its exact fix.',
        'example': CHANGES_EXAMPLE,
    },
    'move note': {
        'help': 'For fraud or the admins, not the player: say what looks off and what you checked.',
    },
    'deflation reason': {
        'help': 'One line on why you cut hours. The player is told the same thing in plain words in the note.',
    },
    'danger flag acknowledgement': {
        'help': 'One line per danger flag on why it is fine (or what it actually is).',
    },
}


def guide():
    return {'verdicts': VERDICTS, 'changes_reasons': CHANGES_REASONS, 'changes_dont': CHANGES_DONT,
            'changes_example': CHANGES_EXAMPLE, 'deflation': [{'when': a, 'do': b} for a, b in DEFLATION],
            'multiplier_intro': MULTIPLIER_INTRO, 'multipliers': [{'value': v, 'text': t} for v, t in MULTIPLIERS],
            'ai_note': AI_NOTE, 'fields': FIELDS}
