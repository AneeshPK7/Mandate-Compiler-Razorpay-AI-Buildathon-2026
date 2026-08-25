"""Database-backed usage accumulation.

Kept separate from `engine.py` so the enforcement logic itself stays a pure
function with no I/O. This module's only job is answering "how much of this
mandate's budget has already been consumed in the period containing `moment`?"
"""

from datetime import datetime

from sqlmodel import Session, func, select

from app.engine import Usage, period_bounds
from app.models import Decision, DecisionResult, Mandate, Transaction


def compute_usage(session: Session, mandate: Mandate, moment: datetime) -> Usage:
    """Sum ALLOWed spend and count for the mandate's period containing `moment`.

    Only allowed transactions count — a blocked attempt moves no money, so it
    must not consume budget.
    """
    start, end = period_bounds(moment, mandate.period)

    statement = (
        select(
            func.coalesce(func.sum(Transaction.amount), 0),
            func.count(Transaction.id),
        )
        .join(Decision, Decision.transaction_id == Transaction.id)
        .where(
            Transaction.mandate_id == mandate.id,
            Decision.result == DecisionResult.allow,
            Transaction.timestamp >= start,
            Transaction.timestamp < end,
        )
    )

    amount_spent, transaction_count = session.exec(statement).one()
    return Usage(amount_spent=int(amount_spent), transaction_count=int(transaction_count))
