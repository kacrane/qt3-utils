"""
Instrument hub for QT3 Microscope.

Each device gets one worker thread and one queue, so two panels never talk
to the same instrument at the same time, and a slow device never freezes
the window. Panels hand the hub a function to run; the result comes back
on the Tk thread through ``on_done(ok, result)``.
"""

import logging
import queue
import threading

logger = logging.getLogger(__name__)


class DeviceWorker:
    """Runs jobs for one device, one at a time, on its own thread."""

    def __init__(self, name, results):
        self.name = name
        self._jobs = queue.Queue()
        self._results = results
        self._thread = threading.Thread(target=self._loop, name=f"hub-{name}", daemon=True)
        self._thread.start()

    def submit(self, job, fn, on_done=None, quiet=False):
        self._jobs.put((job, fn, on_done, quiet))

    def pending(self):
        return self._jobs.qsize()

    def _loop(self):
        while True:
            job, fn, on_done, quiet = self._jobs.get()
            try:
                result = fn()
                self._results.put((on_done, True, result))
            except Exception as e:  # report every failure back to the panel
                logger.warning("%s: %s failed: %r", self.name, job, e, exc_info=not quiet)
                self._results.put((on_done, False, e))


class Hub:
    """Owns the device workers and delivers their results to the Tk thread."""

    def __init__(self, tk_widget, poll_ms=50):
        self._tk = tk_widget
        self._poll_ms = poll_ms
        self._results = queue.Queue()
        self._workers = {}
        self._closers = []
        self._closed = False
        self._tk.after(self._poll_ms, self._poll)

    def worker(self, name):
        if name not in self._workers:
            self._workers[name] = DeviceWorker(name, self._results)
        return self._workers[name]

    def on_close(self, fn):
        """Register a function to run when the window closes."""
        self._closers.append(fn)

    def close(self):
        self._closed = True
        for fn in self._closers:
            try:
                fn()
            except Exception as e:
                logger.warning("close step failed: %r", e)

    def _poll(self):
        try:
            while True:
                on_done, ok, result = self._results.get_nowait()
                if on_done is not None:
                    try:
                        on_done(ok, result)
                    except Exception:
                        logger.exception("panel callback failed")
        except queue.Empty:
            pass
        if not self._closed:
            self._tk.after(self._poll_ms, self._poll)
