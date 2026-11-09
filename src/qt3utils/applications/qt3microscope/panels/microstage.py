"""
XY micrometers box (TQLM): the MCL MicroDrive stage, formerly qt3move's
microstage controls, always on screen beside the scope.

Lock: one click toggles it. While locked, Set, the step arrows, arrow keys,
Home and Center are grayed out, and every move is refused where the stage is
driven (move()), not only in the buttons, with "Microstage locked" in the
log. The readout keeps updating.

The stage has no encoders: it keeps its position by counting steps from the
homed corner. The last position, whether it was homed, and the Lock state
are saved to ~/.qt3utils/qt3microscope_microstage_<setup>.json after every
move and restored at startup, so the stage opens as it was left. The first
launch starts locked and not homed.

Directions follow qt3move: Left (◀) is +X, Right (▶) is -X, Up (▲) is +Y.
Arrow keys only act while "Arrow keys" is ticked and the box has keyboard
focus (click anywhere in the box), so typing elsewhere never moves it.
"""

import datetime
import importlib
import json
import logging
import os
import tkinter as tk
from tkinter import ttk

from qt3utils.applications.qt3microscope.widgets import AMBER, GRAY, GREEN, ORANGE, RED
from qt3utils.applications.qt3microscope.theme import C

logger = logging.getLogger(__name__)

STATE_DIR = os.path.join(os.path.expanduser("~"), ".qt3utils")
KEYS = {"Left": ("x", 1), "Right": ("x", -1), "Up": ("y", 1), "Down": ("y", -1)}
TYPING_CLASSES = {"Entry", "TEntry", "Spinbox", "TSpinbox", "Text", "TCombobox"}


class MicrostagePanel(ttk.Frame):
    def __init__(self, parent, hub, cfg, setup="TQLM", scale=1.0):
        super().__init__(parent, padding=(10, 4), relief="solid", borderwidth=1, takefocus=1)
        self.owns_arrow_keys = True  # the piezo stage leaves arrow keys alone while this has focus
        self.worker = hub.worker("microstage")
        self.cfg = cfg or {}
        self.limits = dict(self.cfg.get("limits") or {})
        self.state_path = os.path.join(STATE_DIR, f"qt3microscope_microstage_{setup}.json")
        self.stage = None
        self.homed = False
        self.busy = False
        self.position = (None, None)
        self._closing = False
        self._warned_unhomed = False
        saved = self._load()
        self.locked = bool(saved.get("locked", True))  # first launch: locked
        self._saved = saved

        self.scale = scale
        self._build()
        self._refresh()
        self.bind("<Button-1>", lambda e: self.focus_set(), add="+")
        self.after(200, self._bind_keys)
        hub.on_close(self.close)
        self._connect()

    # -- layout --------------------------------------------------------------
    def _build(self):
        scale = self.scale
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="XY micrometers", font=("TkDefaultFont", 10, "bold")).pack(side="left")
        self.lock_btn = tk.Button(top, width=10, command=self.toggle_lock, font=("TkDefaultFont", 9, "bold"))
        self.lock_btn.pack(side="right")
        # Shown only when the stage could not be connected.
        self.connect_btn = ttk.Button(top, text="Connect", width=8, command=self._connect)
        self.state_lbl = tk.Label(top, text="Connecting...", fg=GRAY)
        self.state_lbl.pack(side="right", padx=8)

        grid = ttk.Frame(self)
        grid.pack(fill="x", pady=(4, 0))
        self.move_widgets = []
        self.readouts, self.entries = {}, {}
        for row, axis in enumerate(("x", "y")):
            ttk.Label(grid, text=axis.upper(), width=2).grid(row=row, column=0, sticky="w")
            ro = ttk.Label(grid, text="—", width=10, relief="sunken", anchor="e")
            ro.grid(row=row, column=1, padx=2, pady=1)
            ttk.Label(grid, text="µm").grid(row=row, column=2)
            e = ttk.Entry(grid, width=10)
            e.grid(row=row, column=3, padx=4)
            e.bind("<Return>", lambda ev, a=axis: self.set_axis(a))
            b = ttk.Button(grid, text="Set", width=5, command=lambda a=axis: self.set_axis(a))
            b.grid(row=row, column=4)
            self.readouts[axis], self.entries[axis] = ro, e
            self.move_widgets += [e, b]

        steps = ttk.Frame(self)
        steps.pack(fill="x", pady=(4, 0))
        ttk.Label(steps, text="Step (µm)").pack(side="left")
        self.step_e = ttk.Entry(steps, width=7)
        self.step_e.insert(0, str(self.cfg.get("step", 10)))
        self.step_e.pack(side="left", padx=4)
        self.move_widgets.append(self.step_e)
        for text, axis, sign in (("◀", "x", 1), ("▲", "y", 1), ("▼", "y", -1), ("▶", "x", -1)):
            b = ttk.Button(steps, text=text, width=3, command=lambda a=axis, s=sign: self.step(a, s))
            b.pack(side="left", padx=1)
            self.move_widgets.append(b)
        self.home_btn = ttk.Button(steps, text="Home", width=6, command=self.home)
        self.home_btn.pack(side="left", padx=(8, 1))
        self.center_btn = ttk.Button(steps, text="Center", width=7, command=self.center)
        self.center_btn.pack(side="left", padx=1)
        self.move_widgets += [self.home_btn, self.center_btn]

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", pady=(4, 0))
        self.keys_var = tk.BooleanVar(value=False)
        self.keys_chk = ttk.Checkbutton(bottom, text="Arrow keys (click this box first)", variable=self.keys_var)
        self.keys_chk.pack(side="left")
        self.move_widgets.append(self.keys_chk)
        self.note = ttk.Label(self, foreground=C.muted, wraplength=int(380 * scale), justify="left")
        self.note.pack(anchor="w", pady=(2, 0))
        lim = self.limits
        if lim:
            self.limits_text = (f"Limits X {lim.get('x_min', 0):g}–{lim.get('x_max', 0):g}, "
                                f"Y {lim.get('y_min', 0):g}–{lim.get('y_max', 0):g} µm")
        else:
            self.limits_text = ""

    def _refresh(self):
        if self.locked:
            self.lock_btn.config(text="Locked", bg=AMBER, activebackground=AMBER,
                                fg="#000000", activeforeground="#000000")  # dark text on amber in both themes
            state = "disabled"
        else:
            self.lock_btn.config(text="Unlocked", bg=C.button, activebackground=C.button,
                                fg=C.text, activeforeground=C.text)
            state = "normal" if (self.stage is not None and not self.busy) else "disabled"
        for w in self.move_widgets:
            w.configure(state=state)
        if state == "normal" and not self.homed:
            self.center_btn.configure(state="disabled")  # needs a homed stage, as in qt3move
        for axis, val in zip(("x", "y"), self.position):
            self.readouts[axis].configure(text="—" if val is None else f"{val:.1f}")
        if self.stage is None:
            return
        if self.busy:
            self.state_lbl.configure(text=self.busy, fg=ORANGE)
        elif not self.homed:
            self.state_lbl.configure(text="Not homed", fg=ORANGE)
        else:
            self.state_lbl.configure(text="Ready", fg=GREEN)
        if self.locked:
            self.note.configure(text="Locked: no moves from buttons, arrow keys or Home.")
        elif not self.homed:
            self.note.configure(text="Not homed: press Home to find (0, 0). Set and Center need it.")
        else:
            self.note.configure(text=self.limits_text)

    def summary_text(self):
        x, y = self.position
        where = "—" if x is None else f"{x:.0f}, {y:.0f} µm"
        return f"XY {where}  " + ("Locked" if self.locked else "Unlocked")

    # -- saved state -----------------------------------------------------------
    def _load(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        x, y = self.position
        data = {"locked": self.locked, "homed": self.homed, "x_um": x, "y_um": y,
                "saved_at": datetime.datetime.now().isoformat(timespec="seconds")}
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, self.state_path)
        except OSError as e:
            logger.warning("could not save the microstage state: %r", e)

    # -- hardware (jobs run on the microstage worker) ---------------------------
    def _connect(self):
        saved = self._saved
        self.connect_btn.pack_forget()
        self.state_lbl.configure(text="Connecting...", fg=GRAY)

        def job():
            mod = importlib.import_module(self.cfg.get(
                "import_path", "qt3utils.applications.qt3move.microstage.encoderless_wrapper"))
            conf = dict(self.limits)
            conf["velocity"] = float(self.cfg.get("velocity", 0.5))
            stage = getattr(mod, self.cfg.get("class_name", "EncoderlessMicrostage"))(conf)
            restored = False
            if saved.get("homed") and saved.get("x_um") is not None and saved.get("y_um") is not None:
                stage.set_position(float(saved["x_um"]), float(saved["y_um"]))
                restored = True
            return stage, restored, stage.get_position()

        self.worker.submit("microstage connect", job, self._connected)

    def _connected(self, ok, result):
        if self._closing:
            return
        if not ok:
            self.state_lbl.configure(text="Not connected", fg=RED)
            text = f"Could not connect: {result}"
            if "-8" in str(result) or "Invalid Handle" in str(result):
                # MCL_InitHandle found no free Micro-Drive: another program has it.
                text = ("Could not connect: the stage is in use or unplugged. Close qt3move "
                        "(and any other QT3 Microscope), then press Connect.")
            self.note.configure(text=text)
            self.connect_btn.pack(side="right", padx=4, before=self.state_lbl)
            logger.warning("XY micrometers: could not connect: %r", result)
            return
        self.stage, restored, pos = result
        self.homed = restored
        self.position = pos
        self._refresh()
        if restored:
            print(f"XY micrometers: position ({pos[0]:.1f}, {pos[1]:.1f}) µm restored from "
                  f"{self._saved.get('saved_at', 'the last session')}. Press Home if the stage was moved since.")

    def move(self, label, fn):
        """Every move goes through here, so the Lock holds whatever asks for it."""
        if self.locked:
            print("Microstage locked: move refused.")
            self.note.configure(text="Microstage locked: unlock to move.")
            return False
        if self.stage is None or self.busy:
            return False
        self.busy = label
        self._refresh()

        def job():
            fn(self.stage)
            return self.stage.get_position()

        self.worker.submit(f"microstage {label}", job, self._moved)
        return True

    def _moved(self, ok, result):
        if self._closing:
            return
        homing = self.busy == "Homing..."
        self.busy = False
        if ok:
            self.position = result
            if homing:
                self.homed = True
        else:
            self.note.configure(text=f"Move failed: {result}")
        self._save()
        self._refresh()
        if not ok:
            self.note.configure(text=f"Move failed: {result}")

    # -- actions -------------------------------------------------------------------
    def toggle_lock(self):
        self.locked = not self.locked
        self._save()
        self._refresh()

    def _step_size(self):
        try:
            return abs(float(self.step_e.get()))
        except ValueError:
            self.note.configure(text="Step must be a number.")
            return None

    def step(self, axis, sign):
        d = self._step_size()
        if d is None or self.position[0] is None:
            return
        if not self.homed and not self._warned_unhomed:
            self._warned_unhomed = True
            print("XY micrometers: stepping before Home; the limits are only meaningful once homed.")
        x, y = self.position
        if axis == "x":
            x += sign * d
        else:
            y += sign * d
        self.move("Moving...", lambda st: st.move_to(x, y))

    def set_axis(self, axis):
        if not self.homed:
            self.note.configure(text="Press Home first: Set needs a homed stage.")
            return
        try:
            target = float(self.entries[axis].get())
        except ValueError:
            self.note.configure(text="Type a position in µm.")
            return
        x, y = self.position
        if axis == "x":
            x = target
        else:
            y = target
        self.move("Moving...", lambda st: st.move_to(x, y))

    def home(self):
        self.move("Homing...", lambda st: st.find_home())

    def center(self):
        if not self.homed:
            return
        lim = self.limits
        cx = (float(lim.get("x_min", 0)) + float(lim.get("x_max", 12000))) / 2
        cy = (float(lim.get("y_min", 0)) + float(lim.get("y_max", 12000))) / 2
        self.move("Moving...", lambda st: st.move_to(cx, cy))

    # -- arrow keys ------------------------------------------------------------------
    def _bind_keys(self):
        top = self.winfo_toplevel()
        for key in KEYS:
            top.bind(f"<KeyPress-{key}>", self._on_key, add="+")

    def _on_key(self, event):
        if not self.keys_var.get() or self.locked:
            return
        if event.widget.winfo_class() in TYPING_CLASSES:
            return
        focus = self.focus_get()
        if focus is None or not str(focus).startswith(str(self)):
            return  # only while this box has keyboard focus
        axis, sign = KEYS[event.keysym]
        self.step(axis, sign)

    def close(self):
        self._closing = True
        self._save()
        stage = self.stage
        if stage is not None:
            self.worker.submit("microstage close", stage.close)
