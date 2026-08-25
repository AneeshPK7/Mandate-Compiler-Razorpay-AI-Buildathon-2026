from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.audit import append_decision
from app.database import get_session
from app.main import app
from app.models import DecisionResult, Transaction
from tests.test_audit import allow, block


@pytest.fixture
def client():
    # StaticPool: without it every connection to "sqlite://" gets its own
    # private in-memory database, so create_all and the session would not see
    # the same tables.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    app.dependency_overrides[get_session] = lambda: session
    # Deliberately not using TestClient as a context manager: that would run
    # the lifespan hook, which creates the real on-disk database file.
    c = TestClient(app)
    c.session = session
    yield c
    app.dependency_overrides.clear()
    session.close()


def seed(session: Session, n: int = 4):
    for i in range(n):
        txn = Transaction(
            mandate_id="m1",
            amount=100 * (i + 1),
            merchant="zepto",
            category="groceries",
            timestamp=datetime(2026, 6, 10, 12, 0),
        )
        session.add(txn)
        session.commit()
        session.refresh(txn)
        append_decision(session, txn, allow() if i % 2 else block())


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_pubkey_is_ed25519_hex(client):
    body = client.get("/pubkey").json()
    assert body["algorithm"] == "Ed25519"
    assert len(bytes.fromhex(body["public_key"])) == 32


def test_verify_empty_chain(client):
    body = client.get("/audit/verify").json()
    assert body["valid"] is True
    assert body["entries_checked"] == 0


def test_verify_intact_chain(client):
    seed(client.session)
    body = client.get("/audit/verify").json()
    assert body["valid"] is True
    assert body["entries_checked"] == 4
    assert body["broken_at_seq"] is None


def test_verify_reports_tampering(client):
    seed(client.session)
    client.session.execute(
        text("UPDATE decision SET reason_code = 'TAMPERED' WHERE seq = 2")
    )
    client.session.commit()

    body = client.get("/audit/verify").json()
    assert body["valid"] is False
    assert body["broken_at_seq"] == 2
    assert "content altered" in body["reason"]
    assert "BROKEN" in body["summary"]


def test_chain_endpoint_returns_entries_in_order(client):
    seed(client.session)
    rows = client.get("/audit/chain").json()
    assert [r["seq"] for r in rows] == [1, 2, 3, 4]
    assert rows[0]["prev_hash"] == "0" * 64
    assert rows[1]["prev_hash"] == rows[0]["audit_hash"]


def test_chain_endpoint_respects_limit(client):
    seed(client.session, 6)
    assert len(client.get("/audit/chain?limit=3").json()) == 3


def test_chain_exposes_reason_codes(client):
    seed(client.session)
    rows = client.get("/audit/chain").json()
    assert {r["result"] for r in rows} == {
        DecisionResult.allow.value,
        DecisionResult.block.value,
    }
