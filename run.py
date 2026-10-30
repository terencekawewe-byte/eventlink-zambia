"""Entry point for the EventLink Zambia Flask application."""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(ROOT, ".venv")
VENV_PYTHON = os.path.join(VENV, "Scripts", "python.exe") if os.name == "nt" else os.path.join(VENV, "bin", "python")


def _relaunch_in_project_venv():
    """Run with the project's .venv when started from another Python, which lacks the dependencies."""
    if not os.path.exists(VENV_PYTHON):
        return
    if os.path.normcase(os.path.realpath(sys.prefix)) == os.path.normcase(os.path.realpath(VENV)):
        return
    process = subprocess.Popen([VENV_PYTHON, os.path.abspath(__file__), *sys.argv[1:]])
    while True:
        try:
            sys.exit(process.wait())
        except KeyboardInterrupt:
            continue


if __name__ == "__main__":
    _relaunch_in_project_venv()

from dotenv import load_dotenv

load_dotenv()

from app import create_app

app = create_app()

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    reload = os.environ.get("FLASK_RELOAD", "1") == "1"
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=debug, use_reloader=reload)
