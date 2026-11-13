"""
QT3 Microscope: one window for the whole microscope.

A status strip along the top, measurement tabs in the middle, a scope
strip along the bottom and a folding instrument drawer on the right.
Instruments are added step by step: laser and power (step 2), the scope
strip, formerly qdlscope (step 3), and the stage, formerly qdlmove, and
flip mirrors (step 4), and the Camera tab (qt3camera). A drawer section without a panel yet says which
later step fills it in.
All hardware calls go through the instrument hub (hub.py): one worker
thread per device, so a slow device never freezes the window.

One program and one branch serve both microscopes. The setup profile
(config_files/VQLM.yaml or TQLM.yaml) decides which instruments appear,
so setup differences live in the profiles, not in separate branches.

Desktop shortcut (no terminal window): run make_desktop_shortcut.ps1 once.
Terminal output goes to the Log section under the scope instead.

File > Preferences chooses light or dark colors and the setup profile
(prefs.py); both apply on the next start, and the dialog can restart.

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
from qt3utils.applications.qt3microscope import prefs as prefs_mod
from qt3utils.applications.qt3microscope import theme
from qt3utils.applications.qt3microscope.theme import C

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
APP_ID = "QT3.Microscope"  # Windows taskbar ID; must match make_desktop_shortcut.ps1

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
    # On the qt3utils logger, so warnings from the panels and hardware reach the file too.
    logging.getLogger("qt3utils").addHandler(_fh)
except OSError:
    pass
logger.setLevel(logging.INFO)


def set_verbose_logging(verbose):
    """File > Preferences > Verbose logging: every qt3utils message down to
    DEBUG goes to the Log section and qt3microscope.log. Off: this window's own
    messages from INFO up, other modules' from WARNING up (as before)."""
    logging.getLogger("qt3utils").setLevel(logging.DEBUG if verbose else logging.NOTSET)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if verbose:
        logger.info("Verbose logging is on (File > Preferences).")


def profile_path(setup=None, path=None):
    """The profile file to read. An explicit path wins over the setup name."""
    return path or os.path.join(CONFIG_DIR, f"{(setup or DEFAULT_SETUP).upper()}.yaml")


def load_profile(setup=None, path=None):
    """Read a setup profile. An explicit path wins over the setup name."""
    with open(profile_path(setup, path), "r", encoding="utf-8") as f:  # Windows would read it as cp1252
        return yaml.safe_load(f)["QT3MICROSCOPE"]


class MicroscopeWindow:
    def __init__(self, window, profile, path=None, prefs=None, config_locked=False):
        self.window = window
        self.profile = profile
        self.profile_path = path or profile_path(profile.get("setup"))
        self.prefs = prefs or prefs_mod.load()
        self.config_locked = config_locked
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
                # default= also gives popped-out windows and message boxes the icon.
                window.iconbitmap(default=ICON_PATH)
            except tk.TclError:
                pass  # .ico only works on Windows; other systems keep the default icon

        self.hub = Hub(window)  # before the body: the XY micrometer box uses it
        self._build_menu()
        self._build_status_strip()
        self._build_body()
        self._build_footer()
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
        file_menu.add_command(label="Preferences…", command=self.open_preferences)
        file_menu.add_command(label="Restart", command=self.restart)
        file_menu.add_separator()
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

        help_menu = tk.Menu(bar, tearoff=0)
        help_menu.add_command(label="User Manual", command=self.open_user_manual)
        bar.add_cascade(label="Help", menu=help_menu)
        self.window.config(menu=bar)

    def _build_status_strip(self):
        strip = tk.Frame(self.window, bd=1, relief="raised", padx=6, pady=4)
        strip.pack(side="top", fill="x")
        for item in self.profile.get("status", []):
            chip = StatusChip(strip, item["label"])
            chip.pack(side="left", padx=3)
            self.chips[item["key"]] = chip
        tk.Label(strip, text=f"Setup: {self.setup}", fg=C.muted).pack(side="right", padx=6)

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
                      foreground=C.muted, justify="left").pack(anchor="nw", padx=20, pady=20)
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
        # Drag bar along the top of the strip: drag up or down to change the
        # height of the scope plot (remembered in the window state).
        self.scope_grip = tk.Frame(outer, height=self.px(6), cursor="sb_v_double_arrow", bg=C.border)
        self.scope_grip.pack(side="top", fill="x")
        self.scope_grip.bind("<ButtonPress-1>", self._grip_press)
        self.scope_grip.bind("<B1-Motion>", self._grip_drag)
        self.scope_grip.bind("<ButtonRelease-1>", lambda e: self.save_state())
        frame = ttk.Frame(outer)  # scope, and the XY micrometers on TQLM
        frame.pack(side="top", fill="x")
        self.scope_frame = frame

        scope = CollapsibleSection(frame, bottom.get("scope_label", "Scope"),
                                   start_open=self._start_open("scope", True),
                                   on_popout=lambda: self.toggle_popout("scope"),
                                   back_text="↙ Back to main window")
        scope.pack(side="left", fill="x", expand=True, anchor="s")
        ttk.Label(scope.body, text="Count rate and trace go here (scope step).",
                  foreground=C.muted).pack(anchor="w", pady=30)
        self.sections["scope"] = scope

        if bottom.get("xy_micrometers", False):
            ms_cfg = (self.profile.get("hardware") or {}).get("microstage")
            self.xy_box = None
            if ms_cfg:
                try:
                    from qt3utils.applications.qt3microscope.panels.microstage import MicrostagePanel
                    self.xy_box = MicrostagePanel(frame, self.hub, ms_cfg, setup=self.setup, scale=self.scale)
                except Exception:
                    logger.exception("XY micrometer box failed to start")
            if self.xy_box is None:  # no hardware in the profile: layout only
                self.xy_box = XYMicrometerBox(frame, start_locked=True)
            # Packed before the scope so the scope's plot can never squeeze it off screen.
            self.xy_box.pack(side="right", anchor="s", before=scope)

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
            ttk.Label(log.body, text=f"Could not start the log: {e}", foreground=C.red).pack(anchor="w")
        return outer

    def _grip_press(self, event):
        panel = getattr(self, "scope_panel", None)
        self._grip_start = (event.y_root, panel.plot_height() if panel else None)

    def _grip_drag(self, event):
        y0, h0 = getattr(self, "_grip_start", (None, None))
        panel = getattr(self, "scope_panel", None)
        if h0 is None or panel is None or "scope" in self.popped:
            return
        if not self.sections["scope"].is_open:
            self.sections["scope"].set_open(True)
        top = max(self.px(110), int(self.window.winfo_height() * 0.75))
        panel.set_plot_height(min(top, max(self.px(90), h0 - (event.y_root - y0))))

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
            ttk.Label(sec.body, text=note, foreground=C.muted, wraplength=self.px(330),
                      justify="left").pack(anchor="w")
            self.sections[item["key"]] = sec
        self.logo = self._make_logo(scroll.inner)
        self._repack_drawer()
        return outer

    def _make_logo(self, parent):
        """QT3 / UW logo under the drawer sections (black and purple #32006e;
        white and #e8e3d3 in dark mode). It sits right after the last section, so
        opening sections pushes it down out of view."""
        path = os.path.join(APP_DIR, "images", "qt3_uw_logo_dark.png" if getattr(C, "dark", False)
                            else "qt3_uw_logo_light.png")
        try:
            from PIL import Image, ImageTk
            img = Image.open(path)
            width = self.px(230)
            img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
            self._logo_image = ImageTk.PhotoImage(img)
        except Exception as e:
            logger.info("No logo: %s", e)
            return None
        return ttk.Label(parent, image=self._logo_image)

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
        if getattr(self, "logo", None) is not None:  # always after the last section
            self.logo.pack_forget()
            self.logo.pack(pady=(self.px(18), self.px(12)))

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
            x = max(0, x0 - width - self.px(20)) if key != "scope" else x0
            y = self.window.winfo_rooty() + self.px(80 + 40 * len(self.popped))
            geometry = f"{width}x{height}+{x}+{y}"
        tk_call("wm", "geometry", sec._w, geometry)

        if key == "scope":  # the placeholder stays where the scope was
            note = ttk.Frame(self.scope_frame, padding=(8, 4), relief="solid", borderwidth=1)
            note.pack(side="left", fill="x", expand=True, anchor="s")
            self.scope_grip.pack_forget()
        else:
            note = ttk.Frame(self.drawer_scroll.inner, padding=(8, 4), relief="solid", borderwidth=1)
        ttk.Label(note, text=f"{sec.title_text}  ↗", foreground=C.muted).pack(side="left")
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
        if key == "scope":
            sec.pack(side="left", fill="x", expand=True, anchor="s")
            self.scope_grip.pack(side="top", fill="x", before=self.scope_frame)
            panel = getattr(self, "scope_panel", None)
            if panel is not None:
                panel.set_plot_height(panel.plot_height())  # its main-window height, not the popped size
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
            if key == "scope" or (key in self.section_shown and self.section_shown[key].get()):
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
        panel = getattr(self, "scope_panel", None)
        if panel is not None and panel.plot_height():
            state["scope_height"] = round(panel.plot_height() / self.scale)  # in 96 dpi pixels
        elif self.saved.get("scope_height"):
            state["scope_height"] = self.saved["scope_height"]
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
                if self.saved.get("scope_height"):
                    self.scope_panel.set_plot_height(self.px(self.saved["scope_height"]))
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
                    extra = {"setup": self.setup} if key == "stage" else {}
                    panel = getattr(mod, cls)(self.sections[key], self.hub, hw[key],
                                              set_status=self._chip_setter(chip), **extra)
                    setattr(self, f"{key}_panel", panel)
                except Exception as e:
                    self._panel_failed(key, e)
        self._attach_scan_tabs(hw)
        self._attach_camera_tab(hw)
        self._attach_auto_mirrors()

    # ----- automatic flip mirrors (File > Preferences) ----------------------
    COUNTER_TABS = ("Image scan", "XYZ Optimization", "Line scan")

    def _attach_auto_mirrors(self):
        """Camera tab -> mirrors to the camera; scan tabs, a scan or the scope
        starting -> mirrors to the photon counter. Only when switched on in
        File > Preferences, and only the mirrors the profile lists under
        flippers: auto:, so the excitation choice is kept."""
        fp = getattr(self, "flippers_panel", None)
        self._auto_ready = False
        if fp is None or not fp.auto_targets:
            return
        fp.auto_enabled = lambda: bool(self.prefs.get("auto_mirrors", False))
        detector = (self.profile.get("hardware", {}).get("scope") or {}).get("detector") or "SPCM"
        fp.AUTO_LABELS = {"camera": "Auto: Camera", "counter": f"Auto: {detector}"}
        to_counter = lambda go: fp.auto_switch("counter", go)  # noqa: E731
        if getattr(self, "scope_panel", None) is not None:
            self.scope_panel.before_counting = to_counter
        if getattr(self, "scan_engine", None) is not None:
            self.scan_engine.before_counting = to_counter
        self.tabs.bind("<<NotebookTabChanged>>", lambda e: self._auto_for_tab(), add="+")
        # Not at launch: the mirrors come back as they were left.
        self.window.after(1500, lambda: setattr(self, "_auto_ready", True))

    def _current_tab_name(self):
        try:
            current = self.tabs.select()
        except tk.TclError:
            return None
        return next((n for n, p in self.tab_pages.items() if str(p) == current), None)

    def _auto_for_tab(self):
        fp = getattr(self, "flippers_panel", None)
        if fp is None or not getattr(self, "_auto_ready", False) or not self.prefs.get("auto_mirrors"):
            return
        name = self._current_tab_name()
        if name == "Camera":
            engine = getattr(self, "scan_engine", None)
            if engine is not None and engine.busy:
                fp.msg_var.set(f"Mirrors left on the counter: {engine.busy}")
                return
            fp.auto_switch("camera")
        elif name in self.COUNTER_TABS:
            fp.auto_switch("counter")

    def _auto_mirrors_text(self):
        fp = getattr(self, "flippers_panel", None)
        if fp is None or not fp.auto_targets:
            return None
        names = list(fp.cfg.flipper_names)

        def desc(mode):
            t = fp.auto_targets.get(mode) or {}
            return ", ".join(f"{names[i]} {'up' if v else 'down'}" for i, v in sorted(t.items())) or "nothing"
        return (f"Camera tab: {desc('camera')}. Scan tabs, scans and the scope: {desc('counter')}. "
                "Other mirrors (the excitation choice) are left alone. Not while a scan is running.")

    def _attach_camera_tab(self, hw):
        """Camera tab (qt3camera): live view from the setup's camera."""
        if "Camera" not in self.tab_pages or "camera" not in hw:
            return
        page = self.tab_pages["Camera"]
        try:
            from qt3utils.applications.qt3microscope.panels.camera import CameraTab
            for child in page.winfo_children():
                child.destroy()
            self.camera_tab = CameraTab(page, hw["camera"], setup=self.setup,
                                        stage=getattr(self, "stage_panel", None),
                                        set_status=self._chip_setter("camera"),
                                        scale=self.scale, hub=self.hub)
        except Exception as e:
            logger.exception("camera tab failed to start")
            self._tab_failed("Camera", e)
            return
        # Live view pauses while another tab is in front and resumes on return.
        def on_tab(_event=None):
            try:
                shown = self.tabs.select() == str(page)
            except tk.TclError:
                return
            self.camera_tab.set_visible(shown)
        self.tabs.bind("<<NotebookTabChanged>>", on_tab, add="+")
        self.window.after(100, on_tab)

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
        ttk.Label(page, text=f"Could not start this tab: {err}", foreground=C.red,
                  wraplength=self.px(600), justify="left").pack(anchor="nw", padx=20, pady=20)

    def _panel_failed(self, key, err):
        logger.exception("%s panel failed to start", key)
        self._clear_note(key)
        ttk.Label(self.sections[key].body, text=f"Could not start this panel: {err}",
                  foreground=C.red, wraplength=self.px(330), justify="left").pack(anchor="w")

    def quit(self):
        self.save_state()
        self.hub.close()
        self.window.destroy()

    # ----- help ------------------------------------------------------------
    def open_user_manual(self):
        """Open the offline copy of the user guide (help/user_manual.html) in the web browser."""
        import pathlib
        import webbrowser
        from tkinter import messagebox
        path = os.path.join(APP_DIR, "help", "user_manual.html")
        if not os.path.exists(path):
            messagebox.showerror("User Manual", f"The manual file is missing:\n{path}", parent=self.window)
            return
        logger.info("Opening the user manual: %s", path)
        try:
            webbrowser.open(pathlib.Path(path).as_uri())
        except Exception as e:
            messagebox.showerror("User Manual", f"Could not open the manual: {e}\nOpen it by hand:\n{path}",
                                 parent=self.window)

    # ----- preferences -----------------------------------------------------
    def open_preferences(self):
        prefs_mod.PreferencesDialog(self.window, prefs_mod.load(), self.profile_path, self._prefs_saved,
                                    scale=self.scale, config_locked=self.config_locked,
                                    auto_mirrors_text=self._auto_mirrors_text())

    def _prefs_saved(self, prefs, restart):
        was_verbose = bool(self.prefs.get("verbose_log", False))
        self.prefs = prefs
        if bool(prefs.get("verbose_log", False)) != was_verbose:
            set_verbose_logging(prefs.get("verbose_log", False))
            if not prefs.get("verbose_log", False):
                logger.info("Verbose logging is off.")
        if restart:
            self.restart()
        else:
            self._auto_for_tab()  # switched on: match the tab in front now

    def restart(self):
        """Close everything and start QT3 Microscope again with the same
        command line (the saved preferences are read again on start)."""
        from tkinter import messagebox
        if not messagebox.askokcancel(
                "Restart QT3 Microscope",
                "Restart now? A scan or measurement that is running will stop.", parent=self.window):
            return
        import subprocess
        logger.info("Restarting QT3 Microscope")
        self.save_state()
        self.hub.close()
        env = dict(os.environ, QT3MICROSCOPE_WAIT_PID=str(os.getpid()))
        cmd = [sys.executable, os.path.abspath(__file__)] + sys.argv[1:]
        try:
            subprocess.Popen(cmd, env=env, cwd=os.getcwd())
        except OSError as e:
            messagebox.showerror("Restart failed", f"Could not start again: {e}\nStart it from the shortcut.",
                                 parent=self.window)
        self.window.destroy()

    # ----- bottom ----------------------------------------------------------
    def _build_footer(self):
        foot = tk.Frame(self.window, bd=1, relief="sunken")
        foot.pack(side="bottom", fill="x", before=self.hpane)
        theme_name = "dark" if C.dark else "light"
        tk.Label(foot, text=f"Profile: {self.profile_path}   ·   {theme_name} theme   ·   Log: {LOG_PATH}",
                 fg=C.muted, font=("TkDefaultFont", 8)).pack(side="left", padx=6)


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
        # make_desktop_shortcut.ps1 gives the shortcuts the same ID, so Windows
        # shows the taskbar button as "QT3 Microscope" with its icon.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
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


def _wait_for_previous(timeout=15.0):
    """After File > Restart, wait until the old process has exited, so it has
    let go of the hardware (the XY micrometers allow one process at a time)."""
    pid = os.environ.pop("QT3MICROSCOPE_WAIT_PID", "")
    if not pid.isdigit():
        return
    pid = int(pid)
    if sys.platform == "win32":
        import ctypes
        SYNCHRONIZE = 0x00100000
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(SYNCHRONIZE, False, pid)
        if h:
            k32.WaitForSingleObject(h, int(timeout * 1000))
            k32.CloseHandle(h)
        return
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)


def main(is_root_process=True, argv=None):
    args = parse_args(argv if (argv is not None or is_root_process) else [])
    try:
        if is_root_process:
            _wait_for_previous()
        prefs = prefs_mod.load()
        set_verbose_logging(prefs.get("verbose_log", False))
        setup, path = prefs_mod.resolve_profile(prefs, args.setup, args.config)
        path = profile_path(setup, path)
        try:
            profile = load_profile(path=path)
        except Exception as e:
            if args.config or prefs["profile"] == "shortcut":
                raise
            # A broken profile chosen in Preferences must not lock the program out.
            print(f"WARNING: could not read the profile chosen in Preferences ({path}): {e}. "
                  f"Using the shortcut's profile instead; fix it in File > Preferences.")
            path = profile_path(args.setup)
            profile = load_profile(path=path)
        if is_root_process:
            enable_sharp_display()  # must happen before the Tk window exists
        window = tk.Tk(className="QT3 Microscope") if is_root_process else tk.Toplevel()
        if is_root_process:
            theme.apply(window, prefs["dark"])  # before any widget is built
        MicroscopeWindow(window, profile, path, prefs, config_locked=bool(args.config))
    except Exception as e:
        if not is_root_process:
            raise
        _startup_failed(e)
        sys.exit(1)
    if is_root_process:
        window.mainloop()


if __name__ == "__main__":
    main()
