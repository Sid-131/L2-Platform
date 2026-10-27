"""L2 Review Platform - web application."""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta

from flask import (Flask, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)
from sqlalchemy import func, select

import services as svc
from auth import (authenticate, current_user, hash_password, login_required,
                  roles_required, set_password)
from models import Case, CaseWeek, QueueMembership, Review, User, Week
from store import init_db, load_settings, save_settings, session_scope

HERE = os.path.dirname(os.path.abspath(__file__))
UPLOADS = os.path.join(os.environ.get("L2_DATA", HERE), "uploads")
os.makedirs(UPLOADS, exist_ok=True)
ALLOWED = {".xlsx", ".xls", ".csv"}

app = Flask(__name__)
app.secret_key = os.environ.get("L2_SECRET", "change-me-in-production")
if app.secret_key == "change-me-in-production" and os.environ.get("L2_ENV") == "production":
    raise SystemExit("L2_SECRET must be set in production. See /etc/l2platform/env.")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

if os.environ.get("L2_TLS") == "1":
    # Behind nginx with TLS: the session cookie must never travel in clear.
    app.config["SESSION_COOKIE_SECURE"] = True
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


@app.before_request
def _reset_user_cache():
    g._user = None


@app.context_processor
def _inject():
    s = load_settings()
    return {"me": current_user(), "settings": s, "buckets": s["buckets"],
            "queues": s["queues"]}


# ---------------------------------------------------------------- auth

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = authenticate(request.form.get("username", ""), request.form.get("password", ""))
        if not u:
            flash("Wrong username or password.")
            return redirect(url_for("login"))
        session["uid"] = u.id
        nxt = session.pop("next", "")
        # only our own paths: "//host" would be an open redirect
        if nxt.startswith("/") and not nxt.startswith("//"):
            return redirect(nxt)
        return redirect(url_for("my_queue"))
    return render_template("login.html")


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        p1 = request.form.get("p1", "")
        if len(p1) < 8:
            flash("Use at least 8 characters.")
        elif p1 != request.form.get("p2", ""):
            flash("The two entries do not match.")
        else:
            set_password(current_user().id, p1)
            flash("Password updated.")
            return redirect(url_for("my_queue"))
    return render_template("password.html")


# ---------------------------------------------------------------- upload

@app.route("/upload", methods=["GET", "POST"])
@roles_required("admin")
def upload():
    with session_scope() as s:
        weeks = s.scalars(select(Week).order_by(Week.week_ending.desc()).limit(15)).all()
        weeks = [{"label": w.label, "week_ending": w.week_ending, "rows": w.rows_total,
                  "new": w.cases_new, "carried": w.cases_carried,
                  "uploaded_at": w.uploaded_at, "by": w.uploaded_by,
                  "file": w.source_file} for w in weeks]

    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Choose a file first.")
            return redirect(url_for("upload"))
        if os.path.splitext(f.filename)[1].lower() not in ALLOWED:
            flash("Only .xlsx, .xls or .csv are accepted.")
            return redirect(url_for("upload"))
        try:
            wk = datetime.strptime(request.form.get("week_ending", ""), "%Y-%m-%d").date()
        except ValueError:
            flash("Pick the week-ending date.")
            return redirect(url_for("upload"))

        path = os.path.join(UPLOADS, f"{uuid.uuid4().hex[:8]}{os.path.splitext(f.filename)[1]}")
        f.save(path)
        try:
            with session_scope() as s:
                res = svc.ingest_week(s, path, f.filename, wk, current_user().display_name)
        except Exception as exc:                        # noqa: BLE001
            flash(f"Could not process that file: {exc}")
            return redirect(url_for("upload"))
        flash(f"{res['week']}: {res['rows']} rows read. "
              f"{res.get('new', 0)} new, {res.get('changed', 0)} reopened after a change, "
              f"{res.get('periodic', 0)} due a periodic look, "
              f"{res.get('carried', 0)} carried forward unchanged.")
        if res.get("blank_case_ids"):
            flash(f"Warning: {res['blank_case_ids']} of {res['rows']} rows had no case "
                  f"number, so those cases are identified by a hash of their details "
                  f"instead. {res.get('merged_rows', 0)} rows could not be told apart and "
                  f"were merged. Include the case-number column to avoid this.")
        return redirect(url_for("my_queue"))

    default_week = date.today() - timedelta(days=date.today().weekday() + 1)
    return render_template("upload.html", weeks=weeks, default_week=default_week.isoformat())


# ---------------------------------------------------------------- review

@app.get("/")
@login_required
def my_queue():
    s_cfg = load_settings()
    me = current_user()
    with session_scope() as s:
        week = svc.current_week(s)
        if not week:
            return render_template("empty.html")
        wid = request.args.get("week_id", type=int) or week.id
        # an L2 sees their own cases only, whatever the URL asks for
        assignee = request.args.get("assignee", "") if me.can_see_all else me.display_name
        rows = svc.queue_rows(
            s, wid, queue=request.args.get("queue", ""), assignee=assignee,
            state=request.args.get("state", ""),
            pending_only=bool(request.args.get("pending")),
            search=request.args.get("search", ""))
        items = []
        for cw, case in rows:
            rev = cw.current_review
            items.append({
                "cw_id": cw.id, "case_id": case.id,
                "lightning": case.lightning_id or case.case_key[:12],
                "sf_url": svc.salesforce_url(case.lightning_id),
                "summary": case.problem_summary, "severity": case.severity,
                "status": cw.status, "age": cw.age_days, "tse": case.tse_name,
                "customer": case.customer, "skill": case.skill_final,
                "suggested": case.skill_suggested, "evidence": case.match_evidence,
                "assigned": cw.assigned_to, "state": cw.review_state,
                "reason": cw.review_reason, "needs": cw.needs_review,
                "bucket": rev.bucket if rev else "", "call": rev.call_made if rev else "",
                "comment": rev.comment if rev else "",
                "prev_author": rev.author if rev else "",
                "carried": bool(rev and rev.kind == "carried"),
            })
        all_weeks = [{"id": w.id, "label": w.label} for w in
                     s.scalars(select(Week).order_by(Week.week_ending.desc())).all()]
        people = [r[0] for r in s.execute(
            select(User.display_name).where(User.active.is_(True))
            .order_by(User.display_name)).all()]
    return render_template("queue.html", items=items, weeks=all_weeks, week_id=wid,
                           people=people, f=request.args, buckets=s_cfg["buckets"],
                           queues=s_cfg["queues"])


@app.post("/api/review")
@login_required
def api_review():
    d = request.get_json(force=True)
    me = current_user()
    with session_scope() as s:
        cw = s.get(CaseWeek, int(d["cw_id"]))
        if not cw:
            return jsonify({"ok": False, "error": "unknown case"}), 404
        if not me.can_see_all and cw.assigned_to != me.display_name:
            return jsonify({"ok": False, "error": "not your case"}), 403
        bucket = d.get("bucket", "")
        if bucket and bucket != "NAC" and not d.get("comment", "").strip():
            return jsonify({"ok": False, "error": "comment required"}), 400
        svc.save_review(s, cw.id, bucket, d.get("call_made", ""),
                        d.get("comment", ""), me.display_name)
    return jsonify({"ok": True})


@app.post("/api/skill")
@login_required
def api_skill():
    if not current_user().can_see_all:
        return jsonify({"ok": False, "error": "only a manager can move cases between queues"}), 403
    d = request.get_json(force=True)
    by = current_user().display_name
    with session_scope() as s:
        if not d.get("cw_id"):
            svc.change_skill(s, int(d["case_id"]), d["skill"], by)
            return jsonify({"ok": True})
        # a reviewer who does not work the new queue hands the case on
        _target, owner = svc.reassign(s, int(d["cw_id"]), d["skill"], by,
                                      keep_member=True)
        if owner:
            return jsonify({"ok": True, "assigned": owner})
        kept = s.get(CaseWeek, int(d["cw_id"])).assigned_to
        return jsonify({"ok": True, "assigned": kept,
                        "warning": f"Nobody works {d['skill']} yet, so it stays with "
                                   f"{kept or 'nobody'}. Add someone to that queue."})


@app.post("/api/bulk")
@login_required
def api_bulk():
    """Move several cases to a queue in one action. Each move is recorded as a
    correction, so a bulk fix feeds the training set exactly like a single one."""
    d = request.get_json(force=True)
    ids = [int(i) for i in d.get("cw_ids", [])][:500]
    skill = d.get("skill", "")
    owner = d.get("owner", "")
    if not ids or not (skill or owner):
        return jsonify({"ok": False, "error": "nothing to do"}), 400
    me = current_user()
    if not me.can_see_all:
        return jsonify({"ok": False, "error": "only a manager can move or reassign cases"}), 403
    moved = []
    with session_scope() as s:
        for cw_id in ids:
            target, who = svc.reassign(s, cw_id, skill, me.display_name, owner)
            if target:
                moved.append(who)
    return jsonify({"ok": True, "count": len(moved),
                    "owners": sorted({m for m in moved if m})})


@app.get("/triage")
@roles_required("admin", "manager")
def triage():
    cfg = load_settings()
    max_score = request.args.get("score", type=int) or 0
    with session_scope() as s:
        week = svc.current_week(s)
        if not week:
            return render_template("empty.html")
        wid = request.args.get("week_id", type=int) or week.id
        rows = svc.unrouted(s, wid, max_score)
        items = [{
            "cw_id": cw.id, "case_id": case.id,
            "lightning": case.lightning_id or case.case_key[:12],
            "sf_url": svc.salesforce_url(case.lightning_id),
            "summary": case.problem_summary, "severity": case.severity,
            "status": cw.status, "age": cw.age_days, "tse": case.tse_name,
            "customer": case.customer, "product": case.product,
            "skill": case.skill_final, "score": case.match_score,
            "evidence": case.match_evidence, "assigned": cw.assigned_to,
        } for cw, case in rows]
        weeks = [{"id": w.id, "label": w.label} for w in
                 s.scalars(select(Week).order_by(Week.week_ending.desc())).all()]
    targets = [q for q in cfg["queues"] if q != cfg.get("fallback_queue", "General")]
    return render_template("triage.html", items=items, weeks=weeks, week_id=wid,
                           targets=targets, score=max_score)


@app.get("/case/<int:case_id>")
@login_required
def case_history(case_id):
    me = current_user()
    with session_scope() as s:
        case = s.get(Case, case_id)
        mine = me.can_see_all or s.scalar(
            select(func.count()).select_from(CaseWeek)
            .where(CaseWeek.case_id == case_id, CaseWeek.assigned_to == me.display_name))
        if not case or not mine:
            flash("No such case.")
            return redirect(url_for("my_queue"))
        rows = s.execute(
            select(Week.label, CaseWeek.age_days, CaseWeek.status, CaseWeek.severity,
                   CaseWeek.review_state, CaseWeek.review_reason, CaseWeek.assigned_to,
                   CaseWeek.id)
            .join(CaseWeek, CaseWeek.week_id == Week.id)
            .where(CaseWeek.case_id == case_id).order_by(Week.week_ending.desc())).all()
        history = []
        for label, age, status, sev, state, reason, who, cwid in rows:
            revs = s.scalars(select(Review).where(Review.case_week_id == cwid)
                             .order_by(Review.created_at.desc())).all()
            history.append({"week": label, "age": age, "status": status, "severity": sev,
                            "state": state, "reason": reason, "who": who,
                            "reviews": [{"bucket": r.bucket, "comment": r.comment,
                                         "author": r.author, "kind": r.kind,
                                         "at": r.created_at} for r in revs]})
        info = {"lightning": case.lightning_id or case.case_key,
                "sf_url": svc.salesforce_url(case.lightning_id),
                "summary": case.problem_summary,
                "tse": case.tse_name, "customer": case.customer, "skill": case.skill_final,
                "suggested": case.skill_suggested, "source": case.skill_source,
                "evidence": case.match_evidence}
    return render_template("case.html", c=info, history=history)


@app.get("/case/by-number/<number>")
@login_required
def case_by_number(number):
    """Stable address for a case from outside the platform (EWS links here), since
    /case/<id> uses our internal id. Access is still decided by case_history."""
    with session_scope() as s:
        case_id = s.scalar(select(Case.id).where(Case.lightning_id == number.strip()))
    if not case_id:
        flash("No such case.")
        return redirect(url_for("my_queue"))
    return redirect(url_for("case_history", case_id=case_id))


# ---------------------------------------------------------------- reports

@app.get("/tse")
@roles_required("admin", "manager")
def tse_report():
    cfg = load_settings()
    name = request.args.get("name", "").strip()
    period = request.args.get("period", "all")
    if period not in svc.PERIODS:
        period = "all"
    buckets = cfg["buckets"]
    with session_scope() as s:
        names = svc.tse_names(s)
        week_ids, period_label = svc.period_weeks(s, period)
        grid = (svc.bucket_pivot(s, buckets, Case.tse_name, tse_like=name, week_ids=week_ids)
                if name else [])
        trend = svc.tse_trend(s, name, buckets, week_ids) if name else []
        comments = [dict(zip(
            ["lightning", "summary", "skill", "week", "bucket", "comment", "author",
             "call", "age"], row))
            for row in svc.tse_comments(s, name, week_ids=week_ids)] if name else []
    totals = {b: sum(g[b] for g in grid) for b in buckets}
    totals["Total"] = sum(g["Total"] for g in grid)
    totals["Pending"] = sum(g["Pending"] for g in grid)
    return render_template("tse.html", name=name, names=names, grid=grid, trend=trend,
                           comments=comments, totals=totals, buckets=buckets,
                           period=period, periods=svc.PERIODS, period_label=period_label)


@app.get("/dashboard")
@roles_required("admin", "manager")
def dashboard():
    cfg = load_settings()
    buckets = cfg["buckets"]
    with session_scope() as s:
        week = svc.current_week(s)
        wid = request.args.get("week_id", type=int) or (week.id if week else None)
        by_person = svc.bucket_pivot(s, buckets, CaseWeek.assigned_to, week_id=wid)
        by_queue = svc.bucket_pivot(s, buckets, Case.skill_final, week_id=wid)
        weeks = [{"id": w.id, "label": w.label} for w in
                 s.scalars(select(Week).order_by(Week.week_ending.desc())).all()]
        states = dict(s.execute(
            select(CaseWeek.review_state, func.count()).where(CaseWeek.week_id == wid)
            .group_by(CaseWeek.review_state)).all()) if wid else {}
        weekly = []
        for w in s.scalars(select(Week).order_by(Week.week_ending)).all():
            row = svc.bucket_pivot(s, buckets, Week.label, week_id=w.id)
            weekly.append(row[0] if row else {"name": w.label, "Total": 0, "Pending": 0,
                                              **{b: 0 for b in buckets}})
        train = svc.training_stats(s)
    return render_template("dashboard.html", by_person=by_person, by_queue=by_queue,
                           weeks=weeks, week_id=wid, states=states, weekly=weekly,
                           buckets=buckets, train=train)


# ---------------------------------------------------------------- admin

@app.route("/settings", methods=["GET", "POST"])
@roles_required("admin")
def settings_page():
    cfg = load_settings()
    if request.method == "POST":
        for key, cast in (("weekly_cap_per_reviewer", int), ("recheck_after_weeks", int)):
            try:
                cfg[key] = cast(request.form.get(key, cfg[key]))
            except ValueError:
                pass
        b = [x.strip() for x in request.form.get("buckets", "").split(",") if x.strip()]
        if b:
            cfg["buckets"] = b
        bands = [int(x) for x in request.form.get("age_bands", "").replace(" ", "").split(",")
                 if x.isdigit()]
        if bands:
            cfg["age_bands"] = sorted(bands)
        for q in cfg["queues"]:
            cfg["keywords"][q] = [w.strip().lower() for w in
                                  request.form.get(f"kw_{q}", "").split(",") if w.strip()]
        save_settings(cfg)
        flash("Saved. Applies to the next upload.")
        return redirect(url_for("settings_page"))
    return render_template("settings.html", c=cfg)


@app.route("/users", methods=["GET", "POST"])
@roles_required("admin")
def users_page():
    cfg = load_settings()
    if request.method == "POST":
        action = request.form.get("action")
        by = current_user().display_name
        with session_scope() as s:
            if action == "add":
                name = request.form.get("display_name", "").strip()
                uname = request.form.get("username", "").strip().lower()
                if name and uname:
                    u = User(username=uname, display_name=name,
                             password_hash=hash_password(request.form.get("password", "changeme")),
                             role=request.form.get("role", "l2"))
                    s.add(u)
                    s.flush()
                    picked = request.form.getlist("queues")
                    for q in picked:
                        s.add(QueueMembership(user_id=u.id, queue=q))
                    note = svc.rebalance_note(svc.rebalance(s, by, picked)) if picked else ""
                    flash(f"Added {name}. {note}".strip())
            elif action == "queues":
                uid = int(request.form["user_id"])
                u = s.get(User, uid)
                before = set(u.queues())
                s.execute(QueueMembership.__table__.delete()
                          .where(QueueMembership.user_id == uid))
                picked = request.form.getlist("queues")
                for q in picked:
                    s.add(QueueMembership(user_id=uid, queue=q))
                s.expire(u, ["memberships"])
                changed = before ^ set(picked)
                note = svc.rebalance_note(svc.rebalance(s, by, changed)) if changed else ""
                flash(f"Queues updated. {note}".strip())
            elif action == "toggle":
                u = s.get(User, int(request.form["user_id"]))
                u.active = not u.active
                if u.queues():
                    note = svc.rebalance_note(svc.rebalance(s, by, u.queues()))
                    flash(f"{u.display_name} {'enabled' if u.active else 'disabled'}. "
                          f"{note}".strip())
            elif action == "reset":
                u = s.get(User, int(request.form["user_id"]))
                u.password_hash = hash_password("changeme")
                u.must_change_password = True
                flash(f"{u.display_name} reset to 'changeme'.")
            elif action == "delete":
                ok, note = svc.delete_user(s, int(request.form["user_id"]), current_user())
                flash(note)
        return redirect(url_for("users_page"))

    with session_scope() as s:
        users = []
        for u in s.scalars(select(User).order_by(User.display_name)).all():
            act = svc.user_activity(s, u)
            users.append({"id": u.id, "username": u.username, "name": u.display_name,
                          "role": u.role, "active": u.active, "queues": u.queues(),
                          "last_login": u.last_login, **act})
    return render_template("users.html", users=users, queues=cfg["queues"])


@app.get("/healthz")
def healthz():
    """Liveness probe for systemd and nginx. No auth - it leaks nothing."""
    try:
        with session_scope() as s:
            s.execute(select(func.count()).select_from(Week))
        return jsonify({"ok": True}), 200
    except Exception:                               # noqa: BLE001
        app.logger.exception("health check failed")
        return jsonify({"ok": False}), 503


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
