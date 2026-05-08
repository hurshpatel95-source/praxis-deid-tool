"""Tests for the 6 canonical target schemas defined in
`praxis_deid/wizard/canonical_schemas.py`.

Asserts schema-level invariants: required fields, valid enum lists,
deterministic prompt-dict shape, etc. Pure structural — no Claude API
involvement, so these run on every CI invocation.
"""

from __future__ import annotations

from praxis_deid.wizard.canonical_schemas import (
    CANONICAL_SCHEMAS,
    CanonicalColumn,
    get_schema,
    load_canonical_schemas,
)

# Expected extension letters, in order. Locked by METRIC_COVERAGE_AUDIT.md §4.1.
EXPECTED_EXTENSION_LETTERS = ("A", "B", "C", "D", "E", "F")
EXPECTED_NAMES = (
    "treatment_plans_raw",
    "claims_raw",
    "schedule_capacity_raw",
    "payments_raw",
    "timekeeping_raw",
    "patients_raw_extension",
)
VALID_HIPAA_HANDLINGS = frozenset(
    {"hmac", "month", "band", "category", "passthrough"}
)
VALID_TYPES = frozenset(
    {"string", "int", "numeric", "date", "datetime", "bool", "enum"}
)


def test_load_canonical_schemas_returns_six():
    schemas = load_canonical_schemas()
    assert len(schemas) == 6
    assert tuple(s.extension_letter for s in schemas) == EXPECTED_EXTENSION_LETTERS
    assert tuple(s.name for s in schemas) == EXPECTED_NAMES


def test_canonical_schemas_module_constant_is_consistent():
    assert load_canonical_schemas() == CANONICAL_SCHEMAS


def test_get_schema_lookup():
    import pytest as _pytest

    s = get_schema("treatment_plans_raw")
    assert s.extension_letter == "A"
    with _pytest.raises(KeyError):
        get_schema("not_a_real_schema")


def test_every_schema_has_at_least_one_required_column():
    for schema in CANONICAL_SCHEMAS:
        required = schema.required_columns
        assert len(required) >= 1, (
            f"canonical schema {schema.name} has no required columns; "
            "every CSV needs at least source_id-equivalent"
        )


def test_every_column_has_required_metadata():
    for schema in CANONICAL_SCHEMAS:
        for col in schema.columns:
            assert isinstance(col, CanonicalColumn)
            assert col.name and col.name.replace("_", "").isalnum(), (
                f"{schema.name}.{col.name} has invalid identifier"
            )
            assert col.type in VALID_TYPES, (
                f"{schema.name}.{col.name} has unknown type {col.type!r}"
            )
            assert col.hipaa_handling in VALID_HIPAA_HANDLINGS, (
                f"{schema.name}.{col.name} has unknown hipaa_handling "
                f"{col.hipaa_handling!r}"
            )
            assert col.description.strip(), (
                f"{schema.name}.{col.name} has empty description"
            )


def test_enum_columns_have_enum_values():
    for schema in CANONICAL_SCHEMAS:
        for col in schema.columns:
            if col.type == "enum":
                assert col.enum_values is not None, (
                    f"{schema.name}.{col.name} is enum but has no enum_values"
                )
                assert len(col.enum_values) >= 2, (
                    f"{schema.name}.{col.name} has too few enum values"
                )
                # No duplicates.
                assert len(set(col.enum_values)) == len(col.enum_values)


def test_non_enum_columns_have_no_enum_values():
    for schema in CANONICAL_SCHEMAS:
        for col in schema.columns:
            if col.type != "enum":
                assert col.enum_values is None, (
                    f"{schema.name}.{col.name} type={col.type} but enum_values "
                    "is set; should be None"
                )


def test_date_columns_have_format():
    for schema in CANONICAL_SCHEMAS:
        for col in schema.columns:
            if col.type in ("date", "datetime"):
                assert col.format, (
                    f"{schema.name}.{col.name} is a date but has no format hint"
                )


def test_extension_a_specific_columns():
    """Spot-check Extension A — the highest-leverage extension."""
    s = get_schema("treatment_plans_raw")
    names = [c.name for c in s.columns]
    expected = {
        "source_id", "patient_source_id", "provider_id",
        "presented_date", "accepted_date", "declined_date", "expired_date",
        "status", "plan_dollars", "procedure_category",
    }
    assert expected.issubset(set(names)), (
        f"Extension A missing columns; got {names}"
    )
    status = next(c for c in s.columns if c.name == "status")
    assert status.type == "enum"
    assert "presented" in (status.enum_values or ())
    assert "accepted" in (status.enum_values or ())
    assert "declined" in (status.enum_values or ())


def test_extension_b_specific_columns():
    s = get_schema("claims_raw")
    names = {c.name for c in s.columns}
    assert {
        "source_id", "patient_source_id", "payer_category", "submission_date",
        "payment_date", "denial_date", "authorization_required",
        "authorization_date", "denial_reason_category", "status", "pre_verified",
    } == names


def test_extension_f_marked_as_extension():
    """Extension F adds columns to existing patients_raw — not a new file."""
    s = get_schema("patients_raw_extension")
    assert s.extends == "patients_raw"


def test_to_prompt_dict_is_serializable():
    """Used by claude_mapper. Every schema must round-trip JSON."""
    import json
    for schema in CANONICAL_SCHEMAS:
        d = schema.to_prompt_dict()
        # Should contain all keys callers expect.
        assert set(d.keys()) == {
            "name", "extension_letter", "description", "extends",
            "columns", "claude_notes",
        }
        # Round-trips cleanly.
        s = json.dumps(d)
        rt = json.loads(s)
        assert rt == d


def test_hmac_handling_only_on_id_columns():
    """HMAC handling means the column is HIPAA-sensitive ID. Sanity check."""
    for schema in CANONICAL_SCHEMAS:
        for col in schema.columns:
            if col.hipaa_handling == "hmac":
                assert "id" in col.name.lower(), (
                    f"{schema.name}.{col.name}: hmac handling on a non-ID-named "
                    "column is suspicious"
                )


def test_hipaa_handling_for_dates_is_month():
    """Every date column should be banded to YYYY-MM at de-id time."""
    for schema in CANONICAL_SCHEMAS:
        for col in schema.columns:
            if col.type == "date":
                assert col.hipaa_handling == "month", (
                    f"{schema.name}.{col.name} is a date but hipaa_handling="
                    f"{col.hipaa_handling}; expected 'month'"
                )


def test_canonical_schemas_are_frozen():
    """Dataclass should be frozen — preventing accidental mutation."""
    import pytest as _pytest
    s = CANONICAL_SCHEMAS[0]
    with _pytest.raises(AttributeError):
        s.name = "mutated"  # type: ignore[misc]


def test_extension_c_and_e_have_either_or_grain():
    """Extensions C and E both have provider_id OR (chair_id|staff_role)."""
    for name, alt_col in [
        ("schedule_capacity_raw", "chair_id"),
        ("timekeeping_raw", "staff_role"),
    ]:
        s = get_schema(name)
        cols = {c.name for c in s.columns}
        assert "provider_id" in cols
        assert alt_col in cols
        # Both should be optional — exactly one is set per row.
        prov = next(c for c in s.columns if c.name == "provider_id")
        alt = next(c for c in s.columns if c.name == alt_col)
        assert not prov.required
        assert not alt.required
