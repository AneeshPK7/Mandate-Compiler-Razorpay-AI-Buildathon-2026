# Mandate Compiler

Compiles a plain-English spending policy ("let my agent buy groceries, up to
₹2,000/week, only from Zepto/Swiggy/BigBasket, no alcohol, only 6am-11pm")
into a signed, structured, machine-checkable mandate — then deterministically
enforces every transaction against it, independent of any LLM.

See [CONTEXT.md](CONTEXT.md) for the full architecture and build plan.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Tests

```bash
python -m pytest tests/ -q
```

## Design note: why the engine is boring on purpose

[app/engine.py](app/engine.py) is a pure function of
`(Mandate, Transaction, Usage)` — no LLM, no network, no database. The LLM
compiles English into a `Mandate`; only deterministic code decides whether
money moves. Rules evaluate in a fixed priority order, so a revoked mandate
always reports `MANDATE_REVOKED` rather than whichever rule happened to fail
first, keeping the audit trail stable.

Two deliberate choices worth calling out:

- **Empty merchant allowlist fails closed.** An omitted allowlist is far more
  likely a compile error than a grant of unlimited merchant access.
- **Blocked transactions never consume budget.** Otherwise a burst of blocked
  attempts could starve legitimate spending.

## The compiler's trust boundary

[app/compiler.py](app/compiler.py) is the only place an LLM appears. It
translates English (or Hinglish) into a `MandateDraft` — and that draft is not
trusted. `validate_draft` bounds-checks every field with ordinary code before
anything can be signed:

- caps must be positive and under a compiler ceiling
- per-transaction cap may not exceed the period cap
- times must be well-formed 24h `HH:MM`
- the allowlist may not be empty
- a merchant may not also appear as an excluded category

A draft that fails the gate raises `ValidationError` and never becomes an
enforceable mandate. A hallucinated `₹99,999,999` cap is then a translation
bug, not a spending bug.

The model is also required to **flag its own guesses**. Any field the source
sentence left open comes back in `ambiguities` with a clarifying question, so
the human resolves it instead of the system silently assuming. Under-flagging
is treated as a failure in the eval even when the guessed value looks sane.

Money is stored in paise (integer minor units) throughout, matching Razorpay's
convention; the model emits rupees and the conversion happens in code.

### Evaluating the compiler

```bash
export ANTHROPIC_API_KEY=...
python scripts/eval_compiler.py          # 15 cases
python scripts/eval_compiler.py --case 6 -v
```

The corpus covers Hinglish input, `2k`-style shorthand, amounts written as
words, windows crossing midnight, and three cases that *should* be hard: a
policy naming no merchant, a day-of-week constraint the schema cannot express,
and a self-contradictory policy the gate must reject.

## Status

- **Day 1** — core schemas (`Mandate`, `Transaction`, `Decision`), FastAPI/SQLite skeleton.
- **Day 2** — deterministic policy engine (8 rules, fixed precedence) + usage
  accumulation, 46 unit tests.
- **Day 3** — NL→Mandate compiler with ambiguity detection and a deterministic
  validation gate, 84 unit tests, plus a 15-case eval corpus.

Ed25519 signing and the hash-chained audit log land next per the plan in
CONTEXT.md.
