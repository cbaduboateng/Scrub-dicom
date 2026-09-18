"""The app's engine runner and CLI re-entry, exercised end to end on synthetic patients: the exact path the window
uses, minus the widgets."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scrubdicom.app import model
from scrubdicom.app.runner import EngineProcess


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    d = tmp_path_factory.mktemp("fx")
    subprocess.run([sys.executable, "-m", "scrubdicom.fixtures", str(d)], check=True, capture_output=True)
    return d


def run_and_collect(cmd, log_path=None, timeout=120):
    proc = EngineProcess(cmd, log_path)
    proc.start()
    lines = []
    t0 = time.time()
    while not proc.done and time.time() - t0 < timeout:
        lines += proc.poll_lines()
        time.sleep(0.05)
    lines += proc.poll_lines()
    return proc, lines


def test_cli_reentry_runs_the_engine(fixtures, tmp_path):
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, "-m", "scrubdicom.app", "--cli", "run", "--input", str(fixtures / "ORFAN0231"), "--output", str(out),
                        "--study-id", "CBB0901"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (out / "CBB0901").is_dir()
    v = subprocess.run([sys.executable, "-m", "scrubdicom.app", "--cli", "verify", "--output", str(out)], capture_output=True, text=True)
    assert v.returncode == 0 and "PASS" in v.stdout


def test_cli_reentry_exit_codes():
    assert subprocess.run([sys.executable, "-m", "scrubdicom.app", "--cli", "run", "--output", "/nowhere"], capture_output=True).returncode == 1
    assert subprocess.run([sys.executable, "-m", "scrubdicom.app", "--cli", "bogus"], capture_output=True).returncode == 2
    assert subprocess.run([sys.executable, "-m", "scrubdicom.app", "--cli", "--help"], capture_output=True).returncode == 0


def test_runner_streams_progress_and_writes_log(fixtures, tmp_path):
    out = tmp_path / "out"
    m = tmp_path / "m.csv"
    m.write_text(f"source_folder,study_id\n{fixtures / 'ORFAN0231'},CBB0501\n{fixtures / 'ORFAN0418'},CBB0502\n{fixtures / 'nope'},CBB0503\n")
    spec = model.JobSpec(mode="manifest", output=str(out), manifest=str(m), resume=True, ctca_only=False, confidential=str(tmp_path / "conf"))
    assert spec.validate() == []
    log = out / "_logs" / "app_run_test.txt"
    proc, lines = run_and_collect(spec.command(), log)
    assert proc.done and proc.returncode == 0, "\n".join(lines)
    progress = [model.parse_progress(l) for l in lines if model.parse_progress(l)]
    assert [p.done for p in progress] == [1, 2, 3] and progress[0].total == 3
    starts = [model.parse_start(l) for l in lines if model.parse_start(l)]
    assert [s[0] for s in starts] == [1, 2], "a 'starting' line per patient found on disk"
    assert any(model.classify_line(l) == "error" for l in lines if "folder not found" in l)
    assert (out / "CBB0501" / ".complete").exists() and (out / "CBB0502" / ".complete").exists()
    text = log.read_text()
    assert text.startswith("# ") and "exit code 0" in text and "[1/3] CBB0501" in text
    # second run: resume lines are the ones the window hides by default
    proc2, lines2 = run_and_collect(spec.command())
    assert proc2.returncode == 0 and sum(model.is_skipped_line(l) for l in lines2) == 2


def test_runner_ctca_only_produces_series_decisions(fixtures, tmp_path):
    out = tmp_path / "out"
    m = tmp_path / "m.csv"
    m.write_text(f"source_folder,study_id\n{fixtures / 'ORFAN0231'},CBB0601\n")
    spec = model.JobSpec(mode="manifest", output=str(out), manifest=str(m), resume=True, ctca_only=True, confidential=str(tmp_path / "conf"))
    proc, lines = run_and_collect(spec.command())
    assert proc.returncode == 0
    rows = model.load_series_rows(tmp_path / "conf")
    assert rows and all(r["decision"] == "drop" for r in rows), "the 4- and 6-image fixture series are below the 100-image rule"
    assert any("NO CORONARY" in l for l in lines)


def test_runner_stop_terminates_child(tmp_path):
    proc = EngineProcess([sys.executable, "-u", "-c", "import time\nprint('started', flush=True)\ntime.sleep(60)"])
    proc.start()
    t0 = time.time()
    while not proc.poll_lines() and time.time() - t0 < 10:
        time.sleep(0.05)
    assert proc.running
    proc.stop()
    assert proc.wait(timeout=10) is not None and not proc.running
    assert proc.returncode != 0


def test_dry_run_creates_nothing_including_no_app_log(fixtures, tmp_path):
    out = tmp_path / "out"
    m = tmp_path / "m.csv"
    m.write_text(f"source_folder,study_id\n{fixtures / 'ORFAN0231'},CBB0701\n")
    spec = model.JobSpec(mode="manifest", output=str(out), manifest=str(m), dry_run=True, confidential=str(tmp_path / "conf"))
    proc, lines = run_and_collect(spec.command())
    assert proc.returncode == 0 and any(l.startswith("DRY RUN") for l in lines)
    assert not out.exists()
