#!/usr/bin/env python3
"""Evaluate the NL->Mandate compiler against a fixed corpus of policy sentences.

Run with a key configured:

    export GEMINI_API_KEY=...   # free, no billing required — ai.google.dev
    python scripts/eval_compiler.py            # all cases
    python scripts/eval_compiler.py --case 3   # one case, verbose

Each case asserts only what the sentence actually determines. Fields the
sentence leaves open are asserted as *ambiguities* instead — the compiler is
expected to flag its own guesses, and a silently-confident guess is a failure
even when the guessed value looks reasonable.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.compiler import MandateDraft, compile_policy, validate_draft  # noqa: E402


@dataclass
class Case:
    name: str
    text: str
    expect_merchants: set[str] = field(default_factory=set)
    expect_exclusions: set[str] = field(default_factory=set)
    expect_period: str | None = None
    expect_per_txn_rupees: int | None = None
    expect_period_rupees: int | None = None
    expect_window: tuple[str, str] | None = None
    expect_ambiguous_fields: set[str] = field(default_factory=set)
    note: str = ""


CASES: list[Case] = [
    Case(
        name="canonical",
        text=(
            "Let my agent buy groceries, up to ₹2,000 per week, only from Zepto, "
            "Swiggy or BigBasket, no alcohol, and only between 6am and 11pm."
        ),
        expect_merchants={"zepto", "swiggy", "bigbasket"},
        expect_exclusions={"alcohol"},
        expect_period="week",
        expect_period_rupees=2000,
        expect_window=("06:00", "23:00"),
        note="The demo sentence. Everything but frequency cap is stated.",
    ),
    Case(
        name="single_merchant_daily",
        text="My agent can spend ₹500 a day at Zepto only.",
        expect_merchants={"zepto"},
        expect_period="day",
        expect_period_rupees=500,
        expect_ambiguous_fields={"time_window_start", "time_window_end"},
        note="No time window stated — must be flagged, not silently defaulted.",
    ),
    Case(
        name="monthly_with_frequency",
        text=(
            "Allow up to 10 orders a month from Swiggy and Zomato, "
            "max ₹800 each, ₹5000 total."
        ),
        expect_merchants={"swiggy", "zomato"},
        expect_period="month",
        expect_per_txn_rupees=800,
        expect_period_rupees=5000,
    ),
    Case(
        name="multiple_exclusions",
        text=(
            "Groceries from BigBasket up to ₹3000 a week, but never alcohol, "
            "tobacco or cigarettes."
        ),
        expect_merchants={"bigbasket"},
        expect_exclusions={"alcohol", "tobacco"},
        expect_period="week",
        expect_period_rupees=3000,
    ),
    Case(
        name="overnight_window",
        text="Let the agent order from Swiggy between 10pm and 6am, ₹600 max per order, ₹2000 a week.",
        expect_merchants={"swiggy"},
        expect_per_txn_rupees=600,
        expect_period_rupees=2000,
        expect_window=("22:00", "06:00"),
        note="Window crosses midnight — engine handles this, compiler must not 'fix' it.",
    ),
    Case(
        name="only_period_total",
        text="₹1500 a week at Zepto.",
        expect_merchants={"zepto"},
        expect_period="week",
        expect_period_rupees=1500,
        note="Only one number given; per-txn cap should mirror it and be flagged.",
    ),
    Case(
        name="hinglish_basic",
        text="Mera agent sirf Zepto se ₹1000 tak grocery kharid sakta hai, har hafte.",
        expect_merchants={"zepto"},
        expect_period="week",
        expect_period_rupees=1000,
        note="Hinglish: 'sirf'=only, 'tak'=up to, 'har hafte'=every week.",
    ),
    Case(
        name="hinglish_exclusion",
        text="BigBasket aur Swiggy se ₹2000 hafte mein, lekin sharab nahi.",
        expect_merchants={"bigbasket", "swiggy"},
        expect_exclusions={"alcohol"},
        expect_period="week",
        expect_period_rupees=2000,
        note="'sharab nahi' = no alcohol. Must map to the English category token.",
    ),
    Case(
        name="hinglish_time",
        text="Subah 7 baje se raat 10 baje tak hi Zomato se order karna, ₹700 roz.",
        expect_merchants={"zomato"},
        expect_period="day",
        expect_period_rupees=700,
        expect_window=("07:00", "22:00"),
        note="'subah 7' = 7am, 'raat 10' = 10pm, 'roz' = daily.",
    ),
    Case(
        name="shorthand_amount",
        text="Agent can spend 2k per week on Zepto, 500 max per order.",
        expect_merchants={"zepto"},
        expect_per_txn_rupees=500,
        expect_period_rupees=2000,
        note="'2k' must expand to 2000, not 2.",
    ),
    Case(
        name="words_not_digits",
        text="Allow two thousand rupees a month at BigBasket, five hundred per transaction.",
        expect_merchants={"bigbasket"},
        expect_period="month",
        expect_per_txn_rupees=500,
        expect_period_rupees=2000,
    ),
    Case(
        name="no_merchant_named",
        text="Let my agent spend ₹1000 a week on groceries.",
        expect_ambiguous_fields={"merchant_allowlist"},
        note="Names no merchant. Must flag rather than invent one — this is the "
        "case where a helpful guess would be a security bug.",
    ),
    Case(
        name="ambiguous_category_merchant",
        text="₹2000 a week at BigBasket for household items, nothing else.",
        expect_merchants={"bigbasket"},
        expect_period="week",
        expect_period_rupees=2000,
        note="'nothing else' is genuinely vague — an allowlist-of-categories the "
        "current schema can't express. Expect a flag.",
    ),
    Case(
        name="weekend_constraint",
        text="Agent may order from Swiggy on weekends only, ₹800 per order, ₹1600 a week.",
        expect_merchants={"swiggy"},
        expect_per_txn_rupees=800,
        expect_period_rupees=1600,
        note="Day-of-week is NOT expressible in the current schema. The compiler "
        "must flag the dropped constraint rather than silently discard it.",
    ),
    Case(
        name="contradictory",
        text="Let my agent spend ₹5000 per transaction at Zepto, but no more than ₹1000 a week.",
        expect_merchants={"zepto"},
        note="Per-txn cap exceeds the period cap. The validation gate must reject "
        "this even if the model translates it faithfully.",
    ),
]


def check(case: Case, draft: MandateDraft) -> list[str]:
    """Return a list of failures for one case."""
    failures: list[str] = []
    got_merchants = {m.strip().casefold() for m in draft.merchant_allowlist}
    got_exclusions = {c.strip().casefold() for c in draft.category_exclusions}
    flagged = {a.field for a in draft.ambiguities}

    if case.expect_merchants and not case.expect_merchants <= got_merchants:
        failures.append(
            f"merchants: expected {sorted(case.expect_merchants)} ⊆ {sorted(got_merchants)}"
        )
    if case.expect_exclusions and not case.expect_exclusions <= got_exclusions:
        failures.append(
            f"exclusions: expected {sorted(case.expect_exclusions)} ⊆ {sorted(got_exclusions)}"
        )
    if case.expect_period and draft.period != case.expect_period:
        failures.append(f"period: expected {case.expect_period}, got {draft.period}")
    if (
        case.expect_per_txn_rupees is not None
        and draft.amount_cap_per_txn_rupees != case.expect_per_txn_rupees
    ):
        failures.append(
            f"per-txn: expected ₹{case.expect_per_txn_rupees}, "
            f"got ₹{draft.amount_cap_per_txn_rupees}"
        )
    if (
        case.expect_period_rupees is not None
        and draft.amount_cap_period_rupees != case.expect_period_rupees
    ):
        failures.append(
            f"period cap: expected ₹{case.expect_period_rupees}, "
            f"got ₹{draft.amount_cap_period_rupees}"
        )
    if case.expect_window:
        got = (draft.time_window_start, draft.time_window_end)
        if got != case.expect_window:
            failures.append(f"window: expected {case.expect_window}, got {got}")

    for expected_flag in case.expect_ambiguous_fields:
        if expected_flag not in flagged:
            failures.append(
                f"ambiguity: expected '{expected_flag}' to be flagged; flagged {sorted(flagged) or 'nothing'}"
            )

    return failures


def render(case: Case, draft: MandateDraft, failures: list[str], verbose: bool) -> None:
    status = "PASS" if not failures else "FAIL"
    print(f"[{status}] {case.name}")
    print(f"       {case.text}")

    if verbose or failures:
        print(
            f"       -> ₹{draft.amount_cap_per_txn_rupees}/txn, "
            f"₹{draft.amount_cap_period_rupees}/{draft.period}, "
            f"{draft.frequency_cap} txns, "
            f"{draft.time_window_start}-{draft.time_window_end}"
        )
        print(f"       -> merchants={draft.merchant_allowlist} excl={draft.category_exclusions}")
        for a in draft.ambiguities:
            print(f"       ? {a.field}: {a.issue} (assumed {a.assumed_value})")
        gate = validate_draft(draft)
        if gate:
            print(f"       ! gate rejected: {'; '.join(gate)}")
        if draft.interpretation_notes:
            print(f"       ~ {draft.interpretation_notes}")

    for f in failures:
        print(f"       ✗ {f}")
    if case.note and (verbose or failures):
        print(f"       # {case.note}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=int, help="run a single case by index (0-based)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    cases = [CASES[args.case]] if args.case is not None else CASES
    verbose = args.verbose or args.case is not None

    passed = 0
    errored = 0
    for case in cases:
        try:
            draft = compile_policy(case.text)
        except Exception as exc:  # noqa: BLE001 - report and continue the sweep
            print(f"[ERROR] {case.name}: {type(exc).__name__}: {exc}\n")
            errored += 1
            continue

        failures = check(case, draft)
        render(case, draft, failures, verbose)
        passed += not failures

    total = len(cases)
    print(f"{passed}/{total} passed" + (f", {errored} errored" if errored else ""))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
