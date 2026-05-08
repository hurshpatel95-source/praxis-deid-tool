"""Extension F: patients_raw_extension extractor.

Adds three columns to the existing patients_raw contract:
  * last_visit_date     -> last_visit_date_month  (banded to YYYY-MM)
  * recall_due_date     -> recall_due_date_month  (banded; NULL ok)
  * referral_source_category  (controlled vocab via per-practice lookup)

Implementation choice (per Phase-C brief option (b)):
  Produces a SEPARATE `patients_extension.csv` keyed on
  patient_external_id (HMAC of patient.PatNum), so the cloud aggregator
  joins on patient_external_id at ingest time. Rationale: this keeps
  the v0.1 locked module `praxis_deid/sources/csv_source.py` untouched
  (per the BAA invariants no-edit-locked-modules rule) and gives
  cloud-side the freedom to lazy-join the extension data.

Source columns (Open Dental):
  patient.PatNum         -> patient_external_id (HMAC)
  patient.DateLastVisit  -> last_visit_date     -> month
  patient.ReferredBy     -> referral_source_category (via lookup)
  recall.DateDue (MIN)   -> recall_due_date     -> month
                           where recall.PatNum = patient.PatNum
                                AND recall.IsDisabled = 0
                                AND recall.DateDue >= CURDATE()

The recall_due_date sub-aggregate is computed by the row source
(provided as `recall_min_due_aggregated`); the extractor doesn't
execute the WHERE clause.

Referral source category lookup:
  Loaded from `mappings/<pms>/referral_source_lookup.json` if present.
  Defaults to a small built-in vocabulary mapping common Open Dental
  ReferredBy text to canonical categories: 'internal', 'external',
  'google_ads', 'walk_in', 'insurance_directory', 'unknown'.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .base import BaseExtractor, ExtractorError, Filter
from .rows import PatientExtensionRow

_DEFAULT_REFERRAL_LOOKUP: dict[str, str] = {
    "patient referral": "internal",
    "patient": "internal",
    "existing patient": "internal",
    "doctor referral": "external",
    "physician": "external",
    "specialist": "external",
    "google": "google_ads",
    "ads": "google_ads",
    "search": "google_ads",
    "walk-in": "walk_in",
    "walk in": "walk_in",
    "walkin": "walk_in",
    "insurance": "insurance_directory",
    "directory": "insurance_directory",
}


class PatientsExtensionExtractor(BaseExtractor):
    canonical_schema_name = "patients_raw_extension"

    SOURCE_TABLE = "patient"
    DATE_COLUMN = "patient.DateLastVisit"

    def __init__(
        self,
        *args: Any,
        referral_lookup_path: Path | str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.referral_lookup = self._load_referral_lookup(referral_lookup_path)
        self.unmapped_referral_sources: dict[str, int] = {}

    @staticmethod
    def _load_referral_lookup(path: Path | str | None) -> dict[str, str]:
        if path is None:
            return dict(_DEFAULT_REFERRAL_LOOKUP)
        p = Path(path)
        if not p.exists():
            return dict(_DEFAULT_REFERRAL_LOOKUP)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise ExtractorError(f"referral_lookup {p} invalid JSON: {err}") from err
        if not isinstance(data, dict):
            raise ExtractorError(f"referral_lookup {p} must be an object")
        out = dict(_DEFAULT_REFERRAL_LOOKUP)
        for k, v in data.items():
            out[str(k).lower()] = str(v)
        return out

    def extract(self, filter: Filter | None = None) -> list[PatientExtensionRow]:
        raw_rows = list(
            self.row_source(self.SOURCE_TABLE, self._needed_columns(), filter)
        )
        # Don't filter by month here — last_visit_date can be far in the
        # past and still be the correct value to attach to the patient row.

        out: list[PatientExtensionRow] = []
        for row in raw_rows:
            try:
                ex_row = self._build_row(row)
            except (ExtractorError, ValueError, KeyError) as err:
                self._drop(f"row_error:{type(err).__name__}:{str(err)[:80]}")
                continue
            if ex_row is None:
                continue
            try:
                ex_row.validate()
            except Exception as err:  # noqa: BLE001
                self._drop(f"validation:{str(err)[:120]}")
                continue
            out.append(ex_row)
        return out

    # --- helpers ----------------------------------------------------------

    def _needed_columns(self) -> list[str]:
        return sorted(
            {
                "patient.PatNum",
                "patient.DateLastVisit",
                "patient.ReferredBy",
                "recall_min_due_aggregated",
            }
        )

    def _build_row(self, row: Mapping[str, Any]) -> PatientExtensionRow | None:
        patient_raw = row.get("patient.PatNum")
        if patient_raw in (None, ""):
            raise ExtractorError("missing patient.PatNum")

        last_visit_raw = row.get("patient.DateLastVisit")
        if not _is_real_date(last_visit_raw):
            # No real visit date — drop (the row would be invalid).
            raise ExtractorError("no real DateLastVisit")
        last_visit_month = self._hipaa("last_visit_date", last_visit_raw)

        recall_raw = row.get("recall_min_due_aggregated")
        recall_month = (
            self._hipaa("recall_due_date", recall_raw) if _is_real_date(recall_raw) else None
        )

        referral_raw = row.get("patient.ReferredBy")
        referral_category = (
            self._categorize_referral(str(referral_raw))
            if referral_raw not in (None, "")
            else None
        )

        # Reuse the same Deidentifier salt as v0.1 patients so this row's
        # patient_external_id matches the patients_raw row's external_id.
        patient_external_id = self._hipaa("last_visit_date", patient_raw) \
            if False else self._hmac_patient_id(patient_raw)

        return PatientExtensionRow(
            practice_id=self.deidentifier.practice_id,
            patient_external_id=patient_external_id,
            last_visit_date_month=last_visit_month,
            recall_due_date_month=recall_month,
            referral_source_category=referral_category,
        )

    def _hmac_patient_id(self, value: Any) -> str:
        """HMAC the patient PatNum the same way v0.1's Deidentifier does
        for patients_raw rows. Cross-extension stability invariant."""
        from ..hashing import stable_external_id
        return stable_external_id(self.deidentifier._salt, value)

    def _categorize_referral(self, raw: str) -> str:
        if not raw.strip():
            return "unknown"
        low = raw.lower()
        for needle, category in self.referral_lookup.items():
            if needle in low:
                return category
        self.unmapped_referral_sources[raw] = self.unmapped_referral_sources.get(raw, 0) + 1
        return "unknown"


def _is_real_date(value: Any) -> bool:
    if value is None:
        return False
    s = str(value).strip()
    if not s:
        return False
    if s.startswith("0001-01-01"):
        return False
    return len(s) >= 10
