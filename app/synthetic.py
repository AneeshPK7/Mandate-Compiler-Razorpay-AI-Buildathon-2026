"""Synthetic transaction generator with exact ground-truth labels.

Generation is deterministic (seeded), not LLM-driven. An LLM would give more
lexical variety, but the value here is *exact* labels: if the expected outcome
were itself a guess, replaying the dataset would prove nothing. A fixed
vocabulary plus a seeded RNG gives reproducible data whose correct answer is
known by construction — which lets the dataset act as a second, independent
check on the policy engine (see tests/test_dataset.py).

How the labels stay exact
-------------------------
Every transaction is built to violate *at most one* rule. When generating a
merchant violation, the amount, category, time and validity are all deliberately
kept in-policy, so exactly one rule can fire and the expected reason code is
known without reasoning about precedence.

Stateful rules (cumulative cap, frequency cap) are handled by giving them their
own weeks, where every other dimension is clean and the generator counts the
crossover point deliberately. No part of this module imports the engine's
evaluation logic, so agreement between the two is meaningful.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.engine import ReasonCode
from app.models import DecisionResult, Mandate, MandateStatus, Period, Transaction

RUPEE = 100  # paise

# --- the mandate every generated transaction is evaluated against ------------

MANDATE_ID = "mandate-demo-001"
PRINCIPAL_ID = "user-aneesh"
AGENT_ID = "shopping-agent-1"

CAP_PER_TXN = 1000 * RUPEE
CAP_PER_PERIOD = 5000 * RUPEE
FREQUENCY_CAP = 15
WINDOW = ("06:00", "23:00")

ALLOWED_MERCHANTS = ["zepto", "swiggy", "bigbasket", "blinkit"]
BLOCKED_MERCHANTS = ["amazon", "flipkart", "myntra", "nykaa", "bookmyshow"]
ALLOWED_CATEGORIES = ["groceries", "produce", "dairy", "household", "snacks", "bakery"]
EXCLUDED_CATEGORIES = ["alcohol", "tobacco"]

# The dataset spans several months of agent activity, starting from a Monday.
# Weeks are the unit because the mandate's period is weekly: caps reset each
# week, which is what lets the dataset contain many transactions while still
# respecting a realistic ₹5,000/week budget.
START = datetime(2026, 1, 5, 0, 0)  # a Monday
WEEKS = 26


@dataclass
class LabelledTransaction:
    """A transaction paired with the outcome it must produce."""

    transaction: Transaction
    expected_result: DecisionResult
    expected_reason: str
    scenario: str  # which situation this case was built to exercise

    @property
    def expected_allowed(self) -> bool:
        return self.expected_result is DecisionResult.allow


@dataclass
class Dataset:
    mandate: Mandate
    cases: list[LabelledTransaction] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.cases)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for case in self.cases:
            out[case.expected_reason] = out.get(case.expected_reason, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

    def scenario_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for case in self.cases:
            out[case.scenario] = out.get(case.scenario, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def build_mandate() -> Mandate:
    """The mandate the dataset is generated against.

    Validity starts one week after START so the generator can produce
    genuine MANDATE_NOT_YET_VALID cases in week 0.
    """
    return Mandate(
        id=MANDATE_ID,
        principal_id=PRINCIPAL_ID,
        agent_id=AGENT_ID,
        amount_cap_per_txn=CAP_PER_TXN,
        amount_cap_period=CAP_PER_PERIOD,
        period=Period.week,
        merchant_allowlist=list(ALLOWED_MERCHANTS),
        category_exclusions=list(EXCLUDED_CATEGORIES),
        time_window_start=WINDOW[0],
        time_window_end=WINDOW[1],
        frequency_cap=FREQUENCY_CAP,
        valid_from=START + timedelta(weeks=1),
        valid_until=START + timedelta(weeks=WEEKS - 1),
        status=MandateStatus.active,
        version=1,
    )


class _Builder:
    """Generates transactions inside one week, keeping every unstated field legal."""

    def __init__(self, rng: random.Random, week_start: datetime):
        self.rng = rng
        self.week_start = week_start
        self._n = 0

    def _timestamp(
        self,
        hour: int | None = None,
        day: int | None = None,
        minute: int | None = None,
    ) -> datetime:
        day = self.rng.randrange(0, 7) if day is None else day
        # Default to comfortably inside the 06:00-23:00 window.
        hour = self.rng.randrange(7, 22) if hour is None else hour
        # Boundary cases pass minute=0 explicitly: a random minute added to
        # hour 23 would land at 23:xx, i.e. outside the window, silently
        # turning an at-the-boundary case into an over-the-boundary one.
        minute = self.rng.randrange(0, 60) if minute is None else minute
        return self.week_start + timedelta(days=day, hours=hour, minutes=minute)

    def sequential(self, index: int, per_day: int = 3, **kwargs) -> Transaction:
        """A transaction whose timestamp strictly increases with `index`.

        Stateful scenarios (cumulative cap, frequency cap) depend on the order
        transactions are evaluated in, and the engine evaluates in timestamp
        order. Random times would make generation order and timestamp order
        disagree, so the labels would be assigned to the wrong transactions.
        Spread across days at fixed hours, all inside the 06:00-23:00 window.
        """
        day, slot = divmod(index, per_day)
        assert day < 7, f"index {index} overflows the week at per_day={per_day}"
        hour = 8 + slot * (14 // max(per_day, 1))
        return self.make(day=day, hour=hour, **kwargs)

    def make(
        self,
        *,
        amount: int | None = None,
        merchant: str | None = None,
        category: str | None = None,
        hour: int | None = None,
        day: int | None = None,
        minute: int | None = None,
        timestamp: datetime | None = None,
    ) -> Transaction:
        self._n += 1
        return Transaction(
            id=f"txn-{self.week_start:%Y%m%d}-{self._n:03d}",
            mandate_id=MANDATE_ID,
            amount=amount if amount is not None else self.rng.randrange(50, 400) * RUPEE,
            merchant=merchant or self.rng.choice(ALLOWED_MERCHANTS),
            category=category or self.rng.choice(ALLOWED_CATEGORIES),
            timestamp=timestamp or self._timestamp(hour=hour, day=day, minute=minute),
        )


def _allow(txn: Transaction, scenario: str) -> LabelledTransaction:
    return LabelledTransaction(txn, DecisionResult.allow, ReasonCode.ALLOWED, scenario)


def _block(txn: Transaction, reason: str, scenario: str) -> LabelledTransaction:
    return LabelledTransaction(txn, DecisionResult.block, reason, scenario)


def _week_not_yet_valid(b: _Builder) -> list[LabelledTransaction]:
    """Week 0: before valid_from. Everything else about these is in-policy."""
    return [
        _block(b.make(), ReasonCode.MANDATE_NOT_YET_VALID, "before_valid_from")
        for _ in range(8)
    ]


def _week_baseline(b: _Builder, n: int) -> list[LabelledTransaction]:
    """Ordinary in-policy activity. Small amounts keep the period cap clear."""
    return [_allow(b.make(amount=b.rng.randrange(50, 300) * RUPEE), "in_policy") for _ in range(n)]


def _week_stateless_violations(b: _Builder) -> list[LabelledTransaction]:
    """One violation per transaction, every other dimension kept legal."""
    cases: list[LabelledTransaction] = []

    # Interleave a few clean transactions so the week isn't pathological.
    for _ in range(3):
        cases.append(_allow(b.make(amount=100 * RUPEE), "in_policy"))

    # Over the per-transaction cap (still under the period cap on its own).
    for _ in range(2):
        amount = b.rng.randrange(CAP_PER_TXN + RUPEE, CAP_PER_TXN + 500 * RUPEE, RUPEE)
        cases.append(
            _block(b.make(amount=amount), ReasonCode.AMOUNT_CAP_EXCEEDED, "over_per_txn_cap")
        )

    # Merchant outside the allowlist.
    for _ in range(3):
        cases.append(
            _block(
                b.make(amount=150 * RUPEE, merchant=b.rng.choice(BLOCKED_MERCHANTS)),
                ReasonCode.MERCHANT_NOT_ALLOWED,
                "merchant_not_allowed",
            )
        )

    # Excluded category at an ALLOWED merchant — the grocer-sells-alcohol case.
    for _ in range(3):
        cases.append(
            _block(
                b.make(
                    amount=120 * RUPEE,
                    merchant=b.rng.choice(ALLOWED_MERCHANTS),
                    category=b.rng.choice(EXCLUDED_CATEGORIES),
                ),
                ReasonCode.CATEGORY_EXCLUDED,
                "excluded_category_at_allowed_merchant",
            )
        )

    # Outside the time window, both sides of it. Minutes are pinned: hour 23
    # with a random minute could land on 23:00, which is *inside* the window.
    for hour, minute in ((2, 15), (4, 30), (23, 30), (5, 45)):
        cases.append(
            _block(
                b.make(amount=100 * RUPEE, hour=hour, minute=minute),
                ReasonCode.OUTSIDE_TIME_WINDOW,
                "outside_time_window",
            )
        )

    return cases


def _week_cumulative_cap(b: _Builder) -> list[LabelledTransaction]:
    """Walk the running total up to the period cap, then cross it.

    Amounts stay under the per-transaction cap and the count stays under the
    frequency cap, so the cumulative rule is the only one that can fire.
    """
    cases: list[LabelledTransaction] = []
    spent = 0
    amount = 900 * RUPEE  # under CAP_PER_TXN

    for i in range(9):
        txn = b.sequential(i, per_day=2, amount=amount)
        if spent + amount <= CAP_PER_PERIOD:
            spent += amount
            cases.append(_allow(txn, "cumulative_under_cap"))
        else:
            cases.append(
                _block(txn, ReasonCode.PERIOD_CAP_EXCEEDED, "cumulative_cap_exceeded")
            )
    return cases


def _week_cumulative_boundary(b: _Builder) -> list[LabelledTransaction]:
    """Land exactly on the period cap, then exceed it by one paisa."""
    cases: list[LabelledTransaction] = []
    spent = 0

    # Each instalment sits exactly at the per-transaction cap, so only the
    # cumulative rule can fire. Five of them land exactly on the period cap.
    instalments = CAP_PER_PERIOD // CAP_PER_TXN
    for i in range(instalments):
        cases.append(
            _allow(b.sequential(i, per_day=1, amount=CAP_PER_TXN), "cumulative_exact_cap")
        )
        spent += CAP_PER_TXN

    assert spent == CAP_PER_PERIOD, "boundary week must land exactly on the cap"

    cases.append(
        _block(
            b.sequential(instalments, per_day=1, amount=1),
            ReasonCode.PERIOD_CAP_EXCEEDED,
            "cumulative_over_by_one_paisa",
        )
    )
    return cases


def _week_frequency_cap(b: _Builder) -> list[LabelledTransaction]:
    """Fill the frequency cap with tiny amounts, then exceed it.

    Amounts are small enough that the period cap is never in play.
    """
    cases: list[LabelledTransaction] = []
    for i in range(FREQUENCY_CAP + 5):
        txn = b.sequential(i, per_day=3, amount=50 * RUPEE)
        if i < FREQUENCY_CAP:
            cases.append(_allow(txn, "frequency_under_cap"))
        else:
            cases.append(
                _block(txn, ReasonCode.FREQUENCY_CAP_EXCEEDED, "frequency_cap_exceeded")
            )
    return cases


def _week_boundaries(b: _Builder) -> list[LabelledTransaction]:
    """Exact-boundary cases, where off-by-one bugs live."""
    return [
        _allow(b.make(amount=CAP_PER_TXN), "amount_exactly_at_cap"),
        _block(
            b.make(amount=CAP_PER_TXN + 1),
            ReasonCode.AMOUNT_CAP_EXCEEDED,
            "amount_one_paisa_over_cap",
        ),
        _allow(b.make(amount=100 * RUPEE, hour=6, day=0, minute=0), "time_at_window_open"),
        _allow(b.make(amount=100 * RUPEE, hour=23, day=1, minute=0), "time_at_window_close"),
        _block(
            b.make(amount=100 * RUPEE, hour=5, day=2, minute=59),
            ReasonCode.OUTSIDE_TIME_WINDOW,
            "time_one_hour_before_open",
        ),
        _allow(b.make(amount=100 * RUPEE, merchant="ZEPTO"), "merchant_case_insensitive"),
        _allow(b.make(amount=100 * RUPEE, merchant="  swiggy  "), "merchant_whitespace"),
        _block(
            b.make(amount=100 * RUPEE, merchant="bigbasket", category="Alcohol"),
            ReasonCode.CATEGORY_EXCLUDED,
            "category_case_insensitive",
        ),
    ]


def _week_expired(b: _Builder, mandate: Mandate) -> list[LabelledTransaction]:
    """After valid_until. Everything else about these is in-policy."""
    cases = []
    for i in range(8):
        ts = mandate.valid_until + timedelta(days=1 + i % 5, hours=10)
        cases.append(
            _block(b.make(amount=100 * RUPEE, timestamp=ts), ReasonCode.MANDATE_EXPIRED, "after_valid_until")
        )
    return cases


# Weeks with a designed scenario. Every other week is ordinary in-policy
# activity, which keeps the mix realistic: most agent spending is legitimate.
SPECIAL_WEEKS = {
    2: _week_stateless_violations,
    4: _week_cumulative_cap,
    6: _week_frequency_cap,
    8: _week_cumulative_boundary,
    10: _week_boundaries,
    13: _week_stateless_violations,
    16: _week_cumulative_cap,
    19: _week_frequency_cap,
    21: _week_stateless_violations,
    22: _week_boundaries,
    24: _week_stateless_violations,
}


def generate_dataset(seed: int = 20260905) -> Dataset:
    """Build the labelled dataset. Deterministic for a given seed."""
    rng = random.Random(seed)
    mandate = build_mandate()
    cases: list[LabelledTransaction] = []

    # Week 0 sits before valid_from.
    cases += _week_not_yet_valid(_Builder(rng, START))

    for week in range(1, WEEKS - 1):
        builder = _Builder(rng, START + timedelta(weeks=week))
        if week in SPECIAL_WEEKS:
            cases += SPECIAL_WEEKS[week](builder)
        else:
            # Baseline weeks stay well inside both the frequency and period caps.
            cases += _week_baseline(builder, rng.randrange(8, 13))

    # Trailing weeks fall past valid_until.
    cases += _week_expired(_Builder(rng, START + timedelta(weeks=WEEKS)), mandate)

    return Dataset(mandate=mandate, cases=cases)
