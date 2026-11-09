"""
Power panel: excitation power at the objective from photodetector voltages.

Each detector in the profile has a DAQ channel and a polynomial that turns
volts into mW (highest power first, like numpy.polyval). VQLM uses the
quadratic from the standalone qt3power. Reads run on the hub's DAQ worker,
so they never overlap other DAQ jobs.
"""

import json
import logging
import os
import time
import tkinter as tk
from tkinter import ttk

from qt3utils.applications.qt3microscope.widgets import GRAY, GREEN, RED
from qt3utils.applications.qt3microscope.theme import C

logger = logging.getLogger(__name__)

BLUE = C.reading  # theme.apply() runs before this module is imported
MW_FONT = ("TkDefaultFont", 18, "bold")
V_FONT = ("TkDefaultFont", 9)
LARGE_SCALE = 8  # double-clicked readings are this many times bigger
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".qt3utils", "qt3microscope_power.json")


def _big(font):
    return (font[0], font[1] * LARGE_SCALE) + tuple(font[2:])


def _load_large():
    """Which readings were enlarged last time (remembered between sessions)."""
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return dict(json.load(f).get("large_readings", {}))
    except (OSError, ValueError):
        return {}


def _save_large(large):
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump({"large_readings": large}, f, indent=2)
    except OSError as e:
        logger.warning("could not save power settings: %r", e)


def volts_to_mw(volts, coeffs):
    """Evaluate the polynomial coeffs (highest power first) at volts."""
    p = 0.0
    for c in coeffs:
        p = p * volts + c
    return p


def read_voltages(device, channels, samples, v_range=(-10.0, 10.0)):
    """Read each channel `samples` times on demand and return the averages (V)."""
    import nidaqmx  # imported here so the window still opens without NI-DAQmx

    with nidaqmx.Task() as task:
        for ch in channels:
            task.ai_channels.add_ai_voltage_chan(f"{device}/{ch}", min_val=v_range[0], max_val=v_range[1])
        n = max(1, int(samples))
        sums = [0.0] * len(channels)
        for _ in range(n):  # software-timed: one sample per channel per read
            values = task.read()
            if len(channels) == 1:
                values = [values]
            for i, v in enumerate(values):
                sums[i] += float(v)
    return [x / n for x in sums]


class PowerPanel:
    def __init__(self, section, hub, cfg, set_status=None, on_layout=None):
        self.section = section
        self.worker = hub.worker("daq")
        self.device = cfg.get("device", "Dev1")
        self.detectors = cfg.get("detectors", [])
        self.interval_ms = int(cfg.get("interval_ms", 200))
        self.samples = int(cfg.get("samples", 10))
        self.set_status = set_status or (lambda value, colour: None)
        self.running = False
        self._waiting = False
        self._closing = False
        self.on_layout = on_layout or (lambda: None)
        self.large = _load_large()
        self._build(section.body)
        hub.on_close(self.close)
        if cfg.get("auto_start", True):
            self.start()

    def _build(self, body):
        self.rows = []
        for det in self.detectors:
            row = ttk.Frame(body)
            row.pack(fill="x", pady=2)
            name = ttk.Label(row, text=det.get("label", det["name"]), font=("TkDefaultFont", 10, "bold"))
            name.pack(side="left")
            mw = tk.Label(row, text="--", font=MW_FONT, fg=BLUE)
            mw_unit = ttk.Label(row, text="mW")
            volts = tk.Label(row, text="", font=V_FONT, fg=GRAY)
            # Enlarged copies, each on its own line under the row (hidden until double-clicked).
            big_mw_line = ttk.Frame(body)
            big_mw = tk.Label(big_mw_line, text="--", font=_big(MW_FONT), fg=BLUE, padx=12)
            big_mw.pack(side="left")
            # Extra side padding: some systems draw big glyphs slightly wider than measured.
            tk.Label(big_mw_line, text="mW", font=_big(("TkDefaultFont", 10)),
                     padx=12).pack(side="left", anchor="s")
            big_v_line = ttk.Frame(body)
            big_v = tk.Label(big_v_line, text="", font=_big(V_FONT), fg=C.text, padx=12)
            big_v.pack(side="left")
            r = {"key": det["name"], "row": row, "name": name, "mw": mw, "mw_unit": mw_unit,
                 "volts": volts, "big_mw_line": big_mw_line, "big_mw": big_mw,
                 "big_v_line": big_v_line, "big_v": big_v}
            for w in (mw, big_mw, *big_mw_line.winfo_children()):
                w.bind("<Double-Button-1>", lambda _e, r=r: self.toggle_size(r, "mw"))
            for w in (volts, big_v):
                w.bind("<Double-Button-1>", lambda _e, r=r: self.toggle_size(r, "v"))
            self.rows.append(r)
            self._show_size(r)
        ttk.Label(body, foreground=GRAY, text=", ".join(
            f"{d['name']} on {self.device}/{d['channel']}" for d in self.detectors)).pack(anchor="w")
        ttk.Label(body, foreground=GRAY, text="Double-click a reading to enlarge it.").pack(anchor="w")

        ctrl = ttk.Frame(body)
        ctrl.pack(fill="x", pady=(6, 0))
        self.start_btn = tk.Button(ctrl, text="Start", bg=C.go, fg="white", width=8, command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = tk.Button(ctrl, text="Stop", bg=C.stop, fg="white", width=8, command=self.stop)
        self.stop_btn.pack(side="left", padx=6)
        self.state_var = tk.StringVar(value="Stopped")
        ttk.Label(ctrl, textvariable=self.state_var).pack(side="left", padx=6)
        self.msg_var = tk.StringVar(value="")
        wrap = int(330 * max(1.0, body.winfo_fpixels("1i") / 96.0))
        ttk.Label(body, textvariable=self.msg_var, foreground=GRAY, wraplength=wrap,
                  justify="left").pack(anchor="w", pady=(4, 0))

    # -- double-click to enlarge (for reading across the room while aligning)
    def _is_large(self, r, kind):
        return bool(self.large.get(f"{r['key']}_{kind}", False))

    def _show_size(self, r):
        for w in ("mw", "mw_unit", "volts", "big_mw_line", "big_v_line"):
            r[w].pack_forget()
        if self._is_large(r, "mw"):
            r["big_mw_line"].pack(fill="x", after=r["row"])
        else:
            r["mw"].pack(side="left", padx=(10, 2))
            r["mw_unit"].pack(side="left")
        if self._is_large(r, "v"):
            r["big_v_line"].pack(fill="x", after=r["big_mw_line"] if self._is_large(r, "mw") else r["row"])
        else:
            r["volts"].pack(side="right")

    def toggle_size(self, r, kind):
        key = f"{r['key']}_{kind}"
        self.large[key] = not self.large.get(key, False)
        self._show_size(r)
        _save_large(self.large)
        self.on_layout()

    def start(self):
        if not self.detectors:
            self.state_var.set("No detectors in the profile")
            return
        self.running = True
        self.state_var.set("Running")
        self.msg_var.set("")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._tick()

    def stop(self):
        self.running = False
        self.state_var.set("Stopped")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def _tick(self):
        if not self.running or self._closing:
            return
        if not self._waiting:  # skip a tick rather than queue up reads
            self._waiting = True
            channels = [d["channel"] for d in self.detectors]
            self.worker.submit("power read",
                               lambda: read_voltages(self.device, channels, self.samples),
                               self._done, quiet=True)
        self.section.after(self.interval_ms, self._tick)

    def _done(self, ok, result):
        self._waiting = False
        if self._closing:
            return
        if not ok:
            self.stop()
            self.state_var.set("DAQ error")
            self.msg_var.set(f"{time.strftime('%H:%M:%S')}  {result}. Press Start to try again.")
            self.section.set_summary("DAQ error")
            self.set_status("error", RED)
            return
        powers = []
        for det, r, v in zip(self.detectors, self.rows, result):
            p = volts_to_mw(v, det.get("v_to_mw", [1 / 7.0, 0.0]))
            powers.append(p)
            for k in ("mw", "big_mw"):
                r[k].configure(text=f"{p:.4f}")
            for k in ("volts", "big_v"):
                r[k].configure(text=f"{v:.4f} V")
        summary = " · ".join(f"{p:.4f}" for p in powers) + " mW"
        self.section.set_summary(summary)
        self.set_status(summary, GREEN)

    def close(self):
        self._closing = True
        self.running = False
