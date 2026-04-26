# Praxis practice-side de-identification tool

[![Tests](https://github.com/hurshpatel95-source/praxis-deid-tool/actions/workflows/test.yml/badge.svg)](.)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> ⚠️ **PRE-PRODUCTION.** This tool is published for source review and HIPAA-consultant validation. Not yet certified for production deployment.
>
> - 5 critical findings from the internal security audit are already fixed (see commit [`52f2afc`](https://github.com/hurshpatel95-source/praxis-deid-tool/commit/52f2afc) and [`SECURITY_AUDIT.md`](SECURITY_AUDIT.md)).
> - HIPAA consultant validation is scheduled before first customer deployment.
> - 63/63 pytest assertions pass on every commit; no known active leakage paths.
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
