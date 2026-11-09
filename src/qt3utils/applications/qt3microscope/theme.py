"""
Light and dark colour themes for QT3 Microscope.

Panels take colours from theme.C at the moment they draw (theme.C.muted,
theme.C.text, ...), never as hex strings, so one switch here recolours the
whole window. apply() runs once, before any widget exists; the theme is
chosen in File > Preferences and takes effect on the next start.

Saved PNGs stay light in both themes (save_figure).

Status colours (green, orange, red, amber) keep their meaning in both
themes; the dark versions are only brightened where they are used as text.
"""

import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace

LIGHT = dict(
    dark=False,
    bg="#f0f0f0",           # window and frames (Tk's default on Windows is close to this)
    panel="#f0f0f0",
    field="#ffffff",         # entries, text boxes, lists
    text="#000000",
    muted="#555555",         # secondary text: notes, hints, units
    faint="#888888",         # least important text, idle status dot
    header="#c9c9c9",        # drawer section headers
    header_active="#b5b5b5",
    button="#e3e3e3",
    border="#d0d0d0",
    select="#cce4ff",
    tooltip="#ffffe0",
    accent="#1864ab",        # blue: shown scan, current position
    reading="#1d4fb0",       # big blue readings (count rate, power)
    green="#2e9d3a", go="#1f8f2f", orange="#e08a00", red="#c62828", stop="#e33333", amber="#f2c14e",
    pin="#ff9f1c",           # orange: pinned scan
    play_bg="#d8f0d8", pause_bg="#f3e0b5",
    log_bg="#fbfbfb",
    plot_bg="#ffffff", plot_text="#000000", plot_grid="#b0b0b0", plot_line="#000000",
    plot_unscanned="#e6e6e6",
)

DARK = dict(
    dark=True,
    bg="#1f1f1f",
    panel="#262626",
    field="#2d2d2d",
    text="#e6e6e6",
    muted="#a8a8a8",
    faint="#8a8a8a",
    header="#383838",
    header_active="#4a4a4a",
    button="#3a3a3a",
    border="#4a4a4a",
    select="#264f78",
    tooltip="#3a3a2a",
    accent="#4dabf7",
    reading="#74b4ff",
    green="#3fb950", go="#238636", orange="#f0a020", red="#f05252", stop="#d73a3a", amber="#d4a72c",
    pin="#ffa94d",
    play_bg="#2c4a2c", pause_bg="#5a4a28",
    log_bg="#1a1a1a",
    plot_bg="#1f1f1f", plot_text="#dddddd", plot_grid="#4a4a4a", plot_line="#e6e6e6",
    plot_unscanned="#3a3a3a",
)

C = SimpleNamespace(**LIGHT)


def apply(root, dark=False):
    """Set the theme for everything built after this call (call before building)."""
    C.__dict__.update(DARK if dark else LIGHT)
    # Keep widgets.py's module colours (imported by older code) in step.
    from qt3utils.applications.qt3microscope import widgets
    widgets.GREEN, widgets.ORANGE, widgets.RED = C.green, C.orange, C.red
    widgets.GRAY, widgets.AMBER, widgets.HEADER_BG = C.faint, C.amber, C.header
    _apply_matplotlib()
    if not dark:
        return  # light: leave Tk's native look alone
    _apply_tk_dark(root)


def _apply_tk_dark(root):
    # Plain tk widgets read their defaults from the option database.
    for pattern, value in (
            ("*Background", C.bg), ("*Foreground", C.text),
            ("*activeBackground", C.header_active), ("*activeForeground", C.text),
            ("*highlightBackground", C.bg), ("*highlightColor", C.accent),
            ("*selectBackground", C.select), ("*selectForeground", C.text),
            ("*insertBackground", C.text), ("*disabledForeground", C.faint),
            ("*troughColor", C.panel),
            ("*Entry.Background", C.field), ("*Text.Background", C.field),
            ("*Listbox.Background", C.field), ("*Spinbox.Background", C.field),
            ("*Button.Background", C.button), ("*Canvas.Background", C.bg),
            ("*Menu.Background", C.panel), ("*Menu.Foreground", C.text),
            ("*Menu.activeBackground", C.select), ("*Menu.activeForeground", C.text),
            ("*TCombobox*Listbox.background", C.field), ("*TCombobox*Listbox.foreground", C.text)):
        root.option_add(pattern, value)
    root.configure(background=C.bg)

    # ttk widgets: the "clam" theme can be recoloured completely.
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=C.bg, foreground=C.text, fieldbackground=C.field,
                bordercolor=C.border, darkcolor=C.bg, lightcolor=C.panel,
                troughcolor=C.panel, selectbackground=C.select, selectforeground=C.text,
                insertcolor=C.text, arrowcolor=C.text, focuscolor=C.accent)
    s.map(".", foreground=[("disabled", C.faint)], background=[("disabled", C.bg)])
    s.configure("TButton", background=C.button, foreground=C.text, bordercolor=C.border)
    s.map("TButton", background=[("disabled", C.bg), ("pressed", C.header_active), ("active", C.header_active)],
          foreground=[("disabled", C.faint)])
    for w in ("TEntry", "TCombobox", "TSpinbox"):
        s.configure(w, fieldbackground=C.field, foreground=C.text, background=C.button)
        s.map(w, fieldbackground=[("readonly", C.field), ("disabled", C.bg)],
              foreground=[("disabled", C.faint)])
    s.configure("TNotebook", background=C.bg, bordercolor=C.border)
    s.configure("TNotebook.Tab", background=C.panel, foreground=C.muted, bordercolor=C.border, padding=(8, 3))
    s.map("TNotebook.Tab", background=[("selected", C.header)], foreground=[("selected", C.text)])
    for w in ("TCheckbutton", "TRadiobutton"):
        s.configure(w, background=C.bg, foreground=C.text, indicatorbackground=C.field)
        s.map(w, background=[("active", C.bg)], indicatorbackground=[("selected", C.accent)])
    s.configure("TScrollbar", background=C.button, troughcolor=C.panel, bordercolor=C.bg, arrowcolor=C.text)
    s.configure("TPanedwindow", background=C.border)
    s.configure("Sash", sashthickness=6, gripcount=0, background=C.border)
    s.configure("TLabelframe", background=C.bg, bordercolor=C.border)
    s.configure("TLabelframe.Label", background=C.bg, foreground=C.text)
    s.configure("TSeparator", background=C.border)
    # clam draws check boxes, radio buttons and arrows at fixed pixel sizes:
    # scale them on high-resolution screens like the rest of the window.
    scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
    for w in ("TCheckbutton", "TRadiobutton"):
        s.configure(w, indicatorsize=int(round(11 * scale)), indicatormargin=int(round(3 * scale)))
    for w in ("TCombobox", "TSpinbox", "TScrollbar"):
        s.configure(w, arrowsize=int(round(14 * scale)))


def _apply_matplotlib():
    try:
        import matplotlib as mpl
    except ImportError:
        return
    rc = mpl.rcParams
    rc.update({
        "figure.facecolor": C.plot_bg, "figure.edgecolor": C.plot_bg, "savefig.facecolor": "white",
        "axes.facecolor": C.plot_bg, "axes.edgecolor": C.plot_text, "axes.labelcolor": C.plot_text,
        "axes.titlecolor": C.plot_text, "text.color": C.plot_text,
        "xtick.color": C.plot_text, "ytick.color": C.plot_text, "grid.color": C.plot_grid,
        "legend.facecolor": C.panel, "legend.edgecolor": C.border,
    })


def save_figure(fig, path, **kwargs):
    """Save a figure as PNG. Saved files are always light (black on white),
    for papers and slides: in the dark theme the plot is recoloured for the
    save and then put back."""
    if not C.dark:
        fig.savefig(path, **kwargs)
        return
    from matplotlib.colors import same_color
    undo = []

    def swap(obj, getter, setter, mapping):
        old = getattr(obj, getter)()
        for dark_colour, light_colour in mapping:
            try:
                if same_color(old, dark_colour):
                    getattr(obj, setter)(light_colour)
                    undo.append((getattr(obj, setter), old))
                    return
            except (ValueError, TypeError):
                return

    text = [(C.plot_text, "black"), (C.text, "black"), (C.faint, LIGHT["faint"])]
    face = [(C.plot_bg, "white"), (C.plot_unscanned, LIGHT["plot_unscanned"]), (C.panel, "white")]
    swap(fig.patch, "get_facecolor", "set_facecolor", face)
    for ax in fig.axes:
        swap(ax.patch, "get_facecolor", "set_facecolor", face)
        for sp in ax.spines.values():
            swap(sp, "get_edgecolor", "set_edgecolor", [(C.plot_text, "black")])
        items = [ax.title, ax.xaxis.label, ax.yaxis.label] + list(ax.texts)
        items += ax.get_xticklabels() + ax.get_yticklabels()
        legend = ax.get_legend()
        if legend is not None:
            items += legend.get_texts()
            swap(legend.get_frame(), "get_facecolor", "set_facecolor", face)
        for item in items:
            swap(item, "get_color", "set_color", text)
        for line in ax.lines:
            swap(line, "get_color", "set_color", [(C.plot_line, LIGHT["plot_line"])])
        for tick in ax.xaxis.get_major_ticks() + ax.yaxis.get_major_ticks():
            for tl in (tick.tick1line, tick.tick2line):
                swap(tl, "get_color", "set_color", [(C.plot_text, "black")])
    try:
        fig.savefig(path, **kwargs)
    finally:
        for setter, old in reversed(undo):
            setter(old)
