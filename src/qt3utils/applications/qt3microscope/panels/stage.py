"""
Stage panel: set and step the piezo axes.

Formerly qdlmove: this is the qdlmove app (Set position, Step, Current, and
keyboard stepping) placed in a drawer section. The axes and their hardware
come from the profile, like the qdlmove YAML.

Differences from qdlmove:
- At launch the piezos go back to where they were when the program last
  closed, if that was less than 2 hours ago (RESTORE_WITHIN_S), otherwise to
  0, 0, 0 (as qdlmove did). The last position is kept in
  ~/.qt3utils/qt3microscope_piezos_<setup>.json, and a "Go to last" button
  moves back to it after a 0, 0, 0 start.
- Keyboard stepping ignores keys typed into entry boxes, and only works
  while this section is open, so arrow keys elsewhere in the window can't
  move the stage by accident.
- Moves run on the hub's DAQ worker; holding a key down adds the steps up
  instead of queueing hundreds of moves.
"""

import datetime
import importlib
import json
import logging
import os
import time
import tkinter as tk
from tkinter import ttk

from qt3utils.applications.qt3microscope.widgets import AMBER, GRAY, GREEN, RED
from qt3utils.applications.qt3microscope.theme import C

logger = logging.getLogger(__name__)

# key -> (axis index, direction)
STEP_KEYS = {
    "Left": (0, -1), "Right": (0, 1),
    "Down": (1, -1), "Up": (1, 1),
    "minus": (2, -1), "KP_Subtract": (2, -1),
    "plus": (2, 1), "equal": (2, 1), "KP_Add": (2, 1),
}
TYPING_CLASSES = {"Entry", "TEntry", "Spinbox", "TSpinbox", "Text", "TCombobox"}

STATE_DIR = os.path.join(os.path.expanduser("~"), ".qt3utils")
RESTORE_WITHIN_S = 2 * 3600  # same as the window state in main.py


def load_last_positions(path, n_axes):
    """(positions, saved time) from the state file, or (None, None)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        pos = [float(p) for p in saved["positions"]]
        when = float(saved["time"])
        if len(pos) != n_axes:
            return None, None
        return pos, when
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


def make_positioner(spec):
    """Build a positioner from {import_path, class_name, configure} (qdlmove YAML style)."""
    module = importlib.import_module(spec["import_path"])
    cls = getattr(module, spec["class_name"])
    obj = cls()
    obj.configure(spec.get("configure", {}) or {})
    return obj


class StagePanel:
    def __init__(self, section, hub, cfg, set_status=None, setup="default"):
        self.section = section
        self.worker = hub.worker("daq")
        self.axes = cfg.get("axes", [])
        self.precision = int(cfg.get("precision", 2))
        self.default_step = float(cfg.get("step", 0.1))
        self.title = cfg.get("title", "Piezos")
        self.set_status = set_status or (lambda value, color: None)
        self.positioners = [None] * len(self.axes)
        self.positions = [None] * len(self.axes)
        self._busy = [False] * len(self.axes)
        self._pending = [0.0] * len(self.axes)
        self._closing = False
        # Set by the window: returns a reason (str) while another tool (a scan)
        # owns the piezos, or "" when the stage is free to move.
        self.blocked = lambda: ""
        self.state_path = os.path.join(STATE_DIR, f"qt3microscope_piezos_{setup}.json")
        self.last_positions, self.last_time = load_last_positions(self.state_path, len(self.axes))
        self._saved = None
        self._build(section.body)
        self.window = section.winfo_toplevel()
        self._bindings = []
        hub.on_close(self.close)
        if self.axes:
            section.after(300, self._launch_move)

    # -- GUI ---------------------------------------------------------------
    def _build(self, body):
        grid = ttk.Frame(body)
        grid.pack(fill="x")
        for col, text in enumerate(("", "Set (µm)", "", "Step (µm)", "Current")):
            ttk.Label(grid, text=text, foreground=GRAY).grid(row=0, column=col, padx=2)
        self.rows = []
        for i, ax in enumerate(self.axes):
            r = i + 1
            ttk.Label(grid, text=ax.get("label", f"Axis {i + 1}"),
                      font=("TkDefaultFont", 10, "bold")).grid(row=r, column=0, sticky="w", padx=(0, 4))
            set_e = ttk.Entry(grid, width=8)
            set_e.insert(0, "0")
            set_e.grid(row=r, column=1, padx=2, pady=1)
            set_b = ttk.Button(grid, text="Set", width=4, command=lambda i=i: self.set_axis(i))
            set_b.grid(row=r, column=2, padx=2)
            set_e.bind("<Return>", lambda _e, i=i: self.set_axis(i))
            step_e = ttk.Entry(grid, width=7)
            step_e.insert(0, f"{self.default_step:g}")
            step_e.grid(row=r, column=3, padx=2)
            cur = ttk.Label(grid, text="—", width=9, anchor="e", relief="sunken")
            cur.grid(row=r, column=4, padx=2)
            self.rows.append({"set": set_e, "step": step_e, "cur": cur, "btn": set_b})

        last = ttk.Frame(body)
        last.pack(fill="x", pady=(6, 0))
        self.last_btn = ttk.Button(last, text="Go to last", command=self.go_to_last)
        self.last_btn.pack(side="left")
        self.last_var = tk.StringVar(value="")
        ttk.Label(last, textvariable=self.last_var, foreground=GRAY).pack(side="left", padx=6)
        self._show_last()

        keys = ttk.Frame(body)
        keys.pack(fill="x", pady=(6, 0))
        self.step_var = tk.IntVar(value=0)
        ttk.Checkbutton(keys, text="Arrow-key stepping", variable=self.step_var,
                        command=self._toggle_keys).pack(side="left")
        self.keys_note = tk.Label(keys, text="", fg=C.text)
        self.keys_note.pack(side="left", padx=6)
        self._note_bg = self.keys_note.cget("bg")

        self.msg_var = tk.StringVar(value=self._hardware_text())
        ttk.Label(body, textvariable=self.msg_var, foreground=GRAY, justify="left",
                  wraplength=int(330 * max(1.0, body.winfo_fpixels("1i") / 96.0))).pack(anchor="w", pady=(4, 0))

    def _hardware_text(self):
        chans = []
        for ax in self.axes:
            c = (ax.get("configure") or {})
            chans.append(f"{ax.get('label', '?')} {c.get('device_name', '')}/{c.get('write_channel', '')}")
        return ", ".join(chans) + "."

    # -- launch position and "Go to last" --------------------------------------
    def _fmt(self, positions):
        return ", ".join(f"{p:.{self.precision}f}" for p in positions) + " µm"

    def _when(self, t):
        d = datetime.datetime.fromtimestamp(t)
        today = datetime.date.today()
        if d.date() == today:
            return d.strftime("%H:%M today")
        if d.date() == today - datetime.timedelta(days=1):
            return d.strftime("%H:%M yesterday")
        return d.strftime("%Y-%m-%d %H:%M")

    def _show_last(self):
        if self.last_positions is None:
            self.last_var.set("No last position saved yet.")
            self.last_btn.configure(state="disabled")
        else:
            self.last_var.set(f"{self._fmt(self.last_positions)} ({self._when(self.last_time)})")
            self.last_btn.configure(state="normal")

    def _launch_move(self):
        """Last position if it was saved within RESTORE_WITHIN_S, else 0, 0, 0."""
        if self._closing:
            return
        recent = (self.last_positions is not None
                  and 0 <= time.time() - self.last_time <= RESTORE_WITHIN_S)
        targets = list(self.last_positions) if recent else [0.0] * len(self.axes)
        if recent:
            note = f"Launch: back to the last position, {self._fmt(targets)}."
        elif self.last_positions is not None:
            note = (f"Launch: to 0, 0, 0 (last position is over 2 hours old; "
                    f"Go to last moves back to it).")
        else:
            note = "Launch: to 0, 0, 0."
        logger.info(note)
        self.msg_var.set(note)
        self._fill_set_boxes(targets)
        for i, t in enumerate(targets):
            self._pending[i] = 0.0
            self._submit_move(i, t, keep_msg=True)

    def _fill_set_boxes(self, targets):
        for row, t in zip(self.rows, targets):
            row["set"].delete(0, "end")
            row["set"].insert(0, f"{t:.{self.precision}f}")

    def go_to_last(self):
        if self.last_positions is None:
            return
        if self.blocked():
            self.msg_var.set(self.blocked())
            return
        if any(self._busy):
            self.msg_var.set("Still moving, try again.")
            return
        self.msg_var.set(f"Going to the last position, {self._fmt(self.last_positions)}.")
        self._fill_set_boxes(self.last_positions)
        for i, t in enumerate(self.last_positions):
            self._pending[i] = 0.0
            self._submit_move(i, t, keep_msg=True)

    def _save_positions(self):
        if any(p is None for p in self.positions):
            return
        rounded = [round(p, 6) for p in self.positions]
        if rounded == self._saved:
            return
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"positions": rounded, "time": time.time()}, f)
            os.replace(tmp, self.state_path)
            self._saved = rounded
        except OSError as e:
            logger.warning("could not save piezo positions: %s", e)

    def _show_positions(self):
        for row, pos in zip(self.rows, self.positions):
            row["cur"].configure(text="—" if pos is None else f"{pos:.{self.precision}f}")
        known = [f"{ax.get('label', '?')} {p:.{self.precision}f}"
                 for ax, p in zip(self.axes, self.positions) if p is not None]
        summary = "  ".join(known) + " µm" if known else "—"
        self.section.set_summary(summary)
        if known:
            self.set_status(" ".join(f"{p:.1f}" for p in self.positions if p is not None), GREEN)
        self._save_positions()

    # -- keyboard stepping ----------------------------------------------------
    def rebind_keys(self):
        """The section moved to or from its own window: bind the keys there."""
        self._toggle_keys()

    def _toggle_keys(self):
        for window, seq, funcid in self._bindings:
            try:
                window.unbind(seq, funcid)
            except tk.TclError:
                pass
        self._bindings = []
        self.window = self.section.winfo_toplevel()
        if self.step_var.get():
            for key in STEP_KEYS:
                seq = f"<KeyPress-{key}>"
                self._bindings.append((self.window, seq, self.window.bind(seq, self._on_key, add="+")))
            labels = [ax.get("label", "?") for ax in self.axes]
            hint = "←/→ " + labels[0] if labels else ""
            if len(labels) > 1:
                hint += ", ↑/↓ " + labels[1]
            if len(labels) > 2:
                hint += ", +/− " + labels[2]
            self.keys_note.configure(text=f"ON: {hint}", bg=AMBER, fg="#000000")
        else:
            self.keys_note.configure(text="", bg=self._note_bg, fg=C.text)

    def _on_key(self, event):
        if self._closing or not self.step_var.get():
            return
        if self.blocked():
            self.msg_var.set(self.blocked())
            return
        if event.widget.winfo_class() in TYPING_CLASSES:
            return  # typing a number, not stepping
        w = event.widget
        while w is not None:  # the XY micrometer box has the keys while it has focus
            if getattr(w, "owns_arrow_keys", False):
                return
            w = getattr(w, "master", None)
        if not self.section.is_open:
            self.msg_var.set("Open the Stage section to step with the keys.")
            return
        axis, sign = STEP_KEYS.get(event.keysym, (None, 0))
        if axis is None or axis >= len(self.axes):
            return
        try:
            dx = float(self.rows[axis]["step"].get())
        except ValueError:
            self.msg_var.set("Step must be a number.")
            return
        if self.positions[axis] is None:
            self.msg_var.set(f"Set {self.axes[axis].get('label', 'the axis')} once before stepping.")
            return
        self._pending[axis] += sign * dx
        self._flush(axis)
        return "break"

    # -- moves --------------------------------------------------------------
    def set_axis(self, i):
        if self.blocked():
            self.msg_var.set(self.blocked())
            return
        try:
            target = float(self.rows[i]["set"].get())
        except ValueError:
            self.msg_var.set("Set position must be a number.")
            return
        if self._busy[i]:
            self.msg_var.set("Still moving, try again.")
            return
        self._pending[i] = 0.0
        self._submit_move(i, target)

    def _flush(self, i):
        if self._busy[i] or self._pending[i] == 0.0 or self.positions[i] is None:
            return
        target = self.positions[i] + self._pending[i]
        self._pending[i] = 0.0
        self._submit_move(i, target)

    def _submit_move(self, i, target, keep_msg=False):
        self._busy[i] = True
        self.worker.submit(f"move {self.axes[i].get('label', i)}",
                           lambda: self._daq_move(i, target),
                           lambda ok, res: self._moved(i, target, ok, res, keep_msg))

    def positioner(self, i):
        """The shared positioner for axis i (built on first use). Call on the DAQ worker."""
        if self.positioners[i] is None:
            self.positioners[i] = make_positioner(self.axes[i])
        return self.positioners[i]

    def axis_limits(self, i):
        c = self.axes[i].get("configure") or {}
        return float(c.get("min_position", -40.0)), float(c.get("max_position", 40.0))

    def refresh_positions(self):
        """Show the positions other tools (scans, go-to) left the piezos at."""
        for i, p in enumerate(self.positioners):
            if p is not None and hasattr(p, "get_last_commanded_position"):
                pos = p.get_last_commanded_position()
                if pos is not None:
                    self.positions[i] = pos
        self._show_positions()

    def _daq_move(self, i, target):
        p = self.positioner(i)
        lo, hi = getattr(p, "min_position", None), getattr(p, "max_position", None)
        if lo is not None and hi is not None and not lo <= target <= hi:
            raise ValueError(f"{target:g} µm is outside {lo:g} to {hi:g} µm")
        p.go_to_position(target)
        return target

    def _moved(self, i, target, ok, result, keep_msg=False):
        self._busy[i] = False
        if self._closing:
            return
        if ok:
            self.positions[i] = result
            if not keep_msg:
                self.msg_var.set(self._hardware_text())
            self._show_positions()
            self._flush(i)
        else:
            self._pending[i] = 0.0
            self.msg_var.set(f"{self.axes[i].get('label', 'Axis')}: {result}")
            self.set_status("error", RED)

    def close(self):
        self._closing = True
