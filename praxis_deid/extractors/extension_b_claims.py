"""Extension B: claims_raw extractor.

Source (Open Dental):
  claim
    LEFT JOIN insplan ON insplan.PlanNum = claim.PlanNum
    LEFT JOIN carrier ON carrier.CarrierNum = insplan.CarrierNum

CRITICAL: claimproc.Status = 1 means Received/paid (per the audited
mapping config B_claims_raw.json — see Open Dental docs
https://www.opendental.com/manual/claimprocstatus.html). Status = 2 is
Preauth and is a common source of analyst error. The payment_date
column comes from MAX(claimproc.DateCP) WHERE claimproc.Status = 1 and
NOTHING ELSE.

ClaimStatus single-char enum derivations (per the audited config):
  R, A      -> 'paid'
  S         -> 'submitted'
  U, W, H, I -> 'pending'
  else      -> 'pending'

payer_category: requires per-practice carrier-name -> category lookup.
Loaded from `mappings/open_dental/payer_lookup.json` if present;
otherwise defaults to a small built-in lookup of common US carriers.
Unmapped names -> 'other' AND a one-time warning recorded in
self.unmapped_carriers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .base import BaseExtractor, ExtractorError, Filter
from .rows import CLAIM_STATUSES, ClaimRow

# Default carrier-name -> canonical-payer-category lookup. Lower-case
# substring match. Practices override via payer_lookup.json.
_DEFAULT_PAYER_LOOKUP: dict[str, str] = {
    "medicare": "medicare",
    "medicaid": "medicaid",
    "bcbs": "commercial",
    "blue cross": "commercial",
    "blue shield": "commercial",
    "aetna": "commercial",
    "cigna": "commercial",
    "unitedhealth": "commercial",
    "uhc": "commercial",
    "humana": "commercial",
    "anthem": "commercial",
    "horizon": "commercial",
    "metlife": "commercial",
    "delta dental": "commercial",
    "guardian": "commercial",
    "self pay": "self_pay",
    "self-pay": "self_pay",
    "cash": "self_pay",
    "workers comp": "workers_comp",
    "auto": "auto",
}

_CLAIM_STATUS_MAP = {
    "R": "paid",
    "A": "paid",
    "S": "submitted",
    "U": "pending",
    "W": "pending",
    "H": "pending",
    "I": "pending",
    "P": "pending",
}


class ClaimsExtractor(BaseExtractor):
    canonical_schema_name = "claims_raw"

    SOURCE_TABLE = "claim"
    DATE_COLUMN = "claim.DateSent"

    def __init__(
        self,
        *args: Any,
        payer_lookup_path: Path | str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.payer_lookup = self._load_payer_lookup(payer_lookup_path)
        self.unmapped_carriers: dict[str, int] = {}

    @staticmethod
    def _load_payer_lookup(path: Path | str | None) -> dict[str, str]:
        if path is None:
            return dict(_DEFAULT_PAYER_LOOKUP)
        p = Path(path)
        if not p.exists():
            return dict(_DEFAULT_PAYER_LOOKUP)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise ExtractorError(f"payer_lookup {p} is not valid JSON: {err}") from err
        if not isinstance(data, dict):
            raise ExtractorError(f"payer_lookup {p} must be an object")
        # Lower-case keys for substring match.
        out = dict(_DEFAULT_PAYER_LOOKUP)
        for k, v in data.items():
            out[str(k).lower()] = str(v)
        return out

    def extract(self, filter: Filter | None = None) -> list[ClaimRow]:
        raw_rows = list(
            self.row_source(self.SOURCE_TABLE, self._needed_columns(), filter)
        )
        raw_rows = list(self._filter_to_period(raw_rows, self.DATE_COLUMN, filter))

        out: list[ClaimRow] = []
        for row in raw_rows:
            try:
                claim_row = self._build_row(row)
            except (ExtractorError, ValueError, KeyError) as err:
                self._drop(f"row_error:{type(err).__name__}:{str(err)[:80]}")
                continue
            try:
                claim_row.validate()
            except Exception as err:  # noqa: BLE001
                self._drop(f"validation:{str(err)[:120]}")
                continue
            out.append(claim_row)
        return out

    # --- helpers ----------------------------------------------------------

    def _needed_columns(self) -> list[str]:
        needed = {
            "claim.ClaimNum",
            "claim.PatNum",
            "claim.DateSent",
            "claim.ClaimStatus",
            "claim.PreAuthString",
            "claim.DateService",
            "carrier.CarrierName",
            # Pre-aggregated columns the practice's row source must
            # provide (since we don't run sub-SELECTs in-process):
            "claim.PaymentDate_aggregated",  # MAX(claimproc.DateCP) WHERE Status=1
            "claim.PreVerified_aggregated",  # bool
        }
        return sorted(needed)

    def _build_row(self, row: Mapping[str, Any]) -> ClaimRow:
        source_id_raw = row.get("claim.ClaimNum")
        if source_id_raw is None:
            raise ExtractorError("missing claim.ClaimNum")
        patient_raw = row.get("claim.PatNum")
        if patient_raw is None:
            raise ExtractorError("missing claim.PatNum")

        external_id = self._hipaa("source_id", source_id_raw)
        patient_external_id = self._hipaa("patient_source_id", patient_raw)

        # payer_category: derive from carrier name -> lookup. The
        # canonical schema marks this as `category` hipaa_handling, so
        # the dispatcher passes through to a string; we then map.
        carrier_name = row.get("carrier.CarrierName") or ""
        payer_category = self._categorize_carrier(str(carrier_name))

        # Dates -> month.
        submission_raw = row.get("claim.DateSent")
        if submission_raw in (None, ""):
            raise ExtractorError("missing submission_date")
        submission_month = self._hipaa("submission_date", submission_raw)

        payment_raw = row.get("claim.PaymentDate_aggregated")
        # ClaimprocStatus=1 means Received/paid per Open Dental docs.
        # The row source pre-aggregates MAX(DateCP) WHERE Status=1; if
        # absent OR sentinel '0001-01-01', payment_date is None.
        payment_month = (
            self._hipaa("payment_date", payment_raw)
            if _is_real_date(payment_raw)
            else None
        )

        # denial_date: no source column in Open Dental — always None.
        denial_month = None

        # authorization_required: derive from PreAuthString.
        preauth = row.get("claim.PreAuthString")
        authorization_required = bool(
            preauth is not None and str(preauth).strip() != ""
        )

        # authorization_date: no source column.
        authorization_date_month = None

        # denial_reason_category: no source column.
        denial_reason_category = None

        # status from ClaimStatus single-char.
        claim_status = row.get("claim.ClaimStatus")
        status = _CLAIM_STATUS_MAP.get(
            str(claim_status).upper().strip() if claim_status else "",
            "pending",
        )
        if status not in CLAIM_STATUSES:
            status = "pending"

        # pre_verified from the aggregated column.
        pre_verified_raw = row.get("claim.PreVerified_aggregated")
        pre_verified = _coerce_bool(pre_verified_raw)

        return ClaimRow(
            external_id=external_id,
            practice_id=self.deidentifier.practice_id,
            patient_external_id=patient_external_id,
            payer_category=payer_category,
            submission_date_month=submission_month,
            payment_date_month=payment_month,
            denial_date_month=denial_month,
            authorization_required=authorization_required,
            authorization_date_month=authorization_date_month,
            denial_reason_category=denial_reason_category,
            status=status,
            pre_verified=pre_verified,
        )

    def _categorize_carrier(self, carrier_name: str) -> str:
        """Carrier-name -> canonical payer_category. Records unmapped
        names so the operator can curate the lookup over time."""
        if not carrier_name.strip():
            return "other"
        low = carrier_name.lower()
        for needle, category in self.payer_lookup.items():
            if needle in low:
                return category
        # Unmapped — record without leaking PHI (carrier names are not
        # PHI but are sensitive practice metadata; stored in-memory only).
        self.unmapped_carriers[carrier_name] = self.unmapped_carriers.get(carrier_name, 0) + 1
        return "other"


def _is_real_date(value: Any) -> bool:
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


def _coerce_bool(v: Any) -> bool:
    if v is None or v == "":
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    return s in {"true", "1", "yes", "t", "y"}
