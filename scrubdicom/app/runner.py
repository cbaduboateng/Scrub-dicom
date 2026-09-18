"""Run the engine as a child process and stream its output to the window without blocking it.

One EngineProcess per run / verify / thick invocation. A reader thread copies the child's stdout into a queue
(and, if asked, into a log file under <output>/_logs); the Tk side drains the queue on a timer. Stop
terminates the child: a study interrupted mid-way has no .complete marker, so the next --resume redoes it.
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from pathlib import Path


class EngineProcess:
    def __init__(self, cmd: list[str], log_path: Path | None = None):
        self.cmd = list(cmd)
        self.log_path = Path(log_path) if log_path else None
        self.started = 0.0
        self.finished: float | None = None
        self._proc: subprocess.Popen | None = None
        self._q: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        kw: dict = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.started = time.time()
        self._proc = subprocess.Popen(
            self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env, **kw)
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        fh = None
        if self.log_path:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                fh = open(self.log_path, "a", encoding="utf-8")
                fh.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S')}  {' '.join(self.cmd)}\n")
            except OSError:
                fh = None
        try:
            for line in self._proc.stdout:
                line = line.rstrip("\r\n")
                self._q.put(line)
                if fh:
                    try:
                        fh.write(line + "\n")
                        fh.flush()
                    except OSError:
                        pass
        finally:
            self._proc.wait()
            self.finished = time.time()
            if fh:
                try:
                    fh.write(f"# exit code {self._proc.returncode}\n")
                    fh.close()
                except OSError:
                    pass

    # ------------------------------------------------------------------ queries
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc is not None else None

    @property
    def done(self) -> bool:
        """True once the process has exited AND every line has been read into the queue."""
        return self._proc is not None and self._proc.poll() is not None and (self._thread is None or not self._thread.is_alive())

    def poll_lines(self, limit: int = 2000) -> list[str]:
        out: list[str] = []
        try:
            while len(out) < limit:
                out.append(self._q.get_nowait())
        except queue.Empty:
            pass
        return out

    def elapsed(self) -> float:
        end = self.finished or time.time()
        return end - self.started if self.started else 0.0

    # ------------------------------------------------------------------ control
    def stop(self, grace_s: float = 5.0) -> None:
        if not self.running:
            return
        assert self._proc is not None
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=grace_s)
        except OSError:
            pass

    def wait(self, timeout: float | None = None) -> int | None:
        """Blocking wait, for tests and scripts; the window never calls this."""
        if self._proc is None:
            return None
        self._proc.wait(timeout=timeout)
        if self._thread:
            self._thread.join(timeout=timeout)
        return self._proc.returncode
