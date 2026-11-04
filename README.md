# terra-desk

A Windows app for Terra reviewers: pull ships from the review queue, set them up for testing, run them in a sandbox, write your notes, record a video, fill in the review, and queue it. A runner sends queued reviews one at a time.

It's a web app that runs on your own computer: a small server does the work (cloning, downloading, checking, sending), and the interface opens in its own window (Microsoft Edge in app mode) or a browser tab. Nothing is hosted anywhere else; your Terra key never leaves your PC.

## Install (Windows)

1. Download **`terra-desk-setup.exe`** from the [latest release](../../releases/latest) (or from the newest [build run](../../actions) under *Artifacts*).
2. Double-click it. It installs just for you, so there's no admin prompt, and starts terra-desk when it finishes.

That's all. The installer includes Python, Git (MinGit) and ffmpeg, so there's nothing else to install: no WSL, no Ubuntu packages, no PySide6. Start it again later from the Start menu (or the desktop shortcut). Uninstall from *Settings → Apps*. Your settings, key and workspace are kept.

The first time it starts, open **Settings…** (`Ctrl+,`):

- **Terra key:** on terra.hackclub.com, open the browser console (`F12`) and run `copy(localStorage.terra_auth_token)`, then paste it here. It's saved to `%APPDATA%\terra\token`.
- **General:** your Slack handle goes at the end of every note to a player.

Your work goes in `Documents\terra-desk` (the *workspace*). Settings are in `%APPDATA%\terra-desk\config.toml`.

### Running builds safely: Windows Sandbox

**Run** starts a player's `.exe` inside [Windows Sandbox](https://learn.microsoft.com/windows/security/application-security/application-isolation/windows-sandbox/): a throwaway Windows with no network, where only that release's folder is visible. Windows Sandbox comes with Windows 10/11 **Pro, Enterprise and Education**. Turn it on once: Start → *Turn Windows features on or off* → tick **Windows Sandbox** → restart.

On Windows Home there is no Windows Sandbox. terra-desk then asks before running a build directly on your PC. Only say yes for builds you trust, or use a VM.

## How it works

The ship list is on the left. Pick a ship and work through three steps:

1. **Review page:** an *at a glance* row first (hours tracked and credited, AI time, flags, fraud's suggested cut, checks, earlier decisions), then the same things Terra's review page shows (the project, ship notes and AI declaration, hours and flags, votes, earlier decisions, devlogs with their commits, the files Hackatime saw), plus automatic **checks** (demo link, release build, README, AI signs, and missing DLLs or Debug builds in a downloaded `.exe`).
2. **Test it:** **Set up** downloads the code (at the exact ship commit) and the release. Nothing runs until you press **Run**, which starts it in Windows Sandbox (see above). Write your **notes** here; they save as you type.
3. **Decide:** pick a verdict (**Approve**, **Request changes**, **Reject** or **Move review** to fraud or an admin) and fill in the review. Each field shows what to write from the review guide, with an example. For an approve, the multiplier is a list of the rubric's levels, the hours cut explains when to cut, and you can attach a test video (shrunk to under 20 MB): drop it on the box, copy it in File Explorer and press **Ctrl+V** (or **Paste copied video**), use **Use my latest recording** for the newest video in your Videos folder (where Snipping Tool and Xbox Game Bar save), or choose a file. The draft saves as you type. **Queue for sending** checks it against the live ship and adds it to the send queue. It won't queue while reminders in `(parentheses)` are left from the template.

Also:

- **Review guide** (top bar): the review rubric in a side panel next to your work: which verdict, clear reasons to request changes, when to cut hours, the multiplier levels, AI use, and how to write the technical features, the Airtable justification and the note to the player.
- **The list:** "Waiting for review" shows Terra's queue, oldest first, refreshed every 2 minutes (or **Refresh queue**). Other choices show your drafts, what's queued and sent, and what you skipped. **More filters** narrows it by week and platform. terra-desk only reads the queue: it never claims a ship unless you press **Claim this ship**, and it never opens Terra review pages (opening one claims the ship).
- **Sending:** the "waiting to send" button in the top bar (or **More → Send queued reviews**) starts the sender. It sends one review every few minutes, claiming each ship right before it goes out, and keeps going if you close the window. Pause or stop it from the same place; stopping gives back its claims. A review it couldn't send (the ship changed since you queued it, or the claim failed) shows as **failed to send** in the top bar, with the reason, **Try again** and **Remove**.
- **Activity:** Set up, Run, sending and test-step commands show their output here. **Open in a terminal** runs the same command in its own window.
- **More:** check every shown ship, save the whole queue to a file, reload your markdown pages, or **stash** all your work to start fresh and bring it back later.

## Keys

`J`/`K` move through ships, `1`/`2`/`3` switch between the steps, `Ctrl+S` saves notes and the draft, `Ctrl+Enter` queues the draft, `Ctrl+R` refreshes from Terra, `F5` reloads your markdown pages, `Ctrl+,` opens Settings, `Ctrl+`` shows or hides Activity.

## Settings file

`config.toml` (`%APPDATA%\terra-desk\config.toml` on Windows, `~/.config/terra-desk/config.toml` elsewhere, or the file in `TERRA_DESK_CONFIG`) is written on the first start. Things you might change there:

- `workspace`: where the database, clones, release downloads and videos go.
- `ship_globs`: folders of markdown notes on ships (`12-some-ship.md` files with the review URL in them). Their text shows on the **Page** tab, and their notes and draft (a code block of `### Verdict`-style headings under `## Drafts`) are imported once, if you haven't written your own yet.
- `window`: `"app"` (its own window) or `"browser"` (a tab in your default browser). Also under Settings → General.
- `port`: the port on `127.0.0.1` the page is served on (default 8765; another free one is picked if it's taken).
- `github_token`: `"gh"` uses `gh auth token` if the GitHub CLI is logged in; or paste a token for 5,000 GitHub lookups an hour instead of 60.

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
- `hooks.test_actions`: `(label, function(window, ship))` buttons on the Test tab.
- `hooks.prompts` and `hooks.settings`: texts and choices to edit under Settings.

A ship has `sid`, `title`, `username`, `week`, `num`, `item` (its queue row), `page_text`, `quick`, `notes`, `draft` and `fields`. `window` has `cfg`, `store`, `hooks`, `page_json` (the ship's review-page data), `say(text)` (the status line), `copy(text)` (the clipboard), `reload()`, `tabs.setCurrentIndex(n)` and `background(fn, done)`. A button's function can also return a message to show. For example, a button that copies the title:

```
def copy_title(window, ship):
    window.copy(ship.title)
    return f'copied {ship.title}'

def register(hooks, cfg):
    hooks.decision_actions.append(("Copy title", copy_title, "always"))
```

Plugins written for the old Qt window still load, but anything in them that uses PySide6 (`QApplication`, `QMessageBox`, …) has to change to the calls above.

## Running from source, building the installer

terra-desk itself only needs Python 3.11+ (standard library only). From the `terra-desk` folder:

```
terra-desk.bat          # Windows
./terra-desk            # Linux / macOS
```

On Linux, Set up needs `git`, video compression needs `ffmpeg`, and Run uses `tools/sandbox-run` (bubblewrap, plus Wine for `.exe` files) as before. `--no-browser` starts it without opening a window; `--port N` picks the port.

Tests: `python -X utf8 -m pytest -q` in `terra-desk`.

The installer is built by GitHub Actions (`.github/workflows/windows.yml`) on every push: tests on Windows and Linux, then PyInstaller (`packaging/terra-desk.spec`), MinGit, and Inno Setup (`packaging/terra-desk.iss`). Push a tag like `v1.0.0` to publish `terra-desk-setup.exe` as a release. To build it on a Windows PC yourself:

```
pip install pyinstaller==6.11.1 imageio-ffmpeg==0.5.1
pyinstaller packaging/terra-desk.spec --noconfirm --distpath dist --workpath build/pyinstaller
iscc /DAppVersion=1.0.0 packaging/terra-desk.iss
```

(Download MinGit and unzip it into `build/mingit` first if you want Git bundled.)

### Moving from the Linux version

- The PySide6 window and its terminal are gone; the same features are in the web page. Nothing needs installing besides Python.
- The send queue, `writes.jsonl` and `queue-json/` dumps now live in the workspace's `send` folder instead of next to `tools/`. Move yours there (or set `data_dir` in `config.toml` to the `tools` folder) so ships you already sent stay marked as sent.

## How it keeps you safe

- The server only listens on `127.0.0.1`. Every request from the page carries a key that is new on each start, and requests with any other host name are refused, so other websites you have open can't control it.
- Player-supplied text on the review page is escaped, scripts are blocked by the page's Content-Security-Policy, and links open in your normal browser. Terra review pages are never opened (that would claim the ship).
- Downloads are never run by Set up or the checks; only **Run** starts anything, and on Windows that's inside Windows Sandbox unless you say otherwise.

## Credits

The installer bundles [MinGit](https://github.com/git-for-windows/git) (GPL-2.0) for cloning and an ffmpeg build from [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) (ffmpeg is GPL) for compressing videos; both are separate programs that terra-desk starts.

## Changing the review guide or templates

The templates each verdict starts from are under **Settings → Templates**. The review guide lives in `terra-desk/terra_desk/rubric.py`; edit it there and restart terra-desk.
