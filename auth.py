"""Login, roles and access control.

Attribution has to be real: a bucket of Behaviour against a named engineer, written
by someone who could be anyone, is worthless the first time it is disputed. Hence
real accounts rather than a name picker, even for the pilot.
"""

from __future__ import annotations

from datetime import datetime
from functools import wraps

from flask import flash, g, redirect, request, session, url_for
from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

from models import User
from store import session_scope

ROLES = ("admin", "manager", "l2")


def hash_password(raw: str) -> str:
    return generate_password_hash(raw)


def verify(user: User, raw: str) -> bool:
    return check_password_hash(user.password_hash, raw)


def authenticate(username: str, raw: str) -> User | None:
    with session_scope() as s:
        u = s.scalar(select(User).where(User.username == username.strip().lower(),
                                        User.active.is_(True)))
        if u and verify(u, raw):
            u.last_login = datetime.now()
            s.flush()
            s.expunge(u)
            return u
    return None


def current_user() -> User | None:
    if "uid" not in session:
        return None
    if getattr(g, "_user", None) is None:
        with session_scope() as s:
            u = s.get(User, session["uid"])
            if u:
                _ = u.queues()          # load memberships before detaching
                s.expunge(u)
            g._user = u
    return g._user


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if not u:
            # a link from outside (EWS) should land on its page after sign-in
            if request.method == "GET":
                session["next"] = request.full_path.rstrip("?")
            return redirect(url_for("login"))
        if u.must_change_password and fn.__name__ != "change_password":
            flash("Set a new password before continuing.")
            return redirect(url_for("change_password"))
        return fn(*a, **kw)
    return wrapper


def roles_required(*allowed):
    def deco(fn):
        @wraps(fn)
        @login_required
        def wrapper(*a, **kw):
            u = current_user()
            if u.role not in allowed:
                flash("That page is not available for your role.")
                return redirect(url_for("my_queue"))
            return fn(*a, **kw)
        return wrapper
    return deco


def set_password(user_id: int, raw: str) -> None:
    with session_scope() as s:
        u = s.get(User, user_id)
        u.password_hash = hash_password(raw)
        u.must_change_password = False
