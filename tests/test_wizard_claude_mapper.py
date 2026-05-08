"""Tests for `praxis_deid/wizard/claude_mapper.py`.

Two surfaces under test:

  1. PhiGuard — defense-in-depth check that refuses to send PHI-shaped
     content to the Anthropic API. Adversarial cases.
  2. ClaudeMapper in REPLAY mode — uses a recorded Anthropic response
     fixture instead of calling the live API. This makes the test suite
     deterministic and free.

Live API calls are NEVER made from this test file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from praxis_deid.wizard.canonical_schemas import load_canonical_schemas
from praxis_deid.wizard.claude_mapper import (
    ClaudeMapper,
    ColumnMapping,
    Join,
    MappingConfig,
    PhiDetectedError,
    PhiGuard,
    _strip_and_parse_json,
)
from praxis_deid.wizard.schema_reader import read_pms_schema_from_json

FIXTURE_DIR = Path(__file__).parent / "fixtures"
OPEN_DENTAL_FIXTURE = FIXTURE_DIR / "open_dental_schema.json"
ANTHROPIC_FIXTURE = (
    FIXTURE_DIR / "anthropic_responses" / "open_dental_treatment_plans.json"
)


# -----------------------------------------------------------------------
# PhiGuard
# -----------------------------------------------------------------------

def test_phi_guard_passes_clean_metadata():
    guard = PhiGuard()
    payload = {
        "pms_schema": {
            "pms_name": "open_dental",
            "tables": {
                "patient": {
                    "columns": [
                        {"name": "PatNum", "type": "BIGINT", "nullable": False},
                        {"name": "LName", "type": "VARCHAR(100)", "nullable": True},
                    ],
                }
            },
        }
    }
    # Should not raise.
    guard.assert_clean(payload)


def test_phi_guard_rejects_ssn():
    guard = PhiGuard()
    bad = {"pms_schema": {"note": "test record SSN 123-45-6789 here"}}
    with pytest.raises(PhiDetectedError, match="ssn"):
        guard.assert_clean(bad)


def test_phi_guard_rejects_email():
    guard = PhiGuard()
    bad = {"note": "contact patient@example.com"}
    with pytest.raises(PhiDetectedError, match="email"):
        guard.assert_clean(bad)


def test_phi_guard_rejects_phone():
    guard = PhiGuard()
    bad = {"description": "patient phone 555-123-4567"}
    with pytest.raises(PhiDetectedError, match="phone"):
        guard.assert_clean(bad)


def test_phi_guard_rejects_full_dob():
    """A full ISO date-of-birth indicates row-data leakage."""
    guard = PhiGuard()
    # Note: avoid PHI-shaped field NAMES like 'sample'/'data' here — those
    # trip the field-name check, not the regex check. Use a benign name
    # so the date-pattern check is what fails.
    bad = {"description": "Birthdate is 1985-04-22"}
    with pytest.raises(PhiDetectedError, match="dob"):
        guard.assert_clean(bad)


def test_phi_guard_rejects_zip4():
    guard = PhiGuard()
    bad = {"address": "ZIP 08201-1234"}
    with pytest.raises(PhiDetectedError, match="zip4"):
        guard.assert_clean(bad)


def test_phi_guard_rejects_forbidden_field_names():
    """Even if values are clean, fields named 'rows' or 'sample_data'
    indicate the caller was about to ship row data."""
    guard = PhiGuard()
    bad = {"pms_schema": {"tables": {"patient": {"sample_data": []}}}}
    with pytest.raises(PhiDetectedError, match="forbidden field"):
        guard.assert_clean(bad)


def test_phi_guard_rejects_rows_field_name():
    guard = PhiGuard()
    bad = {"tables": {"patient": {"rows": []}}}
    with pytest.raises(PhiDetectedError, match="forbidden field"):
        guard.assert_clean(bad)


def test_phi_guard_does_not_reject_year_only_dates():
    """Schema descriptions sometimes mention 'as of 2024' — that is a
    bare 4-digit year, not a date-of-birth pattern, and must pass."""
    guard = PhiGuard()
    payload = {
        "tables": {
            "patient": {
                "description": "Updated to match Open Dental 2024 schema"
            }
        }
    }
    guard.assert_clean(payload)


def test_phi_guard_does_not_redact_payload():
    """Defense in depth — the guard refuses to send, it doesn't sanitize.
    Sanitizing would mask the bug that caused PHI to reach this point."""
    guard = PhiGuard()
    bad = {"x": "555-12-3456"}  # 555-12-3456 is SSN-shaped
    with pytest.raises(PhiDetectedError):
        guard.assert_clean(bad)
    # Original payload unchanged.
    assert bad == {"x": "555-12-3456"}


def test_phi_guard_error_message_is_truncated():
    """The error message must NOT echo the offending PHI back into logs."""
    guard = PhiGuard()
    bad = {"x": "ssn 123-45-6789"}
    with pytest.raises(PhiDetectedError) as exc_info:
        guard.assert_clean(bad)
    assert "123-45-6789" not in str(exc_info.value), (
        "error message should redact the offending PHI"
    )


def test_phi_guard_open_dental_fixture_passes():
    """The full Open Dental fixture must pass PhiGuard. If this ever
    fails, the fixture has been corrupted with row data."""
    schema = read_pms_schema_from_json(OPEN_DENTAL_FIXTURE)
    canonicals = load_canonical_schemas()
    guard = PhiGuard()
    payload = {
        "pms_schema": schema.to_dict(),
        "canonical_schemas": [s.to_prompt_dict() for s in canonicals],
    }
    guard.assert_clean(payload)


# -----------------------------------------------------------------------
# ClaudeMapper in replay mode
# -----------------------------------------------------------------------

def test_claude_mapper_replay_mode_returns_mappings():
    schema = read_pms_schema_from_json(OPEN_DENTAL_FIXTURE)
    mapper = ClaudeMapper(recorded_response_path=ANTHROPIC_FIXTURE)
    mappings = mapper.map_schema(schema)
    # Fixture covers all 6 canonical schemas.
    assert len(mappings) == 6
    names = [m.canonical_schema for m in mappings]
    assert "treatment_plans_raw" in names
    assert "claims_raw" in names
    assert "schedule_capacity_raw" in names
    assert "payments_raw" in names
    assert "timekeeping_raw" in names
    assert "patients_raw_extension" in names


def test_claude_mapper_records_token_usage_in_replay():
    schema = read_pms_schema_from_json(OPEN_DENTAL_FIXTURE)
    mapper = ClaudeMapper(recorded_response_path=ANTHROPIC_FIXTURE)
    mapper.map_schema(schema)
    assert mapper.last_input_tokens > 0
    assert mapper.last_output_tokens > 0


def test_claude_mapper_replay_extension_a_details():
    """Spot-check the Extension A mapping — the highest-leverage one."""
    schema = read_pms_schema_from_json(OPEN_DENTAL_FIXTURE)
    mapper = ClaudeMapper(recorded_response_path=ANTHROPIC_FIXTURE)
    mappings = mapper.map_schema(schema)

    treatment = next(m for m in mappings if m.canonical_schema == "treatment_plans_raw")
    assert "source_id" in treatment.column_mappings
    src = treatment.column_mappings["source_id"]
    assert src.source_expression == "treatplan.TreatPlanNum"
    assert src.confidence == 1.0
    assert not src.needs_review

    # Status mapping should have a CASE expression.
    status = treatment.column_mappings["status"]
    assert "CASE" in status.source_expression.upper()


def test_claude_mapper_replay_marks_unmappable_columns():
    """Columns Claude cannot map should be NULL with confidence=0."""
    schema = read_pms_schema_from_json(OPEN_DENTAL_FIXTURE)
    mapper = ClaudeMapper(recorded_response_path=ANTHROPIC_FIXTURE)
    mappings = mapper.map_schema(schema)

    treatment = next(m for m in mappings if m.canonical_schema == "treatment_plans_raw")
    expired = treatment.column_mappings["expired_date"]
    assert expired.source_expression == "NULL"
    assert expired.confidence == 0.0
    assert expired.needs_review is True


def test_mapping_config_to_dict_round_trips():
    """Serialize -> deserialize equality."""
    m = MappingConfig(
        canonical_schema="treatment_plans_raw",
        column_mappings={
            "source_id": ColumnMapping(
                canonical_column="source_id",
                source_expression="t.id",
                confidence=1.0,
                needs_review=False,
                notes="ok",
            ),
        },
        join_graph=[
            Join(
                left_table="t",
                left_column="x",
                right_table="u",
                right_column="y",
                join_type="LEFT",
            )
        ],
        transformations={"k": "v"},
        confidence=0.9,
        notes=["hi"],
    )
    rt = MappingConfig.from_dict(m.to_dict())
    assert rt.canonical_schema == m.canonical_schema
    assert rt.column_mappings["source_id"].source_expression == "t.id"
    assert len(rt.join_graph) == 1
    assert rt.join_graph[0].join_type == "LEFT"
    assert rt.transformations == {"k": "v"}
    assert rt.confidence == 0.9
    assert rt.notes == ["hi"]


def test_strip_and_parse_json_handles_code_fence():
    text = '```json\n{"mappings": []}\n```'
    parsed = _strip_and_parse_json(text)
    assert parsed == {"mappings": []}


def test_strip_and_parse_json_handles_raw_object():
    parsed = _strip_and_parse_json('{"mappings": [{"canonical_schema": "x"}]}')
    assert parsed["mappings"][0]["canonical_schema"] == "x"


def test_strip_and_parse_json_rejects_non_json():
    with pytest.raises(ValueError, match="no JSON object"):
        _strip_and_parse_json("here is some text and no JSON")


def test_claude_mapper_phi_guard_runs_before_send(monkeypatch):
    """If the mapper somehow received a PHI-laden schema, PhiGuard must
    catch it BEFORE any API call."""

    class _DirtySchema:
        """Mimics PmsSchema enough to slip past type checks."""
        def to_dict(self):
            return {"tables": {"x": {"description": "patient SSN 123-45-6789"}}}

    mapper = ClaudeMapper(
        api_key="sk-fake",
        recorded_response_path=None,
    )
    with pytest.raises(PhiDetectedError):
        mapper.map_schema(_DirtySchema())  # type: ignore[arg-type]


def test_claude_mapper_missing_api_key_in_live_mode():
    """If recorded_response_path is None and no API key is set, error
    should be specific (not a generic AttributeError)."""
    schema = read_pms_schema_from_json(OPEN_DENTAL_FIXTURE)
    mapper = ClaudeMapper(api_key="", recorded_response_path=None)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        mapper.map_schema(schema)
