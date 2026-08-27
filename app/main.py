import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlmodel import Session, select

from app.audit import append_mandate_event, verify_chain
from app.database import create_db_and_tables, get_session
from app.models import Decision, EventType, Mandate
from app.signing import public_key_hex, sign_mandate, verify_mandate
from app.simulator import (
    SimulationStep,
    load_batch_for,
    pending_transactions,
    revoke_mandate,
    seed_dataset,
    simulate,
)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(title="Mandate Compiler", lifespan=lifespan)


@app.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(request, "index.html")


class CompileRequest(BaseModel):
    text: str
    principal_id: str = "user-demo"
    agent_id: str = "shopping-agent-1"


@app.post("/compile")
def compile_endpoint(body: CompileRequest, session: Session = Depends(get_session)):
    """Compile English into a signed mandate, surfacing guesses and rejections.

    Three outcomes, all reported rather than raised:
      - `unavailable`: the model could not be reached (e.g. no API key)
      - `rejected`:    the draft failed the deterministic validation gate
      - `compiled`:    a signed mandate was created

    Ambiguities never block compilation. They are the model's own flags on
    fields it had to guess, shown so a human can resolve them.
    """
    from app.compiler import (
        PAISE_PER_RUPEE,
        compile_policy,
        draft_to_mandate,
        validate_draft,
    )

    try:
        draft = compile_policy(body.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface, don't crash the demo
        return {
            "status": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "hint": "Set ANTHROPIC_API_KEY to enable the compiler.",
        }

    problems = validate_draft(draft)
    draft_view = {
        "amount_cap_per_txn_rupees": draft.amount_cap_per_txn_rupees,
        "amount_cap_period_rupees": draft.amount_cap_period_rupees,
        "period": draft.period,
        "merchant_allowlist": draft.merchant_allowlist,
        "category_exclusions": draft.category_exclusions,
        "time_window": [draft.time_window_start, draft.time_window_end],
        "frequency_cap": draft.frequency_cap,
        "validity_days": draft.validity_days,
        "interpretation_notes": draft.interpretation_notes,
    }
    ambiguities = [
        {
            "field": a.field,
            "issue": a.issue,
            "assumed_value": a.assumed_value,
            "clarifying_question": a.clarifying_question,
        }
        for a in draft.ambiguities
    ]

    if problems:
        return {
            "status": "rejected",
            "draft": draft_view,
            "ambiguities": ambiguities,
            "problems": problems,
        }

    mandate = draft_to_mandate(draft, body.principal_id, body.agent_id)
    sign_mandate(mandate)
    session.add(mandate)
    session.commit()
    session.refresh(mandate)
    append_mandate_event(session, mandate, EventType.MANDATE_CREATED)

    return {
        "status": "compiled",
        "draft": draft_view,
        "ambiguities": ambiguities,
        "problems": [],
        "mandate": _mandate_view(mandate),
        "paise_per_rupee": PAISE_PER_RUPEE,
    }


@app.post("/mandates/{mandate_id}/load-batch")
def load_batch(mandate_id: str, session: Session = Depends(get_session)):
    """Attach the synthetic transaction batch to a mandate so it can be run."""
    try:
        added = load_batch_for(session, mandate_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"transactions_loaded": added, "pending": len(pending_transactions(session, mandate_id))}


@app.get("/mandates")
def list_mandates(session: Session = Depends(get_session)):
    mandates = session.exec(select(Mandate)).all()
    return [
        {
            "id": m.id,
            "principal_id": m.principal_id,
            "status": m.status.value,
            "pending": len(pending_transactions(session, m.id)),
        }
        for m in mandates
    ]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/audit/verify")
def audit_verify(session: Session = Depends(get_session)):
    """Recompute the whole audit chain and report the first break, if any.

    Deliberately unauthenticated and read-only: the point of a tamper-evident
    log is that anyone can check it.
    """
    report = verify_chain(session)
    return {
        "valid": report.valid,
        "entries_checked": report.entries_checked,
        "head": report.head,
        "broken_at_seq": report.broken_at_seq,
        "reason": report.reason,
        "summary": str(report),
    }


@app.get("/audit/chain")
def audit_chain(limit: int = 100, session: Session = Depends(get_session)):
    """The audit log itself, oldest first."""
    decisions = session.exec(select(Decision).order_by(Decision.seq).limit(limit)).all()
    return [
        {
            "seq": d.seq,
            "transaction_id": d.transaction_id,
            "result": d.result,
            "reason_code": d.reason_code,
            "rule_triggered": d.rule_triggered,
            "created_at": d.created_at,
            "prev_hash": d.prev_hash,
            "audit_hash": d.audit_hash,
        }
        for d in decisions
    ]


@app.get("/pubkey")
def pubkey():
    """Ed25519 public key, so mandate signatures can be checked independently."""
    return {"algorithm": "Ed25519", "public_key": public_key_hex()}


def _mandate_view(mandate: Mandate) -> dict:
    return {
        "id": mandate.id,
        "principal_id": mandate.principal_id,
        "agent_id": mandate.agent_id,
        "amount_cap_per_txn": mandate.amount_cap_per_txn,
        "amount_cap_period": mandate.amount_cap_period,
        "period": mandate.period.value,
        "merchant_allowlist": mandate.merchant_allowlist,
        "category_exclusions": mandate.category_exclusions,
        "time_window": [mandate.time_window_start, mandate.time_window_end],
        "frequency_cap": mandate.frequency_cap,
        "valid_from": mandate.valid_from,
        "valid_until": mandate.valid_until,
        "status": mandate.status.value,
        "version": mandate.version,
        "signature": mandate.signature,
        "signature_valid": verify_mandate(mandate),
    }


@app.get("/mandates/{mandate_id}")
def get_mandate(mandate_id: str, session: Session = Depends(get_session)):
    mandate = session.get(Mandate, mandate_id)
    if mandate is None:
        raise HTTPException(status_code=404, detail=f"no mandate {mandate_id}")
    return _mandate_view(mandate)


@app.post("/demo/seed")
def demo_seed(session: Session = Depends(get_session)):
    """Load the synthetic dataset so the simulator has something to replay.

    Idempotent: seeding twice does not duplicate the batch.
    """
    mandate, loaded = seed_dataset(session)
    return {
        "mandate": _mandate_view(mandate),
        "transactions_loaded": loaded,
        "pending": len(pending_transactions(session, mandate.id)),
    }


@app.post("/mandates/{mandate_id}/revoke")
def revoke(mandate_id: str, session: Session = Depends(get_session)):
    """Revoke a mandate. Takes effect on the very next evaluation.

    The transition is written into the audit chain, which is what makes it
    tamper-evident given that `status` sits outside the Ed25519 signature.
    """
    try:
        mandate = revoke_mandate(session, mandate_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": mandate.status.value, "mandate": _mandate_view(mandate)}


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


@app.get("/simulate/{mandate_id}")
def simulate_stream(
    mandate_id: str,
    delay_ms: int = 40,
    limit: int | None = None,
):
    """Stream evaluation of every pending transaction as Server-Sent Events.

    Each step is emitted as it is decided, so revoking mid-stream is visible
    live: the simulator re-reads mandate status for every transaction.
    """

    def event_stream():
        try:
            for item in simulate(
                mandate_id, delay_seconds=delay_ms / 1000, limit=limit
            ):
                if isinstance(item, SimulationStep):
                    yield _sse("step", item.to_dict())
                else:
                    yield _sse("summary", item.to_dict())
        except LookupError as exc:
            yield _sse("error", {"detail": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
