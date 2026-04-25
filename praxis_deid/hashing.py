"""Stable, non-reversible patient ID generation.

Uses HMAC-SHA256 with a practice-held salt. Properties:
  * Deterministic: same (salt, source_id) -> same external_id, always.
    Lets Praxis recognize the same patient across runs for trend analysis.
  * Salt-bound: changing the salt re-keys every external_id. Practice can
    rotate the salt to invalidate every prior export if needed.
  * Non-reversible: external_id leaks no information about source_id without
    the salt. SHA-256 collision resistance is the guarantee.
  * Truncated to 16 hex chars (64 bits): 64-bit collision space is enough
    for any single practice (well under birthday-bound for 10M patients).
    Full 256 bits is wasteful in CSV.

Threat model: an attacker who obtains the de-identified output but NOT the
salt cannot recover source identifiers. An attacker who obtains BOTH (e.g.,
the practice itself, or its backups) can. That's by design — re-identification
must be possible at the practice for legal subpoena response, etc., but
impossible at Praxis cloud which never sees the salt.
"""

from __future__ import annotations

import hashlib
import hmac

ID_LENGTH_HEX = 16  # 64 bits; well under birthday bound for any practice's patient count


def stable_external_id(salt: str, source_id: str | int) -> str:
    """HMAC-SHA256 of source_id with the practice's salt, hex-truncated to 16 chars."""
    if not salt:
        raise ValueError("salt must be a non-empty string")
    digest = hmac.new(
        salt.encode("utf-8"),
        str(source_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:ID_LENGTH_HEX]
