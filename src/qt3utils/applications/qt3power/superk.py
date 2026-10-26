"""Read-only access to the NKT SuperK CHROMATUNE over Ethernet, for qt3power.

Each call connects, reads a few registers, and disconnects. Nothing is ever
written to the laser and emission is never touched (unlike
qt3chromatune.Chromatune.close(), which turns emission off).

Only one program on a PC can talk to the laser over Ethernet at a time (it
replies to a single host port), so this fails while NKT CONTROL or qt3chromatune
holds the Ethernet link.

Register map taken from qt3chromatune; verify with
    python -m qt3utils.tools.test_superk_connection
"""

import time
from dataclasses import dataclass
from typing import Optional

# Defaults copied from qt3chromatune.
DEFAULT_CONNECTION = {
    "host_ip": "192.168.0.209",
    "host_port": 10001,
    "laser_ip": "192.168.0.139",
    "laser_port": 10001,
    "timeout_ms": 100,
}
MAIN_MODULE = 0x0F
FILTER_MODULE = 0x07
SEED_REP_RATE_MHZ = 78.0  # nominal seed rate (product guide); actual may differ slightly
PORT_NAME = "qt3power_superk"


@dataclass
class SuperKSettings:
    center_nm: float
    bandwidth_nm: float
    picker_ratio: Optional[int]  # None if the read failed (e.g. no pulse picker)
    emission_on: Optional[bool]

    @property
    def rep_rate_mhz(self) -> Optional[float]:
        if not self.picker_ratio:
            return None
        return SEED_REP_RATE_MHZ / self.picker_ratio


def read_superk_settings(connection: Optional[dict] = None) -> SuperKSettings:
    """
    Connect, read center wavelength, bandwidth, pulse picker ratio and emission, disconnect.

    Raises:
        RuntimeError: SDK missing, link busy/unreachable, or the filter registers could not be read.
    """
    conn = dict(DEFAULT_CONNECTION)
    conn.update(connection or {})
    try:
        import nkt_tools.NKTP_DLL as nkt
    except Exception as e:
        raise RuntimeError(f"NKT SDK not available ({e}). Install the NKT SDK and `pip install nkt_tools`.")

    port_data = nkt.pointToPointPortData(
        conn["host_ip"], int(conn["host_port"]), conn["laser_ip"], int(conn["laser_port"]), 0, int(conn["timeout_ms"])
    )
    res = nkt.pointToPointPortAdd(PORT_NAME, port_data)
    if res != 0:
        raise RuntimeError(f"Could not set up the Ethernet port: {nkt.P2PPortResultTypes(res)}")
    try:
        res = nkt.openPorts(PORT_NAME, autoMode=1, liveMode=1)  # same mode as qt3chromatune
        if res != 0:
            raise RuntimeError(
                f"Could not open the laser link: {nkt.PortResultTypes(res)}. "
                "Is NKT CONTROL or qt3chromatune connected over Ethernet?"
            )
        try:
            time.sleep(0.3)  # let autoMode find the modules

            def read(reader: str, module: int, reg: int, index: int) -> Optional[int]:
                res, val = getattr(nkt, reader)(PORT_NAME, module, reg, index)
                return val if res == 0 else None

            center = read("registerReadU16", FILTER_MODULE, 0x32, 0)
            bandwidth = read("registerReadU16", FILTER_MODULE, 0x32, 2)
            if center is None or bandwidth is None:
                raise RuntimeError("Connected, but could not read the filter wavelength/bandwidth registers.")
            ratio = read("registerReadU16", MAIN_MODULE, 0x34, -1)
            emission = read("registerReadU8", MAIN_MODULE, 0x30, -1)
            return SuperKSettings(
                center_nm=center / 10.0,
                bandwidth_nm=bandwidth / 10.0,
                picker_ratio=ratio,
                emission_on=None if emission is None else emission == 3,
            )
        finally:
            nkt.closePorts(PORT_NAME)
    finally:
        nkt.pointToPointPortDel(PORT_NAME)
