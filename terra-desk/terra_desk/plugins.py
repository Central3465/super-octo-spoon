"""Plugins: small Python files that add to terra-desk without changing it.

List them in config.toml (`plugins = ["~/my-terra-plugin.py"]`). Each one defines `register(hooks, cfg)` and adds to
the lists below. See README.md for an example.
"""
import runpy
from pathlib import Path


class Hooks:
    def __init__(self):
        # extra "## Section" names that hold test steps, checked before the built-in "Test steps" and "Quick look"
        self.test_sections = []
        # functions (ship) -> text to show as test steps when a page has none of those sections
        self.test_fallbacks = []
        # a "## Section" name whose ``` block is a suggested draft (shown with a button to use it)
        self.suggested_section = None
        # functions (ship) -> list of folders that may hold that ship's downloaded release
        self.release_dirs = []
        # functions (ship) -> list of folders with screenshots (*.png) for that ship
        self.screenshot_dirs = []
        # (label, function(window, ship)) buttons on the Test tab, next to Set up and Run
        self.test_actions = []
        # (label, function(window, ship), when) buttons on the decision panel; when is 'no-draft', 'draft' or 'always'
        self.decision_actions = []
        # editable text a plugin uses (prompts, templates): name -> {'default': text, 'help': what the {placeholders} are}
        # or a file to edit in place: name -> {'path': file, 'help': ...}. Edit them under Settings…;
        # read them with prompts.get(cfg, name, default) (or just read the file)
        self.prompts = {}
        # choices shown under Settings… > General and saved in config.toml:
        # {'key': config key, 'label': text, 'choices': [values], 'default': value, 'help': tooltip}
        self.settings = []


def load(cfg):
    hooks = Hooks()
    errors = []
    for p in cfg.get('plugins') or []:
        path = Path(p).expanduser()
        try:
            mod = runpy.run_path(str(path), run_name='terra_desk_plugin')
            mod['register'](hooks, cfg)
        except Exception as e:  # noqa: BLE001 - shown in the window
            errors.append(f'{path.name}: {e}')
    return hooks, errors
