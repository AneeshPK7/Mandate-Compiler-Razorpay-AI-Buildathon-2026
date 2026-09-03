"""NL -> Mandate compiler.

This is the *only* place an LLM appears in the system. Its job is translation,
never enforcement: it turns an English sentence into a draft policy, and a
deterministic validation gate (`validate_draft`) decides whether that draft is
coherent enough to become a Mandate at all.

The gate matters. An LLM that hallucinates `amount_cap_per_txn: 9999999` is a
translation bug; if that value reached the engine unchecked it would become a
spending bug. So every field the model produces is bounds-checked by ordinary
code before it is signed, and anything the model had to guess is surfaced to
the human as an ambiguity rather than silently assumed.

Money is handled in paise (integer minor units) throughout the system, matching
Razorpay's own convention. The model emits rupees — natural in the source text
and trivial to get right — and the conversion happens here in code.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models import Mandate, MandateStatus, Period, utcnow

# Gemini 2.5 Flash: confirmed free-tier eligible (no billing required) as of
# this writing. Bump this constant, not call sites, if a newer free Flash
# model ships — nothing else in this file should need to change.
MODEL = "gemini-2.5-flash"
PAISE_PER_RUPEE = 100

# Bounds for the validation gate. These are guardrails on the *compiler*, not
# policy limits on the principal — they exist to catch a mistranslation before
# it can be signed into an enforceable mandate.
MAX_RUPEES_PER_TXN = 10_00_000  # ₹10 lakh
MAX_RUPEES_PER_PERIOD = 50_00_000  # ₹50 lakh
MAX_FREQUENCY_CAP = 1_000
MAX_VALIDITY_DAYS = 365


class AmbiguityFlag(BaseModel):
    """A field the model could not determine confidently from the input."""

    field: str = Field(description="Mandate field this concerns, e.g. 'time_window_start'")
    issue: str = Field(description="What was unclear about the source text")
    assumed_value: str = Field(description="The default applied, pending confirmation")
    clarifying_question: str = Field(description="Question to put to the human")


class MandateDraft(BaseModel):
    """Structured output contract for the compiler. Not yet a Mandate."""

    amount_cap_per_txn_rupees: int = Field(
        description="Maximum for any single transaction, in rupees."
    )
    amount_cap_period_rupees: int = Field(
        description="Maximum cumulative spend per period, in rupees."
    )
    period: Literal["day", "week", "month"]
    merchant_allowlist: list[str] = Field(
        description="Lowercase merchant identifiers the agent may pay. "
        "Empty only if the text names no merchants at all."
    )
    category_exclusions: list[str] = Field(
        description="Lowercase categories forbidden even at allowed merchants."
    )
    time_window_start: str = Field(description="Earliest permitted time, 24h 'HH:MM'.")
    time_window_end: str = Field(description="Latest permitted time, 24h 'HH:MM'.")
    frequency_cap: int = Field(description="Maximum transactions per period.")
    validity_days: int = Field(description="Days the mandate stays valid.")
    ambiguities: list[AmbiguityFlag] = Field(
        default_factory=list,
        description="Every field you had to guess at. Do not invent confidence.",
    )
    interpretation_notes: str = Field(
        default="", description="One or two sentences on how you read the request."
    )


SYSTEM_PROMPT = """\
You compile plain-English spending permissions into a strict, structured policy \
for an AI payment agent. A deterministic engine — not you — will enforce the \
result. Your only job is faithful translation.

Rules:

1. Translate only what the text says. Never widen a permission the human did not \
grant. When the text is silent on a field, choose the SAFEST value that still \
lets the stated intent work, and record it in `ambiguities`.
2. Flag every guess. If the human did not state a time window, a frequency cap, \
or a validity period, you MUST add an entry to `ambiguities` for it. A field you \
inferred from context rather than read directly is a guess. Under-flagging is \
worse than over-flagging: an unflagged wrong assumption becomes an enforced rule.
3. Never leave `merchant_allowlist` empty if the text names merchants, brands, or \
apps — normalize them to lowercase single tokens (e.g. "Big Basket" -> "bigbasket"). \
An empty allowlist blocks all spending, so if the text names no merchant, say so \
in `ambiguities` rather than silently inventing one.
4. Amounts are in RUPEES, as integers. "₹2,000" -> 2000. "2k" -> 2000. \
"two thousand rupees" -> 2000. If only a period total is given, set the \
per-transaction cap to that same total and flag it.
5. `category_exclusions` blocks a category even at an allowed merchant — this is \
how "groceries but no alcohol" works when the grocer also sells alcohol.
6. The input may be Hinglish or mixed script. Translate the intent faithfully; \
"sirf", "tak", "mat", "nahi" and similar carry real constraints.

Defaults when the text is silent (each REQUIRES an ambiguity entry):
- time window: "00:00" to "23:59"
- frequency cap: a conservative number consistent with the period
- validity: 30 days
"""


def _client():
    from google import genai

    # Reads GEMINI_API_KEY (or GOOGLE_API_KEY) from the environment — no key
    # threaded through call sites.
    return genai.Client()


def _inline_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A Pydantic JSON schema with every `$ref` resolved inline.

    Pydantic's own `model_json_schema()` factors nested models out into a
    `$defs` block and points at them with `$ref`. The Gemini SDK's structured-
    output path has a confirmed bug resolving those refs for nested models
    (googleapis/python-genai#60) — `MandateDraft` nests `list[AmbiguityFlag]`,
    so it hits this directly. Rather than trust that bug is fixed in whatever
    SDK version happens to be installed, the schema is flattened here so no
    `$ref` ever reaches the API.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                # $ref points at "#/$defs/Name"; the rest of the sibling keys
                # (if any) are dropped, matching how these refs are actually
                # emitted by Pydantic — a $ref is always the sole key.
                name = node["$ref"].rsplit("/", 1)[-1]
                return resolve(copy.deepcopy(defs[name]))
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


# Computed once at import time: the schema never changes at runtime, and
# resolving it is pure work worth doing exactly once.
_DRAFT_SCHEMA = _inline_json_schema(MandateDraft)


class EmptyPolicyError(ValueError):
    """The input text was empty. Distinct from any other ValueError a provider
    SDK might raise (e.g. Gemini raises a plain ValueError for a missing API
    key) — callers need to tell "bad input" apart from "can't reach the model"
    by exception type, not by hoping no SDK ever collides with the base class.
    """


def compile_policy(text: str, client=None) -> MandateDraft:
    """Translate an English (or Hinglish) policy into a structured draft.

    Raises on API failure — callers decide how to surface that. The draft is not
    trustworthy until it has passed `validate_draft`.
    """
    if not text or not text.strip():
        raise EmptyPolicyError("policy text is empty")

    from google.genai import types

    client = client or _client()
    response = client.models.generate_content(
        model=MODEL,
        contents=text.strip(),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_json_schema=_DRAFT_SCHEMA,
        ),
    )
    return MandateDraft.model_validate_json(response.text)


# Fields where a guess is not good enough to enforce without a human saying so.
# These directly bound how much money can move and to whom; a wrong assumption
# on any of them is a spending error, not a cosmetic one. Guesses about the
# time window or validity period are recorded but do not block activation.
CRITICAL_AMBIGUITY_FIELDS = frozenset(
    {
        "merchant_allowlist",
        "category_exclusions",
        "amount_cap_per_txn_rupees",
        "amount_cap_period_rupees",
        "period",
    }
)


def critical_ambiguities(draft: "MandateDraft") -> list[AmbiguityFlag]:
    """Ambiguities that must be resolved by a human before enforcement."""
    return [a for a in draft.ambiguities if a.field.strip() in CRITICAL_AMBIGUITY_FIELDS]


class ValidationError(Exception):
    """The draft failed a deterministic check and must not be signed."""


def validate_draft(draft: MandateDraft) -> list[str]:
    """Deterministic sanity checks on LLM output. Returns a list of problems.

    This is the trust boundary. Nothing the model produced is assumed sane, and
    a draft that fails here never becomes an enforceable mandate.
    """
    problems: list[str] = []

    if draft.amount_cap_per_txn_rupees <= 0:
        problems.append("amount_cap_per_txn must be positive")
    elif draft.amount_cap_per_txn_rupees > MAX_RUPEES_PER_TXN:
        problems.append(
            f"amount_cap_per_txn ₹{draft.amount_cap_per_txn_rupees} exceeds the "
            f"₹{MAX_RUPEES_PER_TXN} compiler ceiling"
        )

    if draft.amount_cap_period_rupees <= 0:
        problems.append("amount_cap_period must be positive")
    elif draft.amount_cap_period_rupees > MAX_RUPEES_PER_PERIOD:
        problems.append(
            f"amount_cap_period ₹{draft.amount_cap_period_rupees} exceeds the "
            f"₹{MAX_RUPEES_PER_PERIOD} compiler ceiling"
        )

    # A per-transaction cap above the period cap is incoherent: it permits a
    # single spend the period budget could never fund.
    if (
        draft.amount_cap_per_txn_rupees > 0
        and draft.amount_cap_period_rupees > 0
        and draft.amount_cap_per_txn_rupees > draft.amount_cap_period_rupees
    ):
        problems.append(
            f"per-transaction cap ₹{draft.amount_cap_per_txn_rupees} exceeds the "
            f"{draft.period} cap ₹{draft.amount_cap_period_rupees}"
        )

    for label, value in [
        ("time_window_start", draft.time_window_start),
        ("time_window_end", draft.time_window_end),
    ]:
        if not _is_valid_hhmm(value):
            problems.append(f"{label} '{value}' is not a valid 24-hour HH:MM time")

    if draft.frequency_cap <= 0:
        problems.append("frequency_cap must be positive")
    elif draft.frequency_cap > MAX_FREQUENCY_CAP:
        problems.append(f"frequency_cap {draft.frequency_cap} is implausibly high")

    if draft.validity_days <= 0:
        problems.append("validity_days must be positive")
    elif draft.validity_days > MAX_VALIDITY_DAYS:
        problems.append(
            f"validity_days {draft.validity_days} exceeds the "
            f"{MAX_VALIDITY_DAYS}-day compiler ceiling"
        )

    if not draft.merchant_allowlist:
        problems.append(
            "merchant_allowlist is empty — this would block all spending; "
            "the policy must name at least one merchant"
        )

    # An excluded category that is also an allowed merchant name is almost
    # always a mistranslation of the source sentence.
    overlap = {m.strip().casefold() for m in draft.merchant_allowlist} & {
        c.strip().casefold() for c in draft.category_exclusions
    }
    if overlap:
        problems.append(
            f"{sorted(overlap)} appears in both merchant_allowlist and "
            "category_exclusions"
        )

    return problems


def _is_valid_hhmm(value: str) -> bool:
    try:
        hours, minutes = value.strip().split(":")
    except (ValueError, AttributeError):
        return False
    if len(hours) != 2 or len(minutes) != 2:
        return False
    if not (hours.isdigit() and minutes.isdigit()):
        return False
    return 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59


def draft_to_mandate(
    draft: MandateDraft,
    principal_id: str,
    agent_id: str,
) -> Mandate:
    """Convert a validated draft into a Mandate, converting rupees to paise.

    A draft carrying a critical ambiguity becomes a mandate in
    `pending_confirmation`: signed and stored, but not enforceable until a human
    resolves the guess. The system declines to act on a policy it is not
    confident it understood, rather than guessing and hoping.

    Raises ValidationError if the draft has not passed `validate_draft` cleanly.
    """
    problems = validate_draft(draft)
    if problems:
        raise ValidationError("; ".join(problems))

    status = (
        MandateStatus.pending_confirmation
        if critical_ambiguities(draft)
        else MandateStatus.active
    )

    now = utcnow()
    return Mandate(
        status=status,
        principal_id=principal_id,
        agent_id=agent_id,
        amount_cap_per_txn=draft.amount_cap_per_txn_rupees * PAISE_PER_RUPEE,
        amount_cap_period=draft.amount_cap_period_rupees * PAISE_PER_RUPEE,
        period=Period(draft.period),
        merchant_allowlist=[m.strip().casefold() for m in draft.merchant_allowlist],
        category_exclusions=[c.strip().casefold() for c in draft.category_exclusions],
        time_window_start=draft.time_window_start.strip(),
        time_window_end=draft.time_window_end.strip(),
        frequency_cap=draft.frequency_cap,
        valid_from=now,
        valid_until=now + timedelta(days=draft.validity_days),
    )
