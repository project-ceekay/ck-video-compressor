#!/usr/bin/env python3
r"""CK Video Compressor - a GUI around FFmpeg.

* Batch queue (drag and drop videos or folders), per-video trim
* Four profiles: Balanced / Fastest / Smallest file / Super Compressed (< 20 MB)
* Size limits (Discord 10 MB, WhatsApp 16 MB, Email 25 MB or custom) using 2-pass encodes
* Live predicted output size and processing time per video
* Scaling (1080p ... 360p), audio bitrate, or strip audio; MP4 or MKV output
* Real progress from FFmpeg's -progress output; encoding runs in a worker thread

Super Compressed (< 20 MB): 2-pass libx264, video bitrate = (20 * 8192) / duration - 32 kbps,
mono 32 kbps audio, max 480p / 30 fps; short clips capped at 0.2 bits/pixel/frame; if the
result still lands over budget it is re-encoded slightly lower (up to 2 retries).

Requires:  pip install pillow      and FFmpeg + ffprobe (next to the program, in its bin
           folder, on PATH, or located with the "Locate FFmpeg" button)
"""
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageTk

APP_NAME = "CK Video Compressor"
VERSION = "1.0.0"
REPO = "project-ceekay/ck-video-compressor"
SETTINGS_NAME = "Video Compressor"  # %APPDATA%\CK Suite\<this>

# ======================================================================= CK UI kit
# The shared CK Suite look (native Windows ttk, polished). Each program carries its own
# copy so it compiles on its own.
ACCENT = "#0067c0"      # Windows 11 accent blue
ERROR = "#c42b1c"
SUCCESS = "#0f7b0f"
WARN = "#9d5d00"
MUTED = "#5f6368"
HEADER_BG = "#ffffff"
BORDER = "#d9d9d9"
HOVER = "#e5f1fb"
SCALE = 1.0  # screen scaling (1.0 at 100%); set by init_window()


def px(n):
    """n pixels at 100% scaling, converted to this screen's scaling."""
    return round(n * SCALE)


def app_dir():
    """Folder of the exe when compiled, otherwise of this script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def open_path(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
    except Exception:
        pass


def reveal(path):
    """Open the folder with the file selected."""
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            open_path(os.path.dirname(path))
    except Exception:
        pass


def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def icon_base(size):
    """(image, draw, unit) with the blue rounded square every CK icon starts from."""
    s = size / 64
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([4 * s, 4 * s, 60 * s, 60 * s], radius=12 * s, fill=ACCENT)
    return im, d, s


def save_icon(make_icon, path):
    make_icon(256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def init_window(root, title, size, minsize, make_icon):
    """DPI awareness must already be on. Sets scaling, size (capped to the screen), icon, style."""
    global SCALE
    SCALE = root.winfo_fpixels("1i") / 96
    root.title(title)
    h = min(px(size[1]), root.winfo_screenheight() - px(80))
    root.geometry(f"{px(size[0])}x{h}")
    root.minsize(px(minsize[0]), min(px(minsize[1]), h))
    root._icon = ImageTk.PhotoImage(make_icon(64))
    root.iconphoto(True, root._icon)
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    if sys.platform.startswith("win"):
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
            try:
                tkfont.nametofont(name).configure(family="Segoe UI", size=10)
            except tk.TclError:
                pass
    line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
    style.configure("Go.TButton", font=("Segoe UI Semibold", 10), padding=(px(20), px(6)))
    style.configure("Footer.TButton", padding=(px(14), px(6)))
    style.configure("Toolbutton", padding=(px(10), px(3)))
    style.configure("Section.TLabelframe.Label", font=("Segoe UI Semibold", 10), foreground=ACCENT)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Error.TLabel", foreground=ERROR)
    style.configure("Success.TLabel", foreground=SUCCESS)
    style.configure("Warn.TLabel", foreground=WARN)
    style.configure("Invalid.TEntry", foreground=ERROR)
    style.configure("Treeview", rowheight=line + px(10))
    style.configure("Treeview.Heading", font=("Segoe UI Semibold", 10))
    style.configure("TNotebook.Tab", padding=(px(10), px(4)))
    # the mouse wheel scrolls whichever ScrollFrame is under the pointer; number boxes and
    # drop-downs shouldn't change value just because the wheel passes over them
    root.bind_all("<MouseWheel>", lambda e: _on_wheel(root, e))
    for cls in ("TSpinbox", "TCombobox"):
        root.unbind_class(cls, "<MouseWheel>")


def enable_dpi_awareness():
    """Tell Windows we draw at real resolution, so text is sharp instead of stretched."""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def build_header(root, title, subtitle):
    header = tk.Frame(root, bg=HEADER_BG)
    header.pack(fill="x")
    tk.Label(header, text=title, bg=HEADER_BG, fg="#1a1a1a",
             font=("Segoe UI Semibold", 16)).pack(anchor="w", padx=px(20), pady=(px(14), 0))
    tk.Label(header, text=subtitle, bg=HEADER_BG, fg=MUTED,
             font=("Segoe UI", 10)).pack(anchor="w", padx=px(20), pady=(0, px(14)))
    ttk.Separator(root).pack(fill="x")


class Footer(ttk.Frame):
    """Bottom bar: status (+ progress while busy) on the left, buttons added on the right.
    Create it before the body so it always stays visible."""

    def __init__(self, root):
        super().__init__(root, padding=(px(20), px(10)))
        self.pack(side="bottom", fill="x")
        ttk.Separator(root).pack(side="bottom", fill="x")
        self.progress = ttk.Progressbar(self, length=px(160), maximum=100)
        self.status = ttk.Label(self, text="Ready", style="Muted.TLabel")
        self.status.pack(side="left")

    def button(self, text, command, primary=False, **kw):
        b = ttk.Button(self, text=text, command=command, style="Go.TButton" if primary else "Footer.TButton",
                       default="active" if primary else "normal", **kw)
        b.pack(side="right", padx=(px(8), 0))
        return b

    def set_status(self, text, kind="muted"):
        self.status.configure(text=text, style={"muted": "Muted.TLabel", "error": "Error.TLabel",
                                                "ok": "Success.TLabel", "warn": "Warn.TLabel"}[kind])

    def set_progress(self, value):
        """0..100 shows the bar at that point; None hides it."""
        if value is None:
            self.progress.pack_forget()
            return
        if not self.progress.winfo_ismapped():
            self.progress.pack(side="left", padx=(0, px(12)), before=self.status)
        self.progress.configure(value=value)


def section(parent, text, **pack):
    box = ttk.Labelframe(parent, text=text, style="Section.TLabelframe", padding=px(10))
    box.pack(**(pack or {"fill": "x", "pady": (0, px(10))}))
    return box


def hint(parent, text, wrap=380, grid=None, **pack):
    """A muted explanatory line. Packed by default; pass grid={...} for grid layouts."""
    lbl = ttk.Label(parent, text=text, style="Muted.TLabel", wraplength=px(wrap), justify="left")
    if grid is not None:
        lbl.grid(**grid)
    else:
        lbl.pack(anchor="w", **(pack or {"pady": (px(2), 0)}))
    return lbl


def segmented(parent, values, var, command=None):
    """A row of toggle buttons that work like radio buttons. values: list or (value, text) pairs."""
    row = ttk.Frame(parent)
    for v in values:
        value, text = v if isinstance(v, tuple) else (v, v)
        ttk.Radiobutton(row, text=text, value=value, variable=var, style="Toolbutton",
                        command=command).pack(side="left", padx=(0, px(2)))
    return row


def path_row(parent, row, label, var, browse, button="Browse…"):
    """Label / entry / button on one grid row of parent (column 1 stretches)."""
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=px(3))
    ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=px(8), pady=px(3))
    ttk.Button(parent, text=button, command=browse).grid(row=row, column=2, pady=px(3))
    parent.columnconfigure(1, weight=1)


class ScrollFrame(ttk.Frame):
    """A frame whose contents scroll vertically. Put widgets in .body."""

    def __init__(self, master, padding=0):
        super().__init__(master)
        bg = ttk.Style().lookup("TFrame", "background") or "#f0f0f0"
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, bg=bg, yscrollincrement=px(20))
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)

        def set_bar(lo, hi):  # the scrollbar only shows when the contents don't fit
            if float(lo) <= 0 and float(hi) >= 1:
                bar.pack_forget()
            elif not bar.winfo_ismapped():
                bar.pack(side="right", fill="y", before=self.canvas)
            bar.set(lo, hi)

        self.canvas.configure(yscrollcommand=set_bar)
        self.body = ttk.Frame(self.canvas, padding=padding)
        win = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(win, width=e.width))

    def scroll(self, delta):
        if self.canvas.yview() != (0.0, 1.0):
            steps = -delta / 120 * 3
            self.canvas.yview_scroll(int(steps) or (1 if steps > 0 else -1), "units")


def _on_wheel(root, e):
    w = root.winfo_containing(e.x_root, e.y_root)
    while w is not None:
        if isinstance(w, (tk.Listbox, tk.Text, ttk.Treeview)):
            return  # these scroll themselves
        if isinstance(w, ScrollFrame):
            w.scroll(e.delta)
            return
        w = w.master


class SliderRow(ttk.Frame):
    """Label, slider and a number box you can type into. Double-click the slider to reset.
    Works in whole numbers, or in steps of `step` (e.g. 0.1) when given."""

    def __init__(self, master, text, lo, hi, value, on_change, step=1, label_width=16):
        super().__init__(master)
        self.on_change = on_change
        self.lo, self.hi, self.step, self.default = lo, hi, step, value
        self.value = value
        self._setting = False
        ttk.Label(self, text=text, width=label_width).grid(row=0, column=0, sticky="w")
        self.scale = ttk.Scale(self, from_=lo, to=hi, command=self._moved)
        self.scale.grid(row=0, column=1, sticky="ew", padx=(px(4), px(8)))
        self.spin_var = tk.StringVar()
        spin = ttk.Spinbox(self, from_=lo, to=hi, increment=step, textvariable=self.spin_var, width=6,
                           command=self._typed)
        spin.grid(row=0, column=2)
        spin.bind("<Return>", self._typed)
        spin.bind("<FocusOut>", self._typed)
        self.scale.bind("<Double-Button-1>", lambda e: self.after_idle(self.reset))
        self.columnconfigure(1, weight=1)
        self.set(value)

    def _snap(self, v):
        v = round(round(float(v) / self.step) * self.step, 6)
        v = max(self.lo, min(self.hi, v))
        return int(v) if self.step >= 1 else v

    def _text(self, v):
        return str(v) if self.step >= 1 else f"{v:g}"

    def _moved(self, v):
        if self._setting:
            return
        n = self._snap(v)
        if abs(float(v) - n) > 1e-9:
            self._set_scale(n)
        if n != self.value:
            self.value = n
            self.spin_var.set(self._text(n))
            self.on_change()

    def _typed(self, *_):
        try:
            n = self._snap(self.spin_var.get())
        except ValueError:
            self.spin_var.set(self._text(self.value))
            return
        if n != self.value:
            self.set(n)
            self.on_change()
        else:
            self.spin_var.set(self._text(n))

    def _set_scale(self, v):
        self._setting = True
        try:
            self.scale.set(v)
        finally:
            self._setting = False

    def reset(self):
        if self.value != self.default:
            self.set(self.default)
            self.on_change()

    def get(self):
        return self.value

    def set(self, v):
        self.value = self._snap(v)
        self._set_scale(self.value)
        self.spin_var.set(self._text(self.value))

    def enable(self, on):
        for w in self.winfo_children():
            try:
                w.state(["!disabled"] if on else ["disabled"])
            except (AttributeError, tk.TclError):
                pass


class LogBox(ttk.Frame):
    """A read-only, timestamped activity log."""

    def __init__(self, master, height=6):
        super().__init__(master)
        self.text = tk.Text(self, height=height, wrap="word", state="disabled", font=("Consolas", 9),
                            relief="flat", borderwidth=0, bg="#fbfbfb", padx=px(6), pady=px(4),
                            highlightthickness=1, highlightbackground=BORDER, highlightcolor=BORDER)
        sb = ttk.Scrollbar(self, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.text.tag_configure("error", foreground=ERROR)
        self.text.tag_configure("ok", foreground=SUCCESS)
        self.text.tag_configure("warn", foreground=WARN)

    def log(self, msg, tag=None):
        self.text.configure(state="normal")
        self.text.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n", tag)
        self.text.see("end")
        self.text.configure(state="disabled")


def make_tree(parent, columns, height=6, selectmode="extended"):
    """columns: list of (id, heading, width, stretch). Returns a Treeview with a scrollbar."""
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height,
                        selectmode=selectmode)
    for cid, text, width, stretch in columns:
        tree.heading(cid, text=text, anchor="w")
        tree.column(cid, width=px(width), minwidth=px(40), stretch=stretch, anchor="w")
    sb = ttk.Scrollbar(frame, command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    tree.tag_configure("pending", foreground=MUTED)
    tree.tag_configure("working", foreground=ACCENT)
    tree.tag_configure("error", foreground=ERROR)
    tree.tag_configure("done", foreground=SUCCESS)
    frame.tree = tree
    return frame


def enable_file_drop(widget, on_drop):
    """Windows only: on_drop(paths) is called when files are dragged onto widget's window.
    Uses the classic WM_DROPFILES message directly, so no extra package is needed."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.CallWindowProcW.restype = LRESULT
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                           wintypes.WPARAM, wintypes.LPARAM]
        shell32.DragQueryFileW.restype = wintypes.UINT
        shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        shell32.DragFinish.argtypes = [ctypes.c_void_p]
        shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        WM_DROPFILES, GWLP_WNDPROC = 0x0233, -4
        hwnd = widget.winfo_id()
        drops = queue.Queue()

        def proc(h, msg, wparam, lparam):
            if msg == WM_DROPFILES:
                paths = []
                for i in range(shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)):
                    size = shell32.DragQueryFileW(wparam, i, None, 0) + 1
                    buf = ctypes.create_unicode_buffer(size)
                    shell32.DragQueryFileW(wparam, i, buf, size)
                    paths.append(buf.value)
                shell32.DragFinish(wparam)
                drops.put(paths)  # never call Tk from in here (it crashes Python)
                return 0
            return user32.CallWindowProcW(old_proc, h, msg, wparam, lparam)

        widget._drop_proc = WNDPROC(proc)  # must stay referenced while the window exists
        shell32.DragAcceptFiles(hwnd, True)
        old_proc = user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.cast(widget._drop_proc, ctypes.c_void_p))

        def check():
            while not drops.empty():
                on_drop(drops.get())
            widget.after(150, check)

        check()
    except Exception:
        pass  # drag and drop is a convenience; the Browse buttons still work


def expand_drop(paths, exts):
    """Dropped files and the matching files inside dropped folders, filtered by extension."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root_, _, files in os.walk(p):
                out += [os.path.join(root_, f) for f in sorted(files) if f.lower().endswith(exts)]
        elif p.lower().endswith(exts):
            out.append(p)
    return [os.path.normpath(p) for p in out]


NO_WINDOW = {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}


def find_tool(name):
    """ffmpeg/ffprobe/ffplay: next to the program (or its bin folder) first, then PATH."""
    exe = name + (".exe" if sys.platform.startswith("win") else "")
    for d in (app_dir(), os.path.join(app_dir(), "bin")):
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            return p
    return shutil.which(name)
# ---------------------------------------------------------------- settings & updates
# Each program defines APP_NAME, VERSION, REPO ("owner/name") and SETTINGS_NAME before this block.
import re as _re
import tempfile as _tempfile
import urllib.error as _urlerror
import urllib.request as _urlrequest
import webbrowser as _webbrowser


def data_dir():
    """Per-user settings folder: %APPDATA%\\CK Suite\\<SETTINGS_NAME>."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "CK Suite", SETTINGS_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def settings_file(old_paths=(), name="config.json"):
    """data_dir()/name; the first existing old location is copied over once."""
    path = os.path.join(data_dir(), name)
    if not os.path.exists(path):
        for old in old_paths:
            if old and os.path.isfile(old):
                try:
                    shutil.copy2(old, path)
                except OSError:
                    pass
                break
    return path


def version_tuple(text):
    return tuple(int(x) for x in _re.findall(r"\d+", text)[:3]) or (0,)


def latest_release():
    """{'version', 'notes', 'url', 'page'} of the newest GitHub release; {} if none yet."""
    req = _urlrequest.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                              headers={"User-Agent": "CK-Suite-Updater", "Accept": "application/vnd.github+json"})
    try:
        with _urlrequest.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except _urlerror.HTTPError as e:
        if e.code == 404:
            return {}
        raise
    asset = next((a for a in data.get("assets", []) if a["name"].lower().endswith("setup.exe")), None)
    return {"version": data.get("tag_name", "").lstrip("v"), "notes": data.get("body") or "",
            "url": asset["browser_download_url"] if asset else None, "page": data.get("html_url")}


class Updater:
    """About box + daily update check + one-click install, living in the footer `bar`.
    status(text, kind) / progress(0..100 or None) report to the window; quit() saves and closes it."""

    def __init__(self, root, bar, cfg, make_icon, status, progress, quit):
        self.root, self.cfg, self.make_icon = root, cfg, make_icon
        self.status, self.progress, self.quit = status, progress, quit
        self.auto = tk.BooleanVar(value=cfg.get("auto_update", True))
        self.release = None
        self.about_lbl = None
        ttk.Style(root).configure("Link.TLabel", foreground="#0067c0")
        about = ttk.Label(bar, text=f"About · v{VERSION}", style="Link.TLabel", cursor="hand2")
        about.pack(side="right", padx=(0, self._px(16)))
        about.bind("<Button-1>", lambda e: self.show_about())
        self.link = ttk.Label(bar, text="", style="Link.TLabel", cursor="hand2", font=("Segoe UI Semibold", 10))
        self.link.bind("<Button-1>", lambda e: self.offer())
        if self.auto.get() and time.time() - cfg.get("last_update_check", 0) > 20 * 3600:
            root.after(2500, lambda: self.check(quiet=True))

    def state(self):
        return {"auto_update": self.auto.get(), "last_update_check": self.cfg.get("last_update_check", 0)}

    def _px(self, n):
        return round(n * self.root.winfo_fpixels("1i") / 96)

    def _dialog(self, title):
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.title(title)
        win.transient(self.root)
        win.resizable(False, False)
        body = ttk.Frame(win, padding=self._px(18))
        body.pack(fill="both", expand=True)
        return win, body

    def _show(self, win):
        win.bind("<Escape>", lambda e: win.destroy())
        win.update_idletasks()
        win.geometry(f"+{self.root.winfo_rootx() + self._px(110)}+{self.root.winfo_rooty() + self._px(110)}")
        win.deiconify()
        win.grab_set()

    def show_about(self):
        win, body = self._dialog(f"About {APP_NAME}")
        P = self._px
        self._icon = ImageTk.PhotoImage(self.make_icon(P(56)))
        ttk.Label(body, image=self._icon).grid(row=0, column=0, rowspan=3, sticky="n", padx=(0, P(14)))
        ttk.Label(body, text=APP_NAME, font=("Segoe UI Semibold", 13)).grid(row=0, column=1, sticky="w")
        ttk.Label(body, text=f"Version {VERSION}  ·  CK Suite  ·  © CK Software  ·  MIT License",
                  foreground="#5f6368").grid(row=1, column=1, sticky="w")
        link = ttk.Label(body, text=f"github.com/{REPO}", style="Link.TLabel", cursor="hand2")
        link.grid(row=2, column=1, sticky="w", pady=(P(4), 0))
        link.bind("<Button-1>", lambda e: _webbrowser.open(f"https://github.com/{REPO}"))
        ttk.Separator(body).grid(row=3, column=0, columnspan=2, sticky="ew", pady=P(14))
        ttk.Checkbutton(body, text="Check for updates automatically (once a day)", variable=self.auto).grid(
            row=4, column=0, columnspan=2, sticky="w")
        self.about_lbl = ttk.Label(body, text="", foreground="#5f6368")
        self.about_lbl.grid(row=5, column=0, columnspan=2, sticky="w", pady=(P(8), 0))
        bar = ttk.Frame(body)
        bar.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(P(14), 0))
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")
        ttk.Button(bar, text="Check for Updates Now",
                   command=lambda: self.check(quiet=False)).pack(side="right", padx=(0, P(8)))
        self._show(win)

    def _about(self, text):
        if self.about_lbl is not None and self.about_lbl.winfo_exists():
            self.about_lbl.configure(text=text)

    def check(self, quiet):
        self._about("Checking…")
        result = {}

        def work():
            try:
                result["rel"] = latest_release()
            except Exception:
                result["rel"] = None

        t = threading.Thread(target=work, daemon=True)
        t.start()

        def done():
            if t.is_alive():
                self.root.after(200, done)
                return
            self.cfg["last_update_check"] = time.time()
            rel = result.get("rel")
            if rel is None:
                self._about("Couldn't reach GitHub. Check your internet connection.")
            elif not rel:
                self._about(f"No releases published yet. You have version {VERSION}.")
            elif version_tuple(rel["version"]) > version_tuple(VERSION):
                self.release = rel
                self.link.configure(text=f"⬆ Version {rel['version']} is available")
                self.link.pack(side="right", padx=(0, self._px(16)))
                self._about(f"Version {rel['version']} is available (you have {VERSION}).")
                if not quiet:
                    self.offer()
            else:
                self._about(f"You have the latest version ({VERSION}).")

        done()

    def offer(self):
        rel = self.release
        if not rel:
            return
        win, body = self._dialog("Update available")
        P = self._px
        ttk.Label(body, text=f"{APP_NAME} {rel['version']} is available", font=("Segoe UI Semibold", 12)).pack(anchor="w")
        ttk.Label(body, text=f"You have version {VERSION}. What's new:",
                  foreground="#5f6368").pack(anchor="w", pady=(P(2), P(8)))
        notes = tk.Text(body, height=10, width=64, wrap="word", font=("Segoe UI", 10), relief="flat",
                        highlightthickness=1, highlightbackground="#d9d9d9", padx=P(8), pady=P(6))
        notes.insert("1.0", rel["notes"].strip() or "(no release notes)")
        notes.configure(state="disabled")
        notes.pack(fill="both", expand=True)
        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(P(12), 0))
        if getattr(sys, "frozen", False) and rel["url"]:
            ttk.Button(bar, text="Update Now", default="active",
                       command=lambda: (win.destroy(), self.install(rel))).pack(side="right")
        else:  # running from source: send them to the release page
            ttk.Button(bar, text="Open Download Page", default="active",
                       command=lambda: (win.destroy(), _webbrowser.open(rel["page"]))).pack(side="right")
        ttk.Button(bar, text="Later", command=win.destroy).pack(side="right", padx=(0, P(8)))
        self._show(win)

    def install(self, rel):
        """Download the installer, run it quietly and close; the installer reopens the program."""
        if getattr(self.root, "busy", False):
            messagebox.showinfo(APP_NAME, "Wait for the current job to finish, then update.")
            return
        self.status(f"Downloading version {rel['version']}…", "muted")
        self.progress(0)
        dest = os.path.join(_tempfile.gettempdir(), "CK Suite updates", os.path.basename(rel["url"]))
        st = {"frac": 0.0}

        def work():
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                req = _urlrequest.Request(rel["url"], headers={"User-Agent": "CK-Suite-Updater"})
                with _urlrequest.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
                    total, got = int(r.headers.get("Content-Length") or 0), 0
                    while chunk := r.read(256 * 1024):
                        f.write(chunk)
                        got += len(chunk)
                        st["frac"] = got / total if total else 0
                st["ok"] = True
            except Exception as e:
                st["err"] = e

        t = threading.Thread(target=work, daemon=True)
        t.start()

        def watch():
            self.progress(st["frac"] * 100)
            if t.is_alive():
                self.root.after(150, watch)
                return
            self.progress(None)
            if not st.get("ok"):
                self.status(f"Download failed: {st.get('err')}", "error")
                return
            try:
                subprocess.Popen([dest, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"])
            except OSError as e:
                self.status(f"Couldn't start the installer: {e}", "error")
                return
            self.quit()

        watch()
# ===================================================================== end CK UI kit

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
NULL_DEV = "NUL" if IS_WIN else "/dev/null"
CONFIG_FILE = Path(settings_file([str(Path.home() / ".video_compressor.json")]))
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}

# --- "Super Compressed" profile settings ------------------------------------
SUPER_TARGET_MB = 20        # size budget; 1 MB = 8192 kilobits, matching the formula
SUPER_AUDIO_KBPS = 32       # forced mono audio bitrate
SUPER_MAX_HEIGHT = 480      # resolution cap
SUPER_MAX_FPS = 30          # frame-rate cap
SUPER_MIN_VIDEO_KBPS = 20   # below this the picture is unwatchable -> refuse and explain
SUPER_MAX_BPP = 0.20        # never spend more than this many bits/pixel/frame


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def fmt_time(sec):
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def fmt_eta(sec):
    """Friendly rounded duration for estimates: '40 sec', '12 min', '1 h 05 min'."""
    sec = max(1, int(round(sec)))
    if sec < 60:
        return f"{sec} sec"
    m = int(round(sec / 60))
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def save_config(cfg):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg))
    except Exception:
        pass


def run_capture(cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, **NO_WINDOW)


# --------------------------------------------------------------------------- #
# FFmpeg discovery
# --------------------------------------------------------------------------- #
def _candidate_dirs():
    here = Path(app_dir())  # the exe folder once compiled
    dirs = [here, here / "bin"]
    if IS_WIN:
        local = os.environ.get("LOCALAPPDATA", "")
        dirs += [Path(r"C:\ffmpeg\bin"), Path(r"C:\Program Files\ffmpeg\bin"),
                 Path(r"C:\ProgramData\chocolatey\bin"), Path.home() / "scoop/shims",
                 Path(local) / "Microsoft/WinGet/Links"]
    elif IS_MAC:
        dirs += [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/opt/local/bin")]
    else:
        dirs += [Path("/usr/bin"), Path("/usr/local/bin"), Path("/snap/bin"),
                 Path.home() / ".local/bin"]
    return dirs


def find_tool(name, saved=None):
    exe = name + (".exe" if IS_WIN else "")
    if saved and Path(saved).is_file():
        return str(saved)
    found = shutil.which(name)
    if found:
        return found
    for d in _candidate_dirs():
        p = d / exe
        if p.is_file():
            return str(p)
    return None


def sibling_tool(ffmpeg_path, name):
    """ffprobe usually lives next to ffmpeg."""
    exe = name + (".exe" if IS_WIN else "")
    p = Path(ffmpeg_path).with_name(exe)
    return str(p) if p.is_file() else shutil.which(name)


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #
def probe_video(ffmpeg, ffprobe, path):
    """Return dict(width,height,duration,size,bitrate,vcodec,fps,has_audio)."""
    info = {"size": os.path.getsize(path), "has_audio": True}
    if ffprobe:
        r = run_capture([ffprobe, "-v", "error", "-print_format", "json",
                         "-show_format", "-show_streams", path])
        data = json.loads(r.stdout or "{}")
        fmt = data.get("format", {})
        if fmt.get("duration"):
            info["duration"] = float(fmt["duration"])
        if fmt.get("bit_rate"):
            info["bitrate"] = int(fmt["bit_rate"])
        streams = data.get("streams", [])
        info["has_audio"] = any(s.get("codec_type") == "audio" for s in streams)
        for s in streams:
            if s.get("codec_type") == "video":
                info["width"], info["height"] = s.get("width"), s.get("height")
                info["vcodec"] = s.get("codec_name")
                try:
                    n, d = s.get("r_frame_rate", "0/1").split("/")
                    info["fps"] = float(n) / float(d) if float(d) else None
                except Exception:
                    pass
                break
    else:  # fallback: parse `ffmpeg -i`
        r = run_capture([ffmpeg, "-hide_banner", "-i", path])
        txt = r.stderr
        m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", txt)
        if m:
            info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
        m = re.search(r"Video:\s*(\w+).*?,\s*(\d{2,5})x(\d{2,5})", txt)
        if m:
            info["vcodec"], info["width"], info["height"] = m[1], int(m[2]), int(m[3])
        m = re.search(r"([\d.]+)\s*fps", txt)
        if m:
            info["fps"] = float(m[1])
        info["has_audio"] = "Audio:" in txt
    if "bitrate" not in info and info.get("duration"):
        info["bitrate"] = int(info["size"] * 8 / info["duration"])
    return info


_encoder_cache = {}


def encoder_available(ffmpeg, name):
    """True if the encoder is compiled in AND actually works on this machine."""
    if name in _encoder_cache:
        return _encoder_cache[name]
    ok = False
    try:
        r = run_capture([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                         "-i", "color=c=black:s=256x256:d=0.2", "-c:v", name,
                         "-f", "null", NULL_DEV], timeout=20)
        ok = r.returncode == 0
    except Exception:
        pass
    _encoder_cache[name] = ok
    return ok


def _ok(name):
    """Cached result only (no subprocess) - used by the UI predictions."""
    return _encoder_cache.get(name, False)


def hw_candidates():
    """Hardware H.264/HEVC encoders to try for the 'Fastest' profile, best first."""
    if IS_MAC:
        return [("hevc_videotoolbox", ["-q:v", "55", "-tag:v", "hvc1"]),
                ("h264_videotoolbox", ["-q:v", "55"])]
    return [("h264_nvenc", ["-preset", "p1", "-rc", "vbr", "-cq", "28", "-b:v", "0"]),
            ("h264_qsv", ["-preset", "veryfast", "-global_quality", "28"]),
            ("h264_amf", ["-quality", "speed", "-rc", "cqp", "-qp_i", "28", "-qp_p", "28"])]


# --------------------------------------------------------------------------- #
# Super Compressed helpers
# --------------------------------------------------------------------------- #
def out_geometry(info, height_cap=0, fps_cap=0):
    """(width, height, fps) of the output after the scaling / fps caps. Falls back to 1080p30."""
    w, h = info.get("width") or 1920, info.get("height") or 1080
    fps = info.get("fps") or 30.0
    if height_cap and h > height_cap:
        w, h = int(round(w * height_cap / h)), height_cap
    if fps_cap:
        fps = min(fps, fps_cap)
    return w, h, fps


def super_height_cap(user_height):
    """Super mode never exceeds 480p; the Resolution dropdown can only go lower."""
    return min(user_height, SUPER_MAX_HEIGHT) if user_height else SUPER_MAX_HEIGHT


def super_raw_kbps(duration):
    """The requested formula:  (20 * 8192) / duration_seconds - 32   (video kbps)."""
    return (SUPER_TARGET_MB * 8192) / duration - SUPER_AUDIO_KBPS


def super_video_kbps(info, user_height=0):
    """Return (video_kbps, capped). The formula, limited to SUPER_MAX_BPP so short clips
    don't get inflated toward 20 MB."""
    raw = super_raw_kbps(info["duration"])
    w, h, fps = out_geometry(info, super_height_cap(user_height), SUPER_MAX_FPS)
    ceiling = SUPER_MAX_BPP * w * h * fps / 1000
    return (ceiling, True) if raw > ceiling else (raw, False)


# --------------------------------------------------------------------------- #
# Encode planning  (turns UI choices into one or two FFmpeg commands)
# --------------------------------------------------------------------------- #
def plan_encode(ffmpeg, cfg, info, inp, out, workdir):
    profile = cfg["profile"]  # "fast" | "best" | "balanced" | "super"
    dur = info.get("duration")
    ext = Path(out).suffix.lower()
    notes = []

    # ---- audio -----------------------------------------------------------
    if cfg["audio"] == "none" or not info.get("has_audio", True):
        audio_args, a_kbps = ["-an"], 0
    elif profile == "super":
        # Forced mono @ 32 kbps. Opus inside MKV/WebM when available, AAC everywhere else.
        acodec = "libopus" if ext in (".mkv", ".webm") and encoder_available(ffmpeg, "libopus") else "aac"
        audio_args = ["-c:a", acodec, "-b:a", f"{SUPER_AUDIO_KBPS}k", "-ac", "1"]
        a_kbps = SUPER_AUDIO_KBPS
    else:
        default_audio = {"fast": "96k", "best": "128k", "balanced": "128k"}[profile]
        br = default_audio if cfg["audio"] == "default" else cfg["audio"]
        audio_args, a_kbps = ["-c:a", "aac", "-b:a", br], int(br.rstrip("k"))

    # ---- video filter (always forces even dimensions; only ever downscales) -
    height = super_height_cap(cfg["height"]) if profile == "super" else cfg["height"]
    if height:
        vf = f"scale=-2:min(ih\\,{height})"
    else:
        vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    if profile == "super" and (not info.get("fps") or info["fps"] > SUPER_MAX_FPS + 0.5):
        vf += f",fps={SUPER_MAX_FPS}"   # cap at 30 fps (slower sources are left alone)

    pix = ["-pix_fmt", "yuv420p"]
    two_pass, x265_pass = False, False

    # ---- video codec -------------------------------------------------------
    if profile == "super":
        if not dur:
            raise ValueError("Couldn't read the video duration, so the Super Compressed "
                             "bitrate can't be calculated.")
        raw = super_raw_kbps(dur)
        if raw < SUPER_MIN_VIDEO_KBPS:
            max_dur = SUPER_TARGET_MB * 8192 / (SUPER_AUDIO_KBPS + SUPER_MIN_VIDEO_KBPS)
            raise ValueError(
                f"This video is {fmt_time(dur)} long. The {SUPER_TARGET_MB} MB budget leaves only "
                f"{raw:.0f} kbps for video, which would be unwatchable (the limit is about "
                f"{fmt_time(max_dur)}). Trim the video first, or use a larger size.")
        v_f, capped = super_video_kbps(info, cfg["height"])
        if cfg.get("super_kbps"):                 # set by the automatic retry
            v_f = min(v_f, cfg["super_kbps"])
        v_kbps = int(v_f)
        two_pass = True
        vcodec = ["-c:v", "libx264", "-preset", "slow", "-b:v", f"{v_kbps}k"]
        notes.append(f"2-pass x264, {v_kbps} kbps video, <={SUPER_MAX_HEIGHT}p, "
                     f"<={SUPER_MAX_FPS} fps, mono {SUPER_AUDIO_KBPS}k audio")
        if capped:
            notes.append("short video: bitrate capped to avoid wasted space")
    elif cfg["target_mb"]:
        # Target-size mode: software encoder + 2-pass (hardware encoders can't 2-pass)
        if not dur:
            raise ValueError("Couldn't read the video duration, so a target size can't be calculated.")
        total_kbps = cfg["target_mb"] * 8000 * 0.97 / dur  # 3% container-overhead margin
        v_kbps = int(total_kbps - a_kbps)
        if v_kbps < 40:
            raise ValueError(
                f"{cfg['target_mb']} MB is too small for a {fmt_time(dur)} video "
                f"(only {v_kbps} kbps left for video). Raise the target, remove audio, or lower the resolution.")
        two_pass = True
        if profile == "best" and encoder_available(ffmpeg, "libx265"):
            vcodec = ["-c:v", "libx265", "-preset", "slow", "-tag:v", "hvc1"]
            x265_pass = True
        elif profile == "fast":
            vcodec = ["-c:v", "libx264", "-preset", "superfast"]
        elif profile == "best":
            vcodec = ["-c:v", "libx264", "-preset", "slow"]
        else:
            vcodec = ["-c:v", "libx264", "-preset", "medium"]
        vcodec += ["-b:v", f"{v_kbps}k"]
        notes.append(f"2-pass, ~{v_kbps} kbps video")
        if profile == "best" and not x265_pass:
            notes.append("libx265 unavailable, used x264")
    elif profile == "fast":
        vcodec = None
        for name, args in hw_candidates():
            if encoder_available(ffmpeg, name):
                vcodec = ["-c:v", name] + args
                notes.append(f"hardware encoder: {name}")
                break
        if vcodec is None:
            vcodec = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28"]
            notes.append("no hardware encoder found, using x264 ultrafast")
    elif profile == "best":
        if encoder_available(ffmpeg, "libsvtav1"):
            vcodec = ["-c:v", "libsvtav1", "-preset", "4", "-crf", "32"]
            notes.append("SVT-AV1 preset 4, CRF 32")
        elif encoder_available(ffmpeg, "libx265"):
            vcodec = ["-c:v", "libx265", "-preset", "slow", "-crf", "28", "-tag:v", "hvc1"]
            notes.append("x265 slow, CRF 28 (SVT-AV1 unavailable)")
        else:
            vcodec = ["-c:v", "libx264", "-preset", "slow", "-crf", "20"]
            notes.append("x264 slow, CRF 20 (no AV1/HEVC encoder in this FFmpeg build)")
    else:  # balanced
        vcodec = ["-c:v", "libx264", "-preset", "medium", "-crf", "23"]
        notes.append("x264 medium, CRF 23")

    container = ["-movflags", "+faststart"] if ext in (".mp4", ".mov", ".m4v") else []

    trim = cfg.get("trim")  # (start seconds, length seconds or None)
    seek = ["-ss", f"{trim[0]:.3f}"] if trim and trim[0] else []
    length = ["-t", f"{trim[1]:.3f}"] if trim and trim[1] else []
    base = [ffmpeg, "-hide_banner", "-y", "-progress", "pipe:1", "-nostats", *seek, "-i", inp, *length,
            "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-vf", vf]

    if not two_pass:
        return [base + vcodec + pix + audio_args + container + [out]], notes

    if x265_pass:
        p1 = ["-x265-params", "pass=1:stats=x265_2pass.log:log-level=error"]
        p2 = ["-x265-params", "pass=2:stats=x265_2pass.log:log-level=error"]
    else:
        p1 = ["-pass", "1", "-passlogfile", "ffpass"]
        p2 = ["-pass", "2", "-passlogfile", "ffpass"]
    pass1 = base + vcodec + pix + p1 + ["-an", "-f", "null", NULL_DEV]
    pass2 = base + vcodec + pix + p2 + audio_args + container + [out]
    return [pass1, pass2], notes


# --------------------------------------------------------------------------- #
# Size / time predictions  (rough estimates, shown in the UI)
# --------------------------------------------------------------------------- #
# Bits per pixel per frame each profile typically needs at its quality setting.
BPP = {"fast": 0.055, "best": 0.032, "balanced": 0.070}

# Encode speed as a multiple of real time, for 1080p30 video on a mid-range
# 8-core PC / mainstream GPU. Scaled by resolution and frame rate below.
SPEED_X = {
    "hw": 12.0,             # NVENC / QSV / AMF / VideoToolbox
    "x264_ultrafast": 8.0,
    "x264_superfast": 6.0,
    "x264_medium": 2.5,
    "x264_slow": 1.4,
    "x265_slow": 0.3,
    "svtav1_p4": 0.5,
}
DECODE_X = 12.0             # software decode speed (x real time at 1080p30)
PX_1080 = 1920 * 1080


def _audio_kbps(cfg, info):
    if cfg["audio"] == "none" or not info.get("has_audio", True):
        return 0
    if cfg["profile"] == "super":
        return SUPER_AUDIO_KBPS
    if cfg["audio"] == "default":
        return {"fast": 96, "best": 128, "balanced": 128}[cfg["profile"]]
    return int(cfg["audio"].rstrip("k"))


def _encoder_speed(cfg):
    """Real-time multiplier of the encoder this profile would end up using."""
    profile = cfg["profile"]
    if profile == "super":
        return SPEED_X["x264_slow"]
    if cfg["target_mb"]:                       # 2-pass => software encoders only
        if profile == "fast":
            return SPEED_X["x264_superfast"]
        if profile == "best":
            return SPEED_X["x265_slow"] if _ok("libx265") else SPEED_X["x264_slow"]
        return SPEED_X["x264_medium"]
    if profile == "fast":
        hw = any(_ok(n) for n, _ in hw_candidates())
        return SPEED_X["hw"] if hw else SPEED_X["x264_ultrafast"]
    if profile == "best":
        if _ok("libsvtav1"):
            return SPEED_X["svtav1_p4"]
        if _ok("libx265"):
            return SPEED_X["x265_slow"]
        return SPEED_X["x264_slow"]
    return SPEED_X["x264_medium"]


def predict_output(info, cfg):
    """Return (size_text, time_text) for the current settings."""
    dur = info.get("duration")
    if not dur:
        return "unknown (couldn't read the duration)", "unknown"

    profile, target = cfg["profile"], cfg["target_mb"]
    two_pass = profile == "super" or bool(target)
    a_kbps = _audio_kbps(cfg, info)

    height = super_height_cap(cfg["height"]) if profile == "super" else cfg["height"]
    w, h, fps = out_geometry(info, height, SUPER_MAX_FPS if profile == "super" else 0)

    # ---------------- predicted size ----------------
    v_kbps = floor = None
    if profile == "super":
        v_kbps, floor = super_raw_kbps(dur), SUPER_MIN_VIDEO_KBPS
        kbps, capped = super_video_kbps(info, cfg["height"])
        if capped:
            est = dur * (kbps + a_kbps) * 1000 / 8
            size_txt = (f"up to {SUPER_TARGET_MB} MB (Target) - likely ~{human_size(est)} "
                        f"(short video, bitrate capped)")
        else:
            size_txt = f"~{SUPER_TARGET_MB} MB (Target)"
    elif target:
        v_kbps, floor = target * 8000 * 0.97 / dur - a_kbps, 40
        size_txt = f"~{target:g} MB (Target)"
    else:
        est = BPP[profile] * w * h * fps / 1000              # kbps the encoder "wants"
        if info.get("bitrate"):                               # re-encoding rarely needs more
            src_video = max(info["bitrate"] / 1000 - (128 if info.get("has_audio") else 0), 50)
            est = min(est, src_video * 0.9)
        total = dur * (est + a_kbps) * 1000 / 8
        size_txt = (f"~{human_size(total)}  (likely {human_size(total * 0.7)} - "
                    f"{human_size(total * 1.3)})")
        if info.get("size"):
            pct = (1 - total / info["size"]) * 100
            size_txt += f", about {abs(pct):.0f}% {'smaller' if pct >= 0 else 'larger'}"
    if floor is not None and v_kbps < floor:
        size_txt += "   [!] too long for this size - quality would be unusable"

    # ---------------- estimated time ----------------
    src_w, src_h = info.get("width") or 1920, info.get("height") or 1080
    src_fps = info.get("fps") or 30.0
    scale = max(0.3, w * h / PX_1080) * (fps / 30.0)
    encode = dur / _encoder_speed(cfg) * scale
    decode = dur / DECODE_X * (src_w * src_h / PX_1080) * (src_fps / 30.0)
    # 2-pass: the first pass is a bit cheaper than the second; decoding happens twice.
    total_t = encode * (1.8 if two_pass else 1.0) + decode * (2 if two_pass else 1)
    return size_txt, f"~{fmt_eta(total_t)}"


# --------------------------------------------------------------------------- #
# The GUI
# --------------------------------------------------------------------------- #
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".ts", ".mts", ".3gp")
LIMITS = [("none", "No limit"), ("10", "Discord 10 MB"), ("16", "WhatsApp 16 MB"), ("25", "Email 25 MB"),
          ("custom", "Custom")]


def make_icon(size=64):
    """A film frame with arrows squeezing inward."""
    im, d, s = icon_base(size)
    d.rounded_rectangle([18 * s, 18 * s, 46 * s, 46 * s], radius=4 * s, fill="white")
    d.polygon([(28 * s, 26 * s), (28 * s, 38 * s), (38 * s, 32 * s)], fill=ACCENT)
    for x0, x1 in ((8, 15), (56, 49)):
        d.polygon([(x0 * s, 26 * s), (x1 * s, 32 * s), (x0 * s, 38 * s)], fill="white")
    return im


def parse_time(text):
    """'90', '1:30' or '0:01:30.5' -> seconds; '' -> None. Raises ValueError on junk."""
    text = text.strip()
    if not text:
        return None
    parts = [float(p) for p in text.split(":")]
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return secs


class Video:
    def __init__(self, path):
        self.path = path
        self.info = None        # probe result
        self.error = None
        self.start = None       # trim, seconds (None = from the beginning)
        self.end = None         # trim, seconds (None = to the end)
        self.status = ""
        self.output = None

    def trimmed_info(self):
        """The probe info as if the video were already trimmed (for predictions and bitrates)."""
        if not self.info or not self.info.get("duration"):
            return self.info
        dur = self.info["duration"]
        s, e = self.start or 0.0, min(self.end or dur, dur)
        length = max(0.1, e - s)
        return dict(self.info, duration=length, size=self.info["size"] * length / dur)

    def trim(self):
        if self.start is None and self.end is None:
            return None
        s = self.start or 0.0
        return (s, (self.end - s) if self.end else None)


class App(tk.Tk):
    PROFILES = {
        "balanced": ("Balanced", "x264 medium, CRF 23. Great size/speed trade-off, plays everywhere."),
        "fast": ("Fastest / lowest power", "Hardware encoder if available, otherwise x264 ultrafast. 96k audio."),
        "best": ("Smallest file (slowest)", "SVT-AV1 (or x265) at a slow preset. Best size, long encode."),
        "super": ("Super Compressed (under 20 MB)", "2-pass x264 sized to fit 20 MB: max 480p / 30 fps, mono 32k audio."),
    }
    HEIGHTS = {"Original": 0, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
    AUDIO = {"Profile default": "default", "64 kbps": "64k", "96 kbps": "96k",
             "128 kbps": "128k", "192 kbps": "192k", "Remove audio": "none"}

    def __init__(self):
        enable_dpi_awareness()
        super().__init__()
        init_window(self, "CK Video Compressor", (1260, 840), (1020, 660), make_icon)
        self.cfg_store = load_config()
        self.ffmpeg = find_tool("ffmpeg", self.cfg_store.get("ffmpeg"))
        self.ffprobe = sibling_tool(self.ffmpeg, "ffprobe") if self.ffmpeg else None
        c = self.cfg_store
        self.profile_var = tk.StringVar(value=c.get("profile", "balanced"))
        self.limit_var = tk.StringVar(value=c.get("limit", "none"))
        self.custom_mb = tk.StringVar(value=c.get("custom_mb", "50"))
        self.res_var = tk.StringVar(value=c.get("res", "Original"))
        self.aud_var = tk.StringVar(value=c.get("audio", "Profile default"))
        self.out_dir = tk.StringVar(value=c.get("out_dir", ""))
        self.suffix = tk.StringVar(value=c.get("suffix", "_compressed"))
        self.container = tk.StringVar(value=c.get("container", ".mp4"))
        self.videos = {}         # tree iid -> Video
        self.q = queue.Queue()
        self.proc = None
        self.cancel_evt = threading.Event()
        self.busy = False
        self.last_output = None
        self._workdir = None

        self.build_ui()
        enable_file_drop(self, self.add_paths)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.poll)
        self.after(300, self.ensure_ffmpeg)
        self.warm_encoders()

    # ---- layout ------------------------------------------------------------------
    def build_ui(self):
        build_header(self, "Video Compressor",
                     "Shrink videos with FFmpeg: pick a profile or a size limit, see the predicted size, "
                     "and compress one or many.")
        self.footer = Footer(self)
        self.start_btn = self.footer.button("Compress All", self.start, primary=True)
        self.footer.button("Show in Folder", self.reveal_last)
        self.updater = Updater(self, self.footer, self.cfg_store, make_icon, self.footer.set_status,
                               self.footer.set_progress, self.on_close)
        self.cancel_btn = ttk.Button(self.footer, text="Cancel", style="Footer.TButton", command=self.do_cancel)

        body = ttk.Frame(self, padding=(px(16), px(12), px(16), px(12)))
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, width=px(450))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        sf = ScrollFrame(left, padding=(0, 0, px(6), 0))
        sf.pack(fill="both", expand=True)
        panel = sf.body

        s = section(panel, "Compression profile")
        for key, (title, desc) in self.PROFILES.items():
            ttk.Radiobutton(s, text=title, value=key, variable=self.profile_var).pack(anchor="w", pady=(px(4), 0))
            hint(s, desc, wrap=360, padx=(px(24), 0))

        s = section(panel, "Size limit")
        self.limit_row = ttk.Frame(s)
        self.limit_row.pack(anchor="w")
        for i, (value, text) in enumerate(LIMITS):
            ttk.Radiobutton(self.limit_row, text=text, value=value, variable=self.limit_var,
                            style="Toolbutton").grid(row=i // 3, column=i % 3, sticky="w", padx=(0, px(2)), pady=px(1))
        row = ttk.Frame(s)
        row.pack(anchor="w", pady=(px(6), 0))
        ttk.Label(row, text="Custom limit").pack(side="left")
        self.custom_entry = ttk.Entry(row, textvariable=self.custom_mb, width=7)
        self.custom_entry.pack(side="left", padx=px(6))
        ttk.Label(row, text="MB").pack(side="left")
        self.limit_hint = hint(s, "", wrap=360, pady=(px(6), 0))

        s = section(panel, "Picture & sound")
        s.columnconfigure(1, weight=1)
        ttk.Label(s, text="Resolution").grid(row=0, column=0, sticky="w")
        ttk.Combobox(s, textvariable=self.res_var, values=list(self.HEIGHTS), state="readonly", width=16).grid(
            row=0, column=1, sticky="w", padx=px(8), pady=px(2))
        ttk.Label(s, text="Audio").grid(row=1, column=0, sticky="w")
        ttk.Combobox(s, textvariable=self.aud_var, values=list(self.AUDIO), state="readonly", width=16).grid(
            row=1, column=1, sticky="w", padx=px(8), pady=px(2))
        hint(s, "Resolution only ever scales down. Super Compressed stays at 480p or lower.", wrap=360,
             grid=dict(row=2, column=0, columnspan=2, sticky="w", pady=(px(4), 0)))

        s = section(panel, "Output")
        path_row(s, 0, "Save in", self.out_dir, self.browse_out)
        ttk.Label(s, text="Name ends in").grid(row=1, column=0, sticky="w", pady=(px(4), 0))
        ttk.Entry(s, textvariable=self.suffix, width=16).grid(row=1, column=1, sticky="w", padx=px(8), pady=(px(4), 0))
        ttk.Label(s, text="Container").grid(row=2, column=0, sticky="w", pady=(px(6), 0))
        segmented(s, [(".mp4", "MP4"), (".mkv", "MKV")], self.container).grid(
            row=2, column=1, columnspan=2, sticky="w", padx=px(8), pady=(px(6), 0))
        hint(s, "Leave \"Save in\" empty to save next to each video.", wrap=360,
             grid=dict(row=3, column=0, columnspan=3, sticky="w", pady=(px(6), 0)))

        s = section(panel, "FFmpeg")
        self.ff_lbl = ttk.Label(s, text="", style="Muted.TLabel", wraplength=px(360))
        self.ff_lbl.pack(anchor="w")
        ttk.Button(s, text="Locate FFmpeg…", command=lambda: self.ensure_ffmpeg(force=True)).pack(anchor="w", pady=(px(6), 0))

        # right side
        self.tabs = ttk.Notebook(body)
        self.tabs.pack(side="left", fill="both", expand=True, padx=(px(14), 0))
        vt = ttk.Frame(self.tabs, padding=px(10))
        self.tabs.add(vt, text="  Videos  ")
        bar = ttk.Frame(vt)
        bar.pack(fill="x", pady=(0, px(8)))
        ttk.Button(bar, text="Add Videos…", command=self.browse_videos).pack(side="left")
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="right")
        ttk.Button(bar, text="Remove", command=self.remove).pack(side="right", padx=(0, px(6)))
        tf = make_tree(vt, [("file", "Video", 260, True), ("length", "Length", 80, False), ("size", "Size", 90, False),
                            ("pred", "Predicted", 120, False), ("status", "Status", 170, True)], height=7,
                       selectmode="browse")
        tf.pack(fill="both", expand=True)
        self.tree = tf.tree
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_selected())
        self.tree.bind("<Delete>", lambda e: self.remove())
        self.tree.bind("<Double-1>", self.open_row)

        sel = section(vt, "Selected video", fill="x", pady=(px(10), 0))
        self.sel_name = ttk.Label(sel, text="Drop videos anywhere on this window, or use Add Videos…",
                                  font=("Segoe UI Semibold", 10))
        self.sel_name.pack(anchor="w")
        self.sel_info = ttk.Label(sel, text="", style="Muted.TLabel", justify="left")
        self.sel_info.pack(anchor="w", pady=(px(2), px(8)))
        trow = ttk.Frame(sel)
        trow.pack(fill="x")
        ttk.Label(trow, text="Trim: start").pack(side="left")
        self.start_var, self.end_var = tk.StringVar(), tk.StringVar()
        for var, label in ((self.start_var, None), (self.end_var, "end")):
            if label:
                ttk.Label(trow, text=label).pack(side="left", padx=(px(10), 0))
            e = ttk.Entry(trow, textvariable=var, width=10)
            e.pack(side="left", padx=px(6))
            e.bind("<Return>", lambda ev: self.apply_trim())
            e.bind("<FocusOut>", lambda ev: self.apply_trim())
        ttk.Button(trow, text="No Trim", command=self.clear_trim).pack(side="left", padx=(px(10), 0))
        hint(sel, "Times as seconds or m:ss (e.g. 1:30). Leave empty to keep the start / end.", wrap=700, pady=(px(4), px(8)))
        grid = ttk.Frame(sel)
        grid.pack(fill="x")
        ttk.Label(grid, text="Predicted size").grid(row=0, column=0, sticky="w")
        self.pred_size = ttk.Label(grid, text="—", wraplength=px(560), justify="left")
        self.pred_size.grid(row=0, column=1, sticky="w", padx=px(12))
        ttk.Label(grid, text="Estimated time").grid(row=1, column=0, sticky="w", pady=(px(2), 0))
        self.pred_time = ttk.Label(grid, text="—")
        self.pred_time.grid(row=1, column=1, sticky="w", padx=px(12), pady=(px(2), 0))
        hint(sel, "Rough estimates: real results vary with the footage, your CPU/GPU and FFmpeg build.",
             wrap=700, pady=(px(6), 0))

        act = ttk.Frame(self.tabs, padding=px(10))
        self.tabs.add(act, text="  Activity  ")
        self.act_tab = act
        self.logbox = LogBox(act)
        self.logbox.pack(fill="both", expand=True)

        for var in (self.profile_var, self.limit_var, self.custom_mb, self.res_var, self.aud_var,
                    self.out_dir, self.suffix, self.container):
            var.trace_add("write", lambda *_: self.on_option_change())
        self.refresh_ffmpeg_label()
        self.on_option_change()

    # ---- settings --------------------------------------------------------------------
    def target_mb(self):
        if self.profile_var.get() == "super":
            return None
        v = self.limit_var.get()
        if v == "none":
            return None
        try:
            mb = float(self.custom_mb.get() if v == "custom" else v)
            return mb if mb > 0 else None
        except ValueError:
            return None

    def current_cfg(self):
        return {"profile": self.profile_var.get(), "height": self.HEIGHTS[self.res_var.get()],
                "audio": self.AUDIO[self.aud_var.get()], "target_mb": self.target_mb()}

    def cfg_for(self, info):
        """current_cfg(), minus the size limit when this video is already under it."""
        cfg = self.current_cfg()
        if cfg["target_mb"] and info and info["size"] <= cfg["target_mb"] * 1_000_000 * 0.97:
            cfg["target_mb"] = None
        return cfg

    def on_option_change(self):
        is_super = self.profile_var.get() == "super"
        for w in self.limit_row.winfo_children():
            w.state(["disabled"] if is_super else ["!disabled"])
        self.custom_entry.state(["!disabled"] if (self.limit_var.get() == "custom" and not is_super) else ["disabled"])
        if is_super:
            self.limit_hint.configure(text="Super Compressed always aims for 20 MB, so the limit is off.")
        elif self.target_mb():
            self.limit_hint.configure(text=f"Every video is encoded in 2 passes to land just under {self.target_mb():g} MB "
                                           "(software encoder; hardware can't do 2-pass).")
        else:
            self.limit_hint.configure(text="No size limit: the profile decides the quality.")
        self.refresh_rows()
        self.show_selected()

    # ---- ffmpeg ----------------------------------------------------------------------
    def warm_encoders(self):
        if not self.ffmpeg:
            return
        ff = self.ffmpeg

        def work():
            for name in [n for n, _ in hw_candidates()] + ["libsvtav1", "libx265", "libopus"]:
                encoder_available(ff, name)
            self.q.put(("encoders_ready",))
        threading.Thread(target=work, daemon=True).start()

    def refresh_ffmpeg_label(self):
        self.ff_lbl.configure(text=f"Using {self.ffmpeg}" if self.ffmpeg else "FFmpeg not found.",
                              style="Muted.TLabel" if self.ffmpeg else "Error.TLabel")

    def ensure_ffmpeg(self, force=False):
        if self.ffmpeg and not force:
            return
        if not force:
            messagebox.showwarning(APP_NAME, "FFmpeg couldn't be found on this system.\n\n"
                                             "Please locate the ffmpeg executable (or install it from ffmpeg.org).")
        path = filedialog.askopenfilename(title="Locate the ffmpeg executable",
                                          filetypes=[("ffmpeg", "ffmpeg*"), ("All files", "*.*")])
        if path:
            self.ffmpeg = path
            self.ffprobe = sibling_tool(path, "ffprobe")
            self.cfg_store["ffmpeg"] = path
            save_config(self.cfg_store)
            _encoder_cache.clear()
            self.refresh_ffmpeg_label()
            self.warm_encoders()
            for v in self.videos.values():
                if not v.info:
                    self.probe_async(v)

    # ---- videos ------------------------------------------------------------------------
    def add_paths(self, paths):
        new = [p for p in expand_drop(paths, VIDEO_EXTS) if p not in {v.path for v in self.videos.values()}]
        if not new:
            self.footer.set_status("No new videos there.", "warn")
            return
        for p in new:
            v = Video(p)
            v.status = "Reading…"
            iid = self.tree.insert("", "end", values=(os.path.basename(p), "", human_size(os.path.getsize(p)), "", ""))
            self.videos[iid] = v
            self.probe_async(v)
        self.cfg_store["last_dir"] = os.path.dirname(new[0])
        self.refresh_rows()
        if not self.tree.selection():
            self.tree.selection_set(self.tree.get_children()[0])

    def browse_videos(self):
        paths = filedialog.askopenfilenames(title="Choose videos", initialdir=self.cfg_store.get("last_dir"),
                                            filetypes=[("Video files", " ".join("*" + e for e in VIDEO_EXTS)),
                                                       ("All files", "*.*")])
        if paths:
            self.add_paths(list(paths))

    def remove(self):
        if self.busy:
            return
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.videos.pop(iid, None)
        self.show_selected()

    def clear(self):
        if self.busy:
            return
        self.tree.delete(*self.tree.get_children())
        self.videos.clear()
        self.show_selected()

    def probe_async(self, v):
        if not self.ffmpeg:
            return

        def work():
            try:
                self.q.put(("info", v, probe_video(self.ffmpeg, self.ffprobe, v.path)))
            except Exception as e:
                self.q.put(("info_err", v, str(e)))
        threading.Thread(target=work, daemon=True).start()

    def selected(self):
        sel = self.tree.selection()
        return (sel[0], self.videos[sel[0]]) if sel and sel[0] in self.videos else (None, None)

    def out_path(self, v):
        folder = self.out_dir.get().strip() or os.path.dirname(v.path)
        stem = os.path.splitext(os.path.basename(v.path))[0]
        return os.path.join(folder, stem + (self.suffix.get() or "_compressed") + self.container.get())

    def refresh_rows(self):
        cfg = self.current_cfg()
        for iid, v in self.videos.items():
            info = v.trimmed_info()
            length = fmt_time(info["duration"]) if info and info.get("duration") else ""
            if v.trim() and length:
                length += " ✂"
            pred = ""
            if info:
                size_txt = predict_output(info, self.cfg_for(info))[0]
                pred = size_txt.split("  (")[0].split(" (")[0].split(",")[0]
            self.tree.item(iid, values=(os.path.basename(v.path), length, human_size(v.info["size"]) if v.info else
                                        self.tree.set(iid, "size"), pred, v.error or v.status))

    def show_selected(self):
        iid, v = self.selected()
        if not v:
            self.sel_name.configure(text="Drop videos anywhere on this window, or use Add Videos…" if not self.videos
                                    else "Click a video to see its details.")
            self.sel_info.configure(text="")
            self.pred_size.configure(text="—")
            self.pred_time.configure(text="—")
            return
        self.sel_name.configure(text=os.path.basename(v.path))
        if v.error:
            self.sel_info.configure(text=f"Couldn't read this video: {v.error}")
        elif not v.info:
            self.sel_info.configure(text="Reading the video details…")
        else:
            self.sel_info.configure(text=self.format_info(v.info))
        self.start_var.set(fmt_time(v.start) if v.start else "")
        self.end_var.set(fmt_time(v.end) if v.end else "")
        info = v.trimmed_info()
        if info:
            size_txt, time_txt = predict_output(info, self.cfg_for(info))
            if self.cfg_for(info)["target_mb"] != self.current_cfg()["target_mb"]:
                size_txt += "   (already under the limit, so the profile is used)"
            self.pred_size.configure(text=size_txt)
            self.pred_time.configure(text=time_txt)
        else:
            self.pred_size.configure(text="—")
            self.pred_time.configure(text="—")

    def apply_trim(self):
        iid, v = self.selected()
        if not v:
            return
        try:
            s, e = parse_time(self.start_var.get()), parse_time(self.end_var.get())
        except ValueError:
            self.footer.set_status("Trim times must look like 90 or 1:30.", "error")
            return
        dur = (v.info or {}).get("duration")
        if dur:
            s = None if not s or s <= 0 else min(s, dur - 0.5)
            e = None if not e or e >= dur else e
        if s is not None and e is not None and e <= s:
            self.footer.set_status("The end must come after the start.", "error")
            return
        v.start, v.end = s, e
        self.refresh_rows()
        self.show_selected()

    def clear_trim(self):
        iid, v = self.selected()
        if v:
            v.start = v.end = None
            self.refresh_rows()
            self.show_selected()

    @staticmethod
    def format_info(i):
        res = f"{i['width']}×{i['height']}" if i.get("width") else "unknown size"
        fps = f" @ {i['fps']:.2f} fps" if i.get("fps") else ""
        dur = fmt_time(i["duration"]) if i.get("duration") else "unknown"
        br = f"{i['bitrate'] / 1000:.0f} kbps" if i.get("bitrate") else "unknown"
        return (f"{res}{fps}   ·   {i.get('vcodec', '?')}   ·   {dur}   ·   {human_size(i['size'])}   ·   ~{br}   ·   "
                f"audio: {'yes' if i.get('has_audio') else 'no'}")

    def browse_out(self):
        d = filedialog.askdirectory(title="Save the compressed videos in…",
                                    initialdir=self.out_dir.get() or self.cfg_store.get("last_dir"))
        if d:
            self.out_dir.set(os.path.normpath(d))

    def open_row(self, event):
        v = self.videos.get(self.tree.identify_row(event.y))
        if v and v.output and os.path.exists(v.output):
            open_path(v.output)

    def reveal_last(self):
        if self.last_output and os.path.exists(self.last_output):
            reveal(self.last_output)

    # ---- run / cancel ------------------------------------------------------------------
    def start(self):
        if self.busy:
            return
        if not self.ffmpeg:
            return self.ensure_ffmpeg()
        if not self.videos:
            return messagebox.showinfo(APP_NAME, "Add some videos first (drop them on the window).")
        self.apply_trim()
        if self.limit_var.get() == "custom" and self.profile_var.get() != "super" and not self.target_mb():
            return messagebox.showerror(APP_NAME, "Enter a valid custom size limit in MB.")
        jobs, problems = [], []
        for iid in self.tree.get_children():
            v = self.videos[iid]
            out = os.path.abspath(self.out_path(v))
            if not v.info:
                problems.append(f"{os.path.basename(v.path)}: {'unreadable' if v.error else 'still reading details'}")
                continue
            if os.path.normcase(out) == os.path.normcase(os.path.abspath(v.path)):
                problems.append(f"{os.path.basename(v.path)}: output would overwrite the original")
                continue
            jobs.append((iid, v, out))
        if problems:
            if not jobs:
                return messagebox.showerror(APP_NAME, "Nothing can be compressed:\n\n" + "\n".join(problems))
            if not messagebox.askyesno(APP_NAME, "These will be skipped:\n\n" + "\n".join(problems) + "\n\nContinue with the rest?"):
                return
        existing = [out for _, _, out in jobs if os.path.exists(out)]
        if existing and not messagebox.askyesno(APP_NAME, f"{len(existing)} output file(s) already exist and will be "
                                                          "replaced:\n\n" + "\n".join(os.path.basename(p) for p in existing[:10])
                                                          + "\n\nContinue?"):
            return
        cfg = self.current_cfg()
        self.busy = True
        self.cancel_evt.clear()
        self.start_btn.state(["disabled"])
        self.cancel_btn.pack(side="right", padx=(px(8), 0))
        self.footer.set_progress(0)
        for iid, v, _ in jobs:
            v.status = "Waiting"
            self.tree.item(iid, tags=("pending",))
        self.refresh_rows()
        self.started = time.perf_counter()
        threading.Thread(target=self.worker, args=(jobs, cfg), daemon=True).start()

    def do_cancel(self):
        self.cancel_evt.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.footer.set_status("Cancelling…", "warn")

    # ---- worker thread (NO tkinter calls in here, only queue.put) ----------------------
    def run_cmds(self, cmds, dur, on_progress, prefix=""):
        """Run one encode (1 or 2 FFmpeg passes) and stream real progress."""
        n = len(cmds)
        for i, cmd in enumerate(cmds):
            label = f"{prefix}pass {i + 1}/{n}" if n > 1 else f"{prefix}encoding"
            tail = deque(maxlen=20)
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace", cwd=self._workdir, bufsize=1, **NO_WINDOW)
            speed = ""
            for line in self.proc.stdout:
                line = line.strip()
                if line.startswith(("out_time_us=", "out_time_ms=")):
                    val = line.split("=", 1)[1]
                    if val.lstrip("-").isdigit() and dur:
                        cur = max(0, int(val)) / 1_000_000
                        on_progress((i + min(cur / dur, 1.0)) / n, f"{label} · {fmt_time(cur)} / {fmt_time(dur)} {speed}")
                elif line.startswith("speed="):
                    speed = "· speed " + line.split("=", 1)[1].strip()
                elif "=" not in line or line.startswith("Error"):
                    if line:
                        tail.append(line)
            self.proc.wait()
            if self.cancel_evt.is_set():
                raise InterruptedError
            if self.proc.returncode != 0:
                raise RuntimeError("\n".join(tail) or f"FFmpeg exited with code {self.proc.returncode}")

    def worker(self, jobs, cfg):
        n = len(jobs)
        for k, (iid, v, out) in enumerate(jobs):
            if self.cancel_evt.is_set():
                break
            name = os.path.basename(v.path)
            self.q.put(("row", iid, "Starting…", "working"))
            self._workdir = tempfile.mkdtemp(prefix="vc_")
            info = v.trimmed_info()
            run_cfg = dict(cfg, trim=v.trim())
            under_limit = cfg["target_mb"] and info["size"] <= cfg["target_mb"] * 1_000_000 * 0.97
            if under_limit:  # aiming AT the limit would only make it bigger
                run_cfg["target_mb"] = None

            def progress(frac, text, k=k, iid=iid):
                self.q.put(("progress", (k + frac) / n * 100))
                self.q.put(("row", iid, f"{frac * 100:.0f}% · {text.split(' · ')[0]}", "working"))
                self.q.put(("status", f"{name}: {text}"))

            try:
                dur = info.get("duration") or 0
                limit = SUPER_TARGET_MB * 1024 * 1024
                attempt = 0
                while True:
                    prefix = f"retry {attempt}: " if attempt else ""
                    cmds, notes = plan_encode(self.ffmpeg, run_cfg, info, v.path, out, self._workdir)
                    self.run_cmds(cmds, dur, progress, prefix)
                    final = os.path.getsize(out)
                    if cfg["profile"] != "super" or final <= limit:
                        break
                    prev = run_cfg.get("super_kbps") or int(super_video_kbps(info, cfg["height"])[0])
                    new = int(prev * limit / final * 0.97)
                    if attempt >= 2 or new < SUPER_MIN_VIDEO_KBPS:
                        notes.append(f"still {human_size(final - limit)} over {SUPER_TARGET_MB} MB")
                        break
                    attempt += 1
                    run_cfg["super_kbps"] = new
                v.output = out
                before = info["size"]
                saved = (1 - final / before) * 100 if before else 0
                if under_limit:
                    notes.append(f"already under {cfg['target_mb']:g} MB, so the profile was used instead")
                change = f"{abs(saved):.0f}% {'smaller' if saved >= 0 else 'larger'}"
                self.q.put(("row", iid, f"✓ {human_size(final)} ({change})", "done"))
                self.q.put(("log", f"{name}: {human_size(before)} → {human_size(final)}  [{'; '.join(notes)}]", "ok"))
                self.q.put(("saved", out))
            except InterruptedError:
                if os.path.exists(out):
                    try:
                        os.remove(out)
                    except OSError:
                        pass
                self.q.put(("row", iid, "Cancelled", "pending"))
                break
            except Exception as e:
                self.q.put(("row", iid, "Failed", "error"))
                self.q.put(("log", f"{name}: {str(e)[-1500:]}", "error"))
            finally:
                shutil.rmtree(self._workdir, ignore_errors=True)
                self.proc = None
        self.q.put(("done",))

    # ---- queue polling (runs on the Tk thread) -------------------------------------------
    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "info":
                    msg[1].info, msg[1].status = msg[2], ""
                    self.refresh_rows()
                    self.show_selected()
                elif kind == "info_err":
                    msg[1].error, msg[1].status = msg[2][:80], ""
                    self.refresh_rows()
                    self.show_selected()
                elif kind == "encoders_ready":
                    self.refresh_rows()
                    self.show_selected()
                elif kind == "row":
                    v = self.videos.get(msg[1])
                    if v:
                        v.status = msg[2]
                        self.tree.set(msg[1], "status", msg[2])
                        self.tree.item(msg[1], tags=(msg[3],))
                elif kind == "progress":
                    self.footer.set_progress(msg[1])
                elif kind == "status":
                    self.footer.set_status(msg[1])
                elif kind == "log":
                    self.logbox.log(msg[1], msg[2])
                elif kind == "saved":
                    self.last_output = msg[1]
                elif kind == "done":
                    self.finished()
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def finished(self):
        self.busy = False
        self.start_btn.state(["!disabled"])
        self.cancel_btn.pack_forget()
        self.footer.set_progress(None)
        statuses = [v.status for v in self.videos.values()]
        ok = sum(1 for s in statuses if s.startswith("✓"))
        bad = statuses.count("Failed")
        for iid, v in self.videos.items():
            if v.status == "Waiting":
                v.status = ""
                self.tree.item(iid, tags=())
        self.refresh_rows()
        secs = time.perf_counter() - self.started
        if bad:
            self.footer.set_status(f"{ok} compressed, {bad} failed. See the Activity tab.", "error")
            self.tabs.select(self.act_tab)
        elif self.cancel_evt.is_set():
            self.footer.set_status(f"Cancelled. {ok} video(s) finished.", "warn")
        else:
            self.footer.set_status(f"Done in {fmt_eta(secs)}  ·  {ok} video(s) compressed", "ok")

    def on_close(self):
        if self.busy:
            if not messagebox.askyesno(APP_NAME, "A video is being compressed. Cancel it and quit?"):
                return
            self.do_cancel()
        self.cfg_store.update(profile=self.profile_var.get(), limit=self.limit_var.get(), custom_mb=self.custom_mb.get(),
                              res=self.res_var.get(), audio=self.aud_var.get(), out_dir=self.out_dir.get().strip(),
                              suffix=self.suffix.get(), container=self.container.get())
        self.cfg_store.update(self.updater.state())
        save_config(self.cfg_store)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
