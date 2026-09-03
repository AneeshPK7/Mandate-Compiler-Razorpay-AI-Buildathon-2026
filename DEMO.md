# Run of show — 5-minute pitch video

```bash
python scripts/preflight.py                  # must print GO
MANDATE_DEMO_TAMPER=1 uvicorn app.main:app   # then open http://localhost:8000
```

Record wide (≥1400px) — the console is a three-column layout and collapses
awkwardly below that. This reflects the trading-desk console: the amber
compiler column on the left, the jade decision ledger in the middle, the
steel audit chain on the right, with the status/budget/kill-switch band
across the top.

---

## 0:00 – 0:35 · The gap

**Do:** nothing. Title card or just talk over the idle console.

> Four protocols are racing to solve agentic payments. NPCI's UAP, Google's
> AP2, OpenAI and Stripe's ACP, Coinbase's x402. They all answer the same
> question: *is this agent authorized to transact?*
>
> None of them answer the next one: **does this specific payment match what the
> human actually meant** when they granted that authorization — and can anyone
> verify it afterwards?
>
> An agent with a valid credential and a broad grant is still an agent that can
> buy the wrong thing, from the wrong merchant, at 3am. That's the gap.

---

## 0:35 – 1:20 · Compile English into an enforceable policy

**Do:** the default sentence is already in the amber textarea, top left. Click **Compile**.

> This is the sentence a human actually says. An LLM compiles it into a
> structured mandate — caps, allowlist, excluded categories, time window — and
> signs it with Ed25519.
>
> But here's the thing that matters: **the LLM never decides whether money
> moves.** Its only job is translation. Everything downstream is deterministic
> code.

**Then:** click the third example chip — *"₹5000 per transaction at Zepto, but
no more than ₹1000 a week"* — and **Compile** again.

> That's contradictory. A per-transaction cap above the weekly cap. The
> validation gate is ordinary code that bounds-checks everything the model
> produces, and it rejected this — the red notice, no mandate below it.
> **Nothing was signed.**
>
> A hallucinated cap is a translation bug. It never gets to be a spending bug.

> ⚠️ **Without `GEMINI_API_KEY` set**, Compile shows a red "Compiler
> unavailable" notice instead. Skip to **Demo mandate** (next to Compile) and
> say: *"the compiler needs an API key; here's a pre-compiled mandate"* — then
> continue. Nothing else in the demo depends on it.

---

## 1:20 – 2:20 · 275 transactions, enforced

**Do:** click **Demo mandate**, then **Run batch** (top of the middle column).

> 275 transactions against that mandate. Every decision you see is made by a
> pure function — no model, no network. Eight rules in fixed priority order.
>
> Watch the status band at the top: the committed-this-period line climbs as
> ALLOW rows land, and the reason-code spectrum bar underneath the tallies
> builds up in real time. Every decision carries a named reason — never a
> vibe, never a confidence score.

**Point at** a `CATEGORY_EXCLUDED` row in the ledger.

> That one's the interesting case. Allowed merchant, allowed amount, right
> time — but it's alcohol at a grocery store. The category rule catches what
> the merchant rule can't.

---

## 2:20 – 2:50 · Kill it mid-flight

**Do:** while the batch is still streaming, click the **Revoke mandate** panel
— the dark red block in the top-right of the status band.

> Every transaction from this instant is `MANDATE_REVOKED`. The status word
> at top-left flips to "Revoked" in real time.
>
> Not at the end of the batch — the *next* transaction. The engine re-reads
> mandate state on every single evaluation. I tested that by deliberately
> breaking it: cache the mandate once at the top of the loop, and 186
> transactions slip through after the revoke.

---

## 2:50 – 3:50 · Break the audit log on camera

**Do:** in the right-hand column, click **Verify chain from genesis** first.

> Every decision is hash-chained. Each entry's hash covers the previous entry's
> hash, so altering anything orphans everything after it. Right now: chain
> intact, shown right there.

**Do:** scroll to the **attack it** row at the bottom of the chain column,
click **Forge**.

> Now I'm going to attack it — with raw SQL, straight into the database, which
> is the realistic threat. And not the naive attack: this one edits a decision
> **and recomputes its hash**, so that entry now verifies against itself.
>
> [point at the chain state, now red] Caught anyway. The forged entry looks
> fine, but the *next* entry still points at the old hash. `broken link`, and
> it names the exact sequence number.

**Optional if time allows** — click **Reset** (middle column, top right),
**Demo mandate**, **Run batch** briefly, then **Delete**:

> Different attack, different signature: `sequence gap`. That's why sequence
> numbers are integers and not UUIDs — a gap is evidence of deletion.

---

## 3:50 – 4:20 · What it refuses to do

**Do:** click the second example chip — *"Let my agent spend ₹1000 a week on
groceries"* — and **Compile**.

> No merchant named. The compiler flags it as a guess it isn't willing to make
> silently — the amber "flagged its own guess" panel — and the mandate is
> created **unenforceable**. The engine blocks everything with
> `MANDATE_NOT_CONFIRMED` until a human resolves it.
>
> The system declines to act on a policy it isn't confident it understood.
> A helpful guess here would be a security bug.

**Do:** click **Correct it**, type a merchant into the field that appears, click **Apply**.

> Corrected, re-signed, version bumped — and the amendment is itself in the
> audit chain.

---

## 4:20 – 5:00 · Close honestly

> Everything you saw runs on 262 tests, plus a 275-transaction labelled dataset
> replayed through the engine as an independent check.
>
> What it doesn't do: an attacker who can rewrite the *entire* chain forward
> produces a consistent log — that needs the head hash anchored externally, and
> that's next. There's no authentication. It's not on real rails yet.
>
> But the shape is the point. **Trust the LLM to translate. Trust verifiable
> code to enforce.** That separation is what makes an agent safe to hand a
> wallet to — and it sits under UAP, AP2, ACP, any of them.
>
> It's the seatbelt. It doesn't stop the agent driving. It stops it crashing.

---

## Control reference

| On screen | Location | Does |
|---|---|---|
| **Compile** | left column, amber | Sends the textarea to `/compile` |
| **Demo mandate** | left column, next to Compile | Loads the seeded 275-txn demo mandate |
| example chips (3) | left column, under the buttons | Fill the textarea with one scripted sentence |
| **Confirm as assumed** / **Correct it** | left column, appears on a pending mandate | Resolve a blocking ambiguity |
| **Run batch** | middle column header | Starts the SSE stream over pending transactions |
| **Reset** | middle column header | Clears the run; wipes the DB too when tamper mode is on |
| **Revoke mandate** panel | top status band, right | The kill switch — dark red, top-right of the band |
| **Verify chain from genesis** | right column | Re-checks the whole hash chain, updates the state card |
| **Flip / Delete / Forge** | right column, bottom (tamper mode only) | The three attacks |

---

## If something goes wrong

| Symptom | Fix |
|---|---|
| Compile shows a red "unavailable" notice | No API key. Click **Demo mandate** instead. |
| Run batch is disabled | Batch already run. Click **Reset**, then **Demo mandate**. |
| Flip/Delete/Forge row missing at the bottom of the chain column | Server started without `MANDATE_DEMO_TAMPER=1`. |
| Attack button does nothing / error text appears | Run a batch first — needs decisions to attack. |
| Second attack reports the first one's break | Verification reports the *first* break. Click **Reset** between attacks. |
| Stream too fast to click Revoke | `delay_ms=60` is hardcoded in the template's `btnSim` handler (~16s for 275 txns) — raise it if you need more time. |

**Reset between takes:** click **Reset**, or `rm mandate_compiler.db` and
restart the server.
