# Run of show — 5-minute pitch video

```bash
python scripts/preflight.py                  # must print GO
MANDATE_DEMO_TAMPER=1 uvicorn app.main:app   # then open http://localhost:8000
```

Record wide (≥1400px). The dashboard collapses to one column below 980px.

---

## 0:00 – 0:35 · The gap

**Do:** nothing. Title card or just talk.

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

**Do:** the default sentence is already in the box. Click **Compile**.

> This is the sentence a human actually says. An LLM compiles it into a
> structured mandate — caps, allowlist, excluded categories, time window — and
> signs it with Ed25519.
>
> But here's the thing that matters: **the LLM never decides whether money
> moves.** Its only job is translation. Everything downstream is deterministic
> code.

**Then:** click the third example — *"₹5000 per transaction at Zepto, but no
more than ₹1000 a week"* — and Compile.

> That's contradictory. A per-transaction cap above the weekly cap. The
> validation gate is ordinary code that bounds-checks everything the model
> produces, and it rejected this. **Nothing was signed.**
>
> A hallucinated cap is a translation bug. It never gets to be a spending bug.

> ⚠️ **Without `ANTHROPIC_API_KEY` set**, Compile reports "unavailable" instead.
> Skip to **Use demo mandate** and say: *"the compiler needs an API key; here's
> a pre-compiled mandate"* — then continue. Nothing else in the demo depends
> on it.

---

## 1:20 – 2:20 · 275 transactions, enforced

**Do:** click **Use demo mandate**, then **Simulate**.

> 275 transactions against that mandate. Every decision you see is made by a
> pure function — no model, no network. Eight rules in fixed priority order.
>
> Watch the reason codes build up on the right. Every single decision carries
> one: `MERCHANT_NOT_ALLOWED`, `OUTSIDE_TIME_WINDOW`, `PERIOD_CAP_EXCEEDED`.
> Never a vibe, never a confidence score — a specific rule, named.

**Point at** a `CATEGORY_EXCLUDED` row.

> That one's the interesting case. Allowed merchant, allowed amount, right
> time — but it's alcohol at a grocery store. The category rule catches what
> the merchant rule can't.

---

## 2:20 – 2:50 · Kill it mid-flight

**Do:** while the stream is still running, click **Revoke now**.

> Every transaction from this instant is `MANDATE_REVOKED`.
>
> Not at the end of the batch — the *next* transaction. The engine re-reads
> mandate state on every single evaluation. I tested that by deliberately
> breaking it: cache the mandate once at the top of the loop, and 186
> transactions slip through after the revoke.

---

## 2:50 – 3:50 · Break the audit log on camera

**Do:** click **Verify chain** first.

> Every decision is hash-chained. Each entry's hash covers the previous entry's
> hash, so altering anything orphans everything after it. Right now: valid.

**Do:** click **Forge the hash**.

> Now I'm going to attack it — with raw SQL, straight into the database, which
> is the realistic threat. And not the naive attack: this one edits a decision
> **and recomputes its hash**, so that entry now verifies against itself.
>
> [point at the result] Caught anyway. The forged entry looks fine, but the
> *next* entry still points at the old hash. `broken link`, and it names the
> exact sequence number.

**Optional if time allows** — click **Reset demo**, **Use demo mandate**,
**Simulate** briefly, then **Delete an entry**:

> Different attack, different signature: `sequence gap`. That's why sequence
> numbers are integers and not UUIDs — a gap is evidence of deletion.

---

## 3:50 – 4:20 · What it refuses to do

**Do:** click the second example — *"Let my agent spend ₹1000 a week on
groceries"* — and Compile.

> No merchant named. The compiler flags it as a guess it isn't willing to make
> silently, and the mandate is created **unenforceable** — the engine blocks
> everything with `MANDATE_NOT_CONFIRMED` until a human resolves it.
>
> The system declines to act on a policy it isn't confident it understood.
> A helpful guess here would be a security bug.

**Do:** type a merchant into the correction box and click **Apply**.

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

## If something goes wrong

| Symptom | Fix |
|---|---|
| Compile says "unavailable" | No API key. Use **Use demo mandate**. |
| Simulate does nothing | Batch already run. **Reset demo**, then **Use demo mandate**. |
| Tamper buttons missing | Server started without `MANDATE_DEMO_TAMPER=1`. |
| Tamper says "could not tamper" | Run a simulation first — needs decisions to attack. |
| Second attack reports the first one's break | Verification reports the *first* break. **Reset demo** between attacks. |
| Stream too fast to click Revoke | `delay_ms` is 60 in the template's Simulate handler; raise it. |

**Reset between takes:** click **Reset demo**, or `rm mandate_compiler.db` and
restart.
