"""The two deliberate failures, handled rather than hidden.

1. The compiler is unsure it understood a money-bounding term, so the mandate
   is created unenforceable and a human is asked, instead of the system acting
   on a guess.
2. Someone with database access edits the audit log, and verification catches
   it — including the case where they recompute the hash to cover their tracks.
"""

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.amend import AmendmentError, amend_mandate, confirm_mandate
from app.audit import append_decision, verify_chain
from app.compiler import AmbiguityFlag, critical_ambiguities, draft_to_mandate
from app.engine import ReasonCode, evaluate
from app.models import Decision, DecisionResult, EventType, Mandate, MandateStatus
from app.signing import verify_mandate
from app.tamper import (
    TamperUnavailable,
    delete_an_entry,
    flip_a_block_to_allow,
    forge_hash_after_edit,
)
from tests.test_audit import add_txn, allow, block, build_chain
from tests.test_compiler import make_draft
from tests.test_engine import make_txn


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def flag(field: str) -> AmbiguityFlag:
    return AmbiguityFlag(
        field=field,
        issue="not stated in the policy",
        assumed_value="something",
        clarifying_question="What did you mean?",
    )


# === failure 1: the compiler is not sure ====================================


@pytest.mark.parametrize(
    "field",
    [
        "merchant_allowlist",
        "category_exclusions",
        "amount_cap_per_txn_rupees",
        "amount_cap_period_rupees",
        "period",
    ],
)
def test_guessing_a_money_bounding_term_blocks_activation(field):
    draft = make_draft(ambiguities=[flag(field)])
    assert critical_ambiguities(draft)
    mandate = draft_to_mandate(draft, "u", "a")
    assert mandate.status is MandateStatus.pending_confirmation


@pytest.mark.parametrize("field", ["time_window_start", "time_window_end", "validity_days"])
def test_guessing_a_non_critical_term_does_not_block(field):
    """Recorded, but not worth stopping the world for."""
    draft = make_draft(ambiguities=[flag(field)])
    assert not critical_ambiguities(draft)
    assert draft_to_mandate(draft, "u", "a").status is MandateStatus.active


def test_an_unambiguous_policy_is_active_immediately():
    assert draft_to_mandate(make_draft(), "u", "a").status is MandateStatus.active


def test_a_pending_mandate_is_still_signed():
    """Unenforceable is not the same as untrustworthy."""
    from app.signing import sign_mandate

    mandate = sign_mandate(draft_to_mandate(make_draft(ambiguities=[flag("period")]), "u", "a"))
    assert verify_mandate(mandate)


def test_the_engine_refuses_to_enforce_a_pending_mandate():
    """The whole point: it fails closed, and says why."""
    mandate = draft_to_mandate(make_draft(ambiguities=[flag("merchant_allowlist")]), "u", "a")
    txn = make_txn(mandate_id=mandate.id, amount=100, merchant="zepto")

    outcome = evaluate(mandate, txn)
    assert outcome.result is DecisionResult.block
    assert outcome.reason_code == ReasonCode.MANDATE_NOT_CONFIRMED


def test_pending_beats_expiry_in_the_reason_given():
    """Report the real problem: it was never enforceable, not that it lapsed."""
    from datetime import timedelta

    from app.models import utcnow

    mandate = draft_to_mandate(make_draft(ambiguities=[flag("period")]), "u", "a")
    mandate.valid_until = utcnow() - timedelta(days=1)
    txn = make_txn(mandate_id=mandate.id, merchant="zepto")

    assert evaluate(mandate, txn).reason_code == ReasonCode.MANDATE_NOT_CONFIRMED


# --- resolving it: confirm --------------------------------------------------


def store(session, mandate):
    from app.signing import sign_mandate

    sign_mandate(mandate)
    session.add(mandate)
    session.commit()
    session.refresh(mandate)
    return mandate


def test_confirming_activates_the_mandate(session):
    mandate = store(session, draft_to_mandate(make_draft(ambiguities=[flag("period")]), "u", "a"))
    assert confirm_mandate(session, mandate.id).status is MandateStatus.active


def test_confirmation_is_recorded_in_the_chain(session):
    mandate = store(session, draft_to_mandate(make_draft(ambiguities=[flag("period")]), "u", "a"))
    confirm_mandate(session, mandate.id)

    events = session.exec(
        select(Decision).where(Decision.event_type == EventType.MANDATE_CONFIRMED)
    ).all()
    assert len(events) == 1


def test_confirmed_mandate_now_enforces(session):
    from datetime import timedelta

    mandate = store(session, draft_to_mandate(make_draft(ambiguities=[flag("period")]), "u", "a"))
    confirm_mandate(session, mandate.id)

    # Inside the mandate's validity window and its 06:00-23:00 time window,
    # so the only thing that could block this is the status gate.
    moment = (mandate.valid_from + timedelta(days=1)).replace(hour=12, minute=0)
    txn = make_txn(mandate_id=mandate.id, amount=100, merchant="zepto", timestamp=moment)
    assert evaluate(mandate, txn).allowed


def test_a_revoked_mandate_cannot_be_confirmed(session):
    mandate = store(session, draft_to_mandate(make_draft(ambiguities=[flag("period")]), "u", "a"))
    mandate.status = MandateStatus.revoked
    session.add(mandate)
    session.commit()

    with pytest.raises(AmendmentError, match="revoked"):
        confirm_mandate(session, mandate.id)


# --- resolving it: amend ----------------------------------------------------


def test_amending_corrects_the_term_and_activates(session):
    mandate = store(
        session, draft_to_mandate(make_draft(ambiguities=[flag("merchant_allowlist")]), "u", "a")
    )
    amended = amend_mandate(session, mandate.id, {"merchant_allowlist": ["blinkit"]})

    assert amended.merchant_allowlist == ["blinkit"]
    assert amended.status is MandateStatus.active


def test_amending_bumps_the_version_and_resigns(session):
    mandate = store(session, draft_to_mandate(make_draft(), "u", "a"))
    original_version, original_sig = mandate.version, mandate.signature

    amended = amend_mandate(session, mandate.id, {"frequency_cap": 3})

    assert amended.version == original_version + 1
    assert amended.signature != original_sig
    assert verify_mandate(amended)


def test_amendment_is_recorded_in_the_chain(session):
    mandate = store(session, draft_to_mandate(make_draft(), "u", "a"))
    amend_mandate(session, mandate.id, {"frequency_cap": 3})

    events = session.exec(
        select(Decision).where(Decision.event_type == EventType.MANDATE_AMENDED)
    ).all()
    assert len(events) == 1


def test_amended_caps_are_converted_to_paise(session):
    mandate = store(session, draft_to_mandate(make_draft(), "u", "a"))
    amended = amend_mandate(session, mandate.id, {"amount_cap_per_txn_rupees": 250})
    assert amended.amount_cap_per_txn == 25_000


def test_amendment_normalizes_case(session):
    mandate = store(session, draft_to_mandate(make_draft(), "u", "a"))
    amended = amend_mandate(session, mandate.id, {"merchant_allowlist": ["  ZePto ", "Blinkit"]})
    assert amended.merchant_allowlist == ["zepto", "blinkit"]


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"amount_cap_per_txn_rupees": 0}, "positive"),
        ({"amount_cap_per_txn_rupees": 99_99_99_999}, "ceiling"),
        ({"merchant_allowlist": []}, "cannot be empty"),
        ({"time_window_start": "25:00"}, "valid 24-hour"),
        ({"frequency_cap": -1}, "positive"),
        ({"frequency_cap": 999_999}, "implausibly high"),
    ],
)
def test_the_gate_applies_to_humans_too(session, changes, message):
    """A person resolving a guess still cannot write an incoherent mandate."""
    mandate = store(session, draft_to_mandate(make_draft(), "u", "a"))
    with pytest.raises(AmendmentError, match=message):
        amend_mandate(session, mandate.id, changes)


def test_amendment_rejects_a_cap_above_the_period_cap(session):
    mandate = store(session, draft_to_mandate(make_draft(), "u", "a"))
    with pytest.raises(AmendmentError, match="would exceed"):
        amend_mandate(session, mandate.id, {"amount_cap_per_txn_rupees": 9_000})


def test_two_interdependent_fields_can_be_corrected_together(session):
    """Coherence is checked after all changes, not after each one."""
    mandate = store(session, draft_to_mandate(make_draft(), "u", "a"))
    amended = amend_mandate(
        session,
        mandate.id,
        {"amount_cap_per_txn_rupees": 9_000, "amount_cap_period_rupees": 20_000},
    )
    assert amended.amount_cap_per_txn == 900_000


@pytest.mark.parametrize("field", ["id", "principal_id", "status", "version", "signature"])
def test_identity_fields_are_not_amendable(session, field):
    """Changing these would be a different grant, not a correction."""
    mandate = store(session, draft_to_mandate(make_draft(), "u", "a"))
    with pytest.raises(AmendmentError, match="not amendable"):
        amend_mandate(session, mandate.id, {field: "x"})


def test_a_revoked_mandate_cannot_be_amended_back_into_service(session):
    mandate = store(session, draft_to_mandate(make_draft(), "u", "a"))
    mandate.status = MandateStatus.revoked
    session.add(mandate)
    session.commit()

    with pytest.raises(AmendmentError, match="revoked"):
        amend_mandate(session, mandate.id, {"frequency_cap": 5})


def test_failed_amendment_leaves_no_trace_in_the_chain(session):
    mandate = store(session, draft_to_mandate(make_draft(), "u", "a"))
    with pytest.raises(AmendmentError):
        amend_mandate(session, mandate.id, {"merchant_allowlist": []})

    events = session.exec(
        select(Decision).where(Decision.event_type == EventType.MANDATE_AMENDED)
    ).all()
    assert events == []


# === failure 2: someone edits the audit log =================================


def test_flipping_a_block_to_allow_is_caught(session):
    build_chain(session, 6)
    result = flip_a_block_to_allow(session)

    report = verify_chain(session)
    assert not report.valid
    assert report.broken_at_seq == result.seq
    assert "content altered" in report.reason


def test_deleting_an_entry_is_caught(session):
    build_chain(session, 6)
    result = delete_an_entry(session)

    report = verify_chain(session)
    assert not report.valid
    assert "sequence gap" in report.reason
    assert result.seq not in [d.seq for d in session.exec(select(Decision))]


def test_forging_the_hash_is_still_caught_at_the_next_entry(session):
    """The subtle case: the edited entry verifies, its successor does not."""
    build_chain(session, 6)
    result = forge_hash_after_edit(session)

    report = verify_chain(session)
    assert not report.valid
    assert "broken link" in report.reason
    assert report.broken_at_seq == result.seq + 1


def test_tamper_reports_what_changed(session):
    build_chain(session, 6)
    result = flip_a_block_to_allow(session)

    assert result.before["result"] == DecisionResult.block.value
    assert result.after["result"] == DecisionResult.allow.value
    assert result.before["reason_code"] != result.after["reason_code"]


def test_the_chain_was_valid_before_the_attack(session):
    """Guards against the demo being a false positive."""
    build_chain(session, 6)
    assert verify_chain(session).valid
    flip_a_block_to_allow(session)
    assert not verify_chain(session).valid


def test_flip_needs_a_blocked_decision_to_exist(session):
    for _ in range(3):
        append_decision(session, add_txn(session), allow())
    with pytest.raises(TamperUnavailable, match="no blocked decision"):
        flip_a_block_to_allow(session)


@pytest.mark.parametrize("attack", [delete_an_entry, forge_hash_after_edit])
def test_attacks_refuse_on_a_short_log(session, attack):
    append_decision(session, add_txn(session), block())
    with pytest.raises(TamperUnavailable, match="at least 3"):
        attack(session)
