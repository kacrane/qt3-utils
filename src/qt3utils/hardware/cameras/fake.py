"""A pretend camera for testing the Camera tab without hardware.

Shows a 12-bit mono image with a bright spot that drifts slowly, a few dim
"defects", noise, and a slight column pattern. Brightness follows exposure
and gain, so the controls can be tried out.
"""

import time

import numpy as np

from qt3utils.hardware.cameras.base import Camera, Frame


class FakeCamera(Camera):
    model = "Fake camera"

    def __init__(self, width=1440, height=1080, bit_depth=12, fps=30.0, **_ignored):
        self.width, self.height, self.bit_depth = int(width), int(height), int(bit_depth)
        self.period = 1.0 / float(fps)
        self.exposure_ms = 10.0
        self.gain_db = 0.0
        self._running = False
        self._count = 0
        self._next = 0.0
        self._rng = np.random.default_rng(1)

    def open(self):
        y, x = np.mgrid[0:self.height, 0:self.width].astype(np.float32)
        self._x, self._y = x, y
        cols = 1.0 + 0.03 * np.sin(np.arange(self.width) / 7.0)
        dots = np.zeros((self.height, self.width), np.float32)
        for cx, cy, a in self._rng.uniform([0, 0, 0.1], [self.width, self.height, 0.4], (25, 3)):
            dots += a * np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / 30.0)
        self._background = (0.05 + dots) * cols[None, :].astype(np.float32)

    def info(self):
        return {"model": self.model, "serial": "FAKE0001", "width": self.width,
                "height": self.height, "bit_depth": self.bit_depth}

    def exposure_range_ms(self):
        return (0.05, 2000.0)

    def get_exposure_ms(self):
        return self.exposure_ms

    def set_exposure_ms(self, ms):
        lo, hi = self.exposure_range_ms()
        self.exposure_ms = float(min(max(ms, lo), hi))

    def gain_range_db(self):
        return (0.0, 48.0)

    def get_gain_db(self):
        return self.gain_db

    def set_gain_db(self, db):
        self.gain_db = float(min(max(db, 0.0), 48.0))

    def start(self):
        self._running = True
        self._next = time.time()

    def stop(self):
        self._running = False

    def read(self, timeout_s=0.5):
        if not self._running:
            time.sleep(min(timeout_s, 0.05))
            return None
        wait = max(0.0, self._next - time.time())
        period = max(self.period, self.exposure_ms / 1000.0)
        if wait > timeout_s:
            time.sleep(timeout_s)
            return None
        if wait > 0:
            time.sleep(wait)
        self._next = max(self._next + period, time.time())
        t = time.time()
        cx = self.width * (0.5 + 0.08 * np.sin(t / 3.0))
        cy = self.height * (0.5 + 0.08 * np.cos(t / 4.0))
        spot = np.exp(-((self._x - cx) ** 2 + (self._y - cy) ** 2) / (2 * 25.0 ** 2))
        scale = self.exposure_ms / 10.0 * 10 ** (self.gain_db / 20.0)
        full = 2 ** self.bit_depth - 1
        img = (self._background + 2.0 * spot) * 0.25 * full * scale
        img += self._rng.normal(20, 6, img.shape)
        img = np.clip(img, 0, full).astype(np.uint16)
        self._count += 1
        return Frame(img, self._count, t)
