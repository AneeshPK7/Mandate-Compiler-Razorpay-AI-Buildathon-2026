"""Deterministic policy engine.

This module is the enforcement path. It contains no LLM calls, no network I/O,
and no database access — it is a pure function of (Mandate, Transaction, Usage).
That separation is deliberate: an LLM compiles English into a Mandate, but only
verifiable code decides whether money moves.

Rules are evaluated in a fixed priority order (see RULE_ORDER) and the first
failing rule short-circuits. Ordering is part of the contract: a transaction
against a revoked mandate always reports MANDATE_REVOKED, never a downstream
reason like AMOUNT_CAP_EXCEEDED, so the audit trail stays stable and explainable.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.models import DecisionResult, Mandate, MandateStatus, Period, Transaction


class ReasonCode:
    ALLOWED = "ALLOWED"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    MANDATE_NOT_CONFIRMED = "MANDATE_NOT_CONFIRMED"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    MANDATE_NOT_YET_VALID = "MANDATE_NOT_YET_VALID"
    AMOUNT_CAP_EXCEEDED = "AMOUNT_CAP_EXCEEDED"
    MERCHANT_NOT_ALLOWED = "MERCHANT_NOT_ALLOWED"
    CATEGORY_EXCLUDED = "CATEGORY_EXCLUDED"
    OUTSIDE_TIME_WINDOW = "OUTSIDE_TIME_WINDOW"
    PERIOD_CAP_EXCEEDED = "PERIOD_CAP_EXCEEDED"
    FREQUENCY_CAP_EXCEEDED = "FREQUENCY_CAP_EXCEEDED"


# Priority order. Status/validity gates come first (they invalidate the mandate
# wholesale), then per-transaction attributes, then cumulative counters — which
# depend on prior state and so are the most expensive to reason about.
RULE_ORDER = [
    "status",
    "validity_window",
    "amount_cap_per_txn",
    "merchant_allowlist",
    "category_exclusions",
    "time_window",
    "amount_cap_period",
    "frequency_cap",
]


@dataclass(frozen=True)
class Usage:
    """Consumption already recorded against a mandate for the current period.

    Only ALLOWed transactions contribute: a blocked attempt moves no money and
    must not consume the principal's budget, otherwise a burst of blocked
    attempts could starve legitimate spending.
    """

    amount_spent: int = 0
    transaction_count: int = 0


@dataclass(frozen=True)
class EngineDecision:
    result: DecisionResult
    reason_code: str
    rule_triggered: str
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.result is DecisionResult.allow


def _naive(dt: datetime) -> datetime:
    """Drop tzinfo so naive DB values and aware inputs stay comparable."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _parse_hhmm(value: str) -> time:
    hours, minutes = value.strip().split(":")
    return time(int(hours), int(minutes))


def _norm(value: str) -> str:
    return value.strip().casefold()


def within_time_window(moment: time, start: time, end: time) -> bool:
    """Inclusive-start, inclusive-end window check.

    Supports overnight windows where start > end (e.g. 22:00-06:00), which read
    as a single span crossing midnight rather than an empty range.
    """
    if start <= end:
        return start <= moment <= end
    return moment >= start or moment <= end


def period_bounds(moment: datetime, period: Period) -> tuple[datetime, datetime]:
    """Half-open [start, end) bounds of the period containing `moment`.

    Weeks are ISO weeks (Monday-start). Months are calendar months.
    """
    moment = _naive(moment)
    day = moment.date()

    if period is Period.day:
        start_date = day
        end_date = day + timedelta(days=1)
    elif period is Period.week:
        start_date = day - timedelta(days=day.weekday())
        end_date = start_date + timedelta(days=7)
    elif period is Period.month:
        start_date = day.replace(day=1)
        end_date = (
            date(day.year + 1, 1, 1)
            if day.month == 12
            else date(day.year, day.month + 1, 1)
        )
    else:  # pragma: no cover - Period is exhaustive
        raise ValueError(f"unsupported period: {period}")

    return (
        datetime.combine(start_date, time.min),
        datetime.combine(end_date, time.min),
    )


def evaluate(
    mandate: Mandate,
    transaction: Transaction,
    usage: Usage | None = None,
) -> EngineDecision:
    """Evaluate one transaction against one mandate. Pure and side-effect free."""
    usage = usage or Usage()
    moment = _naive(transaction.timestamp)

    # 1. Status gate — revocation must win over every other outcome.
    if mandate.status is MandateStatus.revoked:
        return EngineDecision(
            DecisionResult.block,
            ReasonCode.MANDATE_REVOKED,
            "status",
            "mandate was revoked by the principal",
        )
    # An unconfirmed mandate fails closed. It is reported before expiry so the
    # reason names the real problem: it was never enforceable to begin with.
    if mandate.status is MandateStatus.pending_confirmation:
        return EngineDecision(
            DecisionResult.block,
            ReasonCode.MANDATE_NOT_CONFIRMED,
            "status",
            "compiler flagged an ambiguous field; awaiting human confirmation",
        )
    if mandate.status is MandateStatus.expired:
        return EngineDecision(
            DecisionResult.block,
            ReasonCode.MANDATE_EXPIRED,
            "status",
            "mandate is marked expired",
        )

    # 2. Validity window.
    if moment < _naive(mandate.valid_from):
        return EngineDecision(
            DecisionResult.block,
            ReasonCode.MANDATE_NOT_YET_VALID,
            "validity_window",
            f"transaction at {moment.isoformat()} precedes valid_from "
            f"{_naive(mandate.valid_from).isoformat()}",
        )
    if moment > _naive(mandate.valid_until):
        return EngineDecision(
            DecisionResult.block,
            ReasonCode.MANDATE_EXPIRED,
            "validity_window",
            f"transaction at {moment.isoformat()} is past valid_until "
            f"{_naive(mandate.valid_until).isoformat()}",
        )

    # 3. Per-transaction amount cap.
    if transaction.amount > mandate.amount_cap_per_txn:
        return EngineDecision(
            DecisionResult.block,
            ReasonCode.AMOUNT_CAP_EXCEEDED,
            "amount_cap_per_txn",
            f"amount {transaction.amount} exceeds per-transaction cap "
            f"{mandate.amount_cap_per_txn}",
        )

    # 4. Merchant allowlist. An empty allowlist means "no merchant approved" —
    # fail closed, since an omitted allowlist is far more likely a compile error
    # than a deliberate grant of unlimited merchant access.
    allowed_merchants = {_norm(m) for m in mandate.merchant_allowlist}
    if _norm(transaction.merchant) not in allowed_merchants:
        return EngineDecision(
            DecisionResult.block,
            ReasonCode.MERCHANT_NOT_ALLOWED,
            "merchant_allowlist",
            f"merchant '{transaction.merchant}' is not in the allowlist",
        )

    # 5. Category exclusions — checked after the allowlist so that an excluded
    # category bought at an allowed merchant still reports CATEGORY_EXCLUDED.
    excluded = {_norm(c) for c in mandate.category_exclusions}
    if _norm(transaction.category) in excluded:
        return EngineDecision(
            DecisionResult.block,
            ReasonCode.CATEGORY_EXCLUDED,
            "category_exclusions",
            f"category '{transaction.category}' is excluded by this mandate",
        )

    # 6. Time-of-day window.
    start = _parse_hhmm(mandate.time_window_start)
    end = _parse_hhmm(mandate.time_window_end)
    if not within_time_window(moment.time(), start, end):
        return EngineDecision(
            DecisionResult.block,
            ReasonCode.OUTSIDE_TIME_WINDOW,
            "time_window",
            f"time {moment.time().strftime('%H:%M')} is outside "
            f"{mandate.time_window_start}-{mandate.time_window_end}",
        )

    # 7. Cumulative spend cap for the period.
    projected = usage.amount_spent + transaction.amount
    if projected > mandate.amount_cap_period:
        return EngineDecision(
            DecisionResult.block,
            ReasonCode.PERIOD_CAP_EXCEEDED,
            "amount_cap_period",
            f"{usage.amount_spent} already spent this {mandate.period.value}; "
            f"this {transaction.amount} would reach {projected}, over the "
            f"{mandate.amount_cap_period} cap",
        )

    # 8. Frequency cap for the period.
    if usage.transaction_count + 1 > mandate.frequency_cap:
        return EngineDecision(
            DecisionResult.block,
            ReasonCode.FREQUENCY_CAP_EXCEEDED,
            "frequency_cap",
            f"{usage.transaction_count} transactions already this "
            f"{mandate.period.value}; cap is {mandate.frequency_cap}",
        )

    return EngineDecision(
        DecisionResult.allow,
        ReasonCode.ALLOWED,
        "none",
        "all rules satisfied",
    )
