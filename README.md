# Praxis practice-side de-identification tool

[![Tests](https://github.com/hurshpatel95-source/praxis-deid-tool/actions/workflows/test.yml/badge.svg)](.)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> ⚠️ **PRE-PRODUCTION.** This tool is published for source review and HIPAA-consultant validation. Not yet certified for production deployment.
>
> - 5 critical findings from the internal security audit are already fixed (see commit [`52f2afc`](https://github.com/hurshpatel95-source/praxis-deid-tool/commit/52f2afc) and [`SECURITY_AUDIT.md`](SECURITY_AUDIT.md)).
> - HIPAA consultant validation is scheduled before first customer deployment.
> - 143/143 pytest assertions pass on every commit (70 v0.1 core + 73 wizard); no known active leakage paths.
> - **MIT-licensed — use at your own risk.** If you deploy this against real PHI before the audit clears, you own the compliance posture.
>
> Issues + PRs welcome. For the privacy architecture this tool is part of, see [praxishealth.ai/security](https://praxis-app-production.up.railway.app/security).

A small, auditable Python tool that runs **at your practice, on your infrastructure**, and produces HIPAA Safe Harbor-compliant aggregates from your raw practice management data.

> Patient data never leaves your practice in identifiable form. Praxis cloud only ever sees this tool's output.

This is the architectural decision that makes Praxis genuinely different. We don't ask for raw PHI; we don't sign BAAs to handle it. The de-identification happens here, before anything crosses the wire.

## Why open source?

Three reasons:

1. **Your IT and compliance teams should read this code before running it.** Open source makes that easy.
2. **It's a credibility signal.** Anyone — your auditor, a PE sponsor, a HIPAA consultant — can see exactly what data we're collecting and what we're stripping out.
3. **Security through obscurity is a bad pattern.** De-identification logic deserves to be inspectable.

## What it does

Reads CSV exports from your PM system and produces six output CSV files — `patients`, `appointments`, `providers`, `procedures`, `referrals`, `invoices` — that have already had **HIPAA Safe Harbor §164.514(b)(2)** de-identification applied:

- Patient names, phones, emails, addresses, SSNs, MRNs: **removed**
- Patient birth dates: replaced with **age bands** (`18-30`, `31-45`, etc.)
- Patient ZIPs: truncated to **first 3 digits**, suppressed to `000` if HHS lists the prefix as <20,000 population
- Specific dates: replaced with **`YYYY-MM`** granularity. Appointment rows additionally carry a `day_of_week` field (`mon`/`tue`/.../`sun`) — a Safe Harbor-permitted category derived from the raw date *before* generalization, used by the cloud dashboard's no-show-by-day-of-week analytics
- Specific procedure codes: emitted as **category strings** (`knee_replacement`, `cleaning`, etc.). The tool passes through the `procedure_category` column from your source — pre-categorize upstream until vertical-specific default mappings ship.
- Per-record dollar amounts: bucketed into **revenue bands** (`$1000-5000`, etc.)
- Patient identifiers: replaced with **HMAC-SHA256 hash** of the source ID using a practice-held salt — stable across runs, irreversible without the salt
- **Small-N suppression**: any patient with fewer than 5 touchpoints (appointments + procedures) is dropped from output, along with their dependent rows

What's kept:
- **Provider names + NPI** (providers are not PHI subjects)
- **Referring provider names + practice** (same reason)

## Install

```bash
pip install praxis-deid               # not yet on PyPI; install from source
# or
pip install -e .                       # from a local clone
```

Requires Python 3.10+.

## Quick start

1. Copy [`examples/praxis-deid.example.yaml`](examples/praxis-deid.example.yaml) to `/etc/praxis-deid/config.yaml`.
2. Set `practice_id` to the UUID Praxis cloud issued to you.
3. Set `deidentification.patient_id_salt` to a long random string. **Save it somewhere safe** (1Password, a sealed envelope, your password manager) — losing it means losing your ability to recognize patients across runs.
4. Point the `source.*_file` paths at your PM's CSV exports.
5. Run:

   ```bash
   praxis-deid run --config /etc/praxis-deid/config.yaml
   ```

6. Six de-identified CSVs land in `output.directory`. Upload them to Praxis (SFTP / email attachment / `praxis-app` admin upload UI).

## Run with a UI

For practices that prefer a point-and-click interface over a YAML config + cron, the tool ships an optional **localhost-only web UI** that an IT admin can use without reading source.

Install the extra (pulls in FastAPI + uvicorn + jinja2 + python-multipart — all offline-capable, no CDNs or telemetry):

```bash
pip install praxis-deid[serve]
```

Start the server:

```bash
praxis-deid serve            # binds 127.0.0.1:8765, no browser auto-open
praxis-deid serve --open     # also opens the UI in your default browser
praxis-deid serve --port 9000
```

Then open http://127.0.0.1:8765/ and the page lets you:

1. **Configure** — practice ID, patient ID salt (with a "Generate" button that runs `crypto.getRandomValues` in-browser), small-N threshold, audit log path
2. **Pick the six input CSVs** from disk
3. **Click Run** — drives the same `Deidentifier` pipeline as the CLI
4. **Review the result** — output folder path + per-file row/byte counts + a pretty-printed audit envelope (with input file SHA-256 fingerprints) + a post-hoc PHI scan over each output CSV (SSN/email/phone/ZIP+4/day-resolution dates)
5. **Open the output folder** in Finder/Explorer with one click

### Safety guards

- **Default bind is `127.0.0.1`.** The UI accepts raw PM data uploads; binding non-loopback would expose those uploads on the LAN.
- **`--allow-remote` is required** to bind any non-loopback host (e.g. `0.0.0.0`, `192.168.x.y`). Doing so prints a loud warning to stderr — only use behind a trusted firewall.
- **No outbound network calls.** Templates and static assets are served from the installed package; nothing in the [serve] extra phones home.
- **The salt is never logged**, never echoed back over the wire, and never written to the audit envelope — same invariant as the CLI.

## Schedule it

Standard cron / Task Scheduler / systemd timer. Example for nightly 2am via cron:

```cron
0 2 * * * /usr/local/bin/praxis-deid run --config /etc/praxis-deid/config.yaml
```

## Source CSV column names

The tool expects these columns in your raw CSV exports. Most PM systems already produce something close — you may need a small staging script to rename columns. Order of columns is free.

| File | Required columns |
|---|---|
| `patients_raw.csv` | `source_id`, `dob`, `zip`, `gender`, `payer_category`, `patient_status`, `first_seen_date` |
| `appointments_raw.csv` | `source_id`, `patient_source_id`, `provider_id`, `appointment_date` (must include day, e.g. `2026-04-15` — needed to derive `day_of_week`), `appointment_type_category`, `status`, `duration_minutes` |
| `providers_raw.csv` | `id`, `full_name`, `npi`, `specialty`, `active` |
| `procedures_raw.csv` | `source_id`, `patient_source_id`, `provider_id`, `procedure_category`, `procedure_date`, `revenue_amount` |
| `referrals_raw.csv` | `source_id`, `referring_provider_id`, `referring_provider_name`, `referring_provider_practice`, `referred_patient_source_id`, `referral_date`, `converted_to_appointment` |
| `invoices_raw.csv` | `source_id`, `invoice_date`, `amount`, `payer_category`, `status`, `age_bucket` |

PHI columns from your source (names, phone, email, address, SSN, etc.) are simply ignored — they never enter the output.

## What the audit log captures

Every run appends a single JSON line to `audit.log_path`:

```json
{
  "timestamp": "2026-04-25T02:00:01.234567+00:00",
  "practice_id": "00000000-0000-0000-0000-0000000000a1",
  "stats": {
    "patients_in": 5247, "patients_out": 5103,
    "appointments_in": 18420, "appointments_out": 18420,
    "rows_dropped": 12,
    "drop_reasons": { "patient: invalid dob: ...": 8, ... },
    "small_n_suppressions": 144
  },
  "output": { "mode": "csv", "files": { ... } }
}
```

The salt is **never** logged.

## Wizard: setup for new PMS connections

Hand-coding a CSV adapter for every dental PMS doesn't scale. The
`praxis-deid wizard` subcommand uses the Anthropic API to propose
mappings from any source PMS schema to Praxis's canonical CSVs, then
runs the proposal through structural validation and a human-in-the-loop
approval flow before writing the practice's `mapping.json`.

**The bright HIPAA line:** the wizard reads SCHEMA METADATA ONLY — table
names, column names, types, foreign keys. It NEVER reads or transmits
row data. Two walls protect the API call:

1. The schema reader is built so it cannot return row data — JSON dumps
   are metadata-only by construction; SQL DDL parsers refuse files
   containing `INSERT`, `COPY`, or `LOAD DATA`; live SQLAlchemy
   reflection issues only `MetaData.reflect()` (information_schema), no
   `SELECT` against user tables.
2. **`PhiGuard`** inspects every payload immediately before it leaves
   the process. If the payload contains anything PHI-shaped (SSN,
   email, full date, phone, MRN, ZIP+4, or a forbidden field name like
   `rows`/`samples`/`data`), `PhiGuard` raises `PhiDetectedError` and
   the request is aborted. Defense in depth — both walls have to fail
   for PHI to leak.

The wizard maps to **6 canonical CSVs** that the de-id pipeline produces
post-mapping (Extensions A-F per `praxis-app/METRIC_COVERAGE_AUDIT.md`
§4.1):

| Extension | Canonical CSV | Unlocks |
|---|---|---|
| A | `treatment_plans_raw.csv` | 5 of 6 metrics in the Treatment Plan section |
| B | `claims_raw.csv` | 6 metrics across Insurance + Compliance |
| C | `schedule_capacity_raw.csv` | Production per chair, utilization, fill rate |
| D | `payments_raw.csv` | Collections rate, insurance vs OOP mix |
| E | `timekeeping_raw.csv` | Provider compensation %, per-hour productivity |
| F | `patients_raw.csv` columns | Recall + referral source extensions |

### Run it

```bash
# Install the wizard extra
pip install praxis-deid[wizard]

# Make sure your Anthropic API key is set
export ANTHROPIC_API_KEY=sk-ant-...

# Run against a JSON schema dump (preferred — review the dump offline first)
praxis-deid wizard run \
  --schema-file path/to/your_pms_schema.json \
  --pms open_dental \
  --output ~/.praxis-deid/mappings/open_dental.json

# Or against a DDL-only SQL dump (rejected if it contains INSERT/COPY)
praxis-deid wizard run \
  --sql-dump path/to/schema_only.sql \
  --pms dentrix \
  --output ~/.praxis-deid/mappings/dentrix.json

# See what canonical schemas the wizard knows about
praxis-deid wizard list-schemas
```

The flow:

1. Read the source PMS schema (metadata only).
2. Send schema metadata to Claude (after `PhiGuard` clears the payload).
3. Claude returns a `MappingConfig` per canonical schema, with per-column
   confidence scores. Columns it can't map confidently get
   `needs_review: true` and `confidence: 0.0` rather than a guess.
4. The validator runs structural checks: every required canonical
   column has a mapping, every source-table reference points at a
   table that exists in the source PMS, enum columns have transformations.
5. The CLI prompts the operator column-by-column to accept Claude's
   mapping, override it, or skip. The operator's edits get the final
   say — Claude's output is a starting point, not a decision.
6. The approved `mapping.json` is written to disk for the de-id
   pipeline to consume.

Cost: a single wizard run for one PMS uses roughly 8-12K input tokens
and 3-5K output tokens — well under $0.10 at current `claude-sonnet-4-5`
pricing.

### Validation gate (Open Dental ground truth)

The wizard's correctness is gated against an expert-audited Open Dental
ground-truth mapping before its output is trusted by the rest of the
de-id pipeline.

```bash
# Rebuild ground truth (only when corrections are added)
python3 scripts/build_ground_truth.py

# Run gate offline (uses recorded fixture; free, no network)
python3 scripts/validate_wizard.py --replay

# Run gate against live Anthropic API (~$0.07 per run)
python3 scripts/validate_wizard.py --live
```

The gate scores wizard output against `tests/fixtures/expected_mapping_open_dental.json`
on four axes:

| Axis | Threshold | Meaning |
|---|---|---|
| `table_agreement` | ≥95% | Each column references a source table that overlaps with ground truth |
| `high_confidence_exact_match` | =100% | Columns with ground-truth confidence ≥0.9 must match exactly |
| `needs_review_agreement` | ≥80% | Wizard's needs_review flag agrees with ground truth |
| `critical_errors` | =0 | No known wrong patterns (e.g. `claimproc.Status=2` interpreted as Received — it's actually Preauth) |

Exit code 0 = GO; exit code 1 = NO-GO (human review required); exit code
2 = wizard itself errored.

## Extractors (Phase-C: Extensions A–F)

The `run` command above consumes CSV exports the practice has already produced from their PMS. The `extract` subcommand is the other side of that workflow: it reads **directly from the PMS database** (default: Open Dental MySQL/MariaDB) and emits the six **Phase-C canonical CSVs** that drive the cloud dashboard's Wave 1 + Wave 2 tiles.

```bash
praxis-deid extract --extension <A|B|C|D|E|F|all>
                    --connection <db-url>            # OR --fixture-json <path>
                    --output <dir>
                    --practice-id <uuid>
                    --salt-env-var PRAXIS_DEID_SALT
                    [--mapping-dir mappings/open_dental]
                    [--since YYYY-MM] [--until YYYY-MM] [--limit N]
                    [--audit-log <path>]
```

The six supported extensions:

| Letter | Canonical CSV                  | Source tables (Open Dental)                       |
|--------|--------------------------------|----------------------------------------------------|
| A      | `treatment_plans_raw.csv`      | `treatplan` LEFT JOIN `proctp`                     |
| B      | `claims_raw.csv`               | `claim` LEFT JOIN `insplan` LEFT JOIN `carrier`    |
| C      | `schedule_capacity_raw.csv`    | `schedule` LEFT JOIN `scheduleop` (UNION ALL of provider-grain + chair-grain) |
| D      | `payments_raw.csv`             | UNION ALL of `paysplit` + `claimpayment` + `adjustment` |
| E      | `timekeeping_raw.csv`          | `schedule` LEFT JOIN `provider`                    |
| F      | `patients_extension.csv`       | `patient` + `recall` (last_visit, recall_due, referral_source) |

**Key safety properties (verified by the test suite):**

- **No raw SQL injection.** Mapping configs supply column expressions; the loader scans every expression for forbidden patterns (`;`, `--`, `/*`, DDL keywords like `DROP`/`TRUNCATE`/`DELETE`) and rejects the config at load time.
- **Per-record dollars are always banded.** Every amount flows through `safe_harbor.amount_to_band` before the canonical row is built. A regex-based dollar-leak scanner runs over every produced CSV.
- **Cross-extension HMAC stability.** A single `Deidentifier` instance (== a single salt) is shared across every extractor in a run, so a `patient_source_id` HMACs to the **same** `external_id` in every CSV (verified by `test_cross_extension_hmac_stability`).
- **Locked v0.1 modules untouched.** `safe_harbor.py`, `deidentify.py`, `hashing.py`, `schema.py`, `audit.py`, `config.py` remain frozen; the extractors compose them through the public API.

**Dry run with synthetic rows (no DB needed):**

```bash
export PRAXIS_DEID_SALT=$(openssl rand -hex 32)
praxis-deid extract --extension all \
                    --fixture-json tests/fixtures/phase_c_fixture.json \
                    --output /tmp/extract-test/ \
                    --practice-id "$PRAXIS_PRACTICE_ID"
```

This produces all six canonical CSVs without ever opening a DB connection — useful for CI, audits, and local development.

The hand-curated mapping configs live in `mappings/open_dental/`. See [`mappings/open_dental/README.md`](mappings/open_dental/README.md) for editing rules.

### Live-DB connectors

By default `praxis-deid extract` reads from a JSON fixture file. For live-DB operation against a practice's PMS, install the connector extra and pass `--connection`:

```bash
# Open Dental (MySQL / MariaDB)
pip install praxis-deid[mysql]
praxis-deid extract \
  --connection "mysql+mysqlconnector://praxis_ro:pwd@dentalsrv:3306/opendental" \
  --extension all --output /tmp/output \
  --practice-id "$PRAXIS_PRACTICE_ID"

# Dentrix (MS SQL Server) — requires ODBC Driver 17 or 18
pip install praxis-deid[mssql]
praxis-deid extract \
  --connection "mssql+pyodbc://praxis_ro:pwd@dentrixsrv/DTXNAME?driver=ODBC+Driver+17+for+SQL+Server" \
  --extension all --output /tmp/output \
  --practice-id "$PRAXIS_PRACTICE_ID"

# Any PostgreSQL-based PMS
pip install praxis-deid[postgres]
praxis-deid extract \
  --connection "postgresql+pg8000://praxis_ro:pwd@pmsserver:5432/pmsdb" \
  --extension all --output /tmp/output \
  --practice-id "$PRAXIS_PRACTICE_ID"

# All three connector extras at once
pip install praxis-deid[all-connectors]
```

The URL scheme drives connector dispatch:

| URL scheme prefix                              | Connector              | PMS dialect |
|------------------------------------------------|------------------------|-------------|
| `mysql+mysqlconnector://...`                   | `MysqlConnector`       | `mysql`     |
| `mssql+pyodbc://...`                           | `MssqlConnector`       | `mssql`     |
| `postgresql://...` (or `+psycopg2`, `+pg8000`) | `PostgresConnector`    | `postgres`  |
| `fixture-json://./path/to/file.json`           | `JsonFixtureConnector` | `fixture`   |

The legacy `--fixture-json <path>` flag still works — it's normalised into a `fixture-json://...` URL behind the scenes, and routes through the same `JsonFixtureConnector` as the explicit URL form.

**Read-only credentials are mandatory.** The tool only ever issues `SELECT` queries; create a dedicated read-only DB user before deployment. The MSSQL connector additionally issues `SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED` at connect time to minimise lock contention against the live Dentrix install.

**ODBC setup on the practice's machine (Dentrix path):**

- **macOS dev**: `brew install unixodbc freetds && pip install praxis-deid[mssql]`
- **Linux prod**: `apt install unixodbc freetds-dev tdsodbc && pip install praxis-deid[mssql]`
- **Windows**: download Microsoft ODBC Driver 17 or 18 from [learn.microsoft.com](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server)

**Security posture of the connector layer:**

- All queries use SQLAlchemy parameterized binding (`text()` + `:bind_param`). Table and column names are validated against the per-table `INFORMATION_SCHEMA` allowlist before any query is built — a mapping config referencing a non-existent column fails at query-build time, not at the DB.
- WHERE clauses (when used) are scanned for `;`, `--`, `/*`, `*/`, and DDL/DML keywords (`DROP`, `TRUNCATE`, etc.) and rejected if any match. The same scanner runs against the mapping config at load time (Phase-C invariant), so the defence is end-to-end.
- The full connection URL (which contains the password) is **never** logged. The audit log records `pms_dialect` and `connection_redacted` (password scrubbed) only.
- Connectors are explicit-lifecycle: `connect()` opens, `close()` releases. `__enter__` / `__exit__` are provided so `with connector_for_url(url) as c: ...` works.

## Running the tests

```bash
pip install -e ".[dev]"
pytest
```

Tests cover the four spec invariants:

1. No PHI fields appear in output
2. Patient external IDs are stable across runs
3. Patient external IDs are non-reversible without the salt
4. Date granularity is month-level only
5. Small-N suppression drops single-touchpoint patients

## HIPAA consultant validation

Before deploying in production, the spec calls for a HIPAA consultant (Compliancy Group, Accountable HQ, or similar) to review this implementation once. The code is designed to be straightforward to review — single repo, no surprises, every transform tested.

## License

MIT. See [LICENSE](LICENSE).
