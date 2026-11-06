"""
qdllaser: turn the VQLM laser on and off through the QICK-DAWG board.

The laser gate is a PMOD pin on the QICK-DAWG. This app connects with
QickDawgClient, which reconnects by itself if the board reboots or the link
drops, and checks the board every 30 s so the status light stays honest.

Run:  python -m qdlutils.applications.qdllaser.main
      python -m qdlutils.applications.qdllaser.main --host 192.168.3.1
Settings: config_files/qdllaser_base.yaml (or --config my.yaml)
"""

import argparse
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

import yaml

from qdlutils.hardware.qickdawg.client import QickDawgClient

logger = logging.getLogger(__name__)
logging.basicConfig()

# Keep a log file so a failure seen hours later can still be diagnosed.
LOG_PATH = os.path.join(os.path.expanduser("~"), ".qdlutils", "qdllaser.log")
try:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    _fh = logging.FileHandler(LOG_PATH)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _fh.setLevel(logging.INFO)
    for _name in ("qdlutils.applications.qdllaser", "qdlutils.hardware.qickdawg"):
        logging.getLogger(_name).addHandler(_fh)
        logging.getLogger(_name).setLevel(logging.INFO)
except OSError:
    pass

CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_files")
DEFAULT_CONFIG = os.path.join(CONFIG_DIR, "qdllaser_base.yaml")

GREEN = "#2e9d3a"
RED = "#c62828"
GRAY = "#888888"
ORANGE = "#e08a00"


def load_config(path=None):
    with open(path or DEFAULT_CONFIG, "r") as f:
        cfg = yaml.safe_load(f)["QDLLASER"]
    return cfg.get("QickDawg", {}), cfg.get("Laser", {})


class LaserApp:
    def __init__(self, window, client, health_check_s=30):
        self.window = window
        self.client = client
        self.health_check_ms = int(health_check_s * 1000)
        self._jobs = queue.Queue()
        self._results = queue.Queue()
        self._busy = False
        self._closing = False

        window.title("qdllaser")
        window.resizable(False, False)
        self._build()
        window.protocol("WM_DELETE_WINDOW", self.close)

        self._worker = threading.Thread(target=self._work_loop, daemon=True)
        self._worker.start()
        self.window.after(100, self._poll_results)
        self._submit("connect", self.client.connect)
        self.window.after(self.health_check_ms, self._health_tick)

    # -- GUI ---------------------------------------------------------------

    def _build(self):
        outer = ttk.Frame(self.window, padding=12)
        outer.grid(row=0, column=0, sticky="nsew")

        ttk.Label(outer, text="VQLM laser (QICK-DAWG)",
                  font=("TkDefaultFont", 12, "bold")).grid(row=0, column=0, columnspan=2, pady=(0, 8))

        conn = ttk.Frame(outer)
        conn.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.conn_dot = tk.Canvas(conn, width=14, height=14, highlightthickness=0)
        self.conn_dot.pack(side=tk.LEFT)
        self._dot = self.conn_dot.create_oval(2, 2, 12, 12, fill=GRAY, outline="")
        self.conn_var = tk.StringVar(value=f"Connecting to {self.client.host}...")
        ttk.Label(conn, textvariable=self.conn_var).pack(side=tk.LEFT, padx=(6, 10))
        self.reconnect_btn = ttk.Button(conn, text="Reconnect", command=self.reconnect)
        self.reconnect_btn.pack(side=tk.RIGHT)

        self.state_label = tk.Label(outer, text="Laser: unknown", font=("TkDefaultFont", 16, "bold"),
                                    fg="white", bg=GRAY, width=16, pady=6)
        self.state_label.grid(row=2, column=0, columnspan=2, pady=10, sticky="ew")

        self.on_btn = tk.Button(outer, text="Laser ON", width=12, height=2,
                                command=self.laser_on)
        self.on_btn.grid(row=3, column=0, padx=(0, 4))
        self.off_btn = tk.Button(outer, text="Laser OFF", width=12, height=2,
                                 command=self.laser_off)
        self.off_btn.grid(row=3, column=1, padx=(4, 0))

        self.msg_var = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self.msg_var, foreground=GRAY,
                  wraplength=260, justify=tk.LEFT).grid(row=4, column=0, columnspan=2,
                                                        sticky="w", pady=(8, 0))

    def _set_connection(self, ok, text):
        self.conn_dot.itemconfigure(self._dot, fill=GREEN if ok else RED)
        self.conn_var.set(text)

    def _set_laser_state(self, state, note=""):
        if state is True:
            self.state_label.configure(text="Laser: ON", bg=GREEN)
        elif state is False:
            self.state_label.configure(text="Laser: OFF", bg=GRAY)
        else:
            self.state_label.configure(text="Laser: unknown", bg=ORANGE)
        if note:
            self.msg_var.set(note)

    def _set_busy(self, busy):
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        for b in (self.on_btn, self.off_btn, self.reconnect_btn):
            b.configure(state=state)

    # -- actions -----------------------------------------------------------

    def laser_on(self):
        self._submit("laser_on", self.client.laser_on)

    def laser_off(self):
        self._submit("laser_off", self.client.laser_off)

    def reconnect(self):
        self._submit("connect", self.client.connect)

    def _health_tick(self):
        if self._closing:
            return
        if not self._busy:
            # Not connected: try to reconnect. Connected: just ping.
            if self.client.connected:
                self._submit("ping", self.client.ping, quiet=True)
            else:
                self._submit("connect", self.client.connect, quiet=True)
        self.window.after(self.health_check_ms, self._health_tick)

    # -- background worker -------------------------------------------------

    def _submit(self, name, fn, quiet=False):
        if self._busy:
            return
        if not quiet:
            self._set_busy(True)
            self.msg_var.set({"connect": f"Connecting to {self.client.host}...",
                              "laser_on": "Turning laser on...",
                              "laser_off": "Turning laser off..."}.get(name, ""))
        self._jobs.put((name, fn, quiet))

    def _work_loop(self):
        while True:
            name, fn, quiet = self._jobs.get()
            was_connected = self.client.connected
            try:
                result = fn()
                self._results.put((name, True, result, was_connected, quiet))
            except Exception as e:
                logger.warning("qdllaser %s failed: %r", name, e, exc_info=not quiet)
                self._results.put((name, False, e, was_connected, quiet))

    def _poll_results(self):
        try:
            while True:
                self._handle_result(*self._results.get_nowait())
        except queue.Empty:
            pass
        if not self._closing:
            self.window.after(100, self._poll_results)

    def _handle_result(self, name, ok, result, was_connected, quiet):
        if not quiet:
            self._set_busy(False)
        stamp = time.strftime("%H:%M:%S")
        if name == "ping":
            ok = bool(result)
            if ok:
                self._set_connection(True, f"Connected to {self.client.host}")
            elif self.client.server_restarted:
                self.client.server_restarted = False
                self._set_connection(False, "Board server restarted, reconnecting")
                self._set_laser_state(None, f"{stamp}  The board server restarted, so the laser "
                                            "pin may have reset. Press ON or OFF to set it.")
            else:
                self._set_connection(False, "Lost connection, retrying")
                self.msg_var.set(f"{stamp}  Board stopped answering. Retrying every "
                                 f"{self.health_check_ms // 1000} s.")
            return
        if not ok:
            self._set_connection(self.client.connected,
                                 f"Connected to {self.client.host}" if self.client.connected
                                 else "Not connected, retrying")
            if not quiet:
                self.msg_var.set(f"{stamp}  {result}")
            return
        self._set_connection(True, f"Connected to {self.client.host}")
        if self.client.server_restarted and name == "connect":
            self.client.server_restarted = False
            self._set_laser_state(None, f"{stamp}  The board server restarted, so the laser "
                                        "pin may have reset. Press ON or OFF to set it.")
        elif name == "connect":
            # Same board server as before (no restart), so the pin kept its state.
            if self.client.laser_state is None:
                self._set_laser_state(None, f"{stamp}  Connected. Press ON or OFF to set the laser.")
            else:
                self.msg_var.set(f"{stamp}  Reconnected")
        elif name == "laser_on":
            self.client.server_restarted = False
            self._set_laser_state(True, f"{stamp}  Laser on")
        elif name == "laser_off":
            self.client.server_restarted = False
            self._set_laser_state(False, f"{stamp}  Laser off")

    def close(self):
        self._closing = True
        try:
            self.client.close()
        except Exception:
            pass
        self.window.destroy()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Turn the VQLM laser on/off through the QICK-DAWG.")
    p.add_argument("--config", default=None, help="YAML settings file (default: qdllaser_base.yaml)")
    p.add_argument("--host", default=None, help="Board IP (overrides the config file)")
    return p.parse_args(argv)


def main(is_root_process=True, argv=None):
    # When launched from qdlhome, ignore qdlhome's own command-line arguments.
    args = parse_args(argv if (argv is not None or is_root_process) else [])
    conn_cfg, laser_cfg = load_config(args.config)
    client = QickDawgClient(
        host=args.host or conn_cfg.get("host", "192.168.3.1"),
        port=conn_cfg.get("port", 8888),
        server_name=conn_cfg.get("server_name", "myqick"),
        timeout_s=conn_cfg.get("timeout_s", 10),
        laser_settings=laser_cfg,
    )
    if is_root_process:
        window = tk.Tk()
    else:
        window = tk.Toplevel()
    # Place it beside qdlhome so it doesn't cover other apps
    window.geometry("+320+40")
    LaserApp(window, client, health_check_s=conn_cfg.get("health_check_s", 30))
    if is_root_process:
        window.mainloop()


if __name__ == "__main__":
    main(argv=sys.argv[1:])
