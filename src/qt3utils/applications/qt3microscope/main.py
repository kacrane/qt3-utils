"""
QT3 Microscope: one window for the whole microscope.

A status strip along the top, measurement tabs in the middle, a scope
strip along the bottom and a folding instrument drawer on the right.
Instruments are added step by step: laser and power (step 2), the scope
strip, formerly qdlscope (step 3), and the stage, formerly qdlmove, and
flip mirrors (step 4). A drawer section without a panel yet says which
later step fills it in.
All hardware calls go through the instrument hub (hub.py): one worker
thread per device, so a slow device never freezes the window.

One program and one branch serve both microscopes. The setup profile
(config_files/VQLM.yaml or TQLM.yaml) decides which instruments appear,
so setup differences live in the profiles, not in separate branches.

Desktop shortcut (no terminal window): run make_desktop_shortcut.ps1 once.
Terminal output goes to the Log section under the scope instead.

Run:  python -m qt3utils.applications.qt3microscope.main
      python -m qt3utils.applications.qt3microscope.main --setup TQLM
      python -m qt3utils.applications.qt3microscope.main --config my_profile.yaml
"""

import argparse
import json
import logging
import os
import sys
import time
import tkinter as tk
from tkinter import ttk

import yaml

if __package__ in (None, ""):  # run as a plain script: put .../src on the path
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.abspath(os.path.join(_here, "..", "..", "..")))

from qt3utils.applications.qt3microscope import console
from qt3utils.applications.qt3microscope.hub import Hub
from qt3utils.applications.qt3microscope.widgets import (CollapsibleSection, ScrollableFrame,
                                                         StatusChip, XYMicrometerBox)

# Catch everything that would go to a terminal before logging is set up, so
# the Log section under the scope shows it (the desktop shortcut runs
# pythonw.exe, which has no terminal window).
console.install()
logger = logging.getLogger(__name__)
logging.basicConfig()

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(APP_DIR, "config_files")
ICON_PATH = os.path.join(APP_DIR, "QT3logo.ico")
DEFAULT_SETUP = "VQLM"

# Window state (step 8): size, drawer width, folded sections and popped-out
# windows. The size and drawer width always come back; the folded sections,
# popped-out windows and open tab only if the program reopens within
# RESTORE_WITHIN_S, otherwise the profile defaults apply.
STATE_DIR = os.path.join(os.path.expanduser("~"), ".qt3utils")
RESTORE_WITHIN_S = 2 * 3600
STATE_SAVE_EVERY_MS = 60_000

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
        self.popped = {}        # drawer section key -> placeholder left in the drawer
        # Pixel sizes below are written for a 96 dpi screen; scale them to this one.
        self.scale = max(1.0, window.winfo_fpixels("1i") / 96.0)
        self.state_path = os.path.join(STATE_DIR, f"qt3microscope_window_{self.setup}.json")
        self.saved, self.recent = self._load_state()

        window.title(profile.get("title", f"QT3 Microscope — {self.setup}"))
        w, h = (int(v) for v in profile.get("geometry", "1440x900").split("x"))
        window.geometry(self.saved.get("geometry") or f"{self.px(w)}x{self.px(h)}")
        window.minsize(self.px(1100), self.px(700))
        if self.saved.get("zoomed", profile.get("start_maximized", True)):
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
        window.after(300, self._restore_late)
        window.after(STATE_SAVE_EVERY_MS, self._autosave)
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
        view.add_command(label="Bring all windows back to the drawer", command=self._pop_in_all)
        view.add_separator()
        self.drawer_shown = tk.BooleanVar(value=True)
        view.add_checkbutton(label="Show instrument drawer", variable=self.drawer_shown,
                             command=self._toggle_drawer)
        # Drawer sections to show; filled in once the drawer is built.
        self.sections_menu = tk.Menu(view, tearoff=0)
        view.add_cascade(label="Drawer sections", menu=self.sections_menu)
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
        self.tab_pages = {}
        for name in self.profile.get("measurements", ["Image scan"]):
            page = ttk.Frame(self.tabs)
            ttk.Label(page, text=f"{name}\n\nNot connected yet: this tab comes in a later step.",
                      foreground="#555555", justify="left").pack(anchor="nw", padx=20, pady=20)
            self.tabs.add(page, text=f"  {name}  ")
            self.tab_pages[name] = page
        self.tabs.pack(side="top", fill="both", expand=True)

        self.drawer = self._build_drawer(self.hpane)
        self.hpane.add(self.drawer, weight=0)
        self.window.after(50, self._place_sash)

    def _place_sash(self):
        width = self.saved.get("drawer_width") or self.px(self.profile.get("drawer_width", 390))
        if hasattr(self, "drawer_scroll"):
            # Widen the drawer if its contents need it (e.g. an enlarged reading).
            self.window.update_idletasks()
            need = (self.drawer_scroll.inner.winfo_reqwidth()
                    + self.drawer_scroll.bar.winfo_reqwidth() + self.px(4))
            width = max(width, need)
        try:
            self.hpane.sashpos(0, max(self.px(500), self.window.winfo_width() - width))
        except tk.TclError:
            pass

    def _build_bottom_strip(self, parent):
        bottom = self.profile.get("bottom", {})
        outer = ttk.Frame(parent)
        frame = ttk.Frame(outer)  # scope, and the XY micrometers on TQLM
        frame.pack(side="top", fill="x")

        scope = CollapsibleSection(frame, bottom.get("scope_label", "Scope"),
                                   start_open=self._start_open("scope", True))
        scope.pack(side="left", fill="x", expand=True, anchor="s")
        ttk.Label(scope.body, text="Count rate and trace go here (scope step).",
                  foreground="#555555").pack(anchor="w", pady=30)
        self.sections["scope"] = scope

        if bottom.get("xy_micrometers", False):
            self.xy_box = XYMicrometerBox(frame, start_locked=True)
            self.xy_box.pack(side="right", anchor="s")

        # Log under the scope: what used to go to the terminal window. Folded
        # by default; its header counts errors and warnings.
        log = CollapsibleSection(outer, "Log", start_open=self._start_open("log", False))
        log.pack(side="top", fill="x")
        self.sections["log"] = log
        try:
            from qt3utils.applications.qt3microscope.panels.log import LogPanel
            self.log_panel = LogPanel(log, scale=self.scale)
        except Exception as e:
            logger.exception("log panel failed to start")
            ttk.Label(log.body, text=f"Could not start the log: {e}", foreground="#c62828").pack(anchor="w")
        return outer

    def _build_drawer(self, parent):
        outer = ttk.Frame(parent, width=self.px(self.profile.get("drawer_width", 390)))
        head = ttk.Frame(outer, padding=(8, 6))
        head.pack(fill="x")
        ttk.Label(head, text="Instruments", font=("TkDefaultFont", 10, "bold")).pack(side="left")
        ttk.Button(head, text="Fold all", width=8, command=lambda: self._set_all(False)).pack(side="right")
        ttk.Button(head, text="Open all", width=8, command=lambda: self._set_all(True)).pack(side="right", padx=4)

        scroll = ScrollableFrame(outer)
        scroll.pack(fill="both", expand=True)
        self.drawer_scroll = scroll
        self.drawer_order = []
        self.section_shown = {}
        for item in self.profile.get("drawer", []):
            key = item["key"]
            sec = CollapsibleSection(scroll.inner, item["label"],
                                     start_open=self._start_open(key, item.get("open", False)),
                                     on_popout=lambda k=key: self.toggle_popout(k))
            # hidden: true leaves a section out of the drawer until View > Drawer sections shows it.
            shown = self.saved.get("shown", {}).get(key, not item.get("hidden", False))
            var = tk.BooleanVar(value=shown)
            self.section_shown[item["key"]] = var
            self.drawer_order.append(item["key"])
            self.sections_menu.add_checkbutton(label=item["label"], variable=var,
                                               command=self._repack_drawer)
            note = item.get("note", "Not connected yet.")
            ttk.Label(sec.body, text=note, foreground="#555555", wraplength=self.px(330),
                      justify="left").pack(anchor="w")
            self.sections[item["key"]] = sec
        self._repack_drawer()
        return outer

    def _repack_drawer(self):
        """Show the drawer sections that are switched on, in profile order."""
        for key in self.drawer_order:
            if key not in self.popped:
                self.sections[key].pack_forget()
            else:
                self.popped[key].pack_forget()
        for key in self.drawer_order:
            if not self.section_shown[key].get():
                continue
            if key in self.popped:  # its own window; a note in the drawer says where it went
                self.popped[key].pack(fill="x", padx=6, pady=3)
            else:
                self.sections[key].pack(fill="x", padx=6, pady=3)

    def _set_all(self, value):
        for key, sec in self.sections.items():
            if key not in ("scope", "log") and key not in self.popped:
                sec.set_open(value)

    def _toggle_drawer(self):
        if self.drawer_shown.get():
            self.hpane.add(self.drawer, weight=0)
            self.window.after(20, self._place_sash)
        else:
            self.hpane.forget(self.drawer)

    # ----- pop-out (step 8) -------------------------------------------------
    def toggle_popout(self, key):
        if key in self.popped:
            self.pop_in(key)
        else:
            self.pop_out(key)

    def pop_out(self, key, geometry=None):
        """Move a drawer section into its own window. The panel keeps running:
        Tk's "wm manage" turns the same frame into a window, so nothing is
        rebuilt and no hardware connection is touched."""
        if key in self.popped:
            return
        sec = self.sections[key]
        self.window.update_idletasks()
        width = max(sec.winfo_width(), sec.winfo_reqwidth())
        x0 = sec.winfo_rootx()
        sec.pack_forget()
        sec.set_popped(True)
        try:
            self.window.wm_manage(sec)
        except tk.TclError as e:
            logger.warning("could not pop out %s: %r", key, e)
            sec.set_popped(False)
            self._repack_drawer()
            return
        title = f"{sec.title_text} — QT3 Microscope {self.setup}"
        tk_call = self.window.tk.call
        tk_call("wm", "title", sec._w, title)
        tk_call("wm", "protocol", sec._w, "WM_DELETE_WINDOW",
                self.window.register(lambda: self.pop_in(key)))
        if os.path.exists(ICON_PATH):
            try:
                tk_call("wm", "iconbitmap", sec._w, ICON_PATH)
            except tk.TclError:
                pass
        self.window.update_idletasks()
        if not geometry:
            # Open it just left of the drawer, the size it needs, each new one
            # a little lower than the last so they don't hide each other.
            height = sec.winfo_reqheight()
            x = max(0, x0 - width - self.px(20))
            y = self.window.winfo_rooty() + self.px(80 + 40 * len(self.popped))
            geometry = f"{width}x{height}+{x}+{y}"
        tk_call("wm", "geometry", sec._w, geometry)

        note = ttk.Frame(self.drawer_scroll.inner, padding=(8, 4), relief="solid", borderwidth=1)
        ttk.Label(note, text=f"{sec.title_text}  ↗", foreground="#555555").pack(side="left")
        ttk.Button(note, text="Bring back", command=lambda: self.pop_in(key)).pack(side="right")
        ttk.Button(note, text="Show", width=6,
                   command=lambda: tk_call("raise", sec._w)).pack(side="right", padx=4)
        self.popped[key] = note
        self._repack_drawer()
        logger.info("popped out %s", key)
        self._panel_moved(key)

    def pop_in(self, key):
        """Put a popped-out section back in the drawer."""
        note = self.popped.pop(key, None)
        if note is None:
            return
        sec = self.sections[key]
        try:
            self.window.wm_forget(sec)
        except tk.TclError:
            pass
        note.destroy()
        sec.set_popped(False)
        self._repack_drawer()
        self._panel_moved(key)
        self.window.after(20, self._place_sash)

    def _panel_moved(self, key):
        panel = getattr(self, f"{key}_panel", None)
        if hasattr(panel, "rebind_keys"):
            panel.rebind_keys()

    def _pop_in_all(self):
        for key in list(self.popped):
            self.pop_in(key)

    # ----- saved window state (step 8) -------------------------------------
    def _load_state(self):
        """Returns (saved state, whether it is recent enough to restore the
        folded sections, popped-out windows and open tab)."""
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return {}, False
        if not isinstance(saved, dict):
            return {}, False
        age = time.time() - float(saved.get("saved_at", 0))
        return saved, 0 <= age <= RESTORE_WITHIN_S

    def _start_open(self, key, default):
        if self.recent and key in self.saved.get("open", {}):
            return bool(self.saved["open"][key])
        return default

    def _restore_late(self):
        """Things that need the panels built first: open tab, popped-out windows."""
        if not self.recent:
            return
        tab = self.saved.get("tab")
        if tab in self.tab_pages:
            try:
                self.tabs.select(self.tab_pages[tab])
            except tk.TclError:
                pass
        for key, geometry in (self.saved.get("popped") or {}).items():
            if key in self.sections and key != "scope" and self.section_shown[key].get():
                self.pop_out(key, geometry)

    def _collect_state(self):
        w = self.window
        state = {"saved_at": time.time(), "setup": self.setup}
        try:
            zoomed = w.state() == "zoomed"
        except tk.TclError:
            zoomed = False
        state["zoomed"] = zoomed
        if zoomed:
            # Keep the un-maximized size from before, so un-maximizing still works.
            if self.saved.get("geometry"):
                state["geometry"] = self.saved["geometry"]
        else:
            state["geometry"] = w.geometry()
        if self.drawer_shown.get():
            try:
                state["drawer_width"] = w.winfo_width() - self.hpane.sashpos(0)
            except tk.TclError:
                pass
        elif self.saved.get("drawer_width"):
            state["drawer_width"] = self.saved["drawer_width"]
        state["open"] = {k: sec.is_open for k, sec in self.sections.items() if k not in self.popped}
        for k in self.popped:  # back in the drawer it opens as it was before popping out
            state["open"][k] = True
        state["shown"] = {k: v.get() for k, v in self.section_shown.items()}
        state["popped"] = {k: w.tk.call("wm", "geometry", self.sections[k]._w) for k in self.popped}
        try:
            current = self.tabs.select()
            state["tab"] = next((n for n, p in self.tab_pages.items() if str(p) == current), None)
        except tk.TclError:
            pass
        return state

    def save_state(self):
        try:
            state = self._collect_state()
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=1)
            os.replace(tmp, self.state_path)
            self.saved = state
        except Exception as e:  # never let a state file stop the program or its closing
            logger.warning("could not save window state: %r", e)

    def _autosave(self):
        """Save now and then too, so the state survives a crash."""
        self.save_state()
        self.window.after(STATE_SAVE_EVERY_MS, self._autosave)

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
        # Scope first: it makes a matplotlib canvas before the laser panel starts
        # loading qick (and IPython) on another thread.
        if "scope" in self.sections and "scope" in hw:
            try:
                from qt3utils.applications.qt3microscope.panels.scope import ScopePanel
                self._clear_note("scope")
                self.scope_panel = ScopePanel(self.sections["scope"], self.hub, hw["scope"],
                                              set_status=self._chip_setter("counts"), scale=self.scale)
            except Exception as e:
                self._panel_failed("scope", e)
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
                                              set_status=self._chip_setter("power"),
                                              on_layout=self._place_sash)
            except Exception as e:
                self._panel_failed("power", e)

        for key, module, cls, chip in (("stage", "stage", "StagePanel", "piezo"),
                                       ("flippers", "flippers", "FlippersPanel", "flippers")):
            if key in self.sections and key in hw:
                try:
                    mod = __import__(f"qt3utils.applications.qt3microscope.panels.{module}", fromlist=[cls])
                    self._clear_note(key)
                    panel = getattr(mod, cls)(self.sections[key], self.hub, hw[key],
                                              set_status=self._chip_setter(chip))
                    setattr(self, f"{key}_panel", panel)
                except Exception as e:
                    self._panel_failed(key, e)
        self._attach_scan_tabs(hw)

    def _attach_scan_tabs(self, hw):
        """Image scan and XYZ Optimization tabs (formerly qdlscan's image and line
        scans): need the stage piezos and the scope counter. "Line scan" is
        still accepted as the old name of XYZ Optimization in a profile."""
        line_name = "XYZ Optimization" if "XYZ Optimization" in self.tab_pages else "Line scan"
        wanted = [n for n in ("Image scan", line_name) if n in self.tab_pages]
        if not wanted or "scan" not in hw:
            return
        stage = getattr(self, "stage_panel", None)
        try:
            if stage is None or len(stage.axes) < 3:
                raise RuntimeError("the scan tabs need the Stage (piezos) section with X, Y and Z axes")
            from qt3utils.applications.qt3microscope.panels.scan import ImageScanTab, LineScanTab, ScanEngine
            self.scan_engine = ScanEngine(self.hub, stage, getattr(self, "scope_panel", None),
                                          hw["scan"], hw.get("scope"))
        except Exception as e:
            logger.exception("scan tabs failed to start")
            for name in wanted:
                self._tab_failed(name, e)
            return
        for name, cls, key in (("Image scan", ImageScanTab, "image"), (line_name, LineScanTab, "line")):
            if name not in self.tab_pages:
                continue
            try:
                page = self.tab_pages[name]
                for child in page.winfo_children():
                    child.destroy()
                setattr(self, f"{key}_tab", cls(page, self.scan_engine, hw["scan"].get(key), scale=self.scale))
            except Exception as e:
                logger.exception("%s tab failed to start", name)
                self._tab_failed(name, e)
        if hasattr(self, "image_tab") and line_name in self.tab_pages:
            self.image_tab.on_optimize = lambda: self.tabs.select(self.tab_pages[line_name])

    def _tab_failed(self, name, err):
        page = self.tab_pages[name]
        for child in page.winfo_children():
            child.destroy()
        ttk.Label(page, text=f"Could not start this tab: {err}", foreground="#c62828",
                  wraplength=self.px(600), justify="left").pack(anchor="nw", padx=20, pady=20)

    def _panel_failed(self, key, err):
        logger.exception("%s panel failed to start", key)
        self._clear_note(key)
        ttk.Label(self.sections[key].body, text=f"Could not start this panel: {err}",
                  foreground="#c62828", wraplength=self.px(330), justify="left").pack(anchor="w")

    def quit(self):
        self.save_state()
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
        # Its own taskbar button and icon, not grouped with other Python programs.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("QT3.Microscope")
    except Exception:
        pass
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Windows 8.1 and later
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception as e:
        logger.info("could not turn on sharp display: %r", e)


def _startup_failed(err):
    """With no terminal window, a failure before the main window opens would
    be silent: say so in a message box."""
    logger.exception("QT3 Microscope could not start")
    try:
        from tkinter import messagebox
        root = tk._default_root or tk.Tk()
        root.withdraw()
        messagebox.showerror("QT3 Microscope",
                             f"QT3 Microscope could not start:\n\n{err}\n\n"
                             f"Details are in {console.CONSOLE_LOG}")
        root.destroy()
    except Exception:
        pass


def main(is_root_process=True, argv=None):
    args = parse_args(argv if (argv is not None or is_root_process) else [])
    try:
        profile = load_profile(args.setup, args.config)
        if is_root_process:
            enable_sharp_display()  # must happen before the Tk window exists
        window = tk.Tk() if is_root_process else tk.Toplevel()
        MicroscopeWindow(window, profile)
    except Exception as e:
        if not is_root_process:
            raise
        _startup_failed(e)
        sys.exit(1)
    if is_root_process:
        window.mainloop()


if __name__ == "__main__":
    main()
