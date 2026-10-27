"""Database session handling, settings, and the classifier interface."""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base

HERE = os.path.dirname(os.path.abspath(__file__))

# Code and data are separate paths. Under systemd the code directory is read-only to
# the service account, so anything the app writes - the database, settings edited on
# the Settings page, uploaded exports - lives under L2_DATA instead.
DATA_DIR = os.environ.get("L2_DATA", HERE)
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.environ.get("L2_DB", os.path.join(DATA_DIR, "l2platform.db"))
SETTINGS_PATH = os.environ.get("L2_SETTINGS", os.path.join(DATA_DIR, "settings.json"))

# First run on a fresh data directory: seed settings from the copy shipped with the code
if not os.path.exists(SETTINGS_PATH):
    _bundled = os.path.join(HERE, "settings.json")
    if os.path.exists(_bundled) and os.path.abspath(_bundled) != os.path.abspath(SETTINGS_PATH):
        import shutil
        shutil.copyfile(_bundled, SETTINGS_PATH)

engine = create_engine(f"sqlite:///{DB_PATH}", future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db() -> None:
    Base.metadata.create_all(engine)


# ---------------------------------------------------------------- settings

def load_settings() -> dict:
    with open(SETTINGS_PATH) as f:
        return json.load(f)


def save_settings(s: dict) -> None:
    with open(SETTINGS_PATH, "w") as f:
        json.dump(s, f, indent=2)


# ---------------------------------------------------------------- classifier

class Classifier:
    """One interface, two implementations. Swapping the keyword rules for a trained
    model must not touch anything downstream, and both must be runnable side by side
    so the new one can be measured before it is trusted."""

    name = "base"

    def classify(self, summary: str, product: str = "") -> tuple[str, int, str]:
        """Return (skill, confidence_score, human-readable evidence)."""
        raise NotImplementedError


class KeywordClassifier(Classifier):
    name = "keyword"

    def __init__(self, keywords: dict, tiebreak: list[str], fallback: str = "General"):
        self.fallback = fallback
        self.tiebreak = tiebreak
        self.compiled = {}
        for cat, words in keywords.items():
            pairs = []
            for w in words:
                w = str(w).strip().lower()
                if not w:
                    continue
                prefix = w.endswith("*")
                core = w[:-1] if prefix else w
                pat = r"(?<![A-Za-z0-9])" + re.escape(core).replace(r"\ ", r"\s+")
                if not prefix:
                    pat += r"(?![A-Za-z0-9])"
                pairs.append((core, re.compile(pat, re.IGNORECASE)))
            self.compiled[cat] = pairs

    def classify(self, summary: str, product: str = "") -> tuple[str, int, str]:
        scores, hits = defaultdict(int), defaultdict(list)
        fields = ((summary.lower(), 3), (product.lower(), 1))
        for cat, pairs in self.compiled.items():
            claimed = defaultdict(list)
            for word, rx in pairs:
                for fi, (text, weight) in enumerate(fields):
                    if not text:
                        continue
                    m = rx.search(text)
                    # a keyword and its prefix variant must score once, not twice
                    if not m or any(m.start() < e and s < m.end() for s, e in claimed[fi]):
                        continue
                    claimed[fi].append((m.start(), m.end()))
                    scores[cat] += weight
                    if word not in hits[cat]:
                        hits[cat].append(word)
        if not scores:
            return self.fallback, 0, ""
        top = max(scores.values())
        winners = [c for c, s in scores.items() if s == top]
        if len(winners) > 1:
            winners.sort(key=lambda c: self.tiebreak.index(c)
                         if c in self.tiebreak else 99)
        return winners[0], top, ", ".join(hits[winners[0]][:6])


def build_classifier(settings: dict | None = None) -> Classifier:
    s = settings or load_settings()
    return KeywordClassifier(s["keywords"], s.get("tiebreak_order", s["queues"]),
                             s.get("fallback_queue", "General"))
