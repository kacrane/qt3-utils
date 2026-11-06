"""
Power panel: excitation power at the objective from photodetector voltages.

Each detector in the profile has a DAQ channel and a polynomial that turns
volts into mW (highest power first, like numpy.polyval). VQLM uses the
quadratic from the standalone qt3power. Reads run on the hub's DAQ worker,
so they never overlap other DAQ jobs.
"""

import logging
import time
import tkinter as tk
from tkinter import ttk

from qt3utils.applications.qt3microscope.widgets import GRAY, GREEN, RED

logger = logging.getLogger(__name__)


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
    def __init__(self, section, hub, cfg, set_status=None):
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
        self._build(section.body)
        hub.on_close(self.close)
        if cfg.get("auto_start", True):
            self.start()

    def _build(self, body):
        self.rows = []
        for det in self.detectors:
            row = ttk.Frame(body)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=det.get("label", det["name"]),
                      font=("TkDefaultFont", 10, "bold")).pack(side="left")
            mw = tk.Label(row, text="--", font=("TkDefaultFont", 18, "bold"), fg="#1d4fb0")
            mw.pack(side="left", padx=(10, 2))
            ttk.Label(row, text="mW").pack(side="left")
            volts = ttk.Label(row, text="", foreground=GRAY)
            volts.pack(side="right")
            self.rows.append((mw, volts))
        ttk.Label(body, foreground=GRAY, text=", ".join(
            f"{d['name']} on {self.device}/{d['channel']}" for d in self.detectors)).pack(anchor="w")

        ctrl = ttk.Frame(body)
        ctrl.pack(fill="x", pady=(6, 0))
        self.start_btn = tk.Button(ctrl, text="Start", bg="#1f8f2f", fg="white", width=8, command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = tk.Button(ctrl, text="Stop", bg="#e33", fg="white", width=8, command=self.stop)
        self.stop_btn.pack(side="left", padx=6)
        self.state_var = tk.StringVar(value="Stopped")
        ttk.Label(ctrl, textvariable=self.state_var).pack(side="left", padx=6)
        self.msg_var = tk.StringVar(value="")
        wrap = int(330 * max(1.0, body.winfo_fpixels("1i") / 96.0))
        ttk.Label(body, textvariable=self.msg_var, foreground=GRAY, wraplength=wrap,
                  justify="left").pack(anchor="w", pady=(4, 0))

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
        for det, (mw_lbl, v_lbl), v in zip(self.detectors, self.rows, result):
            p = volts_to_mw(v, det.get("v_to_mw", [1 / 7.0, 0.0]))
            powers.append(p)
            mw_lbl.configure(text=f"{p:.4f}")
            v_lbl.configure(text=f"{v:.4f} V")
        summary = " · ".join(f"{p:.4f}" for p in powers) + " mW"
        self.section.set_summary(summary)
        self.set_status(summary, GREEN)

    def close(self):
        self._closing = True
        self.running = False
