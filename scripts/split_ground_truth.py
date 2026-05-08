"""Split the audited Open Dental ground-truth mapping bundle into one
file per canonical schema.

The full bundle in `tests/fixtures/expected_mapping_open_dental.json`
contains all 6 schemas. For Wave 2 work, each extension's de-id
extractor + cloud-side surface is owned by its own agent — and each
agent only needs ITS slice of the mapping. Splitting the bundle here
locks the canonical Open Dental contract into 6 stable, reviewable
artifacts.

Output layout:
    mappings/open_dental/
      A_treatment_plans_raw.json
      B_claims_raw.json
      C_schedule_capacity_raw.json
      D_payments_raw.json
      E_timekeeping_raw.json
      F_patients_raw_extension.json
      _bundle.json    # the full multi-schema bundle for tooling that wants it

Each per-schema file has the same shape as one element of the bundle's
`mappings` array, plus a `_meta` block carrying the audit pointers.

Usage:
    python3 scripts/split_ground_truth.py
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "tests/fixtures/expected_mapping_open_dental.json"
OUT_DIR = REPO / "mappings/open_dental"

# Extension-letter → canonical-schema name mapping.
EXTENSION_BY_SCHEMA = {
    "treatment_plans_raw": "A",
    "claims_raw": "B",
    "schedule_capacity_raw": "C",
    "payments_raw": "D",
    "timekeeping_raw": "E",
    "patients_raw_extension": "F",
}


def main() -> None:
    if not SOURCE.exists():
        raise SystemExit(f"missing source: {SOURCE}")
    bundle = json.loads(SOURCE.read_text())
    meta = bundle.get("_meta") or {}
    mappings = bundle.get("mappings") or []
    if not mappings:
        raise SystemExit("source bundle has no mappings array")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    written = 0
    for mapping in mappings:
        schema_name = mapping["canonical_schema"]
        ext_letter = EXTENSION_BY_SCHEMA.get(schema_name)
        if ext_letter is None:
            print(f"WARN: no extension letter for schema {schema_name}; skipping")
            continue
        per_schema = {
            "_meta": {
                "extension_letter": ext_letter,
                "canonical_schema": schema_name,
                "pms": "open_dental",
                "audit_source": meta.get("source_fixture"),
                "audited_against": meta.get("audited_against"),
                "purpose": (
                    f"Extension {ext_letter} hand-curated Open Dental mapping. "
                    f"Consumed by praxis_deid/extractors/{ext_letter.lower()}_*.py "
                    f"to produce {schema_name}.csv at de-id time. "
                    "Derived from expected_mapping_open_dental.json + expert audit. "
                    "DO NOT regenerate from the wizard — the wizard's output is "
                    "78% reliable per the Wizard-1 validation gate (commit 6b19ae9). "
                    "Hand-edits to this file MUST cite the Open Dental manual page "
                    "they are based on."
                ),
            },
            **mapping,
        }
        filename = f"{ext_letter}_{schema_name}.json"
        out_path = OUT_DIR / filename
        out_path.write_text(json.dumps(per_schema, indent=2) + "\n")
        print(f"  wrote {out_path.relative_to(REPO)}")
        written += 1

    bundle_out = OUT_DIR / "_bundle.json"
    bundle_out.write_text(json.dumps(bundle, indent=2) + "\n")
    print(f"  wrote {bundle_out.relative_to(REPO)} (full bundle, all 6 schemas)")
    print(f"split {written} mappings + 1 bundle")


if __name__ == "__main__":
    main()
