"""
Thorlabs scientific cameras (e.g. the VQLM's CS165MU) through the Thorlabs
Scientific Camera SDK (the "Python Toolkit" that comes with ThorCam).

Needs, on the lab PC:
- ThorCam installed (it installs the camera driver and the SDK DLLs).
- The Python package thorlabs_tsi_sdk installed in the Python that runs QT3
  Microscope (pip install the thorlabs_tsi_camera_python_sdk_package.zip
  from ThorCam's "Scientific Camera Support" folder).
- ThorCam closed: only one program can open the camera at a time.

dll_dir: the folder holding thorlabs_tsi_camera_sdk.dll. Left empty, the
usual ThorCam folders are tried.
"""

import logging
import os
import sys
import time

import numpy as np

from qt3utils.hardware.cameras.base import Camera, Frame

logger = logging.getLogger(__name__)

DLL_NAME = "thorlabs_tsi_camera_sdk.dll"
DLL_GUESSES = [
    r"C:\Program Files\Thorlabs\Scientific Imaging\ThorCam",
    r"C:\Program Files\Thorlabs\ThorImageCAM\Bin",
    r"C:\Program Files\Thorlabs\Scientific Imaging\Scientific Camera Support\Scientific Camera Interfaces\SDK\Native Toolkit\dlls\Native_64_lib",
]

_sdk = None  # the SDK may only be opened once per program


def _add_dll_dir(dll_dir):
    if sys.platform != "win32":
        return
    folders = [dll_dir] if dll_dir else DLL_GUESSES
    for folder in folders:
        if folder and os.path.exists(os.path.join(folder, DLL_NAME)):
            os.add_dll_directory(folder)
            os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
            logger.info("Thorlabs camera DLLs from %s", folder)
            return
    raise RuntimeError(f"could not find {DLL_NAME}; put its folder in the profile as dll_dir "
                       f"(tried: {'; '.join(folders)})")


class ThorlabsTSICamera(Camera):
    model = "Thorlabs camera"

    def __init__(self, serial=None, dll_dir=None, exposure_ms=10.0, gain_db=0.0, **_ignored):
        self.serial = str(serial) if serial else None
        self.dll_dir = dll_dir
        self._start_exposure = exposure_ms
        self._start_gain = gain_db
        self.cam = None
        self._armed = False

    def open(self):
        global _sdk
        _add_dll_dir(self.dll_dir)
        try:
            from thorlabs_tsi_sdk.tl_camera import TLCameraSDK
        except ImportError as e:
            raise RuntimeError("the thorlabs_tsi_sdk Python package is not installed "
                               "(pip install the Python Toolkit zip from ThorCam)") from e
        if _sdk is None:
            _sdk = TLCameraSDK()
        found = _sdk.discover_available_cameras()
        if not found:
            raise RuntimeError("no Thorlabs camera found: is it plugged in, and is ThorCam closed?")
        serial = self.serial or found[0]
        if serial not in found:
            raise RuntimeError(f"camera {serial} not found (found {', '.join(found)})")
        self.cam = _sdk.open_camera(serial)
        self.serial = serial
        self.model = getattr(self.cam, "model", None) or self.model
        self.bit_depth = int(self.cam.bit_depth)
        self.cam.frames_per_trigger_zero_for_unlimited = 0
        self.cam.image_poll_timeout_ms = 0  # we poll ourselves, so stop() is never stuck
        self.set_exposure_ms(self._start_exposure)
        if self.gain_range_db() is not None:
            self.set_gain_db(self._start_gain)

    def close(self):
        global _sdk
        try:
            self.stop()
        finally:
            if self.cam is not None:
                try:
                    self.cam.dispose()
                except Exception as e:
                    logger.warning("camera dispose: %r", e)
                self.cam = None
            if _sdk is not None:
                try:
                    _sdk.dispose()
                except Exception as e:
                    logger.warning("SDK dispose: %r", e)
                _sdk = None

    def info(self):
        c = self.cam
        d = {"model": self.model, "serial": self.serial, "bit_depth": self.bit_depth}
        if c is not None:
            d.update(width=c.image_width_pixels, height=c.image_height_pixels,
                     pixel_um=c.sensor_pixel_width_um)
        return d

    def exposure_range_ms(self):
        r = self.cam.exposure_time_range_us
        return (r.min / 1000.0, r.max / 1000.0)

    def get_exposure_ms(self):
        return self.cam.exposure_time_us / 1000.0

    def set_exposure_ms(self, ms):
        lo, hi = self.exposure_range_ms()
        self.cam.exposure_time_us = int(round(min(max(ms, lo), hi) * 1000))

    def gain_range_db(self):
        r = self.cam.gain_range
        if r.max <= r.min:
            return None
        return (self.cam.convert_gain_to_decibels(r.min), self.cam.convert_gain_to_decibels(r.max))

    def get_gain_db(self):
        if self.gain_range_db() is None:
            return None
        return self.cam.convert_gain_to_decibels(self.cam.gain)

    def set_gain_db(self, db):
        rng = self.gain_range_db()
        if rng is None:
            return
        db = min(max(float(db), rng[0]), rng[1])
        self.cam.gain = self.cam.convert_decibels_to_gain(db)

    def start(self):
        if self._armed:
            return
        self.cam.arm(2)
        self.cam.issue_software_trigger()
        self._armed = True

    def stop(self):
        if self._armed and self.cam is not None:
            self.cam.disarm()
        self._armed = False

    def read(self, timeout_s=0.5):
        end = time.time() + timeout_s
        while True:
            frame = self.cam.get_pending_frame_or_null()
            if frame is not None:
                img = np.array(frame.image_buffer, dtype=np.uint16, copy=True)
                return Frame(img, int(frame.frame_count), time.time())
            if time.time() >= end:
                return None
            time.sleep(0.002)
