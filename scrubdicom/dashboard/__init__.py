def main():
    import subprocess, sys
    from pathlib import Path
    app = Path(__file__).with_name("app.py")
    sys.exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app), "--server.headless", "false"]))
