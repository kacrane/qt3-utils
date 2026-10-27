"""
Read-only connectivity test for the NKT SuperK CHROMATUNE over Ethernet.

Checks whether the register map used by qt3chromatune actually works, without
changing anything on the laser: it only READS registers, and never touches
emission (unlike Chromatune.close(), which turns emission off).

Close qt3chromatune and NKT CONTROL first: the laser sends its replies to one
host port (default 10001), so only one program on this PC can talk to it at a time.

Run it with the laser in a state where nothing else is talking to it. The laser's
watchdog (if enabled) turns emission off when communication stops; this script
prints the watchdog setting so you can see whether that applies.

    python -m qt3utils.tools.test_superk_connection
    python -m qt3utils.tools.test_superk_connection --host-ip 3.3.44.52 --laser-ip 3.3.44.56

Requires the NKT SDK (NKTPDLL.dll) and the nkt_tools Python package.
"""

from __future__ import annotations

import argparse
import sys
import time

# Defaults copied from qt3chromatune.
DEFAULT_HOST_IP = "3.3.44.52"  # TQLM on the lab network
DEFAULT_LASER_IP = "3.3.44.56"  # where NKT CONTROL reaches the SuperK
DEFAULT_PORT = 10001
MAIN_MODULE = 0x0F
FILTER_MODULE = 0x07
PORT_NAME = "superk_test"
SEED_REP_RATE_MHZ = 78.0  # nominal seed rate from the product guide; actual may differ slightly


def _step(name: str) -> None:
    print(f"\n--- {name} ---")


def _fail(msg: str, code: int = 1) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(code)


def _ok(msg: str) -> None:
    print(f"OK: {msg}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Read-only test of the SuperK CHROMATUNE Ethernet link.")
    parser.add_argument("--host-ip", default=DEFAULT_HOST_IP, help="IP of this PC on the laser's network")
    parser.add_argument("--host-port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--laser-ip", default=DEFAULT_LASER_IP)
    parser.add_argument("--laser-port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout-ms", type=int, default=100)
    args = parser.parse_args(argv)

    _step("Import NKT SDK")
    try:
        import nkt_tools.NKTP_DLL as nkt
    except Exception as e:
        _fail(f"could not import nkt_tools.NKTP_DLL ({e}). Install the NKT SDK and `pip install nkt_tools`.")
    _ok("nkt_tools imported")

    _step(f"Open point-to-point port {args.host_ip}:{args.host_port} -> {args.laser_ip}:{args.laser_port}")
    port_data = nkt.pointToPointPortData(
        args.host_ip, args.host_port, args.laser_ip, args.laser_port, 0, args.timeout_ms
    )
    res = nkt.pointToPointPortAdd(PORT_NAME, port_data)
    if res != 0:
        _fail(f"pointToPointPortAdd: {nkt.P2PPortResultTypes(res)}")
    res = nkt.openPorts(PORT_NAME, autoMode=1, liveMode=1)
    if res != 0:
        nkt.pointToPointPortDel(PORT_NAME)
        _fail(f"openPorts: {nkt.PortResultTypes(res)} (is qt3chromatune still connected?)")
    _ok("port open")

    failures = 0
    try:
        time.sleep(0.5)  # let autoMode discover modules
        _step("Modules found")
        res, modules = nkt.deviceGetAllTypes(PORT_NAME) if hasattr(nkt, "deviceGetAllTypes") else (None, None)
        if res == 0 and modules:
            found = [f"0x{addr:02X}: type 0x{t:02X}" for addr, t in enumerate(modules) if t]
            print("  " + ("\n  ".join(found) if found else "(none)"))
        else:
            print("  (module listing not available; continuing)")

        reads = [
            # (label, module, register, index, reader, scale, unit)
            ("Emission state (0=off, 3=on)", MAIN_MODULE, 0x30, -1, "registerReadU8", 1, ""),
            ("Pulse picker ratio", MAIN_MODULE, 0x34, -1, "registerReadU16", 1, ""),
            ("Watchdog timeout (0 = disabled)", MAIN_MODULE, 0x36, -1, "registerReadU8", 1, "s"),
            ("Filter center wavelength", FILTER_MODULE, 0x32, 0, "registerReadU16", 0.1, "nm"),
            ("Filter bandwidth", FILTER_MODULE, 0x32, 2, "registerReadU16", 0.1, "nm"),
            ("Filter power setpoint", FILTER_MODULE, 0x32, 4, "registerReadU32", 1, "nW"),
            ("Bandwidth limit (max)", FILTER_MODULE, 0x35, 0, "registerReadU16", 0.1, "nm"),
            ("Bandwidth limit (min)", FILTER_MODULE, 0x35, 1, "registerReadU16", 0.1, "nm"),
        ]
        _step("Read registers (read-only)")
        for label, module, reg, index, reader, scale, unit in reads:
            res, val = getattr(nkt, reader)(PORT_NAME, module, reg, index)
            if res != 0:
                failures += 1
                print(f"  FAIL {label} (module 0x{module:02X} reg 0x{reg:02X}): {nkt.RegisterResultTypes(res)}")
            else:
                shown = val * scale if scale != 1 else val
                print(f"  {label}: {shown:g} {unit}   (raw {val})")
                if label == "Pulse picker ratio" and val > 0:
                    print(f"    -> repetition rate ~ {SEED_REP_RATE_MHZ / val:g} MHz ({SEED_REP_RATE_MHZ:g} MHz / {val})")
    finally:
        _step("Close port (emission untouched)")
        nkt.closePorts(PORT_NAME)
        nkt.pointToPointPortDel(PORT_NAME)
        _ok("closed")

    if failures:
        _fail(f"{failures} register read(s) failed")
    print("\nAll reads OK. Compare the values with the laser's front panel or NKT CONTROL.")


if __name__ == "__main__":
    main()
