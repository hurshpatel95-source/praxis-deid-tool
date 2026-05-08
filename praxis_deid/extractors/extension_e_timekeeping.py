"""Extension E: timekeeping_raw extractor.

Source (Open Dental):
  schedule LEFT JOIN provider ON provider.ProvNum = schedule.ProvNum

Provider-grain only. Open Dental does not store non-provider staff
roles; the canonical schema's `staff_role` grain is documented as
unmappable from the PMS alone (mapping notes E_timekeeping_raw.json:
"Staff timekeeping typically lives in payroll software"). The
extractor leaves the staff_role branch empty by design; integration
with payroll software (Gusto, ADP) is a future agent.

Columns:
  practice_period   = DATE_FORMAT(SchedDate, '%Y-%m')
  provider_id       = schedule.ProvNum (passthrough)
  scheduled_hours   = SUM(TIMESTAMPDIFF(MIN, StartTime, StopTime))/60.0
                      grouped by (period, provider)
  productive_hours  = SUM(appointment.AptLength)/60.0 for AptStatus=2
                      grouped by same key
  hourly_rate       = provider.HourlyRate, banded into
                      "$0-50" / "$50-100" / "$100-150" / "$150-200" / "$200+"
                      because for tiny practices a single $187/hr rate
                      could re-identify the only provider in that band.

Banding policy (sensitive but non-PHI):
  Per the BAA carve-out, provider data is not PHI, but rate is
  treasury-sensitive. The hourly_rate column is banded at de-id time
  even though Safe Harbor doesn't strictly require it. NULL when the
  rate is not populated (Open Dental's payroll module is opt-in).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .base import BaseExtractor, ExtractorError, Filter
from .rows import TimekeepingRow, hourly_rate_to_band


class TimekeepingExtractor(BaseExtractor):
    canonical_schema_name = "timekeeping_raw"

    SOURCE_TABLE = "schedule"
    DATE_COLUMN = "schedule.SchedDate"

    def extract(self, filter: Filter | None = None) -> list[TimekeepingRow]:
        raw_rows = list(
            self.row_source(self.SOURCE_TABLE, self._needed_columns(), filter)
        )
        raw_rows = list(self._filter_to_period(raw_rows, self.DATE_COLUMN, filter))

        sched_min: dict[tuple[str, str], float] = defaultdict(float)
        apt_min: dict[tuple[str, str], float] = defaultdict(float)
        rate_lookup: dict[str, float | None] = {}

        for row in raw_rows:
            try:
                period = self._row_period(row)
            except ExtractorError as err:
                self._drop(f"period:{err}")
                continue
            prov_id = row.get("schedule.ProvNum")
            if prov_id in (None, "", 0, "0"):
                # No provider — skip (this row will be the chair-only
                # branch covered by Extension C).
                continue
            key = (period, str(prov_id))
            sched_min[key] += self._row_minutes(row)
            apt_min[key] += float(row.get("apt_minutes_aggregated") or 0.0)
            # Capture rate once per provider (use the latest seen).
            rate_raw = row.get("provider.HourlyRate")
            try:
                rate_val = float(rate_raw) if rate_raw not in (None, "") else None
            except (TypeError, ValueError):
                rate_val = None
            if rate_val is not None:
                rate_lookup[str(prov_id)] = rate_val
            elif str(prov_id) not in rate_lookup:
                rate_lookup[str(prov_id)] = None

        out: list[TimekeepingRow] = []
        for (period, pid), minutes in sched_min.items():
            try:
                row = TimekeepingRow(
                    practice_id=self.deidentifier.practice_id,
                    practice_period=period,
                    provider_id=pid,
                    staff_role=None,
                    scheduled_hours=round(minutes / 60.0, 2),
                    productive_hours=round(apt_min[(period, pid)] / 60.0, 2),
                    hourly_rate_band=hourly_rate_to_band(rate_lookup.get(pid)),
                )
                row.validate()
            except Exception as err:  # noqa: BLE001
                self._drop(f"validation:{str(err)[:120]}")
                continue
            out.append(row)
        return out

    # --- helpers ----------------------------------------------------------

    def _needed_columns(self) -> list[str]:
        return sorted(
            {
                "schedule.ScheduleNum",
                "schedule.SchedDate",
                "schedule.StartTime",
                "schedule.StopTime",
                "schedule.ProvNum",
                "provider.HourlyRate",
                "apt_minutes_aggregated",
            }
        )

    def _row_period(self, row: Mapping[str, Any]) -> str:
        sched_date = row.get("schedule.SchedDate")
        if sched_date in (None, ""):
            raise ExtractorError("missing schedule.SchedDate")
        s = str(sched_date)
        if " " in s:
            s = s.split(" ", 1)[0]
        if "T" in s:
            s = s.split("T", 1)[0]
        if len(s) < 7:
            raise ExtractorError(f"unparseable SchedDate {sched_date!r}")
        return s[:7]

    def _row_minutes(self, row: Mapping[str, Any]) -> float:
        start = row.get("schedule.StartTime")
        stop = row.get("schedule.StopTime")
        if start in (None, "") or stop in (None, ""):
            return 0.0
        try:
            start_dt = _parse_time_or_datetime(start)
            stop_dt = _parse_time_or_datetime(stop)
        except ValueError:
            return 0.0
        delta = (stop_dt - start_dt).total_seconds()
        if delta <= 0:
            return 0.0
        return delta / 60.0


def _parse_time_or_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"unparseable time/datetime: {value!r}")
