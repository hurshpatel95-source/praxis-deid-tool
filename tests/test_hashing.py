"""Tests for stable_external_id — the load-bearing piece for re-identification resistance."""

import pytest

from praxis_deid.hashing import ID_LENGTH_HEX, stable_external_id


def test_deterministic() -> None:
    a = stable_external_id("salt-1", "mrn-12345")
    b = stable_external_id("salt-1", "mrn-12345")
    assert a == b


def test_salt_dependence() -> None:
    a = stable_external_id("salt-1", "mrn-12345")
    b = stable_external_id("salt-2", "mrn-12345")
    assert a != b


def test_source_dependence() -> None:
    a = stable_external_id("salt-1", "mrn-12345")
    b = stable_external_id("salt-1", "mrn-12346")
    assert a != b


def test_int_and_str_source_match_when_str_repr_matches() -> None:
    assert stable_external_id("s", 12345) == stable_external_id("s", "12345")


def test_length_and_hex() -> None:
    out = stable_external_id("s", "x")
    assert len(out) == ID_LENGTH_HEX
    int(out, 16)  # hex-decodable


def test_empty_salt_rejected() -> None:
    with pytest.raises(ValueError):
        stable_external_id("", "x")


def test_collision_resistance_smoke() -> None:
    # 10k random source ids should not collide at 64-bit space.
    ids = {stable_external_id("salt", i) for i in range(10_000)}
    assert len(ids) == 10_000


def test_non_reversibility_smoke() -> None:
    """Without the salt, brute-forcing 7-digit source ids should not
    produce the right one in practice. We don't claim cryptographic
    proof — just that the ID doesn't trivially leak the source."""
    secret_salt = "the-real-salt"
    target_source = "patient-9999999"
    target_id = stable_external_id(secret_salt, target_source)

    # Attacker knows the candidate space (7-digit MRNs) but not the salt.
    # They guess a wrong salt and try every candidate.
    wrong_salt = "guess-salt"
    found = any(
        stable_external_id(wrong_salt, f"patient-{i:07d}") == target_id
        for i in range(0, 100)  # tiny slice; smoke only
    )
    assert not found
