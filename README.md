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

## Status

Day 1: core schemas (`Mandate`, `Transaction`, `Decision`) and FastAPI/SQLite
skeleton. Policy engine, NL compiler, signing, and audit chain land in
subsequent days per the plan in CONTEXT.md.
