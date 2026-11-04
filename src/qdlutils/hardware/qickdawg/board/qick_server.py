"""
QICK-DAWG board server: Pyro4 name server + QICK daemon in one process.

This script runs ON THE QICK-DAWG BOARD (the PYNQ Linux on the RFSoC), not on
the lab PC. It replaces the two notebooks from qick-dawg's run_server folder
(name_server.ipynb and qick_daemon.ipynb) and keeps the same names, so
existing notebooks that call ``qickdawg.start_client(...)`` keep working.

What it does differently from the notebooks:

* Runs as a systemd service: it starts when the board boots and systemd
  restarts it within seconds if it crashes or stops answering.
* Waits for the board's network address to exist before binding (the USB
  network link can come up late after a reboot).
* Uses a bigger Pyro4 thread pool (200 instead of 40). Every client
  connection holds one server thread; connections from notebooks or apps that
  crashed, slept, or lost the link pile up until the pool is full, after
  which the board refuses new connections with "no free workers".
* Turns on aggressive TCP keepalive (see --install) so the kernel notices a
  dead client within about two minutes and frees its thread.
* A watchdog checks the name server and the QICK object every 30 s and exits
  (so systemd restarts everything) if either stops answering.

Install on the board (once, over ssh, as the xilinx user):

    sudo python3 qick_server.py --install --host 192.168.3.1 --bitfile /home/xilinx/jupyter_notebooks/qick-dawg/firmware/photon_counting/qick_4x2.bit

Then:

    sudo systemctl status qick-server
    sudo journalctl -u qick-server -f
    sudo systemctl restart qick-server
    sudo python3 qick_server.py --uninstall

Run by hand (for testing, after stopping the service and the notebooks):

    sudo -E /usr/local/share/pynq-venv/bin/python qick_server.py --host 192.168.3.1 --bitfile ...
"""

import argparse
import logging
import os
import shlex
import socket
import subprocess
import sys
import threading
import time

logger = logging.getLogger("qick_server")

SERVICE_NAME = "qick-server"
SERVICE_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"
SYSCTL_PATH = "/etc/sysctl.d/90-qick-server-keepalive.conf"
INSTALL_DIR = "/home/xilinx/qick_server"
PYNQ_PYTHON = "/usr/local/share/pynq-venv/bin/python"

DEFAULT_HOST = "192.168.3.1"
DEFAULT_PORT = 8888
DEFAULT_NAME = "myqick"
DEFAULT_BITFILE = "/home/xilinx/jupyter_notebooks/qick-dawg/firmware/photon_counting/qick_4x2.bit"

# Kernel TCP keepalive: first probe after 60 s idle, then every 10 s, give up
# after 6 failed probes. A client that vanished is dropped after ~2 minutes
# instead of the Linux default of more than 2 hours.
SYSCTL_TEXT = (
    "# Installed by qick_server.py: drop dead Pyro4 client connections quickly\n"
    "net.ipv4.tcp_keepalive_time = 60\n"
    "net.ipv4.tcp_keepalive_intvl = 10\n"
    "net.ipv4.tcp_keepalive_probes = 6\n"
)

WATCHDOG_PERIOD_S = 30
WATCHDOG_FAILURES_BEFORE_EXIT = 3


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def configure_pyro():
    import Pyro4
    Pyro4.config.REQUIRE_EXPOSE = False
    Pyro4.config.SERIALIZER = "pickle"
    Pyro4.config.SERIALIZERS_ACCEPTED = set(["pickle"])
    Pyro4.config.PICKLE_PROTOCOL_VERSION = 4
    Pyro4.config.SERVERTYPE = "thread"
    Pyro4.config.THREADPOOL_SIZE = 200
    Pyro4.config.THREADPOOL_SIZE_MIN = 8
    # No server COMMTIMEOUT on purpose: an idle notebook must not be cut off.
    # Dead clients are removed by TCP keepalive instead.
    return Pyro4


def wait_for_address(host, timeout_s=None):
    """Block until `host` can be bound on this machine (the interface is up)."""
    t0 = time.time()
    warned = False
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, 0))
            return
        except OSError as e:
            if not warned:
                logger.warning("Address %s is not up yet (%s); waiting...", host, e)
                warned = True
        finally:
            s.close()
        if timeout_s is not None and time.time() - t0 > timeout_s:
            raise RuntimeError(f"Address {host} never came up")
        time.sleep(2)


def start_name_server(Pyro4, host, port):
    import Pyro4.naming
    while True:
        try:
            ns_uri, ns_daemon, bc_server = Pyro4.naming.startNS(
                host=host, port=port, enableBroadcast=False)
            break
        except OSError as e:
            # Usually "address already in use": an old notebook is still
            # running the name server. Keep retrying so it recovers on its own.
            logger.warning("Cannot start name server on %s:%s (%s); retrying in 5 s",
                           host, port, e)
            time.sleep(5)
    thread = threading.Thread(target=ns_daemon.requestLoop, name="pyro-ns", daemon=True)
    thread.start()
    logger.info("Name server running at %s", ns_uri)
    return ns_daemon, thread


def start_qick_daemon(Pyro4, host, port, server_name, bitfile):
    from qick import QickSoc, QickConfig

    ns = Pyro4.locateNS(host=host, port=port)
    daemon = Pyro4.Daemon(host=host)

    logger.info("Loading QICK firmware %s", bitfile)
    soc = QickSoc(bitfile=bitfile, force_init_clks=True)
    logger.info("QICK initialized:\n%s", QickConfig(soc.get_cfg()))

    # Remove a stale registration from a previous run before re-registering.
    try:
        ns.remove(server_name)
    except Exception:
        pass
    ns.register(server_name, daemon.register(soc))
    for obj in soc.autoproxy:
        daemon.register(obj)
        logger.info("Registered member %s", obj)
    logger.info("Registered QICK as '%s'", server_name)

    thread = threading.Thread(target=daemon.requestLoop, name="pyro-qick", daemon=True)
    thread.start()
    return daemon, thread


def watchdog_ok(Pyro4, host, port, server_name, threads):
    if not all(t.is_alive() for t in threads):
        logger.error("A server thread has stopped")
        return False
    try:
        with Pyro4.locateNS(host=host, port=port) as ns:
            uri = ns.lookup(server_name)
        with Pyro4.Proxy(uri) as soc:
            soc._pyroTimeout = 10
            soc.get_cfg()
        return True
    except Exception as e:
        logger.error("Watchdog check failed: %r", e)
        return False


def serve(args):
    Pyro4 = configure_pyro()
    wait_for_address(args.host)
    ns_daemon, ns_thread = start_name_server(Pyro4, args.host, args.port)
    qick_daemon, qick_thread = start_qick_daemon(
        Pyro4, args.host, args.port, args.name, args.bitfile)
    logger.info("Ready: clients connect with qickdawg.start_client('%s')", args.host)

    failures = 0
    while True:
        time.sleep(WATCHDOG_PERIOD_S)
        if watchdog_ok(Pyro4, args.host, args.port, args.name, [ns_thread, qick_thread]):
            failures = 0
            continue
        failures += 1
        if failures >= WATCHDOG_FAILURES_BEFORE_EXIT:
            logger.error("Watchdog failed %d times in a row; exiting so systemd restarts us",
                         failures)
            # os._exit: don't wait on threads that may be stuck in hardware calls
            os._exit(1)


# ---------------------------------------------------------------------------
# Install / uninstall as a systemd service
# ---------------------------------------------------------------------------

def unit_text(args, script_path):
    run_args = " ".join(shlex.quote(a) for a in [
        script_path, "--host", args.host, "--port", str(args.port),
        "--name", args.name, "--bitfile", args.bitfile])
    # The PYNQ venv and XRT environment are normally set up by login shells;
    # a systemd service has to source them itself.
    shell_cmd = (
        "for f in /etc/profile.d/pynq_venv.sh /etc/profile.d/xrt_setup.sh; "
        "do [ -f \"$f\" ] && . \"$f\"; done; "
        f"exec {PYNQ_PYTHON} -u {run_args}"
    )
    # systemd expands $VAR and %x itself; escape them so bash sees them.
    shell_cmd = shell_cmd.replace("$", "$$").replace("%", "%%")
    return (
        "[Unit]\n"
        "Description=QICK-DAWG Pyro4 name server and QICK daemon\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "StartLimitIntervalSec=0\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "User=root\n"
        f"WorkingDirectory={INSTALL_DIR}\n"
        f"ExecStart=/bin/bash -c {shlex.quote(shell_cmd)}\n"
        "Restart=always\n"
        "RestartSec=10\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def run(cmd):
    print("+ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def install(args):
    if os.geteuid() != 0:
        sys.exit("Run the install with sudo.")
    if not os.path.exists(args.bitfile):
        sys.exit(f"Bitfile not found: {args.bitfile}\n"
                 "Use the qick_path from your qick_daemon notebook with --bitfile.")
    if not os.path.exists(PYNQ_PYTHON):
        sys.exit(f"{PYNQ_PYTHON} not found. Is this a PYNQ 3.x image?")

    os.makedirs(INSTALL_DIR, exist_ok=True)
    script_path = os.path.join(INSTALL_DIR, "qick_server.py")
    with open(os.path.abspath(__file__), "r") as src:
        text = src.read()
    with open(script_path, "w", newline="\n") as dst:
        dst.write(text.replace("\r\n", "\n"))
    print(f"Copied server script to {script_path}")

    with open(SYSCTL_PATH, "w", newline="\n") as f:
        f.write(SYSCTL_TEXT)
    run(["sysctl", "-p", SYSCTL_PATH])

    with open(SERVICE_PATH, "w", newline="\n") as f:
        f.write(unit_text(args, script_path))
    print(f"Wrote {SERVICE_PATH}")

    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", SERVICE_NAME])
    run(["systemctl", "restart", SERVICE_NAME])
    print("\nInstalled. Do NOT run name_server.ipynb or qick_daemon.ipynb any more.")
    print(f"Watch the log with:  sudo journalctl -u {SERVICE_NAME} -f")


def uninstall(args):
    if os.geteuid() != 0:
        sys.exit("Run the uninstall with sudo.")
    subprocess.run(["systemctl", "disable", "--now", SERVICE_NAME])
    for path in (SERVICE_PATH, SYSCTL_PATH):
        if os.path.exists(path):
            os.remove(path)
            print(f"Removed {path}")
    run(["systemctl", "daemon-reload"])
    print("Uninstalled. You can go back to the run_server notebooks.")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", default=DEFAULT_HOST,
                   help="Board IP address the lab PC connects to (default %(default)s)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help="Name server port (default %(default)s)")
    p.add_argument("--name", default=DEFAULT_NAME,
                   help="Name the QICK object is registered under (default %(default)s)")
    p.add_argument("--bitfile", default=DEFAULT_BITFILE, help="QICK firmware .bit file")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true",
                   help="Install and start as a systemd service (needs sudo)")
    g.add_argument("--uninstall", action="store_true",
                   help="Stop and remove the systemd service (needs sudo)")
    return p.parse_args(argv)


def main(argv=None):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    if args.install:
        install(args)
    elif args.uninstall:
        uninstall(args)
    else:
        serve(args)


if __name__ == "__main__":
    main()
