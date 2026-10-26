"""Wavelength-dependent voltage-to-power conversion for qt3power.

Each detector has a conversion factor k(wavelength) in V/mW, so that
    power_mW = (voltage_V - offset_V) / k

Default model (no calibration loaded):
    k(wl) = ref_factor * R(wl) / R(ref_wavelength)
where R is the detector's responsivity curve (A/W) from Thorlabs raw data
(PDA50B2 and PDA100A2). For a detector without a curve, R is flat and k is
just ref_factor at every wavelength.

Calibration model (measured points loaded):
    At each measured wavelength the points (power, voltage) are fit to
    V = k * P + offset (through the origin if only one distinct power).
    The ratio c = k_measured / k_default is interpolated linearly between
    measured wavelengths and held constant outside them, then
    k(wl) = c(wl) * k_default(wl).
    So the measured points are reproduced exactly, the Thorlabs curve shape is
    used between and beyond them, and a single calibration point rescales the
    whole curve.

Optics path (see paths.py):
    Displayed power is power at the objective. Each calibration point records
    which optics were installed when it was measured; its known power is divided
    by that path's transmission T_cal before fitting, so k describes the
    detector alone. The displayed power is then
        power_mW = (voltage_V - offset_V) / k * T_path_now
    so swapping an optic rescales the calibration instead of invalidating it.

Linewidth (broadband sources such as the SuperK CHROMATUNE):
    For a flat-top spectrum of width w around the center wavelength, the
    detector factor and the path transmission are averaged over the band
    instead of taken at the center. Each calibration point records its
    linewidth so it is normalized the same way.

Pure Python/numpy, no tkinter or nidaqmx, so it can be tested off the lab PC.
"""

import json
import datetime
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

DEFAULT_CALIBRATION_PATH = Path.home() / ".qt3utils" / "qt3power_calibration.json"  # legacy single file
CALIBRATIONS_DIR = Path.home() / ".qt3utils" / "qt3power_calibrations"
CALIBRATION_FORMAT = "qt3power_calibration_v1"
SETTINGS_PATH = Path.home() / ".qt3utils" / "qt3power_settings.json"

# Thorlabs PDA50B2 (and PDA30B2) typical responsivity (A/W) vs wavelength (nm),
# from Thorlabs "Ge Biased Photodetectors Responsivity" raw data (ge_biased_responsivity_5.xlsx).
PDA50B2_RESPONSIVITY: Tuple[Tuple[float, float], ...] = (
    (800, 0.22508), (810, 0.24665), (820, 0.26523), (830, 0.26213),
    (840, 0.26513), (850, 0.27525), (860, 0.29272), (870, 0.30612),
    (880, 0.29395), (890, 0.29197), (900, 0.31639), (910, 0.34314),
    (920, 0.33874), (930, 0.35345), (940, 0.36875), (950, 0.37500),
    (960, 0.38370), (970, 0.39427), (980, 0.41518), (990, 0.42412),
    (1000, 0.43576), (1010, 0.43922), (1020, 0.45196), (1030, 0.46431),
    (1040, 0.47640), (1050, 0.48833), (1060, 0.49845), (1070, 0.51611),
    (1080, 0.52045), (1090, 0.53210), (1100, 0.54247), (1110, 0.54571),
    (1120, 0.55472), (1130, 0.56180), (1140, 0.56882), (1150, 0.58110),
    (1160, 0.58872), (1170, 0.60078), (1180, 0.60694), (1190, 0.61870),
    (1200, 0.62561), (1210, 0.63318), (1220, 0.64138), (1230, 0.64911),
    (1240, 0.65293), (1250, 0.65686), (1260, 0.66692), (1270, 0.67623),
    (1280, 0.68537), (1290, 0.69418), (1300, 0.71071), (1310, 0.70957),
    (1320, 0.71724), (1330, 0.72821), (1340, 0.72881), (1350, 0.73514),
    (1360, 0.73725), (1370, 0.70826), (1380, 0.74667), (1390, 0.72364),
    (1400, 0.74545), (1410, 0.75398), (1420, 0.76239), (1430, 0.78291),
    (1440, 0.78974), (1450, 0.80000), (1460, 0.82617), (1470, 0.81905),
    (1480, 0.82667), (1490, 0.83774), (1500, 0.84074), (1510, 0.85138),
    (1520, 0.85405), (1530, 0.87207), (1540, 0.87857), (1550, 0.87788),
    (1560, 0.86667), (1570, 0.83860), (1580, 0.78947), (1590, 0.71930),
    (1600, 0.64561), (1610, 0.59115), (1620, 0.54513), (1630, 0.51071),
    (1640, 0.48288), (1650, 0.45018), (1660, 0.42606), (1670, 0.40636),
    (1680, 0.37981), (1690, 0.35736), (1700, 0.33524), (1710, 0.31423),
    (1720, 0.29608), (1730, 0.27880), (1740, 0.25818), (1750, 0.24016),
    (1760, 0.22004), (1770, 0.20042), (1780, 0.18139), (1790, 0.16173),
    (1800, 0.14279),
)

# Thorlabs PDA100A2 (Si) typical responsivity (A/W) vs wavelength (nm),
# from Thorlabs "PDA100A2 Responsivity" raw data (pda100a2_responsivity-2.xlsx).
PDA100A2_RESPONSIVITY: Tuple[Tuple[float, float], ...] = (
    (320, 0.174), (330, 0.181), (340, 0.184), (350, 0.181), (360, 0.175),
    (370, 0.17), (380, 0.175), (390, 0.186), (400, 0.193), (420, 0.212),
    (440, 0.23), (460, 0.248), (480, 0.269), (500, 0.289), (520, 0.309),
    (540, 0.332), (560, 0.355), (580, 0.377), (600, 0.399), (620, 0.424),
    (640, 0.448), (660, 0.468), (680, 0.488), (700, 0.509), (720, 0.529),
    (740, 0.548), (760, 0.566), (780, 0.585), (800, 0.601), (820, 0.616),
    (840, 0.636), (860, 0.651), (880, 0.667), (900, 0.678), (920, 0.696),
    (940, 0.711), (960, 0.721), (980, 0.726), (1000, 0.708), (1020, 0.653),
    (1040, 0.553), (1060, 0.415), (1080, 0.295), (1100, 0.2),
)

RESPONSIVITY_CURVES: Dict[str, Tuple[Tuple[float, float], ...]] = {
    "PDA50B2": PDA50B2_RESPONSIVITY,
    "PDA100A2": PDA100A2_RESPONSIVITY,
}

# Wavelength at which the legacy fixed 7.0 V/mW factor is assumed to hold.
DEFAULT_REF_WAVELENGTH_NM: Dict[str, float] = {
    "PDA50B2": 1550.0,
    "PDA100A2": 980.0,
}


# Number of wavelengths used to average curves over a linewidth.
BAND_SAMPLES = 41


def band_wavelengths(center_nm: float, linewidth_nm: float) -> np.ndarray:
    """Wavelengths spanning a flat-top band; just the center for zero linewidth."""
    if linewidth_nm <= 0:
        return np.array([center_nm])
    return np.linspace(center_nm - linewidth_nm / 2, center_nm + linewidth_nm / 2, BAND_SAMPLES)


# Points whose path let through less than this are ignored in fits: dividing by
# a near-zero transmission would blow up any error in the curve.
MIN_PATH_TRANSMISSION = 0.01

# Gain switch positions on the Thorlabs PDA50B2 and PDA100A2 (8 positions, 10 dB steps).
GAIN_OPTIONS_DB: Dict[str, Tuple[int, ...]] = {
    "PDA50B2": tuple(range(0, 80, 10)),
    "PDA100A2": tuple(range(0, 80, 10)),
}


@dataclass
class CalibrationPoint:
    wavelength_nm: float
    power_mw: float  # known power at the objective
    voltage_v: float
    path: Dict[str, str] = field(default_factory=dict)  # slot -> optic installed when measured
    linewidth_nm: float = 0.0  # source linewidth when measured (0 = narrow line)
    gain_db: Optional[int] = None  # detector gain switch when measured (None = not recorded)


def _unity_transmission(_point: "CalibrationPoint") -> float:
    return 1.0


@dataclass
class DetectorCalibration:
    """Conversion for one detector: default responsivity model plus optional measured points."""

    name: str
    ref_factor_v_per_mw: float = 7.0
    ref_wavelength_nm: Optional[float] = None
    points: List[CalibrationPoint] = field(default_factory=list)
    # Path transmission at each point's wavelength and recorded path. Set by the
    # app from its PathModel; not saved.
    point_transmission: Callable[[CalibrationPoint], float] = field(
        default=_unity_transmission, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.ref_wavelength_nm is None:
            self.ref_wavelength_nm = DEFAULT_REF_WAVELENGTH_NM.get(self.name, 1550.0)

    @property
    def curve(self) -> Optional[Tuple[Tuple[float, float], ...]]:
        return RESPONSIVITY_CURVES.get(self.name)

    @property
    def is_calibrated(self) -> bool:
        return len(self.points) > 0

    def responsivity(self, wavelength_nm: float) -> float:
        """Default responsivity in A/W (1.0 if the detector has no curve). Clamped to the curve's range."""
        curve = self.curve
        if curve is None:
            return 1.0
        wl, r = np.array(curve).T
        return float(np.interp(wavelength_nm, wl, r))

    def in_curve_range(self, wavelength_nm: float) -> bool:
        curve = self.curve
        if curve is None:
            return True
        return curve[0][0] <= wavelength_nm <= curve[-1][0]

    def default_factor(self, wavelength_nm: float) -> float:
        """Default V/mW from the responsivity curve, scaled so ref_wavelength gives ref_factor."""
        return (
            self.ref_factor_v_per_mw
            * self.responsivity(wavelength_nm)
            / self.responsivity(self.ref_wavelength_nm)
        )

    def band_factor(self, wavelength_nm: float, linewidth_nm: float) -> float:
        """Band-averaged default factor divided by the factor at the center (1.0 for zero width)."""
        if linewidth_nm <= 0:
            return 1.0
        band = band_wavelengths(wavelength_nm, linewidth_nm)
        return float(np.mean([self.default_factor(w) for w in band]) / self.default_factor(wavelength_nm))

    def fit_by_wavelength(self) -> List[Tuple[float, float, float, int]]:
        """
        Fit the measured points at each wavelength.

        Returns:
            Sorted list of (wavelength_nm, k_v_per_mw, offset_v, n_points).
        """
        groups: Dict[float, List[Tuple[float, float]]] = {}
        for p in self.points:
            t = self.point_transmission(p)
            if t < MIN_PATH_TRANSMISSION:
                continue
            # Power the detector model sees at the center wavelength: power at the
            # objective with the path removed, corrected for the linewidth.
            p_eq = p.power_mw / t * self.band_factor(p.wavelength_nm, p.linewidth_nm)
            groups.setdefault(round(float(p.wavelength_nm), 3), []).append((p_eq, p.voltage_v))

        fits = []
        for wl in sorted(groups):
            pts = groups[wl]
            P = np.array([pv[0] for pv in pts], dtype=float)
            V = np.array([pv[1] for pv in pts], dtype=float)
            if len(np.unique(P)) >= 2:
                k, offset = np.polyfit(P, V, 1)
            else:
                k = float(np.sum(V * P) / np.sum(P * P)) if np.any(P) else float("nan")
                offset = 0.0
            fits.append((wl, float(k), float(offset), len(pts)))
        return fits

    def factor_and_offset(self, wavelength_nm: float) -> Tuple[float, float]:
        """Return (k in V/mW, offset in V) at this wavelength."""
        k_default = self.default_factor(wavelength_nm)
        fits = [f for f in self.fit_by_wavelength() if np.isfinite(f[1]) and f[1] != 0]
        if not fits:
            return k_default, 0.0
        wls = np.array([f[0] for f in fits])
        ratios = np.array([f[1] / self.default_factor(f[0]) for f in fits])
        offsets = np.array([f[2] for f in fits])
        ratio = float(np.interp(wavelength_nm, wls, ratios))
        offset = float(np.interp(wavelength_nm, wls, offsets))
        return ratio * k_default, offset

    def voltage_to_mw(
        self,
        voltage_v: float,
        wavelength_nm: float,
        path_transmission: float = 1.0,
        linewidth_nm: float = 0.0,
    ) -> float:
        """Power in mW after a path with this (band-averaged) transmission; 1.0 = power the detector model sees."""
        k, offset = self.factor_and_offset(wavelength_nm)
        k *= self.band_factor(wavelength_nm, linewidth_nm)
        return (voltage_v - offset) / k * path_transmission

    def calibrated_range(self) -> Optional[Tuple[float, float]]:
        if not self.points:
            return None
        wls = [p.wavelength_nm for p in self.points]
        return min(wls), max(wls)

    def to_dict(self) -> dict:
        return {
            "ref_factor_v_per_mw": self.ref_factor_v_per_mw,
            "ref_wavelength_nm": self.ref_wavelength_nm,
            "points": [asdict(p) for p in self.points],
        }

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "DetectorCalibration":
        return cls(
            name=name,
            ref_factor_v_per_mw=float(d.get("ref_factor_v_per_mw", 7.0)),
            ref_wavelength_nm=d.get("ref_wavelength_nm"),
            points=[
                CalibrationPoint(
                    wavelength_nm=float(p["wavelength_nm"]),
                    power_mw=float(p["power_mw"]),
                    voltage_v=float(p["voltage_v"]),
                    path=dict(p.get("path", {})),
                    linewidth_nm=float(p.get("linewidth_nm", 0.0)),
                    gain_db=None if p.get("gain_db") is None else int(p["gain_db"]),
                )
                for p in d.get("points", [])
            ],
        )


def save_calibration(path: Path, det: DetectorCalibration, notes: str = "") -> None:
    """Write one detector's calibration to its own file."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "format": CALIBRATION_FORMAT,
        "saved": datetime.datetime.now().isoformat(timespec="seconds"),
        "detector": det.name,
        "notes": notes,
        "detectors": {det.name: det.to_dict()},
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _read_calibration_file(path: Path) -> dict:
    with open(Path(path).expanduser()) as f:
        data = json.load(f)
    if data.get("format") != CALIBRATION_FORMAT:
        raise ValueError("not a qt3power calibration file")
    return data


def load_calibration(path: Path, name: str) -> DetectorCalibration:
    """Read the calibration for detector `name` from a file.

    Raises:
        ValueError: the file has no calibration for that detector.
    """
    data = _read_calibration_file(path)
    d = data.get("detectors", {}).get(name)
    if d is None:
        other = ", ".join(data.get("detectors", {})) or "no detector"
        raise ValueError(f"This file is a calibration for {other}, not {name}.")
    return DetectorCalibration.from_dict(name, d)


def optics_label(path: Dict[str, str]) -> str:
    """e.g. {'Dichroic': 'DMLP950'} -> 'Dichroic=DMLP950'; empty -> '(optics not recorded)'."""
    if not path:
        return "(optics not recorded)"
    return ", ".join(f"{slot}={optic}" for slot, optic in sorted(path.items()))


def calibration_optics(det: DetectorCalibration) -> List[str]:
    """Sorted distinct optics labels of a detector's calibration points."""
    return sorted({optics_label(p.path) for p in det.points})


def default_calibration_filename(
    name: str, installed: Dict[str, str], when: Optional[datetime.datetime] = None
) -> str:
    when = when or datetime.datetime.now()
    optics = "_".join(optic for _slot, optic in sorted(installed.items())) or "no-optics"
    return f"qt3power_cal_{name}_{optics}_{when:%Y%m%d-%H%M}.json"


@dataclass
class CalibrationFileInfo:
    path: Path
    saved: datetime.datetime
    optics: Dict[str, List[str]]  # detector -> optics labels of its points (detectors with points only)


def scan_calibration_files(folders: List[Path]) -> List[CalibrationFileInfo]:
    """qt3power calibration files in these folders (not recursive), newest first."""
    found: Dict[Path, CalibrationFileInfo] = {}
    for folder in folders:
        folder = Path(folder).expanduser()
        if not folder.is_dir():
            continue
        for f in folder.glob("*.json"):
            try:
                data = _read_calibration_file(f)
                try:
                    saved = datetime.datetime.fromisoformat(data["saved"])
                except Exception:
                    saved = datetime.datetime.fromtimestamp(f.stat().st_mtime)
                optics = {}
                for n, d in data.get("detectors", {}).items():
                    det = DetectorCalibration.from_dict(n, d)
                    if det.points:
                        optics[n] = calibration_optics(det)
                found[f.resolve()] = CalibrationFileInfo(f, saved, optics)
            except Exception:
                continue  # unreadable or not a calibration: ignore
    return sorted(found.values(), key=lambda i: i.saved, reverse=True)


def newest_calibration_file(
    library: List[CalibrationFileInfo], name: str, installed: Dict[str, str]
) -> Optional[CalibrationFileInfo]:
    """Newest file holding a calibration for `name` measured only with the installed optics."""
    current = [optics_label(installed)]
    return next((i for i in library if i.optics.get(name) == current), None)


# Calibration status shown in the main window, per detector.
STATUS_CURRENT = "current"  # newest saved calibration for the installed optics (or unsaved, same optics)
STATUS_OLDER = "older"  # right optics, but a newer file with the same optics exists
STATUS_WRONG_OPTICS = "wrong_optics"  # measured with different optics than installed
STATUS_DEFAULT = "default"  # no calibration: Thorlabs curves


def calibration_status(
    det: DetectorCalibration,
    loaded_path: Optional[Path],
    dirty: bool,
    installed: Dict[str, str],
    library: List[CalibrationFileInfo],
) -> Tuple[str, str]:
    """Return (status, text) describing the calibration in use for one detector."""
    optics = calibration_optics(det)
    if not optics:
        return STATUS_DEFAULT, "Thorlabs default curve"
    name = loaded_path.name if loaded_path is not None else "unsaved points"
    if dirty and loaded_path is not None:
        name += " (unsaved changes)"
    current = optics_label(installed)
    if optics != [current]:
        return STATUS_WRONG_OPTICS, f"{name} - measured with {'; '.join(optics)}, but {current} is installed"
    if loaded_path is None or dirty:
        return STATUS_CURRENT, f"{name} - {current}"
    newest = newest_calibration_file(library, det.name, installed)
    loaded_saved = next(
        (i.saved for i in library if i.path.resolve() == Path(loaded_path).resolve()), None
    )
    if newest is None or loaded_saved is None or loaded_saved >= newest.saved:
        return STATUS_CURRENT, f"{name} - newest"
    return STATUS_OLDER, f"{name} - older; newest is {newest.path.name}"


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    """App settings (last wavelengths, installed optics); empty if missing or unreadable."""
    try:
        with open(Path(path).expanduser()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def update_settings(values: dict, path: Path = SETTINGS_PATH) -> None:
    """Merge values into the settings file, keeping keys not given."""
    path = Path(path).expanduser()
    data = load_settings(path)
    data.update(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_last_wavelengths(path: Path = SETTINGS_PATH) -> Dict[str, float]:
    """Last wavelength set for each detector; a detector never set gets its reference wavelength."""
    wavelengths = dict(DEFAULT_REF_WAVELENGTH_NM)
    saved = load_settings(path).get("wavelength_nm", {})
    if isinstance(saved, dict):
        for name, wl in saved.items():
            try:
                if float(wl) > 0:
                    wavelengths[name] = float(wl)
            except (TypeError, ValueError):
                pass
    return wavelengths


def load_last_gains(path: Path = SETTINGS_PATH) -> Dict[str, Optional[int]]:
    """Last gain (dB) set for each detector; None if never set."""
    saved = load_settings(path).get("gain_db", {})
    gains: Dict[str, Optional[int]] = {name: None for name in GAIN_OPTIONS_DB}
    if isinstance(saved, dict):
        for name, g in saved.items():
            if name in gains and g in GAIN_OPTIONS_DB[name]:
                gains[name] = int(g)
    return gains


def save_last_wavelengths(wavelengths: Dict[str, float], path: Path = SETTINGS_PATH) -> None:
    update_settings({"wavelength_nm": wavelengths}, path)
