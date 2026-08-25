"""Hash-chained, tamper-evident audit log.

Every decision is linked to the one before it:

    audit_hash(n) = SHA256(canonical({... decision n ..., prev_hash: audit_hash(n-1)}))

Because `prev_hash` is *inside* the hashed payload, altering any entry changes
its own hash and orphans every entry after it. Verification recomputes the
whole chain and reports the first break.

The chain is designed to catch four distinct attacks:

  - **modification** — an edited field no longer hashes to the stored value
  - **deletion**     — a gap appears in the monotonic `seq`
  - **insertion**    — the inserted entry's `prev_hash` won't match its
                       predecessor, and the next entry's link breaks too
  - **reordering**   — `seq` is inside the hashed payload, so an entry moved to
                       a different position no longer verifies

What it does NOT protect against: an attacker who can write to the database
*and* recompute the entire chain forward from the point of tampering. Detecting
that requires anchoring the head hash somewhere the attacker cannot reach —
publishing it, or counter-signing it. `chain_head` exists for exactly that
purpose, and the honest limitation is documented in the README.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlmodel import Session, select

from app.canonical import canonical_bytes
from app.engine import EngineDecision
from app.models import Decision, Transaction

# The chain's anchor. Also what `chain_head` returns for an empty log.
GENESIS_HASH = "0" * 64

# Fields bound by the audit hash. `id` is excluded: it is a convenience handle,
# not part of the attested decision.
HASHED_FIELDS = (
    "seq",
    "transaction_id",
    "result",
    "reason_code",
    "rule_triggered",
    "created_at",
    "prev_hash",
)


def compute_hash(decision: Decision) -> str:
    """The audit hash a decision *should* have, given its contents."""
    return hashlib.sha256(
        canonical_bytes({field: getattr(decision, field) for field in HASHED_FIELDS})
    ).hexdigest()


def chain_head(session: Session) -> str:
    """Hash of the most recent entry, or GENESIS_HASH if the log is empty."""
    last = session.exec(select(Decision).order_by(Decision.seq.desc()).limit(1)).first()
    return last.audit_hash if last else GENESIS_HASH


def append_decision(
    session: Session,
    transaction: Transaction,
    outcome: EngineDecision,
) -> Decision:
    """Record an engine verdict as the next link in the chain.

    Commits twice by necessity: `seq` is assigned by the database and is part
    of the hashed payload, so the row must exist before its hash can be
    computed.
    """
    decision = Decision(
        transaction_id=transaction.id,
        result=outcome.result.value,
        reason_code=outcome.reason_code,
        rule_triggered=outcome.rule_triggered,
        prev_hash=chain_head(session),
        audit_hash="",  # placeholder until seq is assigned
    )
    session.add(decision)
    session.commit()
    session.refresh(decision)

    decision.audit_hash = compute_hash(decision)
    session.add(decision)
    session.commit()
    session.refresh(decision)
    return decision


@dataclass
class ChainReport:
    """Result of verifying the audit chain."""

    valid: bool
    entries_checked: int
    head: str
    broken_at_seq: int | None = None
    reason: str = ""

    def __str__(self) -> str:
        if self.valid:
            return f"chain OK — {self.entries_checked} entries, head {self.head[:12]}…"
        return f"chain BROKEN at seq {self.broken_at_seq} — {self.reason}"


def verify_chain(session: Session) -> ChainReport:
    """Walk the whole chain and report the first break, if any."""
    decisions = list(session.exec(select(Decision).order_by(Decision.seq)))

    if not decisions:
        return ChainReport(valid=True, entries_checked=0, head=GENESIS_HASH)

    expected_prev = GENESIS_HASH
    expected_seq = decisions[0].seq

    for index, decision in enumerate(decisions):
        # Deletion: SQLite assigns seq contiguously, so a gap means a row was
        # removed after the fact.
        if decision.seq != expected_seq:
            return ChainReport(
                valid=False,
                entries_checked=index,
                head=chain_head(session),
                broken_at_seq=decision.seq,
                reason=(
                    f"sequence gap: expected seq {expected_seq}, found {decision.seq} "
                    f"({decision.seq - expected_seq} entr"
                    f"{'y' if decision.seq - expected_seq == 1 else 'ies'} missing)"
                ),
            )

        # Broken link: this entry does not point at its predecessor.
        if decision.prev_hash != expected_prev:
            return ChainReport(
                valid=False,
                entries_checked=index,
                head=chain_head(session),
                broken_at_seq=decision.seq,
                reason=(
                    f"broken link: prev_hash {decision.prev_hash[:12]}… does not match "
                    f"preceding entry's hash {expected_prev[:12]}…"
                ),
            )

        # Modification: contents no longer hash to the stored value.
        recomputed = compute_hash(decision)
        if recomputed != decision.audit_hash:
            return ChainReport(
                valid=False,
                entries_checked=index,
                head=chain_head(session),
                broken_at_seq=decision.seq,
                reason=(
                    f"content altered: stored hash {decision.audit_hash[:12]}… but "
                    f"contents hash to {recomputed[:12]}…"
                ),
            )

        expected_prev = decision.audit_hash
        expected_seq = decision.seq + 1

    return ChainReport(
        valid=True,
        entries_checked=len(decisions),
        head=decisions[-1].audit_hash,
    )
