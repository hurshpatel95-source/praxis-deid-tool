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
            "patient_id_salt": "test-fixture-salt",
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
    assert "test-fixture-salt" not in line
