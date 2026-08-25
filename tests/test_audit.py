"""Audit chain tests, including each tamper scenario the chain claims to catch."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from app.audit import (
    GENESIS_HASH,
    append_decision,
    chain_head,
    compute_hash,
    verify_chain,
)
from app.engine import EngineDecision, ReasonCode
from app.models import Decision, DecisionResult, Transaction


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def add_txn(session: Session, amount: int = 500) -> Transaction:
    txn = Transaction(
        mandate_id="m1",
        amount=amount,
        merchant="zepto",
        category="groceries",
        timestamp=datetime(2026, 6, 10, 12, 0),
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


def allow() -> EngineDecision:
    return EngineDecision(DecisionResult.allow, ReasonCode.ALLOWED, "none", "ok")


def block() -> EngineDecision:
    return EngineDecision(
        DecisionResult.block, ReasonCode.AMOUNT_CAP_EXCEEDED, "amount_cap_per_txn", "over"
    )


def build_chain(session: Session, n: int = 5) -> list[Decision]:
    out = []
    for i in range(n):
        txn = add_txn(session, amount=100 * (i + 1))
        out.append(append_decision(session, txn, allow() if i % 2 else block()))
    return out


# --- construction -----------------------------------------------------------


def test_empty_chain_is_valid(session):
    report = verify_chain(session)
    assert report.valid
    assert report.entries_checked == 0
    assert report.head == GENESIS_HASH


def test_empty_chain_head_is_genesis(session):
    assert chain_head(session) == GENESIS_HASH


def test_first_entry_links_to_genesis(session):
    decision = append_decision(session, add_txn(session), allow())
    assert decision.prev_hash == GENESIS_HASH
    assert decision.audit_hash


def test_entries_link_to_their_predecessor(session):
    decisions = build_chain(session, 4)
    for earlier, later in zip(decisions, decisions[1:]):
        assert later.prev_hash == earlier.audit_hash


def test_sequence_is_monotonic(session):
    decisions = build_chain(session, 5)
    assert [d.seq for d in decisions] == sorted(d.seq for d in decisions)
    assert len(set(d.seq for d in decisions)) == 5


def test_intact_chain_verifies(session):
    build_chain(session, 6)
    report = verify_chain(session)
    assert report.valid, report.reason
    assert report.entries_checked == 6


def test_head_tracks_the_last_entry(session):
    decisions = build_chain(session, 3)
    assert chain_head(session) == decisions[-1].audit_hash


def test_identical_decisions_get_distinct_hashes(session):
    """seq is inside the payload, so repeated identical verdicts still differ."""
    t1, t2 = add_txn(session), add_txn(session)
    a = append_decision(session, t1, allow())
    b = append_decision(session, t2, allow())
    assert a.audit_hash != b.audit_hash


# --- attack 1: modification -------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("result", DecisionResult.allow.value),
        ("reason_code", "ALLOWED"),
        ("rule_triggered", "none"),
        ("transaction_id", "someone-elses-txn"),
    ],
)
def test_editing_an_entry_is_detected(session, field, value):
    decisions = build_chain(session, 5)
    target = decisions[2]

    setattr(target, field, value)
    session.add(target)
    session.commit()

    report = verify_chain(session)
    assert not report.valid
    assert report.broken_at_seq == target.seq
    assert "content altered" in report.reason


def test_flipping_a_block_to_an_allow_is_detected(session):
    """The headline case: rewriting history to authorize a blocked payment."""
    decisions = build_chain(session, 5)
    target = next(d for d in decisions if d.result == DecisionResult.block.value)

    target.result = DecisionResult.allow.value
    target.reason_code = "ALLOWED"
    session.add(target)
    session.commit()

    report = verify_chain(session)
    assert not report.valid
    assert report.broken_at_seq == target.seq


def test_editing_the_last_entry_is_detected(session):
    """No entry is 'safe' merely because nothing follows it."""
    decisions = build_chain(session, 4)
    last = decisions[-1]
    last.reason_code = "TAMPERED"
    session.add(last)
    session.commit()

    assert verify_chain(session).broken_at_seq == last.seq


def test_recomputing_the_hash_after_editing_still_breaks_the_link(session):
    """A partial cover-up — fixing one hash — orphans the following entry."""
    decisions = build_chain(session, 5)
    target = decisions[1]

    target.reason_code = "TAMPERED"
    target.audit_hash = compute_hash(target)  # attacker patches its own hash
    session.add(target)
    session.commit()

    report = verify_chain(session)
    assert not report.valid
    # The edited entry now self-verifies, so the break surfaces at its successor.
    assert report.broken_at_seq == decisions[2].seq
    assert "broken link" in report.reason


def test_garbage_in_the_result_column_is_reported_not_raised(session):
    """A verifier must survive hostile data, not crash on it.

    Regression: `result` was originally a SQL enum column, so a value outside
    the enum made verification raise LookupError — turning a detectable tamper
    into a crash, which is a strictly worse failure mode.
    """
    build_chain(session, 3)
    session.execute(
        text("UPDATE decision SET result = 'TOTALLY-BOGUS' WHERE seq = 2")
    )
    session.commit()

    report = verify_chain(session)  # must not raise
    assert not report.valid
    assert report.broken_at_seq == 2


def test_raw_sql_tampering_is_detected(session):
    """The realistic attack: editing the database outside the application."""
    decisions = build_chain(session, 4)
    blocked = next(d for d in decisions if d.result == DecisionResult.block.value)

    session.execute(
        text(
            "UPDATE decision SET result = 'ALLOW', reason_code = 'ALLOWED' "
            "WHERE seq = :seq"
        ),
        {"seq": blocked.seq},
    )
    session.commit()

    report = verify_chain(session)
    assert not report.valid
    assert report.broken_at_seq == blocked.seq
    assert "content altered" in report.reason


def test_rewriting_an_entry_to_its_existing_values_is_not_a_break(session):
    """No false positives: an UPDATE that changes nothing must still verify."""
    decisions = build_chain(session, 4)
    allowed = next(d for d in decisions if d.result == DecisionResult.allow.value)

    session.execute(
        text(
            "UPDATE decision SET result = 'ALLOW', reason_code = 'ALLOWED' "
            "WHERE seq = :seq"
        ),
        {"seq": allowed.seq},
    )
    session.commit()

    assert verify_chain(session).valid is True


# --- attack 2: deletion -----------------------------------------------------


def test_deleting_an_entry_is_detected(session):
    decisions = build_chain(session, 5)
    removed = decisions[2]
    session.delete(removed)
    session.commit()

    report = verify_chain(session)
    assert not report.valid
    assert "sequence gap" in report.reason


def test_deleting_several_entries_reports_the_count(session):
    decisions = build_chain(session, 6)
    for d in decisions[2:4]:
        session.delete(d)
    session.commit()

    report = verify_chain(session)
    assert not report.valid
    assert "2 entries missing" in report.reason


def test_deleting_the_last_entry_is_not_caught_by_gap_detection(session):
    """Honest limitation: truncating the tail leaves no gap behind.

    Detecting this requires an externally anchored head hash — see the
    limitation documented in app/audit.py.
    """
    decisions = build_chain(session, 5)
    session.delete(decisions[-1])
    session.commit()

    assert verify_chain(session).valid is True


# --- attack 3: insertion ----------------------------------------------------


def test_inserting_a_forged_entry_is_detected(session):
    build_chain(session, 4)

    txn = add_txn(session)
    forged = Decision(
        transaction_id=txn.id,
        result=DecisionResult.allow.value,
        reason_code="ALLOWED",
        rule_triggered="none",
        prev_hash="0" * 64,
        audit_hash="f" * 64,
    )
    session.add(forged)
    session.commit()

    report = verify_chain(session)
    assert not report.valid
    assert report.broken_at_seq == forged.seq


def test_forged_entry_with_correct_hash_still_breaks_the_link(session):
    """Even a well-formed forgery fails: it doesn't point at the real head."""
    build_chain(session, 4)
    txn = add_txn(session)

    forged = Decision(
        transaction_id=txn.id,
        result=DecisionResult.allow.value,
        reason_code="ALLOWED",
        rule_triggered="none",
        prev_hash="a" * 64,  # not the current head
        audit_hash="",
    )
    session.add(forged)
    session.commit()
    session.refresh(forged)
    forged.audit_hash = compute_hash(forged)
    session.add(forged)
    session.commit()

    report = verify_chain(session)
    assert not report.valid
    assert "broken link" in report.reason


# --- attack 4: reordering ---------------------------------------------------


def test_swapping_two_entries_is_detected(session):
    decisions = build_chain(session, 5)
    a, b = decisions[1], decisions[3]

    a_fields = (a.transaction_id, a.result, a.reason_code, a.rule_triggered, a.created_at)
    b_fields = (b.transaction_id, b.result, b.reason_code, b.rule_triggered, b.created_at)
    a.transaction_id, a.result, a.reason_code, a.rule_triggered, a.created_at = b_fields
    b.transaction_id, b.result, b.reason_code, b.rule_triggered, b.created_at = a_fields
    session.add(a)
    session.add(b)
    session.commit()

    assert verify_chain(session).valid is False


def test_backdating_an_entry_is_detected(session):
    decisions = build_chain(session, 4)
    target = decisions[2]
    target.created_at = target.created_at - timedelta(days=30)
    session.add(target)
    session.commit()

    report = verify_chain(session)
    assert not report.valid
    assert report.broken_at_seq == target.seq


# --- reporting --------------------------------------------------------------


def test_report_names_the_first_break_when_several_exist(session):
    decisions = build_chain(session, 6)
    for target in (decisions[4], decisions[2]):
        target.reason_code = "TAMPERED"
        session.add(target)
    session.commit()

    assert verify_chain(session).broken_at_seq == decisions[2].seq


def test_report_renders_readably(session):
    build_chain(session, 3)
    assert "chain OK" in str(verify_chain(session))

    d = session.exec(select(Decision).order_by(Decision.seq)).first()
    d.reason_code = "TAMPERED"
    session.add(d)
    session.commit()
    assert "chain BROKEN" in str(verify_chain(session))
