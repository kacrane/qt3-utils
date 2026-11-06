"""
Laser panel: turns the VQLM laser on and off through the QICK-DAWG board.

Same behaviour as the standalone qt3laser app, placed in a drawer section.
The section header keeps the laser state and ON/OFF buttons visible even
when the section is folded.
"""

import logging
import time
import tkinter as tk
from tkinter import ttk

from qt3utils.applications.qt3microscope.widgets import GRAY, GREEN, ORANGE, RED

logger = logging.getLogger(__name__)


class LaserPanel:
    def __init__(self, section, hub, client, health_check_s=30, set_status=None):
        self.section = section
        self.client = client
        self.worker = hub.worker("qickdawg")
        self.health_check_ms = int(health_check_s * 1000)
        self.set_status = set_status or (lambda value, colour: None)
        self._busy = False
        self._closing = False
        self._build(section.body)
        hub.on_close(self.close)
        self._submit("connect", self.client.connect)
        section.after(self.health_check_ms, self._health_tick)

    # -- GUI ---------------------------------------------------------------
    def _build(self, body):
        conn = ttk.Frame(body)
        conn.pack(fill="x")
        self.conn_dot = tk.Canvas(conn, width=14, height=14, highlightthickness=0)
        self.conn_dot.pack(side="left")
        self._dot = self.conn_dot.create_oval(2, 2, 12, 12, fill=GRAY, outline="")
        self.conn_var = tk.StringVar(value=f"Connecting to {self.client.host}...")
        ttk.Label(conn, textvariable=self.conn_var).pack(side="left", padx=(6, 10))
        self.reconnect_btn = ttk.Button(conn, text="Reconnect", command=self.reconnect)
        self.reconnect_btn.pack(side="right")

        self.state_label = tk.Label(body, text="Laser: unknown", font=("TkDefaultFont", 14, "bold"),
                                    fg="white", bg=ORANGE, pady=4)
        self.state_label.pack(fill="x", pady=6)

        btns = ttk.Frame(body)
        btns.pack(fill="x")
        self.on_btn = tk.Button(btns, text="Laser ON", height=1, command=self.laser_on)
        self.on_btn.pack(side="left", expand=True, fill="x", padx=(0, 3))
        self.off_btn = tk.Button(btns, text="Laser OFF", height=1, command=self.laser_off)
        self.off_btn.pack(side="left", expand=True, fill="x", padx=(3, 0))

        self.msg_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.msg_var, foreground=GRAY,
                  wraplength=int(330 * max(1.0, body.winfo_fpixels("1i") / 96.0)), justify="left").pack(anchor="w", pady=(6, 0))

        # Header buttons: usable while the section is folded.
        self.q_on = tk.Button(self.section.quick, text="ON", padx=6, pady=0, command=self.laser_on)
        self.q_off = tk.Button(self.section.quick, text="OFF", padx=6, pady=0, command=self.laser_off)
        self.q_on.pack(side="left", padx=1)
        self.q_off.pack(side="left", padx=(1, 4))
        self._show_state(None)

    def _set_connection(self, ok, text):
        self.conn_dot.itemconfigure(self._dot, fill=GREEN if ok else RED)
        self.conn_var.set(text)

    def _show_state(self, state, note=""):
        if state is True:
            self.state_label.configure(text="Laser: ON", bg=GREEN)
            self.section.set_summary("ON")
            self.set_status("ON", GREEN)
        elif state is False:
            self.state_label.configure(text="Laser: OFF", bg=GRAY)
            self.section.set_summary("OFF")
            self.set_status("OFF", GRAY)
        else:
            self.state_label.configure(text="Laser: unknown", bg=ORANGE)
            self.section.set_summary("unknown")
            self.set_status("?", ORANGE)
        if note:
            self.msg_var.set(note)

    def _set_busy(self, busy):
        self._busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.on_btn, self.off_btn, self.q_on, self.q_off):
            b.configure(state=state)
        self.reconnect_btn.configure(state=state)

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
            if self.client.connected:
                self._submit("ping", self.client.ping, quiet=True)
            else:
                self._submit("connect", self.client.connect, quiet=True)
        self.section.after(self.health_check_ms, self._health_tick)

    def _submit(self, name, fn, quiet=False):
        if self._busy:
            return
        if not quiet:
            self._set_busy(True)
            self.msg_var.set({"connect": f"Connecting to {self.client.host}...",
                              "laser_on": "Turning laser on...",
                              "laser_off": "Turning laser off..."}.get(name, ""))
        self.worker.submit(name, fn, lambda ok, res: self._done(name, ok, res, quiet), quiet=quiet)

    # -- results (on the Tk thread) -----------------------------------------
    def _done(self, name, ok, result, quiet):
        if self._closing:
            return
        if not quiet:
            self._set_busy(False)
        stamp = time.strftime("%H:%M:%S")
        restart_note = (f"{stamp}  The board server restarted, so the laser pin may "
                        "have reset. Press ON or OFF to set it.")
        if name == "ping":
            if ok and result:
                self._set_connection(True, f"Connected to {self.client.host}")
            elif self.client.server_restarted:
                self.client.server_restarted = False
                self._set_connection(False, "Board server restarted, reconnecting")
                self._show_state(None, restart_note)
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
        if name == "connect":
            if self.client.server_restarted:
                self.client.server_restarted = False
                self._show_state(None, restart_note)
            elif self.client.laser_state is None:
                self._show_state(None, f"{stamp}  Connected. Press ON or OFF to set the laser.")
            else:
                self._show_state(self.client.laser_state, f"{stamp}  Reconnected")
        elif name == "laser_on":
            self.client.server_restarted = False
            self._show_state(True, f"{stamp}  Laser on")
        elif name == "laser_off":
            self.client.server_restarted = False
            self._show_state(False, f"{stamp}  Laser off")

    def close(self):
        self._closing = True
        try:
            self.client.close()
        except Exception:
            pass
