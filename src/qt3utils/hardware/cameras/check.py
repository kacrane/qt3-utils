"""
Quick camera check, without QT3 Microscope: open the camera, grab frames
for a few seconds and print the frame rate and brightness.

    python -m qt3utils.hardware.cameras.check
    python -m qt3utils.hardware.cameras.check --driver fake
    python -m qt3utils.hardware.cameras.check --dll-dir "C:\\Program Files\\Thorlabs\\Scientific Imaging\\ThorCam"

Close ThorCam first: only one program can open the camera.
"""

import argparse
import time

from qt3utils.hardware.cameras.base import make_camera


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--driver", default="thorlabs_tsi", help="thorlabs_tsi, nit or fake")
    p.add_argument("--dll-dir", default=None, help="folder with the camera SDK DLLs")
    p.add_argument("--exposure-ms", type=float, default=10.0)
    p.add_argument("--seconds", type=float, default=3.0)
    a = p.parse_args()
    cfg = {"driver": a.driver, "exposure_ms": a.exposure_ms}
    if a.dll_dir:
        cfg["dll_dir"] = a.dll_dir
    cam = make_camera(cfg)
    print("opening camera ...")
    cam.open()
    try:
        print("camera:", cam.info())
        print(f"exposure {cam.get_exposure_ms():.3f} ms (range {cam.exposure_range_ms()}), "
              f"gain {cam.get_gain_db()} dB (range {cam.gain_range_db()})")
        cam.start()
        n, t0, last = 0, time.time(), None
        while time.time() - t0 < a.seconds:
            f = cam.read(1.0)
            if f is not None:
                n, last = n + 1, f
        cam.stop()
        dt = time.time() - t0
        print(f"{n} frames in {dt:.1f} s = {n / dt:.1f} fps")
        if last is not None:
            img = last.image
            print(f"last frame {img.shape[1]}x{img.shape[0]} {img.dtype}, "
                  f"min {int(img.min())}, mean {float(img.mean()):.1f}, max {int(img.max())}")
        print("OK")
    finally:
        cam.close()


if __name__ == "__main__":
    main()
