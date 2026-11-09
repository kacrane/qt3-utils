"""
Preferences for QT3 Microscope (File > Preferences...).

Settings, kept in ~/.qt3utils/qt3microscope_prefs.json:

    dark     Light or dark colors (theme.py).
    profile  Which setup profile to load: "shortcut" (whatever the desktop
             shortcut or command line says, the default), "VQLM", "TQLM",
             or the path of another profile YAML file.
    auto_mirrors  Flip the mirrors to the camera when the Camera tab is
             opened, and to the photon counter when the scan tabs are
             opened or a scan or the scope starts (the profile's
             flippers: auto: section says which mirrors). The excitation
             mirror is left alone. Off by default.
    verbose_log  Verbose logging for diagnosing problems: every qt3utils
             message down to DEBUG goes to the Log section and
             qt3microscope.log. Off by default.

The theme and profile take effect when the program starts, so the dialog
offers to restart; auto_mirrors and verbose_log take effect at once.
A --config path given on the command line still wins over the preference.
"""

import json
import os
import tkinter as tk
from tkinter import filedialog, ttk

from qt3utils.applications.qt3microscope.theme import C

PREFS_PATH = os.path.join(os.path.expanduser("~"), ".qt3utils", "qt3microscope_prefs.json")
DEFAULTS = {"dark": False, "profile": "shortcut", "auto_mirrors": False, "verbose_log": False}


def load():
    prefs = dict(DEFAULTS)
    try:
        with open(PREFS_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            prefs.update({k: saved[k] for k in DEFAULTS if k in saved})
    except (OSError, ValueError):
        pass
    prefs["dark"] = bool(prefs["dark"])
    prefs["auto_mirrors"] = bool(prefs["auto_mirrors"])
    prefs["verbose_log"] = bool(prefs["verbose_log"])
    if not isinstance(prefs["profile"], str) or not prefs["profile"].strip():
        prefs["profile"] = "shortcut"
    return prefs


def save(prefs):
    os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
    tmp = PREFS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: prefs[k] for k in DEFAULTS}, f, indent=2)
    os.replace(tmp, PREFS_PATH)


def resolve_profile(prefs, setup_arg, config_arg):
    """(setup, path) to load: --config wins, then the preference, then --setup."""
    if config_arg:
        return None, config_arg
    choice = prefs.get("profile", "shortcut")
    if choice in ("VQLM", "TQLM"):
        return choice, None
    if choice != "shortcut":
        return None, choice
    return setup_arg, None


class PreferencesDialog:
    """Modal dialog. on_save(prefs, restart) is called when the user saves."""

    def __init__(self, parent, prefs, current_profile, on_save, scale=1.0, config_locked=False,
                 auto_mirrors_text=None):
        self.on_save = on_save
        self.top = top = tk.Toplevel(parent)
        top.title("Preferences")
        top.transient(parent)
        top.resizable(False, False)
        top.configure(background=C.bg)
        px = lambda v: int(round(v * scale))  # noqa: E731

        body = ttk.Frame(top, padding=px(14))
        body.pack(fill="both", expand=True)

        look = ttk.LabelFrame(body, text="Appearance", padding=px(8))
        look.pack(fill="x")
        self.dark = tk.BooleanVar(value=prefs["dark"])
        ttk.Radiobutton(look, text="Light", variable=self.dark, value=False).pack(anchor="w")
        ttk.Radiobutton(look, text="Dark", variable=self.dark, value=True).pack(anchor="w")

        mir = ttk.LabelFrame(body, text="Flip mirrors", padding=px(8))
        mir.pack(fill="x", pady=(px(10), 0))
        self.auto_mirrors = tk.BooleanVar(value=prefs.get("auto_mirrors", False))
        ttk.Checkbutton(mir, text="Switch the mirrors automatically", variable=self.auto_mirrors).pack(anchor="w")
        ttk.Label(mir, text=auto_mirrors_text or "This profile has no automatic mirror settings "
                  "(flippers: auto: in the profile).", foreground=C.muted, wraplength=px(420),
                  justify="left").pack(anchor="w", padx=(px(20), 0))

        diag = ttk.LabelFrame(body, text="Log", padding=px(8))
        diag.pack(fill="x", pady=(px(10), 0))
        self.verbose_log = tk.BooleanVar(value=prefs.get("verbose_log", False))
        ttk.Checkbutton(diag, text="Verbose logging", variable=self.verbose_log).pack(anchor="w")
        ttk.Label(diag, text="Writes every detail to the Log, for diagnosing problems. "
                  "Leave off for everyday use.", foreground=C.muted, wraplength=px(420),
                  justify="left").pack(anchor="w", padx=(px(20), 0))

        prof = ttk.LabelFrame(body, text="Setup profile", padding=px(8))
        prof.pack(fill="x", pady=(px(10), 0))
        choice = prefs["profile"]
        other = "" if choice in ("shortcut", "VQLM", "TQLM") else choice
        self.choice = tk.StringVar(value=choice if not other else "other")
        self.other = tk.StringVar(value=other)
        ttk.Radiobutton(prof, text="From the desktop shortcut", variable=self.choice,
                        value="shortcut", command=self._sync).pack(anchor="w")
        ttk.Radiobutton(prof, text="VQLM  (config_files/VQLM.yaml)", variable=self.choice,
                        value="VQLM", command=self._sync).pack(anchor="w")
        ttk.Radiobutton(prof, text="TQLM  (config_files/TQLM.yaml)", variable=self.choice,
                        value="TQLM", command=self._sync).pack(anchor="w")
        ttk.Radiobutton(prof, text="Another profile file:", variable=self.choice,
                        value="other", command=self._sync).pack(anchor="w")
        row = ttk.Frame(prof)
        row.pack(fill="x", padx=(px(20), 0), pady=(px(2), 0))
        self.entry = ttk.Entry(row, textvariable=self.other, width=44)
        self.entry.pack(side="left", fill="x", expand=True)
        self.browse = ttk.Button(row, text="Browse…", command=self._browse)
        self.browse.pack(side="left", padx=(px(4), 0))
        ttk.Label(prof, text=f"Now running: {current_profile}", foreground=C.muted,
                  wraplength=px(420), justify="left").pack(anchor="w", pady=(px(6), 0))
        if config_locked:
            ttk.Label(prof, text="This window was started with --config, which overrides this choice.",
                      foreground=C.orange, wraplength=px(420), justify="left").pack(anchor="w")

        self.msg = ttk.Label(body, text="Appearance and profile take effect when QT3 Microscope restarts; "
                                        "the mirror and log settings take effect at once.",
                             foreground=C.muted, wraplength=px(420), justify="left")
        self.msg.pack(anchor="w", pady=(px(10), 0))

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(px(10), 0))
        ttk.Button(btns, text="Cancel", command=top.destroy).pack(side="right")
        ttk.Button(btns, text="Save and restart", command=lambda: self._save(True)).pack(side="right", padx=px(4))
        ttk.Button(btns, text="Save", command=lambda: self._save(False)).pack(side="right")
        top.bind("<Escape>", lambda e: top.destroy())
        self._sync()

        top.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - top.winfo_reqwidth()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - top.winfo_reqheight()) // 3
        top.geometry(f"+{max(0, x)}+{max(0, y)}")
        try:
            top.grab_set()
        except tk.TclError:
            pass
        top.focus_set()

    def _sync(self):
        state = "normal" if self.choice.get() == "other" else "disabled"
        self.entry.configure(state=state)
        self.browse.configure(state=state)

    def _browse(self):
        start = os.path.dirname(self.other.get()) or os.path.join(os.path.dirname(__file__), "config_files")
        path = filedialog.askopenfilename(parent=self.top, title="Choose a setup profile",
                                          initialdir=start,
                                          filetypes=[("Profile", "*.yaml *.yml"), ("All files", "*.*")])
        if path:
            self.other.set(os.path.normpath(path))

    def _save(self, restart):
        choice = self.choice.get()
        if choice == "other":
            path = self.other.get().strip().strip('"')
            if not path or not os.path.isfile(path):
                self.msg.configure(text="Choose a profile file that exists.", foreground=C.red)
                return
            choice = os.path.abspath(path)
        prefs = {"dark": bool(self.dark.get()), "profile": choice, "auto_mirrors": bool(self.auto_mirrors.get()),
                 "verbose_log": bool(self.verbose_log.get())}
        try:
            save(prefs)
        except OSError as e:
            self.msg.configure(text=f"Could not save: {e}", foreground=C.red)
            return
        self.top.destroy()
        self.on_save(prefs, restart)
