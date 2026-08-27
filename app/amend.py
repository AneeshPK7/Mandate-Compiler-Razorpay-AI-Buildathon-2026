"""Resolving an ambiguity: amendment and confirmation.

When the compiler flags a field it had to guess on, the mandate is created in
`pending_confirmation` and the engine refuses to enforce it. A human then either
confirms the assumption as-is, or amends the term to what they actually meant.

Both paths produce an audited, re-signed mandate. Amendment bumps `version`,
which is inside the signed payload, so an amended mandate is cryptographically
distinguishable from the original rather than quietly overwriting it.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from app.audit import append_mandate_event
from app.compiler import (
    MAX_FREQUENCY_CAP,
    MAX_RUPEES_PER_PERIOD,
    MAX_RUPEES_PER_TXN,
    PAISE_PER_RUPEE,
    _is_valid_hhmm,
)
from app.models import EventType, Mandate, MandateStatus
from app.signing import sign_mandate

# Terms a human may correct when resolving an ambiguity. Deliberately a
# whitelist: `id`, `principal_id`, `status`, `version` and `signature` are not
# amendable, because changing them would not be a correction — it would be a
# different grant, or an attempt to launder one.
AMENDABLE_FIELDS = frozenset(
    {
        "amount_cap_per_txn_rupees",
        "amount_cap_period_rupees",
        "merchant_allowlist",
        "category_exclusions",
        "time_window_start",
        "time_window_end",
        "frequency_cap",
    }
)


class AmendmentError(Exception):
    """The requested amendment is not valid and was not applied."""


def _validate(field: str, value: Any, mandate: Mandate) -> None:
    """Bounds-check one amendment against the same ceilings the compiler uses.

    A human correcting a guess is still not permitted to write an incoherent
    mandate — the gate applies to people as well as to the model.
    """
    if field in ("amount_cap_per_txn_rupees", "amount_cap_period_rupees"):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise AmendmentError(f"{field} must be a positive integer")
        ceiling = (
            MAX_RUPEES_PER_TXN
            if field == "amount_cap_per_txn_rupees"
            else MAX_RUPEES_PER_PERIOD
        )
        if value > ceiling:
            raise AmendmentError(f"{field} ₹{value} exceeds the ₹{ceiling} ceiling")

    elif field in ("merchant_allowlist", "category_exclusions"):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise AmendmentError(f"{field} must be a list of strings")
        if field == "merchant_allowlist" and not value:
            raise AmendmentError("merchant_allowlist cannot be empty")

    elif field in ("time_window_start", "time_window_end"):
        if not isinstance(value, str) or not _is_valid_hhmm(value):
            raise AmendmentError(f"{field} '{value}' is not a valid 24-hour HH:MM time")

    elif field == "frequency_cap":
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise AmendmentError("frequency_cap must be a positive integer")
        if value > MAX_FREQUENCY_CAP:
            raise AmendmentError(f"frequency_cap {value} is implausibly high")


def _apply(mandate: Mandate, field: str, value: Any) -> None:
    if field == "amount_cap_per_txn_rupees":
        mandate.amount_cap_per_txn = value * PAISE_PER_RUPEE
    elif field == "amount_cap_period_rupees":
        mandate.amount_cap_period = value * PAISE_PER_RUPEE
    elif field in ("merchant_allowlist", "category_exclusions"):
        setattr(mandate, field, [v.strip().casefold() for v in value])
    else:
        setattr(mandate, field, value.strip() if isinstance(value, str) else value)


def amend_mandate(
    session: Session,
    mandate_id: str,
    changes: dict[str, Any],
    *,
    activate: bool = True,
) -> Mandate:
    """Apply corrections, bump the version, re-sign, and record the amendment.

    `activate` moves a pending mandate to active, which is the normal outcome of
    a human resolving the ambiguity that made it pending. A revoked mandate can
    never be amended back into service — that would defeat revocation.
    """
    mandate = session.get(Mandate, mandate_id)
    if mandate is None:
        raise LookupError(f"no mandate {mandate_id}")

    if mandate.status is MandateStatus.revoked:
        raise AmendmentError("a revoked mandate cannot be amended")

    unknown = set(changes) - AMENDABLE_FIELDS
    if unknown:
        raise AmendmentError(f"not amendable: {', '.join(sorted(unknown))}")
    if not changes:
        raise AmendmentError("no changes supplied")

    for field, value in changes.items():
        _validate(field, value, mandate)
    for field, value in changes.items():
        _apply(mandate, field, value)

    # Coherence is checked after every change is applied, so that correcting two
    # interdependent fields at once is accepted.
    if mandate.amount_cap_per_txn > mandate.amount_cap_period:
        raise AmendmentError(
            f"per-transaction cap ₹{mandate.amount_cap_per_txn // PAISE_PER_RUPEE} "
            f"would exceed the {mandate.period.value} cap "
            f"₹{mandate.amount_cap_period // PAISE_PER_RUPEE}"
        )
    overlap = set(mandate.merchant_allowlist) & set(mandate.category_exclusions)
    if overlap:
        raise AmendmentError(
            f"{sorted(overlap)} would be both an allowed merchant and an "
            "excluded category"
        )

    mandate.version += 1
    if activate and mandate.status is MandateStatus.pending_confirmation:
        mandate.status = MandateStatus.active
    sign_mandate(mandate)

    session.add(mandate)
    session.commit()
    session.refresh(mandate)

    append_mandate_event(session, mandate, EventType.MANDATE_AMENDED)
    return mandate


def confirm_mandate(session: Session, mandate_id: str) -> Mandate:
    """Accept the compiler's assumptions unchanged and make the mandate live."""
    mandate = session.get(Mandate, mandate_id)
    if mandate is None:
        raise LookupError(f"no mandate {mandate_id}")

    if mandate.status is MandateStatus.revoked:
        raise AmendmentError("a revoked mandate cannot be confirmed")
    if mandate.status is not MandateStatus.pending_confirmation:
        return mandate

    mandate.status = MandateStatus.active
    session.add(mandate)
    session.commit()
    session.refresh(mandate)

    append_mandate_event(session, mandate, EventType.MANDATE_CONFIRMED)
    return mandate
