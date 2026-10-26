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
    calibration_status,
    default_calibration_filename,
    optics_label,
    scan_calibration_files,
    CalibrationPoint,
    DetectorCalibration,
    load_calibrations,
    MIN_PATH_TRANSMISSION,
    load_last_wavelengths,
    load_settings,
    save_calibrations,
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
DEFAULT_LARGE_READINGS = {
    "PDA50B2_power": False,
    "PDA50B2_voltage": True,
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
    if loaded, overrides the default. Assumes the detector gain is never changed.
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
        self.calibration_path: Optional[Path] = None
        self.calibration_dirty = False  # points added/deleted since last save or load
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
        self.ent_device = tk.Entry(daq_row, width=8)
        self.ent_device.pack(side="left", padx=(2, 10))
        self.ent_device.insert(0, "Dev1")

        tk.Label(daq_row, text="PDA50B2 Ch:").pack(side="left")
        self.ent_ch1 = tk.Entry(daq_row, width=6)
        self.ent_ch1.pack(side="left", padx=(2, 10))
        self.ent_ch1.insert(0, "ai2")

        tk.Label(daq_row, text="PDA100A2 Ch:").pack(side="left")
        self.ent_ch2 = tk.Entry(daq_row, width=6)
        self.ent_ch2.pack(side="left", padx=(2, 10))
        self.ent_ch2.insert(0, "ai0")

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
        for text, cmd in (
            ("Calibrate...", self.open_calibration_window),
            ("Load Cal...", self.load_calibration_dialog),
            ("Save Cal...", self.save_calibration_dialog),
            ("Use Default Curve", self.clear_calibration),
        ):
            tk.Button(optics_row, text=text, command=cmd).pack(side="left", padx=2)

        cal_row = tk.Frame(cfg_frame)
        cal_row.grid(row=2, column=0, sticky="w")
        tk.Label(cal_row, text="Calibration:").pack(side="left", anchor="n")
        self.lbl_cal_source = tk.Label(
            cal_row, text="", anchor="w", justify="left", wraplength=620, font=("Arial", 10, "bold")
        )
        self.lbl_cal_source.pack(side="left", padx=4)

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
            state="disabled",
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
                text=f"DAQ: Initialized ({device}, {ch1}, {ch2})",
                fg="green",
            )
            self._show_readings(v1, v2)

            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
            self.lbl_run_status.config(text="Monitor: Ready")
        except Exception as e:
            self.detector_ctrl = None
            self.lbl_status.config(text="DAQ: Error", fg="red")
            self.btn_start.config(state="disabled")
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
        if self.calibration_path is not None:
            lines.append(f"File: {self.calibration_path}")
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
        status, text = calibration_status(
            self.detectors,
            self.calibration_path,
            self.calibration_dirty,
            self.path_model.installed,
            self._calibration_library(),
        )
        self.lbl_cal_source.config(text=text, fg=STATUS_COLORS[status])
        if self.last_voltages is not None:
            self._show_readings(*self.last_voltages)

    def _apply_loaded_calibrations(self, loaded: Dict[str, DetectorCalibration]) -> None:
        for name in DETECTOR_NAMES:
            self.detectors[name] = loaded.get(name, DetectorCalibration(name))
        self.calibration_dirty = False
        self._wire_detectors()

    def _confirm_discard_unsaved(self, action: str) -> bool:
        if not self.calibration_dirty:
            return True
        return messagebox.askyesno(
            "Unsaved calibration",
            f"You have unsaved calibration points. {action} anyway and discard them?",
        )

    def _calibration_library(self):
        """Saved calibration files the status compares against, newest first."""
        folders = [CALIBRATIONS_DIR, DEFAULT_CALIBRATION_PATH.parent]
        if self.calibration_path is not None:
            folders.append(self.calibration_path.parent)
        return scan_calibration_files(folders)

    def _load_newest_calibration_for_installed_optics(self) -> None:
        """At startup: load the newest saved calibration measured with the installed optics, if any."""
        current = optics_label(self.path_model.installed)
        for info in self._calibration_library():
            if info.optics != [current]:
                continue
            try:
                self._apply_loaded_calibrations(load_calibrations(info.path))
                self.calibration_path = info.path
            except Exception as e:
                messagebox.showwarning(
                    "Calibration",
                    f"Could not load {info.path}:\n{e}\nUsing default responsivity curve.",
                )
            return

    def load_calibration_dialog(self) -> None:
        if not self._confirm_discard_unsaved("Load a calibration"):
            return
        path = filedialog.askopenfilename(
            title="Load qt3power calibration",
            initialdir=str(CALIBRATIONS_DIR if CALIBRATIONS_DIR.is_dir() else DEFAULT_CALIBRATION_PATH.parent),
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            self._apply_loaded_calibrations(load_calibrations(path))
        except Exception as e:
            messagebox.showerror("Calibration", f"Could not load {path}:\n{e}")
            return
        self.calibration_path = Path(path)
        self._refresh_conversion_display()
        if self.calibration_window is not None:
            self.calibration_window.refresh()

    def save_calibration_dialog(self) -> bool:
        """Returns True if the calibration was saved."""
        CALIBRATIONS_DIR.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(
            title="Save qt3power calibration",
            initialdir=str(CALIBRATIONS_DIR),
            initialfile=default_calibration_filename(self.path_model.installed),
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return False
        try:
            save_calibrations(path, self.detectors)
        except Exception as e:
            messagebox.showerror("Calibration", f"Could not save {path}:\n{e}")
            return False
        self.calibration_path = Path(path)
        self.calibration_dirty = False
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

    def clear_calibration(self) -> None:
        if any(d.is_calibrated for d in self.detectors.values()):
            if not messagebox.askyesno(
                "Use default curve",
                "Discard the measured calibration in memory and use the default responsivity curve?\n"
                "(Saved files are not deleted.)",
            ):
                return
        self.detectors = {name: DetectorCalibration(name) for name in DETECTOR_NAMES}
        self.calibration_path = None
        self.calibration_dirty = False
        self._wire_detectors()
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
        if self.detector_ctrl is None:
            messagebox.showerror("Error", "Initialize DAQ before starting monitoring.")
            return
        if self.running:
            return

        self.running = True
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.lbl_run_status.config(text="Monitor: Running")
        self._schedule_next_update()

    def stop_monitoring(self) -> None:
        self.running = False
        self._set_power_widgets_paused(False)
        self.btn_start.config(state="normal" if self.detector_ctrl else "disabled")
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
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
            self.lbl_run_status.config(text="Monitor: Error")
            self.lbl_status.config(text=f"DAQ: Error ({e})", fg="red")
        finally:
            self._schedule_next_update()

    def on_closing(self) -> None:
        """Cleanly stop monitoring and close the window."""
        if self.calibration_dirty:
            answer = messagebox.askyesnocancel(
                "Unsaved calibration",
                "You have unsaved calibration points. Save them before closing?",
            )
            if answer is None:
                return
            if answer and not self.save_calibration_dialog():
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
        top = tk.Frame(frm)
        top.pack(anchor="w")
        tk.Button(top, text=f"Read {title}", command=lambda: self.read(manual=True)).pack(side="left")
        if open_settings is not None:
            tk.Button(top, text="...", width=2, command=open_settings).pack(side="left", padx=2)
        tk.Checkbutton(frm, text="Auto", variable=self.var_auto, command=self._on_auto_toggle).pack(anchor="w")
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
    Record (wavelength, known power, detector voltage) points.

    Workflow: set the laser wavelength, measure its power AT THE OBJECTIVE with a
    reference power meter, type that power in, press "Read DAQ" to average the
    detector voltage, then "Add Point". Each point records the optics installed
    in the main window, so later optics swaps rescale it. Repeat at several powers and wavelengths. Points take effect
    in the main window immediately; use Save Cal... to keep them.
    """

    def __init__(self, app: PowerMonitorApp) -> None:
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("qt3power Calibration")
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self.lbl_path = tk.Label(self.win, text="", justify="left", anchor="w")
        self.lbl_path.pack(fill="x", padx=10, pady=(8, 0))

        frm = tk.Frame(self.win)
        frm.pack(fill="x", padx=10, pady=5)

        tk.Label(frm, text="Detector:").grid(row=0, column=0, sticky="e")
        self.var_detector = tk.StringVar(value=DETECTOR_NAMES[0])
        for i, name in enumerate(DETECTOR_NAMES):
            tk.Radiobutton(
                frm, text=name, variable=self.var_detector, value=name, command=self._on_detector_change
            ).grid(row=0, column=1 + i, sticky="w")

        tk.Label(frm, text="Wavelength (nm):").grid(row=1, column=0, sticky="e")
        self.ent_wl = tk.Entry(frm, width=10)
        self.ent_wl.grid(row=1, column=1, sticky="w", padx=3, pady=2)
        self.ent_wl.insert(0, f"{app.wavelengths[DETECTOR_NAMES[0]]:g}")
        tk.Label(frm, text="Linewidth (nm):").grid(row=1, column=2, sticky="e")
        self.ent_lw = tk.Entry(frm, width=6)
        self.ent_lw.grid(row=1, column=3, sticky="w", padx=3)
        self._update_linewidth_field()

        tk.Label(frm, text="Power at objective (mW):").grid(row=2, column=0, sticky="e")
        self.ent_power = tk.Entry(frm, width=10)
        self.ent_power.grid(row=2, column=1, sticky="w", padx=3, pady=2)

        tk.Label(frm, text="Voltage (V):").grid(row=3, column=0, sticky="e")
        self.ent_voltage = tk.Entry(frm, width=10)
        self.ent_voltage.grid(row=3, column=1, sticky="w", padx=3, pady=2)
        tk.Button(frm, text="Read DAQ", command=self.read_daq).grid(row=3, column=2, padx=3)

        tk.Button(frm, text="Add Point", command=self.add_point, bg="#ccffcc").grid(
            row=4, column=1, sticky="w", padx=3, pady=5
        )
        tk.Button(frm, text="Delete Selected", command=self.delete_selected).grid(
            row=4, column=2, columnspan=2, sticky="w", padx=3, pady=5
        )

        cols = ("wl", "power", "voltage", "k", "path")
        self.tree = ttk.Treeview(self.win, columns=cols, show="headings", height=8)
        for c, title, w in zip(
            cols, ("Wavelength (nm)", "Power (mW)", "Voltage (V)", "V/mW", "Optics"), (110, 90, 90, 80, 160)
        ):
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=10, pady=5)

        self.lbl_fit = tk.Label(self.win, text="", justify="left", font=("Courier", 9))
        self.lbl_fit.pack(fill="x", padx=10, pady=5)

        self.refresh()

    def _update_linewidth_field(self) -> None:
        name = self.var_detector.get()
        self.ent_lw.config(state="normal")
        self.ent_lw.delete(0, tk.END)
        self.ent_lw.insert(0, f"{self.app.linewidths[name]:g}")
        if name not in LINEWIDTH_DETECTORS:
            self.ent_lw.config(state="disabled")

    def _on_detector_change(self) -> None:
        self.ent_wl.delete(0, tk.END)
        self.ent_wl.insert(0, f"{self.app.wavelengths[self.var_detector.get()]:g}")
        self._update_linewidth_field()
        self.refresh()

    def _detector(self) -> DetectorCalibration:
        return self.app.detectors[self.var_detector.get()]

    def read_daq(self) -> None:
        try:
            n = CALIBRATION_READ_BURSTS
            v1, v2 = self.app.read_average_voltages(n)
        except Exception as e:
            messagebox.showerror("Read DAQ", str(e), parent=self.win)
            return
        name = self.var_detector.get()
        v = v1 if name == "PDA50B2" else v2
        self.ent_voltage.delete(0, tk.END)
        self.ent_voltage.insert(0, f"{v:.5f}")
        if name in self.app.saturated_detectors():
            messagebox.showwarning(
                "Read DAQ",
                f"{name} saturated during this reading, so the average is not reliable. "
                "Lower the detector gain or the power before adding this point.",
                parent=self.win,
            )

    def add_point(self) -> None:
        try:
            wl = float(self.ent_wl.get())
            linewidth = float(self.ent_lw.get() or 0)
            power = float(self.ent_power.get())
            voltage = float(self.ent_voltage.get())
            if wl <= 0 or power <= 0 or linewidth < 0:
                raise ValueError("wavelength and power must be > 0, linewidth >= 0")
        except ValueError as e:
            messagebox.showerror("Add Point", f"Check wavelength, power and voltage: {e}", parent=self.win)
            return
        name = self.var_detector.get()
        path = dict(self.app.path_model.installed)
        t_path = self.app.path_model.transmission(name, wl, path, linewidth)
        if t_path < MIN_PATH_TRANSMISSION:
            messagebox.showerror(
                "Add Point",
                f"Only {t_path * 100:.2f}% of the {name} beam reaches the objective at {wl:g} nm "
                f"with {self.app.path_model.label(path)}. Check the optics selection.",
                parent=self.win,
            )
            return
        self._detector().points.append(CalibrationPoint(wl, power, voltage, path, linewidth))
        self.app.calibration_dirty = True
        self.ent_power.delete(0, tk.END)
        self.ent_voltage.delete(0, tk.END)
        self.refresh()

    def delete_selected(self) -> None:
        det = self._detector()
        idx = sorted((int(i) for i in self.tree.selection()), reverse=True)
        for i in idx:
            del det.points[i]
        if idx:
            self.app.calibration_dirty = True
        self.refresh()

    def refresh(self) -> None:
        det = self._detector()
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(det.points):
            k = p.voltage_v / p.power_mw if p.power_mw else float("nan")
            self.tree.insert(
                "", "end", iid=str(i),
                values=(
                    f"{p.wavelength_nm:g}" + (f" (w {p.linewidth_nm:g})" if p.linewidth_nm else ""),
                    f"{p.power_mw:.5g}", f"{p.voltage_v:.5f}", f"{k:.4g}",
                    self.app.path_model.label(p.path) if p.path else "(not recorded)",
                ),
            )
        name = self.var_detector.get()
        wl_now = self.app.wavelengths[name]
        self.lbl_path.config(
            text=f"Measure the reference power at the objective.\n"
            f"Optics now: {self.app.path_model.label()}  "
            f"({name} path {self.app.path_model.transmission(name, wl_now) * 100:.1f}% at {wl_now:g} nm)"
        )
        lines = ["Fit per wavelength, detector only (V = k*P/T_path + offset):"]
        for wl, k, offset, n in det.fit_by_wavelength():
            ratio = k / det.default_factor(wl)
            lines.append(
                f"  {wl:7.1f} nm  k={k:8.4g} V/mW  offset={offset * 1000:+7.2f} mV"
                f"  n={n}  vs default curve x{ratio:.3f}"
            )
        if len(lines) == 1:
            lines.append("  (no points: using default curve)")
        self.lbl_fit.config(text="\n".join(lines))
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

