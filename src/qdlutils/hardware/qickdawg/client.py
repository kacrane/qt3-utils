"""
Reconnecting client for a QICK-DAWG board.

``qickdawg.start_client()`` connects once. If the board reboots, the server
restarts, or the network link drops, every later call fails until someone
re-runs the notebook. ``QickDawgClient`` wraps that connection: each call
that fails with a connection error reconnects (finds the name server again,
gets a fresh proxy) and retries once.

Needs ``Pyro4``, ``qick`` and ``qickdawg`` installed in the Python
environment (the same ones your QICK-DAWG notebooks use; the ``qick``
version must match the board's).

Example::

    from qdlutils.hardware.qickdawg.client import QickDawgClient
    client = QickDawgClient("192.168.2.99")
    client.laser_on()
    client.laser_off()
"""

import logging
import socket
import threading
import time

logger = logging.getLogger(__name__)

DEFAULT_LASER_SETTINGS = {
    "laser_gate_pmod": 0,
    "adc_channel": 0,
    "edge_counting": True,
    "high_threshold": 8000,
    "low_threshold": 500,
    "relax_delay_tns": 50,
}


class QickDawgError(Exception):
    pass


def _connection_errors():
    """Exception types that mean 'the link is broken, reconnect and retry'."""
    errors = [ConnectionError, socket.timeout, socket.error, TimeoutError]
    try:
        import Pyro4.errors
        errors.append(Pyro4.errors.CommunicationError)
        errors.append(Pyro4.errors.NamingError)
    except ImportError:
        pass
    return tuple(errors)


class QickDawgClient:
    """
    Parameters
    ----------
    host : str
        Board IP address (the ``ns_host`` in qick_daemon.ipynb).
    port : int
        Pyro4 name server port.
    server_name : str
        Name the QickSoc is registered under.
    timeout_s : float
        Seconds before a call to the board is treated as a lost connection.
        Laser on/off takes well under a second.
    laser_settings : dict
        NVConfiguration fields used for laser_on/laser_off
        (see DEFAULT_LASER_SETTINGS).
    """

    def __init__(self, host="192.168.2.99", port=8888, server_name="myqick",
                 timeout_s=10.0, laser_settings=None):
        self.host = host
        self.port = int(port)
        self.server_name = server_name
        self.timeout_s = float(timeout_s)
        self.laser_settings = dict(DEFAULT_LASER_SETTINGS)
        if laser_settings:
            self.laser_settings.update(laser_settings)

        self._qd = None
        self._lock = threading.RLock()
        self.connected = False
        self.last_error = None
        self.laser_state = None  # True / False / None (unknown)
        # URI of the QICK object we are connected to. The board server picks a
        # new port every time it starts, so a changed URI means it restarted
        # (and re-initialized the firmware, which can reset the laser pin).
        self._soc_uri = None
        self.server_restarted = False

    # -- connection --------------------------------------------------------

    def _import(self):
        try:
            import Pyro4  # noqa: F401
            import qickdawg
        except ImportError as e:
            raise QickDawgError(
                f"{e}. Install Pyro4, qick and qickdawg in this Python environment "
                "(the same ones the QICK-DAWG notebooks use).") from e
        return qickdawg

    def _check_port(self):
        """Fast check that something listens on the name server port."""
        try:
            with socket.create_connection((self.host, self.port), timeout=3):
                pass
        except OSError as e:
            raise QickDawgError(
                f"No QICK-DAWG name server at {self.host}:{self.port} ({e}). "
                "Is the board on and the qick-server service running?") from e

    def connect(self):
        """(Re)connect to the board. Raises QickDawgError on failure."""
        with self._lock:
            qd = self._import()
            import Pyro4
            self._release()
            self._check_port()
            self._configure_pyro()
            try:
                qd.start_client(self.host, self.port, self.server_name)
            except Exception as e:
                self.connected = False
                self.last_error = e
                raise QickDawgError(f"Could not connect to QICK-DAWG at {self.host}: {e!r}") from e
            self._qd = qd
            uri = str(qd.soc._pyroUri)
            if self._soc_uri is not None and uri != self._soc_uri:
                self.server_restarted = True
                self.laser_state = None
            self._soc_uri = uri
            self.connected = True
            self.last_error = None
            logger.info("Connected to QICK-DAWG at %s:%s", self.host, self.port)

    def _configure_pyro(self):
        import Pyro4
        # The board only accepts pickle. Proxies read these at creation time;
        # COMMTIMEOUT makes a dead link raise an error instead of hanging.
        Pyro4.config.SERIALIZER = "pickle"
        Pyro4.config.PICKLE_PROTOCOL_VERSION = 4
        Pyro4.config.COMMTIMEOUT = self.timeout_s

    def _release(self):
        soc = getattr(self._qd, "soc", None) if self._qd else None
        if soc is not None and hasattr(soc, "_pyroRelease"):
            try:
                soc._pyroRelease()
            except Exception:
                pass
        self.connected = False

    def close(self):
        with self._lock:
            self._release()

    def call(self, fn, *args, **kwargs):
        """
        Run ``fn(qickdawg_module, *args, **kwargs)``. If it fails for any
        reason, reconnect from scratch once and retry. Only use this for
        calls that are safe to repeat (laser on/off are).
        """
        with self._lock:
            if not self.connected:
                self.connect()
            try:
                return fn(self._qd, *args, **kwargs)
            except Exception as e:
                # After a board reboot the old proxies can fail in ways that
                # are not plain connection errors (e.g. a half-started
                # streamer), so any failure gets one clean reconnect.
                logger.warning("QICK-DAWG call failed (%r); reconnecting and retrying",
                               e, exc_info=True)
                self.last_error = e
                self.connect()
                return fn(self._qd, *args, **kwargs)

    def ping(self):
        """True if the board's name server and QICK object answer."""
        with self._lock:
            try:
                self._check_port()
                import Pyro4
                self._configure_pyro()
                with Pyro4.locateNS(host=self.host, port=self.port) as ns:
                    uri = ns.lookup(self.server_name)
                with Pyro4.Proxy(uri) as proxy:
                    proxy._pyroTimeout = self.timeout_s
                    proxy._pyroBind()
                if self._soc_uri is not None and str(uri) != self._soc_uri:
                    # Still answering, but it is a new server process.
                    self.server_restarted = True
                    self.laser_state = None
                    self.connected = False
                    return False
                return True
            except Exception as e:
                self.last_error = e
                self.connected = False
                return False

    # -- laser -------------------------------------------------------------

    def _laser_config(self, qd):
        cfg = qd.NVConfiguration()
        for key, value in self.laser_settings.items():
            setattr(cfg, key, value)
        return cfg

    def laser_on(self):
        """Set the laser gate PMOD high. Returns the PL reading qickdawg gives back."""
        reading = self.call(lambda qd: qd.laser_on(self._laser_config(qd)))
        self.laser_state = True
        return reading

    def laser_off(self):
        """Set the laser gate PMOD low. Returns the background reading."""
        reading = self.call(lambda qd: qd.laser_off(self._laser_config(qd)))
        self.laser_state = False
        return reading


def main(argv=None):
    """Command-line check: python -m qdlutils.hardware.qickdawg.client --host 192.168.2.99"""
    import argparse
    p = argparse.ArgumentParser(description="Check the QICK-DAWG connection and switch the laser.")
    p.add_argument("--host", default="192.168.2.99")
    p.add_argument("--port", type=int, default=8888)
    p.add_argument("--name", default="myqick")
    p.add_argument("--pmod", type=int, default=None, help="laser_gate_pmod (default 0)")
    p.add_argument("--laser", choices=["on", "off"], default=None)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    settings = {} if args.pmod is None else {"laser_gate_pmod": args.pmod}
    client = QickDawgClient(args.host, args.port, args.name, laser_settings=settings)

    print(f"1. Name server port {args.host}:{args.port} ... ", end="", flush=True)
    try:
        client._check_port()
        print("open")
    except QickDawgError as e:
        print(f"FAILED\n   {e}")
        return 1

    print("2. Name server lookup and QICK object ... ", end="", flush=True)
    print("OK" if client.ping() else f"FAILED\n   {client.last_error!r}")

    print("3. qickdawg.start_client ... ", end="", flush=True)
    t0 = time.time()
    try:
        client.connect()
        print(f"OK ({time.time() - t0:.1f} s)")
    except QickDawgError as e:
        print(f"FAILED\n   {e}")
        return 1

    if args.laser:
        print(f"4. Laser {args.laser} ... ", end="", flush=True)
        reading = client.laser_on() if args.laser == "on" else client.laser_off()
        print(f"done (reading {reading})")
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
