# Project: Mandate Compiler — Razorpay AI Buildathon 2026

## Event context
- **Event:** Razorpay AI Buildathon — "Build. Show. Get hired." Student-only hiring program.
- **Deadline:** 5 September 2026 (submissions).
- **Submission requirements:** public repo, 5-minute pitch video, architecture documentation.
- **No resume/aptitude screening** — judged purely on the submitted work.
- **Track:** Track 01 — AI Growth & Agentic Commerce.
  - Track description: "Grow the merchant's revenue, and make them sellable to AI buyers." Build an agent that grows revenue for a merchant on Razorpay test-mode APIs, or makes a merchant transactable by an AI buyer end-to-end.
  - **The Bar (what's actually judged):** "Every money action explainable, bounded and gated. Show the audit trail and one failure handled gracefully."
  - Why now (per Razorpay): NPCI's UAP (Unified Agent Protocol) and the global protocol race (ACP, AP2, x402) make agent-to-agent commerce the open problem of the year.
- **Builder profile:** solo, backend/systems + LLM/agents strength, payments/fintech + security/fraud domain exposure. No ML/data-science focus — avoid ideas that hinge on training classifiers.

## Industry context (why this idea, not something else)
- **NPCI UAP:** India's Unified Agent Protocol, built on top of existing UPI Circle delegation. Registers/verifies/authorizes AI agents to transact over UPI without changing the underlying rails. Still pending RBI regulatory approval as of Aug 2026. Razorpay + NPCI already piloting agentic UPI payments on Claude with Zomato/Swiggy/Zepto; separate OpenAI/ChatGPT UPI pilot running since Oct 2025.
- **Global protocols racing on the same problem, different layers:**
  - **ACP** (OpenAI + Stripe) — standardizes the checkout flow; Shared Payment Token bound to merchant + amount, time-bounded, single-use.
  - **AP2** (Google, 60+ partners) — the trust/authorization layer; cryptographically signed "mandates."
  - **x402** (Coinbase) — settlement layer, stablecoins over HTTP, machine-to-machine microtransactions.
- **The gap none of them fully solve:** all of these prove an agent is *authorized to transact*. None of them rigorously prove that a *specific transaction* actually matches what the human intended when they granted that authorization, in a way that's independently verifiable after the fact. That's the gap this project fills — a policy-compilation-and-enforcement layer that could sit under any of these protocols.

## The idea, in plain terms
A user grants an AI shopping/payment agent permission in plain English — e.g. *"let my agent buy groceries, up to ₹2,000/week, only from Zepto/Swiggy/BigBasket, no alcohol, only 6am–11pm."* Today there's no way to make that promise verifiably real: the agent either has broad access or none, and nothing checks each individual purchase against what the human actually meant.

This project builds the missing layer:
1. **Compile** the English sentence into a strict, structured, machine-checkable rulebook ("mandate").
2. **Enforce** every transaction attempt against that rulebook — deterministically, not via LLM judgment — returning ALLOW/BLOCK plus a specific reason.
3. **Audit** every decision in a tamper-evident log.
4. **Revoke** the mandate instantly at any time, including mid-batch.

Pitch line: *"We're the seatbelt for AI agents that spend your money — it doesn't stop them from driving, it stops them from crashing."*

## Core design principle (this is the whole pitch — say it explicitly in the demo)
**Keep the LLM and the enforcement completely separate.** The LLM's only job is translating English into a structured policy (the "compiler"). A deterministic, non-LLM policy engine is what actually allows/blocks money movement (the "enforcer"). You cannot trust an LLM to gate money, so it doesn't — trust the LLM to translate, trust verifiable code to enforce.

## Architecture

```
NL policy text
      │
      ▼
[1] Compiler (LLM, Claude structured output)
      │  → Mandate JSON + list of ambiguous fields flagged for clarification
      ▼
[2] Signer (Ed25519) → signed, versioned Mandate
      │
      ▼
[3] Policy Engine (deterministic, pure code, no LLM)
      │  evaluates: Transaction × Mandate → ALLOW/BLOCK + reason code
      ▼
[4] Audit Log (hash-chained, tamper-evident)
      ▲
      │
[5] Simulator — replays synthetic transaction batch through [3], streams results live
[6] Revocation — flips mandate status; engine checks status on every single evaluation
```

## Data model

```python
class Mandate(BaseModel):
    id: str
    principal_id: str              # the human
    agent_id: str                  # which AI agent is authorized
    amount_cap_per_txn: int
    amount_cap_period: int         # e.g. weekly cumulative cap
    period: Literal["day", "week", "month"]
    merchant_allowlist: list[str]  # e.g. ["zepto", "swiggy", "bigbasket"]
    category_exclusions: list[str] # e.g. ["alcohol", "tobacco"]
    time_window: tuple[str, str]   # "06:00"-"23:00"
    frequency_cap: int             # max txns per period
    valid_from: datetime
    valid_until: datetime
    status: Literal["active", "revoked", "expired"]
    version: int
    signature: str                 # Ed25519 signature over the above fields

class Transaction(BaseModel):
    id: str
    mandate_id: str
    amount: int
    merchant: str
    category: str
    timestamp: datetime

class Decision(BaseModel):
    transaction_id: str
    result: Literal["ALLOW", "BLOCK"]
    reason_code: str       # e.g. "AMOUNT_CAP_EXCEEDED", "OUTSIDE_TIME_WINDOW", "MANDATE_REVOKED"
    rule_triggered: str
    audit_hash: str        # hash-chained: hash(prev_hash + this_decision)
```

## Tech stack
- **FastAPI + Pydantic** — schemas above double as the policy engine's backbone.
- **SQLite** via SQLModel — zero setup, plenty for 200–300 synthetic records.
- **Claude API** (structured/tool-use output) for the NL→Mandate compiler step.
- **`pynacl`** for Ed25519 signing — lightweight, no real PKI needed, still credible.
- **Server-rendered HTMX + FastAPI templates** for the dashboard (not React — avoids frontend build overhead for a solo build). SSE for live-streaming the transaction table during simulation.
- **Deploy:** Render or Railway free tier, so the video/README can link a live instance.

## Synthetic data plan
Generate ~250 transactions (use Claude offline, one script) spanning, each labeled with expected ground-truth outcome:
- In-policy (ALLOW) — majority of cases
- Over per-transaction cap
- Over cumulative weekly cap (requires tracking running totals)
- Merchant not in allowlist
- Excluded category inside an otherwise-allowed merchant (e.g. grocery store selling alcohol — good edge case)
- Outside time window
- Frequency cap exceeded
- Expired mandate
- A few genuinely ambiguous cases (multi-category merchant) to show the reasoning isn't just binary

## Day-by-day plan (10 working days to Sept 5)

| Day | Build |
|---|---|
| 1 | Schemas (Mandate/Transaction/Decision), FastAPI skeleton, SQLite, public repo init |
| 2 | Policy engine — all rules (amount, cumulative cap, allowlist, category, time, frequency, validity), unit tests per rule |
| 3 | NL→Mandate compiler via Claude structured output, ambiguity detection, test on ~15 varied sentences (include some Hinglish) |
| 4 | Ed25519 signing + hash-chained audit log + chain-verification endpoint |
| 5 | Synthetic data generator script, produce & label ~250 transactions |
| 6 | Simulator/replay endpoint (SSE streaming) + revocation endpoint; verify mid-batch revoke works |
| 7 | Dashboard: input form → compiled mandate view w/ ambiguity flags → simulate → live table → revoke button → audit viewer |
| 8 | Deliberate failure handling: (a) LLM flags low-confidence field → asks clarification instead of guessing, (b) manually corrupt one audit entry → chain verification catches it |
| 9 | Architecture diagram, README, honest limitations section |
| 10 | Record 5-min pitch video, buffer for bugs, submit |

**Start with Day 1–2 (schema + policy engine) before touching the LLM at all** — that's the part judges will stress-test hardest, and it's what makes the rest of the pitch credible.

## Demo script (~4.5 min video)
1. **(30s)** Problem: UAP/AP2/ACP/x402 all solve "is this agent allowed to transact." None solve "does this specific action match what the human actually meant." That's the gap being closed.
2. **(45s)** Type NL policy → show compiled mandate, including one field flagged ambiguous → resolve it live.
3. **(60s)** Hit simulate → 250 transactions stream in real time, ALLOW/BLOCK ticking up, reason-code breakdown building.
4. **(30s)** Mid-stream, hit revoke → subsequent transactions instantly blocked with `MANDATE_REVOKED`.
5. **(45s)** Corrupt one audit log entry live (raw DB edit) → run chain verification → show it's caught and flagged. This is the "one failure handled gracefully."
6. **(30s)** Close: what's next with more time (real UAP integration once RBI approves it, multi-agent mandates).

## How this maps to the Bar
- **Explainable** → every decision carries a structured reason code, never a vibe.
- **Bounded** → hard caps enforced by deterministic code; LLM is never in the enforcement path.
- **Gated** → validity window + live revocation.
- **Audit trail** → hash-chained log with tamper detection, demoed live on camera.
- **One failure handled gracefully** → two explicit ones: low-confidence compile → clarification instead of silent misfire, and audit tampering → detected and flagged.

## Competitive positioning notes (from prior research)
- Track 01's own example directions (conversational checkout, agent-readable catalog, upsell/cross-sell agent, campaign orchestrator) do NOT include this idea — most competing submissions will look like one of those four.
- This idea has **no direct overlap with an existing shipped Razorpay product** (unlike, say, fraud detection, which competes against their real Thirdwatch/Vulcan products) — lower risk of "we already do this internally, but better" pushback from judges.
- Novelty is at the *mechanism* level (nobody has published a working NL→policy→verifiable-enforcement pipeline for UPI-style agent delegation), not just a new application of a known technique.
- Main risk: has to be explained before it can be judged — the demo video needs to spend real time on framing (see script above), not just show the build.
