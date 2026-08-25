"""Ed25519 signing of mandates.

What the signature covers
-------------------------
The signature attests to the *grant terms*: who authorized whom, the caps, the
allowlist, the window, and the version. It deliberately does NOT cover
`status`.

That is a design choice, not an oversight. Revocation is a legitimate act by
the principal, so including `status` in the signed payload would mean either
(a) revocation invalidates the signature, making a revoked mandate
indistinguishable from a forged one, or (b) re-signing on every transition,
which erodes the signature's meaning as an attestation of the original grant.

So the two mechanisms split the work:
  - the *signature* makes the terms tamper-evident
  - the *audit chain* makes status transitions tamper-evident

Neither alone is sufficient; together they cover the record. A mandate whose
terms were edited in the database fails signature verification; a mandate
silently flipped from revoked back to active leaves a hole in the audit chain.

Key management
--------------
Development keys are generated on demand and persisted to a gitignored file.
This is fine for a test-mode demo and is NOT how you would run this in
production, where the private key belongs in an HSM or KMS and signing happens
behind an authenticated service boundary.
"""

from __future__ import annotations

import os
from pathlib import Path

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from app.canonical import canonical_bytes
from app.models import Mandate

KEY_ENV_VAR = "MANDATE_SIGNING_KEY"
DEV_KEY_PATH = Path(".signing_key")

# Fields covered by the signature. Explicit rather than "everything except X"
# so that adding a field to the model is a deliberate decision about whether it
# is part of the grant.
SIGNED_FIELDS = (
    "id",
    "principal_id",
    "agent_id",
    "amount_cap_per_txn",
    "amount_cap_period",
    "period",
    "merchant_allowlist",
    "category_exclusions",
    "time_window_start",
    "time_window_end",
    "frequency_cap",
    "valid_from",
    "valid_until",
    "version",
)


class SignatureError(Exception):
    """Signature verification failed or could not be attempted."""


def signing_payload(mandate: Mandate) -> bytes:
    """The exact bytes covered by a mandate's signature."""
    return canonical_bytes({field: getattr(mandate, field) for field in SIGNED_FIELDS})


def load_signing_key() -> SigningKey:
    """Resolve the signing key: env var first, then a persisted dev key.

    The env var holds a 64-char hex seed. If neither source exists, a key is
    generated and written to DEV_KEY_PATH (gitignored) so repeated runs verify
    against each other.
    """
    seed_hex = os.environ.get(KEY_ENV_VAR)
    if seed_hex:
        try:
            seed = bytes.fromhex(seed_hex.strip())
        except ValueError as exc:
            raise SignatureError(f"{KEY_ENV_VAR} is not valid hex") from exc
        if len(seed) != 32:
            raise SignatureError(
                f"{KEY_ENV_VAR} must be a 32-byte (64 hex char) seed, got {len(seed)} bytes"
            )
        return SigningKey(seed)

    if DEV_KEY_PATH.exists():
        return SigningKey(bytes.fromhex(DEV_KEY_PATH.read_text().strip()))

    key = SigningKey.generate()
    DEV_KEY_PATH.write_text(bytes(key).hex())
    DEV_KEY_PATH.chmod(0o600)
    return key


def public_key_hex(key: SigningKey | None = None) -> str:
    """Hex-encoded public key, for publishing alongside the audit trail."""
    key = key or load_signing_key()
    return bytes(key.verify_key).hex()


def sign_mandate(mandate: Mandate, key: SigningKey | None = None) -> Mandate:
    """Attach an Ed25519 signature over the mandate's grant terms.

    Mutates and returns the mandate for convenience at call sites.
    """
    key = key or load_signing_key()
    mandate.signature = key.sign(signing_payload(mandate)).signature.hex()
    return mandate


def verify_mandate(mandate: Mandate, verify_key: VerifyKey | str | None = None) -> bool:
    """True if the mandate's terms still match its signature.

    Returns False for a missing or malformed signature rather than raising, so
    that an unsigned mandate and a tampered one are handled the same way by
    callers: neither is trustworthy.
    """
    if not mandate.signature:
        return False

    if verify_key is None:
        verify_key = load_signing_key().verify_key
    elif isinstance(verify_key, str):
        verify_key = VerifyKey(bytes.fromhex(verify_key))

    try:
        verify_key.verify(signing_payload(mandate), bytes.fromhex(mandate.signature))
    except (BadSignatureError, ValueError):
        return False
    return True
