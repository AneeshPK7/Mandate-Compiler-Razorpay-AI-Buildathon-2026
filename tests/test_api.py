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


# --- simulator, seeding and revocation endpoints ----------------------------
#
# Note on streaming: Starlette's TestClient buffers a StreamingResponse, so the
# server-side generator can run to completion before the client reads the first
# chunk. That makes it impossible to issue a *mid-stream* revoke through
# TestClient — the revoke would land after every transaction was already
# decided. Mid-stream revocation is therefore covered in tests/test_simulator.py
# by driving the generator directly, and was additionally verified end-to-end
# against a real uvicorn server. These tests cover endpoint shape only.


@pytest.fixture
def sim_client(client, monkeypatch):
    """Client whose simulator shares the test's in-memory engine."""
    monkeypatch.setattr("app.simulator.db_engine", client.session.get_bind())
    return client


def test_seed_endpoint_loads_the_dataset(sim_client):
    body = sim_client.post("/demo/seed").json()
    assert body["transactions_loaded"] > 250
    assert body["pending"] == body["transactions_loaded"]
    assert body["mandate"]["signature_valid"] is True


def test_seed_endpoint_is_idempotent(sim_client):
    first = sim_client.post("/demo/seed").json()
    second = sim_client.post("/demo/seed").json()
    assert second["transactions_loaded"] == 0
    assert second["pending"] == first["pending"]


def test_get_mandate_returns_terms_and_signature_status(sim_client):
    mandate_id = sim_client.post("/demo/seed").json()["mandate"]["id"]
    body = sim_client.get(f"/mandates/{mandate_id}").json()
    assert body["status"] == "active"
    assert body["signature_valid"] is True
    assert body["merchant_allowlist"]


def test_get_unknown_mandate_is_404(sim_client):
    assert sim_client.get("/mandates/nope").status_code == 404


def test_revoke_endpoint_flips_status(sim_client):
    mandate_id = sim_client.post("/demo/seed").json()["mandate"]["id"]
    assert sim_client.post(f"/mandates/{mandate_id}/revoke").json()["status"] == "revoked"
    assert sim_client.get(f"/mandates/{mandate_id}").json()["status"] == "revoked"


def test_revoke_keeps_the_signature_valid(sim_client):
    mandate_id = sim_client.post("/demo/seed").json()["mandate"]["id"]
    body = sim_client.post(f"/mandates/{mandate_id}/revoke").json()
    assert body["mandate"]["signature_valid"] is True


def test_revoke_unknown_mandate_is_404(sim_client):
    assert sim_client.post("/mandates/nope/revoke").status_code == 404


def test_simulate_streams_sse_events(sim_client):
    mandate_id = sim_client.post("/demo/seed").json()["mandate"]["id"]
    with sim_client.stream(
        "GET", f"/simulate/{mandate_id}?delay_ms=0&limit=5"
    ) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    assert body.count("event: step") == 5
    assert body.count("event: summary") == 1


def test_simulate_unknown_mandate_streams_an_error_event(sim_client):
    sim_client.post("/demo/seed")
    with sim_client.stream("GET", "/simulate/nope?delay_ms=0") as response:
        body = "".join(response.iter_text())
    assert "event: error" in body


# --- dashboard --------------------------------------------------------------


def test_dashboard_renders(sim_client):
    response = sim_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Mandate Compiler" in response.text


def test_dashboard_has_no_external_resources(sim_client):
    """A CDN outage must not be able to break a live demo.

    Checks actual resource references rather than substrings, so prose in the
    page's own comments doesn't trip it.
    """
    import re

    body = sim_client.get("/").text
    external = re.findall(r'(?:src|href)\s*=\s*["\'](?!#)([^"\']+)', body)
    remote = [u for u in external if u.startswith(("http:", "https:", "//"))]
    assert remote == [], f"page loads external resources: {remote}"
    assert "<script" in body and "<style" in body  # everything is inline


def test_list_mandates_reports_pending_count(sim_client):
    sim_client.post("/demo/seed")
    rows = sim_client.get("/mandates").json()
    assert len(rows) == 1
    assert rows[0]["pending"] > 250
    assert rows[0]["status"] == "active"


def test_load_batch_attaches_transactions(sim_client):
    """A mandate compiled from English can be run against the same traffic."""
    from app.models import Mandate
    from app.signing import sign_mandate
    from tests.test_engine import make_mandate

    mandate = sign_mandate(make_mandate())
    mandate.id = "compiled-1"
    sim_client.session.add(mandate)
    sim_client.session.commit()

    body = sim_client.post("/mandates/compiled-1/load-batch").json()
    assert body["transactions_loaded"] > 250
    assert body["pending"] == body["transactions_loaded"]


def test_load_batch_is_idempotent(sim_client):
    mandate_id = sim_client.post("/demo/seed").json()["mandate"]["id"]
    first = sim_client.post(f"/mandates/{mandate_id}/load-batch").json()
    second = sim_client.post(f"/mandates/{mandate_id}/load-batch").json()
    assert second["transactions_loaded"] == 0
    assert second["pending"] == first["pending"]


def test_load_batch_unknown_mandate_is_404(sim_client):
    assert sim_client.post("/mandates/nope/load-batch").status_code == 404


def test_compile_rejects_empty_text(sim_client):
    assert sim_client.post("/compile", json={"text": "   "}).status_code == 400


def test_compile_without_credentials_degrades_gracefully(sim_client, monkeypatch):
    """No API key must produce a reported status, never a 500."""
    def boom(*args, **kwargs):
        raise RuntimeError("Could not resolve authentication method")

    monkeypatch.setattr("app.compiler.compile_policy", boom)
    body = sim_client.post("/compile", json={"text": "spend 500 at zepto"}).json()
    assert body["status"] == "unavailable"
    assert "ANTHROPIC_API_KEY" in body["hint"]


def test_compile_reports_gate_rejection_without_signing(sim_client, monkeypatch):
    """A draft that fails the gate is reported, and no mandate is created."""
    from tests.test_compiler import make_draft

    monkeypatch.setattr(
        "app.compiler.compile_policy",
        lambda *a, **k: make_draft(merchant_allowlist=[]),
    )
    body = sim_client.post("/compile", json={"text": "spend 500 somewhere"}).json()

    assert body["status"] == "rejected"
    assert any("block all spending" in p for p in body["problems"])
    assert "mandate" not in body
    assert sim_client.get("/mandates").json() == []


def test_compile_creates_a_signed_mandate(sim_client, monkeypatch):
    from app.compiler import AmbiguityFlag
    from tests.test_compiler import make_draft

    draft = make_draft(
        ambiguities=[
            AmbiguityFlag(
                field="frequency_cap",
                issue="not stated",
                assumed_value="10",
                clarifying_question="How many orders per week?",
            )
        ]
    )
    monkeypatch.setattr("app.compiler.compile_policy", lambda *a, **k: draft)

    body = sim_client.post("/compile", json={"text": "zepto 500 a week"}).json()
    assert body["status"] == "compiled"
    assert body["mandate"]["signature_valid"] is True
    assert body["mandate"]["status"] == "active"
    # Ambiguities are advisory: they are surfaced but do not block compilation.
    assert body["ambiguities"][0]["field"] == "frequency_cap"


def test_compiled_mandate_is_recorded_in_the_chain(sim_client, monkeypatch):
    from tests.test_compiler import make_draft

    monkeypatch.setattr("app.compiler.compile_policy", lambda *a, **k: make_draft())
    sim_client.post("/compile", json={"text": "zepto 500 a week"})

    log = sim_client.get("/audit/chain").json()
    assert any(e["reason_code"] == "MANDATE_CREATED" for e in log)
