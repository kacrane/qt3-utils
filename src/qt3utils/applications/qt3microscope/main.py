"""
QT3 Microscope: one window for the whole microscope.

A status strip along the top, measurement tabs in the middle, a scope
strip along the bottom and a folding instrument drawer on the right.
Instruments are added step by step (plan step 2: laser and power). A
drawer section without a panel yet says which later step fills it in.
All hardware calls go through the instrument hub (hub.py): one worker
thread per device, so a slow device never freezes the window.

One program and one branch serve both microscopes. The setup profile
(config_files/VQLM.yaml or TQLM.yaml) decides which instruments appear,
so setup differences live in the profiles, not in separate branches.

Run:  python -m qt3utils.applications.qt3microscope.main
      python -m qt3utils.applications.qt3microscope.main --setup TQLM
      python -m qt3utils.applications.qt3microscope.main --config my_profile.yaml
"""

import argparse
import logging
import os
import sys
import tkinter as tk
from tkinter import ttk

import yaml

if __package__ in (None, ""):  # run as a plain script: put .../src on the path
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.abspath(os.path.join(_here, "..", "..", "..")))

from qt3utils.applications.qt3microscope.hub import Hub
from qt3utils.applications.qt3microscope.widgets import (CollapsibleSection, ScrollableFrame,
                                                         StatusChip, XYMicrometerBox)

logger = logging.getLogger(__name__)
logging.basicConfig()

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(APP_DIR, "config_files")
ICON_PATH = os.path.join(APP_DIR, "QT3logo.ico")
DEFAULT_SETUP = "VQLM"

LOG_PATH = os.path.join(os.path.expanduser("~"), ".qt3utils", "qt3microscope.log")
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
    with open(path, "r", encoding="utf-8") as f:  # Windows would read it as cp1252
        return yaml.safe_load(f)["QT3MICROSCOPE"]


class MicroscopeWindow:
    def __init__(self, window, profile):
        self.window = window
        self.profile = profile
        self.setup = profile.get("setup", "?")
        self.sections = {}
        self.chips = {}
        # Pixel sizes below are written for a 96 dpi screen; scale them to this one.
        self.scale = max(1.0, window.winfo_fpixels("1i") / 96.0)

        window.title(profile.get("title", f"QT3 Microscope — {self.setup}"))
        w, h = (int(v) for v in profile.get("geometry", "1440x900").split("x"))
        window.geometry(f"{self.px(w)}x{self.px(h)}")
        window.minsize(self.px(1100), self.px(700))
        if profile.get("start_maximized", True):
            try:
                window.state("zoomed")  # Windows
            except tk.TclError:
                pass
        if os.path.exists(ICON_PATH):
            try:
                window.iconbitmap(ICON_PATH)
            except tk.TclError:
                pass  # .ico only works on Windows; other systems keep the default icon

        self._build_menu()
        self._build_status_strip()
        self._build_body()
        self._build_footer()
        self.hub = Hub(window)
        self._attach_panels()
        window.protocol("WM_DELETE_WINDOW", self.quit)
        logger.info("QT3 Microscope opened with setup %s", self.setup)

    def px(self, value):
        """Convert a 96 dpi pixel size to this screen."""
        return int(round(value * self.scale))

    # ----- top -------------------------------------------------------------
    def _build_menu(self):
        bar = tk.Menu(self.window)
        file_menu = tk.Menu(bar, tearoff=0)
        file_menu.add_command(label="Quit", command=self.quit)
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
        width = self.px(self.profile.get("drawer_width", 390))
        try:
            self.hpane.sashpos(0, max(self.px(500), self.window.winfo_width() - width))
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
        outer = ttk.Frame(parent, width=self.px(self.profile.get("drawer_width", 390)))
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
            ttk.Label(sec.body, text=note, foreground="#555555", wraplength=self.px(330),
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

    # ----- instruments -----------------------------------------------------
    def _chip_setter(self, key):
        chip = self.chips.get(key)
        return chip.set if chip else None

    def _clear_note(self, key):
        for child in self.sections[key].body.winfo_children():
            child.destroy()

    def _attach_panels(self):
        """Put a working panel in each drawer section the profile has hardware for."""
        hw = self.profile.get("hardware", {}) or {}
        if "laser" in self.sections and "qickdawg" in hw:
            try:
                from qt3utils.applications.qt3microscope.panels.laser import LaserPanel
                from qt3utils.hardware.qickdawg.client import QickDawgClient
                q = hw["qickdawg"]
                client = QickDawgClient(host=q.get("host", "192.168.3.1"), port=q.get("port", 8888),
                                        server_name=q.get("server_name", "myqick"),
                                        timeout_s=q.get("timeout_s", 10),
                                        laser_settings=hw.get("laser"))
                self._clear_note("laser")
                self.laser_panel = LaserPanel(self.sections["laser"], self.hub, client,
                                              health_check_s=q.get("health_check_s", 30),
                                              set_status=self._chip_setter("laser"))
            except Exception as e:
                self._panel_failed("laser", e)
        if "power" in self.sections and "power" in hw:
            try:
                from qt3utils.applications.qt3microscope.panels.power import PowerPanel
                self._clear_note("power")
                self.power_panel = PowerPanel(self.sections["power"], self.hub, hw["power"],
                                              set_status=self._chip_setter("power"))
            except Exception as e:
                self._panel_failed("power", e)

    def _panel_failed(self, key, err):
        logger.exception("%s panel failed to start", key)
        self._clear_note(key)
        ttk.Label(self.sections[key].body, text=f"Could not start this panel: {err}",
                  foreground="#c62828", wraplength=self.px(330), justify="left").pack(anchor="w")

    def quit(self):
        self.hub.close()
        self.window.destroy()

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


def enable_sharp_display():
    """
    Tell Windows this program draws at the screen's real resolution.
    Without this, Windows draws the window at 96 dpi and stretches it on
    high-resolution or scaled screens, which makes all text blurry.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Windows 8.1 and later
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception as e:
        logger.info("could not turn on sharp display: %r", e)


def main(is_root_process=True, argv=None):
    args = parse_args(argv if (argv is not None or is_root_process) else [])
    profile = load_profile(args.setup, args.config)
    if is_root_process:
        enable_sharp_display()  # must happen before the Tk window exists
    window = tk.Tk() if is_root_process else tk.Toplevel()
    MicroscopeWindow(window, profile)
    if is_root_process:
        window.mainloop()


if __name__ == "__main__":
    main()
