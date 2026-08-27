"""Simulator and revocation tests.

The headline guarantee under test: a revocation issued *mid-batch* takes effect
on the very next transaction. That is the difference between a mandate that can
actually be withdrawn and one that can only be withdrawn between batches.
"""

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.audit import verify_chain
from app.engine import ReasonCode
from app.models import Decision, EventType, Mandate, MandateStatus, Transaction
from app.signing import verify_mandate
from app.simulator import (
    SimulationStep,
    SimulationSummary,
    pending_transactions,
    revoke_mandate,
    seed_dataset,
    simulate,
)


@pytest.fixture
def db(monkeypatch):
    """An isolated in-memory database wired into the simulator's engine."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("app.simulator.db_engine", engine)
    return engine


@pytest.fixture
def seeded(db):
    with Session(db) as session:
        mandate, count = seed_dataset(session)
        return mandate.id, count


# --- seeding ----------------------------------------------------------------


def test_seeding_loads_the_dataset(db, seeded):
    mandate_id, count = seeded
    assert count > 250

    with Session(db) as session:
        assert len(pending_transactions(session, mandate_id)) == count


def test_seeded_mandate_is_signed(db, seeded):
    mandate_id, _ = seeded
    with Session(db) as session:
        mandate = session.get(Mandate, mandate_id)
        assert mandate.signature
        assert verify_mandate(mandate)


def test_seeding_records_creation_in_the_chain(db, seeded):
    with Session(db) as session:
        events = session.exec(
            select(Decision).where(Decision.event_type == EventType.MANDATE_CREATED)
        ).all()
        assert len(events) == 1


def test_seeding_is_idempotent(db, seeded):
    mandate_id, count = seeded
    with Session(db) as session:
        _, second = seed_dataset(session)
        assert second == 0
        assert len(pending_transactions(session, mandate_id)) == count


def test_seeding_creates_no_decisions(db, seeded):
    """The simulator produces decisions live; seeding must not pre-empt it."""
    with Session(db) as session:
        decided = session.exec(
            select(Decision).where(Decision.event_type == EventType.DECISION)
        ).all()
        assert decided == []


# --- a clean run ------------------------------------------------------------


def test_simulation_decides_every_transaction(db, seeded):
    mandate_id, count = seeded
    steps = [s for s in simulate(mandate_id) if isinstance(s, SimulationStep)]
    assert len(steps) == count


def test_simulation_ends_with_a_summary(db, seeded):
    mandate_id, count = seeded
    items = list(simulate(mandate_id))
    summary = items[-1]
    assert isinstance(summary, SimulationSummary)
    assert summary.total == count
    assert summary.allowed + summary.blocked == count
    assert summary.chain_valid


def test_running_totals_are_consistent(db, seeded):
    mandate_id, _ = seeded
    for step in simulate(mandate_id):
        if isinstance(step, SimulationStep):
            assert step.allowed_so_far + step.blocked_so_far == step.index


def test_simulation_matches_the_dataset_labels(db, seeded):
    """The streamed verdicts agree with the ground-truth labels from Day 5."""
    from app.synthetic import generate_dataset

    mandate_id, _ = seeded
    expected = {c.transaction.id: c.expected_reason for c in generate_dataset().cases}

    for step in simulate(mandate_id):
        if isinstance(step, SimulationStep):
            assert step.reason_code == expected[step.transaction_id]


def test_rerunning_decides_nothing_new(db, seeded):
    """Decided transactions are not re-evaluated, so the log cannot double up."""
    mandate_id, count = seeded
    list(simulate(mandate_id))

    second = [s for s in simulate(mandate_id) if isinstance(s, SimulationStep)]
    assert second == []


def test_limit_stops_early(db, seeded):
    mandate_id, _ = seeded
    steps = [s for s in simulate(mandate_id, limit=10) if isinstance(s, SimulationStep)]
    assert len(steps) == 10


def test_unknown_mandate_raises(db):
    with pytest.raises(LookupError):
        list(simulate("no-such-mandate"))


# --- revocation -------------------------------------------------------------


def test_revoking_sets_status(db, seeded):
    mandate_id, _ = seeded
    with Session(db) as session:
        assert revoke_mandate(session, mandate_id).status is MandateStatus.revoked


def test_revocation_is_recorded_in_the_chain(db, seeded):
    mandate_id, _ = seeded
    with Session(db) as session:
        revoke_mandate(session, mandate_id)
        events = session.exec(
            select(Decision).where(Decision.event_type == EventType.MANDATE_REVOKED)
        ).all()
        assert len(events) == 1
        assert events[0].mandate_id == mandate_id


def test_revocation_preserves_the_signature(db, seeded):
    """Revoking is legitimate and must not make the mandate look forged."""
    mandate_id, _ = seeded
    with Session(db) as session:
        mandate = revoke_mandate(session, mandate_id)
        assert verify_mandate(mandate)


def test_revoking_twice_is_idempotent(db, seeded):
    mandate_id, _ = seeded
    with Session(db) as session:
        revoke_mandate(session, mandate_id)
        revoke_mandate(session, mandate_id)
        events = session.exec(
            select(Decision).where(Decision.event_type == EventType.MANDATE_REVOKED)
        ).all()
        assert len(events) == 1


def test_revoking_an_unknown_mandate_raises(db):
    with Session(db) as session:
        with pytest.raises(LookupError):
            revoke_mandate(session, "no-such-mandate")


def test_everything_blocks_after_revocation(db, seeded):
    mandate_id, _ = seeded
    with Session(db) as session:
        revoke_mandate(session, mandate_id)

    for step in simulate(mandate_id):
        if isinstance(step, SimulationStep):
            assert step.result == "BLOCK"
            assert step.reason_code == ReasonCode.MANDATE_REVOKED


# --- the headline: revoking mid-stream --------------------------------------


def test_mid_batch_revocation_takes_effect_on_the_next_transaction(db, seeded):
    """Revoke after 20 steps; step 21 onward must be MANDATE_REVOKED.

    This is the guarantee that distinguishes a real kill switch from a
    cosmetic one, and it only holds because the simulator re-reads mandate
    state for every transaction rather than caching it.
    """
    mandate_id, _ = seeded
    revoke_after = 20

    before, after = [], []
    for step in simulate(mandate_id):
        if not isinstance(step, SimulationStep):
            continue

        if step.index <= revoke_after:
            before.append(step)
            if step.index == revoke_after:
                with Session(db) as session:
                    revoke_mandate(session, mandate_id)
        else:
            after.append(step)

    assert len(before) == revoke_after
    assert after, "simulation ended before the revocation could be observed"

    # Not one transaction slipped through after the revoke.
    assert all(s.reason_code == ReasonCode.MANDATE_REVOKED for s in after)
    assert all(s.mandate_status == "revoked" for s in after)


def test_no_transaction_is_allowed_after_the_revoke_point(db, seeded):
    mandate_id, _ = seeded
    revoke_after = 5
    allowed_after_revoke = []

    for step in simulate(mandate_id):
        if not isinstance(step, SimulationStep):
            continue
        if step.index == revoke_after:
            with Session(db) as session:
                revoke_mandate(session, mandate_id)
        elif step.index > revoke_after and step.result == "ALLOW":
            allowed_after_revoke.append(step.transaction_id)

    assert allowed_after_revoke == []


def test_revocation_lands_in_the_chain_between_the_decisions(db, seeded):
    """The audit trail shows exactly when the revoke happened."""
    mandate_id, _ = seeded
    revoke_after = 8

    for step in simulate(mandate_id, limit=20):
        if isinstance(step, SimulationStep) and step.index == revoke_after:
            with Session(db) as session:
                revoke_mandate(session, mandate_id)

    with Session(db) as session:
        entries = session.exec(select(Decision).order_by(Decision.seq)).all()

    types = [e.event_type for e in entries]
    revoke_index = types.index(EventType.MANDATE_REVOKED)

    # Decisions on both sides: the revoke is genuinely mid-stream.
    assert EventType.DECISION in types[:revoke_index]
    assert EventType.DECISION in types[revoke_index + 1 :]

    after = [e for e in entries[revoke_index + 1 :] if e.event_type == EventType.DECISION]
    assert all(e.reason_code == ReasonCode.MANDATE_REVOKED for e in after)


def test_chain_stays_valid_across_a_mid_batch_revocation(db, seeded):
    """Lifecycle events are chained like any other entry."""
    mandate_id, _ = seeded

    for step in simulate(mandate_id, limit=30):
        if isinstance(step, SimulationStep) and step.index == 10:
            with Session(db) as session:
                revoke_mandate(session, mandate_id)

    with Session(db) as session:
        report = verify_chain(session)
    assert report.valid, report.reason


def test_tampering_with_a_revocation_event_is_detected(db, seeded):
    """Erasing the revoke from the log does not go unnoticed."""
    mandate_id, _ = seeded
    with Session(db) as session:
        revoke_mandate(session, mandate_id)
        event = session.exec(
            select(Decision).where(Decision.event_type == EventType.MANDATE_REVOKED)
        ).one()
        event.event_type = EventType.DECISION  # try to disguise it
        session.add(event)
        session.commit()

        report = verify_chain(session)

    assert not report.valid
    assert "content altered" in report.reason
