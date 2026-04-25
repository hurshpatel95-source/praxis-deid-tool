"""Generate a synthetic raw practice dataset with PHI.

Produces six CSV files in the output directory matching the column names
the de-id tool expects. Distributions are loosely realistic for an
orthopedic / sports-medicine practice in the NJ/PA region. Names and SSNs
are obviously fake; ZIPs and DOBs are realistically formatted so the
de-id transforms exercise their full range.

  python scripts/generate_synthetic.py --out /tmp/raw --patients 500 --seed 1

The output of this script + the de-id tool together produce the canonical
Layer 2 input that proves the privacy-by-design pipeline end-to-end.
"""

from __future__ import annotations

import argparse
import csv
import random
import string
from datetime import date, timedelta
from pathlib import Path

FIRST_NAMES = [
    "Alice", "Bob", "Carlos", "Dana", "Evan", "Fran", "Grace", "Hassan",
    "Iris", "Jack", "Kira", "Liam", "Maya", "Noah", "Olivia", "Priya",
    "Quinn", "Ravi", "Sofia", "Tariq", "Uma", "Vince", "Wren", "Xiao",
    "Yara", "Zane",
]
LAST_NAMES = [
    "Smith", "Jones", "Williams", "Park", "Khan", "Lee", "Patel", "Cohen",
    "Rivera", "Nguyen", "Singh", "Garcia", "OConnor", "Kumar", "Bianchi",
    "Goldberg", "Reilly", "Chen", "Alvarez", "Rahman",
]
ZIPS = [
    "08201", "08203", "08205", "08210", "08221", "08234", "08243",
    "08240", "08260", "08270", "08401", "08402",
    "19012", "19026", "19038", "19111", "19120",
    "11201", "11215", "11225",
]
PAYERS = [
    "BCBS", "Aetna", "Cigna", "United HealthCare", "Humana", "Anthem",
    "Horizon", "Oxford", "Medicare", "Medicaid", "Self-Pay", "Workers Comp",
]
PROVIDERS = [
    ("prov-1", "Dr. Aisha Khan", "1234567890", "orthopedic_surgery"),
    ("prov-2", "Dr. Marcus Chen", "2345678901", "sports_medicine"),
    ("prov-3", "Dr. Priya Patel", "3456789012", "physical_therapy"),
    ("prov-4", "Dr. Jordan Reilly", "4567890123", "general"),
    ("prov-5", "Dr. Sofia Alvarez", "5678901234", "orthopedic_surgery"),
]
APPT_TYPES = ["routine", "consult", "follow_up", "procedure", "imaging", "urgent", "telehealth"]
APPT_STATUSES = ["scheduled", "completed", "no_show", "cancelled", "rescheduled"]
PROC_CATEGORIES = [
    "knee_replacement", "acl_repair", "shoulder_arthroscopy", "hip_replacement",
    "physical_therapy_session", "imaging_mri", "imaging_xray", "consultation",
]
REFERRING_PRACTICES = [
    "Cherry Hill PCP", "Hammonton Family Med", "Atlantic Sports Med",
    "Toms River Pediatrics", "Jersey Shore Internal", "Linwood Wellness",
]
REFERRING_NAMES = ["Dr. Kumar", "Dr. Singh", "Dr. Williams", "Dr. Nguyen", "Dr. Cohen"]


def _random_dob(rng: random.Random) -> str:
    """Random DOB between 8 and 88 years ago. Mix of ISO and US slash format
    so we exercise both date-parsing paths in the de-id tool."""
    age = rng.randint(8, 88)
    days = rng.randint(0, 364)
    d = date.today() - timedelta(days=age * 365 + days)
    fmt = rng.choice(["%Y-%m-%d", "%m/%d/%Y"])
    return d.strftime(fmt)


def _random_phone(rng: random.Random) -> str:
    return f"609-555-{rng.randint(1000, 9999)}"


def _random_ssn(rng: random.Random) -> str:
    return f"{rng.randint(100, 899)}-{rng.randint(10, 99)}-{rng.randint(1000, 9999)}"


def _random_email(first: str, last: str, rng: random.Random) -> str:
    suffix = "".join(rng.choices(string.digits, k=2))
    return f"{first.lower()}.{last.lower()}{suffix}@example.com"


def _random_address(rng: random.Random) -> str:
    streets = ["Main St", "Oak Ave", "Pine Rd", "Birch Ln", "Cedar Dr", "Maple St", "Elm St"]
    return f"{rng.randint(1, 999)} {rng.choice(streets)}"


def _random_date_in_range(rng: random.Random, months_back: int) -> str:
    """Random date within the last `months_back` months. ISO format."""
    days_back = rng.randint(0, months_back * 30)
    d = date.today() - timedelta(days=days_back)
    return d.isoformat()


def generate(out_dir: Path, patient_count: int, seed: int = 0) -> None:
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- patients (with full PHI) ----
    patients_path = out_dir / "patients_raw.csv"
    patients: list[dict[str, str]] = []
    for i in range(patient_count):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        patients.append(
            {
                "source_id": f"MRN-{i + 1:06d}",
                "first_name": first,
                "last_name": last,
                "dob": _random_dob(rng),
                "ssn": _random_ssn(rng),
                "phone": _random_phone(rng),
                "email": _random_email(first, last, rng),
                "address": _random_address(rng),
                "zip": rng.choice(ZIPS),
                "gender": rng.choices(["F", "M", "Other", ""], weights=[0.52, 0.46, 0.01, 0.01])[0],
                "payer_category": rng.choice(PAYERS),
                "patient_status": rng.choices(["active", "lapsed", "archived"], weights=[0.78, 0.18, 0.04])[0],
                "first_seen_date": _random_date_in_range(rng, 36),
            }
        )
    _write_csv(patients_path, patients)

    # ---- providers ----
    providers_path = out_dir / "providers_raw.csv"
    _write_csv(
        providers_path,
        [
            {
                "id": pid,
                "full_name": name,
                "npi": npi,
                "specialty": specialty,
                "active": "true",
            }
            for (pid, name, npi, specialty) in PROVIDERS
        ],
    )

    # ---- appointments (5-10 per patient on average to clear small-N=5) ----
    appointments_path = out_dir / "appointments_raw.csv"
    appointments: list[dict[str, str]] = []
    appt_id = 0
    for p in patients:
        for _ in range(rng.randint(5, 10)):
            appt_id += 1
            appointments.append(
                {
                    "source_id": f"APT-{appt_id:07d}",
                    "patient_source_id": p["source_id"],
                    "provider_id": rng.choice(PROVIDERS)[0],
                    "appointment_date": _random_date_in_range(rng, 6),
                    "appointment_type_category": rng.choices(
                        APPT_TYPES, weights=[0.40, 0.15, 0.20, 0.10, 0.05, 0.05, 0.05]
                    )[0],
                    "status": rng.choices(APPT_STATUSES, weights=[0.10, 0.70, 0.08, 0.10, 0.02])[0],
                    "duration_minutes": str(rng.choice([15, 30, 45, 60, 90])),
                }
            )
    _write_csv(appointments_path, appointments)

    # ---- procedures (~40% of appointments) ----
    procedures_path = out_dir / "procedures_raw.csv"
    procedures: list[dict[str, str]] = []
    proc_id = 0
    for a in appointments:
        if rng.random() < 0.4:
            proc_id += 1
            procedures.append(
                {
                    "source_id": f"PROC-{proc_id:07d}",
                    "patient_source_id": a["patient_source_id"],
                    "provider_id": a["provider_id"],
                    "procedure_category": rng.choice(PROC_CATEGORIES),
                    "procedure_date": a["appointment_date"],
                    "revenue_amount": str(
                        rng.choice([85, 220, 450, 1500, 4800, 12800, 18500, 32000])
                    ),
                }
            )
    _write_csv(procedures_path, procedures)

    # ---- referrals (~5% of patient count) ----
    referrals_path = out_dir / "referrals_raw.csv"
    referrals: list[dict[str, str]] = []
    for i in range(max(5, patient_count // 20)):
        ref_patient = rng.choice(patients)
        referrals.append(
            {
                "source_id": f"REF-{i + 1:06d}",
                "referring_provider_id": f"ext-{rng.randint(1, 50)}",
                "referring_provider_name": rng.choice(REFERRING_NAMES),
                "referring_provider_practice": rng.choice(REFERRING_PRACTICES),
                "referred_patient_source_id": ref_patient["source_id"],
                "referral_date": _random_date_in_range(rng, 6),
                "converted_to_appointment": rng.choices(["true", "false"], weights=[0.7, 0.3])[0],
                "revenue_generated": str(rng.choice([0, 250, 1500, 4800, 12000])),
            }
        )
    _write_csv(referrals_path, referrals)

    # ---- invoices (~2 per patient) ----
    invoices_path = out_dir / "invoices_raw.csv"
    invoices: list[dict[str, str]] = []
    inv_id = 0
    for p in patients:
        for _ in range(rng.randint(1, 3)):
            inv_id += 1
            invoices.append(
                {
                    "source_id": f"INV-{inv_id:07d}",
                    "invoice_date": _random_date_in_range(rng, 6),
                    "amount": str(rng.choice([85, 220, 450, 1500, 4800])),
                    "payer_category": p["payer_category"],
                    "status": rng.choices(
                        ["paid", "pending", "overdue", "written_off"],
                        weights=[0.60, 0.20, 0.15, 0.05],
                    )[0],
                    "age_bucket": rng.choices(
                        ["current", "30-60", "60-90", "90+"],
                        weights=[0.55, 0.22, 0.12, 0.11],
                    )[0],
                }
            )
    _write_csv(invoices_path, invoices)

    print(
        f"Generated synthetic practice in {out_dir}:\n"
        f"  patients:     {len(patients):>6}\n"
        f"  providers:    {len(PROVIDERS):>6}\n"
        f"  appointments: {len(appointments):>6}\n"
        f"  procedures:   {len(procedures):>6}\n"
        f"  referrals:    {len(referrals):>6}\n"
        f"  invoices:     {len(invoices):>6}"
    )


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic raw practice data with PHI")
    parser.add_argument("--out", type=Path, required=True, help="Output directory for raw CSVs")
    parser.add_argument("--patients", type=int, default=500, help="Patient count (default 500)")
    parser.add_argument("--seed", type=int, default=0, help="RNG seed for reproducibility")
    args = parser.parse_args()
    generate(args.out, args.patients, seed=args.seed)


if __name__ == "__main__":
    main()
