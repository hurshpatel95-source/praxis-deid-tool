"""Extension C: schedule_capacity_raw extractor.

Source (Open Dental):
  schedule LEFT JOIN scheduleop ON scheduleop.ScheduleNum = schedule.ScheduleNum

Output is a UNION ALL of two grains:
  * provider-grain: one row per (practice_period YYYY-MM, schedule.ProvNum)
    with provider_id set, chair_id NULL.
  * chair-grain:    one row per (practice_period, scheduleop.OperatoryNum)
    with chair_id set, provider_id NULL.

scheduled_hours per row:
    SUM(TIMESTAMPDIFF(MINUTE, schedule.StartTime, schedule.StopTime)) / 60.0

productive_hours per row:
    sum(appointment.AptLength) / 60.0  for appointment.AptStatus = 2 (completed)
    matched on (ProvNum or OperatoryNum) and DATE(AptDateTime) = SchedDate.

The extractor expects the row source to return one row per
(schedule, scheduleop) join row, plus per-row aggregated `apt_minutes`
giving completed-appointment minutes for the matching grain. We then
group + sum in Python.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .base import BaseExtractor, ExtractorError, Filter
from .rows import CapacityRow


class CapacityExtractor(BaseExtractor):
    canonical_schema_name = "schedule_capacity_raw"

    SOURCE_TABLE = "schedule"
    DATE_COLUMN = "schedule.SchedDate"

    def extract(self, filter: Filter | None = None) -> list[CapacityRow]:
        raw_rows = list(
            self.row_source(self.SOURCE_TABLE, self._needed_columns(), filter)
        )
        raw_rows = list(self._filter_to_period(raw_rows, self.DATE_COLUMN, filter))

        # Two parallel aggregators: (period, provider_id) and (period, chair_id).
        prov_sched_min: dict[tuple[str, str], float] = defaultdict(float)
        prov_apt_min: dict[tuple[str, str], float] = defaultdict(float)
        chair_sched_min: dict[tuple[str, str], float] = defaultdict(float)
        chair_apt_min: dict[tuple[str, str], float] = defaultdict(float)

        for row in raw_rows:
            try:
                period = self._row_period(row)
            except ExtractorError as err:
                self._drop(f"period:{err}")
                continue

            sched_min = self._row_minutes(row)
            # Practitioner attribution may be absent on operatory-only blocks.
            prov_id = row.get("schedule.ProvNum")
            chair_id = row.get("scheduleop.OperatoryNum")
            apt_min = float(row.get("apt_minutes_aggregated") or 0.0)

            if prov_id not in (None, "", 0, "0"):
                key = (period, str(prov_id))
                prov_sched_min[key] += sched_min
                prov_apt_min[key] += apt_min
            if chair_id not in (None, "", 0, "0"):
                key = (period, str(chair_id))
                chair_sched_min[key] += sched_min
                chair_apt_min[key] += apt_min

        out: list[CapacityRow] = []
        # Provider grain.
        for (period, pid), sched in prov_sched_min.items():
            row = self._make_row(
                period=period,
                provider_id=pid,
                chair_id=None,
                scheduled_hours=sched / 60.0,
                productive_hours=prov_apt_min[(period, pid)] / 60.0,
            )
            if row is not None:
                out.append(row)
        # Chair grain.
        for (period, cid), sched in chair_sched_min.items():
            row = self._make_row(
                period=period,
                provider_id=None,
                chair_id=cid,
                scheduled_hours=sched / 60.0,
                productive_hours=chair_apt_min[(period, cid)] / 60.0,
            )
            if row is not None:
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
                "scheduleop.OperatoryNum",
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
        """Compute (StopTime - StartTime) in minutes. Both times can be
        ISO datetime strings, 'HH:MM:SS' strings, or Python time/datetime.
        Unparseable rows -> 0 minutes (no contribution).
        """
        start = row.get("schedule.StartTime")
        stop = row.get("schedule.StopTime")
        if start in (None, "") or stop in (None, ""):
            return 0.0
        try:
            start_dt = _parse_time_or_datetime(start)
            stop_dt = _parse_time_or_datetime(stop)
        except ValueError:
            return 0.0
        delta_seconds = (stop_dt - start_dt).total_seconds()
        if delta_seconds <= 0:
            return 0.0
        return delta_seconds / 60.0

    def _make_row(
        self,
        *,
        period: str,
        provider_id: str | None,
        chair_id: str | None,
        scheduled_hours: float,
        productive_hours: float,
    ) -> CapacityRow | None:
        try:
            row = CapacityRow(
                practice_id=self.deidentifier.practice_id,
                practice_period=period,
                provider_id=provider_id,
                chair_id=chair_id,
                scheduled_hours=round(scheduled_hours, 2),
                productive_hours=round(productive_hours, 2),
            )
            row.validate()
            return row
        except Exception as err:  # noqa: BLE001
            self._drop(f"validation:{str(err)[:120]}")
            return None


def _parse_time_or_datetime(value: Any) -> datetime:
    """Accept 'HH:MM:SS', 'YYYY-MM-DD HH:MM:SS', ISO datetime, or already-
    parsed datetime/time objects. Returns a datetime anchored to a fixed
    epoch when only time is present so subtraction is well-defined.
    """
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    # Full datetime?
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    # Time-only — anchor to 1970-01-01.
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(s, fmt)
            return t
        except ValueError:
            continue
    raise ValueError(f"unparseable time/datetime: {value!r}")
