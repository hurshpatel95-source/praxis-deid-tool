"""Glue between the web UI and the existing de-id pipeline.

The CLI's `run` command builds a `Config` from a YAML file. The UI builds
the same `Config` from form fields + uploaded files. Both flows then drive
the SAME `Deidentifier` and reuse the SAME audit-record shape — no parallel
implementations, no chance of the UI silently doing something the CLI
doesn't.

Inputs from the UI are written to a per-run temp directory and processed
in place. We never copy uploads anywhere outside that temp dir, and we
never make a network call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..audit import fingerprint_input_file, get_tool_version, write_run_record
from ..config import (
    AuditConfig,
    Config,
    CsvSourceConfig,
    DeidConfig,
    OutputConfig,
)
from ..deidentify import Deidentifier
from ..sources import iter_csv_rows
from ..upload import write_csvs

# Roles the UI accepts as uploads; matches the six canonical entities.
INPUT_ROLES: tuple[str, ...] = (
    "patients",
    "appointments",
    "providers",
    "procedures",
    "referrals",
    "invoices",
)


@dataclass(frozen=True)
class RunRequest:
    """A single web-UI run, materialized to disk."""

    practice_id: str
    patient_id_salt: str
    small_n_threshold: int
    audit_log_path: Path
    output_dir: Path
    # role -> path on disk (already written by the route handler).
    inputs: dict[str, Path]


@dataclass(frozen=True)
class OutputFileSummary:
    role: str
    path: str
    row_count: int
    byte_count: int


@dataclass(frozen=True)
class RunResult:
    status: str  # "success" | "error"
    output_dir: str
    files: list[OutputFileSummary]
    audit_record: dict[str, Any]
    error: str | None = None


def build_config(req: RunRequest) -> Config:
    """Construct the same `Config` the CLI builds, but from form data."""
    return Config(
        practice_id=req.practice_id,
        source=CsvSourceConfig(
            patients_file=req.inputs.get("patients"),
            appointments_file=req.inputs.get("appointments"),
            providers_file=req.inputs.get("providers"),
            procedures_file=req.inputs.get("procedures"),
            referrals_file=req.inputs.get("referrals"),
            invoices_file=req.inputs.get("invoices"),
        ),
        output=OutputConfig(
            type="csv",
            directory=req.output_dir,
            api_endpoint=None,
            api_key=None,
        ),
        deidentification=DeidConfig(
            patient_id_salt=req.patient_id_salt,
            small_n_threshold=req.small_n_threshold,
        ),
        audit=AuditConfig(log_path=req.audit_log_path),
    )


def execute(req: RunRequest) -> RunResult:
    """Run de-id with the same control flow as `cli._cmd_run`.

    Returns a structured RunResult that can be JSON-serialized straight to
    the browser. On any exception we return status='error' with the message
    rather than letting it bubble — the UI will render it.
    """
    try:
        cfg = build_config(req)
    except (ValueError, TypeError) as err:
        return RunResult(
            status="error",
            output_dir=str(req.output_dir),
            files=[],
            audit_record={},
            error=str(err),
        )

    deid = Deidentifier(
        practice_id=cfg.practice_id,
        salt=cfg.deidentification.patient_id_salt,
        small_n_threshold=cfg.deidentification.small_n_threshold,
    )

    # Fingerprint inputs BEFORE ingest. Same ordering as the CLI so audit
    # records from `serve` and `run` are visually indistinguishable.
    input_files: dict[str, dict[str, object]] = {}
    for role in INPUT_ROLES:
        path = req.inputs.get(role)
        if path is not None and path.exists():
            input_files[role] = fingerprint_input_file(path)

    try:
        _ingest_optional(req.inputs.get("patients"), deid.add_patient)
        _ingest_optional(req.inputs.get("appointments"), deid.add_appointment)
        _ingest_optional(req.inputs.get("providers"), deid.add_provider)
        _ingest_optional(req.inputs.get("procedures"), deid.add_procedure)
        _ingest_optional(req.inputs.get("referrals"), deid.add_referral)
        _ingest_optional(req.inputs.get("invoices"), deid.add_invoice)
        patients, appointments, providers, procedures, referrals, invoices = deid.finalize()
    except Exception as err:  # noqa: BLE001 - bubble up cleanly to the UI
        return RunResult(
            status="error",
            output_dir=str(req.output_dir),
            files=[],
            audit_record={"input_files": input_files},
            error=f"de-id pipeline failed: {err}",
        )

    paths = write_csvs(
        cfg.output.directory or req.output_dir,
        patients=patients,
        appointments=appointments,
        providers=providers,
        procedures=procedures,
        referrals=referrals,
        invoices=invoices,
    )

    audit_record: dict[str, Any] = {
        "tool_version": get_tool_version(),
        "practice_id": cfg.practice_id,
        "input_files": input_files,
        "stats": {
            "patients_in": deid.stats.patients_in,
            "patients_out": len(patients),
            "appointments_in": deid.stats.appointments_in,
            "appointments_out": len(appointments),
            "providers_out": len(providers),
            "procedures_in": deid.stats.procedures_in,
            "procedures_out": len(procedures),
            "referrals_out": len(referrals),
            "invoices_out": len(invoices),
            "rows_dropped": deid.stats.rows_dropped,
            "drop_reasons": deid.stats.drop_reasons,
            "small_n_suppressions": deid.stats.small_n_suppressions,
        },
        "output": {
            "mode": "csv",
            "files": {k: str(v) for k, v in paths.items()},
        },
    }
    write_run_record(cfg.audit.log_path, audit_record)

    files: list[OutputFileSummary] = []
    counts = {
        "patients": len(patients),
        "appointments": len(appointments),
        "providers": len(providers),
        "procedures": len(procedures),
        "referrals": len(referrals),
        "invoices": len(invoices),
    }
    for role, p in paths.items():
        files.append(
            OutputFileSummary(
                role=role,
                path=str(p),
                row_count=counts.get(role, 0),
                byte_count=p.stat().st_size if p.exists() else 0,
            )
        )

    return RunResult(
        status="success",
        output_dir=str(cfg.output.directory or req.output_dir),
        files=files,
        audit_record=audit_record,
    )


def _ingest_optional(path: Path | None, add_fn: Any) -> None:
    if path is None:
        return
    for row in iter_csv_rows(path):
        add_fn(row)
