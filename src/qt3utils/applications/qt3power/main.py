import tkinter as tk
from tkinter import messagebox, filedialog, ttk
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple, Union

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
from qt3utils.applications.qt3power.paths import (
    DEFAULT_PATH_CONFIG,
    PATH_CONFIG_PATH,
    PathModel,
)

DETECTOR_NAMES = ("PDA50B2", "PDA100A2")

# Calibration status text colors (yellow darkened to stay readable on grey).
STATUS_COLORS = {
    STATUS_CURRENT: "#008800",
    STATUS_OLDER: "#b08d00",
    STATUS_WRONG_OPTICS: "#cc0000",
    STATUS_DEFAULT: "#d96c00",
}

_STALE_LOCK_TOOLTIP = (
    "Only use this when qt3santec is not scanning but the busy marker was left behind "
    "(e.g. after a crash or force-close). Do not enable during an active Santec sweep—"
    "both apps may touch the same DAQ channels and cause errors."
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
    ) -> None:
        """
        Initialize photodetector controller.

        Args:
            device_name: NI DAQ device name (e.g., 'Dev1').
            channel1: Analog input channel for first detector (PDA50B2).
            channel2: Analog input channel for second detector (PDA100A2).
            min_voltage: Minimum expected voltage.
            max_voltage: Maximum expected voltage.
        """
        self.device_name = device_name
        self.channel1 = channel1
        self.channel2 = channel2
        self.min_voltage = min_voltage
        self.max_voltage = max_voltage

    def read_both_detectors(self) -> Tuple[float, float]:
        """
        Read voltage from both detectors simultaneously.

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
                data = task.read(number_of_samples_per_channel=1)

            if isinstance(data, (list, tuple)) and len(data) >= 2:
                v1_raw = data[0]
                v2_raw = data[1]
                v1 = float(v1_raw[0] if hasattr(v1_raw, "__getitem__") else v1_raw)
                v2 = float(v2_raw[0] if hasattr(v2_raw, "__getitem__") else v2_raw)
                return v1, v2

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

        tk.Label(cfg_frame, text="DAQ Device:").grid(row=0, column=0, sticky="e")
        self.ent_device = tk.Entry(cfg_frame, width=8)
        self.ent_device.grid(row=0, column=1, sticky="w")
        self.ent_device.insert(0, "Dev1")

        tk.Label(cfg_frame, text="PDA50B2 Ch:").grid(row=0, column=2, sticky="e")
        self.ent_ch1 = tk.Entry(cfg_frame, width=6)
        self.ent_ch1.grid(row=0, column=3, sticky="w")
        self.ent_ch1.insert(0, "ai2")

        tk.Label(cfg_frame, text="PDA100A2 Ch:").grid(row=0, column=4, sticky="e")
        self.ent_ch2 = tk.Entry(cfg_frame, width=6)
        self.ent_ch2.grid(row=0, column=5, sticky="w")
        self.ent_ch2.insert(0, "ai0")

        self.btn_init = tk.Button(
            cfg_frame,
            text="Initialize DAQ",
            command=self.init_daq,
            bg="#dddddd",
        )
        self.btn_init.grid(row=0, column=6, sticky="w")

        self.lbl_status = tk.Label(cfg_frame, text="DAQ: Not initialized", fg="red")
        self.lbl_status.grid(row=0, column=7, sticky="w")

        # Optics between the beam sampler and the objective, one dropdown per slot
        optics_row = tk.Frame(cfg_frame)
        optics_row.grid(row=1, column=0, columnspan=8, sticky="w")
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

        tk.Label(cfg_frame, text="Calibration:").grid(row=2, column=0, sticky="e")
        self.lbl_cal_source = tk.Label(
            cfg_frame, text="", anchor="w", justify="left", wraplength=620, font=("Arial", 10, "bold")
        )
        self.lbl_cal_source.grid(row=2, column=1, columnspan=7, sticky="w")

        for child in cfg_frame.winfo_children():
            child.grid_configure(padx=3, pady=3)

        # Power display
        display_frame = tk.LabelFrame(main_frame, text="Photodetector Power")
        display_frame.pack(fill="both", expand=True, pady=10)

        # PDA50B2
        tk.Label(display_frame, text="PDA50B2 Power (mW):", font=("Arial", 11, "bold")).grid(
            row=0,
            column=0,
            sticky="e",
            padx=5,
            pady=5,
        )
        self.lbl_pda50b2_power = tk.Label(
            display_frame,
            text="--",
            font=("Arial", 18, "bold"),
            fg="#0044aa",
        )
        self.lbl_pda50b2_power.grid(row=0, column=1, sticky="w", padx=5, pady=5)
        self._build_wavelength_entry(display_frame, "PDA50B2", row=0)

        tk.Label(display_frame, text="Voltage (V):").grid(
            row=1,
            column=0,
            sticky="e",
            padx=5,
            pady=2,
        )
        self.lbl_pda50b2_voltage = tk.Label(display_frame, text="--",font=("Arial", 40, "bold"))
        self.lbl_pda50b2_voltage.grid(row=1, column=1, sticky="w", padx=5, pady=2)

        # PDA100A2
        tk.Label(display_frame, text="PDA100A2 Power (mW):", font=("Arial", 11, "bold")).grid(
            row=2,
            column=0,
            sticky="e",
            padx=5,
            pady=10,
        )
        self.lbl_pda100a2_power = tk.Label(
            display_frame,
            text="--",
            font=("Arial", 18, "bold"),
            fg="#006600",
        )
        self.lbl_pda100a2_power.grid(row=2, column=1, sticky="w", padx=5, pady=10)
        self._build_wavelength_entry(display_frame, "PDA100A2", row=2)

        tk.Label(display_frame, text="Voltage (V):").grid(
            row=3,
            column=0,
            sticky="e",
            padx=5,
            pady=2,
        )
        self.lbl_pda100a2_voltage = tk.Label(display_frame, text="--")
        self.lbl_pda100a2_voltage.grid(row=3, column=1, sticky="w", padx=5, pady=2)

        for child in display_frame.winfo_children():
            child.grid_configure(padx=5, pady=3)
        display_frame.grid_columnconfigure(1, weight=1)

        for name, labels in (
            ("PDA50B2", (self.lbl_pda50b2_power, self.lbl_pda50b2_voltage)),
            ("PDA100A2", (self.lbl_pda100a2_power, self.lbl_pda100a2_voltage)),
        ):
            for lbl in labels:
                _attach_tooltip(lbl, lambda n=name: self._conversion_text(n))

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
            self.detector_ctrl = PhotodetectorController(device, ch1, ch2)
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

    def _show_readings(self, v1: float, v2: float) -> None:
        self.last_voltages = (v1, v2)
        self.lbl_pda50b2_voltage.config(text=f"{v1:.4f}")
        self.lbl_pda100a2_voltage.config(text=f"{v2:.4f}")
        self.lbl_pda50b2_power.config(text=f"{self._voltage_to_mw('PDA50B2', v1):.4f}")
        self.lbl_pda100a2_power.config(text=f"{self._voltage_to_mw('PDA100A2', v2):.4f}")

    def _voltage_to_mw(self, detector: str, voltage: float) -> float:
        """Convert voltage (V) to power at the objective (mW) at the current wavelength and optics."""
        wl = self.wavelengths[detector]
        t_path = self.path_model.transmission(detector, wl)
        return self.detectors[detector].voltage_to_mw(voltage, wl, t_path)

    def _wire_detectors(self) -> None:
        """Let each detector's calibration look up the path transmission of its recorded points."""
        for name, det in self.detectors.items():
            # Points without a recorded path predate path support: treat as no optics.
            det.point_transmission = lambda p, n=name: (
                self.path_model.transmission(n, p.wavelength_nm, p.path) if p.path else 1.0
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

    # ---- Wavelength / calibration ----

    def _build_wavelength_entry(self, parent: tk.Widget, name: str, row: int) -> None:
        tk.Label(parent, text="Wavelength (nm):").grid(row=row, column=2, sticky="e", padx=5)
        ent = tk.Entry(parent, width=8)
        ent.grid(row=row, column=3, sticky="w", padx=5)
        ent.insert(0, f"{self.wavelengths[name]:g}")
        ent.bind("<Return>", lambda _e: self.set_wavelength(name))
        ent.bind("<FocusOut>", lambda _e: self.set_wavelength(name))
        self.wavelength_entries[name] = ent

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
        k, offset = det.factor_and_offset(wl)
        t_path = self.path_model.transmission(name, wl)
        lines = [f"{name} at {wl:g} nm, power at objective"]
        if t_path > 0:
            lines.append(f"Overall: {k / t_path:.4g} V/mW")
        lines.append(f"Detector: {k:.4g} V/mW")
        if offset:
            lines.append(f"Offset: {offset * 1000:+.2f} mV")
        lines.append(f"Optics to objective: {t_path * 100:.1f}%")
        lines.extend("  " + line for line in self.path_model.describe(name, wl))
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
        """Average n_samples reads of both detectors. Raises RuntimeError if the DAQ is unavailable."""
        if self.detector_ctrl is None:
            raise RuntimeError("Initialize DAQ first.")
        if santec_daq_busy() and not self.var_ignore_santec_lock.get():
            raise RuntimeError("qt3santec has the DAQ (scan in progress).")
        v1s, v2s = [], []
        for _ in range(max(1, n_samples)):
            v1, v2 = self.detector_ctrl.read_both_detectors()
            v1s.append(v1)
            v2s.append(v2)
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
        # No persistent DAQ task is kept open in this minimal app, so nothing to close.
        self.root.destroy()


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

        tk.Label(frm, text="Power at objective (mW):").grid(row=2, column=0, sticky="e")
        self.ent_power = tk.Entry(frm, width=10)
        self.ent_power.grid(row=2, column=1, sticky="w", padx=3, pady=2)

        tk.Label(frm, text="Voltage (V):").grid(row=3, column=0, sticky="e")
        self.ent_voltage = tk.Entry(frm, width=10)
        self.ent_voltage.grid(row=3, column=1, sticky="w", padx=3, pady=2)
        tk.Button(frm, text="Read DAQ", command=self.read_daq).grid(row=3, column=2, padx=3)
        tk.Label(frm, text="Samples:").grid(row=3, column=3, sticky="e")
        self.ent_samples = tk.Entry(frm, width=5)
        self.ent_samples.grid(row=3, column=4, sticky="w", padx=3)
        self.ent_samples.insert(0, "20")

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

    def _on_detector_change(self) -> None:
        self.ent_wl.delete(0, tk.END)
        self.ent_wl.insert(0, f"{self.app.wavelengths[self.var_detector.get()]:g}")
        self.refresh()

    def _detector(self) -> DetectorCalibration:
        return self.app.detectors[self.var_detector.get()]

    def read_daq(self) -> None:
        try:
            n = int(self.ent_samples.get())
            v1, v2 = self.app.read_average_voltages(n)
        except Exception as e:
            messagebox.showerror("Read DAQ", str(e), parent=self.win)
            return
        v = v1 if self.var_detector.get() == "PDA50B2" else v2
        self.ent_voltage.delete(0, tk.END)
        self.ent_voltage.insert(0, f"{v:.5f}")

    def add_point(self) -> None:
        try:
            wl = float(self.ent_wl.get())
            power = float(self.ent_power.get())
            voltage = float(self.ent_voltage.get())
            if wl <= 0 or power <= 0:
                raise ValueError("wavelength and power must be > 0")
        except ValueError as e:
            messagebox.showerror("Add Point", f"Check wavelength, power and voltage: {e}", parent=self.win)
            return
        name = self.var_detector.get()
        path = dict(self.app.path_model.installed)
        t_path = self.app.path_model.transmission(name, wl, path)
        if t_path < MIN_PATH_TRANSMISSION:
            messagebox.showerror(
                "Add Point",
                f"Only {t_path * 100:.2f}% of the {name} beam reaches the objective at {wl:g} nm "
                f"with {self.app.path_model.label(path)}. Check the optics selection.",
                parent=self.win,
            )
            return
        self._detector().points.append(CalibrationPoint(wl, power, voltage, path))
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
                    f"{p.wavelength_nm:g}", f"{p.power_mw:.5g}", f"{p.voltage_v:.5f}", f"{k:.4g}",
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

