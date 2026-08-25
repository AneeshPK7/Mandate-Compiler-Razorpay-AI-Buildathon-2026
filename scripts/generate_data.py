#!/usr/bin/env python3
"""Generate and export the labelled synthetic dataset.

    python scripts/generate_data.py                 # summary only
    python scripts/generate_data.py --out data.json # write JSON
    python scripts/generate_data.py --verify        # replay through the engine
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

from app.engine import evaluate  # noqa: E402
from app.synthetic import RUPEE, generate_dataset  # noqa: E402
from app.usage import compute_usage  # noqa: E402


def summarise(dataset) -> None:
    allowed = sum(1 for c in dataset.cases if c.expected_allowed)
    span = (
        min(c.transaction.timestamp for c in dataset.cases),
        max(c.transaction.timestamp for c in dataset.cases),
    )

    m = dataset.mandate
    print("Mandate")
    print(f"  {m.principal_id} -> {m.agent_id}")
    print(f"  ₹{m.amount_cap_per_txn // RUPEE}/txn, ₹{m.amount_cap_period // RUPEE}/{m.period.value}")
    print(f"  {m.frequency_cap} txns/{m.period.value}, {m.time_window_start}-{m.time_window_end}")
    print(f"  merchants: {', '.join(m.merchant_allowlist)}")
    print(f"  excluded : {', '.join(m.category_exclusions)}")
    print(f"  valid    : {m.valid_from:%Y-%m-%d} to {m.valid_until:%Y-%m-%d}")

    print(f"\nDataset: {len(dataset)} transactions")
    print(f"  span    : {span[0]:%Y-%m-%d} to {span[1]:%Y-%m-%d}")
    print(f"  allowed : {allowed} ({allowed / len(dataset):.0%})")
    print(f"  blocked : {len(dataset) - allowed}")

    print("\nExpected outcomes")
    for reason, count in dataset.counts().items():
        bar = "█" * max(1, round(count / len(dataset) * 60))
        print(f"  {count:>4}  {reason:<26} {bar}")

    print("\nScenarios exercised")
    for scenario, count in dataset.scenario_counts().items():
        print(f"  {count:>4}  {scenario}")


def verify(dataset) -> int:
    """Replay every case through the real engine and report disagreements."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)

    mismatches = []
    with Session(engine) as session:
        session.add(dataset.mandate)
        session.commit()

        from app.audit import append_decision, verify_chain

        for case in sorted(dataset.cases, key=lambda c: c.transaction.timestamp):
            txn = case.transaction
            session.add(txn)
            session.commit()

            usage = compute_usage(session, dataset.mandate, txn.timestamp)
            outcome = evaluate(dataset.mandate, txn, usage)
            append_decision(session, txn, outcome)

            if outcome.reason_code != case.expected_reason:
                mismatches.append(
                    f"  {txn.id} ({case.scenario}): expected "
                    f"{case.expected_reason}, got {outcome.reason_code}"
                )

        chain = verify_chain(session)

    print(f"\nVerification: {len(dataset)} cases replayed through the engine")
    if mismatches:
        print(f"  ✗ {len(mismatches)} mismatches")
        for line in mismatches[:20]:
            print(line)
        return 1

    print("  ✓ every label agrees with the engine")
    print(f"  ✓ {chain}")
    return 0


def export(dataset, path: Path) -> None:
    m = dataset.mandate
    payload = {
        "mandate": {
            "id": m.id,
            "principal_id": m.principal_id,
            "agent_id": m.agent_id,
            "amount_cap_per_txn": m.amount_cap_per_txn,
            "amount_cap_period": m.amount_cap_period,
            "period": m.period.value,
            "merchant_allowlist": m.merchant_allowlist,
            "category_exclusions": m.category_exclusions,
            "time_window_start": m.time_window_start,
            "time_window_end": m.time_window_end,
            "frequency_cap": m.frequency_cap,
            "valid_from": m.valid_from.isoformat(),
            "valid_until": m.valid_until.isoformat(),
            "status": m.status.value,
            "version": m.version,
        },
        "transactions": [
            {
                "id": c.transaction.id,
                "amount": c.transaction.amount,
                "merchant": c.transaction.merchant,
                "category": c.transaction.category,
                "timestamp": c.transaction.timestamp.isoformat(),
                "expected_result": c.expected_result.value,
                "expected_reason": c.expected_reason,
                "scenario": c.scenario,
            }
            for c in sorted(dataset.cases, key=lambda c: c.transaction.timestamp)
        ],
    }
    path.write_text(json.dumps(payload, indent=2))
    print(f"\nWrote {len(dataset)} transactions to {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, help="write the dataset to a JSON file")
    parser.add_argument("--verify", action="store_true", help="replay through the engine")
    parser.add_argument("--seed", type=int, default=20260905)
    args = parser.parse_args()

    dataset = generate_dataset(seed=args.seed)
    summarise(dataset)

    if args.out:
        export(dataset, args.out)

    return verify(dataset) if args.verify else 0


if __name__ == "__main__":
    raise SystemExit(main())
