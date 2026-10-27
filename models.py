"""
Domain model for the L2 review platform.

The shape follows two rules from the architecture doc:

1. The machine's guess and the human's answer live in separate columns. A
   correction is training data and must never be overwritten by a re-run.
2. A review is an event, not a field. Reviews and assignments are append-only;
   current state is the newest row.

The unit of work is the week. A case that stays open across three exports has one
Case row and three CaseWeek rows, each of which may or may not need its own review
(see services.carry_over_decision).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (Boolean, Date, DateTime, ForeignKey, Integer, String,
                        Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now()


# ---------------------------------------------------------------- people

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="l2")   # admin | manager | l2
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    memberships: Mapped[list["QueueMembership"]] = relationship(back_populates="user")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_see_all(self) -> bool:
        return self.role in ("admin", "manager")

    def queues(self) -> list[str]:
        return [m.queue for m in self.memberships]


class QueueMembership(Base):
    """Which skill queues an L2 works. One person can sit on several."""
    __tablename__ = "queue_memberships"
    __table_args__ = (UniqueConstraint("user_id", "queue"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    queue: Mapped[str] = mapped_column(String(32), index=True)

    user: Mapped[User] = relationship(back_populates="memberships")


# ---------------------------------------------------------------- time

class Week(Base):
    """One export = one week. The reporting period, capacity window and the thing
    a manager compares against last week."""
    __tablename__ = "weeks"

    id: Mapped[int] = mapped_column(primary_key=True)
    week_ending: Mapped[date] = mapped_column(Date, unique=True, index=True)
    label: Mapped[str] = mapped_column(String(32))          # e.g. "2026-W37"
    source_file: Mapped[str] = mapped_column(String(256), default="")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    uploaded_by: Mapped[str] = mapped_column(String(64), default="")
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    cases_new: Mapped[int] = mapped_column(Integer, default=0)
    cases_carried: Mapped[int] = mapped_column(Integer, default=0)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)

    case_weeks: Mapped[list["CaseWeek"]] = relationship(back_populates="week")


# ---------------------------------------------------------------- cases

class Case(Base):
    """Stable identity of a support case. Mutable fields hold the latest export's
    values; per-week values live on CaseWeek."""
    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    lightning_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    tse_name: Mapped[str] = mapped_column(String(128), default="", index=True)
    customer: Mapped[str] = mapped_column(String(256), default="")
    country: Mapped[str] = mapped_column(String(64), default="")
    problem_summary: Mapped[str] = mapped_column(Text, default="")
    product: Mapped[str] = mapped_column(String(128), default="")

    severity: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(64), default="")
    age_days: Mapped[int] = mapped_column(Integer, default=0)

    # classification: the guess and the answer are deliberately separate
    skill_suggested: Mapped[str] = mapped_column(String(32), default="")
    skill_source: Mapped[str] = mapped_column(String(16), default="keyword")
    skill_final: Mapped[str] = mapped_column(String(32), default="", index=True)
    match_score: Mapped[int] = mapped_column(Integer, default=0)
    match_evidence: Mapped[str] = mapped_column(Text, default="")

    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=_now)

    weeks: Mapped[list["CaseWeek"]] = relationship(back_populates="case")

    @property
    def was_corrected(self) -> bool:
        """A row where the human disagreed with the router - i.e. training data."""
        return bool(self.skill_suggested and self.skill_final
                    and self.skill_suggested != self.skill_final)


class CaseWeek(Base):
    """A case as it appeared in one week's export, plus whether it needs a fresh
    review that week and why."""
    __tablename__ = "case_weeks"
    __table_args__ = (UniqueConstraint("case_id", "week_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    week_id: Mapped[int] = mapped_column(ForeignKey("weeks.id"), index=True)

    severity: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(64), default="")
    age_days: Mapped[int] = mapped_column(Integer, default=0)

    # 'new' | 'changed' | 'periodic' | 'carried'
    review_state: Mapped[str] = mapped_column(String(16), default="new", index=True)
    review_reason: Mapped[str] = mapped_column(String(256), default="")
    needs_review: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    assigned_to: Mapped[str] = mapped_column(String(128), default="", index=True)
    review_order: Mapped[int] = mapped_column(Integer, default=0)

    case: Mapped[Case] = relationship(back_populates="weeks")
    week: Mapped[Week] = relationship(back_populates="case_weeks")
    reviews: Mapped[list["Review"]] = relationship(
        back_populates="case_week", order_by="Review.created_at.desc()")

    @property
    def current_review(self) -> "Review | None":
        return self.reviews[0] if self.reviews else None


# ---------------------------------------------------------------- events

class Review(Base):
    """Append-only. The newest row for a case-week is the current verdict; older
    rows are the audit trail of how it got there."""
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_week_id: Mapped[int] = mapped_column(ForeignKey("case_weeks.id"), index=True)
    bucket: Mapped[str] = mapped_column(String(64), default="", index=True)
    call_made: Mapped[str] = mapped_column(String(8), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(128), default="", index=True)
    # 'review' | 'carried' (last week's verdict copied forward unchanged)
    kind: Mapped[str] = mapped_column(String(16), default="review")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)

    case_week: Mapped[CaseWeek] = relationship(back_populates="reviews")


class Assignment(Base):
    """Append-only record of who a case-week was given to and why."""
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_week_id: Mapped[int] = mapped_column(ForeignKey("case_weeks.id"), index=True)
    assigned_to: Mapped[str] = mapped_column(String(128), index=True)
    queue: Mapped[str] = mapped_column(String(32), default="")
    reason: Mapped[str] = mapped_column(String(64), default="")   # continuity | least-loaded | manual | rebalance | queue change
    assigned_by: Mapped[str] = mapped_column(String(128), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class SkillChange(Base):
    """Every time a human overrides the router. This table is the training set."""
    __tablename__ = "skill_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), index=True)
    from_skill: Mapped[str] = mapped_column(String(32), default="")
    to_skill: Mapped[str] = mapped_column(String(32), default="")
    changed_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
