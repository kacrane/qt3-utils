"""
Where the Save dialogs (Save scan, Save pinned, the plot toolbars, the
scope's Save) open.

They open in the folder the last save went to, remembered across restarts
in ~/.qt3utils/qt3microscope_save_dir.json. Before the first save, or when
that folder is gone, they open on the M drive (the host-m mhome share),
and on the home folder when the M drive isn't reachable.
"""

import json
import os

STATE_PATH = os.path.join(os.path.expanduser("~"), ".qt3utils", "qt3microscope_save_dir.json")
DEFAULT_DIRS = ["M:\\", r"\\host-m.coral.uw.edu\mhome"]


def _isdir(path):
    try:
        return bool(path) and os.path.isdir(path)
    except OSError:
        return False


def last_dir():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            folder = json.load(f).get("folder")
        return folder if _isdir(folder) else None
    except (OSError, ValueError, AttributeError):
        return None


def initial_dir():
    """The folder a Save dialog should open in."""
    folder = last_dir()
    if folder:
        return folder
    for d in DEFAULT_DIRS:
        if _isdir(d):
            return d
    return os.path.expanduser("~")


def remember(folder):
    """Call after a save, with the folder the user chose."""
    if not folder:
        return
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"folder": os.path.abspath(folder)}, f)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass
