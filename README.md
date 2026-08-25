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

## Status

- **Day 1** — core schemas (`Mandate`, `Transaction`, `Decision`), FastAPI/SQLite skeleton.
- **Day 2** — deterministic policy engine (8 rules, fixed precedence) + usage
  accumulation, 46 unit tests.

NL compiler, Ed25519 signing, and the hash-chained audit log land next per the
plan in CONTEXT.md.
