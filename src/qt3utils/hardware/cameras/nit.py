"""
NIT SWIR camera (TQLM, NSC2001 "Triple H" InGaAs sensor) — not written yet.

Waiting on the camera model and which NIT SDK (NITLibrary) is installed on
the TQLM PC, and whether it includes a Python wrapper. The SWIR image also
needs NIT's non-uniformity correction (NUC) file.
"""

from qt3utils.hardware.cameras.base import Camera


class NITCamera(Camera):
    model = "NIT SWIR camera"

    def __init__(self, **cfg):
        self.cfg = cfg

    def open(self):
        raise RuntimeError("the NIT camera driver is not written yet "
                           "(needs the NIT SDK details from the TQLM PC)")
