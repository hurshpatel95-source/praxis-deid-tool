"""Tests for the shared Phase-C extractor infrastructure (base.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from praxis_deid.deidentify import Deidentifier
from praxis_deid.extractors.base import (
    BaseExtractor,
    ExtractorError,
    Filter,
    apply_hipaa_handling,
    assert_no_exact_dollars_in_csv,
    is_simple_expression,
    load_mapping_config,
    resolve_simple_reference,
)

PRACTICE_ID = "00000000-0000-0000-0000-0000000000a1"
SALT = "X" * 40
MAPPING_DIR = Path(__file__).resolve().parent.parent / "mappings" / "open_dental"


def _deid() -> Deidentifier:
    return Deidentifier(practice_id=PRACTICE_ID, salt=SALT, small_n_threshold=1)


# -------------------------------------------------------------------------
# Filter
# -------------------------------------------------------------------------


def test_filter_accepts_valid_months() -> None:
    f = Filter(since_month="2026-01", until_month="2026-12", limit=100)
    assert f.since_month == "2026-01"
    assert f.until_month == "2026-12"
    assert f.limit == 100


def test_filter_rejects_bad_month_format() -> None:
    with pytest.raises(ExtractorError):
        Filter(since_month="2026-13")
    with pytest.raises(ExtractorError):
        Filter(until_month="2026-01-01")  # not month-only
    with pytest.raises(ExtractorError):
        Filter(since_month="abc")


def test_filter_rejects_inverted_window() -> None:
    with pytest.raises(ExtractorError):
        Filter(since_month="2026-12", until_month="2026-01")


def test_filter_rejects_negative_limit() -> None:
    with pytest.raises(ExtractorError):
        Filter(limit=-1)


def test_filter_allows_none_everywhere() -> None:
    f = Filter()
    assert f.since_month is None
    assert f.until_month is None
    assert f.limit is None


# -------------------------------------------------------------------------
# Mapping config loader / SQL safety
# -------------------------------------------------------------------------


def test_load_mapping_a_succeeds() -> None:
    cfg = load_mapping_config(MAPPING_DIR / "A_treatment_plans_raw.json")
    assert cfg.canonical_schema_name == "treatment_plans_raw"
    assert "source_id" in cfg.column_mappings
    assert cfg.pms == "open_dental"


def test_load_mapping_all_six_succeed() -> None:
    for fn in (
        "A_treatment_plans_raw.json",
        "B_claims_raw.json",
        "C_schedule_capacity_raw.json",
        "D_payments_raw.json",
        "E_timekeeping_raw.json",
        "F_patients_raw_extension.json",
    ):
        cfg = load_mapping_config(MAPPING_DIR / fn)
        # Every required canonical column has a mapping entry.
        for required in cfg.required_columns:
            assert required in cfg.column_mappings


def test_load_mapping_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ExtractorError, match="not found"):
        load_mapping_config(tmp_path / "nope.json")


def test_load_mapping_rejects_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ExtractorError, match="valid JSON"):
        load_mapping_config(p)


def test_load_mapping_rejects_unknown_schema(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text(
        json.dumps({"canonical_schema": "not_real", "column_mappings": {}}),
        encoding="utf-8",
    )
    with pytest.raises(ExtractorError, match="unknown canonical schema"):
        load_mapping_config(p)


def test_load_mapping_rejects_semicolon_injection(tmp_path: Path) -> None:
    p = tmp_path / "evil.json"
    p.write_text(
        json.dumps(
            {
                "canonical_schema": "treatment_plans_raw",
                "column_mappings": {
                    "source_id": {
                        "canonical_column": "source_id",
                        "source_expression": "treatplan.TreatPlanNum; DROP TABLE patient",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ExtractorError, match="forbidden SQL"):
        load_mapping_config(p)


def test_load_mapping_rejects_comment_marker(tmp_path: Path) -> None:
    p = tmp_path / "evil.json"
    p.write_text(
        json.dumps(
            {
                "canonical_schema": "treatment_plans_raw",
                "column_mappings": {
                    "source_id": {
                        "canonical_column": "source_id",
                        "source_expression": "treatplan.PatNum -- ha",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ExtractorError, match="forbidden SQL"):
        load_mapping_config(p)


def test_load_mapping_rejects_drop_keyword(tmp_path: Path) -> None:
    p = tmp_path / "evil.json"
    p.write_text(
        json.dumps(
            {
                "canonical_schema": "treatment_plans_raw",
                "column_mappings": {
                    "source_id": {
                        "canonical_column": "source_id",
                        "source_expression": "treatplan.PatNum WHEN DROP THEN 'x'",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ExtractorError, match="DROP|forbidden"):
        load_mapping_config(p)


def test_load_mapping_rejects_truncate_keyword(tmp_path: Path) -> None:
    p = tmp_path / "evil.json"
    p.write_text(
        json.dumps(
            {
                "canonical_schema": "treatment_plans_raw",
                "column_mappings": {
                    "source_id": {
                        "canonical_column": "source_id",
                        "source_expression": "TRUNCATE TABLE patient",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ExtractorError):
        load_mapping_config(p)


def test_load_mapping_rejects_unknown_canonical_column(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text(
        json.dumps(
            {
                "canonical_schema": "treatment_plans_raw",
                "column_mappings": {
                    "not_a_column": {
                        "canonical_column": "not_a_column",
                        "source_expression": "treatplan.PatNum",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ExtractorError, match="not a column of"):
        load_mapping_config(p)


def test_load_mapping_rejects_missing_required_column(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    # treatment_plans_raw requires source_id, patient_source_id, etc.
    p.write_text(
        json.dumps(
            {
                "canonical_schema": "treatment_plans_raw",
                "column_mappings": {
                    "source_id": {
                        "canonical_column": "source_id",
                        "source_expression": "treatplan.PatNum",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ExtractorError, match="required canonical column"):
        load_mapping_config(p)


def test_load_mapping_rejects_bad_join_type(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    body = {
        "canonical_schema": "treatment_plans_raw",
        "column_mappings": {
            c: {"canonical_column": c, "source_expression": "treatplan.X"}
            for c in (
                "source_id", "patient_source_id", "provider_id",
                "presented_date", "status", "plan_dollars",
            )
        },
        "join_graph": [
            {
                "left_table": "a",
                "left_column": "x",
                "right_table": "b",
                "right_column": "y",
                "join_type": "CROSS",
            }
        ],
    }
    p.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ExtractorError, match="invalid join_type"):
        load_mapping_config(p)


def test_mapping_get_source_expression_unknown_raises() -> None:
    cfg = load_mapping_config(MAPPING_DIR / "A_treatment_plans_raw.json")
    with pytest.raises(ExtractorError):
        cfg.get_source_expression("not_a_column")


def test_mapping_required_columns_property_aligns_with_schema() -> None:
    cfg = load_mapping_config(MAPPING_DIR / "B_claims_raw.json")
    assert "source_id" in cfg.required_columns
    assert "submission_date" in cfg.required_columns


# -------------------------------------------------------------------------
# resolve_simple_reference
# -------------------------------------------------------------------------


def test_resolve_simple_table_column_present() -> None:
    row = {"treatplan.PatNum": "PT-1"}
    assert resolve_simple_reference("treatplan.PatNum", row) == "PT-1"


def test_resolve_simple_returns_none_for_null_expression() -> None:
    assert resolve_simple_reference("NULL", {}) is None
    assert resolve_simple_reference("null", {}) is None
    assert resolve_simple_reference("", {}) is None
    assert resolve_simple_reference("   ", {}) is None


def test_resolve_string_literal_returned_as_value() -> None:
    assert resolve_simple_reference("'patient'", {}) == "patient"
    assert resolve_simple_reference("'self_pay'", {}) == "self_pay"


def test_resolve_unqualified_fallback() -> None:
    # Some PMS row sources flatten the join — accept unqualified column name.
    row = {"PatNum": "PT-9"}
    assert resolve_simple_reference("treatplan.PatNum", row) == "PT-9"


def test_resolve_missing_column_raises() -> None:
    with pytest.raises(ExtractorError, match="row missing column"):
        resolve_simple_reference("treatplan.PatNum", {"something_else": 1})


def test_is_simple_expression_classifier() -> None:
    assert is_simple_expression("treatplan.PatNum")
    assert is_simple_expression("NULL")
    assert is_simple_expression("'x'")
    assert is_simple_expression("")
    assert not is_simple_expression("CASE WHEN x THEN 1 END")
    assert not is_simple_expression("(SELECT MAX(x) FROM y)")


# -------------------------------------------------------------------------
# apply_hipaa_handling dispatch
# -------------------------------------------------------------------------


def test_hipaa_passthrough_returns_value_unchanged() -> None:
    out = apply_hipaa_handling("hello", hipaa_handling="passthrough", deidentifier=_deid())
    assert out == "hello"


def test_hipaa_hmac_returns_stable_hex() -> None:
    deid = _deid()
    out = apply_hipaa_handling("PT-1", hipaa_handling="hmac", deidentifier=deid)
    assert isinstance(out, str)
    assert len(out) == 16
    assert all(c in "0123456789abcdef" for c in out)


def test_hipaa_hmac_stable_across_calls() -> None:
    deid_a = _deid()
    deid_b = _deid()
    a = apply_hipaa_handling("PT-1", hipaa_handling="hmac", deidentifier=deid_a)
    b = apply_hipaa_handling("PT-1", hipaa_handling="hmac", deidentifier=deid_b)
    assert a == b


def test_hipaa_month_truncates_iso_date() -> None:
    out = apply_hipaa_handling("2026-04-15", hipaa_handling="month", deidentifier=_deid())
    assert out == "2026-04"


def test_hipaa_month_handles_datetime_strings() -> None:
    out = apply_hipaa_handling("2026-04-15 10:00:00", hipaa_handling="month", deidentifier=_deid())
    assert out == "2026-04"
    out2 = apply_hipaa_handling("2026-04-15T10:00:00", hipaa_handling="month", deidentifier=_deid())
    assert out2 == "2026-04"


def test_hipaa_band_buckets_dollars() -> None:
    out = apply_hipaa_handling(2500, hipaa_handling="band", deidentifier=_deid())
    assert out == "$1000-5000"


def test_hipaa_band_rejects_non_numeric() -> None:
    with pytest.raises(ExtractorError):
        apply_hipaa_handling("not-a-number", hipaa_handling="band", deidentifier=_deid())


def test_hipaa_unknown_handling_raises() -> None:
    with pytest.raises(ExtractorError):
        apply_hipaa_handling("x", hipaa_handling="???", deidentifier=_deid())


def test_hipaa_none_propagates() -> None:
    assert apply_hipaa_handling(None, hipaa_handling="hmac", deidentifier=_deid()) is None
    assert apply_hipaa_handling("", hipaa_handling="month", deidentifier=_deid()) is None


# -------------------------------------------------------------------------
# Dollar-leak guard
# -------------------------------------------------------------------------


def test_dollar_leak_guard_passes_clean_csv(tmp_path: Path) -> None:
    p = tmp_path / "ok.csv"
    p.write_text(
        "external_id,amount_band\nabcdef0123456789,$1000-5000\n",
        encoding="utf-8",
    )
    # Should not raise.
    assert_no_exact_dollars_in_csv(p)


def test_dollar_leak_guard_catches_unbanded(tmp_path: Path) -> None:
    p = tmp_path / "leak.csv"
    p.write_text(
        "external_id,amount\nabcdef0123456789,2500.00\n",
        encoding="utf-8",
    )
    with pytest.raises(ExtractorError, match="un-banded numeric"):
        assert_no_exact_dollars_in_csv(p)


def test_dollar_leak_guard_ignores_small_numbers(tmp_path: Path) -> None:
    p = tmp_path / "ok.csv"
    p.write_text(
        "id,count\nabcdef0123456789,250\n",  # small enough not to be a $$$ leak
        encoding="utf-8",
    )
    assert_no_exact_dollars_in_csv(p)


def test_dollar_leak_guard_handles_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    assert_no_exact_dollars_in_csv(p)


# -------------------------------------------------------------------------
# BaseExtractor abstract behaviour
# -------------------------------------------------------------------------


class _NoopExtractor(BaseExtractor):
    """Smallest concrete subclass for testing the base directly."""

    canonical_schema_name = "treatment_plans_raw"

    def extract(self, filter: Filter | None = None) -> list[object]:
        return []


def test_base_rejects_mismatched_schema(tmp_path: Path) -> None:
    cfg = load_mapping_config(MAPPING_DIR / "B_claims_raw.json")
    deid = _deid()

    def rs(*a, **k):
        return []

    with pytest.raises(ExtractorError, match="extracts"):
        _NoopExtractor(cfg, deid, rs)


def test_base_drop_helper_increments_counter() -> None:
    cfg = load_mapping_config(MAPPING_DIR / "A_treatment_plans_raw.json")
    deid = _deid()

    def rs(*a, **k):
        return []

    ex = _NoopExtractor(cfg, deid, rs)
    ex._drop("test_reason")
    ex._drop("test_reason")
    ex._drop("other_reason")
    assert ex.dropped_rows == 3
    assert ex.drop_reasons["test_reason"] == 2
    assert ex.drop_reasons["other_reason"] == 1


def test_base_dump_to_csv_requires_output_dir() -> None:
    cfg = load_mapping_config(MAPPING_DIR / "A_treatment_plans_raw.json")
    deid = _deid()

    def rs(*a, **k):
        return []

    ex = _NoopExtractor(cfg, deid, rs)
    with pytest.raises(ExtractorError, match="output_dir not set"):
        ex._dump_to_csv([], "x.csv")


def test_base_dump_to_csv_writes_empty_for_empty_rows(tmp_path: Path) -> None:
    cfg = load_mapping_config(MAPPING_DIR / "A_treatment_plans_raw.json")
    deid = _deid()

    def rs(*a, **k):
        return []

    ex = _NoopExtractor(cfg, deid, rs, output_dir=tmp_path)
    p = ex._dump_to_csv([], "treatment_plans_raw.csv")
    assert p.exists()
    assert p.read_text() == ""
