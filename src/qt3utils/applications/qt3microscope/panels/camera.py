"""
Camera tab (qt3camera): live view from the microscope's camera.

VQLM: Thorlabs CS165MU through the Thorlabs Scientific Camera SDK.
TQLM: NIT SWIR camera (driver still to come). The profile's
hardware.camera entry picks the driver; "fake" draws a test image.

Why this is quicker than the vendor programs: the camera runs on its own
grab thread and only the newest frame is kept; the window shrinks that
frame to the size it is shown at before converting it, and draws it as a
plain Tk image (no matplotlib). Frames that arrive while one is being
drawn are skipped, so the view never lags behind the camera.

Controls: Play/Pause (Pause freezes the last frame, which can still be
zoomed, checked against the crosshair or saved), exposure and gain (box,
slider, or mouse wheel over either; Shift for finer steps), auto exposure
(software: keeps the brightest pixels near 60% of full scale), auto or
fixed contrast, saturated pixels in red, crosshair, zoom (mouse wheel,
drag to pan, Fit), Snapshot (a PNG as shown on screen, the raw camera counts as a
16-bit _raw.tif, and a .json with exposure, gain and piezo position; to the M drive when it is reachable, else
Documents/qt3microscope_camera, unless a folder was picked with Folder…), crosshair and/or a
reticle circle of adjustable diameter (Shift+click or right-click moves them), and Release (next to Snapshot), which lets ThorCam (or the NIT
software) open the camera while QT3 Microscope stays open.

The camera is opened at the first Play, not at launch. Leaving the tab
pauses live view; it stays paused until Play is pressed again.
"""

import datetime
import json
import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

import numpy as np

from qt3utils.applications.qt3microscope.widgets import GRAY, GREEN, ORANGE, RED, ToolTip
from qt3utils.applications.qt3microscope.theme import C

logger = logging.getLogger(__name__)

STATE_DIR = os.path.join(os.path.expanduser("~"), ".qt3utils")


def default_save_dir():
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    return os.path.join(docs if os.path.isdir(docs) else os.path.expanduser("~"), "qt3microscope_camera")


# Snapshots go to the M drive (host-m mhome share) when it is reachable, and
# to the local folder above when it isn't. A profile can list its own
# choices as camera: {snapshot_dirs: [...]}; the first one whose drive or
# share is reachable wins.
M_DRIVE_DIRS = [r"M:\qt3microscope_camera", r"\\host-m.coral.uw.edu\mhome\qt3microscope_camera"]


def _reachable(path):
    """True when the drive or share that holds path is there (the folder itself may not exist yet)."""
    parent = os.path.dirname(os.path.normpath(path))
    try:
        return os.path.isdir(path) or (parent and os.path.isdir(parent))
    except OSError:
        return False


def pick_save_dir(candidates):
    for path in candidates or []:
        if path and _reachable(path):
            return path, True
    return default_save_dir(), False


class CameraStream:
    """Owns the camera on one thread. The Tk side sends commands and reads
    the newest frame; it never calls the camera itself."""

    def __init__(self, cfg):
        self.cfg = dict(cfg or {})
        self.camera = None
        self.commands = queue.Queue()
        self.events = queue.Queue()      # (kind, value) back to the tab
        self._lock = threading.Lock()
        self._latest = None
        self.playing = False
        self._stop = False
        self._fps_times = []
        self.fps = 0.0
        self._thread = threading.Thread(target=self._loop, name="hub-camera", daemon=True)
        self._thread.start()

    def send(self, *cmd):
        self.commands.put(cmd)

    def latest(self):
        with self._lock:
            return self._latest

    def shutdown(self, timeout=3.0):
        self.send("close")
        self._stop = True
        self._thread.join(timeout)

    # ----- grab thread -----
    def _loop(self):
        while True:
            try:
                while True:
                    self._do(*self.commands.get_nowait())
            except queue.Empty:
                pass
            if self._stop and self.camera is None:
                return
            if self.playing and self.camera is not None:
                try:
                    frame = self.camera.read(0.1)
                except Exception as e:
                    logger.exception("camera read failed")
                    self.playing = False
                    self.events.put(("error", f"Read failed: {e}"))
                    continue
                if frame is not None:
                    with self._lock:
                        self._latest = frame
                    now = time.time()
                    self._fps_times = [t for t in self._fps_times if now - t < 2.0] + [now]
                    if len(self._fps_times) > 1:
                        self.fps = (len(self._fps_times) - 1) / (self._fps_times[-1] - self._fps_times[0])
            else:
                time.sleep(0.03)

    def _ensure_open(self):
        if self.camera is not None:
            return
        from qt3utils.hardware.cameras.base import make_camera
        cam = make_camera(self.cfg)
        cam.open()
        self.camera = cam
        self._report_settings()

    def _report_settings(self):
        c = self.camera
        self.events.put(("opened", {"info": c.info(), "exposure": c.get_exposure_ms(),
                                    "exposure_range": c.exposure_range_ms(),
                                    "gain": c.get_gain_db(), "gain_range": c.gain_range_db(),
                                    "bit_depth": getattr(c, "bit_depth", 16)}))

    def _do(self, cmd, *args):
        try:
            if cmd == "play":
                self._ensure_open()
                self.camera.start()
                self.playing = True
                self._fps_times = []
                self.events.put(("playing", True))
            elif cmd == "pause":
                self.playing = False
                if self.camera is not None:
                    self.camera.stop()
                self.events.put(("playing", False))
            elif cmd in ("exposure", "gain"):
                if self.camera is None:
                    self.cfg["exposure_ms" if cmd == "exposure" else "gain_db"] = args[0]
                    return
                if cmd == "exposure":
                    self.camera.set_exposure_ms(args[0])
                    self.events.put(("exposure", self.camera.get_exposure_ms()))
                else:
                    self.camera.set_gain_db(args[0])
                    self.events.put(("gain", self.camera.get_gain_db()))
            elif cmd in ("close", "release"):
                self.playing = False
                if self.camera is not None:
                    cam, self.camera = self.camera, None
                    cam.close()
                self.events.put(("released", None))
        except Exception as e:
            logger.exception("camera %s failed", cmd)
            if cmd == "play":
                self.playing = False
                self.events.put(("playing", False))
            self.events.put(("error", f"{cmd.capitalize()} failed: {e}"))


class CameraTab:
    def __init__(self, page, cfg, setup="?", stage=None, set_status=None, scale=1.0, hub=None):
        self.page = page
        self.cfg = dict(cfg or {})
        self.setup = setup
        self.stage = stage
        self.set_status = set_status or (lambda *a: None)
        self.scale = scale
        self.state_path = os.path.join(STATE_DIR, f"qt3microscope_camera_{setup}.json")
        st = self._load_state()

        if "exposure_ms" in st:
            self.cfg["exposure_ms"] = st["exposure_ms"]
        if "gain_db" in st:
            self.cfg["gain_db"] = st["gain_db"]
        self.stream = CameraStream(self.cfg)
        if hub is not None:
            hub.on_close(self.close)

        self.bit_depth = 16
        self.full_scale = 65535
        self.info = {}
        self.playing = False
        self.want_play = False       # remembered when the tab is hidden
        self.visible = True
        self.shown_index = None
        self.frozen = None           # frame shown (newest or paused)
        self.zoom = None             # display px per image px; None = fit
        self.center = None           # (x, y) image coords at the canvas center
        self._drag = None
        self._photo = None
        self._dirty = True
        self.display_ms = int(1000 / float(self.cfg.get("display_fps", 30)))

        self.v_exposure = tk.StringVar(value=f"{float(self.cfg.get('exposure_ms', 10.0)):g}")
        self.v_gain = tk.StringVar(value=f"{float(self.cfg.get('gain_db', 0.0)):g}")
        self.v_auto = tk.BooleanVar(value=st.get("auto_contrast", True))
        self.v_autoexp = tk.BooleanVar(value=st.get("auto_exposure", False))
        self._autoexp_wait_until = 0.0
        self.autoexp_target = float(self.cfg.get("auto_exposure_target", 0.6))
        self.v_lo = tk.StringVar(value=str(st.get("lo", 0)))
        self.v_hi = tk.StringVar(value=str(st.get("hi", 4095)))
        self.v_cross = tk.BooleanVar(value=st.get("crosshair", True))
        # Crosshair/reticle center in camera pixels; None = image center.
        self.mark = tuple(st["mark"]) if isinstance(st.get("mark"), list) else None
        self.v_reticle = tk.BooleanVar(value=st.get("reticle", False))
        self.v_reticle_px = tk.StringVar(value=f"{float(st.get('reticle_px', 100)):g}")
        self.v_sat = tk.BooleanVar(value=st.get("show_saturated", True))
        # A folder picked with Folder… sticks; otherwise the M drive if reachable.
        self.user_dir = st.get("user_save_dir") or self.cfg.get("save_dir")
        self.dir_candidates = self.cfg.get("snapshot_dirs") or M_DRIVE_DIRS
        self.save_dir = self._current_save_dir()
        self.v_msg = tk.StringVar(value="Press Play for live view. Wheel zooms, drag pans, Shift+click or right-click moves the crosshair.")
        self.v_readout = tk.StringVar(value="")
        self.v_cursor = tk.StringVar(value="")
        self.v_camera = tk.StringVar(value=self.cfg.get("label") or self.cfg.get("driver", "camera"))

        self._build()
        self.set_status("off", GRAY)
        self.page.after(self.display_ms, self._tick)

    def px(self, v):
        return int(round(v * self.scale))

    # ----- layout -----
    def _build(self):
        left = ttk.Frame(self.page, padding=(10, 8))
        left.pack(side="left", fill="y")
        right = ttk.Frame(self.page)
        right.pack(side="left", fill="both", expand=True)

        ttk.Label(left, textvariable=self.v_camera, font=("TkDefaultFont", 10, "bold"),
                  wraplength=self.px(220)).pack(anchor="w")
        top = ttk.Frame(left)
        top.pack(fill="x", pady=(8, 6))
        # Same colors as Start/Pause in the scope and scans: green Play, plain Pause.
        probe = tk.Button(top)
        self._plain_bg = probe.cget("bg"), probe.cget("fg")
        probe.destroy()
        self.play_btn = tk.Button(top, text="▶  Play", width=10, font=("TkDefaultFont", 11, "bold"),
                                  command=self.toggle_play)
        self._play_look(False)
        self.play_btn.pack(side="left")
        tk.Button(top, text="Snapshot", font=("TkDefaultFont", 10), command=self.snapshot).pack(
            side="left", padx=(6, 0), fill="y")
        release_btn = ttk.Button(top, text="Release", width=8, command=self.release)
        release_btn.pack(side="left", padx=(6, 0), fill="y")
        ToolTip(release_btn, "Disconnect from the camera so ThorCam (or another program) can open it. "
                             "Only one program can use the camera at a time. "
                             "Close the other program, then press Play to reconnect.",
                wraplength=self.px(320))

        g = ttk.LabelFrame(left, text="Camera", padding=6)
        g.pack(fill="x", pady=4)
        # Exposure and gain: type a value and press Enter, drag the slider, or
        # turn the mouse wheel over the box or slider (Shift = finer steps).
        # The exposure slider is logarithmic.
        self.exp_range = tuple(self.cfg.get("exposure_range_ms", (0.01, 1000.0)))
        self.gain_range = tuple(self.cfg.get("gain_range_db", (0.0, 48.0)))
        self._touched = {"exposure": 0.0, "gain": 0.0}
        self._apply_job = {"exposure": None, "gain": None}
        g.columnconfigure(1, weight=1)
        ttk.Checkbutton(g, text="Auto exposure", variable=self.v_autoexp,
                        command=self._autoexp_changed).grid(row=6, column=0, columnspan=2, sticky="w")
        self.exp_label = ttk.Label(g, text="Exposure (ms)")
        self.exp_label.grid(row=0, column=0, sticky="w")
        self.exp_entry = ttk.Entry(g, textvariable=self.v_exposure, width=9)
        self.exp_entry.grid(row=0, column=1, sticky="e", padx=4, pady=2)
        self.exp_slider = ttk.Scale(g, orient="horizontal", from_=0.0, to=1.0,
                                    command=lambda v: self._slid("exposure", float(v)))
        self.exp_slider.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Label(g, text="Gain (dB)").grid(row=2, column=0, sticky="w")
        self.gain_entry = ttk.Entry(g, textvariable=self.v_gain, width=9)
        self.gain_entry.grid(row=2, column=1, sticky="e", padx=4, pady=2)
        self.gain_slider = ttk.Scale(g, orient="horizontal", from_=0.0, to=1.0,
                                     command=lambda v: self._slid("gain", float(v)))
        self.gain_slider.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        for what, entry, slider in (("exposure", self.exp_entry, self.exp_slider),
                                    ("gain", self.gain_entry, self.gain_slider)):
            entry.bind("<Return>", lambda _e, w=what: self._typed(w))
            entry.bind("<KP_Enter>", lambda _e, w=what: self._typed(w))
            for widget in (entry, slider):
                widget.bind("<MouseWheel>", lambda e, w=what: self._wheel_value(w, e))
                widget.bind("<Button-4>", lambda e, w=what: self._wheel_value(w, e, +1))
                widget.bind("<Button-5>", lambda e, w=what: self._wheel_value(w, e, -1))
        self.exp_unit = "ms"
        self._put("exposure", float(self.cfg.get("exposure_ms", 10.0)))
        self._sync_sliders()
        self.range_lbl = ttk.Label(g, text="", foreground=C.muted, wraplength=self.px(210), justify="left")
        self.range_lbl.grid(row=4, column=0, columnspan=2, sticky="w")
        self._autoexp_changed(save=False)

        c = ttk.LabelFrame(left, text="Contrast", padding=6)
        c.pack(fill="x", pady=4)
        ttk.Checkbutton(c, text="Auto", variable=self.v_auto, command=self._view_changed).grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.lo_label = ttk.Label(c, text="Black")
        self.lo_label.grid(row=1, column=0, sticky="w")
        self.lo_entry = ttk.Entry(c, textvariable=self.v_lo, width=9)
        self.lo_entry.grid(row=1, column=1, padx=4, pady=2)
        self.hi_label = ttk.Label(c, text="White")
        self.hi_label.grid(row=2, column=0, sticky="w")
        self.hi_entry = ttk.Entry(c, textvariable=self.v_hi, width=9)
        self.hi_entry.grid(row=2, column=1, padx=4, pady=2)
        for w in (self.lo_entry, self.hi_entry):
            w.bind("<Return>", lambda _e: self._fixed_contrast())
            w.bind("<KP_Enter>", lambda _e: self._fixed_contrast())
        ttk.Checkbutton(c, text="Saturated pixels in red", variable=self.v_sat,
                        command=self._view_changed).grid(row=3, column=0, columnspan=2, sticky="w")

        v = ttk.LabelFrame(left, text="View", padding=6)
        v.pack(fill="x", pady=4)
        cross_check = ttk.Checkbutton(v, text="Crosshair", variable=self.v_cross, command=self._mark_changed)
        cross_check.grid(row=0, column=0, columnspan=2, sticky="w")
        center_btn = ttk.Button(v, text="Center", width=7, command=self.center_mark)
        center_btn.grid(row=0, column=2, columnspan=2, sticky="e")
        move_tip = ("Move the crosshair and reticle: Shift+click or Shift+drag on the image, "
                    "or right-click the image and choose \"Move crosshair/reticle here\". "
                    "Center puts them back in the middle of the image.")
        ToolTip(cross_check, move_tip, wraplength=self.px(320))
        ToolTip(center_btn, move_tip, wraplength=self.px(320))
        self.reticle_check = ttk.Checkbutton(v, text="Reticle", variable=self.v_reticle,
                                             command=self._reticle_toggled)
        self.reticle_check.grid(row=1, column=0, sticky="w")
        self.reticle_lbl = ttk.Label(v, text="diameter (px)")
        self.reticle_lbl.grid(row=1, column=1, columnspan=2, sticky="e")
        self.reticle_entry = ttk.Entry(v, textvariable=self.v_reticle_px, width=6)
        self.reticle_entry.grid(row=1, column=3, sticky="e", pady=2)
        self.reticle_entry.bind("<Return>", lambda _e: self._reticle_typed())
        self.reticle_entry.bind("<KP_Enter>", lambda _e: self._reticle_typed())
        # Diameter slider (logarithmic, 4-1000 px); the mouse wheel works over
        # the box, the slider and the checkbox.
        self.reticle_slider = ttk.Scale(v, orient="horizontal", from_=0.0, to=1.0,
                                        command=self._reticle_slid)
        self.reticle_slider.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(0, 4))
        v.columnconfigure(0, weight=1)
        for widget in (self.reticle_entry, self.reticle_slider):
            widget.bind("<MouseWheel>", self._wheel_reticle)
            widget.bind("<Button-4>", lambda e: self._wheel_reticle(e, +1))
            widget.bind("<Button-5>", lambda e: self._wheel_reticle(e, -1))
        self._sync_reticle_slider()
        self._show_reticle_controls()
        zoom_row = ttk.Frame(v)
        zoom_row.grid(row=4, column=0, columnspan=4, sticky="w", pady=2)
        ttk.Button(zoom_row, text="Fit", width=5, command=self.fit).pack(side="left")
        ttk.Button(zoom_row, text="1:1", width=5, command=lambda: self.set_zoom(1.0)).pack(side="left", padx=2)
        ttk.Button(zoom_row, text="2×", width=5, command=lambda: self.set_zoom(2.0)).pack(side="left")

        s = ttk.LabelFrame(left, text="Snapshots go to", padding=6)
        s.pack(fill="x", pady=4)
        self.dir_lbl = ttk.Label(s, text=self._short(self.save_dir), foreground=C.muted, width=20)
        self.dir_lbl.pack(side="left")
        ttk.Button(s, text="Folder…", width=8, command=self.choose_dir).pack(side="right")

        self.canvas = tk.Canvas(right, bg="#202020", highlightthickness=0)
        self.canvas.pack(side="top", fill="both", expand=True)
        bar = ttk.Frame(right, padding=(6, 2))
        bar.pack(side="bottom", fill="x")
        self.readout_lbl = tk.Label(bar, textvariable=self.v_readout, anchor="w")
        self.readout_lbl.pack(side="left")
        ttk.Label(bar, textvariable=self.v_msg, foreground=C.muted).pack(side="left", padx=(20, 0))
        ttk.Label(bar, textvariable=self.v_cursor, foreground=C.text).pack(side="right")

        cv = self.canvas
        cv.bind("<Configure>", lambda _e: self._view_changed())
        cv.bind("<MouseWheel>", self._on_wheel)                       # Windows
        cv.bind("<Button-4>", lambda e: self._on_wheel(e, +1))       # Linux
        cv.bind("<Button-5>", lambda e: self._on_wheel(e, -1))
        cv.bind("<Shift-ButtonPress-1>", self._mark_at)
        cv.bind("<Shift-B1-Motion>", self._mark_at)
        cv.bind("<ButtonPress-3>", self._context_menu)
        cv.bind("<ButtonPress-1>", self._drag_start)
        cv.bind("<B1-Motion>", self._drag_move)
        cv.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_drag", None))
        cv.bind("<Double-Button-1>", lambda _e: self.fit())
        cv.bind("<Motion>", self._on_motion)
        cv.bind("<Leave>", lambda _e: self.v_cursor.set(""))
        self._contrast_entries()

    # ----- play / settings -----
    def toggle_play(self):
        if self.playing:
            self.want_play = False
            self.stream.send("pause")
        else:
            self.want_play = True
            self.v_msg.set("Opening the camera…" if not self.info else "Starting…")
            self.play_btn.config(state="disabled")
            self.stream.send("play")

    def _play_look(self, playing):
        """Green with white text for Play (like Start elsewhere), a plain button for Pause."""
        if playing:
            bg, fg = self._plain_bg
            self.play_btn.config(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)
        else:
            self.play_btn.config(bg=C.go, fg="white", activebackground=C.go, activeforeground="white")

    def set_visible(self, visible):
        """Called when the Camera tab is shown or hidden: leaving it pauses the camera."""
        self.visible = visible
        if not visible and self.playing:
            # Stays paused on return (Kendall): press Play to go live again.
            self.want_play = False
            self.stream.send("pause")
        if visible:
            self._dirty = True

    def _number(self, var, name):
        try:
            return float(var.get())
        except ValueError:
            self.v_msg.set(f"{name} must be a number.")
            return None

    # ----- exposure and gain: box, slider, mouse wheel -----
    def _get(self, what):
        """Value in the box, in ms for exposure (the box may be in s) or dB for gain."""
        v = self._float(self._var(what).get())
        if v is not None and what == "exposure" and getattr(self, "exp_unit", "ms") == "s":
            v *= 1000.0
        return v

    def _put(self, what, value):
        """Show a value (ms for exposure); exposure of 1 s or more shows in seconds,
        and the label beside the box says which."""
        if what != "exposure":
            self._var(what).set(self._fmt(what, value))
            return
        unit = "s" if value >= 1000.0 else "ms"
        if unit != getattr(self, "exp_unit", None):
            self.exp_unit = unit
            self.exp_label.config(text=f"Exposure ({unit})")
        self.v_exposure.set(f"{value / 1000.0:.4g}" if unit == "s" else self._fmt("exposure", value))

    def _var(self, what):
        return self.v_exposure if what == "exposure" else self.v_gain

    def _clamp(self, what, value):
        lo, hi = self.exp_range if what == "exposure" else self.gain_range
        return min(max(value, lo), hi)

    def _to_slider(self, what, value):
        lo, hi = self.exp_range if what == "exposure" else self.gain_range
        if what == "exposure":
            lo, hi, value = np.log10(lo), np.log10(hi), np.log10(max(value, 1e-9))
        return 0.0 if hi <= lo else min(max((value - lo) / (hi - lo), 0.0), 1.0)

    def _from_slider(self, what, x):
        lo, hi = self.exp_range if what == "exposure" else self.gain_range
        if what == "exposure":
            return float(10 ** (np.log10(lo) + x * (np.log10(hi) - np.log10(lo))))
        return float(lo + x * (hi - lo))

    @staticmethod
    def _fmt(what, value):
        return f"{value:.3g}" if what == "exposure" and value < 1 else (
            f"{value:.4g}" if what == "exposure" else f"{value:.1f}")

    def _sync_sliders(self):
        """Put the sliders where the boxes say (not while being dragged)."""
        for what, slider in (("exposure", self.exp_slider), ("gain", self.gain_slider)):
            if time.time() - self._touched[what] < 0.5:
                continue
            value = self._get(what)
            if value is not None:
                self._setting_slider = True
                slider.set(self._to_slider(what, value))
                self._setting_slider = False

    def _slid(self, what, x):
        if getattr(self, "_setting_slider", False):
            return
        if what == "gain" and str(self.gain_entry.cget("state")) == "disabled":
            return
        if what == "exposure" and self.v_autoexp.get():
            return
        self._touched[what] = time.time()
        self._put(what, self._from_slider(what, x))
        self._apply_soon(what)

    def _wheel_value(self, what, event, direction=None):
        if what == "gain" and str(self.gain_entry.cget("state")) == "disabled":
            return "break"
        if what == "exposure" and self.v_autoexp.get():
            return "break"
        if direction is None:
            direction = 1 if event.delta > 0 else -1
        fine = bool(event.state & 0x0001)  # Shift held
        value = self._get(what)
        if value is None:
            value = self._from_slider(what, 0.5)
        if what == "exposure":
            value *= (1.02 if fine else 1.2) ** direction
        else:
            value += (0.1 if fine else 1.0) * direction
        value = self._clamp(what, value)
        self._put(what, value)
        self._touched[what] = 0.0
        self._sync_sliders()
        self._touched[what] = time.time()
        self._apply_soon(what)
        return "break"  # don't also scroll anything underneath

    def _typed(self, what):
        value = self._get(what)
        if value is None:
            self.v_msg.set(f"{what.capitalize()} must be a number.")
            return
        self._put(what, self._clamp(what, value))
        self._touched[what] = 0.0
        self._sync_sliders()
        self._apply(what)

    def _autoexp_changed(self, save=True):
        """Auto exposure: the box and slider follow the camera and can't be changed by hand."""
        on = self.v_autoexp.get()
        self.exp_entry.config(state="disabled" if on else "normal")
        self.exp_slider.state(["disabled"] if on else ["!disabled"])
        self._autoexp_wait_until = 0.0
        if save:
            self._save_state()

    def _auto_expose(self):
        """Software auto exposure: keep the brightest pixels (99.9th percentile)
        near auto_exposure_target of full scale (0.6 by default), changing only
        the exposure. Waits for a frame taken with the new exposure before
        the next change."""
        f = self.frozen
        if f is None or f.time < self._autoexp_wait_until:
            return
        exp = self._get("exposure")
        if exp is None:
            return
        level = float(np.percentile(f.image[::4, ::4], 99.9)) / self.full_scale
        if level >= 0.999:          # saturated: we can't tell by how much, so halve
            ratio = 0.5
        else:
            ratio = self.autoexp_target / max(level, 1e-3)
        if 0.85 < ratio < 1.15:     # close enough: leave it alone
            return
        new = self._clamp("exposure", exp * min(max(ratio, 0.25), 4.0))
        if abs(new - exp) / exp < 0.01:  # at the end of the range
            return
        self._put("exposure", new)
        self._touched["exposure"] = 0.0
        self._sync_sliders()
        self.stream.send("exposure", new)
        self._autoexp_wait_until = time.time() + new / 1000.0 + 0.15

    def _apply_soon(self, what, ms=120):
        """Send the value once the slider or wheel stops for a moment."""
        if self._apply_job[what] is not None:
            self.page.after_cancel(self._apply_job[what])
        self._apply_job[what] = self.page.after(ms, lambda: self._apply(what))

    def _apply(self, what):
        self._apply_job[what] = None
        value = self._get(what)
        if value is not None:
            self.stream.send(what, value)
            self._save_state()

    def set_exposure(self):
        self._typed("exposure")

    def set_gain(self):
        self._typed("gain")

    def _fixed_contrast(self):
        self.v_auto.set(False)
        self._view_changed()
        self._save_state()

    def _contrast_entries(self):
        # Black and White are hidden in Auto (Kendall): they only matter when fixed.
        for w in (self.lo_label, self.lo_entry, self.hi_label, self.hi_entry):
            if self.v_auto.get():
                w.grid_remove()
            else:
                w.grid()

    def release(self):
        self.want_play = False
        self.stream.send("release")

    def close(self):
        self._save_state()
        self.stream.shutdown()

    # ----- events from the grab thread -----
    def _handle_events(self):
        try:
            while True:
                kind, val = self.stream.events.get_nowait()
                if kind == "opened":
                    self.info = val["info"]
                    self.bit_depth = int(val.get("bit_depth") or 16)
                    self.full_scale = 2 ** self.bit_depth - 1
                    name = f"{self.info.get('model', '')}  {self.info.get('serial') or ''}".strip()
                    self.v_camera.set(name)
                    self._put("exposure", val["exposure"])
                    lo, hi = val["exposure_range"]
                    self.exp_range = (lo, hi)
                    text = f"Exposure {self._time_text(lo)} to {self._time_text(hi)}"
                    if val["gain_range"]:
                        g0, g1 = val["gain_range"]
                        self.gain_range = (g0, g1)
                        text += f"\nGain {g0:.3g} to {g1:.3g} dB"
                        self.v_gain.set(self._fmt("gain", val["gain"]))
                        self.gain_entry.config(state="normal")
                        self.gain_slider.state(["!disabled"])
                    else:
                        text += "\nNo gain setting"
                        self.v_gain.set("—")
                        self.gain_entry.config(state="disabled")
                        self.gain_slider.state(["disabled"])
                    self.range_lbl.config(text=text)
                    self._sync_sliders()
                    if not self.v_auto.get() and self.v_hi.get() == "4095" and self.bit_depth != 12:
                        self.v_hi.set(str(self.full_scale))
                elif kind == "playing":
                    self.playing = val
                    self.play_btn.config(state="normal",
                                         text="⏸  Pause" if val else "▶  Play")
                    self._play_look(val)
                    if val:
                        self.v_msg.set("Live.")
                        self.set_status("live", GREEN)
                    else:
                        self.v_msg.set("Paused: the last frame stays on screen." if self.frozen
                                       else "Paused.")
                        self.set_status("paused", GRAY)
                elif kind == "exposure":
                    if time.time() - self._touched["exposure"] > 0.5 or self.v_autoexp.get():
                        self._put("exposure", val)
                        self._sync_sliders()
                    if not self.v_autoexp.get():
                        self.v_msg.set(f"Exposure {self._fmt('exposure', val)} ms.")
                elif kind == "gain":
                    if time.time() - self._touched["gain"] > 0.5:
                        self.v_gain.set(self._fmt("gain", val) if val is not None else "—")
                        self._sync_sliders()
                    self.v_msg.set(f"Gain {self._fmt('gain', val)} dB." if val is not None else "Gain set.")
                elif kind == "released":
                    self.playing = False
                    self.info = {}
                    self.play_btn.config(state="normal", text="▶  Play")
                    self._play_look(False)
                    self.v_msg.set("Camera released. Play opens it again.")
                    self.set_status("off", GRAY)
                elif kind == "error":
                    self.play_btn.config(state="normal")
                    self.v_msg.set(val)
                    self.set_status("error", RED)
        except queue.Empty:
            pass

    # ----- drawing -----
    def _tick(self):
        try:
            self._handle_events()
            frame = self.stream.latest()
            if frame is not None and frame.index != self.shown_index and self.playing:
                self.frozen = frame
                self.shown_index = frame.index
                self._dirty = True
            if self.playing and self.v_autoexp.get():
                self._auto_expose()
            if self._dirty and self.visible and self.frozen is not None:
                self._dirty = False
                self._draw()
            if self.playing:
                self._readout()
        except Exception:
            logger.exception("camera display failed")
        self.page.after(self.display_ms, self._tick)

    def _view_changed(self):
        self._contrast_entries()
        self._dirty = True
        if self.frozen is None:
            self._draw_empty()

    def _draw_empty(self):
        cv = self.canvas
        cv.delete("all")
        cv.create_text(cv.winfo_width() // 2, cv.winfo_height() // 2, fill="#aaaaaa",
                       text="No image yet. Press Play.")

    def _fit_zoom(self, img):
        h, w = img.shape
        cw, ch = max(self.canvas.winfo_width(), 10), max(self.canvas.winfo_height(), 10)
        return min(cw / w, ch / h)

    def fit(self):
        self.zoom = None
        self.center = None
        self._dirty = True

    def set_zoom(self, z, about=None):
        if self.frozen is None:
            return
        h, w = self.frozen.image.shape
        fitz = self._fit_zoom(self.frozen.image)
        old = self.zoom or fitz
        z = min(max(z, fitz), 32.0)
        if self.center is None:
            self.center = (w / 2, h / 2)
        if about is not None:  # keep the image point under the mouse in place
            cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
            ix = self.center[0] + (about[0] - cw / 2) / old
            iy = self.center[1] + (about[1] - ch / 2) / old
            self.center = (ix - (about[0] - cw / 2) / z, iy - (about[1] - ch / 2) / z)
        self.zoom = None if abs(z - fitz) < 1e-6 else z
        if self.zoom is None:
            self.center = None
        self._dirty = True

    def _on_wheel(self, event, direction=None):
        if direction is None:
            direction = 1 if event.delta > 0 else -1
        if self.frozen is None:
            return
        cur = self.zoom or self._fit_zoom(self.frozen.image)
        self.set_zoom(cur * (1.25 if direction > 0 else 0.8), about=(event.x, event.y))

    # ----- crosshair / reticle -----
    def _image_xy(self, event):
        if self.frozen is None or not hasattr(self, "_map"):
            return None
        z, cx, cy, cw, ch = self._map
        h, w = self.frozen.image.shape
        x = min(max(cx + (event.x - cw / 2) / z, 0), w - 1)
        y = min(max(cy + (event.y - ch / 2) / z, 0), h - 1)
        return (round(x, 1), round(y, 1))

    def _mark_at(self, event):
        xy = self._image_xy(event)
        if xy is not None:
            self.mark = xy
            self._mark_changed()
        return "break"  # no panning

    def center_mark(self):
        self.mark = None
        self._mark_changed()

    def _mark_changed(self):
        self._dirty = True
        if not self.playing:
            self.page.after_idle(self._draw_if_frame)
        self._save_state()

    def _draw_if_frame(self):
        if self.frozen is not None:
            self._draw()

    RETICLE_RANGE = (4.0, 1000.0)

    def _show_reticle_controls(self):
        """The diameter box and slider only show while Reticle is checked."""
        for w in (self.reticle_lbl, self.reticle_entry, self.reticle_slider):
            if self.v_reticle.get():
                w.grid()
            else:
                w.grid_remove()

    def _reticle_toggled(self):
        self._show_reticle_controls()
        self._mark_changed()

    def _set_reticle(self, d, from_slider=False):
        lo, hi = self.RETICLE_RANGE
        d = min(max(float(d), lo), hi)
        self.v_reticle_px.set(f"{round(d):g}")
        if not from_slider:
            self._sync_reticle_slider()
        self._mark_changed()

    def _sync_reticle_slider(self):
        lo, hi = self.RETICLE_RANGE
        d = min(max(self._float(self.v_reticle_px.get()) or 100.0, lo), hi)
        self._reticle_syncing = True
        self.reticle_slider.set((np.log10(d) - np.log10(lo)) / (np.log10(hi) - np.log10(lo)))
        self._reticle_syncing = False

    def _reticle_slid(self, x):
        if getattr(self, "_reticle_syncing", False):
            return
        lo, hi = self.RETICLE_RANGE
        self._set_reticle(10 ** (np.log10(lo) + float(x) * (np.log10(hi) - np.log10(lo))), from_slider=True)

    def _reticle_typed(self):
        d = self._float(self.v_reticle_px.get())
        if d is None:
            self.v_msg.set("Reticle diameter must be a number.")
            return
        self._set_reticle(d)

    def _wheel_reticle(self, event, direction=None):
        if direction is None:
            direction = 1 if event.delta > 0 else -1
        d = self._float(self.v_reticle_px.get()) or 100.0
        step = 1.0 if (event.state & 0x0001) else max(1.0, round(d * 0.1))
        self._set_reticle(d + step * direction)
        return "break"

    def _context_menu(self, event):
        xy = self._image_xy(event)
        m = tk.Menu(self.canvas, tearoff=0)
        if xy is not None:
            m.add_command(label=f"Move crosshair/reticle here ({xy[0]:.0f}, {xy[1]:.0f})",
                          command=lambda: (setattr(self, "mark", xy), self._mark_changed()))
        m.add_command(label="Center crosshair/reticle", command=self.center_mark)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _drag_start(self, event):
        self._drag = (event.x, event.y)

    def _drag_move(self, event):
        if self._drag is None or self.frozen is None or self.zoom is None:
            return
        dx, dy = event.x - self._drag[0], event.y - self._drag[1]
        self._drag = (event.x, event.y)
        cx, cy = self.center
        self.center = (cx - dx / self.zoom, cy - dy / self.zoom)
        self._dirty = True

    def _view(self, img):
        """(zoom, center x, center y) after clamping to the image."""
        h, w = img.shape
        cw, ch = max(self.canvas.winfo_width(), 10), max(self.canvas.winfo_height(), 10)
        z = self.zoom or self._fit_zoom(img)
        cx, cy = self.center or (w / 2, h / 2)
        half_w, half_h = cw / (2 * z), ch / (2 * z)
        cx = w / 2 if half_w >= w / 2 else min(max(cx, half_w), w - half_w)
        cy = h / 2 if half_h >= h / 2 else min(max(cy, half_h), h - half_h)
        if self.center is not None:
            self.center = (cx, cy)
        return z, cx, cy

    def _contrast(self, img):
        if self.v_auto.get():
            sample = img[::8, ::8]
            lo, hi = np.percentile(sample, (0.5, 99.9))
            if hi <= lo:
                hi = lo + 1
            self.v_lo.set(str(int(lo)))
            self.v_hi.set(str(int(hi)))
            return float(lo), float(hi)
        try:
            lo, hi = float(self.v_lo.get()), float(self.v_hi.get())
        except ValueError:
            lo, hi = 0.0, float(self.full_scale)
        return lo, (hi if hi > lo else lo + 1)

    def _draw(self):
        from PIL import Image, ImageTk
        img = self.frozen.image
        h, w = img.shape
        cv = self.canvas
        cw, ch = max(cv.winfo_width(), 10), max(cv.winfo_height(), 10)
        z, cx, cy = self._view(img)
        x0, x1 = max(0, int(np.floor(cx - cw / (2 * z)))), min(w, int(np.ceil(cx + cw / (2 * z))))
        y0, y1 = max(0, int(np.floor(cy - ch / (2 * z)))), min(h, int(np.ceil(cy + ch / (2 * z))))
        sub = img[y0:y1, x0:x1]
        step = max(1, int(1 / z))  # drop pixels that would not be seen anyway
        if step > 1:
            sub = sub[::step, ::step]
        lo, hi = self._contrast(img)
        u8 = np.clip((sub.astype(np.float32) - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
        if self.v_sat.get():
            sat = sub >= self.full_scale
            if sat.any():
                rgb = np.repeat(u8[:, :, None], 3, axis=2)
                rgb[sat] = (230, 30, 30)
                pil = Image.fromarray(rgb, "RGB")
            else:
                pil = Image.fromarray(u8, "L")
        else:
            pil = Image.fromarray(u8, "L")
        tw, th = max(1, int(round((x1 - x0) * z))), max(1, int(round((y1 - y0) * z)))
        pil = pil.resize((tw, th), Image.NEAREST if z >= 1 else Image.BILINEAR)
        self._photo = ImageTk.PhotoImage(pil)
        ox = cw / 2 + (x0 - cx) * z
        oy = ch / 2 + (y0 - cy) * z
        self._map = (z, cx, cy, cw, ch)
        cv.delete("all")
        cv.create_image(int(round(ox)), int(round(oy)), image=self._photo, anchor="nw")
        mxi, myi = self.mark or (w / 2, h / 2)
        mx = cw / 2 + (mxi - cx) * z
        my = ch / 2 + (myi - cy) * z
        if self.v_cross.get():
            cv.create_line(mx, 0, mx, ch, fill="#ffd400", dash=(6, 4))
            cv.create_line(0, my, cw, my, fill="#ffd400", dash=(6, 4))
        if self.v_reticle.get():
            r = (self._float(self.v_reticle_px.get()) or 0) / 2 * z
            if r > 0:
                cv.create_oval(mx - r, my - r, mx + r, my + r, outline="#ffd400", width=2)
            if not self.v_cross.get():  # small center mark
                cv.create_line(mx - 6, my, mx + 7, my, fill="#ffd400")
                cv.create_line(mx, my - 6, mx, my + 7, fill="#ffd400")
        cv.create_text(8, 8, anchor="nw", fill="#dddddd",
                       text=f"{z * 100:.0f}%" + ("" if self.playing else "   PAUSED"))
        if not self.playing:
            self._readout()

    def _readout(self):
        f = self.frozen
        if f is None:
            return
        img = f.image
        sample = img[::4, ::4]
        peak = int(img.max())
        n_sat = int((sample >= self.full_scale).sum()) * 16
        parts = []
        if self.playing:
            parts.append(f"{self.stream.fps:4.1f} fps")
        parts += [f"frame {f.index}", f"max {peak}/{self.full_scale}"]
        saturated = peak >= self.full_scale
        if saturated:
            parts.append(f"SATURATED (~{n_sat} px)")
        self.v_readout.set("   ".join(parts))
        self.readout_lbl.config(fg=RED if saturated else C.text)

    def _on_motion(self, event):
        if self.frozen is None or not hasattr(self, "_map"):
            return
        z, cx, cy, cw, ch = self._map
        ix = int(cx + (event.x - cw / 2) / z)
        iy = int(cy + (event.y - ch / 2) / z)
        h, w = self.frozen.image.shape
        if 0 <= ix < w and 0 <= iy < h:
            self.v_cursor.set(f"x {ix}  y {iy}  value {int(self.frozen.image[iy, ix])}")
        else:
            self.v_cursor.set("")

    # ----- saving -----
    def choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.save_dir if os.path.isdir(self.save_dir) else None,
                                    title="Folder for camera snapshots")
        if d:
            self.user_dir = self.save_dir = d
            self.dir_lbl.config(text=self._short(d))
            self._save_state()

    def _current_save_dir(self):
        if self.user_dir:
            return self.user_dir
        path, on_m = pick_save_dir(self.dir_candidates)
        return path

    def use_default_dir(self):
        self.user_dir = None
        self.save_dir = self._current_save_dir()
        self.dir_lbl.config(text=self._short(self.save_dir))
        self._save_state()

    def _stage_positions(self):
        st = self.stage
        if st is None:
            return None
        try:
            if hasattr(st, "refresh_positions"):
                st.refresh_positions()
            return {ax.get("label", str(i)): p for i, (ax, p) in enumerate(zip(st.axes, st.positions))}
        except Exception:
            return None

    def snapshot(self):
        f = self.frozen
        if f is None:
            self.v_msg.set("Nothing to save yet: press Play first.")
            return
        from PIL import Image
        self.save_dir = self._current_save_dir()  # the M drive may have come or gone
        self.dir_lbl.config(text=self._short(self.save_dir))
        stamp = datetime.datetime.fromtimestamp(f.time).strftime("%Y%m%d_%H%M%S")
        base = os.path.join(self.save_dir, f"camera_{stamp}")
        n = 1
        while os.path.exists(base + ".png") or os.path.exists(base + "_raw.tif"):
            n += 1
            base = os.path.join(self.save_dir, f"camera_{stamp}_{n}")
        lo, hi = self._contrast(f.image)
        meta = {"time": datetime.datetime.fromtimestamp(f.time).isoformat(timespec="milliseconds"),
                "setup": self.setup, "camera": self.info, "frame": f.index,
                "exposure_ms": self._get("exposure"),
                "gain_db": self._float(self.v_gain.get()),
                "bit_depth": self.bit_depth,
                "piezo_um": self._stage_positions(),
                "crosshair_px": list(self.mark) if self.mark else None,
                "reticle_diameter_px": self._float(self.v_reticle_px.get()) if self.v_reticle.get() else None,
                "display_contrast": {"auto": self.v_auto.get(), "black": lo, "white": hi},
                "files": {"png": "as shown on screen (8-bit, display contrast, no crosshair)",
                          "raw_tif": f"camera counts, 0-{self.full_scale} ({self.bit_depth}-bit) in 16-bit"}}
        try:
            os.makedirs(self.save_dir, exist_ok=True)
            # PNG: the picture as it looks on screen, full resolution. Image
            # viewers show the raw TIFF almost black, because a 10-bit camera
            # only fills the bottom 1/64 of a 16-bit file.
            u8 = np.clip((f.image.astype(np.float32) - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
            Image.fromarray(u8, "L").save(base + ".png")
            # Raw TIFF: the camera's own counts, for analysis.
            img = np.ascontiguousarray(f.image.astype(np.uint16))
            Image.fromarray(img).save(base + "_raw.tif", description=json.dumps(meta))
            with open(base + ".json", "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=1)
            self.v_msg.set(f"Saved {os.path.basename(base)}.png (+ _raw.tif, .json)")
            logger.info("camera snapshot %s.png", base)
        except Exception as e:
            logger.exception("snapshot failed")
            self.v_msg.set(f"Could not save: {e}")

    @staticmethod
    def _time_text(ms):
        """0.04 -> "0.04 ms", 26843.5 -> "26.84 s"."""
        if ms >= 1000:
            return f"{ms / 1000:.2f} s"
        return f"{ms:.3g} ms"

    @staticmethod
    def _short(path, keep=2):
        parts = os.path.normpath(path).split(os.sep)
        return path if len(parts) <= keep + 1 else os.path.join("…", *parts[-keep:])

    @staticmethod
    def _float(s):
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    # ----- remembered settings -----
    def _load_state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        d = {"exposure_ms": self._get("exposure"),
             "gain_db": self._float(self.v_gain.get()),
             "auto_contrast": self.v_auto.get(), "lo": self.v_lo.get(), "hi": self.v_hi.get(),
             "crosshair": self.v_cross.get(), "show_saturated": self.v_sat.get(),
             "reticle": self.v_reticle.get(), "reticle_px": self._float(self.v_reticle_px.get()),
             "mark": list(self.mark) if self.mark else None,
             "user_save_dir": self.user_dir, "auto_exposure": self.v_autoexp.get()}
        d = {k: v for k, v in d.items() if v is not None}
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump(d, fh, indent=1)
        except OSError as e:
            logger.warning("could not save camera settings: %r", e)
