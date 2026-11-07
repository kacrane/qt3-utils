"""
Console capture for QT3 Microscope.

The desktop shortcut starts the program with pythonw.exe, which has no
terminal window. Everything that used to go to the terminal (print output,
logging messages, tracebacks from Tk callbacks and worker threads) is
caught here instead and shown in the Log section under the scope. It is
also written to ~/.qt3utils/qt3microscope_console.log, so a crash at
startup, before the window exists, still leaves a record. The previous
run's file is kept as qt3microscope_console.prev.log.

install() must run before logging is configured, so the logging handler
writes into the capture rather than into the original (possibly missing)
stderr.
"""

import collections
import datetime
import io
import os
import sys
import threading

LOG_DIR = os.path.join(os.path.expanduser("~"), ".qt3utils")
CONSOLE_LOG = os.path.join(LOG_DIR, "qt3microscope_console.log")
PREV_LOG = os.path.join(LOG_DIR, "qt3microscope_console.prev.log")
KEEP_LINES = 5000  # lines kept for the Log section; the file keeps everything

_lock = threading.Lock()
_lines = collections.deque(maxlen=KEEP_LINES)  # (seq, stream, text)
_seq = 0
_file = None
_installed = False


def _open_file():
    global _file
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        if os.path.exists(CONSOLE_LOG):
            os.replace(CONSOLE_LOG, PREV_LOG)
        _file = open(CONSOLE_LOG, "w", encoding="utf-8", buffering=1)
        _file.write(f"QT3 Microscope started {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
    except OSError:
        _file = None


class _Tee(io.TextIOBase):
    """Stands in for sys.stdout or sys.stderr: keeps each finished line for the
    Log section, writes it to the console log file, and passes it on to the
    real stream when there is one (a terminal, when started from one)."""

    def __init__(self, name, original):
        self.name = name
        self.original = original
        self._partial = ""

    @property
    def encoding(self):
        return getattr(self.original, "encoding", None) or "utf-8"

    def writable(self):
        return True

    def isatty(self):
        return False

    def fileno(self):
        if self.original is not None:
            return self.original.fileno()
        raise io.UnsupportedOperation("no terminal")

    def write(self, text):
        global _seq
        if not isinstance(text, str):
            text = str(text)
        if self.original is not None:
            try:
                self.original.write(text)
            except Exception:
                pass
        with _lock:
            if _file is not None:
                try:
                    _file.write(text)
                except Exception:
                    pass
            parts = (self._partial + text).split("\n")
            self._partial = parts.pop()
            for line in parts:
                _seq += 1
                _lines.append((_seq, self.name, line.rstrip("\r")))
        return len(text)

    def flush(self):
        if self.original is not None:
            try:
                self.original.flush()
            except Exception:
                pass
        if _file is not None:
            try:
                _file.flush()
            except Exception:
                pass


def install():
    """Replace sys.stdout and sys.stderr with the capture (once)."""
    global _installed
    if _installed:
        return
    _installed = True
    _open_file()
    sys.stdout = _Tee("stdout", sys.stdout)
    sys.stderr = _Tee("stderr", sys.stderr)


def lines_since(seq):
    """Lines captured after sequence number seq: list of (seq, stream, text)."""
    with _lock:
        return [item for item in _lines if item[0] > seq]


def clear():
    with _lock:
        _lines.clear()
