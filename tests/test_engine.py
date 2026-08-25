from datetime import datetime, time, timedelta

import pytest

from app.engine import (
    ReasonCode,
    Usage,
    evaluate,
    period_bounds,
    within_time_window,
)
from app.models import DecisionResult, Mandate, MandateStatus, Period, Transaction


def make_mandate(**overrides) -> Mandate:
    """A permissive baseline mandate; each test tightens exactly one rule."""
    defaults = dict(
        id="m1",
        principal_id="user-1",
        agent_id="agent-1",
        amount_cap_per_txn=100_000,
        amount_cap_period=1_000_000,
        period=Period.week,
        merchant_allowlist=["zepto", "swiggy", "bigbasket"],
        category_exclusions=["alcohol", "tobacco"],
        time_window_start="06:00",
        time_window_end="23:00",
        frequency_cap=100,
        valid_from=datetime(2026, 1, 1, 0, 0),
        valid_until=datetime(2026, 12, 31, 23, 59),
        status=MandateStatus.active,
        version=1,
    )
    defaults.update(overrides)
    return Mandate(**defaults)


def make_txn(**overrides) -> Transaction:
    defaults = dict(
        id="t1",
        mandate_id="m1",
        amount=500,
        merchant="zepto",
        category="groceries",
        timestamp=datetime(2026, 6, 10, 12, 0),  # a Wednesday, midday
    )
    defaults.update(overrides)
    return Transaction(**defaults)


# --- baseline ---------------------------------------------------------------


def test_in_policy_transaction_is_allowed():
    decision = evaluate(make_mandate(), make_txn())
    assert decision.result is DecisionResult.allow
    assert decision.reason_code == ReasonCode.ALLOWED
    assert decision.allowed


# --- rule 1: status ---------------------------------------------------------


def test_revoked_mandate_blocks():
    decision = evaluate(make_mandate(status=MandateStatus.revoked), make_txn())
    assert decision.reason_code == ReasonCode.MANDATE_REVOKED
    assert decision.rule_triggered == "status"


def test_revocation_outranks_other_violations():
    """A revoked mandate must report revocation, not a downstream rule."""
    mandate = make_mandate(status=MandateStatus.revoked, amount_cap_per_txn=100)
    decision = evaluate(mandate, make_txn(amount=99_999, merchant="amazon"))
    assert decision.reason_code == ReasonCode.MANDATE_REVOKED


def test_expired_status_blocks():
    decision = evaluate(make_mandate(status=MandateStatus.expired), make_txn())
    assert decision.reason_code == ReasonCode.MANDATE_EXPIRED


# --- rule 2: validity window ------------------------------------------------


def test_transaction_before_valid_from_blocks():
    mandate = make_mandate(valid_from=datetime(2026, 7, 1))
    decision = evaluate(mandate, make_txn(timestamp=datetime(2026, 6, 10, 12, 0)))
    assert decision.reason_code == ReasonCode.MANDATE_NOT_YET_VALID


def test_transaction_after_valid_until_blocks():
    mandate = make_mandate(valid_until=datetime(2026, 5, 1))
    decision = evaluate(mandate, make_txn(timestamp=datetime(2026, 6, 10, 12, 0)))
    assert decision.reason_code == ReasonCode.MANDATE_EXPIRED
    assert decision.rule_triggered == "validity_window"


def test_validity_bounds_are_inclusive():
    mandate = make_mandate(
        valid_from=datetime(2026, 6, 10, 12, 0),
        valid_until=datetime(2026, 6, 10, 12, 0),
    )
    decision = evaluate(mandate, make_txn(timestamp=datetime(2026, 6, 10, 12, 0)))
    assert decision.allowed


# --- rule 3: per-transaction amount cap -------------------------------------


def test_amount_over_per_txn_cap_blocks():
    decision = evaluate(make_mandate(amount_cap_per_txn=1000), make_txn(amount=1001))
    assert decision.reason_code == ReasonCode.AMOUNT_CAP_EXCEEDED
    assert "1001" in decision.detail


def test_amount_exactly_at_cap_is_allowed():
    decision = evaluate(make_mandate(amount_cap_per_txn=1000), make_txn(amount=1000))
    assert decision.allowed


# --- rule 4: merchant allowlist ---------------------------------------------


def test_merchant_not_in_allowlist_blocks():
    decision = evaluate(make_mandate(), make_txn(merchant="amazon"))
    assert decision.reason_code == ReasonCode.MERCHANT_NOT_ALLOWED


def test_merchant_matching_is_case_and_whitespace_insensitive():
    decision = evaluate(make_mandate(), make_txn(merchant="  ZePTo "))
    assert decision.allowed


def test_empty_allowlist_fails_closed():
    """An omitted allowlist is treated as 'nothing approved', never 'all approved'."""
    decision = evaluate(make_mandate(merchant_allowlist=[]), make_txn())
    assert decision.reason_code == ReasonCode.MERCHANT_NOT_ALLOWED


# --- rule 5: category exclusions --------------------------------------------


def test_excluded_category_blocks():
    decision = evaluate(make_mandate(), make_txn(category="alcohol"))
    assert decision.reason_code == ReasonCode.CATEGORY_EXCLUDED


def test_excluded_category_at_allowed_merchant_blocks():
    """The headline edge case: an approved grocer selling an excluded category."""
    decision = evaluate(make_mandate(), make_txn(merchant="bigbasket", category="alcohol"))
    assert decision.reason_code == ReasonCode.CATEGORY_EXCLUDED
    assert decision.rule_triggered == "category_exclusions"


def test_category_matching_is_case_insensitive():
    decision = evaluate(make_mandate(), make_txn(category="Alcohol"))
    assert decision.reason_code == ReasonCode.CATEGORY_EXCLUDED


# --- rule 6: time window ----------------------------------------------------


def test_before_window_blocks():
    decision = evaluate(make_mandate(), make_txn(timestamp=datetime(2026, 6, 10, 5, 59)))
    assert decision.reason_code == ReasonCode.OUTSIDE_TIME_WINDOW


def test_after_window_blocks():
    decision = evaluate(make_mandate(), make_txn(timestamp=datetime(2026, 6, 10, 23, 1)))
    assert decision.reason_code == ReasonCode.OUTSIDE_TIME_WINDOW


def test_window_bounds_are_inclusive():
    for hour, minute in [(6, 0), (23, 0)]:
        decision = evaluate(
            make_mandate(), make_txn(timestamp=datetime(2026, 6, 10, hour, minute))
        )
        assert decision.allowed, f"{hour}:{minute} should be inside the window"


@pytest.mark.parametrize(
    "moment,expected",
    [
        (time(23, 0), True),
        (time(2, 0), True),
        (time(5, 59), True),
        (time(6, 1), False),
        (time(12, 0), False),
    ],
)
def test_overnight_window_spans_midnight(moment, expected):
    assert within_time_window(moment, time(22, 0), time(6, 0)) is expected


def test_overnight_window_end_to_end():
    mandate = make_mandate(time_window_start="22:00", time_window_end="06:00")
    assert evaluate(mandate, make_txn(timestamp=datetime(2026, 6, 10, 23, 30))).allowed
    assert evaluate(mandate, make_txn(timestamp=datetime(2026, 6, 10, 3, 0))).allowed
    assert not evaluate(mandate, make_txn(timestamp=datetime(2026, 6, 10, 12, 0))).allowed


# --- rule 7: cumulative period cap ------------------------------------------


def test_cumulative_cap_exceeded_blocks():
    mandate = make_mandate(amount_cap_period=2000)
    decision = evaluate(mandate, make_txn(amount=500), Usage(amount_spent=1600))
    assert decision.reason_code == ReasonCode.PERIOD_CAP_EXCEEDED


def test_cumulative_cap_exactly_reached_is_allowed():
    mandate = make_mandate(amount_cap_period=2000)
    decision = evaluate(mandate, make_txn(amount=400), Usage(amount_spent=1600))
    assert decision.allowed


def test_cumulative_detail_explains_the_math():
    mandate = make_mandate(amount_cap_period=2000)
    decision = evaluate(mandate, make_txn(amount=900), Usage(amount_spent=1600))
    assert "1600" in decision.detail and "2500" in decision.detail


# --- rule 8: frequency cap --------------------------------------------------


def test_frequency_cap_exceeded_blocks():
    mandate = make_mandate(frequency_cap=3)
    decision = evaluate(mandate, make_txn(), Usage(transaction_count=3))
    assert decision.reason_code == ReasonCode.FREQUENCY_CAP_EXCEEDED


def test_final_permitted_transaction_is_allowed():
    mandate = make_mandate(frequency_cap=3)
    decision = evaluate(mandate, make_txn(), Usage(transaction_count=2))
    assert decision.allowed


# --- rule precedence --------------------------------------------------------


def test_amount_cap_outranks_merchant_and_category():
    mandate = make_mandate(amount_cap_per_txn=100)
    txn = make_txn(amount=5000, merchant="amazon", category="alcohol")
    assert evaluate(mandate, txn).reason_code == ReasonCode.AMOUNT_CAP_EXCEEDED


def test_merchant_outranks_time_window():
    txn = make_txn(merchant="amazon", timestamp=datetime(2026, 6, 10, 2, 0))
    assert evaluate(make_mandate(), txn).reason_code == ReasonCode.MERCHANT_NOT_ALLOWED


def test_period_cap_outranks_frequency_cap():
    mandate = make_mandate(amount_cap_period=1000, frequency_cap=1)
    decision = evaluate(
        mandate, make_txn(amount=900), Usage(amount_spent=500, transaction_count=5)
    )
    assert decision.reason_code == ReasonCode.PERIOD_CAP_EXCEEDED


# --- period bounds ----------------------------------------------------------


def test_week_bounds_start_monday():
    start, end = period_bounds(datetime(2026, 6, 10, 12, 0), Period.week)  # Wednesday
    assert start == datetime(2026, 6, 8)  # Monday
    assert end == datetime(2026, 6, 15)


def test_day_bounds():
    start, end = period_bounds(datetime(2026, 6, 10, 23, 59), Period.day)
    assert start == datetime(2026, 6, 10)
    assert end == datetime(2026, 6, 11)


def test_month_bounds():
    start, end = period_bounds(datetime(2026, 6, 10), Period.month)
    assert start == datetime(2026, 6, 1)
    assert end == datetime(2026, 7, 1)


def test_month_bounds_roll_over_year_end():
    start, end = period_bounds(datetime(2026, 12, 15), Period.month)
    assert start == datetime(2026, 12, 1)
    assert end == datetime(2027, 1, 1)


def test_monday_midnight_belongs_to_its_own_week():
    monday = datetime(2026, 6, 8, 0, 0)
    start, _ = period_bounds(monday, Period.week)
    assert start == monday


# --- determinism ------------------------------------------------------------


def test_evaluation_is_repeatable():
    mandate, txn, usage = make_mandate(), make_txn(), Usage(amount_spent=100)
    results = {
        (d.result, d.reason_code, d.rule_triggered, d.detail)
        for d in (evaluate(mandate, txn, usage) for _ in range(50))
    }
    assert len(results) == 1


def test_evaluate_does_not_mutate_inputs():
    mandate, txn = make_mandate(), make_txn()
    before = (mandate.model_dump(), txn.model_dump())
    evaluate(mandate, txn, Usage(amount_spent=10, transaction_count=1))
    assert (mandate.model_dump(), txn.model_dump()) == before


def test_timezone_aware_timestamp_is_comparable():
    """Aware inputs must not raise against naive DB datetimes."""
    from datetime import timezone

    txn = make_txn(timestamp=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc))
    assert evaluate(make_mandate(), txn).allowed
