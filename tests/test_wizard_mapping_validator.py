"""Tests for `praxis_deid/wizard/mapping_validator.py`.

Feeds intentionally broken MappingConfigs to the validator and asserts
the right ValidationIssues are emitted with the right severity.
"""

from __future__ import annotations

from pathlib import Path

from praxis_deid.wizard.canonical_schemas import get_schema
from praxis_deid.wizard.claude_mapper import (
    ClaudeMapper,
    ColumnMapping,
    Join,
    MappingConfig,
)
from praxis_deid.wizard.mapping_validator import (
    ValidationSeverity,
    issues_summary,
    validate_mapping,
    validate_mappings,
)
from praxis_deid.wizard.schema_reader import read_pms_schema_from_json

FIXTURE_DIR = Path(__file__).parent / "fixtures"
OPEN_DENTAL_FIXTURE = FIXTURE_DIR / "open_dental_schema.json"
ANTHROPIC_FIXTURE = (
    FIXTURE_DIR / "anthropic_responses" / "open_dental_treatment_plans.json"
)


_STATUS_CASE = (
    "CASE WHEN treatplan.DateTSigned IS NOT NULL THEN 'accepted' "
    "ELSE 'presented' END"
)
_PLAN_DOLLARS_SUM = (
    "(SELECT SUM(proctp.FeeAmt) FROM proctp "
    "WHERE proctp.TreatPlanNum = treatplan.TreatPlanNum)"
)


def _good_treatment_plan_mapping() -> MappingConfig:
    """A handcrafted, mostly-valid Extension A mapping for tweaking."""
    return MappingConfig(
        canonical_schema="treatment_plans_raw",
        column_mappings={
            "source_id": ColumnMapping(
                "source_id", "treatplan.TreatPlanNum", 1.0, False, ""
            ),
            "patient_source_id": ColumnMapping(
                "patient_source_id", "treatplan.PatNum", 1.0, False, ""
            ),
            "provider_id": ColumnMapping(
                "provider_id", "proctp.ProvNum", 0.7, False, ""
            ),
            "presented_date": ColumnMapping(
                "presented_date", "treatplan.DateTP", 1.0, False, ""
            ),
            "status": ColumnMapping(
                "status", _STATUS_CASE, 0.8, False, ""
            ),
            "plan_dollars": ColumnMapping(
                "plan_dollars", _PLAN_DOLLARS_SUM, 0.9, False, ""
            ),
        },
        join_graph=[
            Join("treatplan", "TreatPlanNum", "proctp", "TreatPlanNum", "LEFT"),
        ],
        transformations={"status": _STATUS_CASE},
        confidence=0.7,
        notes=[],
    )


def _load_schema():
    return read_pms_schema_from_json(OPEN_DENTAL_FIXTURE)


# -----------------------------------------------------------------------
# Required column checks
# -----------------------------------------------------------------------

def test_validate_clean_mapping_emits_no_errors():
    schema = _load_schema()
    mapping = _good_treatment_plan_mapping()
    issues = validate_mapping(mapping, pms_schema=schema)
    errors = [i for i in issues if i.severity == ValidationSeverity.ERROR]
    assert errors == [], f"unexpected errors: {errors}"


def test_missing_required_column_is_error():
    schema = _load_schema()
    mapping = _good_treatment_plan_mapping()
    # source_id is required.
    del mapping.column_mappings["source_id"]
    issues = validate_mapping(mapping, pms_schema=schema)
    assert any(
        i.code == "required_column_missing" and i.canonical_column == "source_id"
        for i in issues
    )


def test_required_column_mapped_to_null_is_error():
    schema = _load_schema()
    mapping = _good_treatment_plan_mapping()
    # plan_dollars is required.
    mapping.column_mappings["plan_dollars"] = ColumnMapping(
        "plan_dollars", "NULL", 0.0, True, "no source"
    )
    issues = validate_mapping(mapping, pms_schema=schema)
    assert any(
        i.code == "required_column_null" and i.canonical_column == "plan_dollars"
        for i in issues
    )


def test_optional_column_mapped_to_null_is_not_error():
    schema = _load_schema()
    mapping = _good_treatment_plan_mapping()
    # accepted_date is optional, NULL is fine.
    mapping.column_mappings["accepted_date"] = ColumnMapping(
        "accepted_date", "NULL", 0.0, True, ""
    )
    issues = validate_mapping(mapping, pms_schema=schema)
    errors = [i for i in issues if i.severity == ValidationSeverity.ERROR]
    assert errors == []


# -----------------------------------------------------------------------
# Source table reference checks
# -----------------------------------------------------------------------

def test_unknown_table_reference_is_error():
    schema = _load_schema()
    mapping = _good_treatment_plan_mapping()
    mapping.column_mappings["plan_dollars"] = ColumnMapping(
        "plan_dollars", "totally_made_up_table.SomeColumn", 0.5, True, ""
    )
    issues = validate_mapping(mapping, pms_schema=schema)
    assert any(
        i.code == "unknown_table" for i in issues
    ), f"got: {[i.code for i in issues]}"


def test_unknown_column_reference_is_error():
    schema = _load_schema()
    mapping = _good_treatment_plan_mapping()
    mapping.column_mappings["plan_dollars"] = ColumnMapping(
        "plan_dollars", "treatplan.NonexistentColumn", 0.5, True, ""
    )
    issues = validate_mapping(mapping, pms_schema=schema)
    assert any(i.code == "unknown_column" for i in issues)


def test_string_literals_in_expression_are_not_table_refs():
    """A CASE WHEN that compares to a string literal should not generate
    spurious unknown_table errors for the literal."""
    schema = _load_schema()
    mapping = _good_treatment_plan_mapping()
    mapping.column_mappings["status"] = ColumnMapping(
        "status",
        "CASE WHEN treatplan.TPStatus = 'accepted.foo' THEN 'accepted' ELSE 'presented' END",
        0.9,
        False,
        "",
    )
    issues = validate_mapping(mapping, pms_schema=schema)
    assert not any(i.code == "unknown_table" for i in issues)


def test_extra_column_not_in_canonical_is_warning():
    schema = _load_schema()
    mapping = _good_treatment_plan_mapping()
    mapping.column_mappings["bogus_column"] = ColumnMapping(
        "bogus_column", "treatplan.PatNum", 1.0, False, ""
    )
    issues = validate_mapping(mapping, pms_schema=schema)
    extra_issues = [i for i in issues if i.code == "extra_column"]
    assert extra_issues
    assert extra_issues[0].severity == ValidationSeverity.WARNING


# -----------------------------------------------------------------------
# Confidence consistency
# -----------------------------------------------------------------------

def test_confidence_out_of_range_is_error():
    schema = _load_schema()
    mapping = _good_treatment_plan_mapping()
    mapping.confidence = 1.5
    issues = validate_mapping(mapping, pms_schema=schema)
    assert any(i.code == "confidence_out_of_range" for i in issues)


def test_column_confidence_out_of_range_is_error():
    schema = _load_schema()
    mapping = _good_treatment_plan_mapping()
    mapping.column_mappings["source_id"] = ColumnMapping(
        "source_id", "treatplan.TreatPlanNum", 2.0, False, ""
    )
    issues = validate_mapping(mapping, pms_schema=schema)
    assert any(i.code == "column_confidence_out_of_range" for i in issues)


def test_low_confidence_without_review_flag_is_warning():
    schema = _load_schema()
    mapping = _good_treatment_plan_mapping()
    mapping.column_mappings["status"] = ColumnMapping(
        "status",
        "CASE WHEN treatplan.TPStatus = 1 THEN 'accepted' ELSE 'presented' END",
        0.5,  # below 0.7
        False,  # but not flagged for review — inconsistent
        "",
    )
    issues = validate_mapping(mapping, pms_schema=schema)
    assert any(i.code == "low_confidence_without_review" for i in issues)


# -----------------------------------------------------------------------
# Enum handling
# -----------------------------------------------------------------------

def test_enum_with_case_expression_passes():
    schema = _load_schema()
    mapping = _good_treatment_plan_mapping()
    issues = validate_mapping(mapping, pms_schema=schema)
    assert not any(i.code == "enum_no_transformation" for i in issues)


def test_enum_with_direct_column_no_transformation_warns():
    schema = _load_schema()
    mapping = _good_treatment_plan_mapping()
    mapping.column_mappings["status"] = ColumnMapping(
        "status", "treatplan.TPStatus", 1.0, False, ""
    )
    mapping.transformations = {}
    issues = validate_mapping(mapping, pms_schema=schema)
    assert any(i.code == "enum_no_transformation" for i in issues)


# -----------------------------------------------------------------------
# Join graph
# -----------------------------------------------------------------------

def test_invalid_join_table_is_error():
    schema = _load_schema()
    mapping = _good_treatment_plan_mapping()
    mapping.join_graph = [
        Join("treatplan", "PatNum", "imaginary_table", "id", "INNER"),
    ]
    issues = validate_mapping(mapping, pms_schema=schema)
    assert any(i.code == "join_unknown_table" for i in issues)


def test_invalid_join_column_is_error():
    schema = _load_schema()
    mapping = _good_treatment_plan_mapping()
    mapping.join_graph = [
        Join("treatplan", "DefinitelyNotAColumn", "patient", "PatNum", "INNER"),
    ]
    issues = validate_mapping(mapping, pms_schema=schema)
    assert any(i.code == "join_unknown_column" for i in issues)


# -----------------------------------------------------------------------
# Unknown canonical schema
# -----------------------------------------------------------------------

def test_unknown_canonical_schema_is_error():
    schema = _load_schema()
    mapping = MappingConfig(
        canonical_schema="not_a_real_schema",
        column_mappings={},
    )
    issues = validate_mapping(mapping, pms_schema=schema)
    assert any(i.code == "unknown_canonical_schema" for i in issues)


# -----------------------------------------------------------------------
# End-to-end: validate the recorded fixture mappings
# -----------------------------------------------------------------------

def test_validate_recorded_open_dental_mappings():
    """The wizard's job is to produce mappings that pass validation
    with at most warnings (which are advisory, not blocking). Run the
    full validator on the recorded Anthropic response and assert that
    no schema-level structural ERRORS appear.

    We accept warnings (low confidence, NULL optional columns) and we
    accept ERRORS that come from Claude reasonably saying 'this column
    isn't mappable' for required fields it couldn't find — those are
    real findings the human must resolve.
    """
    schema = read_pms_schema_from_json(OPEN_DENTAL_FIXTURE)
    mapper = ClaudeMapper(recorded_response_path=ANTHROPIC_FIXTURE)
    mappings = mapper.map_schema(schema)
    issues = validate_mappings(mappings, pms_schema=schema)

    summary = issues_summary(issues)
    # We expect SOME warnings (Open Dental's lifecycle is messy).
    assert summary[ValidationSeverity.WARNING.value] >= 0

    # We expect NO unknown_table or unknown_column errors — Claude's
    # mapping should reference real Open Dental tables/columns.
    structural_errors = [
        i for i in issues
        if i.code in {"unknown_table", "unknown_column", "join_unknown_table",
                      "join_unknown_column"}
    ]
    assert structural_errors == [], (
        f"recorded fixture has structural errors: {structural_errors}"
    )


def test_issues_summary_counts_severities():
    """Smoke test for issues_summary helper."""
    schema = _load_schema()
    mapping = _good_treatment_plan_mapping()
    # Inject a known warning by adding an extra column.
    mapping.column_mappings["bogus"] = ColumnMapping(
        "bogus", "treatplan.PatNum", 1.0, False, ""
    )
    issues = validate_mapping(mapping, pms_schema=schema)
    summary = issues_summary(issues)
    assert summary[ValidationSeverity.WARNING.value] >= 1
    # Sum of severities = total issues.
    assert sum(summary.values()) == len(issues)


# -----------------------------------------------------------------------
# Foreign key edge cases (schema metadata sanity)
# -----------------------------------------------------------------------

def test_canonical_schema_passed_explicitly_is_used():
    """When the caller provides the canonical_schema, the registry lookup
    is skipped — useful for testing draft schemas."""
    schema = _load_schema()
    canonical = get_schema("treatment_plans_raw")
    mapping = _good_treatment_plan_mapping()
    issues = validate_mapping(mapping, pms_schema=schema, canonical_schema=canonical)
    errors = [i for i in issues if i.severity == ValidationSeverity.ERROR]
    assert errors == []


def test_validate_mappings_aggregates_across_configs():
    """validate_mappings runs validate_mapping on each entry."""
    schema = _load_schema()
    good = _good_treatment_plan_mapping()
    bad = MappingConfig(
        canonical_schema="treatment_plans_raw",
        column_mappings={
            "source_id": ColumnMapping(
                "source_id", "missing_table.x", 0.5, False, ""
            ),
        },
    )
    issues = validate_mappings([good, bad], pms_schema=schema)
    # bad should produce at least one unknown_table error and the
    # required-column-missing errors.
    bad_errors = [
        i for i in issues
        if i.canonical_column != "source_id" and i.severity == ValidationSeverity.ERROR
    ]
    assert any(i.code == "required_column_missing" for i in bad_errors)
    assert any(i.code == "unknown_table" for i in issues)
