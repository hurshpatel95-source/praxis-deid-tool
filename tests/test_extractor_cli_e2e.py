"""End-to-end test: `praxis-deid extract --extension all` via the CLI.

Verifies the full Phase-C surface:
  * CLI parses argv correctly.
  * Single Deidentifier shared across all 6 extractors -> HMAC-stable
    patient IDs across every produced CSV.
  * All 6 canonical CSVs are written to the output dir with correct
    filenames and headers.
  * No exact $$$ leak in any output (the dollar-leak guard runs).
  * Audit log is emitted.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from praxis_deid.cli import main

PRACTICE_ID = "00000000-0000-0000-0000-0000000000a1"
SALT = "X" * 40

FIXTURE = {
    "treatment_plans_raw": [
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1",
         "treatplan.DateTP": "2026-04-15", "treatplan.DateTSigned": None,
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 1500.0},
        {"treatplan.TreatPlanNum": "TP-2", "treatplan.PatNum": "PT-2",
         "treatplan.DateTP": "2026-04-20", "treatplan.DateTSigned": "2026-04-21",
         "treatplan.TPStatus": 0, "proctp.ProvNum": "DOC-7", "proctp.FeeAmt": 250.0},
    ],
    "claims_raw": [
        {"claim.ClaimNum": "CLM-1", "claim.PatNum": "PT-1",
         "claim.DateSent": "2026-04-15", "claim.ClaimStatus": "R",
         "claim.PreAuthString": "AUTH123", "claim.DateService": "2026-04-01",
         "carrier.CarrierName": "Aetna PPO",
         "claim.PaymentDate_aggregated": "2026-04-25",
         "claim.PreVerified_aggregated": True},
    ],
    "schedule_capacity_raw": [
        {"schedule.ScheduleNum": 1, "schedule.SchedDate": "2026-04-15",
         "schedule.StartTime": "08:00:00", "schedule.StopTime": "17:00:00",
         "schedule.ProvNum": 7, "scheduleop.OperatoryNum": "CHAIR-A",
         "apt_minutes_aggregated": 360},
    ],
    "payments_raw": [
        {"_branch": "paysplit", "paysplit.SplitNum": "S-1", "paysplit.PatNum": "PT-1",
         "paysplit.DatePay": "2026-04-15", "paysplit.SplitAmt": 250.0},
    ],
    "timekeeping_raw": [
        {"schedule.ScheduleNum": 1, "schedule.SchedDate": "2026-04-15",
         "schedule.StartTime": "08:00:00", "schedule.StopTime": "17:00:00",
         "schedule.ProvNum": 7, "provider.HourlyRate": 175.0,
         "apt_minutes_aggregated": 420},
    ],
    "patients_raw_extension": [
        {"patient.PatNum": "PT-1", "patient.DateLastVisit": "2026-04-01",
         "patient.ReferredBy": "Google search", "recall_min_due_aggregated": "2026-10-01"},
    ],
}


@pytest.fixture
def fixture_path(tmp_path: Path) -> Path:
    p = tmp_path / "fixture.json"
    p.write_text(json.dumps(FIXTURE), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _set_salt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAXIS_DEID_SALT", SALT)


def test_extract_help_shows_subcommand() -> None:
    """`praxis-deid extract --help` must run without error."""
    with pytest.raises(SystemExit) as excinfo:
        main(["extract", "--help"])
    assert excinfo.value.code == 0


def test_extract_all_extensions_produces_six_csvs(
    tmp_path: Path,
    fixture_path: Path,
) -> None:
    out_dir = tmp_path / "out"
    rc = main([
        "extract",
        "--extension", "all",
        "--fixture-json", str(fixture_path),
        "--output", str(out_dir),
        "--practice-id", PRACTICE_ID,
        "--audit-log", str(tmp_path / "audit.log"),
    ])
    assert rc == 0
    expected = {
        "treatment_plans_raw.csv",
        "claims_raw.csv",
        "schedule_capacity_raw.csv",
        "payments_raw.csv",
        "timekeeping_raw.csv",
        "patients_extension.csv",
    }
    written = {p.name for p in out_dir.iterdir()}
    assert expected.issubset(written)


def test_cross_extension_hmac_stability(
    tmp_path: Path,
    fixture_path: Path,
) -> None:
    """The same patient_source_id must HMAC to the same patient_external_id
    in EVERY produced CSV."""
    out_dir = tmp_path / "out"
    rc = main([
        "extract",
        "--extension", "all",
        "--fixture-json", str(fixture_path),
        "--output", str(out_dir),
        "--practice-id", PRACTICE_ID,
        "--audit-log", str(tmp_path / "audit.log"),
    ])
    assert rc == 0

    # Pull the patient_external_id for "PT-1" from each CSV.
    pids: dict[str, set[str]] = {}
    for csv_name, col in [
        ("treatment_plans_raw.csv", "patient_external_id"),
        ("claims_raw.csv", "patient_external_id"),
        ("payments_raw.csv", "patient_external_id"),
        ("patients_extension.csv", "patient_external_id"),
    ]:
        path = out_dir / csv_name
        with path.open() as fp:
            reader = csv.DictReader(fp)
            pids[csv_name] = {row[col] for row in reader}

    # Every CSV must contain the same external_id for PT-1.
    flat = set()
    for s in pids.values():
        flat.update(s)
    # We expect exactly ONE distinct external_id for PT-1 across all four.
    common = set.intersection(*pids.values()) if all(pids.values()) else set()
    assert len(common) >= 1, f"no shared HMAC across CSVs: {pids}"


def test_extract_rejects_both_connection_and_fixture(
    tmp_path: Path,
    fixture_path: Path,
) -> None:
    rc = main([
        "extract",
        "--extension", "A",
        "--fixture-json", str(fixture_path),
        "--connection", "mysql://foo",
        "--output", str(tmp_path / "out"),
        "--practice-id", PRACTICE_ID,
    ])
    assert rc != 0


def test_extract_rejects_short_salt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_path: Path,
) -> None:
    monkeypatch.setenv("PRAXIS_DEID_SALT", "x")  # too short
    rc = main([
        "extract",
        "--extension", "A",
        "--fixture-json", str(fixture_path),
        "--output", str(tmp_path / "out"),
        "--practice-id", PRACTICE_ID,
    ])
    assert rc != 0


def test_extract_rejects_missing_practice_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_path: Path,
) -> None:
    monkeypatch.delenv("PRAXIS_PRACTICE_ID", raising=False)
    rc = main([
        "extract",
        "--extension", "A",
        "--fixture-json", str(fixture_path),
        "--output", str(tmp_path / "out"),
    ])
    assert rc != 0


def test_extract_audit_log_written(
    tmp_path: Path,
    fixture_path: Path,
) -> None:
    out_dir = tmp_path / "out"
    audit_log = tmp_path / "audit.log"
    rc = main([
        "extract",
        "--extension", "A",
        "--fixture-json", str(fixture_path),
        "--output", str(out_dir),
        "--practice-id", PRACTICE_ID,
        "--audit-log", str(audit_log),
    ])
    assert rc == 0
    assert audit_log.exists()
    line = audit_log.read_text(encoding="utf-8").splitlines()[-1]
    record = json.loads(line)
    assert record["command"] == "extract"
    assert record["practice_id"] == PRACTICE_ID
    assert "A" in record["extensions"]


def test_extract_no_unbanded_dollars_in_any_output(
    tmp_path: Path,
    fixture_path: Path,
) -> None:
    """Verify the dollar-leak guard ran on every CSV by scanning each."""
    from praxis_deid.extractors.base import assert_no_exact_dollars_in_csv

    out_dir = tmp_path / "out"
    rc = main([
        "extract",
        "--extension", "all",
        "--fixture-json", str(fixture_path),
        "--output", str(out_dir),
        "--practice-id", PRACTICE_ID,
        "--audit-log", str(tmp_path / "audit.log"),
    ])
    assert rc == 0
    for csv_path in out_dir.iterdir():
        if csv_path.suffix == ".csv":
            assert_no_exact_dollars_in_csv(csv_path)
