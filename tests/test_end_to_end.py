"""End-to-end CLI test against a fixture dataset."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
import yaml

from praxis_deid.cli import main
from praxis_deid.schema import FORBIDDEN_FIELDS

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    return tmp_path


def _write_config(workdir: Path) -> Path:
    cfg = {
        "practice_id": "00000000-0000-0000-0000-0000000000a1",
        "source": {
            "type": "csv",
            "patients_file": str(FIXTURES / "patients_raw.csv"),
            "appointments_file": str(FIXTURES / "appointments_raw.csv"),
            "providers_file": str(FIXTURES / "providers_raw.csv"),
            "procedures_file": str(FIXTURES / "procedures_raw.csv"),
        },
        "output": {
            "type": "csv",
            "directory": str(workdir / "out"),
        },
        "deidentification": {
            # >= 32 chars — enforced by config.MIN_SALT_LENGTH.
            "patient_id_salt": "test-fixture-salt-padded-to-32-chars",
            "small_n_threshold": 1,  # Allow tiny fixture to round-trip.
        },
        "audit": {
            "log_path": str(workdir / "audit.log"),
        },
    }
    cfg_path = workdir / "config.yaml"
    cfg_path.write_text(yaml.dump(cfg), encoding="utf-8")
    return cfg_path


def test_cli_run_round_trip(workdir: Path) -> None:
    cfg = _write_config(workdir)
    rc = main(["run", "--config", str(cfg)])
    assert rc == 0

    out = workdir / "out"
    assert (out / "patients.csv").exists()
    assert (out / "appointments.csv").exists()
    assert (out / "providers.csv").exists()
    assert (out / "procedures.csv").exists()


def test_output_contains_no_phi(workdir: Path) -> None:
    cfg = _write_config(workdir)
    main(["run", "--config", str(cfg)])
    out = workdir / "out"

    # 1. Output filenames don't include any forbidden columns.
    for csv_file in out.glob("*.csv"):
        if csv_file.stat().st_size == 0:
            continue
        with csv_file.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames is not None, csv_file
            leaked = set(reader.fieldnames) & FORBIDDEN_FIELDS
            assert not leaked, f"{csv_file.name}: forbidden columns {leaked}"

            # 2. Cell-level: no patient names from the fixture leaked.
            blob = "\n".join(",".join(r.values()) for r in reader)
            for tip in ["Alice", "Smith", "Bob", "Jones", "Carlos", "Williams"]:
                assert tip not in blob, f"{csv_file.name}: {tip!r} leaked"


def test_audit_log_written(workdir: Path) -> None:
    cfg = _write_config(workdir)
    main(["run", "--config", str(cfg)])
    audit = workdir / "audit.log"
    assert audit.exists()
    line = audit.read_text(encoding="utf-8").strip()
    assert "patients_out" in line
    assert "small_n_suppressions" in line
    # Salt must NEVER appear in the audit log.
    assert "test-fixture-salt-padded-to-32-chars" not in line


def test_audit_log_includes_input_file_fingerprints(workdir: Path) -> None:
    """SECURITY_AUDIT.md #4: a HIPAA reviewer must be able to reconstruct
    exactly which input file was processed. Each ingested role gets a
    sha256 + path + byte_count entry, plus a tool_version envelope."""
    import hashlib
    import json as _json

    cfg = _write_config(workdir)
    main(["run", "--config", str(cfg)])

    record = _json.loads((workdir / "audit.log").read_text(encoding="utf-8").strip())

    # tool_version is captured.
    assert "tool_version" in record
    assert record["tool_version"]  # non-empty

    # Every ingested role has a forensic fingerprint.
    assert "input_files" in record
    files = record["input_files"]
    for role in ("patients", "appointments", "providers", "procedures"):
        assert role in files, f"missing fingerprint for {role}"
        entry = files[role]
        assert set(entry) == {"path", "sha256", "byte_count"}
        assert entry["byte_count"] > 0
        assert len(entry["sha256"]) == 64  # SHA-256 hex

    # The recorded hash matches a fresh hash of the source file.
    expected = hashlib.sha256(
        (FIXTURES / "patients_raw.csv").read_bytes()
    ).hexdigest()
    assert files["patients"]["sha256"] == expected

    # The salt is still absent — adding fingerprints must not have leaked PHI.
    raw = (workdir / "audit.log").read_text(encoding="utf-8")
    assert "test-fixture-salt-padded-to-32-chars" not in raw
    assert "MRN-001" not in raw
    assert "Alice" not in raw
