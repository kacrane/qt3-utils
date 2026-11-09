"""
Building blocks for the QT3 Microscope window.

These are plain Tkinter widgets with no hardware code. They serve both
setups; the setup profile decides which ones appear.
"""

import tkinter as tk
from tkinter import ttk

GREEN = "#2e9d3a"
ORANGE = "#e08a00"
RED = "#c62828"
GRAY = "#888888"
AMBER = "#f2c14e"
HEADER_BG = "#c9c9c9"


class StatusChip(tk.Frame):
    """One item in the status strip: a coloured dot, a label and a value."""

    def __init__(self, parent, label):
        super().__init__(parent, bd=1, relief="groove", padx=6, pady=2)
        self.dot = tk.Label(self, text="●", fg=GRAY)
        self.dot.pack(side="left")
        tk.Label(self, text=label).pack(side="left")
        self.value = tk.Label(self, text="—", font=("TkDefaultFont", 9, "bold"))
        self.value.pack(side="left", padx=(4, 0))

    def set(self, value, colour=GRAY):
        self.value.config(text=value)
        self.dot.config(fg=colour)


class CollapsibleSection(tk.Frame):
    """
    A drawer section: a header row that folds and unfolds the body.

    The header always shows a one-line summary, so a folded section still
    says what its instrument is doing.
    """

    def __init__(self, parent, title, start_open=False, on_toggle=None, on_popout=None):
        super().__init__(parent, bd=1, relief="solid")
        self._open = start_open
        self._on_toggle = on_toggle
        self.popped = False
        self.title_text = title

        self.header = tk.Frame(self, bg=HEADER_BG, cursor="hand2")
        self.header.pack(fill="x")
        self.arrow = tk.Label(self.header, bg=HEADER_BG, width=2)
        self.arrow.pack(side="left")
        self.title = tk.Label(self.header, text=title, bg=HEADER_BG,
                              font=("TkDefaultFont", 10, "bold"))
        self.title.pack(side="left", pady=4)
        # Pop-out button, rightmost in the header: moves the section to its own
        # window and back (step 8).
        self.pop_btn = None
        if on_popout is not None:
            self.pop_btn = tk.Button(self.header, text="↗", width=2, padx=0, pady=0, bd=1,
                                     relief="flat", bg=HEADER_BG, activebackground="#b5b5b5",
                                     cursor="hand2", command=on_popout)
            self.pop_btn.pack(side="right", padx=(0, 4))
        self.summary = tk.Label(self.header, text="—", bg=HEADER_BG)
        self.summary.pack(side="right", padx=6)
        # Room for a quick-action button (Laser ON/OFF etc.) added by a panel.
        self.quick = tk.Frame(self.header, bg=HEADER_BG)
        self.quick.pack(side="right")
        for w in (self.header, self.arrow, self.title, self.summary):
            w.bind("<Button-1>", lambda e: None if self.popped else self.toggle())

        self.body = ttk.Frame(self, padding=(8, 6))
        self._refresh()

    @property
    def is_open(self):
        return self._open

    def set_open(self, value):
        if self.popped:
            return
        if value != self._open:
            self._open = value
            self._refresh()
            if self._on_toggle:
                self._on_toggle()

    def toggle(self):
        self.set_open(not self._open)

    def set_popped(self, popped):
        """In its own window a section stays open and its header does not fold it."""
        self.popped = popped
        if popped:
            self._open = True
            self.arrow.config(text="")
            self.header.config(cursor="")
            if self.pop_btn is not None:
                self.pop_btn.config(text="↙ Back to drawer", width=0, padx=4)
            self.body.pack(fill="both", expand=True)
        else:
            self.header.config(cursor="hand2")
            if self.pop_btn is not None:
                self.pop_btn.config(text="↗", width=2, padx=0)
            self.body.pack_forget()
            self._refresh()

    def set_summary(self, text):
        self.summary.config(text=text)

    def _refresh(self):
        self.arrow.config(text="▼" if self._open else "▶")
        if self._open:
            self.body.pack(fill="x")
        else:
            self.body.pack_forget()


class ScrollableFrame(ttk.Frame):
    """A vertical scroll area; put children in .inner."""

    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda e: self._update())
        self.canvas.bind("<Configure>", self._on_canvas)
        # Scroll only while the pointer is over the drawer.
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)
        self._update()

    def _update(self):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _wheel(self, event):
        if self.inner.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")


class XYMicrometerBox(ttk.Frame):
    """
    Always-visible XY micrometer readout with a Lock button (TQLM).

    Step 1 is layout only: the readout shows a dash and nothing moves. The
    lock already greys out every move control so its look can be checked.
    """

    def __init__(self, parent, start_locked=True):
        super().__init__(parent, padding=(10, 4), relief="solid", borderwidth=1)
        self.locked = start_locked
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="XY micrometers", font=("TkDefaultFont", 10, "bold")).pack(side="left")
        self.lock_btn = tk.Button(top, width=10, command=self.toggle_lock,
                                  font=("TkDefaultFont", 9, "bold"))
        self.lock_btn.pack(side="right")

        grid = ttk.Frame(self)
        grid.pack(fill="x", pady=(4, 0))
        self.move_widgets = []
        for row, axis in enumerate(("X", "Y")):
            ttk.Label(grid, text=axis, width=2).grid(row=row, column=0, sticky="w")
            ttk.Label(grid, text="—", width=9, relief="sunken", anchor="e").grid(row=row, column=1, padx=2, pady=1)
            ttk.Label(grid, text="µm").grid(row=row, column=2)
            entry = ttk.Entry(grid, width=9)
            entry.grid(row=row, column=3, padx=4)
            btn = ttk.Button(grid, text="Set", width=5)
            btn.grid(row=row, column=4)
            self.move_widgets += [entry, btn]
        steps = ttk.Frame(self)
        steps.pack(fill="x", pady=(4, 0))
        ttk.Label(steps, text="Step (µm)").pack(side="left")
        step_entry = ttk.Entry(steps, width=6)
        step_entry.pack(side="left", padx=4)
        self.move_widgets.append(step_entry)
        for arrow in ("◀", "▲", "▼", "▶"):
            b = ttk.Button(steps, text=arrow, width=3)
            b.pack(side="left", padx=1)
            self.move_widgets.append(b)
        home = ttk.Button(steps, text="Home", width=6)
        home.pack(side="left", padx=(8, 0))
        self.move_widgets.append(home)
        self.note = ttk.Label(self, foreground="#555555")
        self.note.pack(anchor="w", pady=(4, 0))
        self._refresh()

    def toggle_lock(self):
        self.locked = not self.locked
        self._refresh()

    def _refresh(self):
        if self.locked:
            self.lock_btn.config(text="Locked", bg=AMBER, activebackground=AMBER)
            self.note.config(text="Locked: no moves from buttons, arrow keys or Home")
            state = "disabled"
        else:
            self.lock_btn.config(text="Unlocked", bg="#e3e3e3", activebackground="#e3e3e3")
            self.note.config(text="Unlocked: moves allowed")
            state = "normal"
        for w in self.move_widgets:
            w.configure(state=state)

    def summary_text(self):
        return "XY  —  " + ("Locked" if self.locked else "Unlocked")


class ToolTip:
    """Small yellow note that appears when the mouse rests on a widget."""

    def __init__(self, widget, text, delay_ms=500, wraplength=320):
        self.widget, self.text, self.delay, self.wrap = widget, text, delay_ms, wraplength
        self._job = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._job = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._job is not None:
            self.widget.after_cancel(self._job)
            self._job = None

    def _show(self):
        self._job = None
        if self._tip is not None:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, justify="left", background="#ffffe0", relief="solid",
                 borderwidth=1, wraplength=self.wrap, padx=6, pady=4).pack()

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None
