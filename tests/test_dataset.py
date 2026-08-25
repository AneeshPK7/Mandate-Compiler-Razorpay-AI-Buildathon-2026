"""Replay the labelled dataset through the real engine.

This is a differential test. app/synthetic.py constructs each transaction to
violate at most one rule and labels it accordingly, without importing any of
the engine's evaluation logic. app/engine.py decides independently. Every
disagreement is a bug in one of them.

It catches a class of error unit tests miss: unit tests assert the behaviour I
thought to check, whereas 250+ constructed scenarios replayed in sequence also
exercise rule interaction, period rollover, and running-total accumulation.
"""

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.audit import append_decision, verify_chain
from app.engine import ReasonCode, evaluate
from app.models import DecisionResult
from app.synthetic import FREQUENCY_CAP, generate_dataset
from app.usage import compute_usage


@pytest.fixture
def dataset():
    # Function-scoped deliberately: the cases hold ORM instances, which become
    # detached once a test's session closes.
    return generate_dataset()


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def replay(session: Session, dataset):
    """Run every case through the engine in timestamp order, recording results."""
    session.add(dataset.mandate)
    session.commit()

    results = []
    for case in sorted(dataset.cases, key=lambda c: c.transaction.timestamp):
        txn = case.transaction
        session.add(txn)
        session.commit()

        usage = compute_usage(session, dataset.mandate, txn.timestamp)
        outcome = evaluate(dataset.mandate, txn, usage)
        append_decision(session, txn, outcome)
        results.append((case, outcome))
    return results


# --- dataset shape ----------------------------------------------------------


def test_dataset_is_large_enough(dataset):
    assert len(dataset) >= 250


def test_generation_is_deterministic():
    a, b = generate_dataset(), generate_dataset()
    assert [c.transaction.id for c in a.cases] == [c.transaction.id for c in b.cases]
    assert [c.transaction.amount for c in a.cases] == [c.transaction.amount for c in b.cases]
    assert [c.expected_reason for c in a.cases] == [c.expected_reason for c in b.cases]


def test_seed_changes_the_data():
    a, b = generate_dataset(seed=1), generate_dataset(seed=2)
    assert [c.transaction.amount for c in a.cases] != [c.transaction.amount for c in b.cases]


def test_every_reason_code_is_represented(dataset):
    """A dataset missing a reason code would silently under-test the engine."""
    expected = {
        ReasonCode.ALLOWED,
        ReasonCode.AMOUNT_CAP_EXCEEDED,
        ReasonCode.MERCHANT_NOT_ALLOWED,
        ReasonCode.CATEGORY_EXCLUDED,
        ReasonCode.OUTSIDE_TIME_WINDOW,
        ReasonCode.PERIOD_CAP_EXCEEDED,
        ReasonCode.FREQUENCY_CAP_EXCEEDED,
        ReasonCode.MANDATE_EXPIRED,
        ReasonCode.MANDATE_NOT_YET_VALID,
    }
    assert expected <= set(dataset.counts())


def test_most_activity_is_legitimate(dataset):
    """A realistic mix: an agent that is blocked most of the time is not realistic."""
    allowed = sum(1 for c in dataset.cases if c.expected_allowed)
    assert 0.45 < allowed / len(dataset) < 0.80


def test_transaction_ids_are_unique(dataset):
    ids = [c.transaction.id for c in dataset.cases]
    assert len(set(ids)) == len(ids)


def test_amounts_are_positive_integers_in_paise(dataset):
    for case in dataset.cases:
        assert isinstance(case.transaction.amount, int)
        assert case.transaction.amount > 0


# --- the differential check -------------------------------------------------


def test_engine_agrees_with_every_label(session, dataset):
    """The headline assertion: 250+ constructed cases, zero disagreements."""
    mismatches = [
        (
            case.transaction.id,
            case.scenario,
            f"expected {case.expected_reason}, got {outcome.reason_code}",
        )
        for case, outcome in replay(session, dataset)
        if outcome.reason_code != case.expected_reason
    ]
    assert not mismatches, f"{len(mismatches)} mismatches: {mismatches[:10]}"


def test_allow_block_verdicts_agree(session, dataset):
    for case, outcome in replay(session, dataset):
        assert outcome.result is case.expected_result, (
            f"{case.transaction.id} ({case.scenario}): "
            f"expected {case.expected_result}, got {outcome.result}"
        )


# --- specific scenarios survived the replay ---------------------------------


def test_cumulative_cap_actually_fires(session, dataset):
    fired = [
        c
        for c, o in replay(session, dataset)
        if o.reason_code == ReasonCode.PERIOD_CAP_EXCEEDED
    ]
    assert len(fired) >= 5


def test_frequency_cap_fires_after_exactly_the_cap(session, dataset):
    """The 16th transaction in a frequency week blocks; the 15th does not."""
    results = replay(session, dataset)
    freq_weeks = {}
    for case, outcome in results:
        if case.scenario.startswith("frequency_"):
            week = case.transaction.timestamp.isocalendar()[:2]
            freq_weeks.setdefault(week, []).append((case, outcome))

    assert freq_weeks, "dataset contains no frequency-cap week"
    for week, entries in freq_weeks.items():
        allowed = [o for _, o in entries if o.allowed]
        assert len(allowed) == FREQUENCY_CAP, f"week {week}: {len(allowed)} allowed"


def test_boundary_cases_land_on_the_right_side(session, dataset):
    by_scenario = {c.scenario: o for c, o in replay(session, dataset)}
    assert by_scenario["amount_exactly_at_cap"].allowed
    assert not by_scenario["amount_one_paisa_over_cap"].allowed
    assert by_scenario["time_at_window_open"].allowed
    assert by_scenario["time_at_window_close"].allowed
    assert not by_scenario["time_one_hour_before_open"].allowed
    assert by_scenario["cumulative_exact_cap"].allowed
    assert not by_scenario["cumulative_over_by_one_paisa"].allowed


def test_excluded_category_at_allowed_merchant_reports_category(session, dataset):
    """Not MERCHANT_NOT_ALLOWED — the merchant was fine, the item wasn't."""
    for case, outcome in replay(session, dataset):
        if case.scenario == "excluded_category_at_allowed_merchant":
            assert outcome.reason_code == ReasonCode.CATEGORY_EXCLUDED


# --- the replay also produces a valid audit chain ---------------------------


def test_replay_produces_an_intact_audit_chain(session, dataset):
    replay(session, dataset)
    report = verify_chain(session)
    assert report.valid, report.reason
    assert report.entries_checked == len(dataset)


def test_audit_chain_records_the_same_verdicts(session, dataset):
    results = replay(session, dataset)
    from sqlmodel import select

    from app.models import Decision

    decisions = session.exec(select(Decision).order_by(Decision.seq)).all()
    assert len(decisions) == len(results)
    for (case, outcome), decision in zip(results, decisions):
        assert decision.result == outcome.result.value
        assert decision.reason_code == outcome.reason_code
