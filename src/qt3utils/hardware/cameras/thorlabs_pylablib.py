"""
Thorlabs scientific cameras (e.g. the VQLM's CS165MU) through pylablib.

Used when Thorlabs' own Python toolkit (thorlabs_tsi_sdk) isn't installed.
pylablib drives the same ThorCam DLL (thorlabs_tsi_camera_sdk.dll) and
finds it in the ThorCam folder by itself. Install: pip install pylablib
Close ThorCam first: only one program can open the camera at a time.
"""

import logging
import time

import numpy as np

from qt3utils.hardware.cameras.base import Camera, Frame

logger = logging.getLogger(__name__)


class ThorlabsPylablibCamera(Camera):
    model = "Thorlabs camera"

    def __init__(self, serial=None, dll_dir=None, exposure_ms=10.0, gain_db=0.0, **_ignored):
        self.serial = str(serial) if serial else None
        self.dll_dir = dll_dir
        self._start_exposure = exposure_ms
        self._start_gain = gain_db
        self.cam = None
        self._count = 0
        self._last_index = None

    def open(self):
        try:
            import pylablib as pll
            from pylablib.devices import Thorlabs
        except ImportError as e:
            raise RuntimeError("neither thorlabs_tsi_sdk nor pylablib is installed "
                               "(pip install pylablib)") from e
        if self.dll_dir:
            pll.par["devices/dlls/thorlabs_tlcam"] = self.dll_dir
        found = [str(s) for s in Thorlabs.list_cameras_tlcam()]
        if not found:
            raise RuntimeError("no Thorlabs camera found: is it plugged in, and is ThorCam closed?")
        serial = self.serial or found[0]
        if serial not in found:
            raise RuntimeError(f"camera {serial} not found (found {', '.join(found)})")
        self.cam = Thorlabs.ThorlabsTLCamera(serial)
        self.serial = serial
        info = self.cam.get_device_info()
        self.model = f"Thorlabs {info.model}".strip()
        self.bit_depth = int(self.cam.get_sensor_info().bit_depth)
        self.set_exposure_ms(self._start_exposure)
        if self.gain_range_db() is not None:
            self.set_gain_db(self._start_gain)

    def close(self):
        if self.cam is not None:
            try:
                self.stop()
                self.cam.close()
            except Exception as e:
                logger.warning("camera close: %r", e)
            self.cam = None

    def info(self):
        d = {"model": self.model, "serial": self.serial, "bit_depth": self.bit_depth, "driver": "pylablib"}
        if self.cam is not None:
            w, h = self.cam.get_detector_size()
            d.update(width=w, height=h)
        return d

    def exposure_range_ms(self):
        try:
            from pylablib.devices.Thorlabs.TLCamera import lib
            lo, hi = lib.tl_camera_get_exposure_time_range(self.cam.handle)
            return (lo / 1000.0, hi / 1000.0)
        except Exception:
            return (0.04, 20000.0)  # CS165MU data sheet range

    def get_exposure_ms(self):
        return self.cam.get_exposure() * 1000.0

    def set_exposure_ms(self, ms):
        lo, hi = self.exposure_range_ms()
        self.cam.set_exposure(min(max(ms, lo), hi) / 1000.0)

    def gain_range_db(self):
        try:
            lo, hi = self.cam.get_gain_range()
        except Exception:
            return None
        return None if hi <= lo else (lo, hi)

    def get_gain_db(self):
        return self.cam.get_gain() if self.gain_range_db() is not None else None

    def set_gain_db(self, db):
        if self.gain_range_db() is not None:
            self.cam.set_gain(float(db))

    def start(self):
        if not self.cam.acquisition_in_progress():
            self.cam.setup_acquisition(nframes=10)
            self.cam.start_acquisition()

    def stop(self):
        if self.cam is not None and self.cam.acquisition_in_progress():
            self.cam.stop_acquisition()

    def read(self, timeout_s=0.5):
        try:
            self.cam.wait_for_frame(since="lastread", timeout=timeout_s)
        except Exception as e:  # pylablib raises a timeout error when nothing came
            if "imeout" in type(e).__name__ or "imeout" in str(e):
                return None
            raise
        img = self.cam.read_newest_image()
        if img is None:
            return None
        self._count += 1
        return Frame(np.asarray(img, dtype=np.uint16), self._count, time.time())
