"""Deliberate audit-log corruption, for demonstrating tamper detection.

This module exists to ATTACK the audit log, so the defence can be shown working
on camera. It is demo scaffolding, not a feature.

It writes with raw SQL on purpose. Going through the ORM's append path would
maintain the hash chain and prove nothing; the realistic threat is someone with
database access editing rows directly, so that is what is reproduced here.

Guarded two ways: the endpoint that exposes it is registered only when
MANDATE_DEMO_TAMPER=1, and every function refuses to run against anything other
than SQLite. Neither guard belongs in a system that handles real money — in
production this file should not be deployed at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlmodel import Session, select

from app.models import Decision, DecisionResult


class TamperUnavailable(Exception):
    """Refused to corrupt the log."""


@dataclass
class TamperResult:
    seq: int
    attack: str
    description: str
    before: dict
    after: dict


def _guard(session: Session) -> None:
    dialect = session.get_bind().dialect.name
    if dialect != "sqlite":
        raise TamperUnavailable(
            f"tamper demo refuses to run against {dialect}; SQLite only"
        )


def _snapshot(entry: Decision) -> dict:
    return {
        "seq": entry.seq,
        "result": entry.result,
        "reason_code": entry.reason_code,
        "audit_hash": entry.audit_hash,
    }


def flip_a_block_to_allow(session: Session) -> TamperResult:
    """Rewrite history: make a blocked payment look authorised.

    The headline attack. If an audit log cannot catch this, it is decoration.
    """
    _guard(session)

    entry = session.exec(
        select(Decision)
        .where(Decision.result == DecisionResult.block.value)
        .order_by(Decision.seq)
    ).first()
    if entry is None:
        raise TamperUnavailable("no blocked decision in the log yet — run a simulation first")

    before = _snapshot(entry)
    session.execute(
        text(
            "UPDATE decision SET result = :result, reason_code = :reason "
            "WHERE seq = :seq"
        ),
        {"result": DecisionResult.allow.value, "reason": "ALLOWED", "seq": entry.seq},
    )
    session.commit()
    session.expire_all()

    after = _snapshot(session.get(Decision, entry.seq))
    return TamperResult(
        seq=entry.seq,
        attack="flip_block_to_allow",
        description=(
            f"Rewrote decision #{entry.seq} from BLOCK/{before['reason_code']} "
            "to ALLOW/ALLOWED via raw SQL"
        ),
        before=before,
        after=after,
    )


def delete_an_entry(session: Session) -> TamperResult:
    """Remove an entry from the middle of the log, leaving a gap in `seq`."""
    _guard(session)

    entries = list(session.exec(select(Decision).order_by(Decision.seq)))
    if len(entries) < 3:
        raise TamperUnavailable("need at least 3 entries to delete from the middle")

    target = entries[len(entries) // 2]
    before = _snapshot(target)
    # Captured before the delete: the ORM instance is unusable afterwards.
    seq = target.seq

    session.execute(text("DELETE FROM decision WHERE seq = :seq"), {"seq": seq})
    session.commit()
    session.expire_all()

    return TamperResult(
        seq=seq,
        attack="delete_entry",
        description=f"Deleted decision #{seq} outright via raw SQL",
        before=before,
        after={},
    )


def forge_hash_after_edit(session: Session) -> TamperResult:
    """Edit an entry AND recompute its hash — a partial cover-up.

    The most interesting case: the tampered entry now verifies against itself,
    so detection depends entirely on the chain link from the *next* entry.
    """
    _guard(session)

    from app.audit import compute_hash

    entries = list(session.exec(select(Decision).order_by(Decision.seq)))
    if len(entries) < 3:
        raise TamperUnavailable("need at least 3 entries to forge convincingly")

    # Must be a BLOCK, or flipping it to ALLOW changes nothing and the forged
    # hash matches the original — the attack would silently be a no-op. Must
    # also have a successor, since that successor's broken link is what exposes
    # the forgery.
    target = next(
        (e for e in entries[:-1] if e.result == DecisionResult.block.value),
        None,
    )
    if target is None:
        raise TamperUnavailable(
            "no blocked decision with a successor — run a longer simulation first"
        )

    before = _snapshot(target)

    target.reason_code = "ALLOWED"
    target.result = DecisionResult.allow.value
    forged = compute_hash(target)

    session.execute(
        text(
            "UPDATE decision SET result = :result, reason_code = :reason, "
            "audit_hash = :hash WHERE seq = :seq"
        ),
        {
            "result": DecisionResult.allow.value,
            "reason": "ALLOWED",
            "hash": forged,
            "seq": target.seq,
        },
    )
    session.commit()
    session.expire_all()

    after = _snapshot(session.get(Decision, target.seq))
    return TamperResult(
        seq=target.seq,
        attack="forge_hash",
        description=(
            f"Edited decision #{target.seq} and recomputed its hash, so the entry "
            "verifies against itself — the break surfaces at the next entry instead"
        ),
        before=before,
        after=after,
    )


ATTACKS = {
    "flip": flip_a_block_to_allow,
    "delete": delete_an_entry,
    "forge": forge_hash_after_edit,
}
