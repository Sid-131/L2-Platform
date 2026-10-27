"""Demo mode: the real app over a made-up dataset, with one-click sign-in per role.

Nothing in here changes how the platform behaves. It only decides where the data
lives, swaps the password form for three role buttons, and keeps visitors from
locking each other out of the shared demo accounts.

Usage, before anything imports `store` or `webapp`:

    import demo
    demo.prepare()
    from webapp import app
    demo.install(app)
"""

from __future__ import annotations

import os
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PRISTINE = os.path.join(HERE, "demo_data")
SAMPLE = os.path.join(PRISTINE, "sample_next_week.csv")

# username -> what the visitor is told about that role
PERSONAS = {
    "admin": ("alex.rivera", "Alex Rivera", "Admin",
              "Uploads the weekly export, tunes the router, manages the team."),
    "manager": ("maya.collins", "Maya Collins", "Manager",
                "Sees every queue, fixes routing in Triage, reads the TSE report."),
    "l2": ("jordan.lee", "Jordan Lee", "L2 reviewer",
           "Works a personal queue on the keyboard: verdict, call made, comment."),
}


def _data_dir() -> str:
    return os.environ["L2_DATA"]


def _copy_pristine() -> None:
    d = _data_dir()
    os.makedirs(os.path.join(d, "uploads"), exist_ok=True)
    for name in ("l2platform.db", "settings.json"):
        shutil.copyfile(os.path.join(PRISTINE, name), os.path.join(d, name))


def prepare() -> None:
    """Point the app at a writable copy of the demo data. On a serverless host only
    /tmp is writable, and each instance gets its own copy - edits are temporary."""
    os.environ.setdefault("L2_DATA", os.path.join(tempfile.gettempdir(), "l2demo"))
    os.environ.setdefault("L2_SECRET", "l2-demo-only-not-a-secret")
    if not os.path.exists(os.path.join(_data_dir(), "l2platform.db")):
        _copy_pristine()


def install(app) -> None:
    from flask import (flash, redirect, request, send_file, session, url_for)
    from sqlalchemy import select

    import services as svc
    from models import User
    from store import engine, session_scope

    # Made-up case numbers must not turn into links to a real CRM
    svc.salesforce_url = lambda _lid: ""

    if os.environ.get("VERCEL"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.config["SESSION_COOKIE_SECURE"] = True
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    def persona_ids() -> set[int]:
        with session_scope() as s:
            return set(s.scalars(select(User.id).where(
                User.username.in_([p[0] for p in PERSONAS.values()]))).all())

    @app.context_processor
    def _demo_ctx():
        return {"demo": True, "personas": PERSONAS}

    @app.before_request
    def _demo_guard():
        if request.method != "POST":
            return None
        if request.path == "/password":
            flash("Password changes are switched off in the demo.")
            return redirect(url_for("my_queue"))
        if (request.path == "/users"
                and request.form.get("action") in ("toggle", "reset", "delete")
                and request.form.get("user_id", type=int) in persona_ids()):
            flash("The three demo accounts can't be disabled, reset or deleted. "
                  "Try it on anyone else.")
            return redirect(url_for("users_page"))
        return None

    @app.get("/demo/as/<role>")
    def demo_as(role):
        p = PERSONAS.get(role)
        if not p:
            return redirect(url_for("login"))
        with session_scope() as s:
            uid = s.scalar(select(User.id).where(User.username == p[0]))
        if uid is None:                 # someone deleted it on this instance
            _reset()
            return redirect(url_for("demo_as", role=role))
        session.clear()
        session["uid"] = uid
        return redirect(url_for("dashboard") if role == "manager" else url_for("my_queue"))

    @app.get("/demo/sample.csv")
    def demo_sample():
        return send_file(SAMPLE, as_attachment=True, download_name="weekly_export_sample.csv",
                         mimetype="text/csv")

    def _reset():
        engine.dispose()                # no pooled handle may outlive the file swap
        _copy_pristine()

    @app.post("/demo/reset")
    def demo_reset():
        _reset()
        session.clear()
        flash("Demo data reset.")
        return redirect(url_for("login"))
