"""Read-only access to the Santec tunable lasers (as wired for qt3santec), for qt3power.

qt3santec drives several Santec TSL lasers behind an optical switch and, when
it changes laser, turns the previous one's output off. So the laser feeding the
PDA50B2 is the one whose output is on. Each call opens the lasers, sends only
queries (":POW:STAT?", ":WAV?", ":WAV:SWE?"), and closes them. Nothing is written.

Laser addresses come from qt3santec's LASER_CONFIG so there is one source of truth.
"""

from dataclasses import dataclass
from typing import List, Optional

# Fallback if qt3santec can't be imported (copy of its LASER_CONFIG).
_FALLBACK_LASER_CONFIG = [
    {"ip": "192.168.0.96", "switch_port": 1, "socket_port": 5000},
    {"ip": "192.168.0.99", "switch_port": 2, "socket_port": 5000},
]
TIMEOUT_MS = 2000


@dataclass
class SantecLaserState:
    ip: str
    output_on: Optional[bool]
    wavelength_nm: Optional[float]
    sweeping: Optional[bool]
    error: Optional[str] = None


@dataclass
class SantecReading:
    wavelength_nm: float
    ip: str
    sweeping: bool
    lasers: List[SantecLaserState]


def laser_config() -> List[dict]:
    try:
        from qt3utils.applications.qt3santec.main import LASER_CONFIG

        return list(LASER_CONFIG)
    except Exception:
        return list(_FALLBACK_LASER_CONFIG)


def _query_laser(rm, cfg: dict) -> SantecLaserState:
    addr = f"TCPIP0::{cfg['ip']}::{cfg['socket_port']}::SOCKET"
    try:
        inst = rm.open_resource(addr)
    except Exception as e:
        return SantecLaserState(cfg["ip"], None, None, None, f"connect failed: {e}")
    try:
        inst.write_termination = "\r"
        inst.read_termination = "\r"
        inst.timeout = TIMEOUT_MS
        output_on = inst.query(":POW:STAT?").strip() == "1"
        wavelength = float(inst.query(":WAV?"))
        if 0 < wavelength < 1e-3:  # SCPI mode answers in meters
            wavelength *= 1e9
        try:
            sweeping = inst.query(":WAV:SWE?").strip() not in ("0", "")
        except Exception:
            sweeping = None
        return SantecLaserState(cfg["ip"], output_on, wavelength, sweeping)
    except Exception as e:
        return SantecLaserState(cfg["ip"], None, None, None, f"query failed: {e}")
    finally:
        try:
            inst.close()
        except Exception:
            pass


def read_santec_wavelength(configs: Optional[List[dict]] = None) -> SantecReading:
    """
    Return the wavelength of the Santec laser whose output is on.

    Raises:
        RuntimeError: pyvisa missing, no laser reachable, no output on, or more than one on.
    """
    try:
        import pyvisa
    except Exception as e:
        raise RuntimeError(f"pyvisa not available ({e}).")
    configs = configs if configs is not None else laser_config()
    rm = pyvisa.ResourceManager()
    try:
        states = [_query_laser(rm, cfg) for cfg in configs]
    finally:
        try:
            rm.close()
        except Exception:
            pass

    reachable = [s for s in states if s.error is None]
    if not reachable:
        detail = "; ".join(f"{s.ip}: {s.error}" for s in states)
        raise RuntimeError(f"No Santec laser answered. {detail}")
    on = [s for s in reachable if s.output_on]
    if not on:
        raise RuntimeError("No Santec laser has its output on.")
    if len(on) > 1:
        listing = ", ".join(f"{s.ip} at {s.wavelength_nm:g} nm" for s in on)
        raise RuntimeError(f"More than one Santec output is on ({listing}); can't tell which one feeds the PDA50B2.")
    active = on[0]
    return SantecReading(active.wavelength_nm, active.ip, bool(active.sweeping), states)
