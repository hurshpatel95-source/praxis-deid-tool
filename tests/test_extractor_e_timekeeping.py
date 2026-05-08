"""Tests for Extension E — timekeeping_raw extractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from praxis_deid.deidentify import Deidentifier
from praxis_deid.extractors import load_mapping_config
from praxis_deid.extractors.base import ExtractorError, Filter
from praxis_deid.extractors.extension_e_timekeeping import TimekeepingExtractor
from praxis_deid.extractors.rows import HOURLY_RATE_BANDS, hourly_rate_to_band

PRACTICE_ID = "00000000-0000-0000-0000-0000000000a1"
SALT = "X" * 40
MAPPING_DIR = Path(__file__).resolve().parent.parent / "mappings" / "open_dental"


def _deid():
    return Deidentifier(practice_id=PRACTICE_ID, salt=SALT, small_n_threshold=1)


def _config():
    return load_mapping_config(MAPPING_DIR / "E_timekeeping_raw.json")


def _rows(rs):
    def _f(*a, **k):
        return list(rs)
    return _f


def _row(**overrides):
    base = {
        "schedule.ScheduleNum": 1,
        "schedule.SchedDate": "2026-04-15",
        "schedule.StartTime": "08:00:00",
        "schedule.StopTime": "17:00:00",
        "schedule.ProvNum": 7,
        "provider.HourlyRate": 175.0,
        "apt_minutes_aggregated": 420,
    }
    base.update(overrides)
    return base


# --- Mapping ------------------------------------------------------------


def test_mapping_loaded():
    cfg = _config()
    assert cfg.canonical_schema_name == "timekeeping_raw"


def test_mapping_required_present():
    cfg = _config()
    for c in ("practice_period", "scheduled_hours", "productive_hours"):
        assert c in cfg.column_mappings


def test_staff_role_documented_as_unmappable():
    cfg = _config()
    expr = cfg.column_mappings["staff_role"].source_expression
    # The audited mapping marks it NULL — Open Dental can't represent staff roles.
    assert expr in ("NULL", "null", "")


# --- Extraction ---------------------------------------------------------


def test_emits_provider_grain_only():
    ex = TimekeepingExtractor(_config(), _deid(), _rows([_row()]))
    out = ex.extract()
    assert len(out) == 1
    assert out[0].provider_id == "7"
    assert out[0].staff_role is None


def test_scheduled_and_productive_hours():
    ex = TimekeepingExtractor(_config(), _deid(), _rows([_row()]))
    out = ex.extract()
    assert out[0].scheduled_hours == 9.0
    assert out[0].productive_hours == 7.0  # 420 / 60


def test_period_grouped_by_month():
    rows = [
        _row(**{"schedule.SchedDate": "2026-04-01"}),
        _row(**{"schedule.SchedDate": "2026-04-15"}),
        _row(**{"schedule.SchedDate": "2026-05-01"}),
    ]
    ex = TimekeepingExtractor(_config(), _deid(), _rows(rows))
    out = ex.extract()
    periods = {r.practice_period for r in out}
    assert "2026-04" in periods
    assert "2026-05" in periods


# --- Hourly rate banding (sensitive but non-PHI) ------------------------


def test_hourly_rate_175_banded_to_150_200():
    ex = TimekeepingExtractor(_config(), _deid(), _rows([_row(**{"provider.HourlyRate": 175.0})]))
    out = ex.extract()
    assert out[0].hourly_rate_band == "$150-200"


def test_hourly_rate_220_banded_to_200_plus():
    ex = TimekeepingExtractor(_config(), _deid(), _rows([_row(**{"provider.HourlyRate": 220.0})]))
    out = ex.extract()
    assert out[0].hourly_rate_band == "$200+"


def test_hourly_rate_25_banded_to_0_50():
    ex = TimekeepingExtractor(_config(), _deid(), _rows([_row(**{"provider.HourlyRate": 25.0})]))
    out = ex.extract()
    assert out[0].hourly_rate_band == "$0-50"


def test_hourly_rate_null_when_unset():
    ex = TimekeepingExtractor(_config(), _deid(), _rows([_row(**{"provider.HourlyRate": None})]))
    out = ex.extract()
    assert out[0].hourly_rate_band is None


def test_hourly_rate_band_not_exact_dollar():
    """Even at the row level the rate is BANDED — never exact."""
    ex = TimekeepingExtractor(_config(), _deid(), _rows([_row(**{"provider.HourlyRate": 187.50})]))
    out = ex.extract()
    blob = repr(out[0])
    assert "187" not in blob, "exact rate leaked"
    assert out[0].hourly_rate_band == "$150-200"


def test_every_rate_band_in_canonical_set():
    rates = [10, 75, 125, 175, 250]
    rows = [_row(**{"schedule.ScheduleNum": i, "schedule.ProvNum": i,
                    "provider.HourlyRate": r}) for i, r in enumerate(rates)]
    ex = TimekeepingExtractor(_config(), _deid(), _rows(rows))
    out = ex.extract()
    for r in out:
        if r.hourly_rate_band is not None:
            assert r.hourly_rate_band in HOURLY_RATE_BANDS


def test_hourly_rate_to_band_function_directly():
    assert hourly_rate_to_band(0) is None
    assert hourly_rate_to_band(25) == "$0-50"
    assert hourly_rate_to_band(75) == "$50-100"
    assert hourly_rate_to_band(125) == "$100-150"
    assert hourly_rate_to_band(175) == "$150-200"
    assert hourly_rate_to_band(250) == "$200+"
    assert hourly_rate_to_band(None) is None


# --- Filter -------------------------------------------------------------


def test_filter_excludes_outside_window():
    rows = [
        _row(**{"schedule.SchedDate": "2025-12-01"}),
        _row(**{"schedule.SchedDate": "2026-04-15"}),
    ]
    ex = TimekeepingExtractor(_config(), _deid(), _rows(rows))
    out = ex.extract(Filter(since_month="2026-01"))
    periods = {r.practice_period for r in out}
    assert "2025-12" not in periods


# --- Drops --------------------------------------------------------------


def test_no_provider_means_no_row():
    """Open Dental schedule rows without ProvNum belong to chair-only blocks
    (covered by Extension C); Extension E skips them silently."""
    ex = TimekeepingExtractor(_config(), _deid(), _rows([
        _row(**{"schedule.ProvNum": None}),
    ]))
    out = ex.extract()
    assert out == []


# --- Idempotency --------------------------------------------------------


def test_idempotency():
    rows = [_row()]
    a = TimekeepingExtractor(_config(), _deid(), _rows(rows)).extract()
    b = TimekeepingExtractor(_config(), _deid(), _rows(rows)).extract()
    assert a == b


# --- No PHI -------------------------------------------------------------


def test_no_patient_or_phi_columns():
    """TimekeepingRow must not contain any patient-level PHI."""
    from dataclasses import fields

    from praxis_deid.extractors.rows import TimekeepingRow
    field_names = {f.name for f in fields(TimekeepingRow)}
    forbidden = {"patient_external_id", "external_id", "first_name", "last_name", "dob", "mrn"}
    assert not (field_names & forbidden)


def test_validate_rejects_both_provider_and_role():
    from praxis_deid.extractors.rows import TimekeepingRow
    with pytest.raises((ValueError, AssertionError, ExtractorError)):
        TimekeepingRow(
            practice_id=PRACTICE_ID,
            practice_period="2026-04",
            provider_id="7",
            staff_role="hygienist",
            scheduled_hours=8.0,
            productive_hours=6.0,
            hourly_rate_band=None,
        ).validate()


def test_validate_rejects_neither_provider_nor_role():
    from praxis_deid.extractors.rows import TimekeepingRow
    with pytest.raises((ValueError, AssertionError, ExtractorError)):
        TimekeepingRow(
            practice_id=PRACTICE_ID,
            practice_period="2026-04",
            provider_id=None,
            staff_role=None,
            scheduled_hours=8.0,
            productive_hours=6.0,
            hourly_rate_band=None,
        ).validate()
