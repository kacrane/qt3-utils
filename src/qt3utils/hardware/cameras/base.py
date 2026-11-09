"""
Common camera interface for QT3 Microscope's Camera tab.

Every camera driver (Thorlabs, NIT, fake) offers the same few calls, so the
tab doesn't care which camera a setup has. All calls are made from one
thread (the tab's grab thread), never from the Tk thread.
"""

import dataclasses
import importlib


@dataclasses.dataclass
class Frame:
    image: object          # 2-D numpy array (uint16 for 16-bit cameras)
    index: int             # frame counter from the camera (or our own count)
    time: float            # time.time() when the frame arrived


class Camera:
    """Base class. Exposure in milliseconds, gain in dB."""

    model = "camera"
    bit_depth = 16

    def open(self):
        raise NotImplementedError

    def close(self):
        pass

    def info(self):
        """Short dict shown in the tab and saved with snapshots."""
        return {"model": self.model}

    # exposure and gain
    def exposure_range_ms(self):
        return (0.01, 1000.0)

    def get_exposure_ms(self):
        raise NotImplementedError

    def set_exposure_ms(self, ms):
        raise NotImplementedError

    def gain_range_db(self):
        """(min, max) in dB, or None when the camera has no gain setting."""
        return None

    def get_gain_db(self):
        return None

    def set_gain_db(self, db):
        pass

    # acquisition
    def start(self):
        """Start continuous acquisition."""
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def read(self, timeout_s=0.5):
        """Next Frame, or None if nothing arrived within timeout_s."""
        raise NotImplementedError


def make_camera(cfg):
    """Build a camera from a profile entry: {driver: thorlabs_tsi | nit | fake, ...}."""
    cfg = dict(cfg or {})
    driver = cfg.pop("driver", "fake")
    modules = {"thorlabs_tsi": ("thorlabs_tsi", "ThorlabsTSICamera"),
               "nit": ("nit", "NITCamera"),
               "fake": ("fake", "FakeCamera")}
    if driver not in modules:
        raise ValueError(f"unknown camera driver {driver!r} (use one of {', '.join(modules)})")
    mod, cls = modules[driver]
    if driver == "thorlabs_tsi":
        # Thorlabs' own Python toolkit if installed, otherwise pylablib (same DLL).
        try:
            import thorlabs_tsi_sdk  # noqa: F401
        except ImportError:
            mod, cls = "thorlabs_pylablib", "ThorlabsPylablibCamera"
    module = importlib.import_module(f"qt3utils.hardware.cameras.{mod}")
    for k in ("label", "save_dir", "display_fps"):  # tab settings, not driver settings
        cfg.pop(k, None)
    return getattr(module, cls)(**cfg)
