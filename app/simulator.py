"""Replay a batch of transactions through the engine, one at a time.

The defining property here is that **mandate state is re-read from the database
for every single transaction**. Caching the mandate once at the top of the loop
would be the obvious optimisation and would silently break the guarantee the
whole system exists to make: a revocation issued mid-batch must take effect on
the very next transaction, not at the next batch boundary.

To make that literally true rather than merely intended, each iteration opens
its own short-lived session. A long-lived session's identity map can hand back
a stale in-memory mandate even after another connection has committed a change,
which would produce exactly the bug this design is meant to rule out.

The generator is deliberately independent of HTTP so it can be tested directly;
main.py wraps it for SSE.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlmodel import Session, select

from app.audit import append_decision
from app.database import engine as db_engine
from app.engine import evaluate
from app.models import Decision, Mandate, Transaction
from app.usage import compute_usage


@dataclass
class SimulationStep:
    """One evaluated transaction, shaped for the dashboard."""

    index: int
    total: int
    transaction_id: str
    amount: int
    merchant: str
    category: str
    timestamp: str
    result: str
    reason_code: str
    rule_triggered: str
    detail: str
    seq: int
    audit_hash: str
    mandate_status: str
    allowed_so_far: int
    blocked_so_far: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SimulationSummary:
    total: int
    allowed: int
    blocked: int
    by_reason: dict[str, int]
    chain_head: str
    chain_valid: bool
    ended_early: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def pending_transactions(session: Session, mandate_id: str) -> list[str]:
    """IDs of transactions for this mandate with no decision yet, in time order.

    Returns IDs rather than ORM objects because each will be re-loaded in its
    own session during the run.
    """
    decided = select(Decision.transaction_id).where(Decision.transaction_id.is_not(None))
    statement = (
        select(Transaction.id)
        .where(
            Transaction.mandate_id == mandate_id,
            Transaction.id.not_in(decided),
        )
        .order_by(Transaction.timestamp)
    )
    return list(session.exec(statement))


def simulate(
    mandate_id: str,
    delay_seconds: float = 0.0,
    limit: int | None = None,
) -> Iterator[SimulationStep | SimulationSummary]:
    """Evaluate every pending transaction, yielding one step at a time.

    Yields SimulationStep per transaction, then a final SimulationSummary.
    """
    with Session(db_engine) as session:
        mandate = session.get(Mandate, mandate_id)
        if mandate is None:
            raise LookupError(f"no mandate {mandate_id}")
        txn_ids = pending_transactions(session, mandate_id)

    if limit is not None:
        txn_ids = txn_ids[:limit]

    total = len(txn_ids)
    allowed = blocked = 0
    by_reason: dict[str, int] = {}

    for index, txn_id in enumerate(txn_ids, start=1):
        if delay_seconds:
            time.sleep(delay_seconds)

        # A fresh session per transaction: this is what makes a mid-batch
        # revocation visible immediately.
        with Session(db_engine) as session:
            mandate = session.get(Mandate, mandate_id)
            txn = session.get(Transaction, txn_id)
            if mandate is None or txn is None:
                continue

            usage = compute_usage(session, mandate, txn.timestamp)
            outcome = evaluate(mandate, txn, usage)
            entry = append_decision(session, txn, outcome)

            if outcome.allowed:
                allowed += 1
            else:
                blocked += 1
            by_reason[outcome.reason_code] = by_reason.get(outcome.reason_code, 0) + 1

            yield SimulationStep(
                index=index,
                total=total,
                transaction_id=txn.id,
                amount=txn.amount,
                merchant=txn.merchant,
                category=txn.category,
                timestamp=txn.timestamp.isoformat(),
                result=outcome.result.value,
                reason_code=outcome.reason_code,
                rule_triggered=outcome.rule_triggered,
                detail=outcome.detail,
                seq=entry.seq,
                audit_hash=entry.audit_hash,
                mandate_status=mandate.status.value,
                allowed_so_far=allowed,
                blocked_so_far=blocked,
            )

    from app.audit import verify_chain

    with Session(db_engine) as session:
        report = verify_chain(session)

    yield SimulationSummary(
        total=total,
        allowed=allowed,
        blocked=blocked,
        by_reason=dict(sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0]))),
        chain_head=report.head,
        chain_valid=report.valid,
    )


def seed_dataset(session: Session, *, sign: bool = True) -> tuple[Mandate, int]:
    """Load the synthetic dataset's mandate and transactions into the database.

    Decisions are deliberately NOT created — the simulator produces those, so
    the demo can stream them live.
    """
    from app.audit import append_mandate_event
    from app.models import EventType
    from app.signing import sign_mandate
    from app.synthetic import generate_dataset

    dataset = generate_dataset()

    existing = session.get(Mandate, dataset.mandate.id)
    if existing is not None:
        return existing, 0

    mandate = dataset.mandate
    if sign:
        sign_mandate(mandate)
    session.add(mandate)
    session.commit()
    session.refresh(mandate)

    append_mandate_event(session, mandate, EventType.MANDATE_CREATED)

    for case in sorted(dataset.cases, key=lambda c: c.transaction.timestamp):
        session.add(case.transaction)
    session.commit()

    return mandate, len(dataset.cases)


def revoke_mandate(session: Session, mandate_id: str) -> Mandate:
    """Flip a mandate to revoked and record the transition in the audit chain."""
    from app.audit import append_mandate_event
    from app.models import EventType, MandateStatus

    mandate = session.get(Mandate, mandate_id)
    if mandate is None:
        raise LookupError(f"no mandate {mandate_id}")

    if mandate.status is MandateStatus.revoked:
        return mandate

    mandate.status = MandateStatus.revoked
    session.add(mandate)
    session.commit()
    session.refresh(mandate)

    append_mandate_event(session, mandate, EventType.MANDATE_REVOKED)
    return mandate


def utcnow_iso() -> str:
    return datetime.utcnow().isoformat()
