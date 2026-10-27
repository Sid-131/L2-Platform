"""Vercel entry point: the platform in demo mode, as a single WSGI function."""
import os
import sys
from urllib.parse import parse_qsl, urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import demo  # noqa: E402

demo.prepare()

from webapp import app  # noqa: E402

demo.install(app)


class RestorePath:
    """vercel.json rewrites every URL to this function and passes the original path
    as ?__path=. Put it back before Flask routes the request, otherwise Flask sees
    /api/index and answers 404 for every page."""

    def __init__(self, wsgi):
        self.wsgi = wsgi

    def __call__(self, environ, start_response):
        params = parse_qsl(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        path = next((v for k, v in params if k == "__path"), None)
        if path is not None:
            environ["PATH_INFO"] = "/" + path.lstrip("/")
            environ["QUERY_STRING"] = urlencode([(k, v) for k, v in params if k != "__path"])
        elif environ.get("PATH_INFO", "").startswith("/api/index"):
            environ["PATH_INFO"] = "/"
        return self.wsgi(environ, start_response)


app.wsgi_app = RestorePath(app.wsgi_app)
