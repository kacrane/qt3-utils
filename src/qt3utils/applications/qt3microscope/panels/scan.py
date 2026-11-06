"""
Scan tabs: confocal image scans and line scans (position optimization).

Formerly qdlscan: this is qt3scan (the renamed qdlscan) in the middle tabs of
QT3 Microscope. It uses qt3scan's ScanController for the moves and counting,
the same settings (range, pixels, time per row; line range XY/Z, pixels,
time), the same snake scan, Gaussian fit for Optimize X/Y/Z, right-click
"Go to position", and the same HDF5 file layout when saving.

Differences from the standalone qdlscan windows:
- Scans show in the tab instead of a new window each time. Every scan of
  the session stays in the "Scan" list above the plot, so earlier scans
  can be looked at and saved again.
- The piezos are the same ones the Stage drawer moves, and nothing moves
  when the window opens (qdlscan set them to 0). An axis whose position is
  not known yet is treated as 0 when a scan is centred on it.
- While a scan runs, the scope pauses (they share the DAQ counter) and
  restarts afterwards; the Stage drawer won't move the piezos.
- Each image row is its own DAQ job, so Pause takes effect after the
  current row and the power panel keeps updating between rows.
"""

import datetime
import logging
import os
import time
import tkinter as tk
from tkinter import filedialog, ttk

import numpy as np

from qt3utils.applications.qt3microscope.widgets import GRAY, GREEN, ORANGE, RED

logger = logging.getLogger(__name__)

AXIS_INDEX = {"x": 0, "y": 1, "z": 2}
DEFAULT_COLOR_MAP = "gray"  # as qdlscan


def _qt3utils_version():
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


def centred_range(centre, span, lo, hi):
    """(min, max, centre) of a scan of width span around centre, shifted to stay in [lo, hi] (as qdlscan)."""
    mn, mx = centre - span / 2, centre + span / 2
    if mn < lo:
        shift = lo - mn
        mn, mx, centre = mn + shift, mx + shift, centre + shift
    if mx > hi:
        shift = hi - mx
        mn, mx, centre = mn + shift, mx + shift, centre + shift
    return mn, mx, centre


def gaussian_peak(xs, ys, start, lo, hi):
    """qdlscan's Gaussian optimization: fitted centre, or start if the fit fails."""
    from scipy.optimize import curve_fit

    def f(x, a, x0, sigma, c):
        return a * np.exp(-(x - x0) ** 2 / (2 * sigma ** 2)) + c

    i = int(np.nanargmax(ys))
    p0 = [ys[i] - np.nanmin(ys), xs[i], 0.300, np.nanmin(ys)]
    try:
        p, _ = curve_fit(f, xs, ys, p0=p0, bounds=[[100, lo, 0.250, 0], [np.inf, hi, np.inf, np.inf]])
        return float(p[1]), True
    except Exception as e:
        logger.info("Failed to find maximum: %s", e)
        return start, False


class ScanEngine:
    """
    Shared by the scan tabs: owns the ScanController (built on first use, on the
    DAQ worker), hands the counter over from the scope, and blocks the stage
    while a scan runs.
    """

    def __init__(self, hub, stage, scope, scan_cfg, counter_cfg):
        self.worker = hub.worker("daq")
        self.stage = stage
        self.scope = scope
        self.cfg = scan_cfg or {}
        self.counter_cfg = counter_cfg or {}
        self.controller = None
        self.busy = ""
        self._scope_was_running = False
        self.n_scans = 0
        self.last_save_directory = None
        self.closing = False
        hub.on_close(self.close)
        if stage is not None:
            stage.blocked = lambda: self.busy
        if scope is not None:
            scope.blocked = lambda: self.busy

    def next_id(self):
        self.n_scans += 1
        return str(self.n_scans).zfill(3)

    # -- positions (from the Stage drawer's shared positioners) ----------------
    def limits(self, axis):
        if self.stage is None:
            return -40.0, 40.0
        return self.stage.axis_limits(AXIS_INDEX[axis])

    def position(self, axis):
        """Last commanded position of an axis, or None if it hasn't been set yet."""
        if self.stage is None:
            return None
        return self.stage.positions[AXIS_INDEX[axis]]

    # -- DAQ ownership ---------------------------------------------------------
    def acquire(self, reason, then):
        """Take the counter and piezos for a scan, then call then(). False if already busy."""
        if self.busy:
            return False
        self.busy = reason
        if self.scope is not None:
            self.scope.release_counter(lambda was: self._acquired(was, then))
        else:
            self._acquired(False, then)
        return True

    def _acquired(self, scope_was_running, then):
        self._scope_was_running = scope_was_running
        then()

    def release(self):
        self.busy = ""
        if self.stage is not None:
            self.stage.refresh_positions()
        if self.scope is not None and self._scope_was_running:
            self._scope_was_running = False
            self.scope.start()

    def ctrl(self):
        """The ScanController (call on the DAQ worker)."""
        if self.controller is None:
            from qt3utils.applications.qt3scan.application_controller import ScanController
            from qt3utils.hardware.nidaq.counters.nidaqtimedratecounter import NidaqTimedRateCounter
            c = self.counter_cfg
            counter = NidaqTimedRateCounter(
                daq_name=c.get("daq_name", "Dev1"), signal_terminal=c.get("signal_terminal", "PFI0"),
                clock_rate=int(c.get("clock_rate", 100000)), sample_time_in_seconds=1,
                clock_terminal=(c.get("clock_terminal") or None),
                read_write_timeout=float(c.get("read_write_timeout", 10)),
                signal_counter=c.get("signal_counter", "ctr2"))
            self.controller = ScanController(
                x_axis_controller=self.stage.positioner(0), y_axis_controller=self.stage.positioner(1),
                z_axis_controller=self.stage.positioner(2), counter_controller=counter,
                inter_scan_settle_time=float(self.cfg.get("inter_scan_settle_time", 0.01)),
                zero_on_start=False)
        return self.controller

    def submit(self, name, fn, on_done):
        self.worker.submit(name, fn, on_done)

    def close(self):
        """Window closing: tabs stop queueing rows; free the counter after the current one."""
        self.closing = True
        if self.controller is not None:
            c = self.controller
            c.stop_scan = True
            self.worker.submit("scan close", c.stop, quiet=True)

    def move(self, moves, on_done):
        """Move axes [(axis, position), ...] on the DAQ worker, then on_done(ok, result)."""
        def job():
            c = self.ctrl()
            for axis, pos in moves:
                lo, hi = self.limits(axis)
                if not lo <= pos <= hi:
                    raise ValueError(f"{axis} {pos:.3f} µm is outside {lo:g} to {hi:g} µm")
                c._set_axis(axis_controller=getattr(c, f"{axis}_axis_controller"), position=pos)
            return moves

        def done(ok, result):
            if self.stage is not None:
                self.stage.refresh_positions()
            on_done(ok, result)

        self.worker.submit("scan move", job, done)


class _PlotTab:
    """Shared bits of the two tabs: layout, figure, scan list, right-click menu."""

    def __init__(self, parent, engine, scale):
        self.engine = engine
        self.scale = scale
        self.scans = []
        self.shown = None
        self.frame = ttk.Frame(parent)
        self.frame.pack(fill="both", expand=True)
        self.left = ttk.Frame(self.frame, padding=(8, 8))
        self.left.pack(side="left", fill="y")
        right = ttk.Frame(self.frame)
        right.pack(side="left", fill="both", expand=True)
        top = ttk.Frame(right, padding=(4, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Scan").pack(side="left")
        self.pick = ttk.Combobox(top, state="readonly", width=48)
        self.pick.pack(side="left", padx=6)
        self.pick.bind("<<ComboboxSelected>>", lambda e: self._show(self.scans[self.pick.current()]))
        self.save_btn = ttk.Button(top, text="Save scan", command=self.save)
        self.save_btn.pack(side="left", padx=4)
        self.msg_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.msg_var, foreground=GRAY).pack(side="bottom", anchor="w", padx=6)
        self.plot = ttk.Frame(right)
        self.plot.pack(fill="both", expand=True)
        self.fig = None
        try:
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
            self.fig = Figure(figsize=(6, 5), dpi=96 * scale)
            self.canvas = FigureCanvasTkAgg(self.fig, master=self.plot)
            tb = NavigationToolbar2Tk(self.canvas, self.plot, pack_toolbar=False)
            tb.update()
            tb.pack(side="bottom", fill="x")
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
            self.canvas.mpl_connect("button_press_event", self._on_click)
        except Exception as e:
            ttk.Label(self.plot, text=f"No plot: {e}", foreground=RED).pack(anchor="w")
        self.rclick = tk.Menu(self.frame, tearoff=0)
        self.rclick.add_command(label="Go to position", command=self._go_to)
        self._rclick_xy = (None, None)
        self.save_btn.configure(state="disabled")

    def _entry(self, row, label, value, width=8):
        ttk.Label(self.left, text=label).grid(row=row, column=0, sticky="w", pady=2)
        e = ttk.Entry(self.left, width=width)
        e.insert(0, str(value))
        e.grid(row=row, column=1, sticky="w", padx=(6, 0), pady=2)
        return e

    def _heading(self, row, text):
        ttk.Label(self.left, text=text, font=("TkDefaultFont", 11, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(10, 2))

    def _add_scan(self, scan):
        self.scans.append(scan)
        self.pick.configure(values=[s["title"] for s in self.scans])
        self.pick.current(len(self.scans) - 1)
        self._show(scan)

    def _show(self, scan):
        self.shown = scan
        self.save_btn.configure(state="normal")
        self.redraw()

    def _on_click(self, event):
        if event.button != 3 or event.xdata is None:
            return
        self._rclick_xy = (event.xdata, event.ydata)
        x, y = self.frame.winfo_pointerxy()
        try:
            self.rclick.tk_popup(x, y)
        finally:
            self.rclick.grab_release()

    def _ask_save_path(self, prefix):
        scan = self.shown
        default = f'{prefix}{scan["id"]}_{scan["timestamp"].strftime("%Y%m%d_%H%M%S")}'
        afile = filedialog.asksaveasfilename(
            filetypes=[("Image with dataset", "*.png"), ("Dataset", "*.hdf5")],
            initialfile=default + ".png", initialdir=self.engine.last_save_directory)
        if not afile:
            return None
        folder, name = os.path.split(afile)
        self.engine.last_save_directory = folder
        stem, ext = os.path.splitext(name)
        return folder, stem, ext.lower()


class ImageScanTab(_PlotTab):
    def __init__(self, parent, engine, cfg, scale=1.0):
        super().__init__(parent, engine, scale)
        self.cfg = cfg or {}
        self.running = None  # the scan being taken, while rows are being read
        self._pause = False
        self.norm = (None, None)
        r = 0
        self._heading(r, "Confocal image")
        self.range_e = self._entry(r + 1, "Range (µm)", self.cfg.get("range", 80))
        self.pixels_e = self._entry(r + 2, "Number of pixels", self.cfg.get("pixels", 80))
        self.time_e = self._entry(r + 3, "Time per row (s)", self.cfg.get("time", 1))
        btns = ttk.Frame(self.left)
        btns.grid(row=r + 4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.start_btn = tk.Button(btns, text="Start scan", width=10, bg="#1f8f2f", fg="white", command=self.start)
        self.pause_btn = tk.Button(btns, text="Pause", width=8, command=self.pause)
        self.cont_btn = tk.Button(btns, text="Continue", width=8, command=self.continue_scan)
        self.start_btn.grid(row=0, column=0, padx=1)
        self.pause_btn.grid(row=0, column=1, padx=1)
        self.cont_btn.grid(row=0, column=2, padx=1)
        self._heading(r + 5, "Image settings")
        self.min_e = self._entry(r + 6, "Minimum (cts/s)", 0)
        self.max_e = self._entry(r + 7, "Maximum (cts/s)", 5000)
        nb = ttk.Frame(self.left)
        nb.grid(row=r + 8, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(nb, text="Normalize", command=self.set_norm).pack(side="left")
        ttk.Button(nb, text="Auto", command=self.auto_norm).pack(side="left", padx=4)
        self.status = tk.StringVar(value="Scans are centred on the current X/Y position.")
        ttk.Label(self.left, textvariable=self.status, foreground=GRAY, justify="left",
                  wraplength=int(230 * scale)).grid(row=r + 9, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._buttons()
        self.redraw()

    def _buttons(self):
        scan = self.shown
        running = self.running is not None
        unfinished = scan is not None and scan["index"] < scan["n_pixels"]
        self.start_btn.configure(state="disabled" if running else "normal")
        self.pause_btn.configure(state="normal" if running else "disabled")
        self.cont_btn.configure(state="normal" if (not running and unfinished) else "disabled")
        self.save_btn.configure(state="normal" if (scan is not None and not running) else "disabled")

    def _show(self, scan):
        super()._show(scan)
        self._buttons()

    # -- settings ----------------------------------------------------------
    def _read_settings(self):
        rng = float(self.range_e.get())
        n = int(self.pixels_e.get())
        t = float(self.time_e.get())
        lo_x, hi_x = self.engine.limits("x")
        lo_y, hi_y = self.engine.limits("y")
        if rng < 0.1:
            raise ValueError(f"Range {rng} < 100 nm is too small.")
        if rng > min(hi_x - lo_x, hi_y - lo_y):
            raise ValueError(f"Range {rng} µm is larger than the piezo travel.")
        if n < 1:
            raise ValueError("Number of pixels must be at least 1.")
        if t < 0.001:
            raise ValueError("Time per row must be at least 1 ms.")
        return rng, n, t

    # -- scanning ------------------------------------------------------------
    def start(self):
        try:
            rng, n, t = self._read_settings()
        except ValueError as e:
            self.status.set(str(e))
            return
        if not self.engine.acquire("An image scan is running.", lambda: self._begin(rng, n, t)):
            self.status.set(f"Busy: {self.engine.busy}")

    def _begin(self, rng, n, t):
        unknown = [a for a in ("x", "y", "z") if self.engine.position(a) is None]
        pos = [self.engine.position(a) or 0.0 for a in ("x", "y", "z")]
        mn1, mx1, c1 = centred_range(pos[0], rng, *self.engine.limits("x"))
        mn2, mx2, c2 = centred_range(pos[1], rng, *self.engine.limits("y"))
        sid = self.engine.next_id()
        ts = datetime.datetime.now()
        scan = {"id": sid, "timestamp": ts, "axis_1": "x", "axis_2": "y", "range": rng, "n_pixels": n,
                "time": t, "time_per_pixel": t / n, "start_position_vector": tuple(pos),
                "start_position_axis_1": c1, "start_position_axis_2": c2,
                "min_position_1": mn1, "max_position_1": mx1, "min_position_2": mn2, "max_position_2": mx2,
                "data_x": np.linspace(mn1, mx1, n), "data_y": np.linspace(mn2, mx2, n),
                "data_z": np.full((n, n), np.nan), "index": 0,
                "title": f"Image {sid}  {ts.strftime('%H:%M:%S')}  {rng:g} µm, {n} px, {t:g} s/row"}
        self._add_scan(scan)
        note = f" ({', '.join(unknown).upper()} not set yet, using 0)" if unknown else ""
        self._run(scan, f"Scanning image {sid}{note}...")

    def continue_scan(self):
        scan = self.shown
        if scan is None or scan["index"] >= scan["n_pixels"]:
            return
        if not self.engine.acquire("An image scan is running.", lambda: self._run(scan, f"Continuing image {scan['id']}...")):
            self.status.set(f"Busy: {self.engine.busy}")

    def _run(self, scan, text):
        self.running = scan
        self._pause = False
        self.status.set(text)
        self._buttons()
        self.engine.submit("scan start", lambda: self.engine.ctrl().counter_controller.start(), self._row_or_fail)

    def pause(self):
        if self.running is not None:
            self._pause = True
            self.status.set("Pausing after this row...")

    def _row_job(self, scan, index):
        c = self.engine.ctrl()
        ax1 = getattr(c, f"{scan['axis_1']}_axis_controller")
        ax2 = getattr(c, f"{scan['axis_2']}_axis_controller")
        c._set_axis(axis_controller=ax2, position=float(scan["data_y"][index]))
        time.sleep(c.inter_scan_settle_time)
        n = scan["n_pixels"]
        if index % 2 == 0:  # snake scan, as qdlscan
            row = c._scan_axis(axis_controller=ax1, start=scan["min_position_1"], stop=scan["max_position_1"],
                               n_pixels=n, scan_time=scan["time"])
        else:
            row = c._scan_axis(axis_controller=ax1, start=scan["max_position_1"], stop=scan["min_position_1"],
                               n_pixels=n, scan_time=scan["time"])[::-1]
        return row / scan["time_per_pixel"]

    def _row_or_fail(self, ok, result):
        scan = self.running
        if self.engine.closing:
            return
        if not ok:
            return self._finish(scan, f"DAQ error: {result}", home=False)
        if scan is None:
            return
        if scan["index"] >= scan["n_pixels"]:
            return self._finish(scan, f"Image {scan['id']} complete.", home=True)
        if self._pause:
            return self._finish(scan, f"Paused at row {scan['index']} of {scan['n_pixels']}. Continue resumes it.",
                                home=False)
        i = scan["index"]
        self.engine.submit("scan row", lambda: self._row_job(scan, i), lambda ok, res: self._row_done(scan, i, ok, res))

    def _row_done(self, scan, i, ok, result):
        if ok:
            scan["data_z"][i] = result
            scan["index"] = i + 1
            if self.shown is scan:
                self.redraw()
            self.status.set(f"Image {scan['id']}: row {i + 1} of {scan['n_pixels']}")
        self._row_or_fail(ok, result)

    def _finish(self, scan, text, home):
        def stop_job():
            c = self.engine.ctrl()
            c.counter_controller.stop()
            if home:  # back to the centre of the scan, as qdlscan
                c._set_axis(axis_controller=getattr(c, f"{scan['axis_1']}_axis_controller"),
                            position=scan["start_position_axis_1"])
                c._set_axis(axis_controller=getattr(c, f"{scan['axis_2']}_axis_controller"),
                            position=scan["start_position_axis_2"])

        def done(ok, result):
            self.running = None
            self.engine.release()
            self.status.set(text if ok else f"{text} Stopping failed: {result}")
            self._buttons()
            self.redraw()

        self.engine.submit("scan stop", stop_job, done)

    # -- display -------------------------------------------------------------
    def set_norm(self):
        try:
            lo, hi = float(self.min_e.get()), float(self.max_e.get())
            if lo > hi or lo < 0:
                raise ValueError
        except ValueError:
            self.status.set("Minimum must be ≥ 0 and below the maximum.")
            return
        self.norm = (lo, hi)
        self.redraw()

    def auto_norm(self):
        self.norm = (None, None)
        self.redraw()

    def redraw(self):
        if self.fig is None:
            return
        self.fig.clear()
        ax = self.fig.add_subplot(111)
        scan = self.shown
        if scan is None:
            ax.set_xlabel("x position (µm)")
            ax.set_ylabel("y position (µm)")
            ax.text(0.5, 0.5, "Start a scan", ha="center", va="center", transform=ax.transAxes, color="#888888")
            self.canvas.draw_idle()
            return
        pw = scan["range"] / scan["n_pixels"]
        extent = [scan["min_position_1"] - pw / 2, scan["max_position_1"] + pw / 2,
                  scan["min_position_2"] - pw / 2, scan["max_position_2"] + pw / 2]
        img = ax.imshow(scan["data_z"], extent=extent, cmap=DEFAULT_COLOR_MAP, origin="lower",
                        aspect="equal", interpolation="none")
        if self.norm[0] is not None:
            from matplotlib.colors import Normalize
            img.set_norm(Normalize(vmin=self.norm[0], vmax=self.norm[1]))
        cbar = self.fig.colorbar(img, ax=ax)
        cbar.ax.set_ylabel("Intensity (cts/s)", rotation=270, labelpad=15)
        ax.set_xlabel(f"{scan['axis_1']} position (µm)")
        ax.set_ylabel(f"{scan['axis_2']} position (µm)")
        ax.grid(alpha=0.3)
        x, y = self.engine.position("x"), self.engine.position("y")
        if x is not None and y is not None:
            ax.plot(x, y, "o", fillstyle="none", markeredgecolor="#1864ab", markeredgewidth=2)
        self.canvas.draw_idle()

    def _go_to(self):
        x, y = self._rclick_xy
        if x is None or self.engine.busy:
            self.status.set(self.engine.busy or "Right-click inside the image.")
            return
        self.engine.move([("x", float(x)), ("y", float(y))],
                         lambda ok, res: (self.status.set(f"Moved to x {x:.2f}, y {y:.2f} µm" if ok else f"Move failed: {res}"),
                                          self.redraw()))

    def save(self):
        if self.shown is None or self.running is not None:
            return
        path = self._ask_save_path("scan")
        if path is None:
            return
        folder, stem, ext = path
        try:
            if ext == ".png" and self.fig is not None:
                self.fig.savefig(os.path.join(folder, stem + ".png"), dpi=300)
            self._save_hdf5(os.path.join(folder, stem + ".hdf5"), stem, self.shown)
            self.msg_var.set(f"Saved {stem} in {folder}")
        except Exception as e:
            logger.exception("save failed")
            self.msg_var.set(f"Save failed: {e}")

    def _save_hdf5(self, path, stem, s):
        import h5py
        with h5py.File(path, "w") as df:
            ds = df.create_dataset("file_metadata", data=np.array(
                ["application", "qt3utils_version", "scan_id", "timestamp", "original_name"], dtype="S"))
            ds.attrs["application"] = "qt3utils.qt3scan.ImageScanApplication"
            ds.attrs["qt3utils_version"] = _qt3utils_version()
            ds.attrs["scan_id"] = s["id"]
            ds.attrs["timestamp"] = s["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
            ds.attrs["original_name"] = stem
            um = "Micrometers"
            for key, units in (("axis_1", "None"), ("axis_2", "None"), ("range", um), ("n_pixels", "None"),
                               ("time", "Seconds"), ("time_per_pixel", "Seconds"), ("start_position_vector", um),
                               ("start_position_axis_1", um), ("start_position_axis_2", um),
                               ("min_position_1", um), ("min_position_2", um),
                               ("max_position_1", um), ("max_position_2", um)):
                ds = df.create_dataset(f"scan_settings/{key}", data=s[key])
                ds.attrs["units"] = units
            for key, data, units in (("positions_axis_1", s["data_x"], um), ("positions_axis_2", s["data_y"], um),
                                     ("count_rates", s["data_z"], "Counts per second"),
                                     ("counts", s["data_z"] * s["time_per_pixel"], "Counts")):
                ds = df.create_dataset(f"data/{key}", data=data)
                ds.attrs["units"] = units


class LineScanTab(_PlotTab):
    def __init__(self, parent, engine, cfg, scale=1.0):
        super().__init__(parent, engine, scale)
        self.cfg = cfg or {}
        self._heading(0, "Position optimization")
        self.range_xy_e = self._entry(1, "Range XY (µm)", self.cfg.get("range_xy", 3))
        self.range_z_e = self._entry(2, "Range Z (µm)", self.cfg.get("range_z", 20))
        self.pixels_e = self._entry(3, "Number of pixels", self.cfg.get("pixels", 50))
        self.time_e = self._entry(4, "Time (s)", self.cfg.get("time", 1))
        btns = ttk.Frame(self.left)
        btns.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.opt_btns = []
        for axis in ("x", "y", "z"):
            b = tk.Button(btns, text=f"Optimize {axis.upper()}", width=10, command=lambda a=axis: self.optimize(a))
            b.pack(side="left", padx=1)
            self.opt_btns.append(b)
        self.status = tk.StringVar(value="Scans around the current position, then moves to the fitted peak.")
        ttk.Label(self.left, textvariable=self.status, foreground=GRAY, justify="left",
                  wraplength=int(230 * scale)).grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.redraw()

    def _set_running(self, running):
        for b in self.opt_btns:
            b.configure(state="disabled" if running else "normal")
        self.save_btn.configure(state="disabled" if (running or self.shown is None) else "normal")

    def optimize(self, axis):
        try:
            rng = float((self.range_z_e if axis == "z" else self.range_xy_e).get())
            n = int(self.pixels_e.get())
            t = float(self.time_e.get())
            lo, hi = self.engine.limits(axis)
            if rng < 0.1 or rng > hi - lo:
                raise ValueError(f"Range {rng} µm must be between 0.1 µm and the piezo travel.")
            if n < 1 or t < 0.001 or t > 300:
                raise ValueError("Pixels must be ≥ 1 and time between 1 ms and 300 s.")
        except ValueError as e:
            self.status.set(str(e))
            return
        if not self.engine.acquire(f"Optimizing {axis.upper()}.", lambda: self._begin(axis, rng, n, t)):
            self.status.set(f"Busy: {self.engine.busy}")

    def _begin(self, axis, rng, n, t):
        start = self.engine.position(axis)
        note = "" if start is not None else f" ({axis.upper()} not set yet, using 0)"
        start = start or 0.0
        lo, hi = self.engine.limits(axis)
        mn, mx, _ = centred_range(start, rng, lo, hi)
        sid = self.engine.next_id()
        ts = datetime.datetime.now()
        pos = tuple(self.engine.position(a) or 0.0 for a in ("x", "y", "z"))
        scan = {"id": sid, "timestamp": ts, "axis": axis, "range": rng, "n_pixels": n, "time": t,
                "time_per_pixel": t / n, "start_position_vector": pos, "start_position_axis": start,
                "final_position_axis": None, "min_position": mn, "max_position": mx,
                "data_x": np.linspace(mn, mx, n), "data_y": np.full(n, np.nan),
                "title": f"Line {sid}  {ts.strftime('%H:%M:%S')}  {axis.upper()} {rng:g} µm, {n} px, {t:g} s"}
        self._add_scan(scan)
        self._set_running(True)
        self.status.set(f"Scanning {axis.upper()}{note}...")

        def job():
            c = self.engine.ctrl()
            return c.scan_axis(axis=axis, start=mn, stop=mx, n_pixels=n, scan_time=t)

        self.engine.submit("line scan", job, lambda ok, res: self._scanned(scan, ok, res))

    def _scanned(self, scan, ok, result):
        if self.engine.closing:
            return
        if not ok:
            self._end(f"DAQ error: {result}")
            try:  # make sure the counter is released
                self.engine.submit("scan stop", lambda: self.engine.ctrl().stop(), lambda ok, res: None)
            except Exception:
                pass
            return
        scan["data_y"] = np.asarray(result, dtype=float) / scan["time_per_pixel"]
        final, fitted = gaussian_peak(scan["data_x"], scan["data_y"], scan["start_position_axis"],
                                      scan["min_position"], scan["max_position"])
        scan["final_position_axis"] = final
        self.redraw()
        how = "fitted peak" if fitted else "start (fit failed)"
        self.engine.move([(scan["axis"], final)], lambda ok, res: self._end(
            f"{scan['axis'].upper()} moved to {final:.3f} µm ({how})." if ok else f"Move failed: {res}"))

    def _end(self, text):
        self.engine.release()
        self.status.set(text)
        self._set_running(False)
        self.redraw()

    def redraw(self):
        if self.fig is None:
            return
        self.fig.clear()
        ax = self.fig.add_subplot(111)
        s = self.shown
        ax.set_ylabel("Intensity (cts/s)")
        ax.grid(alpha=0.3)
        if s is None:
            ax.set_xlabel("position (µm)")
            ax.text(0.5, 0.5, "Optimize X, Y or Z", ha="center", va="center", transform=ax.transAxes, color="#888888")
            self.canvas.draw_idle()
            return
        if not np.all(np.isnan(s["data_y"])):
            ax.plot(s["data_x"], s["data_y"], color="k", linewidth=1.5)
        ax.axvline(s["start_position_axis"], color="#bac8ff", linewidth=1.5)
        if s["final_position_axis"] is not None:
            ax.axvline(s["final_position_axis"], color="#1864ab", linewidth=1.5)
        ax.set_xlim(s["min_position"], s["max_position"])
        ax.set_xlabel(f"{s['axis']} position (µm)")
        self.canvas.draw_idle()

    def _go_to(self):
        x = self._rclick_xy[0]
        s = self.shown
        if x is None or s is None or self.engine.busy:
            self.status.set(self.engine.busy or "Right-click inside the plot.")
            return

        def done(ok, res):
            if ok:
                s["final_position_axis"] = float(x)
            self.status.set(f"Moved {s['axis'].upper()} to {x:.3f} µm" if ok else f"Move failed: {res}")
            self.redraw()

        self.engine.move([(s["axis"], float(x))], done)

    def save(self):
        if self.shown is None:
            return
        path = self._ask_save_path("scan")
        if path is None:
            return
        folder, stem, ext = path
        s = self.shown
        try:
            if ext == ".png" and self.fig is not None:
                self.fig.savefig(os.path.join(folder, stem + ".png"), dpi=300)
            import h5py
            with h5py.File(os.path.join(folder, stem + ".hdf5"), "w") as df:
                ds = df.create_dataset("file_metadata", data=np.array(
                    ["application", "qt3utils_version", "scan_id", "timestamp", "original_name"], dtype="S"))
                ds.attrs["application"] = "qt3utils.qt3scan.LineScanApplication"
                ds.attrs["qt3utils_version"] = _qt3utils_version()
                ds.attrs["scan_id"] = s["id"]
                ds.attrs["timestamp"] = s["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
                ds.attrs["original_name"] = stem
                um = "Micrometers"
                final = s["final_position_axis"] if s["final_position_axis"] is not None else np.nan
                for key, data, units in (("axis", s["axis"], "None"), ("range", s["range"], um),
                                         ("n_pixels", s["n_pixels"], "None"), ("time", s["time"], "Seconds"),
                                         ("time_per_pixel", s["time_per_pixel"], "Seconds"),
                                         ("start_position_vector", s["start_position_vector"], um),
                                         ("start_position_axis", s["start_position_axis"], um),
                                         ("final_position_axis", final, um),
                                         ("min_position", s["min_position"], um),
                                         ("max_position", s["max_position"], um)):
                    ds = df.create_dataset(f"scan_settings/{key}", data=data)
                    ds.attrs["units"] = units
                for key, data, units in (("positions", s["data_x"], um),
                                         ("count_rates", s["data_y"], "Counts per second"),
                                         ("counts", s["data_y"] * s["time_per_pixel"], "Counts")):
                    ds = df.create_dataset(f"data/{key}", data=data)
                    ds.attrs["units"] = units
            self.msg_var.set(f"Saved {stem} in {folder}")
        except Exception as e:
            logger.exception("save failed")
            self.msg_var.set(f"Save failed: {e}")
