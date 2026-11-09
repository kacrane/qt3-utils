"""
Photon counter for the scope strip and the scans, chosen by the profile.

VQLM: an SPCM on an NI DAQ counter. The profile's scope section gives the
DAQ fields directly (daq_name, signal_terminal, signal_counter, ...).

TQLM: the Single Quantum SNSPD over Ethernet. The scope section has a
counter entry naming the class, as qt3scope's YAML files do:

    scope:
      detector: SNSPD
      counter:
        import_path: qt3utils.hardware.singlequantum.websq_timed_counter
        class_name: WebSqTimedCounter
        configure: {host: "3.3.44.57", counts_port: 12345, ...}

Both kinds have the same methods (start, stop, configure_sample_time,
sample_batch_counts), so the scope and the scans don't care which it is.
"""

import importlib


def counter_spec(cfg):
    """The counter entry of a scope section, or None for the NI DAQ fields."""
    spec = (cfg or {}).get("counter")
    return spec if isinstance(spec, dict) and spec.get("class_name") else None


def is_daq_counter(cfg):
    return counter_spec(cfg) is None


def make_counter(cfg, sample_time):
    """Build the counter the scope section describes (call on its worker)."""
    spec = counter_spec(cfg)
    if spec is not None:
        mod = importlib.import_module(spec["import_path"])
        counter = getattr(mod, spec["class_name"])()
        counter.configure(dict(spec.get("configure") or {}))
        counter.configure_sample_time(sample_time=sample_time)
        return counter
    from qt3utils.hardware.nidaq.counters.nidaqtimedratecounter import NidaqTimedRateCounter
    c = cfg or {}
    return NidaqTimedRateCounter(
        daq_name=c.get("daq_name", "Dev1"), signal_terminal=c.get("signal_terminal", "PFI0"),
        clock_rate=int(c.get("clock_rate", 100000)), sample_time_in_seconds=sample_time,
        clock_terminal=(c.get("clock_terminal") or None),
        read_write_timeout=float(c.get("read_write_timeout", 10)),
        signal_counter=c.get("signal_counter", "ctr2"))


def detector_name(cfg):
    return (cfg or {}).get("detector") or ("SPCM" if is_daq_counter(cfg) else "Counter")


def source_text(cfg):
    """Where the counts come from, for the scope's status line."""
    spec = counter_spec(cfg)
    if spec is not None:
        conf = spec.get("configure") or {}
        where = conf.get("host")
        if where and conf.get("counts_port"):
            where = f"{where}:{conf['counts_port']}"
        return where or spec["class_name"]
    c = cfg or {}
    return f"{c.get('daq_name', 'Dev1')}/{c.get('signal_terminal', 'PFI0')} ({c.get('signal_counter', 'ctr2')})"


_warning = {"win": None}  # the open "no counts" window, if any


def _has_counts(values):
    import numpy as np
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    return v.size > 0 and bool(np.any(v != 0))


def warn_if_no_counts(widget, values, cfg, what="scan"):
    """A first read of exactly 0 counts almost always means the detector is
    off (Kendall, 2026-11-09). Opens a warning window (the measurement keeps
    running) and logs it; clear_no_counts_warning() closes the window once
    counts come in. Returns True when it warned. Only for profiles whose
    scope section sets warn_zero_counts: true (the VQLM SPCM)."""
    if not (cfg or {}).get("warn_zero_counts", False):
        return False
    try:
        import numpy as np
        v = np.asarray(values, dtype=float)
        if v[np.isfinite(v)].size == 0 or _has_counts(values):
            return False
    except Exception:
        return False
    name = detector_name(cfg)
    text = (f"The {name} read 0 counts at the start of this {what}. Check that the {name} is "
            f"switched on, and that the flip mirrors send the light to it.")
    print(f"WARNING: {text}")
    widget.after(10, lambda: _show(widget, f"No counts from the {name}", text))  # not inside the data callback
    return True


def _show(widget, title, text):
    import tkinter as tk
    from tkinter import ttk
    win = _warning["win"]
    try:
        if win is not None and win.winfo_exists():
            win.msg.configure(text=text)
            win.lift()
            return
    except tk.TclError:
        pass
    try:
        top = widget.winfo_toplevel()
        win = tk.Toplevel(top)
        win.title(title)
        win.transient(top)
        win.resizable(False, False)
        scale = max(1.0, win.winfo_fpixels("1i") / 96.0)
        body = ttk.Frame(win, padding=int(14 * scale))
        body.pack(fill="both", expand=True)
        win.msg = ttk.Label(body, text=text, wraplength=int(360 * scale), justify="left")
        win.msg.pack(anchor="w")
        ttk.Label(body, text="This closes by itself once counts come in.",
                  wraplength=int(360 * scale), justify="left").pack(anchor="w", pady=(int(6 * scale), 0))
        ttk.Button(body, text="OK", command=win.destroy).pack(anchor="e", pady=(int(10 * scale), 0))
        win.update_idletasks()
        x = top.winfo_rootx() + (top.winfo_width() - win.winfo_reqwidth()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        _warning["win"] = win
    except tk.TclError:
        pass


def clear_no_counts_warning(values):
    """Close the "no counts" window once a read is not all zeros."""
    win = _warning["win"]
    if win is None:
        return
    try:
        if not _has_counts(values):
            return
    except Exception:
        return
    _warning["win"] = None
    try:
        win.destroy()
        print("Counts are coming in again; the no-counts warning closed.")
    except Exception:
        pass
