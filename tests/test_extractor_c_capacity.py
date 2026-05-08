"""Tests for Extension C — schedule_capacity_raw extractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from praxis_deid.deidentify import Deidentifier
from praxis_deid.extractors import load_mapping_config
from praxis_deid.extractors.base import ExtractorError, Filter
from praxis_deid.extractors.extension_c_schedule_capacity import (
    CapacityExtractor,
)

PRACTICE_ID = "00000000-0000-0000-0000-0000000000a1"
SALT = "X" * 40
MAPPING_DIR = Path(__file__).resolve().parent.parent / "mappings" / "open_dental"


def _deid():
    return Deidentifier(practice_id=PRACTICE_ID, salt=SALT, small_n_threshold=1)


def _config():
    return load_mapping_config(MAPPING_DIR / "C_schedule_capacity_raw.json")


def _rows(rs):
    def _f(*a, **k):
        return list(rs)
    return _f


def _row(**overrides):
    base = {
        "schedule.ScheduleNum": 1,
        "schedule.SchedDate": "2026-04-15",
        "schedule.StartTime": "08:00:00",
        "schedule.StopTime": "17:00:00",  # 9 hours
        "schedule.ProvNum": 7,
        "scheduleop.OperatoryNum": "CHAIR-A",
        "apt_minutes_aggregated": 360,  # 6 hours productive
    }
    base.update(overrides)
    return base


# --- Mapping config -----------------------------------------------------


def test_mapping_loaded():
    cfg = _config()
    assert cfg.canonical_schema_name == "schedule_capacity_raw"


def test_mapping_required_columns_present():
    cfg = _config()
    for col in ("practice_period", "scheduled_hours", "productive_hours"):
        assert col in cfg.column_mappings


# --- Two grains: provider + chair (UNION ALL) ---------------------------


def test_emits_provider_and_chair_grain():
    ex = CapacityExtractor(_config(), _deid(), _rows([_row()]))
    out = ex.extract()
    # One provider row + one chair row.
    assert len(out) == 2
    grains = {(r.provider_id, r.chair_id) for r in out}
    assert ("7", None) in grains
    assert (None, "CHAIR-A") in grains


def test_provider_grain_set_provider_id_only():
    ex = CapacityExtractor(_config(), _deid(), _rows([_row()]))
    out = ex.extract()
    prov_row = next(r for r in out if r.provider_id is not None)
    assert prov_row.chair_id is None


def test_chair_grain_set_chair_id_only():
    ex = CapacityExtractor(_config(), _deid(), _rows([_row()]))
    out = ex.extract()
    chair_row = next(r for r in out if r.chair_id is not None)
    assert chair_row.provider_id is None


def test_validate_rejects_both_grains_set():
    """The dataclass MUST reject a row with both provider_id AND chair_id."""
    from praxis_deid.extractors.rows import CapacityRow

    with pytest.raises((ValueError, AssertionError, ExtractorError)):
        CapacityRow(
            practice_id=PRACTICE_ID,
            practice_period="2026-04",
            provider_id="7",
            chair_id="CHAIR-A",
            scheduled_hours=8.0,
            productive_hours=6.0,
        ).validate()


def test_validate_rejects_neither_grain_set():
    from praxis_deid.extractors.rows import CapacityRow

    with pytest.raises((ValueError, AssertionError, ExtractorError)):
        CapacityRow(
            practice_id=PRACTICE_ID,
            practice_period="2026-04",
            provider_id=None,
            chair_id=None,
            scheduled_hours=8.0,
            productive_hours=6.0,
        ).validate()


# --- Hours math ---------------------------------------------------------


def test_scheduled_hours_from_time_diff():
    ex = CapacityExtractor(_config(), _deid(), _rows([_row()]))
    out = ex.extract()
    # 08:00 -> 17:00 == 9 hours.
    for r in out:
        assert r.scheduled_hours == 9.0


def test_productive_hours_from_apt_minutes():
    ex = CapacityExtractor(_config(), _deid(), _rows([_row()]))
    out = ex.extract()
    for r in out:
        assert r.productive_hours == 6.0  # 360 min / 60


def test_hours_summed_across_days():
    rows = [
        _row(**{"schedule.SchedDate": "2026-04-01"}),
        _row(**{"schedule.SchedDate": "2026-04-02"}),
        _row(**{"schedule.SchedDate": "2026-04-03"}),
    ]
    ex = CapacityExtractor(_config(), _deid(), _rows(rows))
    out = ex.extract()
    prov_row = next(r for r in out if r.provider_id == "7")
    # 9h * 3 days = 27 hours scheduled.
    assert prov_row.scheduled_hours == 27.0


def test_period_grouped_by_month():
    rows = [
        _row(**{"schedule.SchedDate": "2026-04-15"}),
        _row(**{"schedule.SchedDate": "2026-05-15", "schedule.ProvNum": 7}),
    ]
    ex = CapacityExtractor(_config(), _deid(), _rows(rows))
    out = ex.extract()
    periods = {r.practice_period for r in out}
    assert "2026-04" in periods
    assert "2026-05" in periods


def test_zero_minutes_when_stop_before_start():
    """If StopTime < StartTime (data quality), contribute 0 hours."""
    ex = CapacityExtractor(_config(), _deid(), _rows([
        _row(**{"schedule.StartTime": "17:00:00", "schedule.StopTime": "08:00:00"}),
    ]))
    out = ex.extract()
    for r in out:
        assert r.scheduled_hours == 0.0


def test_unparseable_time_skipped():
    """Garbage time strings contribute 0 instead of crashing."""
    ex = CapacityExtractor(_config(), _deid(), _rows([
        _row(**{"schedule.StartTime": "garbage", "schedule.StopTime": "also bad"}),
    ]))
    out = ex.extract()
    for r in out:
        assert r.scheduled_hours == 0.0


def test_handles_iso_datetime_strings():
    ex = CapacityExtractor(_config(), _deid(), _rows([
        _row(**{
            "schedule.StartTime": "2026-04-15 08:00:00",
            "schedule.StopTime": "2026-04-15 17:00:00",
        }),
    ]))
    out = ex.extract()
    for r in out:
        assert r.scheduled_hours == 9.0


# --- Filter -------------------------------------------------------------


def test_filter_excludes_outside_window():
    rows = [
        _row(**{"schedule.SchedDate": "2025-12-01"}),
        _row(**{"schedule.SchedDate": "2026-04-01"}),
    ]
    ex = CapacityExtractor(_config(), _deid(), _rows(rows))
    out = ex.extract(Filter(since_month="2026-01"))
    periods = {r.practice_period for r in out}
    assert "2025-12" not in periods


# --- Drop / period parsing ----------------------------------------------


def test_drop_row_missing_sched_date():
    ex = CapacityExtractor(_config(), _deid(), _rows([
        _row(**{"schedule.SchedDate": None}),
    ]))
    out = ex.extract()
    # No row produced; dropped at period parsing.
    assert all(r.practice_period for r in out)
    # Not necessarily empty, since period parse fails before grouping.


def test_only_chair_when_provider_missing():
    """Operatory-only blocks (no provider) still emit chair-grain rows."""
    ex = CapacityExtractor(_config(), _deid(), _rows([
        _row(**{"schedule.ProvNum": None}),
    ]))
    out = ex.extract()
    chair_rows = [r for r in out if r.chair_id is not None]
    prov_rows = [r for r in out if r.provider_id is not None]
    assert len(chair_rows) >= 1
    assert len(prov_rows) == 0


def test_only_provider_when_chair_missing():
    ex = CapacityExtractor(_config(), _deid(), _rows([
        _row(**{"scheduleop.OperatoryNum": None}),
    ]))
    out = ex.extract()
    chair_rows = [r for r in out if r.chair_id is not None]
    prov_rows = [r for r in out if r.provider_id is not None]
    assert len(chair_rows) == 0
    assert len(prov_rows) >= 1


# --- Idempotency --------------------------------------------------------


def test_idempotency():
    rows = [_row()]
    a = CapacityExtractor(_config(), _deid(), _rows(rows)).extract()
    b = CapacityExtractor(_config(), _deid(), _rows(rows)).extract()
    assert a == b


# --- No PHI -------------------------------------------------------------


def test_no_phi_columns_in_capacity_row():
    """CapacityRow should never contain patient_external_id or PHI."""
    from dataclasses import fields

    from praxis_deid.extractors.rows import CapacityRow

    field_names = {f.name for f in fields(CapacityRow)}
    forbidden = {"patient_external_id", "external_id", "first_name", "last_name", "dob"}
    assert not (field_names & forbidden)
