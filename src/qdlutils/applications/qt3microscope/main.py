"""
QT3 Microscope: one window for the whole microscope.

Step 1 of the single-window plan. This is the empty shell: a status strip
along the top, measurement tabs in the middle, a scope strip along the
bottom and a folding instrument drawer on the right. Nothing talks to
hardware yet; each section says which later step fills it in.

The setup profile (config_files/VQLM.yaml or TQLM.yaml) decides which
instruments appear.

Run:  python -m qdlutils.applications.qt3microscope.main
      python -m qdlutils.applications.qt3microscope.main --setup TQLM
      python -m qdlutils.applications.qt3microscope.main --config my_profile.yaml
"""

import argparse
import logging
import os
import sys
import tkinter as tk
from tkinter import ttk

import yaml

try:
    from .widgets import CollapsibleSection, ScrollableFrame, StatusChip, XYMicrometerBox
except ImportError:  # run as a plain script
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from widgets import CollapsibleSection, ScrollableFrame, StatusChip, XYMicrometerBox

logger = logging.getLogger(__name__)
logging.basicConfig()

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(APP_DIR, "config_files")
ICON_PATH = os.path.join(APP_DIR, "QT3logo.ico")
DEFAULT_SETUP = "VQLM"

LOG_PATH = os.path.join(os.path.expanduser("~"), ".qdlutils", "qt3microscope.log")
try:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    _fh = logging.FileHandler(LOG_PATH)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_fh)
    logger.setLevel(logging.INFO)
except OSError:
    pass


def load_profile(setup=None, path=None):
    """Read a setup profile. An explicit path wins over the setup name."""
    if path is None:
        path = os.path.join(CONFIG_DIR, f"{(setup or DEFAULT_SETUP).upper()}.yaml")
    with open(path, "r") as f:
        return yaml.safe_load(f)["QT3MICROSCOPE"]


class MicroscopeWindow:
    def __init__(self, window, profile):
        self.window = window
        self.profile = profile
        self.setup = profile.get("setup", "?")
        self.sections = {}
        self.chips = {}

        window.title(profile.get("title", f"QT3 Microscope — {self.setup}"))
        window.geometry(profile.get("geometry", "1440x900"))
        window.minsize(1100, 700)
        if os.path.exists(ICON_PATH):
            try:
                window.iconbitmap(ICON_PATH)
            except tk.TclError:
                pass  # .ico only works on Windows; other systems keep the default icon

        self._build_menu()
        self._build_status_strip()
        self._build_body()
        self._build_footer()
        logger.info("QT3 Microscope opened with setup %s", self.setup)

    # ----- top -------------------------------------------------------------
    def _build_menu(self):
        bar = tk.Menu(self.window)
        file_menu = tk.Menu(bar, tearoff=0)
        file_menu.add_command(label="Quit", command=self.window.destroy)
        bar.add_cascade(label="File", menu=file_menu)

        view = tk.Menu(bar, tearoff=0)
        view.add_command(label="Open all drawer sections", command=lambda: self._set_all(True))
        view.add_command(label="Fold all drawer sections", command=lambda: self._set_all(False))
        view.add_separator()
        self.drawer_shown = tk.BooleanVar(value=True)
        view.add_checkbutton(label="Show instrument drawer", variable=self.drawer_shown,
                             command=self._toggle_drawer)
        bar.add_cascade(label="View", menu=view)
        self.window.config(menu=bar)

    def _build_status_strip(self):
        strip = tk.Frame(self.window, bd=1, relief="raised", padx=6, pady=4)
        strip.pack(side="top", fill="x")
        for item in self.profile.get("status", []):
            chip = StatusChip(strip, item["label"])
            chip.pack(side="left", padx=3)
            self.chips[item["key"]] = chip
        tk.Label(strip, text=f"Setup: {self.setup}", fg="#444444").pack(side="right", padx=6)

    # ----- middle ----------------------------------------------------------
    def _build_body(self):
        self.hpane = ttk.PanedWindow(self.window, orient="horizontal")
        self.hpane.pack(side="top", fill="both", expand=True)

        # Left side: measurement tabs fill the space above the bottom strip.
        # The bottom strip has no sash, so folding the scope gives its room
        # back to the tabs.
        left = ttk.Frame(self.hpane)
        self.hpane.add(left, weight=1)
        self._build_bottom_strip(left).pack(side="bottom", fill="x")

        self.tabs = ttk.Notebook(left)
        for name in self.profile.get("measurements", ["Image scan"]):
            page = ttk.Frame(self.tabs, padding=20)
            ttk.Label(page, text=f"{name}\n\nNot connected yet: the scan and PLE tabs come in later steps.",
                      foreground="#555555", justify="left").pack(anchor="nw")
            self.tabs.add(page, text=f"  {name}  ")
        self.tabs.pack(side="top", fill="both", expand=True)

        self.drawer = self._build_drawer(self.hpane)
        self.hpane.add(self.drawer, weight=0)
        self.window.after(50, self._place_sash)

    def _place_sash(self):
        width = self.profile.get("drawer_width", 390)
        try:
            self.hpane.sashpos(0, max(500, self.window.winfo_width() - width))
        except tk.TclError:
            pass

    def _build_bottom_strip(self, parent):
        bottom = self.profile.get("bottom", {})
        frame = ttk.Frame(parent)

        scope = CollapsibleSection(frame, bottom.get("scope_label", "Scope"), start_open=True)
        scope.pack(side="left", fill="x", expand=True, anchor="s")
        ttk.Label(scope.body, text="Count rate and trace go here (scope step).",
                  foreground="#555555").pack(anchor="w", pady=30)
        self.sections["scope"] = scope

        if bottom.get("xy_micrometers", False):
            self.xy_box = XYMicrometerBox(frame, start_locked=True)
            self.xy_box.pack(side="right", anchor="s")
        return frame

    def _build_drawer(self, parent):
        outer = ttk.Frame(parent, width=self.profile.get("drawer_width", 390))
        head = ttk.Frame(outer, padding=(8, 6))
        head.pack(fill="x")
        ttk.Label(head, text="Instruments", font=("TkDefaultFont", 10, "bold")).pack(side="left")
        ttk.Button(head, text="Fold all", width=8, command=lambda: self._set_all(False)).pack(side="right")
        ttk.Button(head, text="Open all", width=8, command=lambda: self._set_all(True)).pack(side="right", padx=4)

        scroll = ScrollableFrame(outer)
        scroll.pack(fill="both", expand=True)
        for item in self.profile.get("drawer", []):
            sec = CollapsibleSection(scroll.inner, item["label"], start_open=item.get("open", False))
            sec.pack(fill="x", padx=6, pady=3)
            note = item.get("note", "Not connected yet.")
            ttk.Label(sec.body, text=note, foreground="#555555", wraplength=330,
                      justify="left").pack(anchor="w")
            self.sections[item["key"]] = sec
        return outer

    def _set_all(self, value):
        for key, sec in self.sections.items():
            if key != "scope":
                sec.set_open(value)

    def _toggle_drawer(self):
        if self.drawer_shown.get():
            self.hpane.add(self.drawer, weight=0)
            self.window.after(20, self._place_sash)
        else:
            self.hpane.forget(self.drawer)

    # ----- bottom ----------------------------------------------------------
    def _build_footer(self):
        foot = tk.Frame(self.window, bd=1, relief="sunken")
        foot.pack(side="bottom", fill="x", before=self.hpane)
        tk.Label(foot, text=f"Profile: {self.setup}.yaml   ·   Log: {LOG_PATH}",
                 fg="#444444", font=("TkDefaultFont", 8)).pack(side="left", padx=6)


def parse_args(argv):
    p = argparse.ArgumentParser(description="QT3 Microscope: one window for the microscope.")
    p.add_argument("--setup", choices=["VQLM", "TQLM"], default=None,
                   help=f"Which setup profile to load (default {DEFAULT_SETUP}).")
    p.add_argument("--config", default=None, help="Path to a profile YAML file instead.")
    return p.parse_args(argv)


def main(is_root_process=True, argv=None):
    args = parse_args(argv if (argv is not None or is_root_process) else [])
    profile = load_profile(args.setup, args.config)
    window = tk.Tk() if is_root_process else tk.Toplevel()
    MicroscopeWindow(window, profile)
    if is_root_process:
        window.mainloop()


if __name__ == "__main__":
    main()
