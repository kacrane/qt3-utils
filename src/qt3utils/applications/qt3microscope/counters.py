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
