# Praxis de-id tool — security & claims audit

Audit date: 2026-04-25.
Auditor: independent agent commissioned for HIPAA pre-consultant readiness.
Scope: `/Users/hurshpatel/Documents/praxis/praxis-deid-tool/` v0.1.0 against the public claims at `/Users/hurshpatel/Documents/praxis/praxis-app/app/security/page.tsx` and `README.md`.
Method: source review of every file in `praxis_deid/`, `tests/`, `examples/`, `LICENSE`; cross-reference of the cloud-side `lib/canonical/*.ts` and `lib/adapters/csv-upload.ts`; live execution of the test suite and additional unscripted assertions (HMAC re-derivation, ZIP-3 set comparison, fuzz scan of fixture-driven output for SSN/phone/email/name patterns, audit-log content scan for salt/source-id leakage, edge-case probes).
Constraint honored: zero source files modified in either repository. The only file written is this report.

---

## Executive summary

**Verdict: CLAIMS UPHELD WITH GAPS.**

The de-identification logic is sound and the test suite — 39/39 passing locally on Python 3.15 — exercises the load-bearing invariants the public page asserts. HMAC-SHA256 is real (independently re-derived), the salt is genuinely held by the practice and never logged or returned to the cloud, the 17 HHS-restricted ZIP-3 prefixes are present and correct, dates are reduced to `YYYY-MM` everywhere they leave the tool, and a fuzz scan of the fixture-driven output found zero SSN/phone/email/patient-name leakage. The cloud's TypeScript canonical schema and the Python output dataclasses match field-for-field, so ingestion will not silently drop columns.

Where it falls short of the **public** claim (not the technical reality):

1. **NULL DOB silently buckets patients into "0-17"** (`deidentify.py:139` — `age = _age_from_dob(...) if raw.get("dob") else 0`). A patient whose DOB is missing in the source becomes a pediatric record. The public page promises age banding; this is age fabrication. **Must fix before HIPAA consultant.**
2. **Future-dated DOB returns a negative age** which then clamps to "0-17" (`safe_harbor.py:33` — `if age < 0: age = 0`). Same outcome as #1, same severity.
3. **The "39 De-id (Safe Harbor) assertions" tile on `/security` counts every `pytest` test — including small-N, end-to-end, and validator tests — not 39 Safe Harbor §164.514(b)(2) primitive tests.** The number is honest as a pytest count, but the label "(Safe Harbor)" overstates what's actually verified at the §164.514 level (the `test_safe_harbor.py` file alone contributes 19). Reword the tile or split the suites.
4. **Audit log captures stats but not data lineage.** A HIPAA reviewer asking "exactly which file did the practice export on day X, and what was its hash?" cannot answer it from the current log. The log is good for "did it run, how many rows" but not for forensic reconstruction.
5. **No schema validation library on the YAML config.** Hand-rolled `_require_str` / `_require_mapping` works, but pydantic would catch the next config-shape regression for free and is already in the Python toolchain. Low severity, easy win.

Everything else on the public page is honest. The architecture is what they say it is.

---

## Per-claim validation table

| # | Claim (verbatim from `/security` or README) | Evidence | Verdict |
|---|---|---|---|
| 1 | "HIPAA Safe Harbor — all 18 categories handled at practice" | `safe_harbor.py:1-129` covers ages, ZIP-3 with HHS list, dates→month, dollar→band, duration→band; `schema.py:177-187` `FORBIDDEN_FIELDS` enumerates names/SSN/MRN/email/phone/address/IP/device/biometric — checked at every emitted dataclass; `deidentify.py` ignores all PHI source columns by simply not reading them. The claim of "18 categories" is rhetorical — Safe Harbor enumerates 18, the tool's output schema makes most structurally inexpressible. | **CONFIRMED** |
| 2 | "Names, phone, email, address — Removed entirely" | `schema.py:44-63` `Patient` dataclass has zero PHI fields; `deidentify.py:140-149` constructs Patient using only `source_id` (hashed), `dob` (banded), `zip` (truncated), `gender`, `payer_category`, `patient_status`, `first_seen_date` (monthed). PHI source columns (`first_name`, `last_name`, `phone`, `email`, `address`) are never referenced. Live fuzz scan against `tests/fixtures/patients_raw.csv` (which contains real-shaped PHI) confirms zero SSN/phone/email/name regex hits in any output CSV. | **CONFIRMED** |
| 3 | "SSN, MRN, account numbers — Removed entirely" | Same mechanism as #2; the source `ssn` column in `patients_raw.csv` is not read. `MRN` (`source_id`) is HMAC-hashed before output. Account numbers were never in the input contract. `FORBIDDEN_FIELDS` in `schema.py:177-187` includes `ssn`, `social_security_number`, `mrn`, `medical_record_number`, `account_number`. | **CONFIRMED** |
| 4 | "Birth dates / exact age — age band (e.g. 31-45). 90+ collapsed to 76+" | `safe_harbor.py:31-46` `age_to_band`. Bands: 0-17, 18-30, 31-45, 46-60, 61-75, 76+. **More aggressive than Safe Harbor required** (Safe Harbor only mandates collapse at 90+; collapsing at 76 reduces residual identifiability — strictly safer). The public page words this correctly: "90+ collapsed to 76+" — the audit prompt suggested this might be wrong but it isn't; the tool's bucket boundary at 76 absorbs the 90+ requirement. Independently verified: `age_to_band(89)=='76+'`, `age_to_band(90)=='76+'`, `age_to_band(110)=='76+'`. | **CONFIRMED** |
| 5 | "ZIP truncated to first 3 digits. Restricted prefixes (population <20,000) suppressed to '000'" | `safe_harbor.py:19-24` lists exactly 17 prefixes: `036, 059, 063, 102, 203, 556, 692, 790, 821, 823, 830, 831, 878, 879, 884, 890, 893`. **Independently compared against the audit prompt's reference list — exact match.** `zip_to_prefix()` (`safe_harbor.py:49-63`) truncates and suppresses; tested in `tests/test_safe_harbor.py:58-61` against the full set. Empty / non-numeric / <3-digit input is also suppressed to `000`. | **CONFIRMED** |
| 6 | "Specific dates — Generalized to YYYY-MM. Day-of-week is not derivable" | `safe_harbor.py:66-81` `date_to_month` returns `YYYY-MM`. Cloud schema (`primitives.ts:110-113`) enforces `^\d{4}-(0[1-9]\|1[0-2])$` with Zod — any rogue day-level value would be **rejected by ingestion**, not silently stored. Every output dataclass field carrying a date is a `*_month` (e.g. `appointment_date_month`, `procedure_date_month`, `referral_date_month`, `invoice_date_month`, `first_seen_month`). Grep across the entire repo: the only `strftime("%Y-%m-%d")` is in `scripts/generate_synthetic.py:71` (synthetic input generator — never crosses to cloud output). The `%Y-%m-%d` strings in `deidentify.py:77` are PARSE format strings for the input DOB, not output formats. | **CONFIRMED** |
| 7 | "Specific procedure codes — Mapped to category strings per vertical" | `deidentify.py:333-339` `_categorize_procedure` accepts a `procedure_categorization` dict and falls back to passthrough. **WEAKNESS**: the CLI never wires the YAML's `procedure_categorization: default` field into anything (`cli.py:52-56` constructs `Deidentifier` without passing it). So unless the practice's PM already emits categorized labels, raw category strings flow through. The README documents this on lines 22-28 ("`knee_replacement`, `cleaning`") as if mapping is automatic. The fixture data already uses category strings, masking the issue. | **WEAK** — the mechanism exists but is unwired in the CLI; a "default" mapping was promised by config schema and never materialized. |
| 8 | "Per-record dollar amounts — Bucketed into bands" | `safe_harbor.py:96-111` `amount_to_band` with 7 bands. Used at `deidentify.py:206` (procedure revenue), `deidentify.py:228` (referral revenue), `deidentify.py:243` (invoice amount). Negative values clamp to `$0-100`. Cloud schema enforces enum (`primitives.ts:84-94`). | **CONFIRMED** |
| 9 | "Patient identifiers — HMAC-SHA256(salt, source_id) truncated to 16 hex chars. Stable across runs, irreversible without the salt" | `hashing.py:29-38`: `hmac.new(salt.encode(), str(source_id).encode(), hashlib.sha256).hexdigest()[:16]`. Independently re-derived — output matches Python `hmac` stdlib exactly: `stable_external_id('mysalt','MRN-1234') == '254fe655d1697631'` and matches `hmac.new(b'mysalt', b'MRN-1234', hashlib.sha256).hexdigest()[:16]`. Truncation is on the **hex string** (cryptographically safe — every nibble of SHA-256 is uniformly distributed). NOT raw-bytes truncation, NOT Python's `hash()` builtin (which is randomized). Stability verified by `test_hashing.py:8-12` and live re-run. Salt-dependence verified by `test_hashing.py:14-17` and live re-run. | **CONFIRMED** |
| 10 | "Provider names + NPI — Kept (not PHI subjects)" | `schema.py:88-103` `Provider.full_name` and `Provider.npi` are output fields; `deidentify.py:179-187` passes `full_name` through verbatim. NPI is normalized: only kept if exactly 10 digits, else `None`. **Note**: any non-NPI value the source emits as `npi` (e.g. internal provider number) is silently dropped — this is correct under HIPAA but worth flagging in the README so practices don't think their internal IDs survived. | **CONFIRMED** with note. |
| 11 | "Append-only local audit log: every run records what was processed and what crossed the wire" | `audit.py:21-38` opens with `"a"` (append). `cli.py:99-119` writes one JSON line per run with `patients_in/out`, `appointments_in/out`, `procedures_in/out`, `rows_dropped`, `drop_reasons`, `small_n_suppressions`, output mode + file paths. File mode chmod'd to `0o640`. **GAP**: no SHA-256 of the input file, no input file path, no row-checksum, no operator identity. A reviewer asking "what exactly did you export on 2026-04-15" sees counts and output paths but cannot prove which input was processed. | **WEAK** — append-only and counts are real; "what crossed the wire" is honored as aggregate counts and output filenames, but not as a forensic fingerprint of the dataset. |
| 12 | "Salt that drives patient ID hashing is held by the practice; Praxis never sees it" | `config.py:117` reads salt from local YAML; `cli.py:54` passes it to the `Deidentifier` constructor in-process; `deidentify.py:121` stores it as `self._salt` (single underscore — convention only, not enforcement); `audit.py:24` documents "must NOT contain raw source identifiers, the salt, or any PHI" and the `cli.py` audit-record construction does not include the salt; `upload.py:72-79` the `post_to_api` path is a stub (`NotImplementedError`) — currently the only output path is local CSV files. **Live verification**: ran the tool against fixtures with salt `"fuzz-salt"`, scanned the resulting `audit.log` — string `"fuzz-salt"` does not appear; string `"MRN-001"` (a source ID) does not appear. The output CSVs contain only HMAC outputs, not the salt or sources. | **CONFIRMED** |
| 13 | "Stable IDs, non-reversibility, no PHI in output, small-N suppression, schema conformance — all tested" | `test_hashing.py` (8 tests) covers stable + salt-dependence + non-reversibility smoke + length + collision-resistance + empty-salt rejection. `test_safe_harbor.py` (19 tests) covers ages, ZIPs, dates, amounts, durations. `test_deidentify.py` (9 tests) covers no-PHI-in-output + stable + salt-dependent + month-only + small-N (3 cases) + invalid-row drop + ZIP suppression. `test_end_to_end.py` (3 tests) covers CLI round-trip + no-PHI-in-CSV + audit log written and salt-free. **Schema conformance**: every output dataclass has a `validate()` method (`schema.py`) called inline at construction time; the cloud's Zod schemas (`lib/canonical/*.ts`) accept the exact same field names + enum sets — independently diffed. | **CONFIRMED** |
| 14 | "39 De-id (Safe Harbor) assertions" tile on `/security` | `pytest tests/ -q` → `39 passed in 0.28s`. Counted: `test_deidentify.py=9`, `test_end_to_end.py=3`, `test_hashing.py=8`, `test_safe_harbor.py=19` → **9+3+8+19 = 39**. Number is exact. Label is slightly misleading — only 19 tests are §164.514(b)(2) primitive tests; the rest are integration/hashing/E2E. Total count honest, taxonomy slightly aggressive. | **CONFIRMED on count, OVERSTATED on category** — actual: 19 primitive Safe Harbor tests + 20 supporting tests, sold as "39 (Safe Harbor)". |
| 15 | "MIT license" | `LICENSE` file present, MIT text, copyright "2026 Praxis". `pyproject.toml:7` `license = { text = "MIT" }`. README badge `License: MIT`. | **CONFIRMED** |

---

## Deep findings (15 audit dimensions)

### 1. Salt hygiene
- Loaded from YAML config (`config.py:117`) — not env var, not keyring. **Footgun**: any practice that backs up `/etc/praxis-deid/config.yaml` to a non-encrypted location ships the salt. README:51-52 instructs practices to save the salt in 1Password or a sealed envelope, but the config file itself is not encouraged to be encrypted at rest. **Recommend**: support `${ENV_VAR}` interpolation OR a `patient_id_salt_file` pointer to a separately-permissioned file.
- Salt is stored in `Deidentifier._salt` (single underscore — Python convention only, not language enforcement).
- Salt is NOT in `__repr__` of any dataclass (none of the dataclasses contain it).
- Salt does not appear in `cli.py`'s `print()` outputs (lines 121-125 — only counts).
- Salt does not appear in any exception message: `_age_from_dob` raises `ValueError(f"unparseable dob: {dob_str!r}")` — DOB only; `hashing.py:32` raises `ValueError("salt must be a non-empty string")` — generic; no `f"...{salt}..."` strings exist anywhere.
- Salt is not logged: `logging` module not imported; `traceback` not imported. The only `print()` calls are version output (`cli.py:40`) and the run summary (`cli.py:121-125`). Live audit-log scan with a known salt confirms zero leakage.
- **Verdict**: **CLEAN**, with a recommendation to support env-var loading.

### 2. HMAC implementation
- `hashing.py:33-37` uses `hmac.new(salt.encode("utf-8"), str(source_id).encode("utf-8"), hashlib.sha256).hexdigest()`.
- Real HMAC (not naive `sha256(salt + source)` which is vulnerable to length-extension on some constructions).
- Truncation is `[:16]` on the **hex string** — 16 hex chars = 64 bits of entropy, and every hex nibble of SHA-256 is uniformly distributed, so truncation does not bias the output. Independently re-derived.
- Python's `hash()` builtin is **NOT** used. (`grep -n "hash(" praxis_deid/` returns only `hashlib` imports.)
- `int(out, 16)` succeeds on the output (`test_hashing.py:32`) — confirms hex.
- **Verdict**: **CRYPTOGRAPHICALLY CORRECT** for the stated threat model.

### 3. Day-of-week leakage
- Every output date field is named `*_month` and goes through `date_to_month()` which returns `YYYY-MM`.
- Cloud `monthString` (`primitives.ts:110-113`) regex-rejects anything that isn't `YYYY-MM` — so even if a future code change in this tool emitted a day, **ingestion would fail loudly**.
- Grep of the entire `praxis_deid/` package for `strftime` returns zero matches. Grep for `%Y-%m-%d` returns one match: `deidentify.py:77`, which is a **parse** format for incoming DOB strings, not an output format. The synthetic generator (`scripts/generate_synthetic.py:71`) uses `%Y-%m-%d` to write input fixtures — also fine, that data never appears as output.
- **Verdict**: **NO LEAKAGE**.

### 4. ZIP-3 + restricted prefixes
- 17 prefixes hardcoded as a `frozenset[str]` in `safe_harbor.py:19-24`.
- Independently checked against the audit prompt's reference list (`036, 059, 063, 102, 203, 556, 692, 790, 821, 823, 830, 831, 878, 879, 884, 890, 893`) — **exact match, both directions**.
- `zip_to_prefix("")` → `"000"`, `zip_to_prefix(None)` → `"000"`, `zip_to_prefix("12")` → `"000"`, `zip_to_prefix("ab")` → `"000"` (`test_safe_harbor.py:63-67`).
- ZIP+4 input handled (`zip_to_prefix("08201-1234") == "082"`, `test_safe_harbor.py:55-56`).
- **Maintenance risk**: HHS publishes an updated list when Census refreshes. There is no `# Last reviewed: YYYY-MM-DD` comment, no test asserting the list version. Recommend a comment + a CI assertion that the file's hash matches a known SHA when the list is reviewed.
- **Verdict**: **CORRECT TODAY**, brittle to HHS revisions.

### 5. Age 90+ collapse
- The audit prompt suggested the public claim might be wrong ("76+ vs 90+"). It is not wrong — the tool collapses at 76, which **subsumes** the Safe Harbor 90+ requirement. Bands: `0-17, 18-30, 31-45, 46-60, 61-75, 76+`. A 76-year-old, an 89-year-old, and a 110-year-old all become `76+`. Strictly safer than the regulation requires.
- Independently confirmed: `age_to_band(75)=='61-75'`, `age_to_band(76)=='76+'`, `age_to_band(89)=='76+'`, `age_to_band(90)=='76+'`, `age_to_band(110)=='76+'`. (`tests/test_safe_harbor.py:33-38` covers this.)
- **Verdict**: **CORRECT, MORE CONSERVATIVE THAN SAFE HARBOR**.

### 6. Small-N suppression
- Implemented at the practice tool, not the cloud (`deidentify.py:256-324`). Default threshold = 5 (live verified — `Deidentifier('id','salt').small_n_threshold == 5`).
- Patients with **fewer than 5 touchpoints** (appointments + procedures combined) are dropped, AND their dependent appointment/procedure/referral rows are dropped (`deidentify.py:309-314`).
- Patients with **zero touches** ride a separate stratum check (`deidentify.py:288-299`): they only survive if their (age_band, zip_prefix, payer_category) triple has ≥5 patients in the same stratum. Otherwise dropped. **Conservative, correct.**
- Suppression count surfaced in audit (`stats.small_n_suppressions`).
- **Cloud aggregator**: not audited here; the public page says "small-N cells suppressed" — the practice tool already prevents the problematic rows from leaving, so the cloud claim is not misleading.
- **Verdict**: **STRONG**.

### 7. No-PHI-in-output assertion
- `tests/test_deidentify.py:23-51` asserts `out_fields & FORBIDDEN_FIELDS` is empty AND that stringifying the dataclass does not contain any of: `alice`, `smith`, `123-45-6789`, `609-555-1212`, `alice@example.com`, `123 main st`. This IS a "fuzzy" PHI scan.
- `tests/test_end_to_end.py:61-79` repeats the scan against the rendered CSV files for all six entities, with name list `[Alice, Smith, Bob, Jones, Carlos, Williams]`.
- **Audit-added live fuzz**: ran the CLI against `tests/fixtures/*_raw.csv` and scanned every output CSV with regex SSN (`\b\d{3}-\d{2}-\d{4}\b`), phone (`\b\d{3}-\d{3}-\d{4}\b`), email (RFC-ish), and the patient-name list — **zero hits in `patients.csv`, `appointments.csv`, `procedures.csv`, `invoices.csv`, `referrals.csv`**. Provider names appear in `providers.csv` by design.
- **Gap**: no `hypothesis`-based property test, despite `hypothesis>=6.0` being a declared dev dependency (`pyproject.toml:26`). Adding a property test that fuzzes input PHI shapes through the pipeline would harden #7 substantially.
- **Verdict**: **STRONG TODAY, MISSING PROPERTY-BASED FUZZ**.

### 8. HMAC stability across runs
- `tests/test_hashing.py:8-12` asserts deterministic with same salt+source.
- `tests/test_deidentify.py:56-63` asserts stable across two `Deidentifier` instances.
- `tests/test_deidentify.py:66-73` asserts different salt → different IDs.
- **Live re-verification**: ran the same `(salt, source_id)` pair through a fresh process — got `c39fa56c17574a1f` both times.
- **Salt-rotation test**: not present. Recommend a test asserting that after a salt change, the new external_ids are completely disjoint from the old set (over a sample of 100). Today this is implied by `test_salt_dependence` but not verified at scale.
- **Verdict**: **STABILITY CONFIRMED, salt-rotation test missing.**

### 9. Audit log content
- Written to the path in YAML `audit.log_path` (`audit.py:32`).
- Mode `0o640` chmod (`audit.py:36-38`) — owner rw, group r, world none. Best-effort (silent OSError if unsupported).
- **Per-line content** (sample from live run):
  ```json
  {"timestamp":"2026-04-25T05:03:50.142679+00:00","practice_id":"00000000-0000-0000-0000-0000000000a1","stats":{"patients_in":6,"patients_out":6,"appointments_in":8,"appointments_out":8,"providers_out":2,"procedures_in":4,"procedures_out":4,"referrals_out":0,"invoices_out":0,"rows_dropped":0,"drop_reasons":{},"small_n_suppressions":0},"output":{"mode":"csv","files":{"patients":"...","appointments":"...","providers":"...","procedures":"...","referrals":"...","invoices":"..."}}}
  ```
- **What's there**: timestamp, practice_id, in/out counts per entity, drop reasons (aggregated by reason string, no row identifiers), output paths.
- **What's missing**: input file paths, input file SHA-256 (forensic chain-of-custody), tool version, operator identity (uid/username), Python version, host identifier. **A HIPAA reviewer asking "prove what you exported on 2026-04-15" can answer "I exported these 6 files at this path with these counts" but not "the input that produced this had checksum X and was at path Y."**
- **What's correctly absent**: salt, source IDs, PHI of any kind. Live scan confirms.
- **Verdict**: **HONEST, AGGREGATE-ONLY** — augment with input file digests + tool version before consultant.

### 10. Config validation
- Hand-rolled `_require_str`, `_require_mapping`, `_optional_str`, `_optional_path` (`config.py:138-163`).
- `yaml.safe_load` (`config.py:81`) is correct (not `yaml.load`, which executes Python).
- Schema invariants enforced: practice_id non-empty; source.type=='csv'; output.type in `{csv, api}`; output.directory required when csv; output.api_endpoint+api_key required when api; small_n_threshold>=1.
- **Missing checks**: salt minimum length is documented as `>=32 chars recommended` (example.yaml:33) but not enforced. A practice that sets `patient_id_salt: "x"` will hash a single character. `if not salt` rejects empty but accepts `"a"`. Recommend `len(salt) >= 16` minimum + warn at <32.
- No pydantic / dataclasses-json. The hand-rolled validator is fine for v0.1; pydantic would be lighter to extend and produce better error messages. Low severity.
- **Verdict**: **ADEQUATE, MISSING SALT-LENGTH GUARD**.

### 11. Output format guarantees
- Independent diff of Python output dataclass column order (`schema.py`) vs. cloud Zod schemas (`lib/canonical/*.ts`):

| Entity | Python (deid) | TS (cloud) | Match |
|---|---|---|---|
| Patient | external_id, age_band, zip_prefix, gender, payer_category, patient_status, first_seen_month | external_id, age_band, zip_prefix, gender, payer_category, patient_status, first_seen_month (+ practice_id injected) | ✅ |
| Appointment | external_id, patient_external_id, provider_id, appointment_date_month, appointment_type_category, status, duration_minutes_band | same | ✅ |
| Provider | id, full_name, npi, specialty, active | same | ✅ |
| Procedure | external_id, patient_external_id, provider_id, procedure_category, procedure_date_month, revenue_band | same | ✅ |
| Referral | external_id, referring_provider_id, referring_provider_name, referring_provider_practice, referred_patient_external_id, referral_date_month, converted_to_appointment, revenue_generated_band | same | ✅ |
| Invoice | external_id, invoice_date_month, amount_band, payer_category, status, age_bucket | same | ✅ |

- `practice_id` correctly stripped at write time (`upload.py:54, 61`) — cloud injects it from adapter config.
- Booleans normalized to lowercase `"true"`/`"false"` (`upload.py:64-65`) to match the TS reader's `coerceBool` (`csv-upload.ts:139-142`).
- `None` → `""` (`upload.py:66-67`) — fine for the TS adapter's `r.field ?? ''` path.
- **Edge case**: empty result set writes a 0-byte file with no header (`upload.py:48-51`). The TS `parseCsv` returns `[]` for empty input — safe.
- **Verdict**: **CONFORMANT** end-to-end.

### 12. Provider data
- `deidentify.py:179-187`: NPI is normalized to keep only valid 10-digit values; non-NPI strings drop to `None`. Other provider fields (`id`, `full_name`, `specialty`, `active`) pass through verbatim.
- **Risk**: practice-specific provider IDs (e.g., `"PROV-00042-MAIN-OFFICE"`) flow through unmodified to the cloud. These are not PHI under HIPAA Safe Harbor (provider is not the data subject) but **could** identify the practice. Already known to Praxis (practice_id is the pivot), so not a leak — but worth mentioning in README.
- **Risk**: a source that misuses the `full_name` column to store a patient's name (data quality bug at the practice) would push a patient name into the cloud. The tool trusts the practice to populate `full_name` correctly. Hard to defend against; recommend a runtime warn if `full_name` matches `dob`/`ssn`-bearing rows in patients_raw.
- **Verdict**: **MEETS CLAIM**, with one ambient data-quality risk that belongs in the consultant memo.

### 13. Edge cases (live-tested)

| Input | Behavior | Concern |
|---|---|---|
| Patient with NULL DOB | `age = 0` → `age_band = "0-17"`. Patient is **kept**, classified as pediatric. | **HIGH** — silent miscategorization. Should drop the row to `drop_reasons` instead. |
| Patient with future DOB (`2099-01-01`) | `age = -73` → `age_to_band` clamps to 0 → `age_band = "0-17"`. | **HIGH** — same outcome as NULL DOB; data-quality bug becomes pediatric record. |
| Patient with unparseable DOB (`"gibberish"`) | `_age_from_dob` raises `ValueError`; caught at `deidentify.py:153-154`; row dropped, `drop_reasons["patient: unparseable dob: 'gibberish'"]++`. | **OK** — explicit drop with reason. |
| Phone `"555-1212"` (non-standard) | Source phone is never read. | OK. |
| Multi-line address with newlines | Source address is never read. | OK. |
| Provider full_name with apostrophe + unicode + embedded newline (`"Dr. Aisha O'Brien-Sánchez\nMD"`) | Passed through verbatim. The TS CSV parser does NOT handle embedded newlines in quoted fields (`csv-upload.ts:79-80` — explicitly documented). **Round-trip would corrupt** if provider names contain newlines. | **MEDIUM** — practical risk if PM exports allow embedded newlines. |
| Negative dollar amount `-500.00` | Clamps to `$0-100`. | OK — documented in `safe_harbor.py:97-98`. |
| Provider with `id="p1"` (length 1) | Accepted (`schema.py:98` requires `>=1`). Cloud accepts (`provider.ts:9` requires `>=1`). | OK — internally consistent. |
| Empty CSV | `iter_csv_rows` yields nothing; `_write` writes a 0-byte file; cloud parser returns empty array. | OK. |
| Duplicate `source_id` in patients_raw | Two `Patient` rows with the same `external_id` are emitted; cloud will detect on insert. | OK — surfaced downstream, not silently merged. |

**Top edge-case concerns**: NULL/future DOB (#1+#2 below) and embedded newlines in provider names.

### 14. Test coverage
- Live run: `pytest tests/ -q --tb=no` → `39 passed in 0.28s`. Number is exact.
- Breakdown:
  - `test_hashing.py`: 8 tests — deterministic, salt-dep, source-dep, int/str equivalence, length+hex, empty-salt rejection, 10k-no-collision smoke, brute-force resistance smoke.
  - `test_safe_harbor.py`: 19 tests across 5 classes — age boundaries (incl. elderly collapse + negative), ZIP standard/+4/restricted/short/non-digit, dates ISO/datetime/year-month/unparseable, amounts boundaries/negative/all-bands-reachable, durations.
  - `test_deidentify.py`: 9 tests — FORBIDDEN_FIELDS + substring scan, stable-across-runs, salt-dependent, month-only, small-N drop-lone, small-N keep-at-threshold, dependent-row-drop, invalid-row-drop, restricted-zip suppression.
  - `test_end_to_end.py`: 3 tests — CLI round-trip, no-PHI-in-CSV, audit log written + salt-free.
- **Gaps**:
  - No salt-rotation test (audit dimension #8).
  - No `hypothesis` property-based fuzz test, despite `hypothesis>=6.0` being a declared dev dep.
  - No NULL-DOB explicit test (would have caught the `_age_from_dob` silent-pediatric bug).
  - No future-DOB test.
  - No referral with `revenue_generated` test (the field is read at `deidentify.py:218` but no fixture exercises it).
  - No invoice-fixture E2E test (fixtures dir has 4 files; invoices not exercised at the CLI level).
- **Verdict**: **39 honest tests; coverage of negative cases is thin in 4 named places.**

### 15. Deployability
- `requires-python = ">=3.10"` (`pyproject.toml:6`).
- Dependencies: `pyyaml>=6.0`, `requests>=2.31`. Both pure-Python on most platforms; no compiled wheels needed; works on Linux/macOS/Windows.
- No system libraries assumed.
- `audit.py:36 os.chmod(..., 0o640)` is wrapped in try/except OSError — **Windows-friendly** (chmod silently no-ops). Good.
- File paths: `Path(...).expanduser()` (`config.py:125, 163`) — handles `~/`. Good.
- README mentions cron / Task Scheduler / systemd — no tooling assumes a Unix shell.
- Cloud-PMS exception (Dentrix Ascend hosted by Henry Schein cloud): **HANDOFF.md:186 acknowledges this** — "Practice-side Python may not deploy on cloud-PMS DSOs ... need a 'managed de-id relay' mode." The de-id tool itself is silent on this; the public `/security` page should add a one-liner caveat.
- **Verdict**: **DEPLOYABLE on standard practice infra**; cloud-PMS exception known to Praxis but not surfaced in tool README.

---

## Top 5 must-fix-before-HIPAA-consultant

1. **NULL DOB silently becomes pediatric.** `deidentify.py:139` → `age = 0` when `dob` is missing → `age_to_band(0) == "0-17"`. The patient SURVIVES output as a pediatric record. **Fix**: drop the row to `drop_reasons["patient: missing dob"]` instead. Add a test.
2. **Future-dated DOB silently becomes pediatric.** Same mechanism: `_age_from_dob("2099-01-01")` returns `-73`, then `age_to_band(-73)` clamps to `0-17`. **Fix**: when computed age is negative, raise `ValueError("future dob: ...")` and drop the row. Add a test.
3. **No salt-length guard.** A practice config with `patient_id_salt: "x"` is accepted by `_require_str`. The example file recommends ≥32 but nothing enforces ≥16. **Fix**: enforce `len(salt) >= 16` in `config.py:117-120` and warn at `<32`. Add a test.
4. **Audit log is aggregate-only — no input fingerprint.** A reviewer asking "prove what you exported" cannot reconstruct the input from the log. **Fix**: capture input file path + SHA-256 + byte size + tool version + Python version per source file, alongside the existing counts.
5. **`procedure_categorization: default` config field is unwired.** `cli.py:52-56` constructs `Deidentifier(...)` without passing `procedure_categorization`, so the YAML field is read by `config.py` then ignored. The README at lines 28 + 34-36 implies a default mapping exists. **Fix**: either remove the YAML field + README mention, or wire a real default mapping per vertical.

## Top 5 should-fix-before-publishing-to-public-GitHub

1. **README's referrals_raw column list is wrong.** Line 79 omits `revenue_generated`, but `deidentify.py:218` reads it. Either add `revenue_generated` to the README table or change the code to ignore the field. (Today it's silently optional — works either way, doc is misleading.)
2. **Add a `hypothesis` property-based test that fuzzes raw input dicts** with PHI-shaped values (random emails, SSNs, names) and asserts that no fuzzed input ever appears in any output dataclass field. The dev dep is already declared (`pyproject.toml:26`); the test would be ~30 lines. This is the kind of test that wins HIPAA-consultant respect on a public repo.
3. **Add a `# Last reviewed: YYYY-MM-DD` comment** above `RESTRICTED_ZIP3_PREFIXES` (`safe_harbor.py:19`) and a CI test that fails after a year without an explicit re-review. HHS revises the list with Census; brittle today.
4. **Document the cloud-PMS deployment exception** in the README (the Dentrix Ascend / Henry Schein situation from HANDOFF.md:186). A practice on a hosted PMS that reads "runs on your infrastructure" will get burned. One sentence.
5. **Mention salt rotation operationally**, not just at line 7-11 of the example YAML. README should have a "Salt rotation" section: "rotating the salt invalidates all prior external_ids. Coordinate with Praxis support before rotating in production — analytics continuity will break for one cycle."

---

## Tested in this audit

| Assertion | How | Result |
|---|---|---|
| `pytest tests/ -q` | Live in repo | **39 passed in 0.28s** |
| HMAC implementation matches `hmac.new(salt, src, sha256).hexdigest()[:16]` | Re-derived in Python | **Match** (`254fe655d1697631`) |
| 17 restricted ZIP-3 prefixes match HHS list | Set equality vs prompt's reference | **Exact match** |
| Age 89 / 90 / 110 → `76+` | Live `age_to_band` | **All three → `76+`** |
| Date `2026-04-15` → `2026-04` | Live `date_to_month` | **`2026-04`** |
| Future DOB `2099-01-01` → age | Live `_age_from_dob` | **`-73`** (silently clamps to `0-17` band — finding) |
| NULL DOB patient survives output | Live full pipeline | **Survives as `0-17`** (finding) |
| Negative dollar `-500` → band | Live `amount_to_band` | **`$0-100`** (documented) |
| Default `small_n_threshold` | Construct `Deidentifier` with no override | **5** (matches public claim) |
| Stable across runs (same salt, same source) | Two fresh processes | **Identical** |
| Different salts → different IDs | Two `Deidentifier` instances | **Different** |
| Output CSVs scanned with SSN regex `\b\d{3}-\d{2}-\d{4}\b` | Run pipeline over fixture; scan all output | **0 matches** |
| Output CSVs scanned with phone regex | Same | **0 matches** |
| Output CSVs scanned with email regex | Same | **0 matches** |
| Output CSVs scanned for fixture patient names | Same | **0 matches** in patients/appointments/procedures/referrals/invoices; provider names appear in providers.csv by design |
| Audit log contains salt | grep `"fuzz-salt"` | **Absent** |
| Audit log contains source MRN | grep `"MRN-001"` | **Absent** |
| Output schema field-by-field match with cloud Zod | Manual diff | **6/6 entities exact** |
| Provider name with unicode + apostrophe + newline | Live add_provider | **Preserved verbatim** (round-trip caveat: TS parser does not handle embedded newlines) |
| LICENSE file present, MIT, 2026 Praxis | Read | **Confirmed** |
| Python version compatibility floor | `pyproject.toml` | **>=3.10** (tests passed on 3.15 in this audit) |

---

## Closing note

The de-identification logic is solid, the cryptography is correct, the schema conformance with the cloud is exact, and the test count on the public page is honest. The five must-fix items are bugs of omission, not architectural failures — the tool has the right shape, it just lets a few quiet data-quality issues through that an attentive HIPAA consultant will absolutely notice. None of them are show-stoppers. All five are <50 lines of code each.

The biggest credibility lift before public GitHub: add a `hypothesis` property-based PHI fuzz test. It's the single most differentiating thing a small open-source de-id tool can ship, and the dev dependency is already declared.
