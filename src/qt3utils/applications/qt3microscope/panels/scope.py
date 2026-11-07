"""
Scope strip: live count rate from the photon counter: the SPCM on the NI
DAQ (VQLM) or the SNSPD over Ethernet (TQLM), as the profile's scope
section says (see counters.py).

Formerly qdlscope: this is the qdlscope app (renamed qt3scope) placed in the
bottom strip of QT3 Microscope. Same controls (time per sample, raw counts,
Start, Pause, Reset, Save) and the same HDF5 file layout as qt3scope.

Samples are read on the hub's DAQ worker in short chunks (about 0.1 s each),
so the power panel and later the scans can use the DAQ in between.
"""

import datetime
import logging
import os
import sys
import time
import tkinter as tk
from tkinter import filedialog, ttk

from qt3utils.applications.qt3microscope import counters
from qt3utils.applications.qt3microscope.widgets import GRAY, GREEN, RED

logger = logging.getLogger(__name__)

MAX_SAMPLES_TO_PLOT = 500
MAX_ALLOWED_SAMPLES = 1_000_000  # about 3 hours at 0.01 s per sample
CHUNK_S = 0.1  # how long one DAQ job samples before handing the DAQ back


def format_rate(value, is_rate):
    """1234567 -> '1.235 M', for the big readout."""
    unit = "cts/s" if is_rate else "cts"
    for scale, prefix in ((1e6, "M"), (1e3, "k")):
        if abs(value) >= scale:
            return f"{value / scale:.3f} {prefix}{unit}"
    return f"{value:.0f} {unit}"


def _qt3utils_version():
    """Version for the file metadata; a copied (not installed) qt3utils may not have one."""
    try:
        import qt3utils
        return str(getattr(qt3utils, "__version__"))
    except Exception:
        pass
    try:
        from importlib.metadata import version
        return version("qt3utils")
    except Exception:
        return "unknown"


class ScopePanel:
    def __init__(self, section, hub, cfg, set_status=None, scale=1.0):
        self.section = section
        # A DAQ counter shares the DAQ worker with the power panel and stage; the
        # SNSPD is its own device, so its slow reads get their own worker.
        self.worker = hub.worker("daq" if counters.is_daq_counter(cfg) else "counter")
        self.cfg = cfg
        self.scale = scale
        self.set_status = set_status or (lambda value, colour: None)
        self.counter = None
        self.running = False
        self._job_out = False
        self._closing = False
        self.data_x, self.data_y = [], []
        self.total_measurement_time = 0.0
        self.sample_time = float(cfg.get("sample_time", 0.01))
        self.get_rate = True
        self.timestamp = datetime.datetime.now()
        self.last_save_directory = None
        self._last_draw = 0.0
        self._after_stop = None
        # Set by the window: returns a reason (str) while a scan owns the counter.
        self.blocked = lambda: ""
        self._build(section.body)
        self._enable_buttons()
        hub.on_close(self.close)

    # -- GUI ---------------------------------------------------------------
    def _build(self, body):
        ctrl = ttk.Frame(body)
        ctrl.pack(side="left", fill="y", padx=(0, 10))
        self.rate_lbl = tk.Label(ctrl, text="--", font=("TkDefaultFont", 20, "bold"), fg="#1d4fb0",
                                 width=13, anchor="w")
        self.rate_lbl.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(ctrl, text="Time per sample (s)").grid(row=1, column=0, sticky="w", pady=2)
        self.sample_time_entry = ttk.Entry(ctrl, width=8)
        self.sample_time_entry.insert(0, f"{self.sample_time:g}")
        self.sample_time_entry.grid(row=1, column=1, sticky="w")
        self.raw_var = tk.IntVar(value=0)
        self.raw_check = ttk.Checkbutton(ctrl, text="Raw counts", variable=self.raw_var)
        self.raw_check.grid(row=2, column=0, columnspan=2, sticky="w")
        btns = ttk.Frame(ctrl)
        btns.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.start_btn = tk.Button(btns, text="Start", width=7, bg="#1f8f2f", fg="white", command=self.start)
        self.pause_btn = tk.Button(btns, text="Pause", width=7, command=self.pause)
        self.reset_btn = tk.Button(btns, text="Reset", width=7, command=self.reset)
        self.save_btn = tk.Button(btns, text="Save", width=7, command=self.save)
        for i, b in enumerate((self.start_btn, self.pause_btn, self.reset_btn, self.save_btn)):
            b.grid(row=i // 2, column=i % 2, padx=1, pady=1)
        self.msg_var = tk.StringVar(value=f"{counters.detector_name(self.cfg)} on {self._source_text()}")
        ttk.Label(ctrl, textvariable=self.msg_var, foreground=GRAY, wraplength=int(230 * self.scale),
                  justify="left").grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.plot_frame = ttk.Frame(body)
        self.plot_frame.pack(side="left", fill="both", expand=True)
        self._build_plot()

        # Header buttons: usable while the strip is folded.
        self.q_start = tk.Button(self.section.quick, text="Start", padx=6, pady=0, command=self.start)
        self.q_pause = tk.Button(self.section.quick, text="Pause", padx=6, pady=0, command=self.pause)
        self.q_start.pack(side="left", padx=1)
        self.q_pause.pack(side="left", padx=(1, 4))

    def _source_text(self):
        return counters.source_text(self.cfg)

    def _build_plot(self):
        try:
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        except ImportError as e:
            self.fig = None
            ttk.Label(self.plot_frame, text=f"No plot: {e}", foreground=RED).pack(anchor="w")
            return
        # Matplotlib reads IPython.version_info when it makes a canvas. The laser
        # panel loads qick on another thread, which imports IPython, so IPython
        # can be half-imported at this moment and lack version_info. Matplotlib
        # only needs it to skip an old-IPython fix, so give it a new version.
        ip = sys.modules.get("IPython")
        if ip is not None and not hasattr(ip, "version_info"):
            ip.version_info = (99, 0, 0)
        dpi = 96 * self.scale
        self.fig = Figure(figsize=(8, 1.9), dpi=dpi)
        self.ax = self.fig.add_subplot(111)
        self.fig.subplots_adjust(left=0.11, right=0.99, top=0.95, bottom=0.25)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.plot_frame)
        self.canvas.get_tk_widget().configure(height=int(190 * self.scale))
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self._init_axes()

    def _ylabel(self):
        return "Intensity (cts/s)" if self.get_rate else "Intensity (cts)"

    def _init_axes(self):
        if self.fig is None:
            return
        self.ax.clear()
        self.ax.set_xlim(0, 5)
        self.ax.set_xlabel("Time (s)", fontsize=9)
        self.ax.set_ylabel(self._ylabel(), fontsize=9)
        self.ax.tick_params(labelsize=8)
        self.ax.grid(alpha=0.3)
        self.canvas.draw_idle()

    def _update_plot(self, force=False):
        if self.fig is None:
            return
        now = time.time()
        if not force and now - self._last_draw < 0.1:  # redraw at most 10 times a second
            return
        self._last_draw = now
        x = self.data_x[-MAX_SAMPLES_TO_PLOT:]
        y = self.data_y[-MAX_SAMPLES_TO_PLOT:]
        self.ax.clear()
        self.ax.plot(x, y, color="k", linewidth=1.2)
        if len(x) > 1:
            self.ax.set_xlim(x[0], x[-1])
        self.ax.set_xlabel("Time (s)", fontsize=9)
        self.ax.set_ylabel(self._ylabel(), fontsize=9)
        self.ax.tick_params(labelsize=8)
        self.ax.grid(alpha=0.3)
        self.canvas.draw_idle()

    def _set_buttons(self, start, pause, reset, save, settings):
        for b, on in ((self.start_btn, start), (self.q_start, start), (self.pause_btn, pause),
                      (self.q_pause, pause), (self.reset_btn, reset), (self.save_btn, save)):
            b.configure(state="normal" if on else "disabled")
        state = "normal" if settings else "disabled"
        self.sample_time_entry.configure(state=state)
        self.raw_check.configure(state=state)

    def _enable_buttons(self):
        """Fresh state: no data yet."""
        self._set_buttons(start=True, pause=False, reset=False, save=False, settings=True)

    # -- actions -----------------------------------------------------------
    def start(self):
        if self.running or self._job_out:
            return
        if self.blocked():
            self.msg_var.set(self.blocked())
            return
        if not self.data_x:  # sample time and raw counts only change after Reset
            try:
                st = float(self.sample_time_entry.get())
            except ValueError:
                self.msg_var.set("Time per sample must be a number.")
                return
            if not 0.001 <= st <= 60:
                self.msg_var.set("Time per sample must be between 0.001 and 60 s.")
                return
            self.sample_time = st
            self.get_rate = not bool(self.raw_var.get())
            self._init_axes()
        self.running = True
        self._set_buttons(start=False, pause=True, reset=False, save=False, settings=False)
        self.msg_var.set("Starting...")
        self._job_out = True
        self.worker.submit("scope start", self._daq_start, self._started)

    def release_counter(self, callback):
        """Stop sampling (if running) so another tool can use the counter, then call callback(was_running)."""
        if self.running or self._job_out:
            was = self.running
            self.running = False
            self.msg_var.set("Paused for a scan")
            self._after_stop = lambda: callback(was)
            if not self._job_out:  # between chunks: nothing will call _next_chunk
                self._next_chunk()
        else:
            callback(False)

    def pause(self):
        if not self.running:
            return
        self.running = False  # the sampling chain stops after the current chunk
        self.msg_var.set("Paused")

    def reset(self):
        if self.running:
            return
        self.data_x, self.data_y = [], []
        self.total_measurement_time = 0.0
        self.rate_lbl.configure(text="--")
        self.section.set_summary("—")
        self._init_axes()
        self._enable_buttons()
        self.msg_var.set(f"{counters.detector_name(self.cfg)} on {self._source_text()}")

    # -- DAQ jobs (run on the DAQ worker thread) -----------------------------
    def _daq_start(self):
        if self.counter is None:
            self.counter = counters.make_counter(self.cfg, self.sample_time)
        self.counter.start()
        self.counter.configure_sample_time(sample_time=self.sample_time)
        time.sleep(0.1)  # drain readings taken with the old sample time (as qt3scope does)
        for _ in range(5):
            try:
                self.counter.sample_batch_counts()
            except Exception:
                pass
        t0 = self.data_x[-1] if self.data_x else 0.0
        return time.time() - t0  # time origin so the trace continues after a pause

    def _daq_chunk(self, origin, scale):
        xs, ys = [], []
        n = max(1, int(round(CHUNK_S / self.sample_time)))
        for _ in range(n):
            counts = self.counter.sample_batch_counts()
            xs.append(time.time() - origin)
            ys.append(float(counts) * scale)
        return xs, ys

    def _daq_stop(self):
        if self.counter is not None:
            self.counter.stop()

    # -- results (on the Tk thread) ------------------------------------------
    def _started(self, ok, result):
        self._job_out = False
        if self._closing:
            return
        if not ok:
            self.running = False
            self.counter = None
            self._failed(result)
            return
        self._origin = result
        self._run_start = time.time()
        self.msg_var.set("Running")
        self.set_status("running", GREEN)
        self._next_chunk()

    def _next_chunk(self):
        if self._closing:
            return
        if not self.running:
            self._job_out = True
            self.worker.submit("scope stop", self._daq_stop, self._stopped)
            return
        scale = 1.0 / self.sample_time if self.get_rate else 1.0
        self._job_out = True
        origin = self._origin
        self.worker.submit("scope read", lambda: self._daq_chunk(origin, scale), self._chunk_done, quiet=True)

    def _chunk_done(self, ok, result):
        self._job_out = False
        if self._closing:
            return
        if not ok:
            self.running = False
            self._failed(result)
            self.worker.submit("scope stop", self._daq_stop, quiet=True)
            return
        xs, ys = result
        self.data_x.extend(xs)
        self.data_y.extend(ys)
        if ys:
            text = format_rate(ys[-1], self.get_rate)
            self.rate_lbl.configure(text=text)
            self.section.set_summary(text)
            self.set_status(text, GREEN)
        self._update_plot()
        if len(self.data_x) > MAX_ALLOWED_SAMPLES:
            self.running = False
            self.msg_var.set(f"Stopped at the {MAX_ALLOWED_SAMPLES} sample limit. Save, then Reset.")
        self._next_chunk()

    def _stopped(self, ok, result):
        self._job_out = False
        if self._after_stop is not None:
            cb, self._after_stop = self._after_stop, None
            self.section.after(0, cb)
        self.total_measurement_time += time.time() - getattr(self, "_run_start", time.time())
        if self._closing:
            return
        self._update_plot(force=True)
        self._set_buttons(start=True, pause=False, reset=True, save=bool(self.data_x), settings=False)
        self.set_status("paused", GRAY)
        if not ok:
            self.msg_var.set(f"{time.strftime('%H:%M:%S')}  Stopping the counter failed: {result}")

    def _failed(self, err):
        self.msg_var.set(f"{time.strftime('%H:%M:%S')}  DAQ error: {err}")
        self.section.set_summary("DAQ error")
        self.set_status("error", RED)
        has_data = bool(self.data_x)
        self._set_buttons(start=True, pause=False, reset=has_data, save=has_data, settings=not has_data)

    # -- saving (same layout as qt3scope) --------------------------------------
    def save(self):
        if self.running or not self.data_x:
            return
        default_name = f'scope_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}'
        afile = filedialog.asksaveasfilename(
            filetypes=[("Image with dataset", "*.png"), ("Dataset", "*.hdf5")],
            initialfile=default_name + ".png", initialdir=self.last_save_directory)
        if not afile:
            return
        folder, name = os.path.split(afile)
        stem, ext = os.path.splitext(name)
        self.last_save_directory = folder
        try:
            if ext.lower() == ".png" and self.fig is not None:
                self._update_plot(force=True)
                self.fig.savefig(os.path.join(folder, stem + ".png"), dpi=300)
            self._save_hdf5(os.path.join(folder, stem + ".hdf5"), stem)
            self.msg_var.set(f"Saved {stem} in {folder}")
        except Exception as e:
            logger.exception("save failed")
            self.msg_var.set(f"Save failed: {e}")

    def _save_hdf5(self, path, stem):
        import h5py
        import numpy as np
        with h5py.File(path, "w") as df:
            ds = df.create_dataset("file_metadata", data=np.array(
                ["application", "qt3utils_version", "timestamp", "original_name"], dtype="S"))
            ds.attrs["application"] = "qt3utils.qt3scope"
            ds.attrs["qt3utils_version"] = _qt3utils_version()
            ds.attrs["timestamp"] = self.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            ds.attrs["original_name"] = stem
            ds.attrs["signal_source"] = "counter" if counters.is_daq_counter(self.cfg) else "snspd"
            ds = df.create_dataset("scan_settings/sample_time", data=self.sample_time)
            ds.attrs["units"] = "seconds"
            ds.attrs["description"] = "Time that the DAQ integrates counts over."
            ds = df.create_dataset("scan_settings/is_rate", data=self.get_rate)
            ds.attrs["units"] = "None"
            ds.attrs["description"] = "Boolean; if the recorded data is the rate."
            ds = df.create_dataset("data/sample_timestamps", data=self.data_x)
            ds.attrs["units"] = "seconds"
            ds.attrs["description"] = "Timestamp of each sample relative to the start of the sampling."
            ds = df.create_dataset("data/intensity", data=self.data_y)
            ds.attrs["units"] = "counts per second" if self.get_rate else "counts"
            ds.attrs["description"] = "Intensity of samples."
            ds = df.create_dataset("data/total_measurement_time", data=self.total_measurement_time)
            ds.attrs["units"] = "seconds"
            ds.attrs["description"] = "Total time in seconds that samples were taken (not including pauses)."

    def close(self):
        """Stop the counter so the DAQ task is released when the window closes."""
        self._closing = True
        self.running = False
        if self.counter is not None:
            self.worker.submit("scope stop", self._daq_stop, quiet=True)
