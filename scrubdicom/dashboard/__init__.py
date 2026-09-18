def main():
    """Developer dashboard (Streamlit). Bound to this machine only and with Streamlit's usage telemetry off:
    the tool handles patient data, so nothing may listen beyond localhost or report anywhere."""
    import subprocess, sys
    from pathlib import Path
    app = Path(__file__).with_name("app.py")
    sys.exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app), "--server.headless", "false",
                              "--server.address", "127.0.0.1", "--browser.gatherUsageStats", "false",
                              "--server.enableCORS", "true", "--server.enableXsrfProtection", "true"]))
