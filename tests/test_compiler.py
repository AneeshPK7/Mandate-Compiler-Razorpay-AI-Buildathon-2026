"""Tests for the compiler's deterministic trust boundary.

No API key needed: these exercise `validate_draft` and `draft_to_mandate`,
which are the code that decides whether LLM output is safe to sign. The live
model behaviour is exercised separately by scripts/eval_compiler.py.
"""

import pytest

from app.compiler import (
    PAISE_PER_RUPEE,
    AmbiguityFlag,
    MandateDraft,
    ValidationError,
    compile_policy,
    draft_to_mandate,
    validate_draft,
)
from app.models import MandateStatus, Period


def make_draft(**overrides) -> MandateDraft:
    defaults = dict(
        amount_cap_per_txn_rupees=500,
        amount_cap_period_rupees=2000,
        period="week",
        merchant_allowlist=["zepto", "swiggy"],
        category_exclusions=["alcohol"],
        time_window_start="06:00",
        time_window_end="23:00",
        frequency_cap=10,
        validity_days=30,
        ambiguities=[],
        interpretation_notes="",
    )
    defaults.update(overrides)
    return MandateDraft(**defaults)


# --- the gate accepts sane drafts -------------------------------------------


def test_valid_draft_has_no_problems():
    assert validate_draft(make_draft()) == []


# --- amount bounds ----------------------------------------------------------


@pytest.mark.parametrize("amount", [0, -1, -5000])
def test_non_positive_per_txn_cap_rejected(amount):
    problems = validate_draft(make_draft(amount_cap_per_txn_rupees=amount))
    assert any("amount_cap_per_txn must be positive" in p for p in problems)


def test_absurd_per_txn_cap_rejected():
    """A hallucinated cap must not become an enforceable spending limit."""
    problems = validate_draft(
        make_draft(amount_cap_per_txn_rupees=99_99_99_999, amount_cap_period_rupees=99_99_99_999)
    )
    assert any("compiler ceiling" in p for p in problems)


def test_per_txn_cap_above_period_cap_rejected():
    problems = validate_draft(
        make_draft(amount_cap_per_txn_rupees=5000, amount_cap_period_rupees=2000)
    )
    assert any("exceeds the week cap" in p for p in problems)


def test_per_txn_cap_equal_to_period_cap_is_fine():
    """Legitimate when the text gives only one number."""
    assert validate_draft(
        make_draft(amount_cap_per_txn_rupees=2000, amount_cap_period_rupees=2000)
    ) == []


# --- time format ------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_time", ["6:00", "25:00", "06:60", "0600", "morning", "", "06:0", "-1:00"]
)
def test_malformed_times_rejected(bad_time):
    problems = validate_draft(make_draft(time_window_start=bad_time))
    assert any("time_window_start" in p for p in problems)


@pytest.mark.parametrize("good_time", ["00:00", "06:00", "23:59", "12:30"])
def test_wellformed_times_accepted(good_time):
    assert validate_draft(make_draft(time_window_start=good_time)) == []


# --- allowlist / exclusions -------------------------------------------------


def test_empty_allowlist_rejected():
    """Fails closed, and says why — an empty allowlist blocks everything."""
    problems = validate_draft(make_draft(merchant_allowlist=[]))
    assert any("block all spending" in p for p in problems)


def test_merchant_also_excluded_as_category_rejected():
    problems = validate_draft(
        make_draft(merchant_allowlist=["zepto"], category_exclusions=["zepto"])
    )
    assert any("both merchant_allowlist and" in p for p in problems)


# --- frequency and validity -------------------------------------------------


def test_non_positive_frequency_cap_rejected():
    assert any("frequency_cap must be positive" in p for p in validate_draft(make_draft(frequency_cap=0)))


def test_implausible_frequency_cap_rejected():
    assert any("implausibly high" in p for p in validate_draft(make_draft(frequency_cap=100_000)))


def test_excessive_validity_rejected():
    assert any("validity_days" in p for p in validate_draft(make_draft(validity_days=5000)))


def test_multiple_problems_all_reported():
    """The gate reports every problem, not just the first."""
    problems = validate_draft(
        make_draft(
            amount_cap_per_txn_rupees=-1,
            time_window_start="nope",
            frequency_cap=0,
            merchant_allowlist=[],
        )
    )
    assert len(problems) >= 4


# --- draft -> mandate conversion --------------------------------------------


def test_rupees_converted_to_paise():
    mandate = draft_to_mandate(make_draft(), "user-1", "agent-1")
    assert mandate.amount_cap_per_txn == 500 * PAISE_PER_RUPEE
    assert mandate.amount_cap_period == 2000 * PAISE_PER_RUPEE


def test_conversion_normalizes_case_and_whitespace():
    draft = make_draft(
        merchant_allowlist=["  ZePto ", "BigBasket"], category_exclusions=["Alcohol"]
    )
    mandate = draft_to_mandate(draft, "user-1", "agent-1")
    assert mandate.merchant_allowlist == ["zepto", "bigbasket"]
    assert mandate.category_exclusions == ["alcohol"]


def test_new_mandate_is_active_and_versioned():
    mandate = draft_to_mandate(make_draft(), "user-1", "agent-1")
    assert mandate.status is MandateStatus.active
    assert mandate.version == 1
    assert mandate.signature is None  # signing happens downstream


def test_validity_window_spans_requested_days():
    mandate = draft_to_mandate(make_draft(validity_days=7), "user-1", "agent-1")
    assert (mandate.valid_until - mandate.valid_from).days == 7


def test_period_maps_to_enum():
    assert draft_to_mandate(make_draft(period="month"), "u", "a").period is Period.month


def test_invalid_draft_cannot_become_a_mandate():
    """The gate is enforced at conversion, not merely advisory."""
    with pytest.raises(ValidationError):
        draft_to_mandate(make_draft(merchant_allowlist=[]), "user-1", "agent-1")


def test_conversion_error_names_the_problem():
    with pytest.raises(ValidationError, match="amount_cap_per_txn must be positive"):
        draft_to_mandate(make_draft(amount_cap_per_txn_rupees=0), "user-1", "agent-1")


# --- compile_policy plumbing (model stubbed) --------------------------------


class FakeResponse:
    def __init__(self, draft):
        self.parsed_output = draft


class FakeClient:
    """Stands in for anthropic.Anthropic to test plumbing without a live call."""

    def __init__(self, draft):
        self._draft = draft
        self.calls = []
        self.messages = self

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self._draft)


def test_compile_policy_returns_parsed_draft():
    draft = make_draft()
    assert compile_policy("spend on groceries", client=FakeClient(draft)) is draft


def test_compile_policy_sends_the_policy_text():
    client = FakeClient(make_draft())
    compile_policy("  only zepto up to 500  ", client=client)
    sent = client.calls[0]
    assert sent["messages"][0]["content"] == "only zepto up to 500"
    assert sent["output_format"] is MandateDraft


@pytest.mark.parametrize("empty", ["", "   ", "\n"])
def test_compile_policy_rejects_empty_input(empty):
    with pytest.raises(ValueError):
        compile_policy(empty, client=FakeClient(make_draft()))


def test_ambiguities_survive_to_the_draft():
    draft = make_draft(
        ambiguities=[
            AmbiguityFlag(
                field="time_window_start",
                issue="no time of day stated",
                assumed_value="00:00",
                clarifying_question="Should the agent be able to spend overnight?",
            )
        ]
    )
    assert draft.ambiguities[0].field == "time_window_start"
    # Ambiguities are advisory — they never block conversion on their own.
    assert validate_draft(draft) == []
