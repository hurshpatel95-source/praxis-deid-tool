"""Tests for Extension B — claims_raw extractor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from praxis_deid.deidentify import Deidentifier
from praxis_deid.extractors import load_mapping_config
from praxis_deid.extractors.base import ExtractorError
from praxis_deid.extractors.extension_b_claims import ClaimsExtractor
from praxis_deid.extractors.rows import CLAIM_STATUSES

PRACTICE_ID = "00000000-0000-0000-0000-0000000000a1"
SALT = "X" * 40
MAPPING_DIR = Path(__file__).resolve().parent.parent / "mappings" / "open_dental"


def _deid() -> Deidentifier:
    return Deidentifier(practice_id=PRACTICE_ID, salt=SALT, small_n_threshold=1)


def _config():
    return load_mapping_config(MAPPING_DIR / "B_claims_raw.json")


def _rows(rs):
    def _f(*a, **k):
        return list(rs)
    return _f


def _row(**overrides):
    base = {
        "claim.ClaimNum": "CLM-1",
        "claim.PatNum": "PT-1",
        "claim.DateSent": "2026-04-15",
        "claim.ClaimStatus": "R",
        "claim.PreAuthString": None,
        "claim.DateService": "2026-04-01",
        "carrier.CarrierName": "Aetna PPO",
        "claim.PaymentDate_aggregated": "2026-04-25",
        "claim.PreVerified_aggregated": True,
    }
    base.update(overrides)
    return base


# --- Mapping config validation -----------------------------------------


def test_mapping_loaded():
    cfg = _config()
    assert cfg.canonical_schema_name == "claims_raw"


def test_mapping_required_columns_all_have_entries():
    cfg = _config()
    for col in cfg.canonical_schema.required_columns:
        assert col in cfg.column_mappings


def test_mapping_payment_date_uses_status_one_not_two():
    """The audit caught this: claimproc.Status=1 is paid, NOT Status=2 (Preauth)."""
    cfg = _config()
    expr = cfg.column_mappings["payment_date"].source_expression
    assert "Status = 1" in expr
    assert "Status = 2" not in expr


# --- Status derivation -------------------------------------------------


def test_claim_status_R_paid():
    ex = ClaimsExtractor(_config(), _deid(), _rows([_row(**{"claim.ClaimStatus": "R"})]))
    out = ex.extract()
    assert out[0].status == "paid"


def test_claim_status_S_submitted():
    ex = ClaimsExtractor(_config(), _deid(), _rows([_row(**{"claim.ClaimStatus": "S"})]))
    out = ex.extract()
    assert out[0].status == "submitted"


def test_claim_status_W_pending():
    ex = ClaimsExtractor(_config(), _deid(), _rows([_row(**{"claim.ClaimStatus": "W"})]))
    out = ex.extract()
    assert out[0].status == "pending"


def test_claim_status_unknown_falls_back_to_pending():
    ex = ClaimsExtractor(_config(), _deid(), _rows([_row(**{"claim.ClaimStatus": "Z"})]))
    out = ex.extract()
    assert out[0].status == "pending"


def test_every_status_in_canonical_enum():
    rows = [
        _row(**{
            "claim.ClaimNum": f"CLM-{i}",
            "claim.PatNum": f"PT-{i}",
            "claim.ClaimStatus": s,
        })
        for i, s in enumerate("RWHIASUP")
    ]
    ex = ClaimsExtractor(_config(), _deid(), _rows(rows))
    for r in ex.extract():
        assert r.status in CLAIM_STATUSES


# --- Payer category derivation -----------------------------------------


def test_carrier_aetna_categorized_commercial():
    ex = ClaimsExtractor(_config(), _deid(), _rows([
        _row(**{"carrier.CarrierName": "Aetna PPO"}),
    ]))
    out = ex.extract()
    assert out[0].payer_category == "commercial"


def test_carrier_medicare_categorized_medicare():
    ex = ClaimsExtractor(_config(), _deid(), _rows([
        _row(**{"carrier.CarrierName": "Medicare Part B"}),
    ]))
    out = ex.extract()
    assert out[0].payer_category == "medicare"


def test_carrier_medicaid_categorized_medicaid():
    ex = ClaimsExtractor(_config(), _deid(), _rows([
        _row(**{"carrier.CarrierName": "NJ FamilyCare Medicaid"}),
    ]))
    out = ex.extract()
    assert out[0].payer_category == "medicaid"


def test_unknown_carrier_falls_back_to_other_and_records():
    ex = ClaimsExtractor(_config(), _deid(), _rows([
        _row(**{"carrier.CarrierName": "Quirky Local Plan"}),
    ]))
    out = ex.extract()
    assert out[0].payer_category == "other"
    assert "Quirky Local Plan" in ex.unmapped_carriers


def test_empty_carrier_falls_back_to_other():
    ex = ClaimsExtractor(_config(), _deid(), _rows([_row(**{"carrier.CarrierName": ""})]))
    out = ex.extract()
    assert out[0].payer_category == "other"


# --- Authorization required ---------------------------------------------


def test_authorization_required_when_preauth_string_present():
    ex = ClaimsExtractor(_config(), _deid(), _rows([
        _row(**{"claim.PreAuthString": "AUTH123"})]))
    out = ex.extract()
    assert out[0].authorization_required is True


def test_authorization_not_required_when_preauth_empty():
    ex = ClaimsExtractor(_config(), _deid(), _rows([_row(**{"claim.PreAuthString": ""})]))
    out = ex.extract()
    assert out[0].authorization_required is False


def test_authorization_not_required_when_preauth_null():
    ex = ClaimsExtractor(_config(), _deid(), _rows([_row(**{"claim.PreAuthString": None})]))
    out = ex.extract()
    assert out[0].authorization_required is False


# --- Date handling ------------------------------------------------------


def test_submission_date_truncated_to_month():
    ex = ClaimsExtractor(_config(), _deid(), _rows([_row(**{"claim.DateSent": "2026-04-22"})]))
    out = ex.extract()
    assert out[0].submission_date_month == "2026-04"
    assert "22" not in out[0].submission_date_month


def test_payment_date_null_when_not_paid():
    ex = ClaimsExtractor(_config(), _deid(), _rows([
        _row(**{"claim.PaymentDate_aggregated": None})]))
    out = ex.extract()
    assert out[0].payment_date_month is None


def test_payment_date_sentinel_treated_as_null():
    ex = ClaimsExtractor(_config(), _deid(), _rows([
        _row(**{"claim.PaymentDate_aggregated": "0001-01-01"})]))
    out = ex.extract()
    assert out[0].payment_date_month is None


def test_denial_date_always_null_for_open_dental():
    ex = ClaimsExtractor(_config(), _deid(), _rows([_row()]))
    out = ex.extract()
    assert out[0].denial_date_month is None


# --- Pre-verified -------------------------------------------------------


def test_pre_verified_passthrough_true():
    ex = ClaimsExtractor(_config(), _deid(), _rows([
        _row(**{"claim.PreVerified_aggregated": True})]))
    out = ex.extract()
    assert out[0].pre_verified is True


def test_pre_verified_false_when_missing():
    ex = ClaimsExtractor(_config(), _deid(), _rows([
        _row(**{"claim.PreVerified_aggregated": None})]))
    out = ex.extract()
    assert out[0].pre_verified is False


def test_pre_verified_handles_string_truthy():
    ex = ClaimsExtractor(_config(), _deid(), _rows([
        _row(**{"claim.PreVerified_aggregated": "1"})]))
    out = ex.extract()
    assert out[0].pre_verified is True


# --- HMACs --------------------------------------------------------------


def test_external_id_hmac_format():
    ex = ClaimsExtractor(_config(), _deid(), _rows([_row(**{"claim.ClaimNum": "CLM-XYZ"})]))
    out = ex.extract()
    assert len(out[0].external_id) == 16
    assert "CLM-XYZ" not in out[0].external_id


def test_patient_external_id_matches_treatment_plans_extractor():
    """Cross-extension stability: same PatNum -> same external_id everywhere."""
    from praxis_deid.extractors.extension_a_treatment_plans import TreatmentPlansExtractor

    deid = _deid()
    a_cfg = load_mapping_config(MAPPING_DIR / "A_treatment_plans_raw.json")
    a_rows = [{"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
               "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
               "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 100.0}]
    a_ex = TreatmentPlansExtractor(a_cfg, deid, _rows(a_rows))
    a_out = a_ex.extract()

    b_ex = ClaimsExtractor(_config(), deid, _rows([_row()]))
    b_out = b_ex.extract()

    assert a_out[0].patient_external_id == b_out[0].patient_external_id


# --- Custom payer lookup ------------------------------------------------


def test_custom_payer_lookup_overrides_default(tmp_path: Path):
    p = tmp_path / "lookup.json"
    p.write_text(json.dumps({"weirdpayer": "workers_comp"}), encoding="utf-8")
    ex = ClaimsExtractor(
        _config(),
        _deid(),
        _rows([_row(**{"carrier.CarrierName": "WeirdPayer Inc"})]),
        payer_lookup_path=p,
    )
    out = ex.extract()
    assert out[0].payer_category == "workers_comp"


def test_invalid_payer_lookup_path_falls_back_to_default(tmp_path: Path):
    """Missing path should not error; should use built-in defaults."""
    ex = ClaimsExtractor(
        _config(),
        _deid(),
        _rows([_row(**{"carrier.CarrierName": "Aetna"})]),
        payer_lookup_path=tmp_path / "nope.json",
    )
    out = ex.extract()
    assert out[0].payer_category == "commercial"


def test_invalid_payer_lookup_json_raises(tmp_path: Path):
    p = tmp_path / "lookup.json"
    p.write_text("not json", encoding="utf-8")
    with pytest.raises(ExtractorError):
        ClaimsExtractor(
            _config(),
            _deid(),
            _rows([_row()]),
            payer_lookup_path=p,
        )


# --- Dropped rows --------------------------------------------------------


def test_drop_row_missing_claim_num():
    ex = ClaimsExtractor(_config(), _deid(), _rows([_row(**{"claim.ClaimNum": None})]))
    out = ex.extract()
    assert out == []
    assert ex.dropped_rows >= 1


def test_drop_row_missing_submission_date():
    ex = ClaimsExtractor(_config(), _deid(), _rows([_row(**{"claim.DateSent": None})]))
    out = ex.extract()
    assert out == []
    assert ex.dropped_rows >= 1


# --- PHI guard ----------------------------------------------------------


def test_no_phi_in_output():
    ex = ClaimsExtractor(_config(), _deid(), _rows([
        {**_row(),
         "patient.LName": "Smith", "patient.SSN": "123-45-6789",
         "patient.Email": "alice@example.com"}
    ]))
    out = ex.extract()
    blob = repr(out[0]).lower()
    for tip in ["smith", "123-45-6789", "alice@example.com"]:
        assert tip not in blob
