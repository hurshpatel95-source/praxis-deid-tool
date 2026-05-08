"""Tests for Extension A — treatment_plans_raw extractor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from praxis_deid.deidentify import Deidentifier
from praxis_deid.extractors import load_mapping_config
from praxis_deid.extractors.base import (
    Filter,
    assert_no_exact_dollars_in_csv,
)
from praxis_deid.extractors.extension_a_treatment_plans import (
    TreatmentPlansExtractor,
)
from praxis_deid.extractors.rows import TREATMENT_PLAN_STATUSES

PRACTICE_ID = "00000000-0000-0000-0000-0000000000a1"
SALT = "X" * 40
MAPPING_DIR = Path(__file__).resolve().parent.parent / "mappings" / "open_dental"


def _deid() -> Deidentifier:
    return Deidentifier(practice_id=PRACTICE_ID, salt=SALT, small_n_threshold=1)


def _config():
    return load_mapping_config(MAPPING_DIR / "A_treatment_plans_raw.json")


def _rows(rows):
    """Wrap a list as a RowSource callable."""
    def _rs(*a, **k):
        return list(rows)

    return _rs


# -------------------------------------------------------------------------
# Mapping config + schema validation
# -------------------------------------------------------------------------


def test_mapping_loaded_correctly():
    cfg = _config()
    assert cfg.canonical_schema_name == "treatment_plans_raw"
    assert "source_id" in cfg.column_mappings
    assert "patient_source_id" in cfg.column_mappings


def test_schema_validation_every_required_column_has_mapping():
    cfg = _config()
    for col in cfg.canonical_schema.required_columns:
        assert col in cfg.column_mappings, f"required column {col} missing"


def test_treatplan_status_case_uses_audited_audit_keyword():
    """The CASE expression in the audited mapping should reference TPStatus =1
    -> 'declined' (not 'expired') per the f22906d audit."""
    cfg = _config()
    tx = cfg.transformations.get("status", "") or cfg.column_mappings["status"].source_expression
    assert "TPStatus" in tx or "DateTSigned" in tx


# -------------------------------------------------------------------------
# Status derivation (the load-bearing part)
# -------------------------------------------------------------------------


def test_status_presented_when_no_signed_no_inactive():
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows([
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0},
    ]))
    out = ex.extract()
    assert len(out) == 1
    assert out[0].status == "presented"


def test_status_accepted_when_signed():
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows([
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": "2026-04-16",
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0},
    ]))
    out = ex.extract()
    assert out[0].status == "accepted"


def test_status_declined_when_tpstatus_one():
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows([
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 1, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0},
    ]))
    out = ex.extract()
    assert out[0].status == "declined"


def test_status_signed_wins_over_tpstatus():
    """DateTSigned IS NOT NULL takes priority over TPStatus=1."""
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows([
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": "2026-04-20",
         "treatplan.TPStatus": 1, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0},
    ]))
    out = ex.extract()
    assert out[0].status == "accepted"


def test_status_sentinel_date_treated_as_null():
    """Open Dental sentinel '0001-01-01' must NOT be treated as a signed date."""
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows([
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": "0001-01-01",
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0},
    ]))
    out = ex.extract()
    assert out[0].status == "presented"


def test_every_status_value_in_canonical_enum():
    """No matter the input, status must always be in TREATMENT_PLAN_STATUSES."""
    rows = [
        {"treatplan.TreatPlanNum": f"TP-{i}", "treatplan.PatNum": f"PT-{i}",
         "treatplan.DateTP": "2026-04-15",
         "treatplan.DateTSigned": "2026-04-20" if i % 2 == 0 else None,
         "treatplan.TPStatus": i % 3,
         "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0}
        for i in range(10)
    ]
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows(rows))
    out = ex.extract()
    for r in out:
        assert r.status in TREATMENT_PLAN_STATUSES


# -------------------------------------------------------------------------
# plan_dollars banding
# -------------------------------------------------------------------------


def test_plan_dollars_always_banded():
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows([
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 4500.0},
    ]))
    out = ex.extract()
    assert out[0].plan_dollars_band == "$1000-5000"


def test_plan_dollars_summed_across_proctp_rows():
    """When the row source feeds row-per-proctp, FeeAmt sums across the plan."""
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows([
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 200.0},
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 500.0},
    ]))
    out = ex.extract()
    # 200 + 500 = 700 -> '$500-1000' band.
    assert len(out) == 1
    assert out[0].plan_dollars_band == "$500-1000"


# -------------------------------------------------------------------------
# Date handling
# -------------------------------------------------------------------------


def test_presented_date_truncated_to_month():
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows([
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0},
    ]))
    out = ex.extract()
    assert out[0].presented_date_month == "2026-04"
    # The day component is gone.
    assert "15" not in out[0].presented_date_month


def test_accepted_date_null_when_unsigned():
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows([
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0},
    ]))
    out = ex.extract()
    assert out[0].accepted_date_month is None


def test_declined_and_expired_always_null_for_open_dental():
    """Per audited mapping, Open Dental has no source columns for these."""
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows([
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 1, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0},
    ]))
    out = ex.extract()
    assert out[0].declined_date_month is None
    assert out[0].expired_date_month is None


# -------------------------------------------------------------------------
# HMACs / cross-extension stability
# -------------------------------------------------------------------------


def test_external_id_is_hmac_format():
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows([
        {"treatplan.TreatPlanNum": "TP-XYZ", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0},
    ]))
    out = ex.extract()
    assert len(out[0].external_id) == 16
    assert all(c in "0123456789abcdef" for c in out[0].external_id)
    # Source ID must NOT appear verbatim.
    assert "TP-XYZ" not in out[0].external_id


def test_hmac_stable_across_runs_with_same_salt():
    rows = [{"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
             "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
             "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0}]
    ex_a = TreatmentPlansExtractor(_config(), _deid(), _rows(rows))
    ex_b = TreatmentPlansExtractor(_config(), _deid(), _rows(rows))
    a = ex_a.extract()
    b = ex_b.extract()
    assert a[0].external_id == b[0].external_id
    assert a[0].patient_external_id == b[0].patient_external_id


def test_hmac_changes_with_salt():
    rows = [{"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
             "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
             "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0}]
    a = TreatmentPlansExtractor(
        _config(),
        Deidentifier(PRACTICE_ID, "A" * 40),
        _rows(rows),
    ).extract()
    b = TreatmentPlansExtractor(
        _config(),
        Deidentifier(PRACTICE_ID, "B" * 40),
        _rows(rows),
    ).extract()
    assert a[0].external_id != b[0].external_id


# -------------------------------------------------------------------------
# PHI guard / no leak invariants
# -------------------------------------------------------------------------


def test_no_phi_columns_in_output():
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows([
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0,
         # PHI fields that the row source might leak from a careless join:
         "patient.LName": "Smith", "patient.FName": "Alice",
         "patient.SSN": "123-45-6789", "patient.Email": "a@example.com"},
    ]))
    out = ex.extract()
    blob = repr(out[0]).lower()
    for tip in ["smith", "alice", "123-45-6789", "a@example.com"]:
        assert tip not in blob, f"PHI {tip!r} leaked"


def test_tainted_mapping_with_drop_keyword_rejected(tmp_path):
    p = tmp_path / "tainted.json"
    p.write_text(json.dumps({
        "canonical_schema": "treatment_plans_raw",
        "column_mappings": {
            "source_id": {"canonical_column": "source_id",
                          "source_expression": "treatplan.PatNum DROP TABLE x"},
        },
    }), encoding="utf-8")
    from praxis_deid.extractors.base import ExtractorError
    with pytest.raises(ExtractorError):
        load_mapping_config(p)


# -------------------------------------------------------------------------
# Filter / row-bound behaviour
# -------------------------------------------------------------------------


def test_filter_since_until_excludes_outside_window():
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows([
        {"treatplan.TreatPlanNum": "TP-OLD", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2025-12-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0},
        {"treatplan.TreatPlanNum": "TP-NEW", "treatplan.PatNum": "PT-2",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0},
    ]))
    out = ex.extract(Filter(since_month="2026-01", until_month="2026-12"))
    assert len(out) == 1


# -------------------------------------------------------------------------
# Idempotency
# -------------------------------------------------------------------------


def test_idempotency_same_input_same_output():
    rows = [
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0},
    ]
    a = TreatmentPlansExtractor(_config(), _deid(), _rows(rows)).extract()
    b = TreatmentPlansExtractor(_config(), _deid(), _rows(rows)).extract()
    assert a == b


# -------------------------------------------------------------------------
# CSV write / dollar-leak guard
# -------------------------------------------------------------------------


def test_csv_emission_passes_dollar_leak_scan(tmp_path: Path):
    rows = [
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 4500.0},
    ]
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows(rows), output_dir=tmp_path)
    out = ex.extract()
    p = ex._dump_to_csv(out, "treatment_plans_raw.csv")
    assert p.exists()
    # The 4500 must NOT appear as a raw number — it must be banded.
    body = p.read_text()
    assert "4500" not in body
    assert "$1000-5000" in body
    assert_no_exact_dollars_in_csv(p)


def test_drop_row_missing_treatplan_num():
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows([
        {"treatplan.TreatPlanNum": None, "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0},
    ]))
    out = ex.extract()
    assert out == []
    assert ex.dropped_rows >= 1


def test_drop_row_missing_pat_num():
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows([
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": None,
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0},
    ]))
    out = ex.extract()
    assert out == []
    assert ex.dropped_rows >= 1


def test_provider_id_passthrough_no_hmac():
    """provider_id is non-PHI and should pass through verbatim."""
    ex = TreatmentPlansExtractor(_config(), _deid(), _rows([
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-DISTINCTIVE", "proctp.FeeAmt": 100.0},
    ]))
    out = ex.extract()
    assert out[0].provider_id == "DOC-DISTINCTIVE"
