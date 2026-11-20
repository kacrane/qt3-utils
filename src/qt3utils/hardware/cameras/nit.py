"""
NIT SWIR camera (TQLM: WiDy SenS 320V-ST, sensor NSC2001, 320x256, 14-bit)
through NIT's NITLibrary Python SDK (NITLIBRARY-Python-3.8.3).

Needs, on the lab PC:
- The SDK folder for this Python version, holding both
  NITLibrary_x64_383_py310.pyd and the SDK's own NITLibrary_x64.dll
  (from the SDK zip: NITLIBRARY-Python-3.8.3\\bin\\PYTHON310). Its path is
  sdk_dir in the profile. Don't point it at NITVision's folder: NITVision's
  NITLibrary_x64.dll is a different version.
- NITVision closed: only one program can open the camera at a time.
- Optional: the camera's factory NUC/BPR folder (SN240792 from NIT's USB
  key). Without it the image has no non-uniformity correction or bad
  pixel replacement. Give its path as nuc_dir, or leave nuc_dir empty and
  the driver looks for an SN<serial> folder in sdk_dir and next to NITVision.

Profile settings (camera: in TQLM.yaml), all optional:
    sdk_dir:   folder with the .pyd and NITLibrary_x64.dll
    nuc_dir:   factory NUC folder (SN<serial>), or "off" for no NUC/BPR
    serial:    open this camera (default: the first NIT camera found)
    params:    camera parameters set at open, in order, e.g.
               {"Sensor Response": "Lin", "Analog Gain": "High", "Cds": "On"}
               (names and values as in the NITLibrary Python guide,
               section "WiDySenS 320V-S series parameters")
    max_fps:   true (default) runs at the fastest frame rate the exposure
               allows; false keeps the camera's frame rate

The camera has no gain in dB: its "Analog Gain" is Low/High (Lin response)
or Standard (Log response), so it is set with params and the tab's gain box
is grayed out. Exposure takes discrete values; the camera picks the nearest.

The SDK hands frames over as floats 0-1. They are turned back into camera
counts (0-16383 for 14-bit) so snapshots and the tab's levels match the
other cameras.
"""

import collections
import ctypes
import glob
import importlib
import logging
import os
import subprocess
import sys
import threading
import time

import numpy as np

from qt3utils.hardware.cameras.base import Camera, Frame

logger = logging.getLogger(__name__)

SDK_GUESSES = [
    r"C:\NITLibrary-Python-3.8.3\bin\PYTHON{v}",
    r"C:\NITLibrary-Python\bin\PYTHON{v}",
]
NITVISION_GUESSES = [
    r"C:\NITVision_x64-1.7.0\NITVision\x64",
    r"C:\NITVision_x64-1.7.0",
]
# Tec Mode "Strong Current" needs the camera's external power supply; on USB
# power it can damage the PC (NIT manual). Refused unless allow_strong_tec.
STRONG_TEC = "strong current"

_nit = None        # the imported NITLibrary module (one per program)
_manager = None    # NITManager singleton
_console_done = False


def _hidden_console():
    """
    Give the program a console with no window, once.

    When the Cypress USB driver is 1.2.3.14 or older, NITLibrary runs
    'powershell.exe -Command Get-ItemProperty ... ProductName' while opening
    the camera, to pick which newer driver to recommend. QT3 Microscope's
    shortcut uses pythonw.exe, which has no console, so Windows opens a
    terminal window for that PowerShell. Attaching to a windowless console
    first makes it run out of sight. (Updating the driver skips the check.)
    """
    global _console_done
    if _console_done or sys.platform != "win32":
        return
    _console_done = True
    k32 = ctypes.windll.kernel32
    if k32.GetConsoleWindow():
        return  # started from a terminal: the check already runs in it, no new window
    try:
        helper = subprocess.Popen(["cmd.exe", "/q", "/k"], stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as e:
        logger.debug("no hidden console: %r", e)
        return
    try:
        for _ in range(100):
            if k32.AttachConsole(helper.pid):
                logger.debug("attached to a hidden console")
                return
            time.sleep(0.02)
        logger.debug("could not attach to the hidden console")
    finally:
        helper.kill()  # the console stays while we are attached to it


def _py_tag():
    return f"{sys.version_info.major}{sys.version_info.minor}"


def _load_sdk(sdk_dir):
    """Import NITLibrary_x64_<ver>_py<XY> from sdk_dir (or the usual places)."""
    global _nit
    if _nit is not None:
        return _nit
    tag = _py_tag()
    ext = ".pyd" if sys.platform == "win32" else ".so"
    folders = [sdk_dir] if sdk_dir else [g.format(v=tag) for g in SDK_GUESSES]
    for folder in folders:
        if not folder:
            continue
        hits = sorted(glob.glob(os.path.join(folder, f"NITLibrary_*_py{tag}{ext}")))
        if not hits:
            continue
        if sys.platform == "win32":
            # The .pyd needs the SDK's NITLibrary_x64.dll from the same folder,
            # not NITVision's copy of a DLL with the same name.
            os.add_dll_directory(folder)
            os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
        if folder not in sys.path:
            sys.path.insert(0, folder)
        name = os.path.basename(hits[-1])[:-len(ext)]
        _nit = importlib.import_module(name)
        logger.info("NIT SDK %s from %s", name, folder)
        return _nit
    raise RuntimeError(
        f"could not find NITLibrary_*_py{tag}{ext} (NIT's Python SDK for Python "
        f"{sys.version_info.major}.{sys.version_info.minor}); put its folder in the "
        f"profile as sdk_dir (tried: {'; '.join(f for f in folders if f)})")


def _make_observers(nit, cam):
    """Observer classes are built here because they subclass SDK classes."""

    class _Frames(nit.NITUserObserver):
        def onNewFrame(self, frame):  # called from the SDK's threads
            try:
                cam._on_frame(frame)
            except Exception:
                logger.exception("NIT frame handling failed")

    class _Config(nit.NITConfigObserver):
        def onParamChanged(self, param_name, str_value, num_value):
            logger.debug("NIT %s = %s (%s)", param_name, str_value, num_value)

        def onParamRangeChanged(self, param_name, str_values, num_values, *rest):
            cam._on_range(param_name, num_values)

        def onFpsChanged(self, new_fps):
            logger.debug("NIT fps %s", new_fps)

        def onFpsRangeChanged(self, fps_min, fps_max, fps):
            logger.debug("NIT fps range %s-%s (now %s)", fps_min, fps_max, fps)

        def onNewFrame(self, status):
            if status != 0:
                cam._dropped += 1

        def onNucChanged(self, nuc_str, status):
            cam.nuc_status = nuc_str
            logger.info("NIT NUC: %s (status %s)", nuc_str, status)

    return _Frames(), _Config()


class NITCamera(Camera):
    model = "NIT SWIR camera"
    bit_depth = 14

    def __init__(self, sdk_dir=None, dll_dir=None, nuc_dir=None, serial=None, params=None,
                 max_fps=True, exposure_ms=10.0, allow_strong_tec=False, **_ignored):
        self.sdk_dir = sdk_dir or dll_dir
        self.nuc_dir = nuc_dir
        self.serial = str(serial) if serial else None
        self.params = dict(params or {})
        self.max_fps = bool(max_fps)
        self.allow_strong_tec = bool(allow_strong_tec)
        self._start_exposure = exposure_ms
        self.dev = None
        self.nuc_status = ""
        self._observers = ()
        self._frames = collections.deque(maxlen=2)
        self._have_frame = threading.Condition()
        self._exp_values_us = None
        self._running = False
        self._dropped = 0
        self._temperature = None

    # ----- open / close -----
    def open(self):
        global _manager
        nit = _load_sdk(self.sdk_dir)
        _hidden_console()
        # That PowerShell also loads the user's profile (conda setup, ~2 s on
        # the TQLM) while QT3 Microscope's window is frozen. The TQLM profile
        # starts with: if ($env:QT3_SKIP_PS_PROFILE) { return }
        os.environ["QT3_SKIP_PS_PROFILE"] = "1"
        _manager = nit.NITManager.getInstance()
        try:
            dev = (_manager.openBySerialNumber(self.serial) if self.serial
                   else _manager.openOneDevice())
        except Exception as e:
            raise RuntimeError(f"could not open the NIT camera: {e} "
                               "(is it plugged in, and is NITVision closed?)") from e
        if dev is None:
            raise RuntimeError("no NIT camera found: is it plugged in, and is NITVision closed?")
        self.dev = dev
        self.serial = str(dev.serialNumber())
        self.model = f"NIT {dev.commercialName()}".strip()
        frames, config = _make_observers(nit, self)
        self._observers = (frames, config)  # keep them alive
        dev << config
        self._setup_nuc()
        for name, value in self.params.items():
            if name.lower().replace(" ", "") == "tecmode" and str(value).lower() == STRONG_TEC \
                    and not self.allow_strong_tec:
                raise RuntimeError("Tec Mode 'Strong Current' needs the camera's external power "
                                   "supply (on USB power it can damage the PC); set "
                                   "allow_strong_tec: true in the profile if it has one")
            logger.info("NIT set %s = %r", name, value)
            dev.setParamValueOf(name, value)
            dev.updateConfig()
        self.set_exposure_ms(self._start_exposure)
        dev << frames
        logger.info("NIT camera open: %s", self.info())

    def _setup_nuc(self):
        dev = self.dev
        if str(self.nuc_dir).lower() == "off":
            dev.activateNuc(False)
            dev.activateBpr(False)
            return
        folder = self.nuc_dir
        if not folder:
            sn = f"SN{self.serial}"
            places = [self.sdk_dir] + NITVISION_GUESSES if self.sdk_dir else NITVISION_GUESSES
            for p in places:
                if p and os.path.isdir(os.path.join(p, sn)):
                    folder = os.path.join(p, sn)
                    break
        if folder:
            try:
                dev.setNucDirectory(folder, True)
                logger.info("NIT NUC/BPR folder %s", folder)
            except Exception as e:
                logger.warning("NIT NUC folder %s not used: %s", folder, e)
        try:
            self.nuc_status = self.nuc_status or str(dev.getCurrentNucPath() or "")
        except Exception:
            pass
        logger.info("NIT NUC active %s, BPR active %s", dev.nucActive(), dev.bprActive())

    def close(self):
        global _manager
        try:
            self.stop()
        finally:
            self.dev = None
            self._observers = ()
            if _manager is not None:
                try:
                    _manager.reset()  # releases the camera so NITVision can use it
                except Exception as e:
                    logger.warning("NITManager reset: %r", e)

    def info(self):
        d = {"model": self.model, "serial": self.serial, "bit_depth": self.bit_depth}
        dev = self.dev
        if dev is not None:
            for key, fn in (("sensor", dev.modelId), ("firmware", dev.firmwareVersion),
                            ("width", dev.sensorWidth), ("height", dev.sensorHeight)):
                try:
                    d[key] = fn()
                except Exception:
                    pass
            for key, name in (("sensor_response", "Sensor Response"),
                              ("analog_gain", "Analog Gain")):
                try:
                    d[key] = dev.paramStrValueOf(name)
                except Exception:
                    pass
            try:
                d["nuc"] = bool(dev.nucActive())
                d["bpr"] = bool(dev.bprActive())
            except Exception:
                pass
        if self._temperature is not None:
            d["temperature_c"] = round(self._temperature, 1)
        return d

    # ----- exposure (no dB gain on this camera) -----
    def _on_range(self, name, num_values):
        if name.lower().replace(" ", "") == "exposuretime":
            try:
                vals = sorted(float(v) for v in num_values)
                self._exp_values_us = vals or None
            except Exception:
                pass

    def exposure_range_ms(self):
        if self._exp_values_us:
            return (self._exp_values_us[0] / 1000.0, self._exp_values_us[-1] / 1000.0)
        return (0.01, 220.0)

    def get_exposure_ms(self):
        return float(self.dev.paramValueOf("Exposure Time")) / 1000.0

    def set_exposure_ms(self, ms):
        lo, hi = self.exposure_range_ms()
        us = min(max(float(ms), lo), hi) * 1000.0
        dev = self.dev
        dev.setParamValueOf("Exposure Time", us)
        dev.updateConfig()
        if self.max_fps:
            dev.setFps(dev.maxFps())
            dev.updateConfig()

    # ----- acquisition -----
    def start(self):
        if self._running:
            return
        self._frames.clear()
        self._dropped = 0
        self.dev.start()
        self._running = True

    def stop(self):
        if self._running and self.dev is not None:
            try:
                self.dev.stop()
            finally:
                self._running = False
        self._running = False

    def _on_frame(self, frame):
        rows, cols = int(frame.rows()), int(frame.columns())
        data = np.asarray(frame.data())
        bits = int(frame.bitsPerPixel()) or self.bit_depth
        if data.dtype.kind == "f":
            img = np.rint(np.clip(data, 0.0, 1.0) * (2 ** bits - 1)).astype(np.uint16)
        else:  # RGBA (not expected without a color filter): keep one channel
            img = (data.astype(np.uint32) & 0xFF).astype(np.uint16)
        img = img.reshape(rows, cols)
        try:
            self._temperature = float(frame.temperature())
        except Exception:
            pass
        with self._have_frame:
            self._frames.append(Frame(img, int(frame.id()), time.time()))
            self._have_frame.notify()

    def read(self, timeout_s=0.5):
        with self._have_frame:
            if not self._frames:
                self._have_frame.wait(timeout_s)
            if not self._frames:
                return None
            newest = self._frames.pop()  # show the newest; drop any older one
            self._frames.clear()
            return newest
