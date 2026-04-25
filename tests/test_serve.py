"""Tests for the optional `praxis-deid serve` web UI.

Skipped automatically if the [serve] extra isn't installed, so contributors
running just `pip install -e .` aren't broken. CI installs `[dev]`, which
pulls the same packages in transitively for these tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Skip the whole module if the serve extra isn't present. Keeps the base
# test run green for contributors who haven't installed the UI deps.
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from praxis_deid.serve.app import (  # noqa: E402
    LOOPBACK_HOSTS,
    build_app,
    validate_bind_host,
)
from praxis_deid.serve.phi_scan import scan_output_csv  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


# --- bind-host validation ---------------------------------------------------

class TestBindHostValidation:
    """The default-deny bind guard is the load-bearing safety net.

    See SECURITY_AUDIT.md style: any change here that loosens the default
    needs explicit justification + matching test."""

    @pytest.mark.parametrize("host", sorted(LOOPBACK_HOSTS))
    def test_loopback_hosts_always_allowed(self, host: str) -> None:
        # Should not raise for any of the canonical loopback names.
        validate_bind_host(host, allow_remote=False)
        validate_bind_host(host, allow_remote=True)

    def test_zero_zero_zero_zero_rejected_by_default(self) -> None:
        with pytest.raises(ValueError, match="refusing to bind"):
            validate_bind_host("0.0.0.0", allow_remote=False)

    def test_lan_address_rejected_by_default(self) -> None:
        with pytest.raises(ValueError, match="refusing to bind"):
            validate_bind_host("192.168.1.10", allow_remote=False)

    def test_zero_zero_zero_zero_allowed_with_explicit_flag(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        validate_bind_host("0.0.0.0", allow_remote=True)
        captured = capsys.readouterr()
        # The escape hatch must be loud — the warning is half the point.
        assert "WARNING" in captured.err
        assert "0.0.0.0" in captured.err


# --- /api/health and / -----------------------------------------------------

@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app())


def test_health_endpoint(client: TestClient) -> None:
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_index_renders(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    body = res.text
    # Every input role must surface in the rendered form so the IT admin
    # can pick all six files. If a role is missing here, the UI silently
    # drops a CSV.
    for role in ("patients", "appointments", "providers", "procedures", "referrals", "invoices"):
        assert f'name="{role}"' in body, f"missing file picker for {role}"
    assert "Praxis de-identification" in body


# --- /api/run end-to-end ---------------------------------------------------

def test_api_run_round_trip_with_fixtures(client: TestClient, tmp_path: Path) -> None:
    """Upload the existing fixture CSVs to /api/run; assert the same audit
    envelope shape the CLI writes (tool_version, input_files with
    sha256/path/byte_count, stats with patients_in/out, etc.)."""
    audit = tmp_path / "audit.log"

    files = {
        "patients": ("patients_raw.csv", (FIXTURES / "patients_raw.csv").read_bytes(), "text/csv"),
        "appointments": ("appointments_raw.csv", (FIXTURES / "appointments_raw.csv").read_bytes(), "text/csv"),
        "providers": ("providers_raw.csv", (FIXTURES / "providers_raw.csv").read_bytes(), "text/csv"),
        "procedures": ("procedures_raw.csv", (FIXTURES / "procedures_raw.csv").read_bytes(), "text/csv"),
    }
    data = {
        "practice_id": "00000000-0000-0000-0000-0000000000a1",
        # >= 32 chars to satisfy the salt-length guard from SECURITY_AUDIT #3.
        "patient_id_salt": "test-fixture-salt-padded-to-32-chars",
        "small_n_threshold": "1",
        "audit_log_path": str(audit),
    }

    res = client.post("/api/run", data=data, files=files)
    assert res.status_code == 200, res.text
    payload = res.json()

    assert payload["status"] == "success", payload
    assert payload["error"] is None
    assert payload["output_dir"]
    assert isinstance(payload["files"], list) and payload["files"], payload["files"]

    # Output files include all six entities, each with non-negative counts.
    file_roles = {f["role"] for f in payload["files"]}
    assert {"patients", "appointments", "providers", "procedures"} <= file_roles

    # PHI scan ran across each output CSV.
    assert payload["phi_scan"], "expected phi_scan results"
    # We uploaded fixtures whose output should be PHI-clean.
    for scan in payload["phi_scan"]:
        assert scan["hits"] == [], (
            f"unexpected PHI hits in {scan['file']}: {scan['hits']}"
        )


def test_api_run_audit_record_matches_cli_fingerprint_shape(
    client: TestClient, tmp_path: Path
) -> None:
    """Audit envelope from `serve` must match the shape the CLI writes
    (52f2afc / SECURITY_AUDIT #4): tool_version + per-role
    {path, sha256, byte_count}."""
    audit = tmp_path / "audit.log"
    files = {
        "patients": ("patients_raw.csv", (FIXTURES / "patients_raw.csv").read_bytes(), "text/csv"),
        "appointments": ("appointments_raw.csv", (FIXTURES / "appointments_raw.csv").read_bytes(), "text/csv"),
        "providers": ("providers_raw.csv", (FIXTURES / "providers_raw.csv").read_bytes(), "text/csv"),
        "procedures": ("procedures_raw.csv", (FIXTURES / "procedures_raw.csv").read_bytes(), "text/csv"),
    }
    data = {
        "practice_id": "00000000-0000-0000-0000-0000000000a1",
        "patient_id_salt": "test-fixture-salt-padded-to-32-chars",
        "small_n_threshold": "1",
        "audit_log_path": str(audit),
    }
    res = client.post("/api/run", data=data, files=files)
    assert res.status_code == 200

    record = res.json()["audit_record"]
    # Same envelope keys as the CLI.
    assert record["tool_version"]
    assert record["practice_id"] == "00000000-0000-0000-0000-0000000000a1"
    assert "input_files" in record
    for role in ("patients", "appointments", "providers", "procedures"):
        entry = record["input_files"][role]
        assert set(entry) == {"path", "sha256", "byte_count"}
        assert len(entry["sha256"]) == 64
        assert entry["byte_count"] > 0

    stats = record["stats"]
    for key in (
        "patients_in", "patients_out", "appointments_in", "appointments_out",
        "providers_out", "procedures_in", "procedures_out",
        "rows_dropped", "drop_reasons", "small_n_suppressions",
    ):
        assert key in stats, f"missing stat {key}"

    # The same record was appended to the audit log on disk.
    line = audit.read_text(encoding="utf-8").strip().splitlines()[-1]
    on_disk = json.loads(line)
    assert on_disk["tool_version"] == record["tool_version"]
    assert on_disk["input_files"]["patients"]["sha256"] == record["input_files"]["patients"]["sha256"]
    # Salt must NEVER appear in the audit log.
    assert "test-fixture-salt-padded-to-32-chars" not in audit.read_text(encoding="utf-8")


def test_api_run_rejects_no_uploads(client: TestClient) -> None:
    res = client.post(
        "/api/run",
        data={
            "practice_id": "00000000-0000-0000-0000-0000000000a1",
            "patient_id_salt": "test-fixture-salt-padded-to-32-chars",
            "small_n_threshold": "1",
        },
    )
    assert res.status_code == 400
    assert "no input CSVs" in res.json()["detail"]


# --- PHI scan --------------------------------------------------------------

def test_phi_scan_flags_obvious_leaks(tmp_path: Path) -> None:
    leaky = tmp_path / "leaky.csv"
    leaky.write_text(
        "external_id,note\n"
        "abc12345,patient ssn 123-45-6789\n"
        "def67890,call (609) 555-0001\n"
        "ghi13579,appt 2026-04-15\n"
        "jkl24680,plain row\n",
        encoding="utf-8",
    )
    result = scan_output_csv(leaky)
    assert result.error is None
    assert result.rows_scanned == 4
    pats = {h.pattern for h in result.hits}
    assert "ssn" in pats
    assert "phone" in pats
    assert "iso_date_with_day" in pats


def test_phi_scan_clean_when_no_patterns(tmp_path: Path) -> None:
    clean = tmp_path / "clean.csv"
    clean.write_text(
        "external_id,age_band,zip_prefix\n"
        "abc12345,31-45,082\n"
        "def67890,46-60,082\n",
        encoding="utf-8",
    )
    result = scan_output_csv(clean)
    assert result.hits == []
    assert result.rows_scanned == 2
