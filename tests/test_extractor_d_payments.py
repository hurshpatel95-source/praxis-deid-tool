"""Tests for Extension D — payments_raw extractor.

Special emphasis on the BAA invariant:
  amounts ALWAYS pass through amount_to_band — exact dollars NEVER leak.
"""

from __future__ import annotations

from pathlib import Path

from praxis_deid.deidentify import Deidentifier
from praxis_deid.extractors import load_mapping_config
from praxis_deid.extractors.base import (
    assert_no_exact_dollars_in_csv,
)
from praxis_deid.extractors.extension_d_payments import PaymentsExtractor

PRACTICE_ID = "00000000-0000-0000-0000-0000000000a1"
SALT = "X" * 40
MAPPING_DIR = Path(__file__).resolve().parent.parent / "mappings" / "open_dental"


def _deid():
    return Deidentifier(practice_id=PRACTICE_ID, salt=SALT, small_n_threshold=1)


def _config():
    return load_mapping_config(MAPPING_DIR / "D_payments_raw.json")


def _rows(rs):
    def _f(*a, **k):
        return list(rs)
    return _f


def _paysplit(**overrides):
    base = {
        "_branch": "paysplit",
        "paysplit.SplitNum": "S-1",
        "paysplit.PatNum": "PT-1",
        "paysplit.DatePay": "2026-04-15",
        "paysplit.SplitAmt": 250.0,
    }
    base.update(overrides)
    return base


def _claimpay(**overrides):
    base = {
        "_branch": "claimpayment",
        "claimpayment.ClaimPaymentNum": "CP-1",
        "claimpayment.PatNum": "PT-1",
        "claimpayment.CheckDate": "2026-04-25",
        "claimpayment.CheckAmt": 1500.0,
        "carrier.CarrierName": "Aetna PPO",
    }
    base.update(overrides)
    return base


def _adjustment(**overrides):
    base = {
        "_branch": "adjustment",
        "adjustment.AdjNum": "A-1",
        "adjustment.PatNum": "PT-1",
        "adjustment.AdjDate": "2026-04-22",
        "adjustment.AdjAmt": -50.0,
    }
    base.update(overrides)
    return base


# --- Mapping ------------------------------------------------------------


def test_mapping_loaded():
    cfg = _config()
    assert cfg.canonical_schema_name == "payments_raw"


def test_mapping_required_present():
    cfg = _config()
    required = (
        "source_id", "patient_source_id", "payment_date",
        "amount", "payment_source", "payer_category",
    )
    for c in required:
        assert c in cfg.column_mappings


# --- Branch handling ----------------------------------------------------


def test_paysplit_branch_emits_patient_self_pay():
    ex = PaymentsExtractor(_config(), _deid(), _rows([_paysplit()]))
    out = ex.extract()
    assert len(out) == 1
    assert out[0].payment_source == "patient"
    assert out[0].payer_category == "self_pay"


def test_claimpayment_branch_emits_insurance():
    ex = PaymentsExtractor(_config(), _deid(), _rows([_claimpay()]))
    out = ex.extract()
    assert out[0].payment_source == "insurance"
    assert out[0].payer_category == "commercial"


def test_adjustment_branch_emits_writeoff():
    ex = PaymentsExtractor(_config(), _deid(), _rows([_adjustment()]))
    out = ex.extract()
    assert out[0].payment_source == "adjustment_writeoff"
    assert out[0].payer_category == "other"


def test_unknown_branch_dropped():
    ex = PaymentsExtractor(_config(), _deid(), _rows([{"_branch": "evil"}]))
    out = ex.extract()
    assert out == []
    assert ex.dropped_rows >= 1


# --- BAA invariant: amount banding --------------------------------------


def test_amount_always_banded_paysplit():
    ex = PaymentsExtractor(_config(), _deid(), _rows([
        _paysplit(**{"paysplit.SplitAmt": 250.0}),
    ]))
    out = ex.extract()
    assert out[0].amount_band == "$100-500"


def test_amount_always_banded_claimpayment():
    ex = PaymentsExtractor(_config(), _deid(), _rows([
        _claimpay(**{"claimpayment.CheckAmt": 4500.0}),
    ]))
    out = ex.extract()
    assert out[0].amount_band == "$1000-5000"


def test_amount_banded_for_negative_writeoff():
    """Adjustment amounts are negative; banding uses absolute value."""
    ex = PaymentsExtractor(_config(), _deid(), _rows([
        _adjustment(**{"adjustment.AdjAmt": -250.0}),
    ]))
    out = ex.extract()
    assert out[0].amount_band == "$100-500"


def test_no_exact_dollar_value_in_output_repr():
    """Check that no per-record amount appears verbatim in the row's repr."""
    ex = PaymentsExtractor(_config(), _deid(), _rows([
        _paysplit(**{"paysplit.SplitAmt": 1234.56}),
    ]))
    out = ex.extract()
    blob = repr(out[0])
    # The exact value MUST NOT appear; it should be banded.
    assert "1234" not in blob
    assert out[0].amount_band == "$1000-5000"


def test_csv_dump_passes_dollar_leak_scan(tmp_path: Path):
    rows = [
        _paysplit(**{"paysplit.SplitAmt": 5500.0}),
        _claimpay(**{"claimpayment.CheckAmt": 45000.0}),
        _adjustment(**{"adjustment.AdjAmt": -1500.0}),
    ]
    ex = PaymentsExtractor(_config(), _deid(), _rows(rows), output_dir=tmp_path)
    out = ex.extract()
    p = ex._dump_to_csv(out, "payments_raw.csv")
    # The dollar-leak guard catches any un-banded numeric > 1000.
    assert_no_exact_dollars_in_csv(p)
    # And every amount cell starts with '$' (it's a band, not a raw number).
    import csv as _csv
    with p.open() as fp:
        reader = _csv.DictReader(fp)
        for row in reader:
            assert row["amount_band"].startswith("$")


def test_amount_non_numeric_drops_row():
    ex = PaymentsExtractor(_config(), _deid(), _rows([
        _paysplit(**{"paysplit.SplitAmt": "not-a-number"}),
    ]))
    out = ex.extract()
    assert out == []
    assert ex.dropped_rows >= 1


# --- HMACs --------------------------------------------------------------


def test_hmac_stable_across_branches_for_same_pat_num():
    rows = [_paysplit(**{"paysplit.PatNum": "PT-XYZ"}),
            _claimpay(**{"claimpayment.PatNum": "PT-XYZ"}),
            _adjustment(**{"adjustment.PatNum": "PT-XYZ"})]
    ex = PaymentsExtractor(_config(), _deid(), _rows(rows))
    out = ex.extract()
    pids = {r.patient_external_id for r in out}
    assert len(pids) == 1, "patient HMAC must be stable across branches"


def test_external_id_format():
    ex = PaymentsExtractor(_config(), _deid(), _rows([_paysplit()]))
    out = ex.extract()
    assert len(out[0].external_id) == 16
    assert all(c in "0123456789abcdef" for c in out[0].external_id)


# --- Dates ---------------------------------------------------------------


def test_payment_date_truncated_to_month():
    ex = PaymentsExtractor(_config(), _deid(), _rows([
        _paysplit(**{"paysplit.DatePay": "2026-04-15"}),
    ]))
    out = ex.extract()
    assert out[0].payment_date_month == "2026-04"


def test_payment_date_handles_datetime():
    ex = PaymentsExtractor(_config(), _deid(), _rows([
        _paysplit(**{"paysplit.DatePay": "2026-04-15 10:00:00"}),
    ]))
    out = ex.extract()
    assert out[0].payment_date_month == "2026-04"


def test_drop_row_missing_date():
    ex = PaymentsExtractor(_config(), _deid(), _rows([
        _paysplit(**{"paysplit.DatePay": None}),
    ]))
    out = ex.extract()
    assert out == []
    assert ex.dropped_rows >= 1


def test_drop_row_missing_source_id():
    ex = PaymentsExtractor(_config(), _deid(), _rows([
        _paysplit(**{"paysplit.SplitNum": None}),
    ]))
    out = ex.extract()
    assert out == []


def test_drop_row_missing_patient_id():
    ex = PaymentsExtractor(_config(), _deid(), _rows([
        _paysplit(**{"paysplit.PatNum": None}),
    ]))
    out = ex.extract()
    assert out == []


# --- Idempotency --------------------------------------------------------


def test_idempotency():
    rows = [_paysplit()]
    a = PaymentsExtractor(_config(), _deid(), _rows(rows)).extract()
    b = PaymentsExtractor(_config(), _deid(), _rows(rows)).extract()
    assert a == b


# --- No PHI -------------------------------------------------------------


def test_no_phi_in_output():
    ex = PaymentsExtractor(_config(), _deid(), _rows([
        {**_paysplit(),
         "patient.LName": "Smith",
         "patient.SSN": "123-45-6789"},
    ]))
    out = ex.extract()
    assert "smith" not in repr(out[0]).lower()
    assert "123-45-6789" not in repr(out[0])
