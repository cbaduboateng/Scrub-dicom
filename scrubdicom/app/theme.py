"""Look and feel: the Sun Valley ttk theme (light / dark, following the system), plus a small palette for the few
classic Tk widgets (Text, Listbox, Canvas) that ttk themes do not restyle.

sv_ttk is optional: without it the app falls back to the platform's native ttk theme and still works.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from tkinter import ttk

try:
    import sv_ttk
except ImportError:  # pragma: no cover
    sv_ttk = None

PALETTES = {
    "light": {"bg": "#fafafa", "panel": "#ffffff", "text": "#1f2328", "muted": "#6e7781", "border": "#d0d7de",
              "ok": "#1a7f37", "warn": "#9a6700", "error": "#cf222e", "accent": "#0969da", "keep": "#1a7f37", "drop": "#6e7781",
              "check": "#9a6700", "block": "#cf222e", "override": "#0969da", "removed": "#cf222e", "changed": "#9a6700",
              "added": "#1a7f37", "maybe": "#9a6700", "review": "#cf222e", "mono_bg": "#ffffff", "select": "#ddf4ff"},
    "dark": {"bg": "#1c1c1e", "panel": "#2c2c2e", "text": "#e6e6e6", "muted": "#9a9aa0", "border": "#3a3a3c",
             "ok": "#3fb950", "warn": "#d29922", "error": "#f85149", "accent": "#58a6ff", "keep": "#3fb950", "drop": "#9a9aa0",
             "check": "#d29922", "block": "#f85149", "override": "#58a6ff", "removed": "#f85149", "changed": "#d29922",
             "added": "#3fb950", "maybe": "#d29922", "review": "#f85149", "mono_bg": "#121214", "select": "#264f78"},
}
_current = "light"


def system_prefers_dark() -> bool:
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True, timeout=2)
            return r.returncode == 0 and "dark" in r.stdout.lower()
        if os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        pass
    return False


def apply(root: tk.Misc, mode: str | None = None) -> str:
    """Apply light or dark (default: follow the system). Returns the mode in force."""
    global _current
    mode = mode or ("dark" if system_prefers_dark() else "light")
    _current = mode if mode in PALETTES else "light"
    if sv_ttk is not None:
        try:
            sv_ttk.set_theme(_current, root)
        except Exception:
            pass
    pal = palette()
    try:
        root.configure(bg=pal["bg"])
    except tk.TclError:
        pass
    style = ttk.Style(root)
    style.configure("Muted.TLabel", foreground=pal["muted"])
    style.configure("Title.TLabel", font=("TkDefaultFont", 18, "bold"))
    style.configure("H2.TLabel", font=("TkDefaultFont", 13, "bold"))
    style.configure("Ok.TLabel", foreground=pal["ok"])
    style.configure("Warn.TLabel", foreground=pal["warn"])
    style.configure("Error.TLabel", foreground=pal["error"])
    style.configure("Big.TLabel", font=("TkDefaultFont", 14, "bold"))
    return _current


def current() -> str:
    return _current


def palette() -> dict[str, str]:
    return PALETTES[_current]


def toggle(root: tk.Misc) -> str:
    return apply(root, "light" if _current == "dark" else "dark")


def style_text(widget: tk.Text | tk.Listbox) -> None:
    """Colour a classic Text/Listbox to match the theme."""
    pal = palette()
    try:
        widget.configure(bg=pal["mono_bg"], fg=pal["text"], insertbackground=pal["text"], selectbackground=pal["select"],
                         selectforeground=pal["text"], highlightthickness=1, highlightbackground=pal["border"], relief="flat")
    except tk.TclError:
        pass


def tag_colours(tree: ttk.Treeview | tk.Text, keys: tuple[str, ...]) -> None:
    pal = palette()
    for k in keys:
        if k in pal:
            tree.tag_configure(k, foreground=pal[k])


def style_or(name: str, fallback: str = "") -> str:
    """A Sun Valley-only style name (Accent.TButton, Toggle.TButton, Switch.TCheckbutton) when the theme is active,
    else the default style, so the app still runs without sv_ttk."""
    return name if sv_ttk is not None else fallback
