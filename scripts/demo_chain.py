#!/usr/bin/env python3
"""End-to-end demo of signing + the tamper-evident audit chain.

    python scripts/demo_chain.py

Signs a mandate, runs transactions through the engine, records each decision in
the hash chain, then tampers with the database directly — the way an attacker
with database access would — and shows the chain catching it.

This is the "one failure handled gracefully" scenario from the pitch, runnable
without the web UI.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

from app.audit import append_decision, verify_chain  # noqa: E402
from app.engine import evaluate  # noqa: E402
from app.models import Decision, DecisionResult, Mandate, MandateStatus, Period, Transaction  # noqa: E402
from app.signing import public_key_hex, sign_mandate, verify_mandate  # noqa: E402

DB_PATH = Path("demo_chain.db")

RUPEE = 100  # paise per rupee

SCENARIO = [
    # (amount_rupees, merchant, category, hour) -> expected outcome
    (300, "zepto", "groceries", 10),
    (450, "swiggy", "groceries", 13),
    (2500, "zepto", "groceries", 14),  # over per-txn cap
    (200, "amazon", "electronics", 15),  # merchant not allowed
    (150, "bigbasket", "alcohol", 16),  # excluded category
    (100, "zepto", "groceries", 3),  # outside time window
]


def rule(title: str) -> None:
    print(f"\n{title}\n{'─' * len(title)}")


def build(session: Session) -> Mandate:
    now = datetime(2026, 6, 10, 0, 0)
    mandate = Mandate(
        principal_id="user-aneesh",
        agent_id="shopping-agent-1",
        amount_cap_per_txn=1000 * RUPEE,
        amount_cap_period=5000 * RUPEE,
        period=Period.week,
        merchant_allowlist=["zepto", "swiggy", "bigbasket"],
        category_exclusions=["alcohol", "tobacco"],
        time_window_start="06:00",
        time_window_end="23:00",
        frequency_cap=20,
        valid_from=now,
        valid_until=datetime(2026, 7, 10, 0, 0),
        status=MandateStatus.active,
    )
    sign_mandate(mandate)
    session.add(mandate)
    session.commit()
    return mandate


def main() -> int:
    DB_PATH.unlink(missing_ok=True)
    engine = create_engine(f"sqlite:///{DB_PATH}")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        rule("1. Mandate is compiled and signed")
        mandate = build(session)
        print(f"   agent      : {mandate.agent_id}")
        print(f"   per-txn cap: ₹{mandate.amount_cap_per_txn // RUPEE}")
        print(f"   week cap   : ₹{mandate.amount_cap_period // RUPEE}")
        print(f"   merchants  : {', '.join(mandate.merchant_allowlist)}")
        print(f"   excluded   : {', '.join(mandate.category_exclusions)}")
        print(f"   window     : {mandate.time_window_start}-{mandate.time_window_end}")
        print(f"   signature  : {mandate.signature[:32]}…")
        print(f"   public key : {public_key_hex()[:32]}…")
        print(f"   verifies   : {verify_mandate(mandate)}")

        rule("2. Transactions evaluated by the deterministic engine")
        for amount, merchant, category, hour in SCENARIO:
            txn = Transaction(
                mandate_id=mandate.id,
                amount=amount * RUPEE,
                merchant=merchant,
                category=category,
                timestamp=datetime(2026, 6, 10, hour, 0),
            )
            session.add(txn)
            session.commit()
            session.refresh(txn)

            outcome = evaluate(mandate, txn)
            decision = append_decision(session, txn, outcome)
            mark = "✓" if outcome.allowed else "✗"
            print(
                f"   {mark} seq {decision.seq}  ₹{amount:<5} {merchant:<11} "
                f"{category:<12} {hour:02d}:00  {outcome.reason_code}"
            )
            if not outcome.allowed:
                print(f"        └─ {outcome.detail}")

        rule("3. Audit chain verification (intact)")
        print(f"   {verify_chain(session)}")

    # Tamper outside the application, via raw SQL — the realistic attack.
    rule("4. Attacker edits the database directly")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE decision SET result = ?, reason_code = ? WHERE seq = ?",
            (DecisionResult.allow.value, "ALLOWED", 3),
        )
    print("   UPDATE decision SET result='ALLOW', reason_code='ALLOWED' WHERE seq=3;")
    print("   (rewriting the ₹2500 over-cap block into an approval)")

    rule("5. Audit chain verification (after tampering)")
    with Session(engine) as session:
        report = verify_chain(session)
        print(f"   {report}")
        print(f"   valid          : {report.valid}")
        print(f"   broken at seq  : {report.broken_at_seq}")
        print(f"   entries before : {report.entries_checked}")

        row = session.exec(select(Decision).where(Decision.seq == 3)).one()
        print(f"\n   tampered row now reads : {row.result} / {row.reason_code}")
        print("   …but its stored hash no longer matches its contents.")

    DB_PATH.unlink(missing_ok=True)
    return 0 if not report.valid else 1  # tampering MUST be detected


if __name__ == "__main__":
    raise SystemExit(main())
