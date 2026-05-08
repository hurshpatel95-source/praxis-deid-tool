"""Extension A: treatment_plans_raw extractor.

Source (Open Dental):
  treatplan LEFT JOIN proctp ON proctp.TreatPlanNum = treatplan.TreatPlanNum

Required canonical columns (per canonical_schemas.py Extension A):
  source_id, patient_source_id, provider_id, presented_date,
  status, plan_dollars
Optional:
  accepted_date, declined_date, expired_date, procedure_category

Status derivation (from mapping config A_treatment_plans_raw.json):
  CASE
    WHEN treatplan.DateTSigned IS NOT NULL THEN 'accepted'
    WHEN treatplan.TPStatus = 1            THEN 'declined'
    ELSE                                        'presented'
  END
The CASE is evaluated in Python over the row dict (NOT executed as SQL).
TPStatus is the canonical lifecycle code per
https://www.opendental.com/manual/treatmentplanstatus.html — the
mapping config audit (commit f22906d) marks Status=1 ('Inactive') as
'declined' rather than the older 'expired' because Open Dental
practices most commonly mark plans Inactive after a documented
patient refusal.

plan_dollars: SUM(proctp.FeeAmt) per treatplan. The row source provides
this either as a pre-aggregated row (each row is one treatplan with
plan_dollars already summed) or, for tests that pass row-per-proctp,
the extractor sums by TreatPlanNum here.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from .base import BaseExtractor, ExtractorError, Filter
from .rows import TREATMENT_PLAN_STATUSES, TreatmentPlanRow


class TreatmentPlansExtractor(BaseExtractor):
    canonical_schema_name = "treatment_plans_raw"

    # Source column names we expect in the joined row dict.
    SOURCE_TABLE = "treatplan"
    DATE_COLUMN = "treatplan.DateTP"

    def extract(self, filter: Filter | None = None) -> list[TreatmentPlanRow]:
        # The row source returns one row per (treatplan x proctp) join row,
        # OR one row per pre-aggregated treatplan when the practice has a
        # SUM(FeeAmt) view. We accept both shapes.
        raw_rows = list(
            self.row_source(self.SOURCE_TABLE, self._needed_columns(), filter)
        )
        # Apply the month-bound filter at the row level.
        raw_rows = list(self._filter_to_period(raw_rows, self.DATE_COLUMN, filter))

        # If the rows have proctp.FeeAmt, group by TreatPlanNum and sum.
        # Otherwise, the row source pre-summed to one row per plan.
        per_plan: dict[Any, list[Mapping[str, Any]]] = defaultdict(list)
        for row in raw_rows:
            key = row.get("treatplan.TreatPlanNum")
            if key is None:
                self._drop("missing_treatplan_num")
                continue
            per_plan[key].append(row)

        out: list[TreatmentPlanRow] = []
        for _plan_id, rows in per_plan.items():
            try:
                row = self._merge_plan_rows(rows)
                tp_row = self._build_row(row)
            except (ExtractorError, ValueError, KeyError) as err:
                self._drop(f"row_error:{type(err).__name__}:{str(err)[:80]}")
                continue
            try:
                tp_row.validate()
            except Exception as err:  # noqa: BLE001 — surface validator errs
                self._drop(f"validation:{str(err)[:120]}")
                continue
            out.append(tp_row)
        return out

    # --- helpers ----------------------------------------------------------

    def _needed_columns(self) -> list[str]:
        """Columns the row source must return.

        We list the union of every column referenced in the mapping config,
        plus the join key TreatPlanNum (always needed for grouping).
        """
        needed = {"treatplan.TreatPlanNum"}
        for cm in self.config.column_mappings.values():
            for tab_col in self._extract_simple_table_cols(cm.source_expression):
                needed.add(tab_col)
        # Also pull the columns the status CASE references.
        needed.add("treatplan.DateTSigned")
        needed.add("treatplan.TPStatus")
        needed.add("proctp.FeeAmt")
        return sorted(needed)

    @staticmethod
    def _extract_simple_table_cols(expr: str) -> list[str]:
        """Pull `table.column` references out of an arbitrary expression
        (CASE, sub-SELECT, simple ref). Used to compute the column list
        the RowSource needs to fetch — NOT used to execute SQL."""
        import re as _re

        return [
            f"{m.group(1)}.{m.group(2)}"
            for m in _re.finditer(
                r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
                expr or "",
            )
        ]

    def _merge_plan_rows(
        self,
        rows: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Collapse a list of (treatplan x proctp) rows for one plan into
        a single dict that has the treatplan-level columns AND a summed
        proctp.FeeAmt. Used when the row source feeds row-per-proctp."""
        if not rows:
            raise ExtractorError("merge: empty row list")
        first = dict(rows[0])
        total_fee = 0.0
        # The first proctp row's ProvNum is the planning provider per
        # the audited mapping notes; that's the one we keep.
        for r in rows:
            fee = r.get("proctp.FeeAmt")
            if fee is None or fee == "":
                continue
            try:
                total_fee += float(fee)
            except (TypeError, ValueError):
                continue
        first["proctp.FeeAmt"] = total_fee  # the summed value
        return first

    def _build_row(self, row: Mapping[str, Any]) -> TreatmentPlanRow:
        # Required FKs.
        source_id_raw = row.get("treatplan.TreatPlanNum")
        if source_id_raw is None:
            raise ExtractorError("missing treatplan.TreatPlanNum")
        patient_raw = row.get("treatplan.PatNum")
        if patient_raw is None:
            raise ExtractorError("missing treatplan.PatNum")
        provider_raw = row.get("proctp.ProvNum")

        # HMACs (cross-extension stable: same Deidentifier salt as v0.1).
        external_id = self._hipaa("source_id", source_id_raw)
        patient_external_id = self._hipaa("patient_source_id", patient_raw)

        # provider_id is passthrough — see CanonicalSchema A.
        provider_id = "" if provider_raw is None else str(provider_raw)

        # Dates -> month.
        presented_raw = row.get("treatplan.DateTP")
        if presented_raw in (None, ""):
            raise ExtractorError("missing presented_date")
        presented_month = self._hipaa("presented_date", presented_raw)

        accepted_raw = row.get("treatplan.DateTSigned")
        # Open Dental sentinel for "no date" is "0001-01-01" — treat as None.
        accepted_month = (
            self._hipaa("accepted_date", accepted_raw)
            if _is_real_date(accepted_raw)
            else None
        )
        # Declined / expired dates: no source column in Open Dental;
        # mapping config has source_expression="NULL", so resolve = None.
        declined_month = None
        expired_month = None

        # Status derivation (CASE in mapping, evaluated in Python).
        status = self._derive_status(row)
        if status not in TREATMENT_PLAN_STATUSES:
            raise ExtractorError(f"invalid derived status {status!r}")

        # plan_dollars: summed FeeAmt -> banded.
        plan_dollars_raw = row.get("proctp.FeeAmt") or 0
        plan_dollars_band = self._hipaa("plan_dollars", plan_dollars_raw or 0)

        procedure_category = None  # mapping says NULL by default

        return TreatmentPlanRow(
            external_id=external_id,
            practice_id=self.deidentifier.practice_id,
            patient_external_id=patient_external_id,
            provider_id=provider_id,
            presented_date_month=presented_month,
            accepted_date_month=accepted_month,
            declined_date_month=declined_month,
            expired_date_month=expired_month,
            status=status,
            plan_dollars_band=plan_dollars_band,
            procedure_category=procedure_category,
        )

    def _derive_status(self, row: Mapping[str, Any]) -> str:
        """Apply the audited CASE:
            DateTSigned IS NOT NULL -> 'accepted'
            TPStatus = 1            -> 'declined'
            else                    -> 'presented'
        """
        date_signed = row.get("treatplan.DateTSigned")
        if _is_real_date(date_signed):
            return "accepted"
        tp_status = row.get("treatplan.TPStatus")
        try:
            if tp_status is not None and int(tp_status) == 1:
                return "declined"
        except (TypeError, ValueError):
            pass
        return "presented"


def _is_real_date(value: Any) -> bool:
    """Open Dental encodes 'no date' as '0001-01-01' or NULL.
    Anything else (a real ISO date) -> True."""
    if value is None:
        return False
    s = str(value).strip()
    if not s:
        return False
    if s.startswith("0001-01-01"):
        return False
    if len(s) < 10:
        return False
    return True
