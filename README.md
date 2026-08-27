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

## Signing and the audit chain

Two mechanisms split the integrity work, because neither covers the other:

**Ed25519 signature** ([app/signing.py](app/signing.py)) covers the *grant
terms* — principal, agent, caps, allowlist, exclusions, window, validity,
version. Editing any of them in the database breaks verification.

It deliberately does **not** cover `status`. Revocation is a legitimate act by
the principal; signing it would mean either revocation invalidates the
signature (making a revoked mandate look forged) or re-signing on every
transition (eroding the signature's meaning as an attestation of the original
grant). Status transitions are made tamper-evident by the audit chain instead.

**Hash chain** ([app/audit.py](app/audit.py)) links every decision to its
predecessor: `audit_hash(n) = SHA256(canonical(decision_n + prev_hash))`.
Because `prev_hash` sits inside the hashed payload, altering one entry orphans
every entry after it. Four attacks are each covered by a test:

| Attack | Caught by |
|---|---|
| modification | contents no longer hash to the stored value |
| deletion | gap in the monotonic `seq` |
| insertion | forged entry's `prev_hash` doesn't match the real head |
| reordering | `seq` is inside the hashed payload |

Both depend on [app/canonical.py](app/canonical.py) — byte-stable JSON (sorted
keys, no insignificant whitespace, ASCII-escaped, naive-UTC ISO timestamps).
Without it the same logical record could serialize two ways, and the whole
scheme would be decorative.

```bash
python scripts/demo_chain.py   # signs, decides, tampers via raw SQL, detects it
curl localhost:8000/audit/verify
```

### Known limitations (deliberate, not oversights)

- **Tail truncation is not detected.** Deleting the *last* entries leaves no
  sequence gap. Catching that needs the head hash anchored somewhere the
  attacker can't reach — published, or counter-signed. `chain_head` exists for
  that; the anchoring does not. There is a test asserting this gap honestly.
- **An attacker who can rewrite the whole chain forward** from the point of
  tampering would produce a self-consistent log. Same fix: external anchoring.
- **Dev signing keys** are generated to a gitignored `.signing_key`. Production
  would put the private key in an HSM/KMS behind an authenticated boundary.

## Synthetic data as a second test suite

[app/synthetic.py](app/synthetic.py) generates 275 transactions with **exact**
ground-truth labels, and [tests/test_dataset.py](tests/test_dataset.py) replays
them through the real engine. Every disagreement is a bug.

```bash
python scripts/generate_data.py --verify
python scripts/generate_data.py --out dataset.json
```

Generation is deterministic and seeded rather than LLM-driven. An LLM would
give richer vocabulary, but the labels are the whole point: if the expected
outcome were itself a guess, replaying would prove nothing.

Labels stay exact through one discipline: **every transaction violates at most
one rule.** When building a merchant violation the amount, category, time and
validity are all kept in-policy, so exactly one rule can fire and precedence
never has to be reasoned about. Stateful rules (cumulative and frequency caps)
get their own weeks, where the generator counts the crossover point
deliberately. The generator imports no evaluation logic from the engine, so
agreement between them is meaningful.

This caught four real bugs the unit tests missed — three in the generator, all
of the same shape: a scenario that *looked* like it tested one rule but
actually tripped another (₹2000 instalments against a ₹1000 per-transaction
cap; an `hour=23` "violation" landing on 23:00, which is inside the window;
stateful labels assigned in generation order while the engine evaluates in
timestamp order).

## Live simulation and revocation

```bash
uvicorn app.main:app --reload
curl -X POST localhost:8000/demo/seed            # load 275 transactions
curl -N localhost:8000/simulate/mandate-demo-001 # stream decisions (SSE)
curl -X POST localhost:8000/mandates/mandate-demo-001/revoke
```

**Revocation takes effect on the very next transaction.** The simulator
re-reads mandate state from the database for every single transaction, opening
a short-lived session each iteration. Caching the mandate once at the top of
the loop is the obvious optimisation and would quietly break the guarantee the
whole system exists to make — a kill switch that only takes effect at the next
batch boundary is not a kill switch.

That claim is tested by mutation, not just assertion: hoisting the mandate read
out of the loop makes `test_mid_batch_revocation_takes_effect_on_the_next_transaction`
fail with 186 transactions slipping through. Verified end-to-end against a real
uvicorn server too — revoke issued at t=1.55s into a 9.2s stream, 45 decisions
before it, 226 after, every one of them `MANDATE_REVOKED`.

### Revocation is chained, not just flagged

The Ed25519 signature deliberately excludes `status`, so revocation is made
tamper-evident by the audit chain instead: the transition is written into the
log as a `MANDATE_REVOKED` entry, hashed like any other. A mandate silently
flipped back to active in the database leaves the chain without a matching
event.

```
seq    1  MANDATE_CREATED
seq   47  MANDATE_REVOKED     <- lands mid-stream, between decisions
chain OK — 277 entries
```

## Dashboard

```bash
uvicorn app.main:app --reload   # then open http://localhost:8000
```

One page, left-to-right through the whole argument: type a policy in English →
see the compiled mandate with its Ed25519 signature and any fields the compiler
had to guess → run the batch → watch decisions stream in with live reason-code
counts → revoke mid-stream → verify the audit chain.

Three example policies are one click away, including the two failure cases:
one that names no merchant (must flag rather than guess) and one that is
self-contradictory (the validation gate rejects it and nothing gets signed).

**No CDN, no build step, no external fonts.** Everything is inline in a single
template, and a test asserts the page loads zero remote resources — a network
hiccup during a recorded demo must not be able to break it.

A mandate compiled from English can be run against the same traffic via
`POST /mandates/{id}/load-batch`. The ground-truth labels no longer apply there
(they were computed against the demo mandate's terms), so that path is for
demonstration; `tests/test_dataset.py` remains the labelled check.

## Status

- **Day 1** — core schemas (`Mandate`, `Transaction`, `Decision`), FastAPI/SQLite skeleton.
- **Day 2** — deterministic policy engine (8 rules, fixed precedence) + usage
  accumulation, 46 unit tests.
- **Day 3** — NL→Mandate compiler with ambiguity detection and a deterministic
  validation gate, plus a 15-case eval corpus.
- **Day 4** — Ed25519 mandate signing, hash-chained audit log with
  four-attack tamper detection, chain-verification endpoint.
- **Day 5** — 275-transaction labelled dataset replayed through the engine as a
  differential test.
- **Day 6** — SSE streaming simulator, mandate lifecycle events in the audit
  chain, live revocation verified mid-stream.
- **Day 7** — single-page dashboard: compile → mandate + ambiguity flags →
  live streaming decisions → revoke → audit viewer. 212 tests.

Deliberate failure handling and the architecture write-up land next per the
plan in CONTEXT.md.
