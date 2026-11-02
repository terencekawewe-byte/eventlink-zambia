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

def _ssl_context():
    """Certificate and key for HTTPS, which phone browsers require before they allow camera access."""
    cert = os.environ.get("SSL_CERT_FILE", "").strip()
    key = os.environ.get("SSL_KEY_FILE", "").strip()
    if not cert and not key:
        return None
    if not (os.path.isfile(cert) and os.path.isfile(key)):
        sys.exit("SSL_CERT_FILE and SSL_KEY_FILE must both point to existing files.")
    return (cert, key)


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    reload = os.environ.get("FLASK_RELOAD", "1") == "1"
    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("HOST", "127.0.0.1").strip() or "127.0.0.1"
    if debug and host not in {"127.0.0.1", "localhost"}:
        sys.exit("FLASK_DEBUG=1 is only allowed with HOST=127.0.0.1. Turn debug off before sharing on the network.")
    app.run(host=host, port=port, debug=debug, use_reloader=reload, ssl_context=_ssl_context())
