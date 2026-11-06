"""
Flip mirrors panel: TTL flipper mounts on NI-DAQ digital outputs.

Same behaviour as the standalone qt3mirror app, placed in a drawer section:
Connect (which drives every line UP, as in qt3mirror), Initialize (sync to
UP), All up / All down, a Toggle per mirror and the preset buttons. Mirror
names, lines and presets come from the profile, in qt3mirror's YAML layout.

Writes run on the hub's DAQ worker. The 0.5 s settle time after each
command is waited out on the window side, so it doesn't hold up the DAQ.
"""

import logging
import time
import tkinter as tk
from tkinter import ttk

from qt3utils.applications.qt3microscope.widgets import GRAY, GREEN, ORANGE, RED

logger = logging.getLogger(__name__)

SETTLE_MS_AFTER_COMMAND = 500  # as qt3mirror


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
        self._build(section.body)
        self._update_states()
        hub.on_close(self.close)

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
            b = ttk.Button(pf, text=pr.label, command=lambda pr=pr: self._do(pr.label, "apply_partial", pr.targets_dict()))
            b.grid(row=j // 2, column=j % 2, sticky="ew", padx=1, pady=1)
            self.preset_btns.append(b)
        pf.columnconfigure(0, weight=1)
        pf.columnconfigure(1, weight=1)

        wrap = int(330 * max(1.0, body.winfo_fpixels("1i") / 96.0))
        self.msg_var = tk.StringVar(value="Not connected. Connect sets every mirror UP (as in qt3mirror).")
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
                lbl.configure(text="—", bg=self._idle_bg, fg="black")
            else:
                lbl.configure(text="UP" if lv else "DOWN", bg=GREEN if lv else GRAY, fg="white")
        if self._connected():
            summary = self._last_action or "connected"
            self.section.set_summary(summary)
            self.set_status(summary, GREEN)
        else:
            self.section.set_summary("not connected")
            self.set_status("off", GRAY)

    # -- actions -----------------------------------------------------------
    def connect(self):
        if self._busy or self._connected():
            return
        def work():
            from qt3utils.hardware.nidaq.digitaloutputs import FlipperMountController  # needs nidaqmx
            ctrl = FlipperMountController(self.cfg.device, line_channels=self.cfg.flipper_lines)
            ctrl.connect()
            return ctrl

        self._last_action = "all UP"
        self._run("connect", work, on_ok=lambda ctrl: setattr(self, "controller", ctrl))

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

    def _run(self, label, fn, on_ok=None):
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
            self.section.after(wait, lambda: self._finish(label, ok, result))

        self.worker.submit(f"flippers {label}", fn, done)

    def _finish(self, label, ok, result):
        if self._closing:
            return
        self._busy = False
        if ok:
            self.msg_var.set(f"{time.strftime('%H:%M:%S')}  {label}. Connected: {self.controller.channel_string}")
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
