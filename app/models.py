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


class Decision(SQLModel, table=True):
    """The policy engine's verdict on a Transaction, chained into the audit log."""

    id: str = Field(default_factory=_uuid, primary_key=True)
    transaction_id: str = Field(index=True, foreign_key="transaction.id")

    result: DecisionResult
    reason_code: str
    rule_triggered: str

    prev_hash: str
    audit_hash: str = Field(index=True)

    created_at: datetime = Field(default_factory=utcnow)
