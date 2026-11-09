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
DEFAULT_COLOR_MAP = "gray"  # black and white, as qdlscan
FONT = 9  # plot text size, same as the scope


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
        self.n_scans = {}  # numbered separately per kind: image scans, line scans
        self.last_save_directory = None
        self.closing = False
        hub.on_close(self.close)
        if stage is not None:
            stage.blocked = lambda: self.busy
        if scope is not None:
            scope.blocked = lambda: self.busy

    def next_id(self, kind):
        self.n_scans[kind] = self.n_scans.get(kind, 0) + 1
        return str(self.n_scans[kind]).zfill(3)

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
            from qt3utils.applications.qt3microscope.counters import make_counter
            # The same photon counter as the scope: SPCM on the DAQ or the SNSPD.
            counter = make_counter(self.counter_cfg, 1.0)
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
        self.top = top
        ttk.Label(top, text="Scan").pack(side="left")
        self.pick = ttk.Combobox(top, state="readonly", width=48)
        self.pick.pack(side="left", padx=6)
        self.pick.bind("<<ComboboxSelected>>", lambda e: self._show(self.scans[self.pick.current()]))
        self.save_btn = ttk.Button(top, text="Save scan", command=self.save)
        self.save_btn.pack(side="left", padx=4)
        self.msg_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.msg_var, foreground=GRAY).pack(side="bottom", anchor="w", padx=6)
        self.plot = ttk.Frame(right)
        self.plot.pack(side="left", fill="both", expand=True)
        self.plot_row = right
        self.fig = None
        try:
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
            # Same text size as the scope plot. The figure follows the size of
            # the tab, so ask Tk for a small canvas and let it grow, rather than
            # asking for more room than the tab has and getting clipped. The
            # axes are placed by hand with margins in points (_place_axes), the
            # same on every matplotlib version.
            self.fig = Figure(figsize=(4, 3), dpi=96 * scale)
            self.canvas = FigureCanvasTkAgg(self.fig, master=self.plot)
            self.canvas.get_tk_widget().configure(width=int(300 * scale), height=int(220 * scale))
            tb = NavigationToolbar2Tk(self.canvas, self.plot, pack_toolbar=False)
            tb.update()
            tb.pack(side="bottom", fill="x")
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
            # The Line scan tab is hidden at launch: when a tab is first shown,
            # size the figure to it in case Tk sent no resize while hidden.
            self.canvas.get_tk_widget().bind("<Map>", self._fit_to_widget, add="+")
            self.canvas.mpl_connect("button_press_event", self._on_click)
        except Exception as e:
            ttk.Label(self.plot, text=f"No plot: {e}", foreground=RED).pack(anchor="w")
        self.rclick = tk.Menu(self.frame, tearoff=0)
        self.rclick.add_command(label="Go to position", command=self._go_to)
        self._rclick_xy = (None, None)
        self.save_btn.configure(state="disabled")

    def _lock_left_width(self):
        """Fix the settings column's width so the plots never resize when a
        status or position message gets longer or shorter. The column is as
        wide as its fixed controls (at least 230 px at 96 dpi), and the
        messages wrap inside it."""
        wrapping = [w for w in self.left.winfo_children()
                    if isinstance(w, ttk.Label) and int(str(w.cget("wraplength") or 0)) > 0]
        saved = []
        for w in wrapping:  # measure the column without the messages
            saved.append(w.cget("textvariable") or None)
            w.configure(textvariable="", text="")
        self.left.update_idletasks()
        pad = int(16 * self.scale)
        width = max(self.left.winfo_reqwidth(), int(230 * self.scale) + pad)
        for w, var in zip(wrapping, saved):
            if var:
                w.configure(textvariable=var)
            w.configure(wraplength=width - pad)
        self.left.configure(width=width)
        self.left.grid_propagate(False)  # height still comes from pack(fill="y")

    def _fit_to_widget(self, _event=None):
        widget = self.canvas.get_tk_widget()
        w, h = widget.winfo_width(), widget.winfo_height()
        if w > 10 and h > 10:
            self.fig.set_size_inches(w / self.fig.dpi, h / self.fig.dpi, forward=False)
            self.redraw()

    def _place_axes(self, ax):
        """Fill the plot area, leaving room in points for the tick labels and titles."""
        pt = self.fig.dpi / 72.0
        w = self.fig.get_figwidth() * self.fig.dpi
        h = self.fig.get_figheight() * self.fig.dpi
        left, right, bottom, top = 56 * pt, 14 * pt, 36 * pt, 10 * pt
        ax.set_position([left / w, bottom / h, max(0.05, 1 - (left + right) / w), max(0.05, 1 - (bottom + top) / h)])

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
        self._rclick_ax = event.inaxes
        x, y = self.frame.winfo_pointerxy()
        try:
            self.rclick.tk_popup(x, y)
        finally:
            self.rclick.grab_release()

    def _ask_save_path(self, prefix, scan=None):
        scan = scan or self.shown
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
        self.pick.configure(width=40)
        # Right-hand image: a scan you pin stays there (e.g. the 80 µm overview
        # while you take zoom-ins) until you unpin it or pin another. Its
        # controls are on a second row under the first.
        self.pinned = None
        self.pin_btn = ttk.Button(self.top, text="Pin →", command=self.pin)
        self.pin_btn.pack(side="left", padx=(0, 4))
        row2 = ttk.Frame(self.top.master, padding=(4, 0, 4, 4))
        row2.pack(fill="x", after=self.top)
        ttk.Label(row2, text="Pinned").pack(side="left")
        self.pin_pick = ttk.Combobox(row2, state="readonly", width=40)
        self.pin_pick.pack(side="left", padx=6)
        self.pin_pick.bind("<<ComboboxSelected>>", lambda e: self.pin(self.scans[self.pin_pick.current()]))
        self.save_pin_btn = ttk.Button(row2, text="Save pinned", command=lambda: self.save(self.pinned))
        self.save_pin_btn.pack(side="left", padx=4)
        self.unpin_btn = ttk.Button(row2, text="Unpin", command=self.unpin)
        self.unpin_btn.pack(side="left")
        self._build_gallery()
        # Δz (µm) at which the outline of the left scan on the pinned scan is faintest.
        self.box_z_full = float(self.cfg.get("box_z_full", 5.0))
        self.running = None  # the scan being taken, while rows are being read
        self.paused = None   # a scan stopped by Pause, until Continue or Cancel
        self.pin_manual = False  # True once you pin a scan yourself
        self._pause = False
        self._cancel = False
        self.norm = (None, None)
        r = 0
        self._heading(r, "Confocal image")
        self.range_e = self._entry(r + 1, "Range (µm)", self.cfg.get("range", 80))
        self.pixels_e = self._entry(r + 2, "Number of pixels", self.cfg.get("pixels", 80))
        self.time_e = self._entry(r + 3, "Time per row (s)", self.cfg.get("time", 1))
        btns = ttk.Frame(self.left)
        btns.grid(row=r + 4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.start_btn = tk.Button(btns, text="Start scan", width=10, bg="#1f8f2f", fg="white", command=self.start)
        # Pause turns into Continue while a scan is paused; Cancel ends the scan
        # early (running or paused) and goes back to where it started.
        self.pause_btn = tk.Button(btns, text="Pause", width=8, command=self.pause_or_continue)
        self.cancel_btn = tk.Button(btns, text="Cancel", width=8, command=self.cancel)
        # While paused, a double-click on Start scan cancels the paused scan
        # (back to where it started) and starts a new one.
        self.start_btn.bind("<Double-Button-1>", lambda e: self.restart())
        self._restart = False
        self.start_btn.grid(row=0, column=0, padx=1)
        self.pause_btn.grid(row=0, column=1, padx=1)
        self.cancel_btn.grid(row=0, column=2, padx=1)
        self._heading(r + 5, "Image settings")
        self.min_e = self._entry(r + 6, "Minimum (cts/s)", 0)
        self.max_e = self._entry(r + 7, "Maximum (cts/s)", 5000)
        nb = ttk.Frame(self.left)
        nb.grid(row=r + 8, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(nb, text="Normalize", command=self.set_norm).pack(side="left")
        ttk.Button(nb, text="Auto", command=self.auto_norm).pack(side="left", padx=4)
        self.pos_var = tk.StringVar(value="")
        ttk.Label(self.left, textvariable=self.pos_var, justify="left",
                  wraplength=int(230 * scale)).grid(row=r + 9, column=0, columnspan=2, sticky="w", pady=(10, 0))
        # Set by the window: switches to the XYZ Optimization tab.
        self.on_optimize = None
        self.optimize_btn = ttk.Button(self.left, text="Optimize Position",
                                       command=lambda: self.on_optimize and self.on_optimize())
        self.optimize_btn.grid(row=r + 10, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.status = tk.StringVar(value="")
        ttk.Label(self.left, textvariable=self.status, foreground=GRAY, justify="left",
                  wraplength=int(230 * scale)).grid(row=r + 11, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._lock_left_width()
        self._show_position()
        self.range_e.bind("<KeyRelease>", lambda e: self.redraw() if self.shown is None else None)
        self._buttons()
        self.redraw()
        if self.fig is not None:  # margins are in pixels: recompute when the tab resizes
            self.canvas.mpl_connect("resize_event", lambda e: self.redraw())

    def _show_position(self):
        """Keep "centered on the current X/Y position" up to date (the stage, scans
        and XYZ Optimization all move the piezos)."""
        if self.engine.closing:
            return
        x, y = self.engine.position("x"), self.engine.position("y")
        fmt = lambda v: "—" if v is None else f"{v:.2f}"
        self.pos_var.set(f"Scans are centered on the current X/Y position: ({fmt(x)}, {fmt(y)}) µm")
        if (x, y) != getattr(self, "_last_xy", None):
            self._last_xy = (x, y)
            if self.running is None:  # move the empty frame / position marker
                self.redraw()
        self.frame.after(500, self._show_position)

    def _buttons(self):
        scan = self.shown
        running = self.running is not None
        busy = running or self.paused is not None
        paused_only = self.paused is not None and not running and not self._cancel
        self.start_btn.configure(state="normal" if (not busy or paused_only) else "disabled")
        if self.paused is not None and not running:
            self.pause_btn.configure(text="Continue", state="normal")
        else:
            self.pause_btn.configure(text="Pause", state="normal" if (running and not self._pause and not self._cancel)
                                     else "disabled")
        self.cancel_btn.configure(state="normal" if busy and not self._cancel else "disabled")
        self.save_btn.configure(state="normal" if (scan is not None and scan is not self.running) else "disabled")
        self.pin_btn.configure(state="normal" if scan is not None else "disabled")
        pinned_busy = self.pinned is None or self.pinned is self.running
        self.save_pin_btn.configure(state="disabled" if pinned_busy else "normal")
        self.unpin_btn.configure(state="disabled" if self.pinned is None else "normal")
        if hasattr(self, "thumbs"):
            self._gallery_marks()

    # -- gallery -----------------------------------------------------------
    def _build_gallery(self):
        """Scrollable column of thumbnails of every kept scan, newest at the top.
        Click a thumbnail to show it on the left; Pin sends it to the right;
        × discards it (it is gone from the lists too, so save it first)."""
        self.thumb_px = int(110 * self.scale)
        outer = ttk.Frame(self.plot_row)
        outer.pack(side="right", fill="y", before=self.plot)
        ttk.Label(outer, text="Scans", foreground=GRAY).pack(anchor="w", padx=4)
        self.gal_canvas = tk.Canvas(outer, width=self.thumb_px + int(16 * self.scale), highlightthickness=0)
        bar = ttk.Scrollbar(outer, orient="vertical", command=self.gal_canvas.yview)
        self.gal_canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.gal_canvas.pack(side="left", fill="y", expand=True)
        self.gal_inner = ttk.Frame(self.gal_canvas)
        self.gal_canvas.create_window((0, 0), window=self.gal_inner, anchor="nw")
        self.gal_inner.bind("<Configure>", lambda e: self.gal_canvas.configure(
            scrollregion=self.gal_canvas.bbox("all")))
        for w in (self.gal_canvas, self.gal_inner):
            w.bind("<Enter>", lambda e: self._wheel(True))
            w.bind("<Leave>", lambda e: self._wheel(False))
        self.thumbs = {}  # scan id -> {"frame", "img", "photo", "pin", "x"}

    def _wheel(self, on):
        top = self.gal_canvas.winfo_toplevel()
        if on:
            top.bind_all("<MouseWheel>", lambda e: self.gal_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
            top.bind_all("<Button-4>", lambda e: self.gal_canvas.yview_scroll(-1, "units"))
            top.bind_all("<Button-5>", lambda e: self.gal_canvas.yview_scroll(1, "units"))
        else:
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                top.unbind_all(seq)

    def _thumb_photo(self, scan):
        from PIL import Image, ImageTk
        z = np.array(scan["data_z"], dtype=float)
        lo, hi = self.norm if self.norm[0] is not None else (np.nanmin(z) if np.any(np.isfinite(z)) else 0.0,
                                                              np.nanmax(z) if np.any(np.isfinite(z)) else 1.0)
        g = np.clip((z - lo) / ((hi - lo) or 1.0), 0, 1) * 255
        g = np.where(np.isfinite(z), g, 230)  # rows not scanned yet: light grey
        im = Image.fromarray(np.flipud(g).astype(np.uint8), "L").resize((self.thumb_px, self.thumb_px), Image.NEAREST)
        return ImageTk.PhotoImage(im)

    def _gallery_add(self, scan):
        f = tk.Frame(self.gal_inner, bd=0, highlightthickness=3, highlightbackground="#d0d0d0")
        f.pack(side="top", before=self._first_thumb(), padx=2, pady=3)
        z = scan["start_position_vector"][2]
        ttk.Label(f, text=f"{scan['id']} · {scan['range']:g} µm · z {z:.2f}",
                  font=("TkDefaultFont", 8)).pack(anchor="w")
        img = tk.Label(f, cursor="hand2", bd=0)
        img.pack()
        row = ttk.Frame(f)
        row.pack(fill="x")
        pin = ttk.Button(row, text="Pin", width=4, command=lambda: self.pin(scan))
        pin.pack(side="left")
        x = ttk.Button(row, text="×", width=2, command=lambda: self.discard(scan))
        x.pack(side="right")
        img.bind("<Button-1>", lambda e: self._pick_scan(scan))
        self.thumbs[scan["id"]] = {"frame": f, "img": img, "pin": pin, "x": x}
        self._gallery_update(scan)
        self.gal_canvas.yview_moveto(0)

    def _first_thumb(self):
        kids = self.gal_inner.pack_slaves()
        return kids[0] if kids else None

    def _gallery_update(self, scan):
        t = self.thumbs.get(scan["id"])
        if t is None:
            return
        try:
            t["photo"] = self._thumb_photo(scan)
            t["img"].configure(image=t["photo"])
        except Exception as e:  # Pillow missing: keep the text label only
            logger.info("no thumbnail: %s", e)

    def _gallery_marks(self):
        """Blue border: shown on the left. Orange: pinned."""
        for sc in self.scans:
            t = self.thumbs.get(sc["id"])
            if t is None:
                continue
            colour = "#1864ab" if sc is self.shown else ("#ff9f1c" if sc is self.pinned else "#d0d0d0")
            t["frame"].configure(highlightbackground=colour, highlightcolor=colour)
            t["x"].configure(state="disabled" if sc is self.running or sc is self.paused else "normal")

    def _pick_scan(self, scan):
        self._show(scan)
        self.pick.current(self.scans.index(scan))

    def discard(self, scan):
        if scan is self.running or scan is self.paused:
            return
        t = self.thumbs.pop(scan["id"], None)
        if t is not None:
            t["frame"].destroy()
        self.scans.remove(scan)
        if self.pinned is scan:
            self.pinned = None
            self.pin_manual = False
        if self.shown is scan:
            self.shown = self.scans[-1] if self.scans else None
        self.pick.configure(values=[s["title"] for s in self.scans])
        if self.shown is None:
            self.pick.set("")
        else:
            self.pick.current(self.scans.index(self.shown))
        self._pin_list()
        self._buttons()
        self.redraw()
        self.status.set(f"Discarded image {scan['id']}.")

    # -- pinned scan --------------------------------------------------------
    def _add_scan(self, scan):
        super()._add_scan(scan)
        self._pin_list()
        self._gallery_add(scan)
        self._gallery_marks()

    def _pin_list(self):
        self.pin_pick.configure(values=[s["title"] for s in self.scans])
        if self.pinned is None:
            self.pin_pick.set("")
        else:
            self.pin_pick.current(self.scans.index(self.pinned))

    def pin(self, scan=None):
        scan = scan or self.shown
        if scan is None:
            return
        self.pinned = scan
        self.pin_manual = True
        self._pin_list()
        self._buttons()
        self.redraw()

    def unpin(self):
        self.pinned = None
        self.pin_manual = False
        self._pin_list()
        self._buttons()
        self.redraw()

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
        if self.paused is not None and self.running is None and not self._cancel:
            self.status.set(f"Image {self.paused['id']} is paused. Double-click Start scan to cancel it "
                            "and start a new scan, or press Continue.")
            return
        if self.running is not None or self.paused is not None:
            return
        try:
            rng, n, t = self._read_settings()
        except ValueError as e:
            self.status.set(str(e))
            return
        if not self.engine.acquire("An image scan is running.", lambda: self._begin(rng, n, t)):
            self.status.set(f"Busy: {self.engine.busy}")

    def _begin(self, rng, n, t):
        unknown = [a for a in ("x", "y", "z") if self.engine.position(a) is None]
        before = tuple(self.engine.position(a) for a in ("x", "y"))  # go back here after
        pos = [self.engine.position(a) or 0.0 for a in ("x", "y", "z")]
        mn1, mx1, c1 = centred_range(pos[0], rng, *self.engine.limits("x"))
        mn2, mx2, c2 = centred_range(pos[1], rng, *self.engine.limits("y"))
        sid = self.engine.next_id("image")
        ts = datetime.datetime.now()
        scan = {"id": sid, "timestamp": ts, "axis_1": "x", "axis_2": "y", "range": rng, "n_pixels": n,
                "time": t, "time_per_pixel": t / n, "start_position_vector": tuple(pos),
                "start_position_axis_1": c1, "start_position_axis_2": c2,
                "min_position_1": mn1, "max_position_1": mx1, "min_position_2": mn2, "max_position_2": mx2,
                "data_x": np.linspace(mn1, mx1, n), "data_y": np.linspace(mn2, mx2, n),
                "data_z": np.full((n, n), np.nan), "index": 0, "return_to": before,
                "mode": self.cfg.get("mode", "raster"),
                "title": f"Image {sid}  {ts.strftime('%H:%M:%S')}  {rng:g} µm, {n} px, {t:g} s/row"}
        if not self.pin_manual and self.shown is not None:
            self.pinned = self.shown  # nothing pinned by hand: the previous scan moves over
        self._add_scan(scan)
        note = f" ({', '.join(unknown).upper()} not set yet, using 0)" if unknown else ""
        self._run(scan, f"Scanning image {sid}{note}...")

    def pause_or_continue(self):
        if self.paused is not None and self.running is None:
            scan = self.paused
            if not self.engine.acquire("An image scan is running.",
                                       lambda: self._run(scan, f"Continuing image {scan['id']}...")):
                self.status.set(f"Busy: {self.engine.busy}")
        elif self.running is not None:
            self._pause = True
            self.status.set("Pausing after this row...")
            self._buttons()

    def restart(self):
        """Double-click on Start scan while paused: Cancel, then Start."""
        if self.paused is None or self.running is not None or self._cancel:
            return
        self._restart = True
        self.cancel()
        if not self._cancel:  # the cancel could not begin (DAQ busy)
            self._restart = False

    def cancel(self):
        """End the scan early and go back to where it started."""
        if self.running is not None:
            self._cancel = True
            self.status.set("Cancelling after this row...")
            self._buttons()
        elif self.paused is not None:
            scan = self.paused
            self._cancel = True
            self._buttons()
            if not self.engine.acquire("An image scan is ending.", lambda: self._finish(
                    scan, f"Image {scan['id']} cancelled at row {scan['index']} of {scan['n_pixels']}.",
                    home=True, cancelled=True)):
                self._cancel = False
                self.status.set(f"Busy: {self.engine.busy}")
                self._buttons()

    def _run(self, scan, text):
        self.running = scan
        self.paused = None
        self._pause = False
        self._cancel = False
        self.status.set(text)
        self._buttons()
        self.engine.submit("scan start", lambda: self.engine.ctrl().counter_controller.start(), self._row_or_fail)

    def _row_job(self, scan, index):
        c = self.engine.ctrl()
        ax1 = getattr(c, f"{scan['axis_1']}_axis_controller")
        ax2 = getattr(c, f"{scan['axis_2']}_axis_controller")
        c._set_axis(axis_controller=ax2, position=float(scan["data_y"][index]))
        time.sleep(c.inter_scan_settle_time)
        n = scan["n_pixels"]
        lo, hi = scan["min_position_1"], scan["max_position_1"]
        if scan.get("mode", "raster") == "snake" and index % 2 == 1:
            # Snake (TQLM qt3scan): odd rows run backwards. Faster, but the piezo
            # lags the other way on these rows, so edges shift row to row.
            row = c._scan_axis(axis_controller=ax1, start=hi, stop=lo, n_pixels=n, scan_time=scan["time"])[::-1]
        else:
            row = c._scan_axis(axis_controller=ax1, start=lo, stop=hi, n_pixels=n, scan_time=scan["time"])
            if scan.get("mode", "raster") != "snake":
                # Raster (VQLM qdlscan): every row runs the same way, then the
                # piezo ramps back over inter_scan_settle_time, as qdlscan does,
                # rather than jumping.
                settle = float(c.inter_scan_settle_time)
                for pos in np.linspace(hi, lo, n):
                    c._set_axis(axis_controller=ax1, position=float(pos))
                    if settle > 0:
                        time.sleep(settle / n)
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
        if self._cancel:
            return self._finish(scan, f"Image {scan['id']} cancelled at row {scan['index']} of {scan['n_pixels']}.",
                                home=True, cancelled=True)
        if self._pause:
            return self._finish(scan, f"Paused at row {scan['index']} of {scan['n_pixels']}. Continue resumes it.",
                                home=False, paused=True)
        i = scan["index"]
        self.engine.submit("scan row", lambda: self._row_job(scan, i), lambda ok, res: self._row_done(scan, i, ok, res))

    def _row_done(self, scan, i, ok, result):
        if ok:
            scan["data_z"][i] = result
            scan["index"] = i + 1
            if self.shown is scan or self.pinned is scan:
                self.redraw()
            self._gallery_update(scan)
            self.status.set(f"Image {scan['id']}: row {i + 1} of {scan['n_pixels']}")
        self._row_or_fail(ok, result)

    def _finish(self, scan, text, home, paused=False, cancelled=False):
        def stop_job():
            c = self.engine.ctrl()
            c.counter_controller.stop()
            if home:  # back to where the piezos were before the scan started
                for axis, pos in zip((scan["axis_1"], scan["axis_2"]), scan["return_to"]):
                    if pos is not None:
                        c._set_axis(axis_controller=getattr(c, f"{axis}_axis_controller"), position=pos)

        def done(ok, result):
            self.running = None
            self.paused = scan if paused else None
            self._pause = self._cancel = False
            if cancelled:
                scan["title"] += "  (cancelled)"
                self.pick.configure(values=[s["title"] for s in self.scans])
                if self.shown in self.scans:
                    self.pick.current(self.scans.index(self.shown))
                self._pin_list()
            self.engine.release()
            self.status.set(text if ok else f"{text} Stopping failed: {result}")
            self._buttons()
            self.redraw()
            if self._restart:
                self._restart = False
                if ok and self.paused is None:
                    self.frame.after(10, self.start)

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
        for sc in self.scans:
            self._gallery_update(sc)

    def auto_norm(self):
        self.norm = (None, None)
        self.redraw()
        for sc in self.scans:
            self._gallery_update(sc)

    def redraw(self):
        """Two square images side by side: the newest (or chosen) scan on the
        left, the pinned scan on the right. The left scan's area is outlined
        on the pinned one: solid when both were taken at the same Z, fading
        the farther apart their Z is (faintest at box_z_full µm or more)."""
        if self.fig is None:
            return
        self.fig.clear()
        pt = self.fig.dpi / 72.0
        w = self.fig.get_figwidth() * self.fig.dpi
        h = self.fig.get_figheight() * self.fig.dpi
        left, bottom, top, gap = 48 * pt, 36 * pt, 28 * pt, 16 * pt
        pad, bar, bar_labels = 6 * pt, 9 * pt, 34 * pt
        extra = left + pad + bar + bar_labels
        side = max(10.0, min(h - bottom - top, (w - gap - 2 * extra) / 2))
        bottom = h - top - side  # images at the top of the area, not the bottom
        frame = self._empty_extent()
        self._pane(0.0, side, self.shown, frame, "newest" if self.shown is self._newest() else "selected",
                   "Start a scan", w, h, left, bottom, pad, bar)
        ax2 = self._pane(extra + side + gap, side, self.pinned, frame, "pinned",
                         "Pin a scan to keep it here", w, h, left, bottom, pad, bar)
        if self.pinned is not None and self.shown is not None and self.shown is not self.pinned:
            self._outline(ax2, self.shown, self.pinned)
        self.canvas.draw_idle()

    def _newest(self):
        return self.scans[-1] if self.scans else None

    def _empty_extent(self):
        """Square the size of the scan Start would take now."""
        try:
            rng = float(self.range_e.get())
        except (AttributeError, ValueError):
            rng = float(self.cfg.get("range", 80))
        cx = centred_range(self.engine.position("x") or 0.0, rng, *self.engine.limits("x"))
        cy = centred_range(self.engine.position("y") or 0.0, rng, *self.engine.limits("y"))
        return [cx[0], cx[1], cy[0], cy[1]]

    @staticmethod
    def _extent(scan):
        pw = scan["range"] / scan["n_pixels"]
        return [scan["min_position_1"] - pw / 2, scan["max_position_1"] + pw / 2,
                scan["min_position_2"] - pw / 2, scan["max_position_2"] + pw / 2]

    def _pane(self, x0, side, scan, frame, title, empty_text, w, h, left, bottom, pad, bar):
        from matplotlib.ticker import MaxNLocator
        ax = self.fig.add_axes([(x0 + left) / w, bottom / h, side / w, side / h])
        ax.tick_params(labelsize=FONT)
        ax.xaxis.set_major_locator(MaxNLocator(4))
        ax.yaxis.set_major_locator(MaxNLocator(4))
        ax.grid(alpha=0.3)
        ax.set_facecolor("#e6e6e6" if scan is not None else "white")  # grey = rows not scanned yet
        if scan is None:
            ax.set_xlim(frame[0], frame[1])
            ax.set_ylim(frame[2], frame[3])
            ax.set_aspect("equal")
            ax.set_xlabel("x position (µm)", fontsize=FONT)
            ax.set_ylabel("y position (µm)", fontsize=FONT)
            ax.text(0.5, 0.5, empty_text, ha="center", va="center", transform=ax.transAxes,
                    color="#888888", fontsize=FONT + 1)
            return ax
        img = ax.imshow(scan["data_z"], extent=self._extent(scan), cmap=DEFAULT_COLOR_MAP, origin="lower",
                        aspect="equal", interpolation="none")
        if self.norm[0] is not None:
            from matplotlib.colors import Normalize
            img.set_norm(Normalize(vmin=self.norm[0], vmax=self.norm[1]))
        cax = self.fig.add_axes([(x0 + left + side + pad) / w, bottom / h, bar / w, side / h])
        cbar = self.fig.colorbar(img, cax=cax)
        cbar.ax.set_title("cts/s", fontsize=FONT - 1, pad=3)
        cbar.ax.tick_params(labelsize=FONT - 1)
        ax.set_xlabel(f"{scan['axis_1']} position (µm)", fontsize=FONT)
        ax.set_ylabel(f"{scan['axis_2']} position (µm)", fontsize=FONT)
        z = scan["start_position_vector"][2]
        ax.set_title(f"{title} {scan['id']} · z {z:.2f} µm", fontsize=FONT, pad=14)
        if scan is self.running and scan["index"] < scan["n_pixels"]:
            ax.axhline(scan["data_y"][scan["index"]], color="#1f8f2f", linewidth=1)
        x, y = self.engine.position("x"), self.engine.position("y")
        if x is not None and y is not None:
            ax.plot(x, y, "o", fillstyle="none", markeredgecolor="#1864ab", markeredgewidth=2)
        ax.set_xlim(*self._extent(scan)[:2])
        ax.set_ylim(*self._extent(scan)[2:])
        return ax

    def _outline(self, ax, scan, ref):
        from matplotlib.patches import Rectangle
        e = self._extent(scan)
        dz = scan["start_position_vector"][2] - ref["start_position_vector"][2]
        alpha = 1.0 - 0.85 * min(1.0, abs(dz) / self.box_z_full) if self.box_z_full > 0 else 1.0
        ax.add_patch(Rectangle((e[0], e[2]), e[1] - e[0], e[3] - e[2], fill=False, edgecolor="#ff9f1c",
                               linewidth=1.8, alpha=alpha, clip_on=True))

    def _go_to(self):
        x, y = self._rclick_xy
        if x is None or self.engine.busy:
            self.status.set(self.engine.busy or "Right-click inside the image.")
            return
        self.engine.move([("x", float(x)), ("y", float(y))],
                         lambda ok, res: (self.status.set(f"Moved to x {x:.2f}, y {y:.2f} µm" if ok else f"Move failed: {res}"),
                                          self.redraw()))

    def save(self, scan=None):
        scan = scan or self.shown
        if scan is None or scan is self.running:
            return
        path = self._ask_save_path("scan", scan)
        if path is None:
            return
        folder, stem, ext = path
        try:
            if ext == ".png":
                self._save_png(os.path.join(folder, stem + ".png"), scan)
            self._save_hdf5(os.path.join(folder, stem + ".hdf5"), stem, scan)
            self.msg_var.set(f"Saved {stem} in {folder}")
        except Exception as e:
            logger.exception("save failed")
            self.msg_var.set(f"Save failed: {e}")

    def _save_png(self, path, scan):
        """The one scan on its own (not both panes), like qdlscan's saved image."""
        from matplotlib.figure import Figure
        fig = Figure(figsize=(5.5, 4.5))
        ax = fig.add_subplot(111)
        img = ax.imshow(scan["data_z"], extent=self._extent(scan), cmap=DEFAULT_COLOR_MAP, origin="lower",
                        aspect="equal", interpolation="none")
        if self.norm[0] is not None:
            from matplotlib.colors import Normalize
            img.set_norm(Normalize(vmin=self.norm[0], vmax=self.norm[1]))
        fig.colorbar(img, ax=ax).ax.set_ylabel("Intensity (cts/s)", rotation=270, labelpad=15)
        ax.set_xlabel(f"{scan['axis_1']} position (µm)")
        ax.set_ylabel(f"{scan['axis_2']} position (µm)")
        fig.tight_layout()
        fig.savefig(path, dpi=300)

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
            ds = df.create_dataset("scan_settings/scan_mode", data=s.get("mode", "raster"))
            ds.attrs["units"] = "None"
            ds.attrs["description"] = "raster: every row the same way; snake: odd rows backwards."
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
        ttk.Button(self.left, text="Reset plots", command=self.clear_plots).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.status = tk.StringVar(value="Scans around the current position, then moves to the fitted peak.")
        ttk.Label(self.left, textvariable=self.status, foreground=GRAY, justify="left",
                  wraplength=int(230 * scale)).grid(row=7, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._lock_left_width()
        self.redraw()
        if self.fig is not None:  # margins are in points: recompute when the tab resizes
            self.canvas.mpl_connect("resize_event", lambda e: self.redraw())
            self.canvas.mpl_connect("motion_notify_event", self._on_hover)

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
        sid = self.engine.next_id("line")
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
        """Three plots side by side, X | Y | Z. Every line scan of the session is
        drawn on its axis's plot in turn through the colour cycle; the newest one
        on each plot is drawn heavier, with a dashed line at the position it moved
        to. The title shows where the other two piezos were for the newest scan;
        hovering over a line shows that scan's number and the other two piezos.
        The position axis widens to cover every scan on that plot, and all
        three share one intensity scale."""
        if self.fig is None:
            return
        self.fig.clear()
        pt = self.fig.dpi / 72.0
        w = self.fig.get_figwidth() * self.fig.dpi
        h = self.fig.get_figheight() * self.fig.dpi
        left, gap, right, bottom, top = 56 * pt, 14 * pt, 10 * pt, 36 * pt, 18 * pt
        pw = max(10.0, (w - left - right - 2 * gap) / 3)
        ph = max(10.0, h - bottom - top)
        self.axes_by_name = {}
        self._hover_lines = []  # (axes, Line2D, scan)
        self._hover_note = None
        colours = [c["color"] for c in __import__("matplotlib").rcParams["axes.prop_cycle"]]
        for k, name in enumerate(("x", "y", "z")):
            ax = self.fig.add_axes([(left + k * (pw + gap)) / w, bottom / h, pw / w, ph / h])
            self.axes_by_name[name] = ax
            ax.tick_params(labelsize=FONT)
            ax.grid(alpha=0.3)
            ax.set_xlabel(f"{name} position (µm)", fontsize=FONT)
            if k == 0:
                ax.set_ylabel("Intensity (cts/s)", fontsize=FONT)
            else:  # same intensity scale on all three: labels only on the left plot
                ax.tick_params(labelleft=False)
            scans = [sc for sc in self.scans if sc["axis"] == name]
            if not scans:
                ax.text(0.5, 0.5, f"Optimize {name.upper()}", ha="center", va="center",
                        transform=ax.transAxes, color="#888888", fontsize=FONT + 1)
                continue
            others = [o for o in ("x", "y", "z") if o != name]
            for n, sc in enumerate(scans):
                newest = n == len(scans) - 1
                colour = colours[n % len(colours)]
                vec = sc["start_position_vector"]
                label = f"{sc['id']}  " + "  ".join(f"{o} {vec[AXIS_INDEX[o]]:.2f}" for o in others)
                if np.all(np.isnan(sc["data_y"])):
                    ax.plot([], [], color=colour, linewidth=2.4 if newest else 1.0, label=label + " …")
                else:
                    (line,) = ax.plot(sc["data_x"], sc["data_y"], color=colour, label=label,
                                      linewidth=2.4 if newest else 1.0, alpha=1.0 if newest else 0.75,
                                      zorder=3 if newest else 2)
                    self._hover_lines.append((ax, line, sc))
                if newest and sc["final_position_axis"] is not None:
                    ax.axvline(sc["final_position_axis"], color=colour, linewidth=1.2, linestyle="--", zorder=1)
            ax.set_xlim(min(sc["min_position"] for sc in scans), max(sc["max_position"] for sc in scans))
            vec = scans[-1]["start_position_vector"]
            ax.set_title("  ".join(f"{o} {vec[AXIS_INDEX[o]]:.2f}" for o in others) + " µm",
                         fontsize=FONT, color=colours[(len(scans) - 1) % len(colours)], pad=3)
        # One intensity scale for X, Y and Z so peak heights can be compared across.
        data = [sc["data_y"] for sc in self.scans if not np.all(np.isnan(sc["data_y"]))]
        if data:
            lo = min(0.0, float(min(np.nanmin(d) for d in data)))
            hi = float(max(np.nanmax(d) for d in data))
            hi = hi + 0.05 * (hi - lo) if hi > lo else lo + 1.0
            for ax in self.axes_by_name.values():
                ax.set_ylim(lo, hi)
        self.canvas.draw_idle()

    def _on_hover(self, event):
        """Show scan number and the other two piezo positions for the line under the mouse."""
        note = getattr(self, "_hover_note", None)
        best = None
        if event.inaxes is not None and event.x is not None:
            for ax, line, sc in getattr(self, "_hover_lines", []):
                if ax is not event.inaxes:
                    continue
                pts = ax.transData.transform(np.column_stack([line.get_xdata(), line.get_ydata()]))
                d = np.nanmin(np.hypot(pts[:, 0] - event.x, pts[:, 1] - event.y))
                if d < 8 * self.fig.dpi / 72.0 and (best is None or d < best[0]):
                    best = (d, ax, sc, line.get_color())
        if best is None:
            if note is not None and note.get_visible():
                note.set_visible(False)
                self.canvas.draw_idle()
            return
        _, ax, sc, colour = best
        vec = sc["start_position_vector"]
        others = [o for o in ("x", "y", "z") if o != sc["axis"]]
        text = (f"scan {sc['id']}  {sc['timestamp'].strftime('%H:%M:%S')}\n"
                + ", ".join(f"{o} {vec[AXIS_INDEX[o]]:.2f}" for o in others) + " µm")
        if sc["final_position_axis"] is not None:
            text += f"\nmoved {sc['axis']} to {sc['final_position_axis']:.3f} µm"
        if note is None or note.axes is not ax:
            if note is not None:
                note.remove()
            note = ax.annotate("", xy=(0, 0), xytext=(8, 8), textcoords="offset points", fontsize=FONT - 1,
                               bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#888888", alpha=0.95),
                               zorder=10, annotation_clip=False)
            self._hover_note = note
        note.xy = (event.xdata, event.ydata)
        note.set_text(text)
        note.get_bbox_patch().set_edgecolor(colour)
        # keep the box inside the plot: flip it left of the mouse on the right half
        right_half = event.x > (ax.bbox.x0 + ax.bbox.x1) / 2
        top_half = event.y > (ax.bbox.y0 + ax.bbox.y1) / 2
        note.set_position((-8 if right_half else 8, -8 if top_half else 8))
        note.set_ha("right" if right_half else "left")
        note.set_va("top" if top_half else "bottom")
        note.set_visible(True)
        for other in self.axes_by_name.values():  # draw the box above the neighbouring plots
            other.set_zorder(1 if other is ax else 0)
        self.canvas.draw_idle()

    def clear_plots(self):
        if self.engine.busy.startswith("Optimizing"):
            return
        self.scans = []
        self.shown = None
        self.pick.configure(values=[])
        self.pick.set("")
        self._set_running(False)
        self.redraw()

    def _go_to(self):
        x = self._rclick_xy[0]
        name = next((n for n, a in getattr(self, "axes_by_name", {}).items()
                     if a is getattr(self, "_rclick_ax", None)), None)
        if x is None or name is None or self.engine.busy:
            self.status.set(self.engine.busy or "Right-click inside one of the plots.")
            return
        scans = [sc for sc in self.scans if sc["axis"] == name]

        def done(ok, res):
            if ok and scans:
                scans[-1]["final_position_axis"] = float(x)
            self.status.set(f"Moved {name.upper()} to {x:.3f} µm" if ok else f"Move failed: {res}")
            self.redraw()

        self.engine.move([(name, float(x))], done)

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
