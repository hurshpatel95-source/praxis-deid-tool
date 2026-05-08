"""The 6 canonical CSV schemas the wizard maps source PMS schemas TO.

Derived from `praxis-app/METRIC_COVERAGE_AUDIT.md §4.1` (Extensions A-F).
This file is the source of truth for the wizard's TARGET shape — the
existing per-canonical-row dataclasses in `praxis_deid/schema.py` cover
the v0.1 contract (patients/appointments/providers/procedures/referrals/
invoices); these wizard schemas describe NEW canonical CSVs the de-id
tool will produce after Wizard-1 ships, plus the `patients_raw.csv`
extension columns.

Each schema is described in a Claude-friendly way:
    - Plain-English `description` (Claude reads this)
    - Per-column type, required/optional, enum values, format constraints
    - HIPAA notes (which columns get HMAC'd, banded, dropped) — same
      posture as `safe_harbor.py`. The wizard does NOT apply Safe Harbor;
      it just produces a mapping that the existing de-id pipeline runs
      against. But the notes guide Claude away from confusing source
      columns that need special handling.

The wizard sends these schemas as part of the prompt to Claude. They are
schema-level metadata only — never PHI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CanonicalColumn:
    """A single column in a canonical CSV schema."""

    name: str
    type: str  # "string" | "int" | "numeric" | "date" | "datetime" | "bool" | "enum"
    required: bool
    description: str
    # For enum types, the list of allowed values. None for non-enum types.
    enum_values: tuple[str, ...] | None = None
    # Format hint, e.g. "YYYY-MM-DD" for dates. Free-form, for Claude's eyes.
    format: str | None = None
    # HIPAA handling at de-id time. NOT applied by the wizard — purely
    # documentary so Claude doesn't propose to include raw PHI in a mapping.
    #   "hmac"     -> HMAC-SHA256 with practice salt (IDs)
    #   "month"    -> truncate YYYY-MM-DD to YYYY-MM
    #   "band"     -> bucket numeric into REVENUE_BANDS (or similar)
    #   "category" -> map to controlled vocabulary
    #   "passthrough" -> keep as-is (already non-identifying)
    hipaa_handling: str = "passthrough"


@dataclass(frozen=True)
class CanonicalSchema:
    """One target CSV the wizard knows how to produce."""

    name: str  # e.g. "treatment_plans_raw"
    extension_letter: str  # "A".."F" — matches METRIC_COVERAGE_AUDIT.md
    description: str
    columns: tuple[CanonicalColumn, ...]
    # Free-form notes shown to Claude — joins, gotchas, expected cardinality.
    claude_notes: tuple[str, ...] = field(default_factory=tuple)
    # Whether this schema EXTENDS an existing canonical contract rather
    # than defining a brand-new file. Used by Extension F.
    extends: str | None = None

    @property
    def required_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.required)

    def to_prompt_dict(self) -> dict[str, Any]:
        """Render as a JSON-serializable dict for the Claude prompt.

        Keep this stable — the recorded fixture in tests asserts on this
        shape. Changing it invalidates recorded responses.
        """
        return {
            "name": self.name,
            "extension_letter": self.extension_letter,
            "description": self.description,
            "extends": self.extends,
            "columns": [
                {
                    "name": c.name,
                    "type": c.type,
                    "required": c.required,
                    "description": c.description,
                    "enum_values": list(c.enum_values) if c.enum_values else None,
                    "format": c.format,
                    "hipaa_handling": c.hipaa_handling,
                }
                for c in self.columns
            ],
            "claude_notes": list(self.claude_notes),
        }


# --- Extension A: treatment_plans_raw.csv ---------------------------------
_TREATMENT_PLAN_STATUSES = ("presented", "accepted", "declined", "expired", "partial")

_EXTENSION_A = CanonicalSchema(
    name="treatment_plans_raw",
    extension_letter="A",
    description=(
        "Treatment plans presented to patients, with full lifecycle dates "
        "(presented/accepted/declined/expired). Highest-leverage extension; "
        "unlocks 5 of 6 metrics in the Treatment Plan section. One row per "
        "treatment plan."
    ),
    columns=(
        CanonicalColumn(
            name="source_id",
            type="string",
            required=True,
            description="Source-PMS plan identifier. HMAC'd at de-id time.",
            hipaa_handling="hmac",
        ),
        CanonicalColumn(
            name="patient_source_id",
            type="string",
            required=True,
            description=(
                "Source-PMS patient identifier. Foreign key to patients_raw. "
                "HMAC'd at de-id time using the SAME salt as patients_raw "
                "so joins survive de-identification."
            ),
            hipaa_handling="hmac",
        ),
        CanonicalColumn(
            name="provider_id",
            type="string",
            required=True,
            description="Provider who presented the plan. Provider IDs are not PHI.",
            hipaa_handling="passthrough",
        ),
        CanonicalColumn(
            name="presented_date",
            type="date",
            required=True,
            description="Date the plan was presented to the patient.",
            format="YYYY-MM-DD",
            hipaa_handling="month",
        ),
        CanonicalColumn(
            name="accepted_date",
            type="date",
            required=False,
            description="Date the patient accepted the plan; NULL if not accepted.",
            format="YYYY-MM-DD",
            hipaa_handling="month",
        ),
        CanonicalColumn(
            name="declined_date",
            type="date",
            required=False,
            description="Date the patient declined the plan; NULL if not declined.",
            format="YYYY-MM-DD",
            hipaa_handling="month",
        ),
        CanonicalColumn(
            name="expired_date",
            type="date",
            required=False,
            description=(
                "Date the plan was auto-expired or aged out. Some PMSs auto-expire "
                "stale plans; others don't expose this. NULL if not expired."
            ),
            format="YYYY-MM-DD",
            hipaa_handling="month",
        ),
        CanonicalColumn(
            name="status",
            type="enum",
            required=True,
            description=(
                "Lifecycle status. Map source PMS status codes to this controlled "
                "vocabulary in the `transformations` block."
            ),
            enum_values=_TREATMENT_PLAN_STATUSES,
            hipaa_handling="passthrough",
        ),
        CanonicalColumn(
            name="plan_dollars",
            type="numeric",
            required=True,
            description=(
                "Total plan dollars, sum of constituent procedure fees. "
                "Banded at de-id time."
            ),
            hipaa_handling="band",
        ),
        CanonicalColumn(
            name="procedure_category",
            type="string",
            required=False,
            description=(
                "Optional. The dominant procedure category in the plan "
                "(e.g. 'restorative', 'preventive', 'endo'). Enables the "
                "acceptance-rate-by-category metric."
            ),
            hipaa_handling="category",
        ),
    ),
    claude_notes=(
        "In Open Dental, treatment plans live in `treatplan` joined to "
        "`treatplanattach` (which links plans to procedures) and `proctp` "
        "(procedure-level rows on a plan). plan_dollars usually requires "
        "summing `proctp.FeeAmt` for the plan.",
        "Status often must be derived from a combination of fields, not "
        "a single column — e.g. accepted_date IS NOT NULL implies status="
        "'accepted'. Use the transformations block.",
        "If the source PMS has no notion of 'expired_date', set the source "
        "expression for that column to NULL and `confidence` low so the "
        "human reviewer is alerted.",
    ),
)


# --- Extension B: claims_raw.csv -----------------------------------------
_CLAIM_STATUSES = ("submitted", "paid", "denied", "pending", "partial")
_DENIAL_REASONS = ("eligibility", "coverage", "auth", "documentation", "other")
_PAYER_CATEGORIES = (
    "commercial", "medicare", "medicaid", "self_pay", "workers_comp", "auto", "other",
)

_EXTENSION_B = CanonicalSchema(
    name="claims_raw",
    extension_letter="B",
    description=(
        "Insurance claims submitted by the practice, with full lifecycle "
        "(submission/payment/denial). Second-highest-leverage extension; "
        "unlocks 6 metrics across Insurance + Compliance sections. One row "
        "per claim."
    ),
    columns=(
        CanonicalColumn(
            name="source_id",
            type="string",
            required=True,
            description="Source-PMS claim identifier. HMAC'd at de-id time.",
            hipaa_handling="hmac",
        ),
        CanonicalColumn(
            name="patient_source_id",
            type="string",
            required=True,
            description=(
                "Foreign key to patients_raw. HMAC'd at de-id time with the "
                "same salt as patients_raw."
            ),
            hipaa_handling="hmac",
        ),
        CanonicalColumn(
            name="payer_category",
            type="enum",
            required=True,
            description=(
                "Mapped to the controlled payer vocabulary. Source PMS will "
                "typically have a per-carrier name; map carrier -> category in "
                "the transformations block."
            ),
            enum_values=_PAYER_CATEGORIES,
            hipaa_handling="category",
        ),
        CanonicalColumn(
            name="submission_date",
            type="date",
            required=True,
            description="Date the claim was submitted.",
            format="YYYY-MM-DD",
            hipaa_handling="month",
        ),
        CanonicalColumn(
            name="payment_date",
            type="date",
            required=False,
            description="Date the claim was paid; NULL if unpaid.",
            format="YYYY-MM-DD",
            hipaa_handling="month",
        ),
        CanonicalColumn(
            name="denial_date",
            type="date",
            required=False,
            description="Date the claim was denied; NULL if not denied.",
            format="YYYY-MM-DD",
            hipaa_handling="month",
        ),
        CanonicalColumn(
            name="authorization_required",
            type="bool",
            required=True,
            description="Whether prior authorization was required for this claim.",
            hipaa_handling="passthrough",
        ),
        CanonicalColumn(
            name="authorization_date",
            type="date",
            required=False,
            description="Date authorization was obtained; NULL if not required or pending.",
            format="YYYY-MM-DD",
            hipaa_handling="month",
        ),
        CanonicalColumn(
            name="denial_reason_category",
            type="enum",
            required=False,
            description=(
                "If denied, the high-level reason category. Map source PMS "
                "denial codes (often free-text or carrier-specific) to this "
                "vocabulary in the transformations block."
            ),
            enum_values=_DENIAL_REASONS,
            hipaa_handling="category",
        ),
        CanonicalColumn(
            name="status",
            type="enum",
            required=True,
            description="Claim lifecycle status.",
            enum_values=_CLAIM_STATUSES,
            hipaa_handling="passthrough",
        ),
        CanonicalColumn(
            name="pre_verified",
            type="bool",
            required=True,
            description=(
                "Was eligibility verified BEFORE the appointment that generated "
                "this claim? Drives the eligibility-verification compliance "
                "metric."
            ),
            hipaa_handling="passthrough",
        ),
    ),
    claude_notes=(
        "In Open Dental, claims live in `claim` joined to `claimproc` "
        "(per-procedure claim breakdown) and `clearinghouseslog` "
        "(submission history). Carrier metadata is in `carrier`, plan in "
        "`insplan`, subscriber in `inssub`.",
        "`payer_category` is rarely a single source column. You will almost "
        "always need a CASE expression on `carrier.CarrierName` or a "
        "lookup table the practice maintains. Mark confidence low if the "
        "PMS gives only free-text carrier names.",
        "Pre-verification (`pre_verified`) often lives in a different table "
        "than the claim itself — it's an appointment-level flag in some "
        "PMSs and a procedure-level flag in others. Be explicit about the "
        "join.",
    ),
)


# --- Extension C: schedule_capacity_raw.csv ------------------------------
_EXTENSION_C = CanonicalSchema(
    name="schedule_capacity_raw",
    extension_letter="C",
    description=(
        "Schedule capacity per provider OR chair, per practice month. "
        "One row per (provider OR chair, month). Drives utilization, "
        "production-per-chair, and same-day fill metrics."
    ),
    columns=(
        CanonicalColumn(
            name="practice_period",
            type="string",
            required=True,
            description="First-of-month bucket the row applies to.",
            format="YYYY-MM",
            hipaa_handling="passthrough",
        ),
        CanonicalColumn(
            name="provider_id",
            type="string",
            required=False,
            description=(
                "Provider this row describes. Either provider_id OR chair_id "
                "must be set; rows are at one of those two grains."
            ),
            hipaa_handling="passthrough",
        ),
        CanonicalColumn(
            name="chair_id",
            type="string",
            required=False,
            description=(
                "Chair / operatory this row describes. Either provider_id OR "
                "chair_id must be set; rows are at one of those two grains."
            ),
            hipaa_handling="passthrough",
        ),
        CanonicalColumn(
            name="scheduled_hours",
            type="numeric",
            required=True,
            description="Total slot-hours available in the period (capacity).",
            hipaa_handling="passthrough",
        ),
        CanonicalColumn(
            name="productive_hours",
            type="numeric",
            required=True,
            description=(
                "Hours actually used (sum of completed-appointment durations "
                "in the period)."
            ),
            hipaa_handling="passthrough",
        ),
    ),
    claude_notes=(
        "In Open Dental: scheduled_hours comes from the `schedule` table "
        "(provider blocks, operatory blocks). productive_hours comes from "
        "summing `appointment.AptLength` for completed appointments in the "
        "period.",
        "Note the OR semantics on provider_id/chair_id: this is two grains "
        "stacked in one file. The mapping should produce TWO sets of rows "
        "(one per grain) via UNION ALL — describe both in the transformations "
        "block.",
    ),
)


# --- Extension D: payments_raw.csv ----------------------------------------
_PAYMENT_SOURCES = ("insurance", "patient", "adjustment_writeoff")

_EXTENSION_D = CanonicalSchema(
    name="payments_raw",
    extension_letter="D",
    description=(
        "Individual payments received by the practice — insurance, patient, "
        "or write-off adjustments. One row per payment. Drives Collections "
        "Rate and Insurance vs OOP mix metrics."
    ),
    columns=(
        CanonicalColumn(
            name="source_id",
            type="string",
            required=True,
            description="Source-PMS payment identifier. HMAC'd at de-id time.",
            hipaa_handling="hmac",
        ),
        CanonicalColumn(
            name="patient_source_id",
            type="string",
            required=True,
            description=(
                "Foreign key to patients_raw. HMAC'd at de-id with the same "
                "salt as patients_raw."
            ),
            hipaa_handling="hmac",
        ),
        CanonicalColumn(
            name="payment_date",
            type="date",
            required=True,
            description="Date the payment was received.",
            format="YYYY-MM-DD",
            hipaa_handling="month",
        ),
        CanonicalColumn(
            name="amount",
            type="numeric",
            required=True,
            description="Payment amount in dollars. Banded at de-id time.",
            hipaa_handling="band",
        ),
        CanonicalColumn(
            name="payment_source",
            type="enum",
            required=True,
            description=(
                "Where the payment came from. 'adjustment_writeoff' covers "
                "credits and write-offs that aren't true money in."
            ),
            enum_values=_PAYMENT_SOURCES,
            hipaa_handling="passthrough",
        ),
        CanonicalColumn(
            name="payer_category",
            type="enum",
            required=True,
            description=(
                "Same controlled vocabulary as patients_raw.payer_category."
            ),
            enum_values=_PAYER_CATEGORIES,
            hipaa_handling="category",
        ),
    ),
    claude_notes=(
        "In Open Dental: `payment` joined with `paysplit` (which allocates "
        "the payment across patients/procedures). amount is `paysplit.SplitAmt` "
        "for payment-source = 'patient'; for 'insurance' the payment lives "
        "in `claimpayment` instead.",
        "Adjustments and write-offs may be in a separate `adjustment` table — "
        "if so, the mapping needs UNION ALL of payments + adjustments.",
    ),
)


# --- Extension E: timekeeping_raw.csv -------------------------------------
_EXTENSION_E = CanonicalSchema(
    name="timekeeping_raw",
    extension_letter="E",
    description=(
        "Provider and staff hours per practice month. Either provider_id OR "
        "staff_role grain. Drives provider compensation %, staff turnover, "
        "and per-hour productivity metrics."
    ),
    columns=(
        CanonicalColumn(
            name="practice_period",
            type="string",
            required=True,
            description="Month bucket.",
            format="YYYY-MM",
            hipaa_handling="passthrough",
        ),
        CanonicalColumn(
            name="provider_id",
            type="string",
            required=False,
            description=(
                "Provider this row describes. Either provider_id OR staff_role "
                "must be set."
            ),
            hipaa_handling="passthrough",
        ),
        CanonicalColumn(
            name="staff_role",
            type="string",
            required=False,
            description=(
                "Aggregate staff role (e.g. 'hygienist', 'assistant', 'admin') "
                "if the row is at role-grain rather than per-provider."
            ),
            hipaa_handling="passthrough",
        ),
        CanonicalColumn(
            name="scheduled_hours",
            type="numeric",
            required=True,
            description="Hours scheduled in the period.",
            hipaa_handling="passthrough",
        ),
        CanonicalColumn(
            name="productive_hours",
            type="numeric",
            required=True,
            description="Hours actually worked (clocked or attributed).",
            hipaa_handling="passthrough",
        ),
        CanonicalColumn(
            name="hourly_rate",
            type="numeric",
            required=False,
            description=(
                "OPTIONAL — provider hourly rate if available. Not PHI per "
                "the BAA invariants provider carve-out, but practices may "
                "choose to omit. NULL if unavailable."
            ),
            hipaa_handling="passthrough",
        ),
    ),
    claude_notes=(
        "In Open Dental: hours come from `schedule` (same table as "
        "Extension C, different aggregation). hourly_rate lives on "
        "`provider.HourlyRate` for providers; staff timekeeping rarely "
        "lives in the PMS at all (it's usually in payroll software).",
        "If a column has no source in the PMS, set its source_expression "
        "to NULL and confidence to 0.0 — DO NOT guess.",
    ),
)


# --- Extension F: patients_raw EXTENSION columns --------------------------
_EXTENSION_F = CanonicalSchema(
    name="patients_raw_extension",
    extension_letter="F",
    description=(
        "ADDITIONS to the existing patients_raw.csv contract — three new "
        "columns. Do not re-map the columns already in patients_raw "
        "(external_id, age_band, zip_prefix, gender, payer_category, "
        "patient_status, first_seen_month). Just describe how to populate "
        "these three new ones."
    ),
    extends="patients_raw",
    columns=(
        CanonicalColumn(
            name="last_visit_date",
            type="date",
            required=True,
            description=(
                "Date of the patient's most recent completed visit. "
                "Truncated to month at de-id time."
            ),
            format="YYYY-MM-DD",
            hipaa_handling="month",
        ),
        CanonicalColumn(
            name="recall_due_date",
            type="date",
            required=False,
            description=(
                "Date the patient is next due for a recall (hygiene, perio, "
                "etc.). NULL if no recall is set. Truncated to month at de-id."
            ),
            format="YYYY-MM-DD",
            hipaa_handling="month",
        ),
        CanonicalColumn(
            name="referral_source_category",
            type="string",
            required=False,
            description=(
                "Mapped controlled vocab for how the patient found the "
                "practice — e.g. 'internal' (existing-patient referral), "
                "'external' (other professional), 'google_ads', 'walk_in', "
                "'insurance_directory', 'unknown'. The exact vocabulary is "
                "open; map source PMS referral_source codes to canonical "
                "categories."
            ),
            hipaa_handling="category",
        ),
    ),
    claude_notes=(
        "In Open Dental: last_visit_date is `patient.DateLastVisit`. "
        "recall_due_date comes from the `recall` table (one row per patient "
        "per recall type — pick the soonest open recall for the patient). "
        "referral_source comes from `patient.ReferredBy` (a FK into the "
        "`definition` table, category=referral source).",
        "These three columns ATTACH to existing patients_raw rows — they "
        "are not a new file. The mapping should describe a JOIN expression "
        "on patient_source_id, not a new file.",
    ),
)


# --- Module-level registry -----------------------------------------------
CANONICAL_SCHEMAS: tuple[CanonicalSchema, ...] = (
    _EXTENSION_A,
    _EXTENSION_B,
    _EXTENSION_C,
    _EXTENSION_D,
    _EXTENSION_E,
    _EXTENSION_F,
)


def load_canonical_schemas() -> tuple[CanonicalSchema, ...]:
    """Return the 6 canonical schemas as a frozen tuple.

    Defined as a function (not just a module-level constant) so future
    versions could load additional contracts from disk without breaking
    callers.
    """
    return CANONICAL_SCHEMAS


def get_schema(name: str) -> CanonicalSchema:
    """Lookup a canonical schema by name. Raises KeyError if not found."""
    for schema in CANONICAL_SCHEMAS:
        if schema.name == name:
            return schema
    raise KeyError(f"unknown canonical schema: {name!r}")
