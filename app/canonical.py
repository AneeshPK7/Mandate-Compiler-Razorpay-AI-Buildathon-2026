"""Byte-stable serialization.

Both the Ed25519 signature and the audit hash chain are only as trustworthy as
the encoding underneath them: if the same logical record can serialize to two
different byte strings, a valid record can be made to look tampered with (or
worse, a tampered one can be made to verify). Everything that gets signed or
hashed goes through `canonical_bytes`.

Rules, all chosen so the output depends on the data and nothing else:
  - keys sorted, so dict insertion order is irrelevant
  - no insignificant whitespace
  - ASCII-escaped, so the same string encodes identically everywhere
  - datetimes as naive-UTC ISO 8601, so tzinfo representation can't vary
  - enums as their value, not their Python repr

List order is preserved deliberately: reordering a merchant allowlist changes
the stored record, so it should invalidate the signature.
"""

import enum
import json
from datetime import datetime, timezone
from typing import Any


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat(timespec="microseconds")
    if isinstance(value, enum.Enum):
        return _encode(value.value)
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"cannot canonically encode {type(value).__name__}")


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Serialize a mapping to deterministic bytes suitable for signing/hashing."""
    return json.dumps(
        _encode(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
