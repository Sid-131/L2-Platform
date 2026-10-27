"""Production launcher: Waitress rather than the Flask dev server."""
import os
import sys
import threading
import webbrowser

from waitress import serve

from store import init_db
from webapp import app

if __name__ == "__main__":
    # Env first so systemd can configure it; CLI flags override for local runs.
    port = int(os.environ.get("L2_PORT", 5000))
    host = os.environ.get("L2_HOST", "127.0.0.1")
    for i, a in enumerate(sys.argv):
        if a == "--port":
            port = int(sys.argv[i + 1])
    if "--lan" in sys.argv:
        host = "0.0.0.0"
    init_db()
    url = f"http://127.0.0.1:{port}"
    print(f"\n  L2 Review Platform at {url}")
    if host == "0.0.0.0":
        print("  Reachable from other machines on this network.")
    print("  Close this window to stop.\n")
    if "--no-browser" not in sys.argv and sys.stdout.isatty():
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    serve(app, host=host, port=port, threads=8)
