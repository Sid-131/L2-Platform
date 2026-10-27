"""Build the demo dataset in demo_data/.

Everything is fictional: case numbers, customers, case owners and reviewers. The
data goes in the way real data does - six weekly CSV exports pushed through
services.ingest_week - so carry-over, assignment and routing are the platform's own
decisions, not hand-written rows. Reviews and routing corrections are then made the
way a team would make them.

    python demo_build.py            # rewrites demo_data/
"""

from __future__ import annotations

import csv
import json
import os
import random
import shutil
import sys
from datetime import date, datetime, time, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "demo_data")
BUILD = os.path.join(HERE, ".demo_build")

WEEKS = 6
rng = random.Random(20261002)

# ---------------------------------------------------------------- the team

ADMIN = ("alex.rivera", "Alex Rivera")
MANAGER = ("maya.collins", "Maya Collins")
TEAM = {                                   # queue -> reviewers
    "Admin": ["Grace Liu", "Omar Haddad"],
    "FS": ["Isabel Torres", "Victor Hale"],
    "Net": ["Chloe Martin", "Nikhil Rao"],
    "Win": ["Jordan Lee", "Hannah Brooks"],
    "Nix": ["Jordan Lee", "Samuel Ortiz"],
    "Bu": ["Leila Nasser", "Marcus Green"],
    "Hw": ["Ishaan Patel", "Zoe Adams"],
    "General": ["Grace Liu"],
}

# case owners (TSEs), with a review tendency so the TSE report has a story to tell
OWNERS = {
    "Arjun Mehta": {}, "Neha Kapoor": {}, "Lukas Becker": {},
    "Sofia Marques": {"Knowledge gap": 3},
    "Kenji Watanabe": {"NAC": 2.2},
    "Emma Clarke": {"Customer Delay": 3},
    "Dev Malhotra": {"Behaviour": 3.5, "Monitoring": 1.5},
    "Fatima Zahra": {}, "Daniel Okafor": {"Resources": 2.5},
    "Mira Holt": {"NAC": 1.6}, "Tom Hughes": {"Monitoring": 2.5}, "Ananya Rao": {},
}
CUSTOMERS = ["Northwind Media", "Contoso Health", "Fabrikam Labs", "Globex Logistics",
             "Initech Financial", "Blue Yonder Airlines", "Tailspin Toys", "Wide World Importers",
             "Adventure Works", "Litware Analytics", "Proseware Genomics", "Lucerne Publishing",
             "Margie's Travel", "Coho Vineyard", "Alpine Ski House", "Woodgrove Bank",
             "Trey Research", "Humongous Insurance", "Datum Research", "Relecloud"]
COUNTRIES = ["India", "Germany", "United Kingdom", "United States", "Japan", "Australia",
             "Singapore", "France", "Brazil", "UAE"]
PRODUCTS = ["F600", "F900", "H700", "H7000", "A300", "A3000"]
SEVERITIES = ["1 - Critical", "2 - High", "3 - Medium", "4 - Low"]
SEV_W = [6, 28, 46, 20]
STATUSES = ["Open", "In Progress", "Waiting on Customer", "Pending Parts"]

# (summary, the queue a human would put it in)
SUMMARIES = {
    "Hw": ["Node {n} panic and reboot overnight", "Failed drive in bay {b} not smartfailing",
           "PSU power fault alert on node {n}", "NVRAM battery warning after maintenance",
           "DIMM correctable memory errors on node {n}", "Node {n} unresponsive, needs power cycle",
           "SSD journal errors reported on node {n}", "Fan failure on chassis {n}",
           "Multiple drives failing in the same sled", "Node went down after firmware patch"],
    "Net": ["SmartConnect zone not balancing clients", "Int-a link flap on backend switch",
            "VLAN tagging lost after subnet change", "Frontend NIC port down on node {n}",
            "Static route missing after reboot of node {n}", "IP pool exhausted for SmartConnect",
            "Backend network fabric errors on leaf switch", "Uplink flapping on core switch"],
    "Win": ["SMB share access denied after AD join", "Kerberos failures for SMB3 clients",
            "ACL inheritance broken on Windows share", "Previous versions missing in Explorer",
            "Domain controller unreachable, SMB logins slow", "Machine account password expired",
            "Robocopy to cluster share very slow", "Oplock breaks causing app timeouts on share"],
    "Nix": ["NFS mount hangs on ESXi datastore", "Stale file handle on NFSv3 export",
            "LDAP uid mapping wrong for Linux clients", "Root squash blocking writes on NFS export",
            "NFSv4 lockd errors after upgrade", "Automount fails for unix home directories",
            "showmount returns empty export list", "VMware datastore latency over NFS"],
    "FS": ["SyncIQ policy failing on target cluster", "Snapshot delete job stuck at 0%",
           "SmartQuota hard limit not enforced", "SmartPools file pool policy not tiering",
           "FlexProtect job running for days", "Dedupe job reports no savings",
           "SyncIQ failover test fails", "Quota usage report wrong for directory"],
    "Admin": ["InsightIQ not collecting cluster data", "License expired warning in WebUI",
              "CELOG alerts not sending email notification", "OneFS upgrade health check failing",
              "SupportAssist connectivity lost", "RBAC role cannot see audit settings",
              "NTP drift alert on cluster", "Log upload fails with isi_gather_info"],
    "Bu": ["NDMP backup job failing with NetBackup", "Avamar restore very slow",
           "DD Boost backup fail after network change", "Commvault NDMP incremental backup timing out",
           "Veeam backup job stalls at 99%", "Tape restore missing files"],
    # the router guesses wrong on these: the words point one way, the problem another
    "_mis": [("Snapshot of VMware datastore fails nightly", "FS"),
             ("Quota report email notification not received", "Admin"),
             ("SMB clients disconnect during node reboot", "Win"),
             ("Alerts flood after drive replacement", "Hw"),
             ("NFS export missing after OneFS upgrade", "Nix"),
             ("Backup admin user cannot log in to WebUI", "Admin")],
    "General": ["Product problem", "Customer needs assistance with cluster",
                "Slow response reported by users since Monday", "Follow-up on previous case",
                "Remote session requested", "Customer escalation - see notes",
                "Question about best practices", "Intermittent issue, details to follow"],
}
QUEUE_W = {"Hw": 17, "Net": 12, "Win": 14, "Nix": 12, "FS": 13, "Admin": 10, "Bu": 8,
           "_mis": 6, "General": 8}

BUCKET_W = {"NAC": 46, "Behaviour": 12, "Customer Delay": 16, "Knowledge gap": 11,
            "Monitoring": 8, "Resources": 7}
COMMENTS = {
    "NAC": ["", "", "Clear action plan, customer kept informed.", "Good ownership.",
            "Right logs collected on the first ask."],
    "Behaviour": ["No update to the customer for 9 days despite two follow-ups.",
                  "Closed troubleshooting without confirming the fix with the customer.",
                  "Case notes say 'checking' for a week with no next step.",
                  "Promised a call-back that never happened.",
                  "Handed the customer three KB links instead of looking at their logs."],
    "Customer Delay": ["Waiting on logs from the customer since last week; chased twice.",
                       "Customer postponed the remote session twice.",
                       "Maintenance window not approved on the customer side yet.",
                       "Customer contact on leave, no alternate given."],
    "Knowledge gap": ["Didn't check the SmartConnect delegation before asking for packet captures.",
                      "Should have checked the job engine queue before escalating.",
                      "Missed that the NFS export had root squash enabled.",
                      "Asked for a full log gather when the CELOG event already had the answer.",
                      "Unaware of the known issue fixed in the next patch."],
    "Monitoring": ["Severity should have gone up when the second node dropped.",
                   "Age crossed 30 days with no escalation review.",
                   "No action for 5 days after the customer replied.",
                   "Should have engaged engineering at the 14 day mark."],
    "Resources": ["Parts dispatch delayed, no field engineer in the region.",
                  "Waiting on a lab reproduction slot.",
                  "Engineering backlog, defect fix not scheduled yet."],
}


def pick(weights: dict):
    keys = list(weights)
    return rng.choices(keys, [weights[k] for k in keys])[0]


# ---------------------------------------------------------------- cases over time

_next_id = [240100000]


def new_case(week_end: date, max_age: int) -> dict:
    q = pick(QUEUE_W)
    if q == "_mis":
        summary, truth = rng.choice(SUMMARIES["_mis"])
    else:
        summary, truth = rng.choice(SUMMARIES[q]), q
    summary = summary.format(n=rng.randint(1, 12), b=rng.randint(1, 24))
    _next_id[0] += rng.randint(37, 9000)
    age = rng.randint(0, max_age)
    return {"id": str(_next_id[0]), "owner": rng.choice(list(OWNERS)),
            "customer": rng.choice(CUSTOMERS), "country": rng.choice(COUNTRIES),
            "severity": rng.choices(SEVERITIES, SEV_W)[0], "summary": summary,
            "product": rng.choice(PRODUCTS), "status": rng.choice(STATUSES[:3]),
            "age": age, "created": (week_end - timedelta(days=age)).isoformat(),
            "truth": truth,
            # a coordinator pre-labels some rows in the export; those are the yardstick
            # the router's accuracy is measured against
            "skill": truth if rng.random() < 0.4 else ""}


def advance(cases: list[dict], week_end: date) -> list[dict]:
    """One week passes: some cases close, the rest age and occasionally move."""
    kept = []
    for c in cases:
        if rng.random() < 0.17 + min(c["age"], 60) / 600:
            continue
        c["age"] += 7
        if rng.random() < 0.03:
            i = SEVERITIES.index(c["severity"])
            c["severity"] = SEVERITIES[max(0, min(3, i + rng.choice([-1, 1])))]
        if rng.random() < 0.05:
            c["status"] = rng.choice([s for s in STATUSES if s != c["status"]])
        kept.append(c)
    kept += [new_case(week_end, 6) for _ in range(rng.randint(20, 28))]
    return kept


def write_export(path: str, cases: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Case Number", "Case Owner", "Customer", "Country", "Severity",
                    "Problem Summary", "Product", "Status", "Age (Days)", "Created Date", "Skill"])
        for c in cases:
            w.writerow([c["id"], c["owner"], c["customer"], c["country"], c["severity"],
                        c["summary"], c["product"], c["status"], c["age"], c["created"], c["skill"]])


# ---------------------------------------------------------------- build

def main() -> None:
    shutil.rmtree(BUILD, ignore_errors=True)
    os.makedirs(BUILD)
    os.environ["L2_DATA"] = BUILD

    cfg = json.load(open(os.path.join(HERE, "settings.json"), encoding="utf-8"))
    cfg["roster"] = TEAM
    cfg["_readme"] = "Demo settings. All names and cases are fictional."
    with open(os.path.join(BUILD, "settings.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    sys.path.insert(0, HERE)
    from sqlalchemy import select
    import services as svc
    from auth import hash_password
    from models import Case, CaseWeek, QueueMembership, Review, User, Week
    from store import init_db, session_scope

    init_db()
    pw = hash_password("demo")
    with session_scope() as s:
        people = {ADMIN[1]: (ADMIN[0], "admin"), MANAGER[1]: (MANAGER[0], "manager")}
        for names in TEAM.values():
            for n in names:
                people.setdefault(n, (n.lower().replace(" ", "."), "l2"))
        for name, (uname, role) in people.items():
            s.add(User(username=uname, display_name=name, password_hash=pw, role=role,
                       must_change_password=False,
                       last_login=datetime.now() - timedelta(hours=rng.randint(2, 160))))
        s.flush()
        ids = {u.display_name: u.id for u in s.scalars(select(User))}
        for q, names in TEAM.items():
            for n in names:
                s.add(QueueMembership(user_id=ids[n], queue=q))

    today = date.today()
    last_sunday = today - timedelta(days=today.weekday() + 1)
    week_ends = [last_sunday - timedelta(weeks=WEEKS - 1 - i) for i in range(WEEKS)]

    truth: dict[str, str] = {}
    cases = [new_case(week_ends[0], 45) for _ in range(95)]
    for wi, wk in enumerate(week_ends):
        if wi:
            cases = advance(cases, wk)
        for c in cases:
            truth[c["id"]] = c["truth"]
        owner_of = {c["id"]: c["owner"] for c in cases}
        path = os.path.join(BUILD, f"export_{wk}.csv")
        write_export(path, cases)
        latest = wi == WEEKS - 1

        with session_scope() as s:
            res = svc.ingest_week(s, path, f"open_cases_{wk}.csv", wk, ADMIN[1])
        with session_scope() as s:
            week = s.scalar(select(Week).where(Week.week_ending == wk))
            week.uploaded_at = datetime.combine(wk + timedelta(days=1), time(9, 40))
            stamp = datetime.combine(wk, time(12))
            for case in s.scalars(select(Case).where(Case.first_seen > stamp)):
                case.first_seen = stamp

            # the manager fixes routing; in the live week some is left for Triage
            rows = s.execute(select(CaseWeek, Case).join(Case, Case.id == CaseWeek.case_id)
                             .where(CaseWeek.week_id == week.id)).all()
            for cw, case in rows:
                want = truth.get(case.lightning_id, case.skill_final)
                if (want != case.skill_final and want != "General"
                        and rng.random() < (0.35 if latest else 0.85)):
                    svc.reassign(s, cw.id, want, MANAGER[1], keep_member=True)
            s.flush()

            # reviewers work their lists; the live week is part-done
            for cw, case in s.execute(select(CaseWeek, Case)
                                      .join(Case, Case.id == CaseWeek.case_id)
                                      .where(CaseWeek.week_id == week.id,
                                             CaseWeek.needs_review.is_(True))).all():
                if not cw.assigned_to:
                    continue
                done = 0.97 if not latest else (0.35 if cw.assigned_to == "Jordan Lee" else 0.6)
                if rng.random() > done:
                    continue
                w = dict(BUCKET_W)
                for b, x in OWNERS.get(owner_of.get(case.lightning_id, ""), {}).items():
                    w[b] *= x
                if cw.age_days > 30:
                    w["Monitoring"] *= 1.8
                bucket = pick(w)
                comment = rng.choice(COMMENTS[bucket])
                call = "Yes" if rng.random() < (0.85 if bucket == "NAC" else 0.6) else "No"
                when = datetime.combine(wk + timedelta(days=rng.randint(1, 5)),
                                        time(rng.randint(9, 18), rng.randint(0, 59)))
                s.add(Review(case_week_id=cw.id, bucket=bucket, call_made=call,
                             comment=comment, author=cw.assigned_to, kind="review",
                             created_at=when))
                cw.needs_review = False
        print(f"{res['week']}: {res['rows']} rows, new {res.get('new', 0)}, "
              f"changed {res.get('changed', 0)}, periodic {res.get('periodic', 0)}, "
              f"carried {res.get('carried', 0)}")

    # next week's export, for visitors to upload themselves
    nxt = last_sunday + timedelta(weeks=1)
    sample = os.path.join(BUILD, "sample_next_week.csv")
    write_export(sample, advance(cases, nxt))

    with session_scope() as s:
        print("router:", svc.training_stats(s))

    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)
    for name in ("l2platform.db", "settings.json", "sample_next_week.csv"):
        shutil.copyfile(os.path.join(BUILD, name), os.path.join(OUT, name))
    from store import engine
    engine.dispose()
    shutil.rmtree(BUILD, ignore_errors=True)
    print(f"wrote {OUT}  (next sample export: week ending {nxt})")


if __name__ == "__main__":
    main()
