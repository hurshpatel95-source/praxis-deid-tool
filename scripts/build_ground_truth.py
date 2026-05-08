"""Build the Wizard-1 validation ground truth for Open Dental.

Reads the recorded Anthropic response fixture (which the Wizard-1 author
seeded with broadly correct Open Dental expertise) and applies a small
set of expert-audited corrections to produce
`tests/fixtures/expected_mapping_open_dental.json` — the diff target the
validation harness uses when running the wizard live.

This script is the audit trail. Every correction below has a comment
citing the Open Dental documentation it relies on. Re-run any time the
corrections change.

Usage:
    python3 scripts/build_ground_truth.py
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "tests/fixtures/anthropic_responses/open_dental_treatment_plans.json"
DEST = REPO / "tests/fixtures/expected_mapping_open_dental.json"


# -- Corrections registry --------------------------------------------------
# Each correction is keyed by (canonical_schema, column_name) and either
# overrides specific fields on the column mapping or annotates the schema's
# top-level notes. Source-citation comments live next to the correction so
# the audit trail is one-stop.
#
# Open Dental enum references in this file come from the public manual:
#   - https://www.opendental.com/manual/claimprocstatus.html
#   - https://www.opendental.com/manual/claimstatus.html
#   - https://www.opendental.com/manual/treatmentplanstatus.html
#   - https://www.opendental.com/manual/appointmentstatus.html

COLUMN_CORRECTIONS: dict[tuple[str, str], dict] = {
    # === claims_raw.payment_date ===
    # FIXTURE BUG: uses claimproc.Status = 2 to mean "Received/paid".
    # In Open Dental's claimproc.Status enum, Status = 2 is **Preauth**,
    # not Received. Status = 1 is the Received (paid) value. This is a
    # genuine error that would mis-mark every claim's payment date if
    # left in the wizard's output — the diff harness MUST fail here when
    # live Claude returns the same wrong value.
    ("claims_raw", "payment_date"): {
        "source_expression": (
            "(SELECT MAX(claimproc.DateCP) FROM claimproc "
            "WHERE claimproc.ClaimNum = claim.ClaimNum AND claimproc.Status = 1)"
        ),
        "confidence": 0.85,
        "notes": (
            "Latest claimproc.DateCP for Status=1 (Received/paid). "
            "NULL if no procs paid. Per Open Dental docs, Status=1 is the "
            "canonical 'Received' code; Status=2 is Preauth (pre-authorization, "
            "not a payment) and is a common source of analyst error."
        ),
    },
    # === claims_raw.status ===
    # The fixture's CASE handles 'R'/'U'/'W'/'S' but omits the rarer
    # statuses 'H' (Hold), 'I' (Sent verified), and 'A' (Adjustment).
    # Audited expansion handles all documented values and routes
    # 'H'/'I' to 'pending' and 'A' to 'paid' (because in Open Dental
    # an Adjustment row presupposes the original claim was processed).
    ("claims_raw", "status"): {
        "source_expression": (
            "CASE WHEN claim.ClaimStatus = 'R' THEN 'paid' "
            "WHEN claim.ClaimStatus = 'A' THEN 'paid' "
            "WHEN claim.ClaimStatus = 'S' THEN 'submitted' "
            "WHEN claim.ClaimStatus = 'I' THEN 'pending' "
            "WHEN claim.ClaimStatus = 'H' THEN 'pending' "
            "WHEN claim.ClaimStatus IN ('U','W') THEN 'pending' "
            "ELSE 'pending' END"
        ),
        "confidence": 0.7,
        "notes": (
            "ClaimStatus is a 1-char enum: U=Unsent, W=Waiting, P=Probable, "
            "S=Sent, R=Received, H=Hold, I=Sent-verified, A=Adjustment. "
            "Open Dental does not differentiate paid-in-full from partial "
            "in this field — 'partial' must be derived from amount comparison."
        ),
    },
    # === treatment_plans_raw.status ===
    # The fixture maps treatplan.TPStatus = 1 to 'expired'. In Open Dental,
    # TPStatus = 1 is **Inactive** — the practice manually deactivated the
    # plan, which can mean expired, declined, or simply superseded. Mapping
    # to 'expired' is a stretch; better to flag for human review.
    ("treatment_plans_raw", "status"): {
        "source_expression": (
            "CASE WHEN treatplan.DateTSigned IS NOT NULL THEN 'accepted' "
            "WHEN treatplan.TPStatus = 1 THEN 'declined' "
            "ELSE 'presented' END"
        ),
        "confidence": 0.5,
        "needs_review": True,
        "notes": (
            "TPStatus enum: 0=Active, 1=Inactive (treated here as 'declined' "
            "because Open Dental practices most commonly mark plans Inactive "
            "after a documented patient refusal — but it can also mean "
            "expired or superseded; operator MUST confirm). DateTSigned "
            "presence is the most reliable accepted-signal."
        ),
    },
}


# Top-level schema notes to APPEND (not replace) on specific schemas.
SCHEMA_NOTE_APPENDS: dict[str, list[str]] = {
    "claims_raw": [
        "GROUND TRUTH AUDIT: payment_date corrected from claimproc.Status=2 "
        "to claimproc.Status=1 (the canonical Received code per Open Dental "
        "docs). Status=2 is Preauth.",
        "GROUND TRUTH AUDIT: status CASE expanded to cover H/I/A enum values.",
    ],
    "treatment_plans_raw": [
        "GROUND TRUTH AUDIT: TPStatus=1 mapped to 'declined' rather than "
        "'expired' (per Open Dental docs, TPStatus=1 is Inactive — the "
        "practice manually deactivated; usually post-refusal).",
    ],
}


def main() -> None:
    if not SOURCE.exists():
        raise SystemExit(f"missing source fixture: {SOURCE}")
    src = json.loads(SOURCE.read_text())
    parsed = src.get("parsed") or {}
    mappings = deepcopy(parsed.get("mappings") or [])

    if not mappings:
        raise SystemExit("source fixture has no parsed.mappings")

    corrections_applied = 0
    for mapping in mappings:
        schema = mapping["canonical_schema"]
        for col_name, col in mapping["column_mappings"].items():
            key = (schema, col_name)
            if key in COLUMN_CORRECTIONS:
                col.update(COLUMN_CORRECTIONS[key])
                corrections_applied += 1
        if schema in SCHEMA_NOTE_APPENDS:
            mapping.setdefault("notes", []).extend(SCHEMA_NOTE_APPENDS[schema])

    out = {
        "_meta": {
            "purpose": (
                "Wizard-1 validation ground truth: what an expert Open Dental "
                "schema mapping should look like. Diff target for "
                "scripts/validate_wizard.py."
            ),
            "source_fixture": str(SOURCE.relative_to(REPO)),
            "corrections_applied": corrections_applied,
            "audited_against": [
                "https://www.opendental.com/manual/claimprocstatus.html",
                "https://www.opendental.com/manual/claimstatus.html",
                "https://www.opendental.com/manual/treatmentplanstatus.html",
                "https://www.opendental.com/manual/appointmentstatus.html",
            ],
        },
        "mappings": mappings,
    }

    DEST.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {DEST.relative_to(REPO)}")
    print(f"applied {corrections_applied} column corrections "
          f"and {sum(len(v) for v in SCHEMA_NOTE_APPENDS.values())} schema notes")


if __name__ == "__main__":
    main()
