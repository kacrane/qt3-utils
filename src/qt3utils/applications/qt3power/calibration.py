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

Pure Python/numpy, no tkinter or nidaqmx, so it can be tested off the lab PC.
"""

import json
import datetime
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

DEFAULT_CALIBRATION_PATH = Path.home() / ".qt3utils" / "qt3power_calibration.json"
SETTINGS_PATH = Path.home() / ".qt3utils" / "qt3power_settings.json"
DEFAULT_WAVELENGTH_NM = 1550.0

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


@dataclass
class CalibrationPoint:
    wavelength_nm: float
    power_mw: float
    voltage_v: float


@dataclass
class DetectorCalibration:
    """Conversion for one detector: default responsivity model plus optional measured points."""

    name: str
    ref_factor_v_per_mw: float = 7.0
    ref_wavelength_nm: Optional[float] = None
    points: List[CalibrationPoint] = field(default_factory=list)

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

    def fit_by_wavelength(self) -> List[Tuple[float, float, float, int]]:
        """
        Fit the measured points at each wavelength.

        Returns:
            Sorted list of (wavelength_nm, k_v_per_mw, offset_v, n_points).
        """
        groups: Dict[float, List[CalibrationPoint]] = {}
        for p in self.points:
            groups.setdefault(round(float(p.wavelength_nm), 3), []).append(p)

        fits = []
        for wl in sorted(groups):
            pts = groups[wl]
            P = np.array([p.power_mw for p in pts], dtype=float)
            V = np.array([p.voltage_v for p in pts], dtype=float)
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

    def voltage_to_mw(self, voltage_v: float, wavelength_nm: float) -> float:
        k, offset = self.factor_and_offset(wavelength_nm)
        return (voltage_v - offset) / k

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
            points=[CalibrationPoint(**p) for p in d.get("points", [])],
        )


def save_calibrations(path: Path, detectors: Dict[str, DetectorCalibration], notes: str = "") -> None:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "format": "qt3power_calibration_v1",
        "saved": datetime.datetime.now().isoformat(timespec="seconds"),
        "notes": notes,
        "detectors": {name: det.to_dict() for name, det in detectors.items()},
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_calibrations(path: Path) -> Dict[str, DetectorCalibration]:
    with open(Path(path).expanduser()) as f:
        data = json.load(f)
    return {
        name: DetectorCalibration.from_dict(name, d)
        for name, d in data.get("detectors", {}).items()
    }


def load_last_wavelength(path: Path = SETTINGS_PATH) -> float:
    """Last wavelength set in qt3power, or DEFAULT_WAVELENGTH_NM if none was saved."""
    try:
        with open(Path(path).expanduser()) as f:
            wl = float(json.load(f)["wavelength_nm"])
        return wl if wl > 0 else DEFAULT_WAVELENGTH_NM
    except Exception:
        return DEFAULT_WAVELENGTH_NM


def save_last_wavelength(wavelength_nm: float, path: Path = SETTINGS_PATH) -> None:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"wavelength_nm": wavelength_nm}, f)
