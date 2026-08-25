from datetime import datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models import Decision, DecisionResult, Mandate, MandateStatus, Period, Transaction
from app.usage import compute_usage
from tests.test_engine import make_mandate


@pytest.fixture
def session():
    engine = create_engine("sqlite://")  # in-memory, per-test
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def record(session: Session, mandate_id: str, amount: int, when: datetime, result: DecisionResult):
    txn = Transaction(
        mandate_id=mandate_id,
        amount=amount,
        merchant="zepto",
        category="groceries",
        timestamp=when,
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    session.add(
        Decision(
            transaction_id=txn.id,
            result=result,
            reason_code="ALLOWED" if result is DecisionResult.allow else "BLOCKED",
            rule_triggered="none",
            prev_hash="0" * 64,
            audit_hash="x" * 64,
        )
    )
    session.commit()
    return txn


@pytest.fixture
def mandate(session):
    m = make_mandate(id=None, period=Period.week)
    m.id = "m-db-1"
    session.add(m)
    session.commit()
    return m


def test_usage_is_zero_with_no_history(session, mandate):
    usage = compute_usage(session, mandate, datetime(2026, 6, 10, 12, 0))
    assert usage.amount_spent == 0
    assert usage.transaction_count == 0


def test_usage_sums_allowed_transactions(session, mandate):
    record(session, mandate.id, 500, datetime(2026, 6, 9, 10, 0), DecisionResult.allow)
    record(session, mandate.id, 300, datetime(2026, 6, 10, 10, 0), DecisionResult.allow)

    usage = compute_usage(session, mandate, datetime(2026, 6, 10, 12, 0))
    assert usage.amount_spent == 800
    assert usage.transaction_count == 2


def test_blocked_transactions_do_not_consume_budget(session, mandate):
    record(session, mandate.id, 500, datetime(2026, 6, 9, 10, 0), DecisionResult.allow)
    record(session, mandate.id, 9999, datetime(2026, 6, 9, 11, 0), DecisionResult.block)

    usage = compute_usage(session, mandate, datetime(2026, 6, 10, 12, 0))
    assert usage.amount_spent == 500
    assert usage.transaction_count == 1


def test_usage_excludes_other_periods(session, mandate):
    # Previous ISO week (week of Jun 1) — must not count toward Jun 8-14.
    record(session, mandate.id, 700, datetime(2026, 6, 3, 10, 0), DecisionResult.allow)
    record(session, mandate.id, 200, datetime(2026, 6, 10, 10, 0), DecisionResult.allow)

    usage = compute_usage(session, mandate, datetime(2026, 6, 10, 12, 0))
    assert usage.amount_spent == 200


def test_usage_excludes_other_mandates(session, mandate):
    other = make_mandate(id=None)
    other.id = "m-db-2"
    session.add(other)
    session.commit()

    record(session, "m-db-2", 900, datetime(2026, 6, 10, 10, 0), DecisionResult.allow)
    record(session, mandate.id, 100, datetime(2026, 6, 10, 10, 0), DecisionResult.allow)

    usage = compute_usage(session, mandate, datetime(2026, 6, 10, 12, 0))
    assert usage.amount_spent == 100


def test_week_boundary_is_half_open(session, mandate):
    """Monday 00:00 starts a new week; Sunday 23:59 is still the old one."""
    record(session, mandate.id, 400, datetime(2026, 6, 7, 23, 59), DecisionResult.allow)
    record(session, mandate.id, 600, datetime(2026, 6, 8, 0, 0), DecisionResult.allow)

    usage = compute_usage(session, mandate, datetime(2026, 6, 10, 12, 0))
    assert usage.amount_spent == 600
