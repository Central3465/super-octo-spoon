"""Kept for plugins written for the old Qt window: `from terra_desk.app import background` still works.
The app itself is now terra_desk.server (a web page on this computer)."""
import threading


def background(fn, callback):
    """Run fn() in a thread, then callback(result, error) (also in that thread)."""
    def run():
        try:
            res, err = fn(), None
        except Exception as e:  # noqa: BLE001
            res, err = None, e
        callback(res, err)
    threading.Thread(target=run, daemon=True).start()


def main():
    from .__main__ import main as start
    return start()
