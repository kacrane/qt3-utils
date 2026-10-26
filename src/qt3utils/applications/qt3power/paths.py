"""Optics between the beam sampler and the objective, for qt3power.

The detectors read a sampled beam. The rest of the beam goes through swappable
optics before the objective, so

    power_at_objective = power_seen_by_detector_calibration * T_path(wavelength)

A path is a set of "slots" (e.g. the swappable dichroic). Each slot holds one
optic, or "None". Each detector's beam either reflects off or transmits through
the optic in each slot. T_path is the product over slots of the optic's
reflectance or transmission (as a fraction) at that wavelength.

Optic curves are CSV files in the optics/ folder next to this module, named
<OPTIC>.csv, with columns:
    wavelength_nm,T_unpolarized,R_unpolarized,T_p,R_p,T_s,R_s   (percent)
Drop in another CSV in that format to add an optic.

The slot layout can be overridden with ~/.qt3utils/qt3power_paths.json, in the
same shape as DEFAULT_PATH_CONFIG.
"""

import copy
import csv
import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from qt3utils.applications.qt3power.calibration import band_wavelengths, optics_label

OPTICS_DIR = Path(__file__).parent / "optics"
PATH_CONFIG_PATH = Path.home() / ".qt3utils" / "qt3power_paths.json"
NO_OPTIC = "None"
POLARIZATIONS = ("unpolarized", "p", "s")

DEFAULT_PATH_CONFIG: dict = {
    # Slot name -> optics that can be installed there, and the default.
    "slots": {
        "Dichroic": {"options": [NO_OPTIC, "DMLP950", "DMLP1180"], "default": "DMLP950"},
    },
    # Per detector: how its beam interacts with each slot ("reflect" or "transmit"),
    # and the polarization used to pick the optic curve.
    "detectors": {
        "PDA100A2": {"modes": {"Dichroic": "reflect"}, "polarization": "unpolarized"},
        "PDA50B2": {"modes": {"Dichroic": "transmit"}, "polarization": "unpolarized"},
    },
}


def load_path_config(path: Path = PATH_CONFIG_PATH) -> dict:
    """DEFAULT_PATH_CONFIG, replaced by the user's file if it exists and parses."""
    path = Path(path).expanduser()
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return copy.deepcopy(DEFAULT_PATH_CONFIG)


def available_optics() -> List[str]:
    return sorted(p.stem for p in OPTICS_DIR.glob("*.csv"))


@lru_cache(maxsize=None)
def _optic_table(optic: str) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
    csv_path = OPTICS_DIR / f"{optic}.csv"
    with open(csv_path) as f:
        rows = [r for r in csv.reader(f) if r and not r[0].startswith("#")]
    header, data = rows[0], np.array(rows[1:], dtype=float)
    columns = {name: data[:, i] for i, name in enumerate(header)}
    return columns["wavelength_nm"], columns


def optic_fraction(optic: str, mode: str, polarization: str, wavelength_nm: float) -> float:
    """Fraction (0-1) of light reflected or transmitted by the optic."""
    if optic == NO_OPTIC:
        return 1.0
    if mode not in ("reflect", "transmit"):
        raise ValueError(f"mode must be 'reflect' or 'transmit', not {mode!r}")
    wl, columns = _optic_table(optic)
    key = ("R_" if mode == "reflect" else "T_") + polarization
    return float(np.clip(np.interp(wavelength_nm, wl, columns[key]) / 100.0, 0.0, 1.0))


def optic_range(optic: str) -> Optional[Tuple[float, float]]:
    if optic == NO_OPTIC:
        return None
    wl, _ = _optic_table(optic)
    return float(wl[0]), float(wl[-1])


class PathModel:
    """Path transmission per detector, given which optic is in each slot."""

    def __init__(self, config: Optional[dict] = None) -> None:
        self.config = config if config is not None else load_path_config()
        self.installed: Dict[str, str] = {
            slot: spec.get("default", NO_OPTIC) for slot, spec in self.config["slots"].items()
        }

    @property
    def slots(self) -> Dict[str, dict]:
        return self.config["slots"]

    def set_installed(self, installed: Dict[str, str]) -> None:
        """Apply saved slot choices, ignoring slots or optics no longer configured."""
        for slot, optic in installed.items():
            if slot in self.slots and optic in self.slots[slot]["options"]:
                self.installed[slot] = optic

    def _detector_spec(self, detector: str) -> dict:
        return self.config["detectors"].get(detector, {"modes": {}, "polarization": "unpolarized"})

    def transmission(
        self,
        detector: str,
        wavelength_nm: float,
        installed: Optional[Dict[str, str]] = None,
        linewidth_nm: float = 0.0,
    ) -> float:
        """T_path for this detector, averaged over a flat-top band of linewidth_nm.

        installed defaults to the current slot choices.
        """
        installed = self.installed if installed is None else installed
        spec = self._detector_spec(detector)
        pol = spec.get("polarization", "unpolarized")
        totals = []
        for wl in band_wavelengths(wavelength_nm, linewidth_nm):
            t = 1.0
            for slot, mode in spec.get("modes", {}).items():
                optic = installed.get(slot, NO_OPTIC)
                t *= optic_fraction(optic, mode, pol, wl)
            totals.append(t)
        return float(np.mean(totals))

    def describe(
        self,
        detector: str,
        wavelength_nm: float,
        installed: Optional[Dict[str, str]] = None,
        linewidth_nm: float = 0.0,
    ) -> List[str]:
        """One line per slot for tooltips, e.g. 'Dichroic: DMLP950 reflect (unpolarized) 97.1%'."""
        installed = self.installed if installed is None else installed
        spec = self._detector_spec(detector)
        pol = spec.get("polarization", "unpolarized")
        lines = []
        for slot, mode in spec.get("modes", {}).items():
            optic = installed.get(slot, NO_OPTIC)
            if optic == NO_OPTIC:
                lines.append(f"{slot}: none")
                continue
            frac = float(np.mean([optic_fraction(optic, mode, pol, w) for w in band_wavelengths(wavelength_nm, linewidth_nm)]))
            lines.append(f"{slot}: {optic} {mode} ({pol}) {frac * 100:.1f}%")
        return lines

    def label(self, installed: Optional[Dict[str, str]] = None) -> str:
        installed = self.installed if installed is None else installed
        return optics_label(installed)
