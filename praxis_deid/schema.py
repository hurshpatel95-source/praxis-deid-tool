"""Canonical row dataclasses + validators.

These match the TypeScript schemas in praxis-app/lib/canonical/. Any change
here must be mirrored there. Both sides validate so CSV produced by this
tool round-trips through Praxis ingestion without surprises.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .safe_harbor import AGE_BANDS, DURATION_BANDS, REVENUE_BANDS

# --- Allowed enum values (kept in sync with lib/canonical/primitives.ts) ---
GENDERS = frozenset({"F", "M", "X", "unknown"})
PAYER_CATEGORIES = frozenset(
    {"commercial", "medicare", "medicaid", "self_pay", "workers_comp", "auto", "other"}
)
PATIENT_STATUSES = frozenset({"active", "lapsed", "archived"})
APPOINTMENT_STATUSES = frozenset(
    {"scheduled", "completed", "no_show", "cancelled", "rescheduled"}
)
APPOINTMENT_TYPE_CATEGORIES = frozenset(
    {"routine", "consult", "follow_up", "procedure", "imaging", "urgent", "telehealth", "other"}
)
INVOICE_AGE_BUCKETS = frozenset({"current", "30-60", "60-90", "90+"})
INVOICE_STATUSES = frozenset({"paid", "pending", "overdue", "written_off"})

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_ZIP3_RE = re.compile(r"^\d{3}$")


class ValidationError(ValueError):
    """Raised when a canonical row fails schema validation. Caller decides
    whether to drop the row, abort the run, or surface to the audit log."""


def _check(field: str, value: object, ok: bool) -> None:
    if not ok:
        raise ValidationError(f"invalid {field}: {value!r}")


@dataclass(frozen=True, slots=True)
class Patient:
    external_id: str
    practice_id: str
    age_band: str
    zip_prefix: str
    gender: str
    payer_category: str
    patient_status: str
    first_seen_month: str

    def validate(self) -> None:
        _check("external_id", self.external_id, len(self.external_id) >= 8)
        _check("practice_id", self.practice_id, len(self.practice_id) >= 8)
        _check("age_band", self.age_band, self.age_band in AGE_BANDS)
        _check("zip_prefix", self.zip_prefix, bool(_ZIP3_RE.match(self.zip_prefix)))
        _check("gender", self.gender, self.gender in GENDERS)
        _check("payer_category", self.payer_category, self.payer_category in PAYER_CATEGORIES)
        _check("patient_status", self.patient_status, self.patient_status in PATIENT_STATUSES)
        _check("first_seen_month", self.first_seen_month, bool(_MONTH_RE.match(self.first_seen_month)))


@dataclass(frozen=True, slots=True)
class Appointment:
    external_id: str
    practice_id: str
    patient_external_id: str
    provider_id: str
    appointment_date_month: str
    appointment_type_category: str
    status: str
    duration_minutes_band: str

    def validate(self) -> None:
        _check("external_id", self.external_id, len(self.external_id) >= 8)
        _check("practice_id", self.practice_id, len(self.practice_id) >= 8)
        _check("patient_external_id", self.patient_external_id, len(self.patient_external_id) >= 8)
        _check("provider_id", self.provider_id, len(self.provider_id) >= 1)
        _check("appointment_date_month", self.appointment_date_month, bool(_MONTH_RE.match(self.appointment_date_month)))
        _check("appointment_type_category", self.appointment_type_category, self.appointment_type_category in APPOINTMENT_TYPE_CATEGORIES)
        _check("status", self.status, self.status in APPOINTMENT_STATUSES)
        _check("duration_minutes_band", self.duration_minutes_band, self.duration_minutes_band in DURATION_BANDS)


@dataclass(frozen=True, slots=True)
class Provider:
    id: str
    practice_id: str
    full_name: str
    npi: str | None
    specialty: str
    active: bool

    def validate(self) -> None:
        _check("id", self.id, len(self.id) >= 1)
        _check("practice_id", self.practice_id, len(self.practice_id) >= 8)
        _check("full_name", self.full_name, len(self.full_name) >= 1)
        if self.npi is not None:
            _check("npi", self.npi, len(self.npi) == 10 and self.npi.isdigit())
        _check("specialty", self.specialty, len(self.specialty) >= 1)


@dataclass(frozen=True, slots=True)
class Procedure:
    external_id: str
    practice_id: str
    patient_external_id: str
    provider_id: str
    procedure_category: str
    procedure_date_month: str
    revenue_band: str

    def validate(self) -> None:
        _check("external_id", self.external_id, len(self.external_id) >= 8)
        _check("practice_id", self.practice_id, len(self.practice_id) >= 8)
        _check("patient_external_id", self.patient_external_id, len(self.patient_external_id) >= 8)
        _check("provider_id", self.provider_id, len(self.provider_id) >= 1)
        _check("procedure_category", self.procedure_category, len(self.procedure_category) >= 1)
        _check("procedure_date_month", self.procedure_date_month, bool(_MONTH_RE.match(self.procedure_date_month)))
        _check("revenue_band", self.revenue_band, self.revenue_band in REVENUE_BANDS)


@dataclass(frozen=True, slots=True)
class Referral:
    external_id: str
    practice_id: str
    referring_provider_id: str
    referring_provider_name: str
    referring_provider_practice: str
    referred_patient_external_id: str
    referral_date_month: str
    converted_to_appointment: bool
    revenue_generated_band: str | None = None

    def validate(self) -> None:
        _check("external_id", self.external_id, len(self.external_id) >= 8)
        _check("practice_id", self.practice_id, len(self.practice_id) >= 8)
        _check("referring_provider_id", self.referring_provider_id, len(self.referring_provider_id) >= 1)
        _check("referring_provider_name", self.referring_provider_name, len(self.referring_provider_name) >= 1)
        _check("referring_provider_practice", self.referring_provider_practice, len(self.referring_provider_practice) >= 1)
        _check("referred_patient_external_id", self.referred_patient_external_id, len(self.referred_patient_external_id) >= 8)
        _check("referral_date_month", self.referral_date_month, bool(_MONTH_RE.match(self.referral_date_month)))
        if self.revenue_generated_band is not None:
            _check("revenue_generated_band", self.revenue_generated_band, self.revenue_generated_band in REVENUE_BANDS)


@dataclass(frozen=True, slots=True)
class Invoice:
    external_id: str
    practice_id: str
    invoice_date_month: str
    amount_band: str
    payer_category: str
    status: str
    age_bucket: str

    def validate(self) -> None:
        _check("external_id", self.external_id, len(self.external_id) >= 8)
        _check("practice_id", self.practice_id, len(self.practice_id) >= 8)
        _check("invoice_date_month", self.invoice_date_month, bool(_MONTH_RE.match(self.invoice_date_month)))
        _check("amount_band", self.amount_band, self.amount_band in REVENUE_BANDS)
        _check("payer_category", self.payer_category, self.payer_category in PAYER_CATEGORIES)
        _check("status", self.status, self.status in INVOICE_STATUSES)
        _check("age_bucket", self.age_bucket, self.age_bucket in INVOICE_AGE_BUCKETS)


# Convenience: dataclass -> dict for CSV writing.
def to_dict(record: object) -> dict[str, object]:
    return asdict(record)  # type: ignore[arg-type]


# Set of fields that MUST NOT appear in any canonical row, regardless of source.
# Used by the test suite to assert no PHI leaked through.
FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "first_name", "last_name", "full_name_patient", "name", "patient_name",
        "dob", "date_of_birth", "birthdate", "birth_date",
        "ssn", "social_security_number",
        "mrn", "medical_record_number", "account_number",
        "email", "phone", "phone_number", "address", "street", "city",
        "zip", "zip5", "zip_code", "postal_code",
        "ip_address", "device_id", "biometric",
    }
)
