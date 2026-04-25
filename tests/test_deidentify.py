"""End-to-end tests for the Deidentifier — the spec's core invariants."""

from __future__ import annotations

from dataclasses import asdict, fields

import pytest

from praxis_deid.deidentify import Deidentifier
from praxis_deid.schema import FORBIDDEN_FIELDS

PRACTICE_ID = "00000000-0000-0000-0000-0000000000a1"
SALT = "test-practice-salt"


def _make() -> Deidentifier:
    # small_n_threshold=1 so single-row tests don't get suppressed.
    return Deidentifier(practice_id=PRACTICE_ID, salt=SALT, small_n_threshold=1)


# --- Spec invariant 1: no PHI fields in output ----------------------------

def test_output_has_no_forbidden_fields() -> None:
    d = _make()
    d.add_patient(
        {
            "source_id": "MRN-001",
            "first_name": "Alice",
            "last_name": "Smith",
            "dob": "1985-03-12",
            "ssn": "123-45-6789",
            "phone": "609-555-1212",
            "email": "alice@example.com",
            "address": "123 Main St",
            "zip": "08201",
            "gender": "F",
            "payer_category": "BCBS",
            "patient_status": "active",
            "first_seen_date": "2026-01-15",
        }
    )
    patients, *_ = d.finalize()
    assert len(patients) == 1
    out_fields = {f.name for f in fields(patients[0])}
    leaked = out_fields & FORBIDDEN_FIELDS
    assert not leaked, f"forbidden fields in output: {leaked}"

    # Belt-and-braces: stringified row contains no PHI substrings.
    blob = repr(asdict(patients[0])).lower()
    for tip in ["alice", "smith", "123-45-6789", "609-555-1212", "alice@example.com", "123 main st"]:
        assert tip.lower() not in blob, f"{tip!r} leaked into {blob!r}"


# --- Spec invariant 2: stable IDs across runs -----------------------------

def test_patient_external_id_is_stable_across_runs() -> None:
    a = _make()
    a.add_patient(_minimal_patient("MRN-001"))
    b = _make()
    b.add_patient(_minimal_patient("MRN-001"))
    pa, *_ = a.finalize()
    pb, *_ = b.finalize()
    assert pa[0].external_id == pb[0].external_id


def test_patient_external_id_is_salt_dependent() -> None:
    a = Deidentifier(PRACTICE_ID, "salt-1", small_n_threshold=1)
    b = Deidentifier(PRACTICE_ID, "salt-2", small_n_threshold=1)
    a.add_patient(_minimal_patient("MRN-001"))
    b.add_patient(_minimal_patient("MRN-001"))
    pa, *_ = a.finalize()
    pb, *_ = b.finalize()
    assert pa[0].external_id != pb[0].external_id


# --- Spec invariant 3: dates always month granularity --------------------

def test_dates_are_month_granular() -> None:
    d = _make()
    d.add_patient(_minimal_patient("MRN-1"))
    d.add_appointment(
        {
            "source_id": "APT-1",
            "patient_source_id": "MRN-1",
            "provider_id": "prov-1",
            "appointment_date": "2026-04-15",
            "appointment_type_category": "routine",
            "status": "completed",
            "duration_minutes": "30",
        }
    )
    patients, appts, *_ = d.finalize()
    assert appts[0].appointment_date_month == "2026-04"
    # The day component is gone.
    assert "15" not in appts[0].appointment_date_month


# --- Spec invariant 4: small-N suppression --------------------------------

def test_small_n_suppression_drops_lone_patient_with_no_touches() -> None:
    d = Deidentifier(PRACTICE_ID, SALT, small_n_threshold=5)
    # One patient, no appointments / procedures, unique demographic stratum.
    d.add_patient(_minimal_patient("MRN-1"))
    patients, *_ = d.finalize()
    assert patients == []
    assert d.stats.small_n_suppressions == 1


def test_small_n_suppression_keeps_patient_with_threshold_touches() -> None:
    d = Deidentifier(PRACTICE_ID, SALT, small_n_threshold=3)
    d.add_patient(_minimal_patient("MRN-1"))
    # Three appointments => threshold met.
    for i in range(3):
        d.add_appointment(
            {
                "source_id": f"APT-{i}",
                "patient_source_id": "MRN-1",
                "provider_id": "prov-1",
                "appointment_date": "2026-04-15",
                "appointment_type_category": "routine",
                "status": "completed",
                "duration_minutes": "30",
            }
        )
    patients, appts, *_ = d.finalize()
    assert len(patients) == 1
    assert len(appts) == 3


def test_dependent_rows_dropped_when_patient_suppressed() -> None:
    d = Deidentifier(PRACTICE_ID, SALT, small_n_threshold=5)
    # Patient with only 2 appointments → patient suppressed → appointments
    # of that patient also dropped.
    d.add_patient(_minimal_patient("MRN-LONE"))
    for i in range(2):
        d.add_appointment(
            {
                "source_id": f"APT-{i}",
                "patient_source_id": "MRN-LONE",
                "provider_id": "prov-1",
                "appointment_date": "2026-04-15",
                "appointment_type_category": "routine",
                "status": "completed",
                "duration_minutes": "30",
            }
        )
    patients, appts, *_ = d.finalize()
    assert patients == []
    assert appts == []


# --- Spec invariant 5: validation rejects malformed rows -----------------

def test_invalid_row_dropped_with_reason() -> None:
    d = _make()
    d.add_patient(
        {
            "source_id": "MRN-1",
            "dob": "not-a-date",
            "zip": "08201",
            "gender": "F",
            "payer_category": "commercial",
            "patient_status": "active",
            "first_seen_date": "2026-01-01",
        }
    )
    patients, *_ = d.finalize()
    assert patients == []
    assert d.stats.rows_dropped == 1
    assert any("dob" in k for k in d.stats.drop_reasons)


# --- Spec invariant 6: ZIP suppression --------------------------------

def test_restricted_zip_suppressed_to_000() -> None:
    d = _make()
    d.add_patient(_minimal_patient("MRN-1", zip_code="03600"))
    patients, *_ = d.finalize()
    # 036 is in the restricted list.
    assert patients[0].zip_prefix == "000"


# --- Helper -----------------------------------------------------------------

def _minimal_patient(source_id: str, *, zip_code: str = "08201") -> dict[str, str]:
    return {
        "source_id": source_id,
        "dob": "1985-03-12",
        "zip": zip_code,
        "gender": "F",
        "payer_category": "commercial",
        "patient_status": "active",
        "first_seen_date": "2026-01-15",
    }
