from datetime import datetime

import pytest
from nacl.signing import SigningKey

from app.canonical import canonical_bytes
from app.models import MandateStatus, Period
from app.signing import (
    SignatureError,
    load_signing_key,
    public_key_hex,
    sign_mandate,
    signing_payload,
    verify_mandate,
)
from tests.test_engine import make_mandate


@pytest.fixture
def key():
    return SigningKey(b"\x01" * 32)


# --- canonical serialization ------------------------------------------------


def test_key_order_does_not_change_encoding():
    assert canonical_bytes({"a": 1, "b": 2}) == canonical_bytes({"b": 2, "a": 1})


def test_encoding_is_whitespace_free():
    assert canonical_bytes({"a": 1, "b": "x"}) == b'{"a":1,"b":"x"}'


def test_aware_and_naive_utc_datetimes_encode_identically():
    from datetime import timezone

    naive = canonical_bytes({"t": datetime(2026, 6, 10, 12, 0)})
    aware = canonical_bytes({"t": datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)})
    assert naive == aware


def test_enum_encodes_as_value():
    assert canonical_bytes({"p": Period.week}) == b'{"p":"week"}'


def test_non_ascii_is_escaped_stably():
    once = canonical_bytes({"m": "café"})
    assert b"caf" in once and b"\\u00e9" in once


def test_unencodable_type_raises():
    with pytest.raises(TypeError):
        canonical_bytes({"x": object()})


def test_list_order_is_significant():
    """Reordering an allowlist changes the record, so it must change the bytes."""
    assert canonical_bytes({"l": ["a", "b"]}) != canonical_bytes({"l": ["b", "a"]})


# --- signing round trip -----------------------------------------------------


def test_signed_mandate_verifies(key):
    mandate = sign_mandate(make_mandate(), key)
    assert mandate.signature
    assert verify_mandate(mandate, key.verify_key)


def test_unsigned_mandate_does_not_verify(key):
    assert verify_mandate(make_mandate(), key.verify_key) is False


def test_signing_is_deterministic(key):
    a = sign_mandate(make_mandate(), key).signature
    b = sign_mandate(make_mandate(), key).signature
    assert a == b


def test_wrong_key_does_not_verify(key):
    mandate = sign_mandate(make_mandate(), key)
    other = SigningKey(b"\x02" * 32)
    assert verify_mandate(mandate, other.verify_key) is False


def test_malformed_signature_returns_false_not_raises(key):
    mandate = sign_mandate(make_mandate(), key)
    mandate.signature = "not-hex"
    assert verify_mandate(mandate, key.verify_key) is False


def test_truncated_signature_returns_false(key):
    mandate = sign_mandate(make_mandate(), key)
    mandate.signature = mandate.signature[:-4]
    assert verify_mandate(mandate, key.verify_key) is False


def test_verify_accepts_hex_public_key(key):
    mandate = sign_mandate(make_mandate(), key)
    assert verify_mandate(mandate, public_key_hex(key))


# --- tampering with grant terms ---------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount_cap_per_txn", 999_999),
        ("amount_cap_period", 999_999),
        ("merchant_allowlist", ["amazon"]),
        ("category_exclusions", []),
        ("time_window_start", "00:00"),
        ("time_window_end", "23:59"),
        ("frequency_cap", 9999),
        ("agent_id", "attacker-agent"),
        ("principal_id", "someone-else"),
        ("valid_until", datetime(2099, 1, 1)),
        ("version", 99),
        ("id", "different-id"),
    ],
)
def test_altering_any_granted_term_breaks_the_signature(key, field, value):
    mandate = sign_mandate(make_mandate(), key)
    setattr(mandate, field, value)
    assert verify_mandate(mandate, key.verify_key) is False


def test_raising_a_cap_by_one_paisa_is_detected(key):
    """Signature strength shouldn't depend on how large the edit is."""
    mandate = sign_mandate(make_mandate(), key)
    mandate.amount_cap_per_txn += 1
    assert verify_mandate(mandate, key.verify_key) is False


def test_reordering_the_allowlist_breaks_the_signature(key):
    mandate = sign_mandate(make_mandate(), key)
    mandate.merchant_allowlist = list(reversed(mandate.merchant_allowlist))
    assert verify_mandate(mandate, key.verify_key) is False


# --- status is deliberately outside the signature ---------------------------


def test_revocation_preserves_the_signature(key):
    """Revoking is a legitimate act and must not look like forgery.

    Status transitions are made tamper-evident by the audit chain instead —
    see the module docstring in app/signing.py.
    """
    mandate = sign_mandate(make_mandate(), key)
    mandate.status = MandateStatus.revoked
    assert verify_mandate(mandate, key.verify_key)


def test_status_is_not_in_the_signing_payload(key):
    mandate = make_mandate()
    before = signing_payload(mandate)
    mandate.status = MandateStatus.revoked
    assert signing_payload(mandate) == before


def test_signature_field_is_not_self_referential(key):
    """The payload must exclude the signature it produces."""
    assert b"signature" not in signing_payload(make_mandate())


# --- key loading ------------------------------------------------------------


def test_env_key_is_used_when_set(monkeypatch, tmp_path):
    seed = "ab" * 32
    monkeypatch.setenv("MANDATE_SIGNING_KEY", seed)
    assert bytes(load_signing_key()) == bytes.fromhex(seed)


def test_bad_hex_env_key_raises(monkeypatch):
    monkeypatch.setenv("MANDATE_SIGNING_KEY", "zzzz")
    with pytest.raises(SignatureError, match="not valid hex"):
        load_signing_key()


def test_wrong_length_env_key_raises(monkeypatch):
    monkeypatch.setenv("MANDATE_SIGNING_KEY", "ab" * 16)
    with pytest.raises(SignatureError, match="32-byte"):
        load_signing_key()


def test_dev_key_persists_across_calls(monkeypatch, tmp_path):
    monkeypatch.delenv("MANDATE_SIGNING_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("app.signing.DEV_KEY_PATH", tmp_path / ".signing_key")
    first = bytes(load_signing_key())
    assert bytes(load_signing_key()) == first
