# terra-desk

A desktop app for Terra reviewers: pull ships from the review queue, set them up for testing, run them in a sandbox, write your notes, record a video, fill in the review, and queue it. A runner sends queued reviews one at a time.

## What it does

- **The queue:** the left side is Terra's review queue, live (refreshed every 2 minutes, or `Ctrl+R`), oldest first, with week, player, hours, how long each has waited and platforms. Ships you've worked on are marked. It only reads; it never claims anything, and it never opens Terra review pages (opening one claims the ship).
- **Review page:** the middle shows the same things as the review page: the project, ship notes and AI declaration, hours and flags, votes, earlier decisions, devlogs with their commits, and the files Hackatime saw.
- **Set up:** clones the repo (plus a checkout of the exact ship commit) and downloads the release next to it. Nothing is run.
- **Run:** starts the release in `sandbox-run` (bubblewrap: no network, no home folder, only its own folder writable), in a new terminal. Windows `.exe` files run in Wine inside the sandbox. Demo links open in your browser.
- **Notes:** type what you saw. They're saved as you type.
- **Video:** attach a recording of your test. It's compressed to under 20 MiB with ffmpeg and uploaded with the approve.
- **Templates:** approve, request changes and reject skeletons with every required field. The app won't queue a draft that still has template text in `(parentheses)`.
- **Queue:** checks the draft against the live ship (stage, evidence version, flags, already sent) and puts it in the send queue. Approves go first.
- **Send:** run the runner yourself in a terminal: `tools/terra-write.py run --every 2`. It claims each ship right before sending it.

## Install

Needs Linux, Python 3.11+, PySide6, ffmpeg, git, bubblewrap and Wine (for Windows builds).

```
# Arch
sudo pacman -S pyside6 ffmpeg git bubblewrap wine
# Debian/Ubuntu
sudo apt install python3-pyside6.qtwidgets ffmpeg git bubblewrap wine
```

Everything you do (notes, drafts, videos, where Set up put things, skipped ships) is kept in one SQLite file, `terra-desk.db` in the workspace.

If you keep markdown notes on ships (`12-some-ship.md` files with the review URL in them), list their folders in `ship_globs`. Their text shows on the **Page** tab, and their notes and draft (a code block of `### Verdict`-style headings under `## Drafts`) are imported once, if you haven't written your own yet.

Start it:

```
./terra-desk
```

Then open **Settings…** (`Ctrl+,`): paste your Terra key under **Terra key** (it's saved to `~/.config/terra/token`, readable only by you) and set your Slack handle under **General** (it goes at the end of every note to a player). **Templates** holds what each verdict's draft starts from, and **Prompts** the plugins' prompts and your own documents.

The first start writes `~/.config/terra-desk/config.toml` (or the file in `TERRA_DESK_CONFIG`). Set the `workspace` folder there if you don't want `~/terra-desk`.

## Plugins

A plugin is a Python file that adds to terra-desk without changing it. List it in `config.toml`:

```
plugins = ["~/my-terra-plugin.py"]
```

It defines `register(hooks, cfg)` and adds to these lists:

- `hooks.test_sections`: extra `## Section` names that hold test steps in your imported pages.
- `hooks.test_fallbacks`: functions `(ship) -> text` to show when a page has no test steps section.
- `hooks.suggested_section`: a `## Section` name whose code block is a suggested draft. The decision panel offers a button to use it.
- `hooks.release_dirs`: functions `(ship) -> [folders]` where that ship's downloaded release may be, for the Run menu.
- `hooks.screenshot_dirs`: functions `(ship) -> [folders]` with screenshots to show under the test steps.
- `hooks.decision_actions`: `(label, function(window, ship), when)` buttons on the decision panel, where `when` is `"no-draft"`, `"draft"` or `"always"`.

A ship has `sid`, `title`, `username`, `week`, `num`, `item` (its queue row), `page_text`, `quick`, `notes`, `draft` and `fields`. For example, a button that copies the title:

```
def copy_url(window, ship):
    from PySide6.QtWidgets import QApplication
    QApplication.clipboard().setText(ship.title)

def register(hooks, cfg):
    hooks.decision_actions.append(("Copy title", copy_url, "always"))
```

## Terminal

Set up, the Run menu and the Run buttons on commands in test steps open in the terminal at the bottom of the window, one tab each (`Ctrl+\``, or **Terminal** in the toolbar, shows and hides it). It's a real terminal: colours, prompts, Ctrl+C, Ctrl+Shift+C/V to copy and paste, the wheel to scroll back. Programs that open their own windows still get their own window. **Open outside** runs the same command again in your own terminal (the `terminal` setting).

It uses [pyte](https://github.com/selectel/pyte) (LGPL-3.0) and [wcwidth](https://github.com/jquast/wcwidth) (MIT), bundled in `terra_desk/vendor/` with their licenses, so there's nothing to install; if pyte is installed on the system, that copy is used instead.

## Keys

`J`/`K` move through ships, `Ctrl+S` saves notes, `Ctrl+Enter` queues the draft, `Ctrl+R` refreshes from Terra, `F5` reloads the files, `Ctrl+,` opens Settings, `Ctrl+\`` shows or hides the terminal.

## Look

The look is borrowed from [Fluxion](https://cloveian.github.io/fluxion/)'s page. Its fonts, Archivo and IBM Plex Mono, come bundled in `terra_desk/fonts/` under the SIL Open Font License (the license files are next to them).
