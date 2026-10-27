"""Run the demo locally:  python run_demo.py  ->  http://127.0.0.1:5000"""
import os
import tempfile

os.environ.setdefault("L2_DATA", os.path.join(tempfile.gettempdir(), "l2demo-local"))

import demo  # noqa: E402

demo.prepare()

from waitress import serve  # noqa: E402

from webapp import app  # noqa: E402

demo.install(app)

if __name__ == "__main__":
    print("\n  L2 Review Platform demo at http://127.0.0.1:5000\n")
    serve(app, host="127.0.0.1", port=5000, threads=8)
