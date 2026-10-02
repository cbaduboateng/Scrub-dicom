"""Drop a folder on the window. Optional: without the tkdnd extension the app is exactly as before.

Tk has no drag-and-drop of its own. The tkdnd extension (BSD-style licence, bundled by the tkinterdnd2 package, MIT)
adds the platform's native one: Cocoa on macOS, OLE on Windows. It is a small native library loaded into the
window's own Tcl interpreter; it opens no sockets and reads nothing but the paths the user drops. Only the loader
from tkinterdnd2 is used; the drop target and its binding are set up through Tcl directly, so nothing here
depends on that package's widget classes.
"""
from __future__ import annotations

from pathlib import Path


def dropped_paths(root, data: str) -> list[Path]:
    """The paths in a drop's data: a Tcl list, with braces around names that contain spaces."""
    try:
        items = root.tk.splitlist(data)
    except Exception:
        return []
    return [Path(str(i)) for i in items if str(i).strip()]


def folder_of(paths: list[Path]) -> Path | None:
    """The folder a drop means: the first folder dropped, or the folder holding the first file."""
    for p in paths:
        try:
            if p.is_dir():
                return p
        except OSError:
            continue
    for p in paths:
        try:
            if p.is_file():
                return p.parent
        except OSError:
            continue
    return None


def enable(root, on_folder) -> bool:
    """Make the whole window accept dropped files and folders; `on_folder(Path)` is called with the folder meant.
    Returns False, silently, when the extension is not available."""
    try:
        from tkinterdnd2 import TkinterDnD
        TkinterDnD._require(root)                    # loads the tkdnd library for this platform into the interpreter
        root.tk.call("tkdnd::drop_target", "register", root._w, "DND_Files")

        def handler(data: str) -> str:
            folder = folder_of(dropped_paths(root, data))
            if folder is not None:
                root.after(10, lambda: on_folder(folder))     # return to the system first; then act
            return "copy"

        root.tk.call("bind", root._w, "<<Drop>>", root.register(handler) + " %D")
        return True
    except Exception:
        return False
