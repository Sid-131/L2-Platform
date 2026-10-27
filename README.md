# L2 Review Platform

**[Live demo →](#)** · pick a role, no sign-up · fictional data, resets on its own

A support team's senior (L2) engineers review the open cases of front-line engineers
(TSEs) every week. Before this tool, that review ran on a shared spreadsheet: someone
split the weekly export by hand, every long-running case got re-reviewed every week,
nobody could tell who wrote which verdict, and the manager's 1:1 prep was a pivot table.

This platform takes the raw weekly export and turns it into routed review queues, a
fast keyboard-driven review desk, and a printable report per case owner.

## What it does

| | |
|---|---|
| **Ingest** | Upload the weekly `.xlsx`/`.csv` export. Columns are matched by fuzzy header names, and cases are tracked across weeks by case number. |
| **Route** | A keyword classifier suggests a skill queue (Networking, Windows/SMB, NFS, Hardware…) with a score and the words it matched. Managers fix wrong guesses in **Triage**. |
| **Carry over** | A case open for six weeks shows up in six exports. Its last verdict is carried forward automatically. It comes back for a fresh look only if its severity or status changed, it crossed an age band (7/14/30/60 days), or it hasn't been looked at for 4 weeks. |
| **Assign** | Continuity first: the reviewer who saw the case last time gets it again. Otherwise it goes to whoever is least loaded in that queue, with a weekly cap per person across all their queues. Adding or removing someone rebalances the open work, without touching anything already started or placed by hand. |
| **Review** | Pick a verdict bucket, whether the right call was made, and a comment. A comment is required for anything other than "No action needed" (NAC), and the API enforces this, not just the form. Keyboard shortcuts: `j/k` to move, `1–9` for the bucket, `y/n` for the call, `Enter` to save. |
| **Report** | A weekly dashboard (verdicts per reviewer and per queue, router accuracy) and a **TSE report**: one case owner's verdicts, week-by-week trend and every reviewer comment for a chosen period, laid out for printing before a 1:1. |

## Design decisions worth a look

- **Reviews and assignments are append-only.** The current verdict is simply the newest row, so the full history of every decision is kept. A verdict about a named engineer has to be traceable to whoever wrote it, which is why there are real accounts and roles rather than a name picker.
- **The router's guess and the human's answer are stored in separate columns.** A correction never overwrites the suggestion, so every fix becomes a labelled training row. The classifier sits behind a small interface, so a trained model can replace the keyword rules later and be measured side by side.
- **The re-review rules are the main control over reviewer workload.** Widening the age bands or lengthening the recheck interval directly cuts each week's work. Both are on the Settings page.
- **Case identity degrades loudly.** If an export has lost its case-number column, cases fall back to a hash of their details, and the upload warns how many rows could not be told apart and were merged.

## Stack

Python 3.12 · Flask · SQLAlchemy 2 · SQLite · pandas · Waitress · Jinja2 with vanilla JS · 51 pytest tests.

Reference deployment, on a self-managed Ubuntu VM: a hardened Docker container (read-only root filesystem, all capabilities dropped, non-root user) bound to localhost. In front of it, nginx terminates TLS on a dedicated port with a private CA, and the firewall keeps the app port closed. systemd timers take nightly SQLite backups with an integrity check and clear out uploaded exports after 30 days.

```
browser ─HTTPS─► nginx ─► app container (Flask/Waitress) ─► SQLite (bind mount)
                                                   ▲
                       systemd timers: nightly backup · weekly upload cleanup
```

## This demo

The demo is the real application with a demo layer (`demo.py`) on top:

- One-click sign-in as **Admin**, **Manager** or **L2 reviewer**, plus a role switch in the top bar.
- `demo_build.py` generates six weeks of fictional exports and pushes them through the platform's own ingest. The carry-over, assignment and routing results are the app's real decisions. Reviews are generated with a few deliberate patterns, so the TSE report has something to show (try *Dev Malhotra* or *Emma Clarke*).
- On the Upload page you can download next week's export and upload it yourself.
- Each server instance works on its own copy of the data in `/tmp`, so changes are temporary. **Reset** in the top bar restores the original data.

All names, customers and case numbers are made up.

## Run it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip
python run_demo.py              # http://127.0.0.1:5000
python demo_build.py            # regenerate demo_data/
python -m pytest tests -q       # the test suite
```

Deploy: import the repo into Vercel. `vercel.json` routes every request to `api/index.py`, and no settings are needed.
Optionally set `L2_SECRET` in the project's environment variables.
