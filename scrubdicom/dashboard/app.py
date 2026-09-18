"""Scrub-DICOM dashboard: a small local web page (Streamlit) to run a job and watch it, review series
decisions, and read the verify report.   Start with:  scrub-dicom-dashboard   or  streamlit run app.py"""
from __future__ import annotations
import csv, io, os, subprocess, sys, time
from pathlib import Path
import streamlit as st

st.set_page_config(page_title="Scrub-DICOM", page_icon="🫀", layout="wide")
st.title("Scrub-DICOM")
st.caption("Batch pseudonymisation of cardiac CT for blinded reads. Originals are never modified.")

# ----------------------------------------------------------------------------- helpers
def read_csv(p: Path) -> list[dict]:
    try:
        with open(p, newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def latest(logs: Path, prefix: str) -> Path | None:
    files = sorted(logs.glob(f"{prefix}_*.csv")) + sorted(logs.glob(f"{prefix}_*.txt"))
    return files[-1] if files else None


def study_state(out: Path) -> dict:
    done, partial = [], []
    if out.is_dir():
        for p in sorted(out.iterdir()):
            if p.is_dir() and not p.name.startswith(("_", ".")):
                (done if any(p.glob(".complete*")) else partial).append(p.name)
    return {"done": done, "partial": partial}


def cli() -> list[str]:
    return [sys.executable, "-u", "-W", "ignore", "-m", "scrubdicom"]


# ----------------------------------------------------------------------------- sidebar: paths
with st.sidebar:
    st.header("Paths")
    out_dir = st.text_input("Output folder", value=st.session_state.get("out_dir", ""), placeholder="/Volumes/Drive/Reader_Study")
    st.session_state["out_dir"] = out_dir
    manifest = st.text_input("Manifest CSV (source_folder, study_id)", value=st.session_state.get("manifest", ""))
    st.session_state["manifest"] = manifest
    picks = st.text_input("Series-pick CSV (optional)", value=st.session_state.get("picks", ""))
    st.session_state["picks"] = picks
    st.divider()
    ctca_only = st.checkbox("Keep only the coronary CTA series (--ctca-only)", value=True)
    resume = st.checkbox("Resume: skip completed studies", value=True)
    keep_tech = st.checkbox("Keep kernel / scan options (--keep-technical)", value=False)

out = Path(out_dir).expanduser() if out_dir else None
logs = out / "_logs" if out else None

tab_run, tab_series, tab_verify, tab_about = st.tabs(["Run", "Series decisions", "Verify", "About"])

# ----------------------------------------------------------------------------- Run
with tab_run:
    if out and out.exists():
        s = study_state(out)
        c1, c2, c3 = st.columns(3)
        c1.metric("Studies complete", len(s["done"]))
        c2.metric("Half-finished (will be redone)", len(s["partial"]))
        n_manifest = len(read_csv(Path(manifest))) if manifest and Path(manifest).exists() else None
        c3.metric("In manifest", n_manifest if n_manifest is not None else "-")
    proc = st.session_state.get("proc")
    running = proc is not None and proc.poll() is None
    col_a, col_b = st.columns([1, 1])
    if col_a.button("Start run", type="primary", disabled=running or not (manifest and out)):
        cmd = cli() + ["run", "--manifest", manifest, "--output", str(out)]
        if resume: cmd.append("--resume")
        if ctca_only: cmd.append("--ctca-only")
        if keep_tech: cmd.append("--keep-technical")
        if picks: cmd += ["--series-pick", picks]
        logf = out / "_logs"; logf.mkdir(parents=True, exist_ok=True)
        st.session_state["logfile"] = str(logf / f"dashboard_run_{time.strftime('%Y%m%d_%H%M%S')}.txt")
        fh = open(st.session_state["logfile"], "w")
        st.session_state["proc"] = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True)
        st.rerun()
    if col_b.button("Run verify only", disabled=running or not out):
        logf = out / "_logs"; logf.mkdir(parents=True, exist_ok=True)
        st.session_state["logfile"] = str(logf / f"dashboard_verify_{time.strftime('%Y%m%d_%H%M%S')}.txt")
        fh = open(st.session_state["logfile"], "w")
        st.session_state["proc"] = subprocess.Popen(cli() + ["verify", "--output", str(out)], stdout=fh, stderr=subprocess.STDOUT, text=True)
        st.rerun()
    if running and st.button("Stop"):
        proc.terminate()
    lf = st.session_state.get("logfile")
    if lf and Path(lf).exists():
        text = Path(lf).read_text(errors="replace")
        lines = [l for l in text.splitlines() if "already complete" not in l]
        last = [l for l in lines if l.startswith("[")]
        if last:
            st.info(last[-1])
        st.code("\n".join(lines[-40:]) or "(waiting for output)", language="text")
        if running:
            time.sleep(3)
            st.rerun()
        else:
            st.success("Finished" if proc and proc.returncode == 0 else "Stopped / finished with warnings - read the log")

# ----------------------------------------------------------------------------- Series decisions
with tab_series:
    if logs and logs.exists():
        rows = []
        for f in sorted(logs.glob("series_*.csv")):
            rows += read_csv(f)
        if rows:
            flt = st.radio("Show", ["All", "Kept", "Dropped", "Needs a look (CHECK)"], horizontal=True)
            if flt == "Kept": rows = [r for r in rows if r["decision"] == "keep"]
            if flt == "Dropped": rows = [r for r in rows if r["decision"] == "drop"]
            if flt == "Needs a look (CHECK)": rows = [r for r in rows if "CHECK" in r["reason"] or "fallback" in r["reason"]]
            q = st.text_input("Filter by study ID")
            if q: rows = [r for r in rows if q.lower() in r["study_id"].lower()]
            st.dataframe(rows, width="stretch", hide_index=True)
            st.download_button("Download as CSV", data="\n".join([",".join(rows[0].keys())] + [",".join(f'"{v}"' for v in r.values()) for r in rows]) if rows else "",
                               file_name="series_decisions.csv")
        else:
            st.write("No series log yet. Run with --ctca-only to produce one.")
    else:
        st.write("Set the output folder in the sidebar.")

# ----------------------------------------------------------------------------- Verify
with tab_verify:
    rep = latest(logs, "verify") if logs and logs.exists() else None
    if rep:
        txt = rep.read_text(errors="replace")
        (st.success if "PASS" in txt.splitlines()[0:3].__str__() or "\nPASS" in txt else st.error)(txt.splitlines()[0] if txt else "empty report")
        st.code(txt[-8000:], language="text")
    else:
        st.write("No verify report yet.")
    summ = latest(logs, "summary") if logs and logs.exists() else None
    if summ:
        st.subheader("Per-study summary (latest run)")
        st.dataframe(read_csv(summ), width="stretch", hide_index=True)
    st.warning("The linkage log in _logs/LINKAGE_*_CONFIDENTIAL.csv links study IDs back to patients. Move it out of the output folder before sharing the scans.")

# ----------------------------------------------------------------------------- About
with tab_about:
    st.markdown(Path(__file__).with_name("ABOUT.md").read_text() if Path(__file__).with_name("ABOUT.md").exists() else "See README.md")
