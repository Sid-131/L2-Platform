"""
Business logic: ingest a weekly export, decide what needs re-reviewing, assign it,
and answer the reporting questions.

The carry-over rule (architecture doc 3.1, option C) lives in
`carry_over_decision` and is the heart of the weekly model.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import func, select

from models import (Assignment, Case, CaseWeek, QueueMembership, Review,
                    SkillChange, User, Week)
from store import build_classifier, load_settings

# ---------------------------------------------------------------- reading

HEADER_PATTERNS = {
    "lightning_id": ["lightning", "case number", "case no", "case id", "case #", "ticket"],
    "tse_name": ["case owner", "owner name", "tse", "engineer", "assigned to"],
    "customer": ["customer", "account"],
    "country": ["country", "region"],
    "severity": ["severity", "priority"],
    "problem_summary": ["problem summary", "summary", "title", "subject"],
    "product": ["product", "model"],
    "status": ["status"],
    "age_days": ["age", "aging", "days open"],
    "created": ["created", "opened", "received", "reported"],
    "skill_in": ["skill"],
    "bucket_in": ["bucket"],
    "assigned_in": ["l2", "reviewer"],
    "call_in": ["call made"],
    "comment_in": ["comment"],
}
WS = re.compile(r"\s+")


def clean(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v)
    if s.lower() == "nan":
        return ""
    for bad in ("\\r", "\\n", "\r", "\n"):
        s = s.replace(bad, " ")
    return WS.sub(" ", s).strip()


def to_int(v) -> int:
    m = re.search(r"-?\d+", clean(v))
    return int(m.group()) if m else 0


def map_headers(headers: list[str]) -> dict:
    low = {h: re.sub(r"[^a-z0-9 ]+", " ", str(h).lower()).strip() for h in headers}
    taken, out = set(), {}
    for field, pats in HEADER_PATTERNS.items():
        best, best_len = None, -1
        for h in headers:
            if h in taken:
                continue
            # "owner" alone must not steal the L2 column from "case owner"
            if field == "assigned_in" and "case owner" in low[h]:
                continue
            if field == "tse_name" and low[h] == "owner":
                continue
            for p in pats:
                if p in low[h] and len(p) > best_len:
                    best, best_len = h, len(p)
        if best is not None:
            out[field] = best
            taken.add(best)
    return out


def read_export(path: str) -> pd.DataFrame:
    if os.path.splitext(path)[1].lower() == ".csv":
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    return pd.read_excel(path, dtype=str).fillna("")


def case_key(lightning_id: str, *parts: str) -> str:
    """The Lightning number when we have it. Without it we fall back to a hash of
    the case's distinguishing fields, which is stable across uploads but NOT
    authoritative: two genuinely different cases with the same summary, owner,
    customer and open date will merge into one. Exports that strip the Lightning
    column should be treated as approximate - see the warning raised on ingest."""
    lid = (lightning_id or "").strip()
    if lid:
        return lid
    blob = "|".join(p or "" for p in parts)
    return "H:" + hashlib.sha1(blob.encode("utf-8", "ignore")).hexdigest()[:14]


SF_LIGHTNING = os.environ.get("L2_SF_LIGHTNING", "https://example.lightning.force.com")


def salesforce_url(lightning_id: str) -> str:
    """Link that opens Salesforce global search for the case number.

    A direct record link needs the 18-character Case Id, which the weekly export
    does not carry, so this lands on search results one click from the case.
    Lightning's search state is an undocumented base64 blob: if Salesforce changes
    it, this is the one place to fix. Hash-keyed cases have no number to search."""
    lid = (lightning_id or "").strip()
    if not lid.isdigit():
        return ""
    state = {"componentDef": "forceSearch:search",
             "attributes": {"term": lid, "scopeMap": {"type": "TOPRESULTS"}}}
    blob = base64.b64encode(json.dumps(state, separators=(",", ":")).encode()).decode()
    return f"{SF_LIGHTNING}/one/one.app#{blob}"


def week_label(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


# ---------------------------------------------------------------- carry-over

def age_band(days: int, thresholds: list[int]) -> int:
    """Which aging bracket a case sits in. Crossing a bracket is a review trigger."""
    band = 0
    for t in sorted(thresholds):
        if days >= t:
            band += 1
    return band


def carry_over_decision(prev_cw: CaseWeek | None, prev_review: Review | None,
                        severity: str, status: str, age_days: int,
                        weeks_since_review: int, s: dict) -> tuple[str, str, bool]:
    """Option C from the architecture doc: carry the previous verdict forward and
    reopen only when something material moved.

    Returns (review_state, reason, needs_review).
    """
    if prev_cw is None:
        return "new", "First appearance in an export", True
    if prev_review is None or not prev_review.bucket:
        return "new", "Never reviewed", True

    triggers = []
    if severity and prev_cw.severity and severity != prev_cw.severity:
        triggers.append(f"severity {prev_cw.severity} to {severity}")
    if status and prev_cw.status and status != prev_cw.status:
        triggers.append(f"status {prev_cw.status} to {status}")

    bands = s.get("age_bands", [7, 14, 30, 60])
    if age_band(age_days, bands) > age_band(prev_cw.age_days, bands):
        triggers.append(f"aged past {max(t for t in sorted(bands) if age_days >= t)} days")
    if triggers:
        return "changed", "; ".join(triggers), True

    recheck = s.get("recheck_after_weeks", 4)
    if weeks_since_review >= recheck:
        return "periodic", f"No fresh look for {weeks_since_review} weeks", True

    return "carried", "Unchanged since last review", False


# ---------------------------------------------------------------- assignment

class Assigner:
    """Least-loaded within a queue, but continuity wins: the L2 who reviewed a case
    before gets it again if they still work that queue. The cap is per person per
    week counted across every queue they sit in."""

    def __init__(self, queues: dict[str, list[str]], weekly_cap: int):
        self.queues = queues
        self.cap = weekly_cap if weekly_cap and weekly_cap > 0 else 10 ** 9
        self.load = defaultdict(int)
        self.over_cap = 0

    def assign(self, queue: str, previous: str = "") -> tuple[str, str]:
        pool = self.queues.get(queue) or []
        if not pool:
            return "", "no reviewer in queue"
        if previous and previous in pool and self.load[previous] < self.cap:
            self.load[previous] += 1
            return previous, "continuity"
        eligible = [n for n in pool if self.load[n] < self.cap] or pool
        if eligible is pool:
            self.over_cap += 1
        low = min(self.load[n] for n in eligible)
        pick = next(n for n in eligible if self.load[n] == low)
        self.load[pick] += 1
        return pick, "least-loaded"


def queue_map(sess) -> dict[str, list[str]]:
    out = defaultdict(list)
    rows = sess.execute(
        select(QueueMembership.queue, User.display_name)
        .join(User, User.id == QueueMembership.user_id)
        .where(User.active.is_(True))
        .order_by(QueueMembership.queue, User.display_name)).all()
    for q, name in rows:
        out[q].append(name)
    return dict(out)


# ---------------------------------------------------------------- ingest

def ingest_week(sess, path: str, filename: str, week_ending: date,
                uploaded_by: str) -> dict:
    s = load_settings()
    clf = build_classifier(s)
    df = read_export(path)
    m = map_headers(list(df.columns))

    week = sess.scalar(select(Week).where(Week.week_ending == week_ending))
    if week is None:
        week = Week(week_ending=week_ending, label=week_label(week_ending))
        sess.add(week)
        sess.flush()
    week.source_file = filename
    week.uploaded_by = uploaded_by
    week.uploaded_at = datetime.now()

    assigner = Assigner(queue_map(sess), s.get("weekly_cap_per_reviewer", 30))
    stats = defaultdict(int)
    keys_seen: set[str] = set()

    for _, row in df.iterrows():
        g = lambda f: clean(row.get(m.get(f), ""))          # noqa: E731
        summary = g("problem_summary")
        if not summary and not g("lightning_id"):
            continue

        if not g("lightning_id"):
            stats["blank_case_ids"] += 1
        key = case_key(g("lightning_id"), summary, g("tse_name"), g("customer"),
                       g("severity"), g("created"))
        # two rows in one export resolving to one case is only possible when the
        # case number column is missing
        duplicate_row = key in keys_seen
        if duplicate_row:
            stats["merged_rows"] += 1
        keys_seen.add(key)
        severity, status = g("severity"), g("status")
        age_days = to_int(g("age_days"))

        case = sess.scalar(select(Case).where(Case.case_key == key))
        suggested, score, evidence = clf.classify(summary, g("product"))
        skill_in = g("skill_in")
        is_new_case = case is None

        if is_new_case:
            case = Case(case_key=key, lightning_id=g("lightning_id"), tse_name=g("tse_name"),
                        customer=g("customer"), country=g("country"), problem_summary=summary,
                        product=g("product"))
            # the guess and the answer are stored separately, on purpose
            case.skill_suggested = suggested
            case.match_score = score
            case.match_evidence = evidence
            if skill_in and skill_in in s["queues"]:
                case.skill_final, case.skill_source = skill_in, "human"
            else:
                case.skill_final, case.skill_source = suggested, clf.name
            sess.add(case)
            sess.flush()
            stats["new_cases"] += 1
        else:
            case.tse_name = g("tse_name") or case.tse_name
            case.customer = g("customer") or case.customer
            stats["known_cases"] += 1

        case.severity, case.status, case.age_days = severity, status, age_days
        case.last_seen = datetime.now()

        prev_cw = sess.scalars(
            select(CaseWeek).join(Week).where(CaseWeek.case_id == case.id,
                                              Week.week_ending < week_ending)
            .order_by(Week.week_ending.desc())).first()
        prev_review = prev_cw.current_review if prev_cw else None
        weeks_since = 0
        if prev_cw:
            # count from the last real verdict, not the last export: a carried copy is
            # written every week and would otherwise reset the clock forever
            last_verdict = sess.scalar(
                select(func.max(Week.week_ending)).select_from(Review)
                .join(CaseWeek, CaseWeek.id == Review.case_week_id)
                .join(Week, Week.id == CaseWeek.week_id)
                .where(CaseWeek.case_id == case.id, Review.kind != "carried",
                       Week.week_ending < week_ending))
            since = last_verdict or prev_cw.week.week_ending
            weeks_since = max(1, (week_ending - since).days // 7)

        state, reason, needs = carry_over_decision(
            prev_cw, prev_review, severity, status, age_days, weeks_since, s)

        cw = sess.scalar(select(CaseWeek).where(CaseWeek.case_id == case.id,
                                                CaseWeek.week_id == week.id))
        if cw is None:
            cw = CaseWeek(case_id=case.id, week_id=week.id)
            sess.add(cw)
            sess.flush()
        cw.severity, cw.status, cw.age_days = severity, status, age_days
        cw.review_state, cw.review_reason, cw.needs_review = state, reason, needs
        if not duplicate_row:          # a merged row must not be counted twice
            stats[state] += 1

        if needs:
            who, why = assigner.assign(case.skill_final,
                                       prev_cw.assigned_to if prev_cw else "")
            cw.assigned_to = who
            sess.add(Assignment(case_week_id=cw.id, assigned_to=who,
                                queue=case.skill_final, reason=why,
                                assigned_by=uploaded_by))
        else:
            cw.assigned_to = prev_cw.assigned_to if prev_cw else ""
            # carry the previous verdict forward so the week's numbers are complete
            if prev_review:
                sess.add(Review(case_week_id=cw.id, bucket=prev_review.bucket,
                                call_made=prev_review.call_made,
                                comment=prev_review.comment,
                                author=prev_review.author, kind="carried"))

        # a bucket already filled in the source file counts as a review
        bucket_in = g("bucket_in")
        if bucket_in and needs and not cw.reviews:
            sess.add(Review(case_week_id=cw.id, bucket=bucket_in,
                            call_made=g("call_in")[:3], comment=g("comment_in"),
                            author=g("assigned_in") or uploaded_by, kind="review"))

    week.rows_total = int(len(df))
    week.cases_new = stats["new_cases"]
    week.cases_carried = stats["carried"]
    return {"week": week.label, "rows": int(len(df)), **dict(stats),
            "over_cap": assigner.over_cap}


# ---------------------------------------------------------------- reviews

def save_review(sess, case_week_id: int, bucket: str, call_made: str,
                comment: str, author: str, kind: str = "review") -> None:
    sess.add(Review(case_week_id=case_week_id, bucket=bucket, call_made=call_made,
                    comment=comment, author=author, kind=kind))
    cw = sess.get(CaseWeek, case_week_id)
    if cw and bucket:
        cw.needs_review = False


def reassign(sess, case_week_id: int, new_skill: str, by: str, owner: str = "",
             keep_member: bool = False):
    """Move a case to a different queue and hand it to someone who works that queue.

    The owner defaults to whoever currently carries the fewest cases in the target
    queue this week, counted live rather than from a stale counter. With
    `keep_member`, a reviewer who already works the target queue keeps the case;
    only one who does not is replaced. If nobody works the queue the owner comes
    back empty and the current reviewer is left in place."""
    cw = sess.get(CaseWeek, case_week_id)
    if not cw:
        return None, None
    case = sess.get(Case, cw.case_id)
    target = new_skill or case.skill_final
    if new_skill and case.skill_final != new_skill:
        change_skill(sess, case.id, new_skill, by)

    reason = "manual"
    if not owner:
        pool = queue_map(sess).get(target) or []
        if keep_member and cw.assigned_to in pool:
            return target, cw.assigned_to
        if pool:
            counts = dict(sess.execute(
                select(CaseWeek.assigned_to, func.count())
                .where(CaseWeek.week_id == cw.week_id, CaseWeek.assigned_to.in_(pool))
                .group_by(CaseWeek.assigned_to)).all())
            owner = min(pool, key=lambda n: (counts.get(n, 0), n))
            if keep_member:
                reason = "queue change"   # picked by the system, so rebalance may move it
    if owner:
        cw.assigned_to = owner
        sess.add(Assignment(case_week_id=cw.id, assigned_to=owner, queue=target,
                            reason=reason, assigned_by=by))
    return target, owner


def rebalance(sess, by: str, queues=None, week_id: int | None = None,
              level: bool = True) -> dict:
    """Share a week's open cases out again after the team changes.

    Runs whenever someone is added, removed, disabled or has their queues changed.
    First, a case whose owner no longer works its queue - or that never had one,
    because the queue was empty at upload - goes to whoever carries least this week.
    Then each queue is levelled: while the busiest member holds at least two more
    cases than the quietest, one case they have not started moves across. A case with
    a review already written, or one a manager placed by hand, is never moved by the
    levelling - nobody loses work in progress. Load counts every case a person holds
    that week, across all their queues, the same way the weekly cap does."""
    if week_id is None:
        week = current_week(sess)
        if week is None:
            return {"moved": 0, "stranded": {}}
        week_id = week.id
    sess.flush()                                   # membership edits must be visible
    pools = queue_map(sess)

    q = (select(CaseWeek, Case.skill_final).join(Case, Case.id == CaseWeek.case_id)
         .where(CaseWeek.week_id == week_id, CaseWeek.needs_review.is_(True)))
    if queues is not None:
        q = q.where(Case.skill_final.in_(list(queues)))
    by_queue = defaultdict(list)
    for cw, skill in sess.execute(q).all():
        by_queue[skill].append(cw)
    ids = [cw.id for cws in by_queue.values() for cw in cws]
    if not ids:
        return {"moved": 0, "stranded": {}}

    load = defaultdict(int, sess.execute(
        select(CaseWeek.assigned_to, func.count())
        .where(CaseWeek.week_id == week_id, CaseWeek.assigned_to != "")
        .group_by(CaseWeek.assigned_to)).all())
    started = set(sess.scalars(select(Review.case_week_id)
                               .where(Review.case_week_id.in_(ids))).all())
    latest = (select(func.max(Assignment.id)).where(Assignment.case_week_id.in_(ids))
              .group_by(Assignment.case_week_id))
    pinned = set(sess.scalars(select(Assignment.case_week_id)
                              .where(Assignment.id.in_(latest),
                                     Assignment.reason == "manual")).all())

    moved, stranded = 0, {}

    def move(cw, who, queue):
        nonlocal moved
        if cw.assigned_to:
            load[cw.assigned_to] -= 1
        load[who] += 1
        cw.assigned_to = who
        sess.add(Assignment(case_week_id=cw.id, assigned_to=who, queue=queue,
                            reason="rebalance", assigned_by=by))
        moved += 1

    for queue, cws in sorted(by_queue.items()):
        pool = pools.get(queue) or []
        if not pool:
            stranded[queue] = len(cws)
            continue
        for cw in cws:
            if cw.assigned_to not in pool:
                move(cw, min(pool, key=lambda n: (load[n], n)), queue)
        if not level:
            continue
        movable = defaultdict(list)
        for cw in sorted(cws, key=lambda c: (c.review_order, c.id)):
            if cw.id not in started and cw.id not in pinned:
                movable[cw.assigned_to].append(cw)
        while True:
            donors = [n for n in pool if movable[n]]
            if not donors:
                break
            giver = max(donors, key=lambda n: (load[n], n))
            taker = min(pool, key=lambda n: (load[n], n))
            if load[giver] - load[taker] < 2:
                break
            cw = movable[giver].pop()              # the one they would reach last
            move(cw, taker, queue)
            movable[taker].append(cw)
    return {"moved": moved, "stranded": stranded}


def rebalance_note(result: dict) -> str:
    parts = []
    if result["moved"]:
        parts.append(f"{result['moved']} open case(s) reassigned to even out the queues.")
    for queue, n in result["stranded"].items():
        parts.append(f"{n} open case(s) in {queue} have nobody to go to - "
                     f"add someone to that queue.")
    return " ".join(parts)


def user_activity(sess, user: User) -> dict:
    """What a person would leave behind if their account were removed."""
    open_cases = sess.scalar(
        select(func.count()).select_from(CaseWeek)
        .where(CaseWeek.assigned_to == user.display_name,
               CaseWeek.needs_review.is_(True))) or 0
    reviews = sess.scalar(
        select(func.count()).select_from(Review)
        .where(Review.author == user.display_name)) or 0
    assigned = sess.scalar(
        select(func.count()).select_from(CaseWeek)
        .where(CaseWeek.assigned_to == user.display_name)) or 0
    return {"open_cases": open_cases, "reviews": reviews, "assigned": assigned}


def delete_user(sess, user_id: int, acting_user: User) -> tuple[bool, str]:
    """Remove an account. Reviews keep the author's name - the audit trail is not
    rewritten by an account going away. Open work is handed to the rest of each
    queue; if nobody else works one of those queues the delete is refused, otherwise
    the cases would sit assigned to nobody."""
    u = sess.get(User, user_id)
    if not u:
        return False, "No such user."
    if u.id == acting_user.id:
        return False, "You cannot delete your own account."
    if u.role == "admin":
        admins = sess.scalar(select(func.count()).select_from(User)
                             .where(User.role == "admin", User.active.is_(True))) or 0
        if admins <= 1:
            return False, "That is the only admin account left."

    act = user_activity(sess, u)
    name = u.display_name
    open_work = sess.execute(
        select(CaseWeek.week_id, Case.skill_final).join(Case, Case.id == CaseWeek.case_id)
        .where(CaseWeek.assigned_to == name, CaseWeek.needs_review.is_(True))
        .distinct()).all()
    open_queues = {skill for _w, skill in open_work}
    pools = queue_map(sess)
    uncovered = sorted(q for q in open_queues
                       if not [n for n in pools.get(q, []) if n != name])
    if uncovered:
        return False, (f"{name} still has {act['open_cases']} case(s) waiting for review "
                       f"and nobody else works {', '.join(uncovered)}. Add someone to "
                       f"that queue first, or disable the account instead.")

    sess.execute(QueueMembership.__table__.delete()
                 .where(QueueMembership.user_id == u.id))
    sess.delete(u)
    current = current_week(sess)
    handed = 0
    for week_id in sorted({w for w, _s in open_work}):
        # only the live week is levelled; older weeks just get their orphans placed
        handed += rebalance(sess, acting_user.display_name, open_queues, week_id,
                            level=current is not None and week_id == current.id)["moved"]
    note = f"Deleted {name}."
    if handed:
        note += f" {handed} open case(s) reassigned to the rest of the queue."
    if act["reviews"]:
        note += (f" Their {act['reviews']} review(s) keep their name, so the history "
                 f"still reads correctly.")
    return True, note


def unrouted(sess, week_id: int, max_score: int = 0, limit: int = 400):
    """Cases the router could not place. Score 0 means no keyword matched at all -
    there is nothing in the text to match, so a person has to read them."""
    s_cfg = load_settings()
    fallback = s_cfg.get("fallback_queue", "General")
    q = (select(CaseWeek, Case).join(Case, Case.id == CaseWeek.case_id)
         .where(CaseWeek.week_id == week_id))
    if max_score > 0:
        q = q.where((Case.skill_final == fallback) | (Case.match_score <= max_score))
    else:
        q = q.where(Case.skill_final == fallback)
    q = q.order_by(Case.severity, CaseWeek.age_days.desc())
    return sess.execute(q.limit(limit)).all()


def change_skill(sess, case_id: int, new_skill: str, by: str) -> None:
    """A correction. The router's suggestion is left intact - it is the label."""
    case = sess.get(Case, case_id)
    if not case or case.skill_final == new_skill:
        return
    sess.add(SkillChange(case_id=case.id, from_skill=case.skill_final,
                         to_skill=new_skill, changed_by=by))
    case.skill_final = new_skill
    case.skill_source = "human"


# ---------------------------------------------------------------- reporting

def current_week(sess) -> Week | None:
    return sess.scalars(select(Week).order_by(Week.week_ending.desc())).first()


PERIODS = {"week": "This week", "4w": "Last 4 weeks", "month": "This month", "all": "All"}


def period_start(end: date, period: str) -> date | None:
    """First week-ending date a report period covers, counted back from `end`.
    None means no lower bound."""
    if period == "week":
        return end
    if period == "4w":
        return end - timedelta(days=21)          # this week and the three before it
    if period == "month":
        return end.replace(day=1)
    return None


def period_weeks(sess, period: str) -> tuple[list[int] | None, str]:
    """Week ids for a report period and a label for the header. Anchored on the
    newest upload, not today: the export lands after the week it covers, so on a
    Monday "this week" still means the one just uploaded, and "this month" on the
    1st still means the month that just ended. None means every week."""
    latest = current_week(sess)
    start = period_start(latest.week_ending, period) if latest else None
    if start is None:
        return None, "All weeks"
    ids = list(sess.scalars(select(Week.id).where(Week.week_ending >= start,
                                                  Week.week_ending <= latest.week_ending)))
    label = {"week": "This week", "4w": "Last 4 weeks",
             "month": latest.week_ending.strftime("%B %Y")}[period]
    return ids, label


def queue_rows(sess, week_id: int, queue="", assignee="", state="",
               pending_only=False, search="", limit=400):
    q = (select(CaseWeek, Case).join(Case, Case.id == CaseWeek.case_id)
         .where(CaseWeek.week_id == week_id))
    if queue:
        q = q.where(Case.skill_final == queue)
    if assignee:
        q = q.where(CaseWeek.assigned_to == assignee)
    if state:
        q = q.where(CaseWeek.review_state == state)
    if pending_only:
        q = q.where(CaseWeek.needs_review.is_(True))
    if search:
        like = f"%{search}%"
        q = q.where(Case.problem_summary.like(like) | Case.lightning_id.like(like)
                    | Case.customer.like(like))
    q = q.order_by(CaseWeek.needs_review.desc(), Case.severity, CaseWeek.age_days.desc())
    return sess.execute(q.limit(limit)).all()


def _with_latest_review(*cols):
    """Select cols over case-weeks joined to Case, Week and the newest review per
    case-week (NULL review columns when there is none yet)."""
    latest = (select(Review.case_week_id, func.max(Review.id).label("rid"))
              .group_by(Review.case_week_id).subquery())
    return (select(*cols)
            .select_from(CaseWeek)
            .join(Case, Case.id == CaseWeek.case_id)
            .join(Week, Week.id == CaseWeek.week_id)
            .join(latest, latest.c.case_week_id == CaseWeek.id, isouter=True)
            .join(Review, Review.id == latest.c.rid, isouter=True))


def _bucket_grid(rows, buckets: list[str]) -> dict:
    grid = {}
    for key, bucket, n in rows:
        cell = grid.setdefault(key, {b: 0 for b in buckets} | {"Pending": 0, "Total": 0})
        b = (bucket or "").strip()
        cell[b if b in buckets else "Pending"] += n
        cell["Total"] += n
    return grid


def bucket_pivot(sess, buckets: list[str], row_field, week_id=None, tse_like="",
                 week_ids=None):
    """Counts of each bucket per row_field value, using the newest review per
    case-week."""
    q = _with_latest_review(row_field, Review.bucket, func.count())
    if week_id:
        q = q.where(CaseWeek.week_id == week_id)
    if week_ids is not None:
        q = q.where(CaseWeek.week_id.in_(week_ids))
    if tse_like:
        q = q.where(Case.tse_name.like(f"%{tse_like}%"))
    rows = ((name or "(unassigned)", b, n)
            for name, b, n in sess.execute(q.group_by(row_field, Review.bucket)))
    grid = _bucket_grid(rows, buckets)
    return [{"name": k, **v} for k, v in sorted(grid.items(), key=lambda kv: -kv[1]["Total"])]


def tse_trend(sess, tse_like: str, buckets: list[str], week_ids=None):
    """Bucket counts per week for one TSE - three Behaviour weeks in a row is the
    signal a single week cannot show."""
    q = (_with_latest_review(Week.label, Review.bucket, func.count())
         .where(Case.tse_name.like(f"%{tse_like}%"))
         .group_by(Week.label, Review.bucket))
    if week_ids is not None:
        q = q.where(CaseWeek.week_id.in_(week_ids))
    grid = _bucket_grid(sess.execute(q), buckets)
    return [{"week": k, **v} for k, v in sorted(grid.items())]


def tse_comments(sess, tse_like: str, week_id=None, week_ids=None):
    q = (_with_latest_review(Case.lightning_id, Case.problem_summary, Case.skill_final,
                             Week.label, Review.bucket, Review.comment, Review.author,
                             Review.call_made, CaseWeek.age_days)
         .where(Case.tse_name.like(f"%{tse_like}%"), Review.bucket != "")
         .order_by(Week.label.desc(), Review.bucket))
    if week_id:
        q = q.where(CaseWeek.week_id == week_id)
    if week_ids is not None:
        q = q.where(CaseWeek.week_id.in_(week_ids))
    return sess.execute(q).all()


def tse_names(sess, term=""):
    q = select(Case.tse_name).where(Case.tse_name != "").distinct().order_by(Case.tse_name)
    if term:
        q = q.where(Case.tse_name.like(f"%{term}%"))
    return [r[0] for r in sess.execute(q).all()]


def training_stats(sess) -> dict:
    total = sess.scalar(select(func.count()).select_from(Case)) or 0
    corrected = sess.scalar(select(func.count()).select_from(SkillChange)) or 0
    human = sess.scalar(select(func.count()).select_from(Case)
                        .where(Case.skill_source == "human")) or 0
    agree = sess.scalar(select(func.count()).select_from(Case)
                        .where(Case.skill_source == "human",
                               Case.skill_suggested == Case.skill_final)) or 0
    return {"cases": total, "human_labelled": human, "corrections": corrected,
            "router_agreed": agree,
            "agreement_pct": round(100 * agree / human, 1) if human else None}
