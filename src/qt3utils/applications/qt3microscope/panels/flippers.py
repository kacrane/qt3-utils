"""
Flip mirrors panel: TTL flipper mounts on NI-DAQ digital outputs.

Same controls as the standalone qt3mirror app, placed in a drawer section:
Connect, Initialize (sync to UP), All up / All down, a Toggle per mirror
and the preset buttons.

Unlike qt3mirror, the panel connects by itself when the window opens and
puts every line back to the level it had when the window last closed
(remembered in ~/.qt3utils/qt3microscope_flippers.json). With no saved
state yet, it sets every line UP, as qt3mirror's Connect does. Mirror
names, lines and presets come from the profile, in qt3mirror's YAML layout.

Writes run on the hub's DAQ worker. The 0.5 s settle time after each
command is waited out on the window side, so it doesn't hold up the DAQ.
"""

import json
import logging
import os
import time
import tkinter as tk
from tkinter import ttk

from qt3utils.applications.qt3microscope.widgets import GRAY, GREEN, ORANGE, RED
from qt3utils.applications.qt3microscope.theme import C

logger = logging.getLogger(__name__)

SETTLE_MS_AFTER_COMMAND = 500  # as qt3mirror
STATE_PATH = os.path.join(os.path.expanduser("~"), ".qt3utils", "qt3microscope_flippers.json")


def daq_label(line):
    """Name a DO line the way the USB-6343 front panel does: port1/lineN -> PFIN, port2/lineN -> PFI(8+N)."""
    spec = line.strip().strip("/").split("/")
    if len(spec) >= 2 and spec[-2] in ("port1", "port2") and spec[-1].startswith("line"):
        try:
            n = int(spec[-1][4:])
        except ValueError:
            return line
        return f"PFI{n + (8 if spec[-2] == 'port2' else 0)}"
    return "/".join(spec[-2:])  # port0 lines are on the D connector; keep portX/lineY


def load_state(n):
    """Last line levels and action, or (None, "") if there is no usable saved state."""
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        levels = [bool(x) for x in data["levels"]]
        if len(levels) == n:
            return levels, str(data.get("last_action", ""))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None, ""


def save_state(levels, last_action):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"levels": list(levels), "last_action": last_action,
                       "saved": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=2)
    except OSError as e:
        logger.warning("could not save flipper state: %r", e)


class FlippersPanel:
    def __init__(self, section, hub, cfg, set_status=None):
        from qt3utils.applications.qt3mirror.config import load_flipper_config_dict
        self.section = section
        self.worker = hub.worker("daq")
        self.cfg = load_flipper_config_dict(cfg)
        self.set_status = set_status or (lambda value, colour: None)
        self.controller = None
        self._busy = False
        self._closing = False
        self._last_action = ""
        # Automatic switching (File > Preferences): mirror index -> level for
        # "camera" and "counter", from the profile's flippers: auto: section.
        self.auto_enabled = lambda: False  # set by the window from the preferences
        self.auto_targets = {}
        names = list(self.cfg.flipper_names)
        for mode, targets in ((cfg.get("auto") or {}).items()):
            try:
                self.auto_targets[mode] = {names.index(n): str(v).lower() in ("up", "true", "1")
                                           for n, v in (targets or {}).items()}
            except ValueError as e:
                logger.warning("flippers auto %s: unknown mirror name (%s)", mode, e)
        self._build(section.body)
        self._update_states()
        hub.on_close(self.close)
        if cfg.get("auto_connect", True):
            self.connect()

    # -- GUI ---------------------------------------------------------------
    def _build(self, body):
        top = ttk.Frame(body)
        top.pack(fill="x")
        self.connect_btn = ttk.Button(top, text="Connect", command=self.connect)
        self.connect_btn.pack(side="left")
        self.disconnect_btn = ttk.Button(top, text="Disconnect", command=self.disconnect)
        self.disconnect_btn.pack(side="left", padx=4)
        self.sync_btn = ttk.Button(top, text="Initialize (sync UP)", command=self.sync_all_up)
        self.sync_btn.pack(side="left")

        grid = ttk.Frame(body)
        grid.pack(fill="x", pady=(6, 0))
        self.all_up_btn = ttk.Button(grid, text="All up", command=lambda: self._do("All up", "all_up"))
        self.all_up_btn.grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.all_down_btn = ttk.Button(grid, text="All down", command=lambda: self._do("All down", "all_down"))
        self.all_down_btn.grid(row=0, column=1, columnspan=2, sticky="w", pady=(0, 4))
        self.state_lbls, self.toggle_btns = [], []
        for i, name in enumerate(self.cfg.flipper_names):
            ttk.Label(grid, text=name, font=("TkDefaultFont", 10, "bold")).grid(row=i + 1, column=0, sticky="w")
            ttk.Label(grid, text=daq_label(self.cfg.flipper_lines[i]), foreground=GRAY).grid(
                row=i + 1, column=3, sticky="w", padx=(6, 0))
            lbl = tk.Label(grid, text="—", width=6)
            lbl.grid(row=i + 1, column=1, padx=6, pady=1)
            btn = ttk.Button(grid, text="Toggle", width=7, command=lambda i=i, n=name: self._do(f"Toggle {n}", "toggle", i))
            btn.grid(row=i + 1, column=2, pady=1)
            self.state_lbls.append(lbl)
            self.toggle_btns.append(btn)

        ttk.Label(body, text="Presets", foreground=GRAY).pack(anchor="w", pady=(6, 0))
        pf = ttk.Frame(body)
        pf.pack(fill="x")
        self.preset_btns = []
        for j, pr in enumerate(self.cfg.presets):
            # tk buttons, so the preset the mirrors are in now can be coloured.
            b = tk.Button(pf, text=pr.label, command=lambda pr=pr: self._do(pr.label, "apply_partial", pr.targets_dict()))
            b.preset_targets = pr.targets_dict()
            b.normal_look = {"bg": b.cget("bg"), "fg": b.cget("fg"), "font": b.cget("font"),
                             "activebackground": b.cget("activebackground")}
            b.grid(row=j // 2, column=j % 2, sticky="ew", padx=1, pady=1)
            self.preset_btns.append(b)
        pf.columnconfigure(0, weight=1)
        pf.columnconfigure(1, weight=1)

        wrap = int(330 * max(1.0, body.winfo_fpixels("1i") / 96.0))
        self.msg_var = tk.StringVar(value="Not connected.")
        ttk.Label(body, textvariable=self.msg_var, foreground=GRAY, wraplength=wrap,
                  justify="left").pack(anchor="w", pady=(6, 0))
        self._idle_bg = self.state_lbls[0].cget("bg") if self.state_lbls else None

    def _connected(self):
        return self.controller is not None and self.controller.connected

    def _update_states(self):
        c, busy = self._connected(), self._busy
        on = "normal" if (c and not busy) else "disabled"
        self.connect_btn.configure(state="disabled" if (busy or c) else "normal")
        self.disconnect_btn.configure(state="disabled" if (busy or not c) else "normal")
        for b in (self.sync_btn, self.all_up_btn, self.all_down_btn, *self.toggle_btns, *self.preset_btns):
            b.configure(state=on)

    def _show_levels(self):
        levels = self.controller.levels if self._connected() else [None] * len(self.state_lbls)
        for lbl, lv in zip(self.state_lbls, levels):
            if lv is None:
                lbl.configure(text="—", bg=self._idle_bg, fg=C.text)
            else:
                lbl.configure(text="UP" if lv else "DOWN", bg=GREEN if lv else GRAY, fg="white")
        self._show_active_preset(levels)
        if self._connected():
            summary = self._last_action or "connected"
            self.section.set_summary(summary)
            self.set_status(summary, GREEN)
        else:
            self.section.set_summary("not connected")
            self.set_status("off", GRAY)

    def _show_active_preset(self, levels):
        """Colour the preset buttons whose mirrors all match the levels now."""
        for b in self.preset_btns:
            targets = getattr(b, "preset_targets", None) or {}
            active = (self._connected() and bool(targets)
                      and all(i < len(levels) and levels[i] is not None and bool(levels[i]) == bool(v)
                              for i, v in targets.items()))
            if active:
                b.configure(bg=C.accent, fg="#000000" if C.dark else "white", activebackground=C.accent,
                            font=("TkDefaultFont", 9, "bold"))
            else:
                b.configure(**b.normal_look)

    # -- actions -----------------------------------------------------------
    def connect(self):
        if self._busy or self._connected():
            return
        levels, last_action = load_state(len(self.cfg.flipper_lines))

        def work():
            from qt3utils.hardware.nidaq.digitaloutputs import FlipperMountController  # needs nidaqmx
            ctrl = FlipperMountController(self.cfg.device, line_channels=self.cfg.flipper_lines)
            ctrl.connect(initial_levels=levels)
            return ctrl

        if levels is None:
            self._last_action, label = "all UP", "Connect (no saved state: all UP)"
        else:
            self._last_action, label = last_action or "last state", "Connect (restored last state)"
        self._run(label, work, on_ok=lambda ctrl: setattr(self, "controller", ctrl))

    def disconnect(self):
        if self.controller is not None:
            ctrl, self.controller = self.controller, None
            self.worker.submit("flippers close", ctrl.close, quiet=True)
        self._last_action = ""
        self.msg_var.set("Disconnected.")
        self._show_levels()
        self._update_states()

    def sync_all_up(self):
        lo, hi = self.cfg.sync_pulse_low_ms, self.cfg.sync_pulse_high_ms
        self._do("Initialize (sync UP)", "sync_all_up", lo, hi)

    def _do(self, label, method, *args):
        if self._busy or not self._connected():
            return
        ctrl = self.controller
        self._last_action = label
        self._run(label, lambda: getattr(ctrl, method)(*args))

    AUTO_LABELS = {"camera": "Auto: camera", "counter": "Auto: counter"}

    def auto_switch(self, mode, then=None, label=None):
        """Put the mirrors in the "camera" or "counter" position if automatic
        switching is on, then call then() (after the mirrors have settled).
        Only the mirrors listed for that mode move, so the excitation choice
        stays as it is. then() runs whatever happens, so a scan never hangs."""
        then = then or (lambda: None)
        targets = self.auto_targets.get(mode)
        if not targets or not self.auto_enabled() or not self._connected() or self._closing:
            then()
            return
        if self._busy:  # a button press is still going: try again shortly
            self.section.after(150, lambda: self.auto_switch(mode, then, label))
            return
        current = self.controller.levels
        updates = {i: v for i, v in targets.items() if current[i] != v}
        if not updates:
            then()
            return
        ctrl = self.controller
        self._last_action = label or self.AUTO_LABELS.get(mode, f"Auto: {mode}")
        self._run(self._last_action, lambda: ctrl.apply_partial(updates), after=then)

    def _run(self, label, fn, on_ok=None, after=None):
        self._busy = True
        self._update_states()
        self.msg_var.set(f"{label}...")
        started = time.time()

        def done(ok, result):
            if self._closing:
                return
            if ok and on_ok:
                on_ok(result)
            wait = max(0, int(SETTLE_MS_AFTER_COMMAND - (time.time() - started) * 1000))
            self.section.after(wait, lambda: self._finish(label, ok, result, after))

        self.worker.submit(f"flippers {label}", fn, done)

    def _finish(self, label, ok, result, after=None):
        if self._closing:
            return
        try:
            self._finish_show(label, ok, result)
        finally:
            if after is not None:
                after()

    def _finish_show(self, label, ok, result):
        self._busy = False
        if ok:
            self.msg_var.set(f"{time.strftime('%H:%M:%S')}  {label}")
            save_state(self.controller.levels, self._last_action)
        else:
            self._last_action = ""
            self.msg_var.set(f"{time.strftime('%H:%M:%S')}  {label} failed: {result}")
            self.set_status("error", RED)
        self._show_levels()
        if not ok:
            self.set_status("error", RED)
        self._update_states()

    def close(self):
        self._closing = True
        if self.controller is not None:
            self.worker.submit("flippers close", self.controller.close, quiet=True)
