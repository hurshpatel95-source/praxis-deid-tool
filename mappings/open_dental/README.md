# Open Dental mapping configs

Six hand-curated JSON files describing how to extract Phase-C canonical CSVs (Extensions A-F) from an Open Dental MySQL/MariaDB database.

| File                                | Canonical schema           | Source tables                                       |
|-------------------------------------|-----------------------------|-----------------------------------------------------|
| `A_treatment_plans_raw.json`        | `treatment_plans_raw`       | `treatplan` LEFT JOIN `proctp`                      |
| `B_claims_raw.json`                 | `claims_raw`                | `claim` LEFT JOIN `insplan` LEFT JOIN `carrier`     |
| `C_schedule_capacity_raw.json`      | `schedule_capacity_raw`     | `schedule` LEFT JOIN `scheduleop`                   |
| `D_payments_raw.json`               | `payments_raw`              | UNION ALL of `paysplit` + `claimpayment` + `adjustment` |
| `E_timekeeping_raw.json`            | `timekeeping_raw`           | `schedule` LEFT JOIN `provider`                     |
| `F_patients_raw_extension.json`     | `patients_raw_extension`    | `patient` + `recall`                                |

## What these files are for

Each JSON file is the Phase-C extractor's source of truth for **how to read one canonical CSV out of Open Dental**. The companion file `_bundle.json` is the full pre-split bundle used by the wizard validation gate — do not edit it directly; regenerate via `scripts/build_mapping_bundle.py` if any of the per-extension files change.

The audited pre-split mapping was hand-curated against the Open Dental documentation (see `_meta.audited_against` in each file). The wizard's auto-generated mappings were ~78% accurate (per the Wizard-1 validation gate, commit `6b19ae9`); the hand-curated ground-truth version corrects the systematic errors the wizard makes (e.g. confusing `claimproc.Status=2` (Preauth) with `claimproc.Status=1` (Received/paid) for the `payment_date` derivation).

## When to edit a mapping file

Hand-edit one of these files if and only if:

1. **The Open Dental schema has changed** (e.g. a new release renamed a column). Cite the Open Dental release notes URL in `_meta.audited_against`.
2. **A practice-specific column lookup is needed** (e.g. a custom carrier-name -> payer-category mapping). For Extension B, supply this via a separate `payer_lookup.json` rather than editing the main config — the `ClaimsExtractor` accepts a `--payer-lookup` path.
3. **A column was previously marked `needs_review: true` and you've confirmed the audited interpretation** with the practice. Update `confidence` and `needs_review` accordingly, and add a note in the file's `notes` array describing the audit.

**Hand-edits MUST cite the Open Dental manual page they are based on.** The CI / human-approval flow checks for `_meta.audited_against` populated with at least one URL.

## Forbidden edits

The mapping config loader (`praxis_deid/extractors/base.py::load_mapping_config`) will REJECT a file that contains any of:

* Semicolons (`;`)
* SQL comment markers (`--`, `/*`, `*/`)
* DDL/DML keywords (`DROP`, `TRUNCATE`, `DELETE`, `UPDATE`, `INSERT`, `ALTER`, `GRANT`, `REVOKE`, `CREATE`, `REPLACE`, `EXEC`, `EXECUTE`, `CALL`, `MERGE`, `ATTACH`, `DETACH`)
* Join types other than `LEFT`, `INNER`, `RIGHT`, `FULL` (no `CROSS`)
* Column mappings for keys that are not columns of the canonical schema
* Missing required canonical columns

These guards are non-negotiable; they keep a hand-edited or maliciously substituted mapping file from triggering SQL injection or sneaking raw PHI into the output. Rejection happens at load time, before any DB activity.

## Validating against canonical schemas

Every mapping config is validated against the canonical schema defined in `praxis_deid/wizard/canonical_schemas.py`. The required columns for each extension are listed in that file's `_EXTENSION_A` ... `_EXTENSION_F` constants. To verify a mapping file conforms:

```bash
python3 -c "
from praxis_deid.extractors import load_mapping_config
cfg = load_mapping_config('mappings/open_dental/A_treatment_plans_raw.json')
print('OK:', cfg.canonical_schema_name, '-', sorted(cfg.column_mappings.keys()))
"
```

If the file is invalid, `ExtractorError` will be raised with a clear message identifying the offending column, keyword, or missing required field. The full test suite covers every type of invalid edit:

```bash
pytest tests/test_extractor_base.py -k "test_load_mapping_rejects" -v
```

## Open Dental documentation references

The audited mappings cite these manual pages:

* https://www.opendental.com/manual/treatmentplanstatus.html (TPStatus enum: `0=Active`, `1=Inactive`)
* https://www.opendental.com/manual/claimstatus.html (ClaimStatus single-char enum: `U=Unsent`, `W=Waiting`, `P=Probable`, `S=Sent`, `R=Received`, `H=Hold`, `I=Sent-verified`, `A=Adjustment`)
* https://www.opendental.com/manual/claimprocstatus.html (claimproc.Status: `1=Received/paid`, `2=Preauth`)
* https://www.opendental.com/manual/appointmentstatus.html (AptStatus: `2=Complete`)

These are the authority for any future hand-edit.
