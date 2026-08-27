import enum
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return uuid4().hex


def utcnow() -> datetime:
    """Naive UTC. Stored naive so DB reads and in-process values compare cleanly."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Period(str, enum.Enum):
    day = "day"
    week = "week"
    month = "month"


class MandateStatus(str, enum.Enum):
    active = "active"
    revoked = "revoked"
    expired = "expired"
    # Compiled, signed and stored, but NOT enforceable: the compiler flagged a
    # field it had to guess on, and a human has not yet confirmed it. Failing
    # closed here is the point — a policy the system is unsure it understood
    # must not silently start authorising payments.
    pending_confirmation = "pending_confirmation"


class DecisionResult(str, enum.Enum):
    allow = "ALLOW"
    block = "BLOCK"


class Mandate(SQLModel, table=True):
    """A signed, versioned spending policy granted by a principal to an agent."""

    id: str = Field(default_factory=_uuid, primary_key=True)
    principal_id: str = Field(index=True)
    agent_id: str = Field(index=True)

    amount_cap_per_txn: int
    amount_cap_period: int
    period: Period

    merchant_allowlist: list[str] = Field(sa_column=Column(JSON), default_factory=list)
    category_exclusions: list[str] = Field(sa_column=Column(JSON), default_factory=list)

    # Stored as separate "HH:MM" bounds rather than a tuple; SQLite has no tuple column type.
    time_window_start: str
    time_window_end: str

    frequency_cap: int

    valid_from: datetime
    valid_until: datetime

    status: MandateStatus = Field(default=MandateStatus.active)
    version: int = Field(default=1)

    signature: str | None = Field(default=None)


class Transaction(SQLModel, table=True):
    """A single spend attempt submitted for evaluation against a Mandate."""

    id: str = Field(default_factory=_uuid, primary_key=True)
    mandate_id: str = Field(index=True, foreign_key="mandate.id")

    amount: int
    merchant: str
    category: str
    timestamp: datetime


class EventType:
    """What kind of thing an audit entry records."""

    DECISION = "DECISION"
    MANDATE_CREATED = "MANDATE_CREATED"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    MANDATE_AMENDED = "MANDATE_AMENDED"
    MANDATE_CONFIRMED = "MANDATE_CONFIRMED"


class Decision(SQLModel, table=True):
    """One entry in the audit chain.

    Most entries are the policy engine's verdict on a Transaction, but the
    chain also carries mandate lifecycle events. That is load-bearing rather
    than incidental: the Ed25519 signature deliberately excludes `status` (see
    app/signing.py), so revocation is made tamper-evident *here*. A mandate
    silently flipped from revoked back to active in the database leaves the
    chain without the corresponding event.

    `seq` is a monotonic integer rather than a UUID because the audit chain
    needs an unambiguous order and a way to notice a *missing* entry. A gap in
    the sequence is evidence of deletion; a UUID primary key could not express
    that.
    """

    seq: int | None = Field(default=None, primary_key=True)
    id: str = Field(default_factory=_uuid, unique=True, index=True)

    event_type: str = Field(default=EventType.DECISION, index=True)
    mandate_id: str | None = Field(default=None, index=True)
    # Null for lifecycle events, which are not about any one transaction.
    transaction_id: str | None = Field(
        default=None, index=True, foreign_key="transaction.id"
    )

    # Stored as a plain string holding a DecisionResult *value*, not as a SQL
    # enum. The audit verifier must be able to read whatever is actually in the
    # database — including values an attacker wrote — and report them as
    # tampering. A native enum column raises on unknown values, which would
    # crash verification instead of flagging it.
    result: str
    reason_code: str
    rule_triggered: str

    prev_hash: str
    audit_hash: str = Field(index=True)

    created_at: datetime = Field(default_factory=utcnow)
