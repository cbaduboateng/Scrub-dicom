"""Scrub-DICOM desktop app entry point.

    scrub-dicom-app                 open the window
    scrub-dicom-app --selftest      open the window, draw every tab, close after a moment (build check)
    scrub-dicom-app --cli <args>    run the engine CLI instead (used by the app to start its own child
                                    processes when packaged; never imports Tk)

The packaged program (PyInstaller) is a single executable that starts here, so the window can launch
`<itself> --cli run ...` as the engine child process exactly as the CLI would be run from a terminal.
"""
from __future__ import annotations

import sys


def _ensure_std_streams() -> None:
    """A windowed (console-less) build on Windows has sys.stdout/stderr set to None unless the parent gave it
    pipes. The engine prints; make sure there is always somewhere for print() to go."""
    import io
    import os
    for name, fd in (("stdout", 1), ("stderr", 2)):
        if getattr(sys, name) is None:
            try:
                setattr(sys, name, io.TextIOWrapper(os.fdopen(fd, "wb", closefd=False), encoding="utf-8", errors="replace", line_buffering=True))
            except OSError:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _cli(args: list[str]) -> int:
    import warnings
    warnings.simplefilter("ignore")      # same as the launchers' `python -W ignore`
    _ensure_std_streams()
    from scrubdicom.core import main as core_main
    try:
        rc = core_main(args)
    except SystemExit as e:              # the engine uses sys.exit("message") for user errors
        code = e.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(str(code), file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        return 130
    return int(rc or 0)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--cli":
        return _cli(argv[1:])
    from .ui import run_app
    return run_app(selftest="--selftest" in argv)


if __name__ == "__main__":
    sys.exit(main())
