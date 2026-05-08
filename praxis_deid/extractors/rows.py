"""Canonical row dataclasses for the Phase-C extension CSVs (A-F).

Mirrors the v0.1 dataclasses in `praxis_deid/schema.py` (which we
deliberately do NOT touch — it's a locked module). These dataclasses
match the canonical schemas defined in
`praxis_deid/wizard/canonical_schemas.py` and the Praxis-cloud
ingestion contract (`praxis-app/lib/canonical/`).

Validation here mirrors the existing patterns: enum membership,
month-format regex, banded-dollar membership. Locked Safe Harbor
constants (`AGE_BANDS`, `REVENUE_BANDS`, etc.) are imported from
`praxis_deid.safe_harbor` so any change there flows through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..safe_harbor import REVENUE_BANDS

# Re-use the v0.1 PAYER_CATEGORIES set so a payer mapped in claims_raw
# matches the patients_raw enum exactly.
from ..schema import PAYER_CATEGORIES, ValidationError, _check

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# --- Phase-C enum sets (mirror canonical_schemas.py) ---------------------
TREATMENT_PLAN_STATUSES = frozenset({"presented", "accepted", "declined", "expired", "partial"})
CLAIM_STATUSES = frozenset({"submitted", "paid", "denied", "pending", "partial"})
DENIAL_REASON_CATEGORIES = frozenset({"eligibility", "coverage", "auth", "documentation", "other"})
PAYMENT_SOURCES = frozenset({"insurance", "patient", "adjustment_writeoff"})
HOURLY_RATE_BANDS = frozenset({"$0-50", "$50-100", "$100-150", "$150-200", "$200+"})


def hourly_rate_to_band(rate: float | None) -> str | None:
    """Provider hourly rate is non-PHI per the BAA carve-out, but is
    sensitive comp data. Banded so "Dr. Jones makes $187/hr" can't be
    re-identified across a tiny practice. None -> None.
    """
    if rate is None:
        return None
    r = float(rate)
    if r <= 0:
        return None
    if r < 50:
        return "$0-50"
    if r < 100:
        return "$50-100"
    if r < 150:
        return "$100-150"
    if r < 200:
        return "$150-200"
    return "$200+"


# -------------------------------------------------------------------------
# Extension A: treatment_plans_raw
# -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TreatmentPlanRow:
    external_id: str
    practice_id: str
    patient_external_id: str
    provider_id: str
    presented_date_month: str
    accepted_date_month: str | None
    declined_date_month: str | None
    expired_date_month: str | None
    status: str
    plan_dollars_band: str
    procedure_category: str | None

    def validate(self) -> None:
        _check("external_id", self.external_id, len(self.external_id) >= 8)
        _check("practice_id", self.practice_id, len(self.practice_id) >= 8)
        _check(
            "patient_external_id",
            self.patient_external_id,
            len(self.patient_external_id) >= 8,
        )
        _check("provider_id", self.provider_id, len(str(self.provider_id)) >= 1)
        _check(
            "presented_date_month",
            self.presented_date_month,
            bool(_MONTH_RE.match(self.presented_date_month)),
        )
        for label, value in (
            ("accepted_date_month", self.accepted_date_month),
            ("declined_date_month", self.declined_date_month),
            ("expired_date_month", self.expired_date_month),
        ):
            if value is not None:
                _check(label, value, bool(_MONTH_RE.match(value)))
        _check("status", self.status, self.status in TREATMENT_PLAN_STATUSES)
        _check(
            "plan_dollars_band",
            self.plan_dollars_band,
            self.plan_dollars_band in REVENUE_BANDS,
        )


# -------------------------------------------------------------------------
# Extension B: claims_raw
# -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimRow:
    external_id: str
    practice_id: str
    patient_external_id: str
    payer_category: str
    submission_date_month: str
    payment_date_month: str | None
    denial_date_month: str | None
    authorization_required: bool
    authorization_date_month: str | None
    denial_reason_category: str | None
    status: str
    pre_verified: bool

    def validate(self) -> None:
        _check("external_id", self.external_id, len(self.external_id) >= 8)
        _check("practice_id", self.practice_id, len(self.practice_id) >= 8)
        _check(
            "patient_external_id",
            self.patient_external_id,
            len(self.patient_external_id) >= 8,
        )
        _check("payer_category", self.payer_category, self.payer_category in PAYER_CATEGORIES)
        _check(
            "submission_date_month",
            self.submission_date_month,
            bool(_MONTH_RE.match(self.submission_date_month)),
        )
        for label, value in (
            ("payment_date_month", self.payment_date_month),
            ("denial_date_month", self.denial_date_month),
            ("authorization_date_month", self.authorization_date_month),
        ):
            if value is not None:
                _check(label, value, bool(_MONTH_RE.match(value)))
        if self.denial_reason_category is not None:
            _check(
                "denial_reason_category",
                self.denial_reason_category,
                self.denial_reason_category in DENIAL_REASON_CATEGORIES,
            )
        _check("status", self.status, self.status in CLAIM_STATUSES)
        if not isinstance(self.authorization_required, bool):
            raise ValidationError(
                f"authorization_required must be bool, got {self.authorization_required!r}"
            )
        if not isinstance(self.pre_verified, bool):
            raise ValidationError(
                f"pre_verified must be bool, got {self.pre_verified!r}"
            )


# -------------------------------------------------------------------------
# Extension C: schedule_capacity_raw
# -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapacityRow:
    practice_id: str
    practice_period: str  # YYYY-MM
    provider_id: str | None
    chair_id: str | None
    scheduled_hours: float
    productive_hours: float

    def validate(self) -> None:
        _check("practice_id", self.practice_id, len(self.practice_id) >= 8)
        _check(
            "practice_period",
            self.practice_period,
            bool(_MONTH_RE.match(self.practice_period)),
        )
        # Exactly one of provider_id / chair_id must be set (the schema
        # is at one of two grains; never both, never neither).
        has_provider = self.provider_id is not None and self.provider_id != ""
        has_chair = self.chair_id is not None and self.chair_id != ""
        if has_provider == has_chair:
            raise ValidationError(
                "schedule_capacity_raw: exactly one of provider_id / chair_id "
                f"must be set (got provider={self.provider_id!r}, chair={self.chair_id!r})"
            )
        _check("scheduled_hours", self.scheduled_hours, self.scheduled_hours >= 0)
        _check("productive_hours", self.productive_hours, self.productive_hours >= 0)


# -------------------------------------------------------------------------
# Extension D: payments_raw
# -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PaymentRow:
    external_id: str
    practice_id: str
    patient_external_id: str
    payment_date_month: str
    amount_band: str
    payment_source: str
    payer_category: str

    def validate(self) -> None:
        _check("external_id", self.external_id, len(self.external_id) >= 8)
        _check("practice_id", self.practice_id, len(self.practice_id) >= 8)
        _check(
            "patient_external_id",
            self.patient_external_id,
            len(self.patient_external_id) >= 8,
        )
        _check(
            "payment_date_month",
            self.payment_date_month,
            bool(_MONTH_RE.match(self.payment_date_month)),
        )
        _check("amount_band", self.amount_band, self.amount_band in REVENUE_BANDS)
        _check("payment_source", self.payment_source, self.payment_source in PAYMENT_SOURCES)
        _check("payer_category", self.payer_category, self.payer_category in PAYER_CATEGORIES)


# -------------------------------------------------------------------------
# Extension E: timekeeping_raw
# -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimekeepingRow:
    practice_id: str
    practice_period: str
    provider_id: str | None
    staff_role: str | None
    scheduled_hours: float
    productive_hours: float
    hourly_rate_band: str | None  # banded so a 4-provider practice can't be re-id'd

    def validate(self) -> None:
        _check("practice_id", self.practice_id, len(self.practice_id) >= 8)
        _check(
            "practice_period",
            self.practice_period,
            bool(_MONTH_RE.match(self.practice_period)),
        )
        has_provider = self.provider_id is not None and self.provider_id != ""
        has_role = self.staff_role is not None and self.staff_role != ""
        if has_provider == has_role:
            raise ValidationError(
                "timekeeping_raw: exactly one of provider_id / staff_role "
                f"must be set (got provider={self.provider_id!r}, role={self.staff_role!r})"
            )
        _check("scheduled_hours", self.scheduled_hours, self.scheduled_hours >= 0)
        _check("productive_hours", self.productive_hours, self.productive_hours >= 0)
        if self.hourly_rate_band is not None:
            _check(
                "hourly_rate_band",
                self.hourly_rate_band,
                self.hourly_rate_band in HOURLY_RATE_BANDS,
            )


# -------------------------------------------------------------------------
# Extension F: patients_raw_extension
# -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PatientExtensionRow:
    """Three additional columns for the existing patients_raw rows.

    Joined to patients_raw on patient_external_id at cloud-side ingest.
    """

    practice_id: str
    patient_external_id: str
    last_visit_date_month: str
    recall_due_date_month: str | None
    referral_source_category: str | None

    def validate(self) -> None:
        _check("practice_id", self.practice_id, len(self.practice_id) >= 8)
        _check(
            "patient_external_id",
            self.patient_external_id,
            len(self.patient_external_id) >= 8,
        )
        _check(
            "last_visit_date_month",
            self.last_visit_date_month,
            bool(_MONTH_RE.match(self.last_visit_date_month)),
        )
        if self.recall_due_date_month is not None:
            _check(
                "recall_due_date_month",
                self.recall_due_date_month,
                bool(_MONTH_RE.match(self.recall_due_date_month)),
            )
