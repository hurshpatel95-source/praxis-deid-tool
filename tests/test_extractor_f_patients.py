"""Tests for Extension F — patients_raw_extension extractor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from praxis_deid.deidentify import Deidentifier
from praxis_deid.extractors import load_mapping_config
from praxis_deid.extractors.base import ExtractorError
from praxis_deid.extractors.extension_f_patients import (
    PatientsExtensionExtractor,
)

PRACTICE_ID = "00000000-0000-0000-0000-0000000000a1"
SALT = "X" * 40
MAPPING_DIR = Path(__file__).resolve().parent.parent / "mappings" / "open_dental"


def _deid():
    return Deidentifier(practice_id=PRACTICE_ID, salt=SALT, small_n_threshold=1)


def _config():
    return load_mapping_config(MAPPING_DIR / "F_patients_raw_extension.json")


def _rows(rs):
    def _f(*a, **k):
        return list(rs)
    return _f


def _row(**overrides):
    base = {
        "patient.PatNum": "PT-1",
        "patient.DateLastVisit": "2026-04-01",
        "patient.ReferredBy": "Google search ad",
        "recall_min_due_aggregated": "2026-10-01",
    }
    base.update(overrides)
    return base


# --- Mapping ------------------------------------------------------------


def test_mapping_loaded():
    cfg = _config()
    assert cfg.canonical_schema_name == "patients_raw_extension"


def test_mapping_extends_patients_raw():
    cfg = _config()
    assert cfg.canonical_schema.extends == "patients_raw"


def test_mapping_required_present():
    cfg = _config()
    assert "last_visit_date" in cfg.column_mappings


# --- Date handling -------------------------------------------------------


def test_last_visit_date_truncated_to_month():
    ex = PatientsExtensionExtractor(_config(), _deid(), _rows([_row()]))
    out = ex.extract()
    assert out[0].last_visit_date_month == "2026-04"
    assert "01" not in out[0].last_visit_date_month or out[0].last_visit_date_month == "2026-01"


def test_recall_due_date_truncated_to_month():
    ex = PatientsExtensionExtractor(_config(), _deid(), _rows([_row()]))
    out = ex.extract()
    assert out[0].recall_due_date_month == "2026-10"


def test_recall_due_date_null_when_no_recall():
    ex = PatientsExtensionExtractor(_config(), _deid(), _rows([
        _row(**{"recall_min_due_aggregated": None}),
    ]))
    out = ex.extract()
    assert out[0].recall_due_date_month is None


def test_drop_row_when_no_real_last_visit():
    ex = PatientsExtensionExtractor(_config(), _deid(), _rows([
        _row(**{"patient.DateLastVisit": None}),
    ]))
    out = ex.extract()
    assert out == []
    assert ex.dropped_rows >= 1


def test_drop_row_when_sentinel_last_visit():
    ex = PatientsExtensionExtractor(_config(), _deid(), _rows([
        _row(**{"patient.DateLastVisit": "0001-01-01"}),
    ]))
    out = ex.extract()
    assert out == []


# --- Referral category --------------------------------------------------


def test_referral_google_categorized_google_ads():
    ex = PatientsExtensionExtractor(_config(), _deid(), _rows([
        _row(**{"patient.ReferredBy": "Google ad campaign"}),
    ]))
    out = ex.extract()
    assert out[0].referral_source_category == "google_ads"


def test_referral_walkin_categorized():
    ex = PatientsExtensionExtractor(_config(), _deid(), _rows([
        _row(**{"patient.ReferredBy": "Walk-in"}),
    ]))
    out = ex.extract()
    assert out[0].referral_source_category == "walk_in"


def test_referral_existing_patient_categorized_internal():
    ex = PatientsExtensionExtractor(_config(), _deid(), _rows([
        _row(**{"patient.ReferredBy": "Existing patient referral"}),
    ]))
    out = ex.extract()
    assert out[0].referral_source_category == "internal"


def test_referral_specialist_categorized_external():
    ex = PatientsExtensionExtractor(_config(), _deid(), _rows([
        _row(**{"patient.ReferredBy": "Dr Smith, specialist"}),
    ]))
    out = ex.extract()
    assert out[0].referral_source_category == "external"


def test_unmapped_referral_categorized_unknown():
    ex = PatientsExtensionExtractor(_config(), _deid(), _rows([
        _row(**{"patient.ReferredBy": "Mysterious source"}),
    ]))
    out = ex.extract()
    assert out[0].referral_source_category == "unknown"
    assert "Mysterious source" in ex.unmapped_referral_sources


def test_referral_null_returns_none():
    ex = PatientsExtensionExtractor(_config(), _deid(), _rows([
        _row(**{"patient.ReferredBy": None}),
    ]))
    out = ex.extract()
    assert out[0].referral_source_category is None


def test_custom_referral_lookup_overrides(tmp_path: Path):
    p = tmp_path / "lookup.json"
    p.write_text(json.dumps({"facebook": "facebook_ads"}), encoding="utf-8")
    ex = PatientsExtensionExtractor(
        _config(),
        _deid(),
        _rows([_row(**{"patient.ReferredBy": "Facebook campaign"})]),
        referral_lookup_path=p,
    )
    out = ex.extract()
    assert out[0].referral_source_category == "facebook_ads"


def test_invalid_referral_lookup_json_raises(tmp_path: Path):
    p = tmp_path / "lookup.json"
    p.write_text("garbage{", encoding="utf-8")
    with pytest.raises(ExtractorError):
        PatientsExtensionExtractor(
            _config(),
            _deid(),
            _rows([_row()]),
            referral_lookup_path=p,
        )


# --- HMACs / cross-extension stability ----------------------------------


def test_patient_external_id_format():
    ex = PatientsExtensionExtractor(_config(), _deid(), _rows([_row()]))
    out = ex.extract()
    assert len(out[0].patient_external_id) == 16
    assert all(c in "0123456789abcdef" for c in out[0].patient_external_id)


def test_patient_hmac_matches_other_extractors():
    """The HMAC of PatNum here must match the patient_external_id in
    every other Phase-C extractor (Extensions A-E) when given the same salt."""
    deid = _deid()
    ex_f = PatientsExtensionExtractor(_config(), deid, _rows([
        _row(**{"patient.PatNum": "PT-CROSS"}),
    ]))
    out_f = ex_f.extract()

    # Compare against Extension A.
    from praxis_deid.extractors.extension_a_treatment_plans import TreatmentPlansExtractor
    a_cfg = load_mapping_config(MAPPING_DIR / "A_treatment_plans_raw.json")
    a_ex = TreatmentPlansExtractor(a_cfg, deid, _rows([
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-CROSS",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0},
    ]))
    a_out = a_ex.extract()
    assert out_f[0].patient_external_id == a_out[0].patient_external_id


# --- No PHI -------------------------------------------------------------


def test_no_phi_columns():
    """PatientExtensionRow must not contain raw PHI."""
    from dataclasses import fields

    from praxis_deid.extractors.rows import PatientExtensionRow
    fns = {f.name for f in fields(PatientExtensionRow)}
    forbidden = {"first_name", "last_name", "dob", "ssn", "email", "phone", "address"}
    assert not (fns & forbidden)


def test_no_phi_in_output_for_pii_laden_input():
    ex = PatientsExtensionExtractor(_config(), _deid(), _rows([
        {**_row(),
         "patient.LName": "Smith",
         "patient.FName": "Alice",
         "patient.SSN": "123-45-6789"},
    ]))
    out = ex.extract()
    blob = repr(out[0]).lower()
    for tip in ["smith", "alice", "123-45-6789"]:
        assert tip not in blob


# --- Idempotency --------------------------------------------------------


def test_idempotency():
    rows = [_row()]
    a = PatientsExtensionExtractor(_config(), _deid(), _rows(rows)).extract()
    b = PatientsExtensionExtractor(_config(), _deid(), _rows(rows)).extract()
    assert a == b


# --- Drops --------------------------------------------------------------


def test_drop_row_missing_pat_num():
    ex = PatientsExtensionExtractor(_config(), _deid(), _rows([
        _row(**{"patient.PatNum": None}),
    ]))
    out = ex.extract()
    assert out == []
    assert ex.dropped_rows >= 1
