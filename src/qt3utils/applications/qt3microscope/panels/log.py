"""
Log section under the scope: everything that used to go to the terminal
window (print output, logging messages and tracebacks), caught by
console.py. Folded by default; the header counts errors and warnings so a
problem shows even while it is folded.
"""

import os
import re
import subprocess
import sys
import tkinter as tk
from tkinter import ttk

from qt3utils.applications.qt3microscope import console
from qt3utils.applications.qt3microscope.widgets import GRAY, ORANGE, RED

POLL_MS = 200
ERROR_RE = re.compile(r"\b(ERROR|CRITICAL)\b|Error:")
WARN_RE = re.compile(r"\bWARNING\b|Warning:")


class LogPanel:
    def __init__(self, section, scale=1.0):
        self.section = section
        self.seq = 0
        self.errors = 0
        self.warnings = 0
        self._in_traceback = False
        body = section.body

        bar = ttk.Frame(body)
        bar.pack(fill="x")
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="left")
        ttk.Button(bar, text="Copy all", command=self.copy_all).pack(side="left", padx=4)
        ttk.Button(bar, text="Open log file", command=self.open_file).pack(side="left")
        self.follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Follow newest", variable=self.follow).pack(side="left", padx=8)
        ttk.Label(bar, text=f"Also saved to {console.CONSOLE_LOG}", foreground=GRAY).pack(side="right")

        box = ttk.Frame(body)
        box.pack(fill="both", expand=True, pady=(4, 0))
        self.text = tk.Text(box, height=10, wrap="none", font=("Courier New", 9),
                            bg="#fbfbfb", relief="sunken", bd=1)
        ys = ttk.Scrollbar(box, orient="vertical", command=self.text.yview)
        xs = ttk.Scrollbar(box, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("error", foreground=RED)
        self.text.tag_configure("warning", foreground=ORANGE)
        self.text.configure(state="disabled")
        self._summary()
        body.after(POLL_MS, self._poll)

    def _kind(self, line):
        """Returns (tag, counts): the colour tag for the line, and whether it
        starts a new error or warning for the header count."""
        if line.startswith("Exception in "):  # "Exception in Tkinter callback", then a traceback
            return "error", False
        if line.startswith("Traceback"):
            self._in_traceback = True
            return "error", True
        if self._in_traceback:
            # A traceback ends with its unindented "SomeError: message" line.
            if line and not line[0].isspace():
                self._in_traceback = False
            return "error", False
        if WARN_RE.search(line):  # first: a warning may quote an error's name
            return "warning", True
        if ERROR_RE.search(line):
            return "error", True
        return None, False

    def _poll(self):
        try:
            new = console.lines_since(self.seq)
            if new:
                self.seq = new[-1][0]
                self.text.configure(state="normal")
                for _, _, line in new:
                    kind, counts = self._kind(line)
                    if counts and kind == "error":
                        self.errors += 1
                    elif counts and kind == "warning":
                        self.warnings += 1
                    self.text.insert("end", line + "\n", (kind,) if kind else ())
                extra = int(self.text.index("end-1c").split(".")[0]) - console.KEEP_LINES
                if extra > 0:
                    self.text.delete("1.0", f"{extra + 1}.0")
                self.text.configure(state="disabled")
                if self.follow.get():
                    self.text.see("end")
                self._summary()
        except tk.TclError:
            return  # window closing
        self.section.body.after(POLL_MS, self._poll)

    def _summary(self):
        parts = []
        if self.errors:
            parts.append(f"{self.errors} error{'s' if self.errors != 1 else ''}")
        if self.warnings:
            parts.append(f"{self.warnings} warning{'s' if self.warnings != 1 else ''}")
        text = ", ".join(parts) if parts else "no errors"
        colour = RED if self.errors else (ORANGE if self.warnings else GRAY)
        self.section.set_summary(text)
        self.section.summary.configure(fg=colour)

    def clear(self):
        console.clear()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self.errors = self.warnings = 0
        self._in_traceback = False
        self._summary()

    def copy_all(self):
        self.text.clipboard_clear()
        self.text.clipboard_append(self.text.get("1.0", "end-1c"))

    def open_file(self):
        path = console.CONSOLE_LOG
        try:
            sys.stdout.flush()
            if sys.platform == "win32":
                os.startfile(path)  # opens in Notepad (or the default .log viewer)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            print(f"Could not open {path}: {e}")
