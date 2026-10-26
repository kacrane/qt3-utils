import tkinter as tk
from tkinter import messagebox, filedialog, ttk
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

import nidaqmx

from qt3utils.applications.qt3_daq_busy_marker import santec_daq_busy
from qt3utils.applications.qt3power.calibration import (
    CALIBRATIONS_DIR,
    DEFAULT_CALIBRATION_PATH,
    STATUS_CURRENT,
    STATUS_DEFAULT,
    STATUS_OLDER,
    STATUS_WRONG_OPTICS,
    calibration_gains,
    calibration_optics,
    gain_label,
    calibration_status,
    default_calibration_filename,
    newest_calibration_file,
    optics_label,
    scan_calibration_files,
    CalibrationPoint,
    DetectorCalibration,
    load_calibration,
    MIN_PATH_TRANSMISSION,
    load_last_wavelengths,
    load_last_gains,
    GAIN_OPTIONS_DB,
    load_settings,
    save_calibration,
    save_last_wavelengths,
    update_settings,
)
from qt3utils.applications.qt3power.santec import laser_config as santec_laser_config
from qt3utils.applications.qt3power.santec import read_santec_wavelength
from qt3utils.applications.qt3power.superk import (
    DEFAULT_CONNECTION as SUPERK_DEFAULT_CONNECTION,
    read_superk_settings,
)
from qt3utils.applications.qt3power.paths import (
    DEFAULT_PATH_CONFIG,
    PATH_CONFIG_PATH,
    PathModel,
)

DETECTOR_NAMES = ("PDA50B2", "PDA100A2")

# Each reading is a hardware-timed burst averaged over many samples, so pulsed
# sources (e.g. SuperK CHROMATUNE, 0.15-78 MHz) average out. Sized for the lab's
# NI USB-6343 (500 kS/s aggregate; 2 channels x ~100 kS/s = 200 kS/s): 5000
# samples take 50 ms per reading. The rate is deliberately not a round number:
# a rate that divides the pulse rate would sample every pulse at the same phase
# and bias the average.
DEFAULT_BURST_SAMPLES = 5000
DEFAULT_BURST_RATE_HZ = 99991.0
# Bursts averaged by Read DAQ in the Calibrate window.
CALIBRATION_READ_BURSTS = 10

# Laser read by each detector; its "Read ..." button fills that detector's boxes.
SUPERK_DETECTOR = "PDA100A2"  # NKT SuperK CHROMATUNE
SANTEC_DETECTOR = "PDA50B2"  # Santec TSL lasers via qt3santec's switch
SOURCE_AUTO_INTERVAL_MS = 5000
# Only broadband sources get a linewidth box; the others are narrow lines (linewidth 0).
LINEWIDTH_DETECTORS = (SUPERK_DETECTOR,)

# Detector output limit (V, high-impedance load; halve for 50 ohm). A burst whose
# largest sample reaches SATURATION_FRACTION of this, or of the DAQ range, is
# flagged: the average is then not linear in power.
DETECTOR_MAX_OUTPUT_V = {"PDA50B2": 10.0, "PDA100A2": 10.0}
SATURATION_FRACTION = 0.95

# Calibration status text colors (yellow darkened to stay readable on grey).
# Reading fonts. Double-clicking a power/voltage reading switches it between regular and large.
REGULAR_READING_FONTS = {"power": ("Arial", 18, "bold"), "voltage": ("Arial", 14)}
LARGE_READING_FONT = ("Arial", 40, "bold")
SOURCE_COLUMN_WIDTH = 130  # px; keeps the wavelength boxes aligned between detector boxes
DEFAULT_LARGE_READINGS = {
    "PDA50B2_power": False,
    "PDA50B2_voltage": False,
    "PDA100A2_power": False,
    "PDA100A2_voltage": False,
}
STATUS_COLORS = {
    STATUS_CURRENT: "#008800",
    STATUS_OLDER: "#b08d00",
    STATUS_WRONG_OPTICS: "#cc0000",
    STATUS_DEFAULT: "#d96c00",
}

_STALE_LOCK_TOOLTIP = (
    "Only use this when qt3santec is not scanning but the busy marker was left behind "
    "(e.g. after a crash or force-close). Do not enable during an active Santec sweep—"
    "both apps may touch the same DAQ channels and cause errors. "
    "While reads continue through a sweep, the PDA50B2 wavelength box is not updated, "
    "so its power is converted at the pre-sweep wavelength."
)


def _gain_text(gain_db: Optional[int]) -> str:
    return "" if gain_db is None else f"{gain_db} dB"


def _attach_tooltip(widget: tk.Widget, text: Union[str, Callable[[], str]]) -> None:
    """Show text (or text() if callable) in a borderless Toplevel while the pointer is over widget."""
    tip_window: Dict[str, Optional[tk.Toplevel]] = {"w": None}

    def on_enter(_event: object) -> None:
        if tip_window["w"] is not None:
            return
        x = widget.winfo_rootx() + 20
        y = widget.winfo_rooty() + widget.winfo_height() + 2
        tw = tk.Toplevel(widget)
        tip_window["w"] = tw
        tw.wm_overrideredirect(True)
        tw.attributes("-topmost", True)
        tw.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(
            tw,
            text=text() if callable(text) else text,
            justify="left",
            background="#ffffe0",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=4,
            font=("Arial", 9),
        )
        lbl.pack()

    def on_leave(_event: object) -> None:
        tw = tip_window["w"]
        tip_window["w"] = None
        if tw is not None:
            tw.destroy()

    widget.bind("<Enter>", on_enter)
    widget.bind("<Leave>", on_leave)


class PhotodetectorController:
    """Minimal controller for reading voltage from two photodetectors via NI DAQ."""

    def __init__(
        self,
        device_name: str = "Dev1",
        channel1: str = "ai2",
        channel2: str = "ai0",
        min_voltage: float = -10.0,
        max_voltage: float = 10.0,
        burst_samples: int = DEFAULT_BURST_SAMPLES,
        burst_rate_hz: float = DEFAULT_BURST_RATE_HZ,
    ) -> None:
        """
        Initialize photodetector controller.

        Args:
            device_name: NI DAQ device name (e.g., 'Dev1').
            channel1: Analog input channel for first detector (PDA50B2).
            channel2: Analog input channel for second detector (PDA100A2).
            min_voltage: Minimum expected voltage.
            max_voltage: Maximum expected voltage.
            burst_samples: Samples per channel averaged for each reading (1 = single sample).
            burst_rate_hz: Sample rate per channel for the burst.
        """
        self.device_name = device_name
        self.channel1 = channel1
        self.channel2 = channel2
        self.min_voltage = min_voltage
        self.max_voltage = max_voltage
        self.burst_samples = max(1, int(burst_samples))
        self.burst_rate_hz = float(burst_rate_hz)
        # Largest |V| seen in the last burst, per detector (for saturation checks).
        self.last_peaks: Tuple[float, float] = (0.0, 0.0)

    def read_both_detectors(self) -> Tuple[float, float]:
        """
        Read both detectors over one burst and return the mean voltages.

        Also stores the largest |V| of each channel in last_peaks.

        Returns:
            (voltage1, voltage2) in volts.

        Raises:
            RuntimeError: If there is an error talking to the DAQ.
        """
        try:
            with nidaqmx.Task() as task:
                task.ai_channels.add_ai_voltage_chan(
                    f"{self.device_name}/{self.channel1}",
                    min_val=self.min_voltage,
                    max_val=self.max_voltage,
                )
                task.ai_channels.add_ai_voltage_chan(
                    f"{self.device_name}/{self.channel2}",
                    min_val=self.min_voltage,
                    max_val=self.max_voltage,
                )
                n = self.burst_samples
                if n > 1:
                    task.timing.cfg_samp_clk_timing(
                        rate=self.burst_rate_hz,
                        sample_mode=nidaqmx.constants.AcquisitionType.FINITE,
                        samps_per_chan=n,
                    )
                data = task.read(number_of_samples_per_channel=n, timeout=n / self.burst_rate_hz + 2.0)

            if isinstance(data, (list, tuple)) and len(data) >= 2:
                s1 = [float(x) for x in (data[0] if hasattr(data[0], "__len__") else [data[0]])]
                s2 = [float(x) for x in (data[1] if hasattr(data[1], "__len__") else [data[1]])]
                self.last_peaks = (max(abs(x) for x in s1), max(abs(x) for x in s2))
                return sum(s1) / len(s1), sum(s2) / len(s2)

            raise RuntimeError(f"Unexpected data format from DAQ: {type(data)}")
        except Exception as e:
            raise RuntimeError(f"Error reading detectors: {e}")


class PowerMonitorApp:
    """
    Simple GUI that shows power in mW for two photodetectors read via NI DAQ.
    Displayed power is the power at the objective: the detectors read a sampled
    beam, and the swappable optics after the sampler are applied via paths.py.

    The conversion is wavelength dependent (see calibration.py):
        power_mW = (voltage_V - offset_V) / k(wavelength)
    By default k = 7.0 V/mW (0.7 V -> 0.1 mW) at a reference wavelength
    (1550 nm PDA50B2, 980 nm PDA100A2), scaled by the
    detector's responsivity curve at other wavelengths. A measured calibration,
    if loaded, overrides the default. The gain dropdowns record each detector's gain
    switch (saved with calibration points); they do not change the conversion.
    """

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("QT3 Power Monitor")
        self.root.minsize(500, 250)

        self.detector_ctrl: Optional[PhotodetectorController] = None
        self.running = False
        self.update_interval_ms = 200  # ~5 Hz polling
        self.var_ignore_santec_lock = tk.BooleanVar(value=False)

        self.wavelengths: Dict[str, float] = load_last_wavelengths()
        self.wavelength_entries: Dict[str, tk.Entry] = {}
        saved_gains = load_last_gains()
        self.gain_vars: Dict[str, tk.StringVar] = {
            name: tk.StringVar(value=_gain_text(saved_gains.get(name))) for name in DETECTOR_NAMES
        }
        for name, var in self.gain_vars.items():
            var.trace_add("write", lambda *_a: self._on_gain_change())
        saved_lw = load_settings().get("linewidth_nm", {})
        self.linewidths: Dict[str, float] = {
            name: float(saved_lw.get(name, 0.0))
            if isinstance(saved_lw, dict) and name in LINEWIDTH_DETECTORS
            else 0.0
            for name in DETECTOR_NAMES
        }
        self.linewidth_entries: Dict[str, tk.Entry] = {}
        saved_superk = load_settings().get("superk", {})
        self.superk_connection = dict(SUPERK_DEFAULT_CONNECTION)
        if isinstance(saved_superk, dict):
            self.superk_connection.update(saved_superk)
        self.source_controls: Dict[str, "LaserSourceControls"] = {}
        self.detectors: Dict[str, DetectorCalibration] = {
            name: DetectorCalibration(name) for name in DETECTOR_NAMES
        }
        # Each detector has its own calibration file, loaded and saved independently.
        self.calibration_path: Dict[str, Optional[Path]] = {name: None for name in DETECTOR_NAMES}
        self.calibration_dirty: Dict[str, bool] = {name: False for name in DETECTOR_NAMES}  # unsaved points
        self.cal_status_labels: Dict[str, tk.Label] = {}
        self.calibration_window: Optional["CalibrationWindow"] = None
        self.last_voltages: Optional[Tuple[float, float]] = None

        path_config_error = None
        try:
            self.path_model = PathModel()
        except Exception as e:
            path_config_error = e
            self.path_model = PathModel(DEFAULT_PATH_CONFIG)
        self.path_model.set_installed(load_settings().get("path", {}))
        self.path_vars: Dict[str, tk.StringVar] = {}
        self._wire_detectors()

        self._build_gui()

        if path_config_error is not None:
            messagebox.showwarning(
                "Optics path",
                f"Could not read {PATH_CONFIG_PATH}:\n{path_config_error}\nUsing the built-in path.",
            )

        self._load_newest_calibration_for_installed_optics()
        self._refresh_conversion_display()

    def _build_gui(self) -> None:
        main_frame = tk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Configuration: DAQ, optics to objective, calibration
        cfg_frame = tk.LabelFrame(main_frame, text="Configuration")
        cfg_frame.pack(fill="x", pady=5)

        daq_row = tk.Frame(cfg_frame)
        daq_row.grid(row=0, column=0, sticky="w")
        tk.Label(daq_row, text="DAQ Device:").pack(side="left")
        self.ent_device = tk.Entry(daq_row, width=5)
        self.ent_device.pack(side="left", padx=(2, 10))
        self.ent_device.insert(0, "Dev1")

        channel_entries = {}
        for name, default_ch in zip(DETECTOR_NAMES, ("ai2", "ai0")):
            tk.Label(daq_row, text=f"{name} Ch:").pack(side="left")
            ent = tk.Entry(daq_row, width=4)
            ent.pack(side="left", padx=(2, 4))
            ent.insert(0, default_ch)
            channel_entries[name] = ent
            tk.Label(daq_row, text="Gain:").pack(side="left")
            self._gain_box(daq_row, name).pack(side="left", padx=(2, 10))
        self.ent_ch1 = channel_entries["PDA50B2"]
        self.ent_ch2 = channel_entries["PDA100A2"]

        self.btn_init = tk.Button(
            daq_row,
            text="Initialize DAQ",
            command=self.init_daq,
            bg="#dddddd",
        )
        self.btn_init.pack(side="left")

        self.lbl_status = tk.Label(daq_row, text="DAQ: Not initialized", fg="red")
        self.lbl_status.pack(side="left", padx=8)

        # Optics between the beam sampler and the objective, one dropdown per slot
        optics_row = tk.Frame(cfg_frame)
        optics_row.grid(row=1, column=0, sticky="w")
        for slot, spec in self.path_model.slots.items():
            tk.Label(optics_row, text=f"{slot}:").pack(side="left", padx=(0, 3))
            var = tk.StringVar(value=self.path_model.installed[slot])
            box = ttk.Combobox(optics_row, textvariable=var, values=spec["options"], state="readonly", width=12)
            box.pack(side="left", padx=(0, 12))
            box.bind("<<ComboboxSelected>>", lambda _e, s=slot: self.set_installed_optic(s))
            self.path_vars[slot] = var
        tk.Button(optics_row, text="Calibrate...", command=self.open_calibration_window).pack(side="left", padx=2)

        # One calibration file per detector: its own Load/Save/Default buttons and status.
        cal_frame = tk.Frame(cfg_frame)
        cal_frame.grid(row=2, column=0, sticky="w")
        for r, name in enumerate(DETECTOR_NAMES):
            tk.Label(cal_frame, text=f"{name} cal:").grid(row=r, column=0, sticky="w")
            for c, (text, cmd) in enumerate((
                ("Load...", self.load_calibration_dialog),
                ("Save...", self.save_calibration_dialog),
                ("Default", self.clear_calibration),
            )):
                tk.Button(cal_frame, text=text, command=lambda f=cmd, n=name: f(n)).grid(
                    row=r, column=1 + c, padx=2, pady=1
                )
            lbl = tk.Label(cal_frame, text="", anchor="w", justify="left", wraplength=520, font=("Arial", 10))
            lbl.grid(row=r, column=4, sticky="w", padx=6)
            self.cal_status_labels[name] = lbl

        for child in cfg_frame.winfo_children():
            child.grid_configure(padx=3, pady=3)

        # Power display: one labeled subsection per detector
        self.reading_labels: Dict[str, tk.Label] = {}
        sat_labels: Dict[str, tk.Label] = {}
        saved_large = load_settings().get("large_readings", {})
        self.large_readings: Dict[str, bool] = {
            key: bool(saved_large.get(key, default)) for key, default in DEFAULT_LARGE_READINGS.items()
        }
        for name, source, title, color, prepare, read, settings in (
            ("PDA50B2", SANTEC_DETECTOR, "Santec", "#0044aa",
             self._prepare_santec_read, self._read_santec, None),
            ("PDA100A2", SUPERK_DETECTOR, "SuperK", "#006600",
             lambda: dict(self.superk_connection), self._read_superk, self.open_superk_settings),
        ):
            frame = tk.LabelFrame(main_frame, text=f"{name} ({title})", font=("Arial", 10, "bold"))
            frame.pack(fill="x", pady=5)

            tk.Label(frame, text="Excitation Power (mW):", font=("Arial", 11, "bold")).grid(
                row=0, column=0, sticky="e"
            )
            power = tk.Label(frame, text="--", fg=color)
            power.grid(row=0, column=1, sticky="w")
            tk.Label(frame, text="Voltage (V):").grid(row=1, column=0, sticky="e")
            voltage = tk.Label(frame, text="--")
            voltage.grid(row=1, column=1, sticky="w")
            sat = tk.Label(frame, text="", fg="#cc0000", font=("Arial", 10, "bold"))
            sat.grid(row=1, column=2, sticky="w")
            sat_labels[name] = sat

            self._build_wavelength_entry(frame, name, row=0)
            self.source_controls[source] = LaserSourceControls(
                self, frame, row=0, detector=source, title=title,
                prepare=prepare, read=read, open_settings=settings,
            )
            for child in frame.winfo_children():
                child.grid_configure(padx=5, pady=3)
            frame.grid_columnconfigure(1, weight=1)
            frame.grid_columnconfigure(5, minsize=SOURCE_COLUMN_WIDTH)

            for kind, lbl in (("power", power), ("voltage", voltage)):
                key = f"{name}_{kind}"
                self.reading_labels[key] = lbl
                self._apply_reading_size(key)
                lbl.bind("<Double-Button-1>", lambda _e, k=key: self.toggle_reading_size(k))
                _attach_tooltip(lbl, lambda n=name: self._conversion_text(n) + "\n\nDouble-click to resize.")

        self.lbl_pda50b2_power = self.reading_labels["PDA50B2_power"]
        self.lbl_pda50b2_voltage = self.reading_labels["PDA50B2_voltage"]
        self.lbl_pda100a2_power = self.reading_labels["PDA100A2_power"]
        self.lbl_pda100a2_voltage = self.reading_labels["PDA100A2_voltage"]
        self.lbl_pda50b2_sat = sat_labels["PDA50B2"]
        self.lbl_pda100a2_sat = sat_labels["PDA100A2"]

        # Controls
        ctrl_frame = tk.Frame(main_frame)
        ctrl_frame.pack(fill="x", pady=5)

        self.btn_start = tk.Button(
            ctrl_frame,
            text="Start",
            width=10,
            command=self.start_monitoring,
            bg="green",
            fg="white",
        )
        self.btn_start.pack(side="left", padx=5)

        self.btn_stop = tk.Button(
            ctrl_frame,
            text="Stop",
            width=10,
            command=self.stop_monitoring,
            state="disabled",
            bg="red",
            fg="white",
        )
        self.btn_stop.pack(side="left", padx=5)

        self.lbl_run_status = tk.Label(ctrl_frame, text="Monitor: Stopped")
        self.lbl_run_status.pack(side="left", padx=15)

        stale_lock_label = "Stale lock: allow DAQ reads anyway (unsafe if Santec is scanning)"
        self.chk_ignore_lock = tk.Checkbutton(
            ctrl_frame,
            text=stale_lock_label,
            variable=self.var_ignore_santec_lock,
        )
        self.chk_ignore_lock.pack(side="left", padx=10)
        _attach_tooltip(self.chk_ignore_lock, _STALE_LOCK_TOOLTIP)

    def init_daq(self) -> None:
        """Initialize the DAQ and test read once."""
        device = self.ent_device.get().strip()
        ch1 = self.ent_ch1.get().strip()
        ch2 = self.ent_ch2.get().strip()

        if not device or not ch1 or not ch2:
            messagebox.showerror("Error", "Please specify DAQ device and both channels.")
            return

        if santec_daq_busy() and not self.var_ignore_santec_lock.get():
            messagebox.showwarning(
                "Santec scanning",
                "qt3santec has the DAQ (scan in progress). Stop the scan or enable "
                "'Stale lock: allow DAQ reads anyway (unsafe if Santec is scanning)' "
                "only if you are sure the marker is stale—not during a real sweep.",
            )
            return

        try:
            # Test connection by reading once.
            test_ctrl = PhotodetectorController(device, ch1, ch2)
            v1, v2 = test_ctrl.read_both_detectors()

            # If we get here, DAQ is working; keep a controller instance.
            self.detector_ctrl = test_ctrl
            self.lbl_status.config(
                text="DAQ: Initialized",
                fg="green",
            )
            self._show_readings(v1, v2)

            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
            self.lbl_run_status.config(text="Monitor: Ready")
        except Exception as e:
            self.detector_ctrl = None
            self.lbl_status.config(text="DAQ: Error", fg="red")
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
            messagebox.showerror("DAQ Error", str(e))

    def saturated_detectors(self) -> List[str]:
        """Detectors whose last burst came within SATURATION_FRACTION of their output or DAQ limit."""
        if self.detector_ctrl is None:
            return []
        ctrl = self.detector_ctrl
        out = []
        for name, peak in zip(DETECTOR_NAMES, ctrl.last_peaks):
            limit = min(DETECTOR_MAX_OUTPUT_V[name], ctrl.max_voltage)
            if peak >= SATURATION_FRACTION * limit:
                out.append(name)
        return out

    def _show_readings(self, v1: float, v2: float) -> None:
        saturated = self.saturated_detectors()
        self.lbl_pda50b2_sat.config(text="SATURATED" if "PDA50B2" in saturated else "")
        self.lbl_pda100a2_sat.config(text="SATURATED" if "PDA100A2" in saturated else "")
        self.last_voltages = (v1, v2)
        self.lbl_pda50b2_voltage.config(text=f"{v1:.4f}")
        self.lbl_pda100a2_voltage.config(text=f"{v2:.4f}")
        self.lbl_pda50b2_power.config(text=f"{self._voltage_to_mw('PDA50B2', v1):.4f}")
        self.lbl_pda100a2_power.config(text=f"{self._voltage_to_mw('PDA100A2', v2):.4f}")

    def _voltage_to_mw(self, detector: str, voltage: float) -> float:
        """Convert voltage (V) to power at the objective (mW) at the current wavelength and optics."""
        wl = self.wavelengths[detector]
        lw = self.linewidths[detector]
        t_path = self.path_model.transmission(detector, wl, linewidth_nm=lw)
        return self.detectors[detector].voltage_to_mw(voltage, wl, t_path, lw)

    def _wire_detectors(self) -> None:
        """Let each detector's calibration look up the path transmission of its recorded points."""
        for name, det in self.detectors.items():
            # Points without a recorded path predate path support: treat as no optics.
            det.point_transmission = lambda p, n=name: (
                self.path_model.transmission(n, p.wavelength_nm, p.path, p.linewidth_nm) if p.path else 1.0
            )

    def set_installed_optic(self, slot: str) -> None:
        self.path_model.installed[slot] = self.path_vars[slot].get()
        try:
            update_settings({"path": self.path_model.installed})
        except OSError:
            pass  # remembering the optics is a convenience; don't block on it
        self._load_newest_calibration_for_installed_optics()
        self._refresh_conversion_display()
        if self.calibration_window is not None:
            self.calibration_window.refresh()

    # ---- Reading size (double-click toggles; large is for reading across the room while aligning) ----

    def _apply_reading_size(self, key: str) -> None:
        kind = key.rsplit("_", 1)[1]
        font = LARGE_READING_FONT if self.large_readings[key] else REGULAR_READING_FONTS[kind]
        self.reading_labels[key].config(font=font)

    def toggle_reading_size(self, key: str) -> None:
        self.large_readings[key] = not self.large_readings[key]
        self._apply_reading_size(key)
        update_settings({"large_readings": self.large_readings})

    # ---- Detector gain (recorded with calibration points) ----

    def _gain_box(self, parent: tk.Widget, name: str) -> ttk.Combobox:
        box = ttk.Combobox(
            parent, textvariable=self.gain_vars[name], state="readonly", width=6,
            values=[_gain_text(g) for g in GAIN_OPTIONS_DB[name]],
        )
        _attach_tooltip(box, f"Position of the {name} gain switch. Saved with calibration points.")
        return box

    def gain_db(self, name: str) -> Optional[int]:
        text = self.gain_vars[name].get().replace("dB", "").strip()
        return int(text) if text else None

    def _on_gain_change(self) -> None:
        try:
            update_settings({"gain_db": {name: self.gain_db(name) for name in DETECTOR_NAMES}})
        except OSError:
            pass
        if not getattr(self, "cal_status_labels", None):
            return  # still building the GUI
        # Like the dichroic: load the newest calibration matching the new gain, if any.
        self._load_newest_calibration_for_installed_optics()
        self._refresh_conversion_display()
        if self.calibration_window is not None:
            self.calibration_window.refresh()

    # ---- Wavelength / calibration ----

    def _build_wavelength_entry(self, parent: tk.Widget, name: str, row: int) -> None:
        tk.Label(parent, text="Wavelength (nm):").grid(row=row, column=3, sticky="e", padx=5)
        ent = tk.Entry(parent, width=8)
        ent.grid(row=row, column=4, sticky="w", padx=5)
        ent.insert(0, f"{self.wavelengths[name]:g}")
        ent.bind("<Return>", lambda _e: self.set_wavelength(name))
        ent.bind("<FocusOut>", lambda _e: self.set_wavelength(name))
        self.wavelength_entries[name] = ent

        if name not in LINEWIDTH_DETECTORS:
            return
        tk.Label(parent, text="Linewidth (nm):").grid(row=row + 1, column=3, sticky="e", padx=5)
        lw = tk.Entry(parent, width=8)
        lw.grid(row=row + 1, column=4, sticky="w", padx=5)
        lw.insert(0, f"{self.linewidths[name]:g}")
        lw.bind("<Return>", lambda _e: self.set_linewidth(name))
        lw.bind("<FocusOut>", lambda _e: self.set_linewidth(name))
        self.linewidth_entries[name] = lw
        _attach_tooltip(lw, "Source spectral width (flat top). 0 for a narrow line.")

    # ---- Laser sources (read-only) ----
    # prepare() runs on the main thread and returns the argument for read(), which
    # runs in a background thread (no Tk calls) and returns a SourceReading.

    @staticmethod
    def _read_superk(conn: dict) -> "SourceReading":
        r = read_superk_settings(conn)
        rep = f"{r.rep_rate_mhz:.3g} MHz" if r.rep_rate_mhz else "rep rate n/a"
        emission = {True: "on", False: "off", None: "?"}[r.emission_on]
        return SourceReading(
            wavelength_nm=r.center_nm,
            linewidth_nm=r.bandwidth_nm,
            status=f"{rep}, emission {emission}",
            detail=f"SuperK (last read): {r.center_nm:g} nm, {r.bandwidth_nm:g} nm wide, rep {rep}",
        )

    def _prepare_santec_read(self) -> list:
        if santec_daq_busy() and not self.var_ignore_santec_lock.get():
            raise SourceBusy("qt3santec is sweeping; the wavelength is changing. Try again after the sweep.")
        return santec_laser_config()

    @staticmethod
    def _read_santec(configs: list) -> "SourceReading":
        r = read_santec_wavelength(configs)
        sweeping = ", SWEEPING" if r.sweeping else ""
        return SourceReading(
            wavelength_nm=r.wavelength_nm,
            linewidth_nm=None,  # tunable CW laser: narrow line, no linewidth box
            status=f"{r.ip}{sweeping}",
            detail=f"Santec (last read): {r.wavelength_nm:g} nm from {r.ip}{sweeping}",
        )

    def apply_source_reading(self, name: str, reading: "SourceReading") -> None:
        for entry, value, setter in (
            (self.wavelength_entries[name], reading.wavelength_nm, self.set_wavelength),
            (self.linewidth_entries.get(name), reading.linewidth_nm, self.set_linewidth),
        ):
            if value is None or entry is None:
                continue
            entry.delete(0, tk.END)
            entry.insert(0, f"{value:g}")
            setter(name)
        if self.calibration_window is not None:
            self.calibration_window.refresh()

    def open_superk_settings(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("SuperK connection")
        entries = {}
        fields = (
            ("host_ip", "This PC's IP"),
            ("host_port", "Host port"),
            ("laser_ip", "Laser IP"),
            ("laser_port", "Laser port"),
            ("timeout_ms", "Timeout (ms)"),
        )
        for i, (key, label) in enumerate(fields):
            tk.Label(win, text=f"{label}:").grid(row=i, column=0, sticky="e", padx=5, pady=2)
            ent = tk.Entry(win, width=16)
            ent.grid(row=i, column=1, sticky="w", padx=5, pady=2)
            ent.insert(0, str(self.superk_connection[key]))
            entries[key] = ent
        tk.Label(
            win,
            text="Read-only: qt3power never writes to the laser.\n"
            "Close NKT CONTROL's Ethernet connection first.",
            fg="#555555",
            justify="left",
        ).grid(row=len(fields), column=0, columnspan=2, sticky="w", padx=5, pady=4)

        def save() -> None:
            try:
                new = {
                    "host_ip": entries["host_ip"].get().strip(),
                    "host_port": int(entries["host_port"].get()),
                    "laser_ip": entries["laser_ip"].get().strip(),
                    "laser_port": int(entries["laser_port"].get()),
                    "timeout_ms": int(entries["timeout_ms"].get()),
                }
            except ValueError:
                messagebox.showerror("SuperK connection", "Ports and timeout must be integers.", parent=win)
                return
            self.superk_connection = new
            try:
                update_settings({"superk": new})
            except OSError:
                pass
            win.destroy()

        tk.Button(win, text="Save", command=save).grid(row=len(fields) + 1, column=1, sticky="e", padx=5, pady=5)

    def set_linewidth(self, name: str) -> None:
        ent = self.linewidth_entries[name]
        text = ent.get().strip()
        try:
            lw = float(text)
            if lw < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Linewidth", f"Invalid {name} linewidth: {text!r}")
            ent.delete(0, tk.END)
            ent.insert(0, f"{self.linewidths[name]:g}")
            return
        if lw == self.linewidths[name]:
            return
        self.linewidths[name] = lw
        try:
            update_settings({"linewidth_nm": self.linewidths})
        except OSError:
            pass  # remembering the linewidth is a convenience; don't block on it
        self._refresh_conversion_display()

    def set_wavelength(self, name: str) -> None:
        ent = self.wavelength_entries[name]
        text = ent.get().strip()
        try:
            wl = float(text)
            if wl <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Wavelength", f"Invalid {name} wavelength: {text!r}")
            ent.delete(0, tk.END)
            ent.insert(0, f"{self.wavelengths[name]:g}")
            return
        if wl == self.wavelengths[name]:
            return
        self.wavelengths[name] = wl
        try:
            save_last_wavelengths(self.wavelengths)
        except OSError:
            pass  # remembering the wavelength is a convenience; don't block on it
        self._refresh_conversion_display()

    def _conversion_text(self, name: str) -> str:
        """Tooltip text describing how this detector's voltage becomes power."""
        det = self.detectors[name]
        wl = self.wavelengths[name]
        lw = self.linewidths[name]
        k, offset = det.factor_and_offset(wl)
        k *= det.band_factor(wl, lw)
        t_path = self.path_model.transmission(name, wl, linewidth_nm=lw)
        band = f" (linewidth {lw:g} nm)" if lw > 0 else ""
        lines = [f"{name} at {wl:g} nm{band}, power at objective"]
        if t_path > 0:
            lines.append(f"Overall: {k / t_path:.4g} V/mW")
        lines.append(f"Detector: {k:.4g} V/mW")
        if offset:
            lines.append(f"Offset: {offset * 1000:+.2f} mV")
        lines.append(f"Optics to objective: {t_path * 100:.1f}%")
        lines.extend("  " + line for line in self.path_model.describe(name, wl, linewidth_nm=lw))
        if t_path < 0.05:
            lines.append("Note: almost no light reaches the objective on this path at this wavelength")
        if det.is_calibrated:
            lo, hi = det.calibrated_range()
            lines.append(f"Source: calibration, {len(det.points)} pts, {lo:g}-{hi:g} nm")
            cal_paths = {self.path_model.label(p.path) for p in det.points if p.path}
            if cal_paths and cal_paths != {self.path_model.label()}:
                lines.append(f"Calibrated with {'; '.join(sorted(cal_paths))}, rescaled to current optics")
            if not lo <= wl <= hi:
                lines.append("Note: outside calibrated range, extrapolated with Thorlabs curve shape")
        elif det.curve is not None:
            lines.append(f"Source: Thorlabs {name} curve, R = {det.responsivity(wl):.3f} A/W")
            lines.append(f"({det.ref_factor_v_per_mw:g} V/mW at {det.ref_wavelength_nm:g} nm, scaled by responsivity)")
            if not det.in_curve_range(wl):
                lo, hi = det.curve[0][0], det.curve[-1][0]
                lines.append(f"Note: outside curve range {lo:g}-{hi:g} nm, using the end value")
        else:
            lines.append("Source: fixed factor, no responsivity curve")
        if self.calibration_path[name] is not None:
            lines.append(f"File: {self.calibration_path[name]}")
        source = self.source_controls.get(name)
        if source is not None and source.last is not None:
            lines.append(source.last.detail)
        if self.detector_ctrl is not None:
            ctrl = self.detector_ctrl
            peak = ctrl.last_peaks[DETECTOR_NAMES.index(name)]
            lines.append(
                f"Averaging: {ctrl.burst_samples} samples at {ctrl.burst_rate_hz:g} Hz, peak |V| {peak:.3f} V"
            )
            if name in self.saturated_detectors():
                limit = min(DETECTOR_MAX_OUTPUT_V[name], ctrl.max_voltage)
                lines.append(
                    f"SATURATED: peak is within {(1 - SATURATION_FRACTION) * 100:.0f}% of {limit:g} V. "
                    "Lower the detector gain or the power."
                )
        return "\n".join(lines)

    def _refresh_conversion_display(self) -> None:
        library = self._calibration_library()
        for name in DETECTOR_NAMES:
            status, text = calibration_status(
                self.detectors[name],
                self.calibration_path[name],
                self.calibration_dirty[name],
                self.path_model.installed,
                self.gain_db(name),
                library,
            )
            self.cal_status_labels[name].config(text=text, fg=STATUS_COLORS[status])
        if self.last_voltages is not None:
            self._show_readings(*self.last_voltages)

    def _apply_loaded_calibration(self, name: str, det: DetectorCalibration, path: Optional[Path]) -> None:
        self.detectors[name] = det
        self.calibration_path[name] = path
        self.calibration_dirty[name] = False
        self._wire_detectors()

    def _confirm_discard_unsaved(self, name: str, action: str) -> bool:
        if not self.calibration_dirty[name]:
            return True
        return messagebox.askyesno(
            "Unsaved calibration",
            f"You have unsaved {name} calibration points. {action} anyway and discard them?",
        )

    def _calibration_library(self):
        """Saved calibration files the status compares against, newest first."""
        folders = [CALIBRATIONS_DIR, DEFAULT_CALIBRATION_PATH.parent]
        folders += [p.parent for p in self.calibration_path.values() if p is not None]
        return scan_calibration_files(folders)

    def _load_newest_calibration_for_installed_optics(self) -> None:
        """For each detector, load the newest saved calibration measured with the installed optics.

        Runs at startup and when the optics change. A detector with unsaved points, or with no
        file for these optics, keeps its current calibration (rescaled to the new optics).
        """
        library = self._calibration_library()
        for name in DETECTOR_NAMES:
            if self.calibration_dirty[name]:
                continue
            info = newest_calibration_file(library, name, self.path_model.installed, self.gain_db(name))
            if info is None:
                continue
            try:
                self._apply_loaded_calibration(name, load_calibration(info.path, name), info.path)
            except Exception as e:
                messagebox.showwarning(
                    "Calibration",
                    f"Could not load {info.path}:\n{e}\nUsing the default {name} curve.",
                )

    def load_calibration_dialog(self, name: str) -> None:
        if not self._confirm_discard_unsaved(name, "Load a calibration"):
            return
        path = filedialog.askopenfilename(
            title=f"Load {name} calibration",
            initialdir=str(CALIBRATIONS_DIR if CALIBRATIONS_DIR.is_dir() else DEFAULT_CALIBRATION_PATH.parent),
            filetypes=[(f"{name} calibration", f"*{name}*.json"), ("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            det = load_calibration(path, name)
        except Exception as e:
            messagebox.showerror("Calibration", f"Could not load {path}:\n{e}")
            return
        self._apply_loaded_calibration(name, det, Path(path))
        self._refresh_conversion_display()
        if self.calibration_window is not None:
            self.calibration_window.refresh()

    def save_calibration_dialog(self, name: str) -> bool:
        """Save one detector's calibration to its own file. Returns True if saved."""
        if not self.detectors[name].points:
            messagebox.showinfo("Save calibration", f"{name} has no calibration points to save.")
            return False
        CALIBRATIONS_DIR.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(
            title=f"Save {name} calibration",
            initialdir=str(CALIBRATIONS_DIR),
            initialfile=default_calibration_filename(
                name, self.path_model.installed, (calibration_gains(self.detectors[name]) or [None])[0]
            ),
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return False
        try:
            save_calibration(path, self.detectors[name])
        except Exception as e:
            messagebox.showerror("Calibration", f"Could not save {path}:\n{e}")
            return False
        self.calibration_path[name] = Path(path)
        self.calibration_dirty[name] = False
        self._refresh_conversion_display()
        if Path(path).expanduser().resolve().parent not in (
            CALIBRATIONS_DIR.resolve(),
            DEFAULT_CALIBRATION_PATH.parent.resolve(),
        ):
            messagebox.showinfo(
                "Calibration saved",
                f"Saved to {path}.\nOnly files in {CALIBRATIONS_DIR} are picked up automatically "
                "at startup and compared when checking for the newest calibration.",
            )
        return True

    def clear_calibration(self, name: str) -> None:
        if self.detectors[name].is_calibrated:
            if not messagebox.askyesno(
                "Use default curve",
                f"Discard the {name} calibration in memory and use the default responsivity curve?\n"
                "(Saved files are not deleted.)",
            ):
                return
        self._apply_loaded_calibration(name, DetectorCalibration(name), None)
        self._refresh_conversion_display()
        if self.calibration_window is not None:
            self.calibration_window.refresh()

    def open_calibration_window(self) -> None:
        if self.calibration_window is not None:
            self.calibration_window.win.lift()
            return
        self.calibration_window = CalibrationWindow(self)

    def read_average_voltages(self, n_samples: int) -> Tuple[float, float]:
        """Average n_samples burst reads of both detectors. Raises RuntimeError if the DAQ is unavailable.

        Afterwards, detector_ctrl.last_peaks holds the largest |V| over all bursts.
        """
        if self.detector_ctrl is None:
            raise RuntimeError("Initialize DAQ first.")
        if santec_daq_busy() and not self.var_ignore_santec_lock.get():
            raise RuntimeError("qt3santec has the DAQ (scan in progress).")
        v1s, v2s = [], []
        peak1 = peak2 = 0.0
        for _ in range(max(1, n_samples)):
            v1, v2 = self.detector_ctrl.read_both_detectors()
            v1s.append(v1)
            v2s.append(v2)
            peak1 = max(peak1, self.detector_ctrl.last_peaks[0])
            peak2 = max(peak2, self.detector_ctrl.last_peaks[1])
        self.detector_ctrl.last_peaks = (peak1, peak2)
        return sum(v1s) / len(v1s), sum(v2s) / len(v2s)

    def _set_power_widgets_paused(self, paused: bool) -> None:
        if paused:
            self.lbl_pda50b2_power.config(fg="#888888")
            self.lbl_pda100a2_power.config(fg="#888888")
            self.lbl_pda50b2_voltage.config(fg="#888888")
            self.lbl_pda100a2_voltage.config(fg="#888888")
        else:
            self.lbl_pda50b2_power.config(fg="#0044aa")
            self.lbl_pda100a2_power.config(fg="#006600")
            self.lbl_pda50b2_voltage.config(fg="black")
            self.lbl_pda100a2_voltage.config(fg="black")

    def start_monitoring(self) -> None:
        if self.running:
            return
        if self.detector_ctrl is None:
            # Start initializes the DAQ itself; init_daq shows any error (or the Santec-busy warning).
            self.init_daq()
            if self.detector_ctrl is None:
                return

        self.running = True
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.lbl_run_status.config(text="Monitor: Running")
        self._schedule_next_update()

    def stop_monitoring(self) -> None:
        self.running = False
        self._set_power_widgets_paused(False)
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.lbl_run_status.config(text="Monitor: Stopped")

    def _schedule_next_update(self) -> None:
        if self.running:
            self.root.after(self.update_interval_ms, self._update_readings)

    def _update_readings(self) -> None:
        if not self.running or self.detector_ctrl is None:
            return

        try:
            if santec_daq_busy() and not self.var_ignore_santec_lock.get():
                self.lbl_run_status.config(
                    text="Monitor: Running — paused (Santec scan, DAQ shared)",
                )
                self._set_power_widgets_paused(True)
            else:
                self.lbl_run_status.config(text="Monitor: Running")
                self._set_power_widgets_paused(False)
                v1, v2 = self.detector_ctrl.read_both_detectors()
                self._show_readings(v1, v2)

            self.lbl_status.config(fg="green")
        except Exception as e:
            # Stop monitoring on error to avoid spamming the DAQ.
            self.running = False
            self.detector_ctrl = None  # next Start re-initializes the DAQ
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
            self.lbl_run_status.config(text="Monitor: Error")
            self.lbl_status.config(text=f"DAQ: Error ({e})", fg="red")
        finally:
            self._schedule_next_update()

    def on_closing(self) -> None:
        """Cleanly stop monitoring and close the window."""
        for name in DETECTOR_NAMES:
            if not self.calibration_dirty[name]:
                continue
            answer = messagebox.askyesnocancel(
                "Unsaved calibration",
                f"You have unsaved {name} calibration points. Save them before closing?",
            )
            if answer is None:
                return
            if answer and not self.save_calibration_dialog(name):
                return
        self.running = False
        for source in self.source_controls.values():
            source.var_auto.set(False)
        # No persistent DAQ task is kept open in this minimal app, so nothing to close.
        self.root.destroy()


class SourceBusy(RuntimeError):
    """The source can't be read right now but will be later (e.g. during a sweep)."""


@dataclass
class SourceReading:
    wavelength_nm: float
    linewidth_nm: Optional[float]  # None leaves the linewidth box alone
    status: str  # short text under the button
    detail: str  # line for the power tooltip


class LaserSourceControls:
    """'Read <laser>' button, '...' settings, Auto checkbox and status line for one detector."""

    def __init__(
        self,
        app: "PowerMonitorApp",
        parent: tk.Widget,
        row: int,
        detector: str,
        title: str,
        prepare: Callable[[], object],
        read: Callable[[object], SourceReading],
        open_settings: Optional[Callable[[], None]] = None,
    ) -> None:
        self.app = app
        self.detector = detector
        self.title = title
        self.prepare = prepare
        self.read_fn = read
        self.busy = False
        self.last: Optional[SourceReading] = None
        self.var_auto = tk.BooleanVar(value=False)

        frm = tk.Frame(parent)
        frm.grid(row=row, column=5, rowspan=2, sticky="nw", padx=5)
        tk.Button(frm, text=f"Read {title}", command=lambda: self.read(manual=True)).pack(anchor="w")
        row2 = tk.Frame(frm)
        row2.pack(anchor="w")
        tk.Checkbutton(row2, text="Auto", variable=self.var_auto, command=self._on_auto_toggle).pack(side="left")
        if open_settings is not None:
            tk.Button(row2, text="...", width=2, command=open_settings).pack(side="left", padx=2)
        self.lbl = tk.Label(frm, text="", fg="#555555", font=("Arial", 8), justify="left")
        self.lbl.pack(anchor="w")

    def read(self, manual: bool = False) -> None:
        if self.busy:
            return
        try:
            arg = self.prepare()
        except SourceBusy as e:
            # Temporary (e.g. a sweep): Auto just waits for the next tick.
            self.lbl.config(text=f"Waiting: sweep running\n{time.strftime('%H:%M:%S')}")
            if manual:
                messagebox.showinfo(f"Read {self.title}", str(e))
            return
        except Exception as e:
            self._failed(e, manual)
            return
        self.busy = True
        self.lbl.config(text="Reading...")
        outcome: Dict[str, object] = {}

        def worker() -> None:
            # No Tk calls here: Tk is not thread safe. The main thread polls outcome.
            try:
                outcome["result"] = self.read_fn(arg)
            except Exception as e:
                outcome["error"] = e

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        def poll() -> None:
            if thread.is_alive():
                self.app.root.after(50, poll)
                return
            self.busy = False
            if "result" in outcome:
                self.last = outcome["result"]
                self.app.apply_source_reading(self.detector, self.last)
                self.lbl.config(text=f"{self.last.status}\nread {time.strftime('%H:%M:%S')}")
            else:
                self._failed(outcome.get("error", RuntimeError("unknown error")), manual)

        self.app.root.after(50, poll)

    def _failed(self, error: Exception, manual: bool) -> None:
        self.lbl.config(text=f"Read failed {time.strftime('%H:%M:%S')}")
        if manual:
            messagebox.showerror(f"Read {self.title}", str(error))
        elif self.var_auto.get():
            # Don't keep hammering a busy or unreachable link with popups; stop auto.
            self.var_auto.set(False)
            messagebox.showwarning(f"Read {self.title}", f"{error}\n\nAuto read turned off.")

    def _on_auto_toggle(self) -> None:
        if self.var_auto.get():
            self._auto_tick()

    def _auto_tick(self) -> None:
        if not self.var_auto.get():
            return
        self.read(manual=False)
        self.app.root.after(SOURCE_AUTO_INTERVAL_MS, self._auto_tick)


class CalibrationWindow:
    """
    Calibrate one detector against a reference power meter at the objective.

    1. Setup, once per series: pick the detector; turn the other laser off (the meter
       sees both beams); confirm the dichroic (from the main window) and gain; block
       the beam and measure the dark offset.
    2. Points: set the wavelength and the meter to it; change only the laser power
       (3-5 levels), typing the meter reading and pressing "Read DAQ + Add". Then
       change the wavelength and repeat.

    Each point records the dichroic and gain; the dark reading is stored as a 0 mW
    point at each wavelength so every fit has an offset. Points take effect in the
    main window immediately; Save keeps them in this detector's own file.
    """

    OTHER_LASER = {SANTEC_DETECTOR: "SuperK", SUPERK_DETECTOR: "Santec"}
    OWN_LASER = {SANTEC_DETECTOR: "Santec", SUPERK_DETECTOR: "SuperK"}

    def __init__(self, app: PowerMonitorApp) -> None:
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("qt3power Calibration")
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.dark_v: Dict[str, Optional[float]] = {name: None for name in DETECTOR_NAMES}

        # ---- 1. Setup ----
        setup = tk.LabelFrame(self.win, text="1. Setup (once per series)", font=("Arial", 10, "bold"))
        setup.pack(fill="x", padx=10, pady=(8, 4))

        top = tk.Frame(setup)
        top.grid(row=0, column=0, columnspan=3, sticky="w", pady=(2, 4))
        tk.Label(top, text="Detector:").pack(side="left")
        self.var_detector = tk.StringVar(value=DETECTOR_NAMES[0])
        for name in DETECTOR_NAMES:
            tk.Radiobutton(
                top, text=f"{name} ({self.OWN_LASER[name]})", variable=self.var_detector, value=name,
                command=self._on_detector_change,
            ).pack(side="left", padx=4)

        self.checks: Dict[str, tk.BooleanVar] = {}

        def check_row(row: int, key: str) -> tk.Frame:
            var = tk.BooleanVar(value=False)
            var.trace_add("write", lambda *_a: self._update_points_state())
            self.checks[key] = var
            tk.Checkbutton(setup, variable=var).grid(row=row, column=0, sticky="nw")
            frm = tk.Frame(setup)
            frm.grid(row=row, column=1, sticky="w", pady=1)
            return frm

        frm = check_row(1, "other")
        self.lbl_other = tk.Label(frm, text="")
        self.lbl_other.pack(side="left")

        frm = check_row(2, "dichroic")
        self.lbl_dichroic = tk.Label(frm, text="")
        self.lbl_dichroic.pack(side="left")

        frm = check_row(3, "gain")
        tk.Label(frm, text="Gain switch on the detector is at").pack(side="left")
        self.gain_frame = tk.Frame(frm)
        self.gain_frame.pack(side="left", padx=4)
        self.gain_box: Optional[ttk.Combobox] = None

        frm = check_row(4, "dark")
        tk.Label(frm, text="Beam blocked, meter zeroed:").pack(side="left")
        tk.Button(frm, text="Measure Dark", command=self.measure_dark).pack(side="left", padx=4)
        self.lbl_dark = tk.Label(frm, text="not measured")
        self.lbl_dark.pack(side="left", padx=4)

        # ---- 2. Points ----
        self.points_frame = tk.LabelFrame(self.win, text="2. Points", font=("Arial", 10, "bold"))
        self.points_frame.pack(fill="both", expand=True, padx=10, pady=4)
        self.lbl_points_hint = tk.Label(self.points_frame, text="", fg="#555555", justify="left", anchor="w")
        self.lbl_points_hint.pack(fill="x", padx=4)

        # Wavelength: set for each group of points, so it lives here rather than in Setup.
        wl_row = tk.Frame(self.points_frame)
        wl_row.pack(fill="x", padx=4, pady=(4, 0))
        tk.Label(wl_row, text="Wavelength (nm):").pack(side="left")
        self.ent_wl = tk.Entry(wl_row, width=8)
        self.ent_wl.pack(side="left", padx=(2, 8))
        self.ent_wl.bind("<Return>", lambda _e: self._set_wavelength())
        self.ent_wl.bind("<FocusOut>", lambda _e: self._set_wavelength())
        self.lw_frame = tk.Frame(wl_row)  # shown only for detectors with a linewidth
        tk.Label(self.lw_frame, text="Linewidth (nm):").pack(side="left")
        self.ent_lw = tk.Entry(self.lw_frame, width=6)
        self.ent_lw.pack(side="left", padx=(2, 8))
        self.ent_lw.bind("<Return>", lambda _e: self._set_linewidth())
        self.ent_lw.bind("<FocusOut>", lambda _e: self._set_linewidth())
        self.btn_read_laser = tk.Button(wl_row, text="Read", command=self._read_laser)
        self.btn_read_laser.pack(side="left")
        # Cleared whenever the wavelength changes, so the meter is re-set for every wavelength.
        self.var_meter_wl = tk.BooleanVar(value=False)
        self.var_meter_wl.trace_add("write", lambda *_a: self._update_points_state())
        self._meter_wl: Optional[float] = None
        tk.Checkbutton(
            wl_row, text="Power meter is set to this wavelength", variable=self.var_meter_wl
        ).pack(side="left", padx=10)

        add = tk.Frame(self.points_frame)
        add.pack(fill="x", padx=4, pady=4)
        tk.Label(add, text="Power at objective (mW):").pack(side="left")
        self.ent_power = tk.Entry(add, width=10)
        self.ent_power.pack(side="left", padx=4)
        self.ent_power.bind("<Return>", lambda _e: self.add_point())
        self.btn_add = tk.Button(add, text="Read DAQ + Add", command=self.add_point, bg="#ccffcc")
        self.btn_add.pack(side="left", padx=4)
        self.btn_delete = tk.Button(add, text="Delete Selected", command=self.delete_selected)
        self.btn_delete.pack(side="left", padx=4)
        self.lbl_last = tk.Label(add, text="", fg="#555555")
        self.lbl_last.pack(side="left", padx=8)

        cols = ("power", "voltage", "k", "gain", "optics")
        self.tree = ttk.Treeview(self.points_frame, columns=cols, show="tree headings", height=9)
        self.tree.heading("#0", text="Wavelength / fit")
        self.tree.column("#0", width=330, anchor="w")
        for c, title, w in zip(
            cols, ("Power (mW)", "Voltage (V)", "Slope (V/mW)", "Gain", "Dichroic"), (85, 85, 110, 55, 85)
        ):
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=4, pady=4)

        bottom = tk.Frame(self.win)
        bottom.pack(fill="x", padx=10, pady=(0, 8))
        self.btn_save = tk.Button(bottom, text="Save", command=self._save)
        self.btn_save.pack(side="right")
        self.lbl_status = tk.Label(bottom, text="", anchor="w")
        self.lbl_status.pack(side="left")

        self._on_detector_change()

    # ---- setup ----

    def _name(self) -> str:
        return self.var_detector.get()

    def _detector(self) -> DetectorCalibration:
        return self.app.detectors[self._name()]

    def _on_detector_change(self) -> None:
        name = self._name()
        for var in self.checks.values():
            var.set(False)
        self.var_meter_wl.set(False)
        self.lbl_other.config(
            text=f"The {self.OTHER_LASER[name]} is off or shuttered (the meter at the objective sees both beams)."
        )
        self.btn_read_laser.config(text=f"Read {self.OWN_LASER[name]}")
        self.lbl_last.config(text="")
        if self.gain_box is not None:
            self.gain_box.destroy()
        # Shares the main window's gain variable, so both always show the same (last used) gain.
        self.gain_box = self.app._gain_box(self.gain_frame, name)
        self.gain_box.pack()
        if name in LINEWIDTH_DETECTORS:
            self.lw_frame.pack(side="left", before=self.btn_read_laser)
        else:
            self.lw_frame.pack_forget()
        self._show_dark()
        self.refresh()

    def _set_wavelength(self) -> None:
        name = self._name()
        ent = self.app.wavelength_entries[name]
        ent.delete(0, tk.END)
        ent.insert(0, self.ent_wl.get().strip())
        self.app.set_wavelength(name)
        self.refresh()

    def _set_linewidth(self) -> None:
        name = self._name()
        ent = self.app.linewidth_entries.get(name)
        if ent is None:
            return
        ent.delete(0, tk.END)
        ent.insert(0, self.ent_lw.get().strip())
        self.app.set_linewidth(name)
        self.refresh()

    def _read_laser(self) -> None:
        # Same read-only reader as the main window; its result updates the main window, then refresh() here.
        self.app.source_controls[self._name()].read(manual=True)

    def _read_voltage(self, title: str) -> Optional[float]:
        try:
            v1, v2 = self.app.read_average_voltages(CALIBRATION_READ_BURSTS)
        except Exception as e:
            messagebox.showerror(title, str(e), parent=self.win)
            return None
        name = self._name()
        if name in self.app.saturated_detectors():
            messagebox.showwarning(
                title,
                f"{name} saturated during this reading, so it was not used. "
                "Lower the detector gain or the power.",
                parent=self.win,
            )
            return None
        return v1 if name == "PDA50B2" else v2

    def measure_dark(self) -> None:
        v = self._read_voltage("Measure Dark")
        if v is None:
            return
        self.dark_v[self._name()] = v
        self._show_dark()
        self.checks["dark"].set(True)

    def _show_dark(self) -> None:
        v = self.dark_v[self._name()]
        self.lbl_dark.config(text="not measured" if v is None else f"{v * 1000:+.2f} mV")

    def _update_points_state(self) -> None:
        if not hasattr(self, "btn_add"):
            return  # still building the window
        setup_done = all(var.get() for var in self.checks.values()) and self.dark_v[self._name()] is not None
        ready = setup_done and self.var_meter_wl.get()
        self.btn_add.config(state="normal" if ready else "disabled")
        self.ent_power.config(state="normal" if ready else "disabled")
        if not setup_done:
            hint = "Finish the setup checks above (including Measure Dark) to add points."
        elif not ready:
            hint = "Set the wavelength, set the power meter to it, then tick the box."
        else:
            hint = ("Change only the laser power (3-5 levels) at this wavelength, then change the wavelength "
                    "and repeat.\nType the meter reading, then Read DAQ + Add.")
        self.lbl_points_hint.config(text=hint)

    # ---- points ----

    def add_point(self) -> None:
        if str(self.btn_add.cget("state")) == "disabled":
            return
        name = self._name()
        try:
            power = float(self.ent_power.get())
            if power <= 0:
                raise ValueError("power must be > 0")
        except ValueError as e:
            messagebox.showerror("Add Point", f"Check the power at the objective: {e}", parent=self.win)
            return
        gain = self.app.gain_db(name)
        if gain is None:
            messagebox.showerror("Add Point", f"Set the {name} gain first.", parent=self.win)
            return
        wl = self.app.wavelengths[name]
        linewidth = self.app.linewidths[name]
        path = dict(self.app.path_model.installed)
        t_path = self.app.path_model.transmission(name, wl, path, linewidth)
        if t_path < MIN_PATH_TRANSMISSION:
            messagebox.showerror(
                "Add Point",
                f"Only {t_path * 100:.2f}% of the {name} beam reaches the objective at {wl:g} nm "
                f"with {self.app.path_model.label(path)}. Check the wavelength and dichroic.",
                parent=self.win,
            )
            return
        det = self._detector()
        mismatch = []
        if det.points and calibration_optics(det) != [optics_label(path)]:
            mismatch.append(
                f"was measured with {'; '.join(calibration_optics(det))}, but {optics_label(path)} is installed"
            )
        if det.points and calibration_gains(det) not in ([], [gain]):
            mismatch.append(f"was taken at {gain_label(calibration_gains(det))}, but {gain} dB is set")
        if mismatch:
            answer = messagebox.askyesnocancel(
                "Add Point",
                f"The {name} calibration in use " + " and ".join(mismatch) + ".\n\n"
                f"Yes: start a new {name} calibration for {optics_label(path)} at {gain} dB "
                "(saved files are not changed).\n"
                "No: add this point to the current calibration anyway (mixed).",
                parent=self.win,
            )
            if answer is None:
                return
            if answer:
                self.app._apply_loaded_calibration(name, DetectorCalibration(name), None)
                det = self._detector()
        voltage = self._read_voltage("Add Point")
        if voltage is None:
            return
        # The dark reading becomes a 0 mW point at each new wavelength, so each fit has an offset.
        if not any(p.power_mw == 0 and p.wavelength_nm == wl for p in det.points):
            det.points.append(CalibrationPoint(wl, 0.0, self.dark_v[name], path, linewidth, gain))
        det.points.append(CalibrationPoint(wl, power, voltage, path, linewidth, gain))
        self.app.calibration_dirty[name] = True
        self.lbl_last.config(text=f"added {power:g} mW -> {voltage:.5f} V")
        self.ent_power.delete(0, tk.END)
        self.refresh()

    def delete_selected(self) -> None:
        det = self._detector()
        idx = set()
        for iid in self.tree.selection():
            if iid.startswith("p"):
                idx.add(int(iid[1:]))
            else:  # a wavelength row: delete all its points
                idx.update(int(c[1:]) for c in self.tree.get_children(iid))
        for i in sorted(idx, reverse=True):
            del det.points[i]
        if idx:
            self.app.calibration_dirty[self._name()] = True
        self.refresh()

    def _save(self) -> None:
        if self.app.save_calibration_dialog(self._name()):
            self.refresh()

    def refresh(self) -> None:
        name = self._name()
        det = self._detector()
        self.lbl_dichroic.config(
            text=f"Installed dichroic is {', '.join(self.app.path_model.installed.values())} "
            f"({self.app.path_model.transmission(name, self.app.wavelengths[name]) * 100:.1f}% of this beam "
            "reaches the objective)."
        )
        if self._meter_wl != self.app.wavelengths[name]:
            self._meter_wl = self.app.wavelengths[name]
            self.var_meter_wl.set(False)
        if self.win.focus_get() is not self.ent_wl:
            self.ent_wl.delete(0, tk.END)
            self.ent_wl.insert(0, f"{self.app.wavelengths[name]:g}")
        if name in LINEWIDTH_DETECTORS and self.win.focus_get() is not self.ent_lw:
            self.ent_lw.delete(0, tk.END)
            self.ent_lw.insert(0, f"{self.app.linewidths[name]:g}")

        self.tree.delete(*self.tree.get_children())
        fits = {wl: (k, offset) for wl, k, offset, _n in det.fit_by_wavelength()}
        groups: Dict[float, List[int]] = {}
        for i, p in enumerate(det.points):
            groups.setdefault(round(float(p.wavelength_nm), 3), []).append(i)
        for wl in sorted(groups):
            lw = det.points[groups[wl][0]].linewidth_nm
            text = f"{wl:g} nm" + (f" (width {lw:g})" if lw else "")
            if wl in fits:
                k, offset = fits[wl]
                text += f"   fit x{k / det.default_factor(wl):.3f} of default, offset {offset * 1000:+.2f} mV"
            parent = self.tree.insert("", "end", iid=f"w{wl}", text=text, open=True)
            dark = next((det.points[i].voltage_v for i in groups[wl] if det.points[i].power_mw == 0), 0.0)
            for i in groups[wl]:
                p = det.points[i]
                k = (p.voltage_v - dark) / p.power_mw if p.power_mw else float("nan")
                self.tree.insert(
                    parent, "end", iid=f"p{i}", text="",
                    values=(
                        "dark" if p.power_mw == 0 else f"{p.power_mw:.5g}",
                        f"{p.voltage_v:.5f}",
                        "" if p.power_mw == 0 else f"{k:.4g}",
                        _gain_text(p.gain_db) or "?",
                        ", ".join(p.path.values()) if p.path else "?",
                    ),
                )
        n = sum(1 for p in det.points if p.power_mw > 0)
        unsaved = " (unsaved)" if self.app.calibration_dirty[name] else ""
        self.lbl_status.config(
            text=f"{name}: {n} point(s) at {len(groups)} wavelength(s){unsaved}" if n else
            f"{name}: no points, using the default curve"
        )
        self.btn_save.config(text=f"Save {name} Calibration...", state="normal" if det.points else "disabled")
        self._update_points_state()
        self.app._refresh_conversion_display()

    def close(self) -> None:
        self.app.calibration_window = None
        self.win.destroy()


def main() -> None:
    root = tk.Tk()
    app = PowerMonitorApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()

