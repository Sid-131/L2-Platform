"""
Smoke and behaviour tests. Run with:  python -m pytest tests -q

The carry-over tests are the important ones - that logic decides how much work the
team does each week, so a regression there is expensive and silent.
"""

import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile
TEST_DB = os.path.join(tempfile.gettempdir(), "l2_test.db")
os.environ["L2_DB"] = TEST_DB
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

import services as svc                                   # noqa: E402
from auth import hash_password                           # noqa: E402
from models import (Case, CaseWeek, QueueMembership, Review, SkillChange,  # noqa: E402
                    User, Week)
from store import init_db, load_settings, session_scope  # noqa: E402
from webapp import app                                   # noqa: E402

FIXTURES = os.path.join(tempfile.gettempdir(), "l2_test_fixtures")
os.makedirs(FIXTURES, exist_ok=True)


def _write_export(name, rows, with_ids=True):
    """A small synthetic weekly export. Without ids the case key falls back to a
    hash of the case's details, as it does for exports with the Lightning column
    stripped."""
    import csv
    path = os.path.join(FIXTURES, name)
    head = (["Case Number"] if with_ids else []) + [
        "Case Owner", "Customer", "Severity", "Problem Summary", "Status", "Age", "Created"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(head)
        for r in rows:
            w.writerow(r if with_ids else r[1:])
    return path


_SUMMARIES = ["SMB share access denied", "NFS mount hangs", "node panic after drive smartfail",
              "SyncIQ policy failing", "SmartConnect zone not balancing", "NDMP backup job fails",
              "license expired warning", "Product problem"]
# 30 rows, no case numbers; the last two repeat earlier rows exactly, so they merge
_ROWS = [[f"7{i:06d}", f"Owner {i % 6}", f"Customer {i % 9}", f"{1 + i % 4} - Sev",
          f"{_SUMMARIES[i % len(_SUMMARIES)]} #{i}", "Open", str(3 + i), f"2026-07-{1 + i % 27:02d}"]
         for i in range(28)]
_ROWS += [list(_ROWS[0]), list(_ROWS[1])]
JUL = _write_export("week_a.csv", _ROWS, with_ids=False)
SEP = _write_export("week_b.csv", [[f"8{i:06d}", f"Owner {i % 5}", f"Customer {i}", "2 - High",
                                    f"{_SUMMARIES[i % len(_SUMMARIES)]} new #{i}", "Open",
                                    str(i), "2026-09-01"] for i in range(20)])
S = load_settings()


@pytest.fixture(scope="module", autouse=True)
def setup():
    init_db()
    import seed
    seed.run()
    # dedicated accounts with fixed passwords so web tests do not depend on order
    with session_scope() as s:
        for uname, name, role in (("t.admin", "Test Admin", "admin"),
                                  ("t.l2", "Vikram", "l2")):
            if not s.query(User).filter(User.username == uname).first():
                s.add(User(username=uname, display_name=name,
                           password_hash=hash_password("test-password-1"),
                           role=role, must_change_password=False))
    yield


# ---------------------------------------------------------------- carry-over

def test_first_appearance_needs_review():
    state, _, needs = svc.carry_over_decision(None, None, "High", "Open", 3, 0, S)
    assert state == "new" and needs


def test_unchanged_case_is_carried():
    cw = CaseWeek(severity="High", status="Open", age_days=10)
    rev = Review(bucket="NAC", comment="")
    state, _, needs = svc.carry_over_decision(cw, rev, "High", "Open", 12, 1, S)
    assert state == "carried" and not needs


def test_severity_change_reopens():
    cw = CaseWeek(severity="Medium", status="Open", age_days=10)
    rev = Review(bucket="NAC")
    state, reason, needs = svc.carry_over_decision(cw, rev, "Critical", "Open", 12, 1, S)
    assert state == "changed" and needs and "severity" in reason


def test_crossing_an_age_band_reopens():
    cw = CaseWeek(severity="High", status="Open", age_days=12)      # band: >7
    rev = Review(bucket="NAC")
    state, reason, needs = svc.carry_over_decision(cw, rev, "High", "Open", 16, 1, S)
    assert state == "changed" and needs and "aged past" in reason


def test_periodic_recheck():
    cw = CaseWeek(severity="High", status="Open", age_days=100)
    rev = Review(bucket="NAC")
    state, _, needs = svc.carry_over_decision(cw, rev, "High", "Open", 100, 5, S)
    assert state == "periodic" and needs


def test_never_reviewed_still_needs_review():
    cw = CaseWeek(severity="High", status="Open", age_days=10)
    state, _, needs = svc.carry_over_decision(cw, None, "High", "Open", 10, 1, S)
    assert needs


# ---------------------------------------------------------------- assignment

def test_continuity_beats_least_loaded():
    a = svc.Assigner({"Net": ["Chloe", "Nikhil"]}, 30)
    a.load["Chloe"] = 5
    who, why = a.assign("Net", previous="Chloe")
    assert who == "Chloe" and why == "continuity"


def test_weekly_cap_pushes_to_the_other_reviewer():
    a = svc.Assigner({"Net": ["Chloe", "Nikhil"]}, 2)
    a.load["Chloe"] = 2
    who, why = a.assign("Net", previous="Chloe")
    assert who == "Nikhil" and why == "least-loaded"


def test_cap_is_per_person_across_queues():
    """Nikhil sits on Net and Win. Her cap is 1 in total, not 1 in each - so once
    Net has used it, Win must go to Jordan even though Win has not used its own."""
    a = svc.Assigner({"Net": ["Nikhil"], "Win": ["Nikhil", "Jordan"]}, 1)
    assert a.assign("Net")[0] == "Nikhil"
    who, _ = a.assign("Win", previous="Nikhil")
    assert who == "Jordan"


# ---------------------------------------------------------------- ingest

def test_ingest_two_weeks_and_carry_over():
    w1 = date(2026, 7, 24)
    with session_scope() as s:
        r1 = svc.ingest_week(s, JUL, "week-a.csv", w1, "tester")
    assert r1["rows"] == 30
    assert r1["new"] == 28                        # first week: every distinct case is new

    with session_scope() as s:
        r2 = svc.ingest_week(s, JUL, "week-a-again.csv", w1 + timedelta(days=7), "tester")
    # Same data a week on. Anything already reviewed and unchanged is carried forward;
    # rows that were never reviewed still need one.
    assert r2.get("new_cases", 0) == 0            # no duplicate Case rows
    # Every distinct case is accounted for: carried forward, or back on a list.
    accounted = sum(r2.get(k, 0) for k in ("new", "changed", "periodic", "carried"))
    assert accounted == r2["rows"] - r2.get("merged_rows", 0)
    assert r2.get("merged_rows", 0) == 2
    # The bulk was carried; only the never-reviewed rows came back.
    assert r2.get("new", 0) == 28                 # nothing was reviewed in week one

    with session_scope() as s:
        n_cases = s.query(Case).count()
        n_weeks = s.query(Week).count()
    # The export has no case-number column, so rows with an identical summary, owner,
    # customer and open date are indistinguishable and collapse into one case. The
    # ingest reports that rather than hiding it.
    assert n_weeks == 2
    assert n_cases == 28
    assert r1.get("blank_case_ids", 0) == 30       # every row lacked a case number
    assert r1.get("merged_rows", 0) == 30 - n_cases


def test_real_case_ids_do_not_merge():
    """With a Lightning number present the key is authoritative and nothing merges."""
    assert svc.case_key("226351167", "x", "y") == "226351167"
    assert svc.case_key("", "same", "owner") == svc.case_key("", "same", "owner")
    assert svc.case_key("", "a", "owner") != svc.case_key("", "b", "owner")


def test_second_export_adds_new_cases():
    with session_scope() as s:
        before = s.query(Case).count()
        res = svc.ingest_week(s, SEP, "week-b.csv", date(2026, 9, 11), "tester")
        after = s.query(Case).count()
    assert res["rows"] == 20
    assert after > before


def test_review_is_append_only():
    with session_scope() as s:
        cw = s.query(CaseWeek).filter(CaseWeek.needs_review.is_(True)).first()
        svc.save_review(s, cw.id, "Behaviour", "Yes", "first take", "Vikram")
        svc.save_review(s, cw.id, "Knowledge gap", "Yes", "changed my mind", "Vikram")
        revs = s.query(Review).filter(Review.case_week_id == cw.id).all()
    assert len(revs) >= 2                          # history kept, not overwritten


def test_skill_correction_keeps_the_suggestion():
    with session_scope() as s:
        case = s.query(Case).filter(Case.skill_suggested != "").first()
        original = case.skill_suggested
        target = "Bu" if case.skill_final != "Bu" else "Hw"
        svc.change_skill(s, case.id, target, "Vikram")
        s.flush()
        again = s.get(Case, case.id)
        assert again.skill_final == target
        assert again.skill_suggested == original   # the label survives the correction
        assert again.skill_source == "human"


# ---------------------------------------------------------------- web

@pytest.fixture
def client():
    app.config["TESTING"] = True
    return app.test_client()


def login(c, username, password="test-password-1"):
    return c.post("/login", data={"username": username, "password": password},
                  follow_redirects=True)


def test_anonymous_is_redirected(client):
    assert client.get("/", follow_redirects=False).status_code == 302


def test_first_login_forces_a_password_change(client):
    r = login(client, "samuel", password="changeme")
    assert b"Set a new password" in r.data


def test_l2_cannot_reach_manager_pages(client):
    login(client, "t.l2")
    assert client.get("/tse", follow_redirects=False).status_code == 302
    assert client.get("/dashboard", follow_redirects=False).status_code == 302
    assert client.get("/users", follow_redirects=False).status_code == 302
    assert client.get("/", follow_redirects=False).status_code == 200


def test_admin_reaches_every_page(client):
    login(client, "t.admin")
    for path in ("/", "/tse", "/dashboard", "/upload", "/settings", "/users"):
        assert client.get(path).status_code == 200, path


def test_comment_required_for_non_nac(client):
    login(client, "t.admin")
    with session_scope() as s:
        cw = s.query(CaseWeek).first()
    r = client.post("/api/review", json={"cw_id": cw.id, "bucket": "Behaviour",
                                         "call_made": "No", "comment": "  "})
    assert r.status_code == 400
    r2 = client.post("/api/review", json={"cw_id": cw.id, "bucket": "NAC",
                                          "call_made": "No", "comment": ""})
    assert r2.status_code == 200                   # NAC alone needs no comment


def test_tse_report_has_a_trend(client):
    login(client, "t.admin")
    with session_scope() as s:
        name = svc.tse_names(s)[0]
    r = client.get(f"/tse?name={name.split()[0]}")
    assert r.status_code == 200 and b"Week by week" in r.data


# ---------------------------------------------------------------- bulk + triage

def test_reassign_moves_queue_and_picks_least_loaded():
    with session_scope() as s:
        week = svc.current_week(s)
        row = s.query(CaseWeek).filter(CaseWeek.week_id == week.id).first()
        case = s.get(Case, row.case_id)
        original_suggestion = case.skill_suggested
        target = "Bu" if case.skill_final != "Bu" else "Hw"

        skill, owner = svc.reassign(s, row.id, target, "Test Admin")
        s.flush()
        moved = s.get(CaseWeek, row.id)
        again = s.get(Case, row.id and moved.case_id)

    assert skill == target
    assert owner, "a move must land on someone who works that queue"
    assert again.skill_final == target
    assert again.skill_suggested == original_suggestion   # the label survives
    assert moved.assigned_to == owner


def test_reassign_records_the_correction_as_training_data():
    with session_scope() as s:
        before = s.query(SkillChange).count()
        week = svc.current_week(s)
        row = (s.query(CaseWeek).filter(CaseWeek.week_id == week.id)
               .order_by(CaseWeek.id.desc()).first())
        case = s.get(Case, row.case_id)
        target = "Nix" if case.skill_final != "Nix" else "Win"
        svc.reassign(s, row.id, target, "Test Admin")
        s.flush()
        after = s.query(SkillChange).count()
    assert after == before + 1


def test_unrouted_finds_only_the_fallback_queue():
    with session_scope() as s:
        week = svc.current_week(s)
        rows = svc.unrouted(s, week.id)
        skills = {case.skill_final for _cw, case in rows}
    assert skills <= {"General"}


def test_unrouted_widens_with_a_score_threshold():
    with session_scope() as s:
        week = svc.current_week(s)
        strict = len(svc.unrouted(s, week.id, max_score=0))
        loose = len(svc.unrouted(s, week.id, max_score=6))
    assert loose >= strict


def test_bulk_move_endpoint(client):
    login(client, "t.admin")
    with session_scope() as s:
        week = svc.current_week(s)
        ids = [cw.id for cw in s.query(CaseWeek)
               .filter(CaseWeek.week_id == week.id).limit(3).all()]
    r = client.post("/api/bulk", json={"cw_ids": ids, "skill": "Hw"})
    assert r.status_code == 200 and r.get_json()["count"] == 3
    with session_scope() as s:
        for cw_id in ids:
            cw = s.get(CaseWeek, cw_id)
            assert s.get(Case, cw.case_id).skill_final == "Hw"
            assert cw.assigned_to in ("Ishaan", "Zoe")


def test_bulk_requires_something_to_do(client):
    login(client, "t.admin")
    assert client.post("/api/bulk", json={"cw_ids": [], "skill": "Hw"}).status_code == 400
    with session_scope() as s:
        one = s.query(CaseWeek).first().id
    assert client.post("/api/bulk", json={"cw_ids": [one]}).status_code == 400


def test_l2_cannot_move_cases_between_queues(client):
    login(client, "t.l2")
    with session_scope() as s:
        one = s.query(CaseWeek).first().id
    assert client.post("/api/bulk", json={"cw_ids": [one], "skill": "Hw"}).status_code == 403


def test_triage_page_is_manager_only(client):
    login(client, "t.l2")
    assert client.get("/triage", follow_redirects=False).status_code == 302
    login(client, "t.admin")
    r = client.get("/triage")
    assert r.status_code == 200 and b"Triage" in r.data


# ---------------------------------------------------------------- user deletion

def test_delete_user_with_no_work(client):
    with session_scope() as s:
        s.add(User(username="t.temp", display_name="Temp Person",
                   password_hash=hash_password("x"), role="l2",
                   must_change_password=False))
        s.flush()
        uid = s.query(User).filter(User.username == "t.temp").first().id
        admin = s.query(User).filter(User.username == "t.admin").first()
        ok, note = svc.delete_user(s, uid, admin)
    assert ok and "Deleted Temp Person" in note
    with session_scope() as s:
        assert s.query(User).filter(User.username == "t.temp").first() is None


def _team_week(queue, week_ending, members, cases_per_holder):
    """A synthetic week in a queue nobody real works: `members` sit on the queue and
    `cases_per_holder` says who holds how many open cases. Returns the week id."""
    with session_scope() as s:
        ids = {}
        for name in set(members) | set(cases_per_holder):
            uname = "t." + name.lower().replace(" ", "")
            u = s.query(User).filter(User.username == uname).first()
            if not u:
                u = User(username=uname, display_name=name, password_hash="x",
                         role="l2", must_change_password=False)
                s.add(u)
                s.flush()
            ids[name] = u.id
        for name in members:
            s.add(QueueMembership(user_id=ids[name], queue=queue))
        week = Week(week_ending=week_ending, label=str(week_ending))
        s.add(week)
        s.flush()
        n = 0
        for holder, count in cases_per_holder.items():
            for _ in range(count):
                n += 1
                case = Case(case_key=f"{queue}-{week_ending}-{n}", skill_final=queue)
                s.add(case)
                s.flush()
                s.add(CaseWeek(case_id=case.id, week_id=week.id, assigned_to=holder,
                               needs_review=True, review_order=n))
        return week.id


def _holdings(week_id):
    with session_scope() as s:
        rows = s.query(CaseWeek).filter(CaseWeek.week_id == week_id).all()
        out = {}
        for cw in rows:
            out[cw.assigned_to] = out.get(cw.assigned_to, 0) + 1
        return out


def test_adding_someone_to_a_queue_shares_the_work():
    wid = _team_week("TQ1", date(2018, 1, 5), ["Rb A"], {"Rb A": 6})
    with session_scope() as s:
        b = s.query(User).filter(User.username == "t.rbb").first()
        if not b:
            b = User(username="t.rbb", display_name="Rb B", password_hash="x", role="l2")
            s.add(b)
            s.flush()
        s.add(QueueMembership(user_id=b.id, queue="TQ1"))
        res = svc.rebalance(s, "Test Admin", {"TQ1"}, wid)
    assert res["moved"] == 3
    assert _holdings(wid) == {"Rb A": 3, "Rb B": 3}


def test_removing_someone_hands_their_cases_on():
    wid = _team_week("TQ2", date(2018, 1, 12), ["Rc A", "Rc B"], {"Rc A": 4, "Rc B": 1})
    with session_scope() as s:
        a = s.query(User).filter(User.username == "t.rca").first()
        s.query(QueueMembership).filter(QueueMembership.user_id == a.id,
                                        QueueMembership.queue == "TQ2").delete()
        svc.rebalance(s, "Test Admin", {"TQ2"}, wid)
    assert _holdings(wid) == {"Rc B": 5}


def test_rebalance_leaves_started_and_hand_placed_cases():
    wid = _team_week("TQ3", date(2018, 1, 19), ["Rd A", "Rd B"], {"Rd A": 4})
    with session_scope() as s:
        cws = (s.query(CaseWeek).filter(CaseWeek.week_id == wid)
               .order_by(CaseWeek.id).all())
        svc.save_review(s, cws[0].id, "", "", "halfway through", "Rd A")
        svc.reassign(s, cws[1].id, "", "Test Admin", owner="Rd A")
        svc.reassign(s, cws[2].id, "", "Test Admin", owner="Rd A")
        svc.rebalance(s, "Test Admin", {"TQ3"}, wid)
        s.flush()
        kept = {cw.id for cw in s.query(CaseWeek).filter(CaseWeek.week_id == wid,
                                                         CaseWeek.assigned_to == "Rd A")}
    # 4 vs 0: only the one untouched case may move, which makes it 3 vs 1
    assert _holdings(wid) == {"Rd A": 3, "Rd B": 1}
    assert {cws[0].id, cws[1].id, cws[2].id} <= kept


def test_a_queue_nobody_works_is_reported_not_emptied():
    wid = _team_week("TQ4", date(2018, 1, 26), [], {"Re A": 2})
    with session_scope() as s:
        res = svc.rebalance(s, "Test Admin", {"TQ4"}, wid)
    assert res == {"moved": 0, "stranded": {"TQ4": 2}}
    assert _holdings(wid) == {"Re A": 2}


def test_deleting_someone_hands_over_their_open_work():
    wid = _team_week("TQ5", date(2018, 2, 2), ["Rf A", "Rf B"], {"Rf A": 3})
    with session_scope() as s:
        u = s.query(User).filter(User.username == "t.rfa").first()
        admin = s.query(User).filter(User.username == "t.admin").first()
        ok, note = svc.delete_user(s, u.id, admin)
    assert ok and "3 open case(s) reassigned" in note
    assert _holdings(wid) == {"Rf B": 3}


def test_cannot_delete_the_only_person_on_a_queue_with_open_work():
    _team_week("TQ6", date(2018, 2, 9), ["Rg A"], {"Rg A": 2})
    with session_scope() as s:
        u = s.query(User).filter(User.username == "t.rga").first()
        admin = s.query(User).filter(User.username == "t.admin").first()
        ok, note = svc.delete_user(s, u.id, admin)
    assert not ok and "nobody else works TQ6" in note


def test_changing_queue_hands_the_case_to_someone_who_works_it(client):
    wid = _team_week("TQ8", date(2018, 3, 2), ["Ri A"], {"Ri A": 2})
    _team_week("TQ9", date(2018, 3, 9), ["Ri B"], {})
    with session_scope() as s:
        a = s.query(User).filter(User.username == "t.ria").one()
        s.add(QueueMembership(user_id=a.id, queue="TQ10"))
        first, second = (s.query(CaseWeek).filter(CaseWeek.week_id == wid)
                         .order_by(CaseWeek.id).all())
    login(client, "t.admin")
    # Ri A does not work TQ9: the case goes to Ri B
    r = client.post("/api/skill", json={"case_id": first.case_id, "cw_id": first.id,
                                        "skill": "TQ9"}).get_json()
    assert r["ok"] and r["assigned"] == "Ri B" and "warning" not in r
    # Ri A does work TQ10: they keep it
    r = client.post("/api/skill", json={"case_id": second.case_id, "cw_id": second.id,
                                        "skill": "TQ10"}).get_json()
    assert r["assigned"] == "Ri A"
    # nobody works TQ11: reviewer stays, with a warning
    r = client.post("/api/skill", json={"case_id": second.case_id, "cw_id": second.id,
                                        "skill": "TQ11"}).get_json()
    assert r["assigned"] == "Ri A" and "Nobody works TQ11" in r["warning"]
    with session_scope() as s:
        assert s.get(CaseWeek, first.id).assigned_to == "Ri B"
        assert s.get(Case, second.case_id).skill_final == "TQ11"


def test_report_periods_count_back_from_the_newest_week():
    end = date(2026, 9, 27)
    assert svc.period_start(end, "week") == end
    assert svc.period_start(end, "4w") == date(2026, 9, 6)     # four weekly exports
    assert svc.period_start(end, "month") == date(2026, 9, 1)
    assert svc.period_start(end, "all") is None


def test_tse_report_accepts_every_period(client):
    login(client, "t.admin")
    for p in ("week", "4w", "month", "all", "bogus"):
        r = client.get(f"/tse?name=zz-nobody&period={p}")
        assert r.status_code == 200, p
    assert b'value="month" selected' in client.get("/tse?name=x&period=month").data


def test_add_user_form_takes_queues(client):
    """The page rebalances the current week; here we only check the form wiring."""
    login(client, "t.admin")
    r = client.post("/users", data={"action": "add", "display_name": "Rh B",
                                    "username": "t.rhb", "password": "x", "role": "l2",
                                    "queues": ["TQ7"]}, follow_redirects=True)
    assert b"Added Rh B" in r.data
    with session_scope() as s:
        assert s.query(User).filter(User.username == "t.rhb").one().queues() == ["TQ7"]
    with session_scope() as s:
        uid = s.query(User).filter(User.username == "t.rhb").one().id
    r = client.post("/users", data={"action": "toggle", "user_id": str(uid)},
                    follow_redirects=True)
    assert b"Rh B disabled" in r.data


def test_cannot_delete_yourself():
    with session_scope() as s:
        admin = s.query(User).filter(User.username == "t.admin").first()
        ok, note = svc.delete_user(s, admin.id, admin)
    assert not ok and "your own account" in note


def test_reviews_survive_their_author_being_deleted():
    with session_scope() as s:
        s.add(User(username="t.leaver", display_name="Leaver",
                   password_hash=hash_password("x"), role="l2",
                   must_change_password=False))
        s.flush()
        u = s.query(User).filter(User.username == "t.leaver").first()
        cw = s.query(CaseWeek).first()
        svc.save_review(s, cw.id, "NAC", "No", "", "Leaver")
        s.flush()
        admin = s.query(User).filter(User.username == "t.admin").first()
        ok, note = svc.delete_user(s, u.id, admin)
        s.flush()
        kept = s.query(Review).filter(Review.author == "Leaver").count()
    assert ok and "keep their name" in note
    assert kept >= 1          # the audit trail is not rewritten by an account leaving


def test_queue_membership_is_multi_select(client):
    """A person can work several queues, and unticking removes only that one."""
    login(client, "t.admin")
    with session_scope() as s:
        u = s.query(User).filter(User.display_name == "Nikhil").first()
        uid = u.id
    client.post("/users", data={"action": "queues", "user_id": str(uid),
                                "queues": ["Net", "Win", "Hw"]}, follow_redirects=True)
    with session_scope() as s:
        assert sorted(s.get(User, uid).queues()) == ["Hw", "Net", "Win"]
    client.post("/users", data={"action": "queues", "user_id": str(uid),
                                "queues": ["Net"]}, follow_redirects=True)
    with session_scope() as s:
        assert sorted(s.get(User, uid).queues()) == ["Net"]


# ---------------------------------------------------------------- regressions
# Synthetic one-case exports dated 2019-2020, so they never need a real export and
# never become the "current week" other tests read.

def _ingest_synthetic(tmp_path, case_no, week_ending):
    p = tmp_path / f"{case_no}-{week_ending}.csv"
    p.write_text("Case Number,Case Owner,Customer,Severity,Status,Problem Summary,Age\n"
                 f"{case_no},Test Owner,Test Co,High,Open,"
                 "Server fails to boot after bios update,10\n")
    with session_scope() as s:
        svc.ingest_week(s, str(p), p.name, week_ending, "tester")
        return (s.query(CaseWeek).join(Case).join(Week)
                .filter(Case.lightning_id == case_no, Week.week_ending == week_ending).one())


def test_periodic_recheck_counts_from_the_last_real_review(tmp_path):
    """Carried copies are written every week; they must not reset the recheck clock."""
    d0 = date(2020, 1, 3)
    cw = _ingest_synthetic(tmp_path, "T-900001", d0)
    with session_scope() as s:
        svc.save_review(s, cw.id, "NAC", "No", "", "tester")
    weeks = S["recheck_after_weeks"]
    states = [_ingest_synthetic(tmp_path, "T-900001", d0 + timedelta(weeks=i)).review_state
              for i in range(1, weeks + 1)]
    assert states == ["carried"] * (weeks - 1) + ["periodic"]


def test_l2_cannot_change_a_cases_queue(client, tmp_path):
    cw = _ingest_synthetic(tmp_path, "T-900002", date(2019, 6, 7))
    with session_scope() as s:
        before = s.get(Case, cw.case_id).skill_final
    login(client, "t.l2")
    target = "Win" if before != "Win" else "Hw"
    assert client.post("/api/skill", json={"case_id": cw.case_id,
                                           "skill": target}).status_code == 403
    with session_scope() as s:
        assert s.get(Case, cw.case_id).skill_final == before


def test_l2_sees_only_their_own_cases(client, tmp_path):
    cw = _ingest_synthetic(tmp_path, "T-900003", date(2019, 6, 14))
    assert cw.assigned_to != "Vikram"
    url = f"/?week_id={cw.week_id}&assignee={cw.assigned_to}"
    login(client, "t.admin")
    assert b"T-900003" in client.get(url).data           # the case is really there
    login(client, "t.l2")
    assert b"T-900003" not in client.get(url).data
    assert client.get(f"/case/{cw.case_id}", follow_redirects=False).status_code == 302


def test_l2_cannot_reassign_cases(client, tmp_path):
    cw = _ingest_synthetic(tmp_path, "T-900004", date(2019, 6, 21))
    login(client, "t.l2")
    r = client.post("/api/bulk", json={"cw_ids": [cw.id], "owner": "Vikram"})
    assert r.status_code == 403


# ---------------------------------------------------------------- outside links

def test_case_number_links_to_salesforce_search():
    import base64
    import json
    url = svc.salesforce_url("231913628")
    base, blob = url.split("#", 1)
    assert base == "https://example.lightning.force.com/one/one.app"
    state = json.loads(base64.b64decode(blob))
    assert state["componentDef"] == "forceSearch:search"
    assert state["attributes"]["term"] == "231913628"
    # hash-keyed cases have no case number to search for
    assert svc.salesforce_url("H:0123456789abcd") == "" and svc.salesforce_url("") == ""


def test_case_by_number_opens_its_history(client, tmp_path):
    cw = _ingest_synthetic(tmp_path, "900000005", date(2019, 7, 5))
    login(client, "t.admin")
    r = client.get("/case/by-number/900000005", follow_redirects=False)
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/case/{cw.case_id}")
    assert b"one/one.app#" in client.get(f"/case/{cw.case_id}").data
    r = client.get("/case/by-number/000000000", follow_redirects=False)
    assert r.status_code == 302 and not r.headers["Location"].startswith("/case/")


def test_l2_cannot_open_someone_elses_case_by_number(client, tmp_path):
    cw = _ingest_synthetic(tmp_path, "900000006", date(2019, 7, 12))
    assert cw.assigned_to != "Vikram"
    login(client, "t.l2")
    r = client.get("/case/by-number/900000006", follow_redirects=True)
    assert b"No such case." in r.data and b"900000006" not in r.data


def test_sign_in_returns_to_the_linked_case(client, tmp_path):
    cw = _ingest_synthetic(tmp_path, "900000007", date(2019, 7, 19))
    client.get("/logout")
    assert client.get("/case/by-number/900000007").status_code == 302   # to login
    r = login(client, "t.admin")
    assert b"900000007" in r.data and b"Case history" in r.data
    assert cw.case_id


def test_sign_in_ignores_an_offsite_next():
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["next"] = "//evil.example/x"
        r = c.post("/login", data={"username": "t.admin", "password": "test-password-1"})
        assert r.status_code == 302 and "evil.example" not in r.headers["Location"]
