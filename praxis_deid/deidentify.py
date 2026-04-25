"""Core de-identification: raw practice data -> canonical Safe Harbor rows.

This is the load-bearing piece. Bugs here become PHI leaks. Every transform
is exercised by the test suite (tests/test_deidentify.py).

Input shape: dictionaries of strings keyed by column names. The CSV reader
hands these in directly. We don't assume any specific PM system's column
names — the caller maps source columns to the standard names below.

Standard input column names (raw, identifying):
  patients:    source_id, first_name, last_name, dob, zip, gender,
               payer_category, patient_status, first_seen_date
  appointments: source_id, patient_source_id, provider_id, appointment_date,
                appointment_type_category, status, duration_minutes
  providers:   id, full_name, npi, specialty, active
  procedures:  source_id, patient_source_id, provider_id, procedure_category,
               procedure_date, revenue_amount
  referrals:   source_id, referring_provider_id, referring_provider_name,
               referring_provider_practice, referred_patient_source_id,
               referral_date, converted_to_appointment, revenue_generated
  invoices:    source_id, invoice_date, amount, payer_category, status, age_bucket

Output: Patient/Appointment/Provider/Procedure/Referral/Invoice dataclasses
from schema.py. Exactly the shape Praxis cloud expects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .hashing import stable_external_id
from .safe_harbor import (
    age_to_band,
    amount_to_band,
    date_to_month,
    duration_to_band,
    zip_to_prefix,
)
from .schema import (
    Appointment,
    Invoice,
    Patient,
    Procedure,
    Provider,
    Referral,
)


@dataclass
class DeidStats:
    """Per-run counters surfaced to the audit log."""
    patients_in: int = 0
    patients_out: int = 0
    appointments_in: int = 0
    appointments_out: int = 0
    providers_in: int = 0
    providers_out: int = 0
    procedures_in: int = 0
    procedures_out: int = 0
    referrals_in: int = 0
    referrals_out: int = 0
    invoices_in: int = 0
    invoices_out: int = 0
    rows_dropped: int = 0
    drop_reasons: dict[str, int] = field(default_factory=dict)
    small_n_suppressions: int = 0


def _age_from_dob(dob_str: str) -> int:
    """Parse common DOB formats (ISO, US slash) to an age in years.

    The exact age never leaves this function — only its band crosses the wire.
    """
    s = dob_str.strip()
    parsed: date | None = None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(s, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        # Try ISO datetime
        try:
            parsed = datetime.fromisoformat(s).date()
        except ValueError as err:
            raise ValueError(f"unparseable dob: {dob_str!r}") from err
    today = date.today()
    age = today.year - parsed.year - ((today.month, today.day) < (parsed.month, parsed.day))
    return age


class Deidentifier:
    """Stateful de-identifier scoped to a single practice + salt + run.

    Public methods take dicts (one row at a time) and return canonical
    dataclasses. Errors during single-row transformation are caught,
    counted in DeidStats.drop_reasons, and the row is dropped — the
    caller decides whether to abort the run if too many drops occur.

    Small-N suppression is applied at finalize() time: any patient with
    fewer than `small_n_threshold` total rows across appointments +
    procedures is removed from the output (along with their dependent
    rows). This prevents single-patient quasi-identifier reconstruction
    when combined with ZIP-3 + age band + payer.
    """

    def __init__(
        self,
        practice_id: str,
        salt: str,
        small_n_threshold: int = 5,
        procedure_categorization: dict[str, str] | None = None,
    ) -> None:
        if not practice_id:
            raise ValueError("practice_id required")
        if not salt:
            raise ValueError("salt required (and never log it)")
        self.practice_id = practice_id
        self._salt = salt
        self.small_n_threshold = small_n_threshold
        self.procedure_categorization = procedure_categorization or {}
        self.stats = DeidStats()

        self._patients: list[Patient] = []
        self._appointments: list[Appointment] = []
        self._providers: list[Provider] = []
        self._procedures: list[Procedure] = []
        self._referrals: list[Referral] = []
        self._invoices: list[Invoice] = []

    # --- per-row transforms ------------------------------------------------

    def add_patient(self, raw: dict[str, str]) -> None:
        self.stats.patients_in += 1
        try:
            ext = stable_external_id(self._salt, raw["source_id"])
            age = _age_from_dob(raw["dob"]) if raw.get("dob") else 0
            patient = Patient(
                external_id=ext,
                practice_id=self.practice_id,
                age_band=age_to_band(age),
                zip_prefix=zip_to_prefix(raw.get("zip")),
                gender=_normalize_gender(raw.get("gender", "unknown")),
                payer_category=_normalize_payer(raw.get("payer_category", "other")),
                patient_status=raw.get("patient_status", "active"),
                first_seen_month=date_to_month(raw.get("first_seen_date") or "2026-01-01"),
            )
            patient.validate()
            self._patients.append(patient)
            self.stats.patients_out += 1
        except (KeyError, ValueError) as err:
            self._drop("patient", str(err))

    def add_appointment(self, raw: dict[str, str]) -> None:
        self.stats.appointments_in += 1
        try:
            duration_raw = float(raw.get("duration_minutes") or 0)
            appt = Appointment(
                external_id=stable_external_id(self._salt, raw["source_id"]),
                practice_id=self.practice_id,
                patient_external_id=stable_external_id(self._salt, raw["patient_source_id"]),
                provider_id=raw["provider_id"],
                appointment_date_month=date_to_month(raw["appointment_date"]),
                appointment_type_category=raw.get("appointment_type_category", "other"),
                status=raw.get("status", "completed"),
                duration_minutes_band=duration_to_band(duration_raw),
            )
            appt.validate()
            self._appointments.append(appt)
            self.stats.appointments_out += 1
        except (KeyError, ValueError) as err:
            self._drop("appointment", str(err))

    def add_provider(self, raw: dict[str, str]) -> None:
        self.stats.providers_in += 1
        try:
            npi_raw = (raw.get("npi") or "").strip()
            provider = Provider(
                id=raw["id"],
                practice_id=self.practice_id,
                full_name=raw["full_name"],
                npi=npi_raw if (len(npi_raw) == 10 and npi_raw.isdigit()) else None,
                specialty=raw.get("specialty", "general"),
                active=str(raw.get("active", "true")).lower() in {"true", "1", "yes", "t"},
            )
            provider.validate()
            self._providers.append(provider)
            self.stats.providers_out += 1
        except (KeyError, ValueError) as err:
            self._drop("provider", str(err))

    def add_procedure(self, raw: dict[str, str]) -> None:
        self.stats.procedures_in += 1
        try:
            cat = self._categorize_procedure(raw.get("procedure_category", "consultation"))
            amount = float(raw.get("revenue_amount") or 0)
            proc = Procedure(
                external_id=stable_external_id(self._salt, raw["source_id"]),
                practice_id=self.practice_id,
                patient_external_id=stable_external_id(self._salt, raw["patient_source_id"]),
                provider_id=raw["provider_id"],
                procedure_category=cat,
                procedure_date_month=date_to_month(raw["procedure_date"]),
                revenue_band=amount_to_band(amount),
            )
            proc.validate()
            self._procedures.append(proc)
            self.stats.procedures_out += 1
        except (KeyError, ValueError) as err:
            self._drop("procedure", str(err))

    def add_referral(self, raw: dict[str, str]) -> None:
        self.stats.referrals_in += 1
        try:
            converted = str(raw.get("converted_to_appointment", "false")).lower() in {"true", "1", "yes", "t"}
            rev = raw.get("revenue_generated")
            ref = Referral(
                external_id=stable_external_id(self._salt, raw["source_id"]),
                practice_id=self.practice_id,
                referring_provider_id=raw["referring_provider_id"],
                referring_provider_name=raw["referring_provider_name"],
                referring_provider_practice=raw["referring_provider_practice"],
                referred_patient_external_id=stable_external_id(self._salt, raw["referred_patient_source_id"]),
                referral_date_month=date_to_month(raw["referral_date"]),
                converted_to_appointment=converted,
                revenue_generated_band=amount_to_band(float(rev)) if rev else None,
            )
            ref.validate()
            self._referrals.append(ref)
            self.stats.referrals_out += 1
        except (KeyError, ValueError) as err:
            self._drop("referral", str(err))

    def add_invoice(self, raw: dict[str, str]) -> None:
        self.stats.invoices_in += 1
        try:
            inv = Invoice(
                external_id=stable_external_id(self._salt, raw["source_id"]),
                practice_id=self.practice_id,
                invoice_date_month=date_to_month(raw["invoice_date"]),
                amount_band=amount_to_band(float(raw.get("amount") or 0)),
                payer_category=_normalize_payer(raw.get("payer_category", "other")),
                status=raw.get("status", "pending"),
                age_bucket=raw.get("age_bucket", "current"),
            )
            inv.validate()
            self._invoices.append(inv)
            self.stats.invoices_out += 1
        except (KeyError, ValueError) as err:
            self._drop("invoice", str(err))

    # --- finalize ---------------------------------------------------------

    def finalize(self) -> tuple[
        list[Patient], list[Appointment], list[Provider], list[Procedure],
        list[Referral], list[Invoice],
    ]:
        """Apply small-N suppression and return the canonical record sets.

        Suppression rule: a patient appears in output only if they have
        small_n_threshold OR MORE total touch points (appointments +
        procedures). Otherwise, the patient AND their dependent rows
        (appointments, procedures, referrals where they're the referred
        patient) are dropped.

        This is the practice-side enforcement of the n>=5 minimum cell
        size mentioned in the spec. Aggregate counts at the cloud side
        will respect this naturally because the underlying records are
        already gone.
        """
        touch_counts: dict[str, int] = {}
        for a in self._appointments:
            touch_counts[a.patient_external_id] = touch_counts.get(a.patient_external_id, 0) + 1
        for p in self._procedures:
            touch_counts[p.patient_external_id] = touch_counts.get(p.patient_external_id, 0) + 1

        keep = {
            ext for ext, n in touch_counts.items()
            if n >= self.small_n_threshold
        }

        # Patients with no touches at all: only keep if there are at least
        # small_n_threshold of THEM in the same demographic stratum (age_band,
        # zip_prefix). That prevents a single 18-30 / 080 / commercial new
        # patient from being identifiable. Conservative: drop them outright.
        new_only_kept: set[str] = set()
        from collections import Counter
        strata = Counter(
            (p.age_band, p.zip_prefix, p.payer_category) for p in self._patients
            if p.external_id not in touch_counts
        )
        for p in self._patients:
            if p.external_id in touch_counts:
                continue
            stratum = (p.age_band, p.zip_prefix, p.payer_category)
            if strata[stratum] >= self.small_n_threshold:
                new_only_kept.add(p.external_id)

        suppressed_before = len(self._patients)
        patients_out = [
            p for p in self._patients
            if p.external_id in keep or p.external_id in new_only_kept
        ]
        suppressed_count = suppressed_before - len(patients_out)
        self.stats.small_n_suppressions = suppressed_count

        kept_set = {p.external_id for p in patients_out}
        appointments_out = [a for a in self._appointments if a.patient_external_id in kept_set]
        procedures_out = [p for p in self._procedures if p.patient_external_id in kept_set]
        referrals_out = [
            r for r in self._referrals if r.referred_patient_external_id in kept_set
        ]
        invoices_out = self._invoices  # invoices aren't tied to patient_external_id

        return (
            patients_out,
            appointments_out,
            list(self._providers),
            procedures_out,
            referrals_out,
            invoices_out,
        )

    # --- internal --------------------------------------------------------

    def _drop(self, entity: str, reason: str) -> None:
        self.stats.rows_dropped += 1
        key = f"{entity}: {reason}"
        self.stats.drop_reasons[key] = self.stats.drop_reasons.get(key, 0) + 1

    def _categorize_procedure(self, raw_value: str) -> str:
        # If the practice provided a custom mapping, use it; otherwise pass
        # through the raw value (the spec lets practices supply categorized
        # data directly when their PM already buckets procedures).
        if not raw_value:
            return "consultation"
        return self.procedure_categorization.get(raw_value, raw_value)


# --- Light normalizers; not load-bearing for de-identification, but they
# keep cardinality of payer_category/gender from blowing up when sources
# emit "Female"/"BCBS"/etc. -------------------------------------------------

_GENDER_MAP = {
    "f": "F", "female": "F", "woman": "F",
    "m": "M", "male": "M", "man": "M",
    "x": "X", "nb": "X", "nonbinary": "X", "non-binary": "X", "other": "X",
}


def _normalize_gender(value: str) -> str:
    v = value.strip().lower()
    return _GENDER_MAP.get(v, "unknown")


_PAYER_KEYWORDS = {
    "medicare": "medicare",
    "medicaid": "medicaid",
    "self": "self_pay",
    "cash": "self_pay",
    "wc": "workers_comp",
    "workers": "workers_comp",
    "auto": "auto",
    "bcbs": "commercial",
    "aetna": "commercial",
    "cigna": "commercial",
    "unitedhealth": "commercial",
    "uhc": "commercial",
    "humana": "commercial",
    "anthem": "commercial",
    "oxford": "commercial",
    "horizon": "commercial",
}


def _normalize_payer(value: str) -> str:
    v = value.strip().lower()
    if v in {"commercial", "medicare", "medicaid", "self_pay", "workers_comp", "auto", "other"}:
        return v
    for key, mapped in _PAYER_KEYWORDS.items():
        if key in v:
            return mapped
    return "other"
