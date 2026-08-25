from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlmodel import Session, select

from app.audit import verify_chain
from app.database import create_db_and_tables, get_session
from app.models import Decision
from app.signing import public_key_hex


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(title="Mandate Compiler", lifespan=lifespan)


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
