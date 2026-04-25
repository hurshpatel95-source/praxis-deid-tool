"""YAML configuration loading + validation.

Salt-length policy (SECURITY_AUDIT.md finding #3): the example.yaml documents
">= 32 chars recommended" but nothing enforced it; a config with
`patient_id_salt: "x"` was silently accepted. We now require >= 32.


Schema (see examples/praxis-deid.example.yaml):

  practice_id: <uuid issued by Praxis cloud>
  api_endpoint: https://api.praxishealth.ai/ingest    # optional
  api_key: <token>                                    # optional

  source:
    type: csv                                          # only 'csv' supported in v0.1
    patients_file:     /path/to/patients.csv
    appointments_file: /path/to/appointments.csv
    providers_file:    /path/to/providers.csv
    procedures_file:   /path/to/procedures.csv
    referrals_file:    /path/to/referrals.csv
    invoices_file:     /path/to/invoices.csv

  output:
    type: csv                                          # 'csv' (write files) or 'api' (POST)
    directory: /var/lib/praxis_deid/out                # for csv

  deidentification:
    patient_id_salt: <practice-secret>                 # required, never logged
    small_n_threshold: 5

  audit:
    log_path: /var/log/praxis_deid/audit.log

Note (SECURITY_AUDIT.md finding #5): a `procedure_categorization: default`
field used to live under `deidentification:`. It was loaded but never
wired through to the Deidentifier — there was no actual default mapping.
Removed in v0.1.x to stop implying a behavior we don't ship. Practices
that need code-to-category mapping should pre-process upstream until we
publish vertical-specific defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Minimum patient_id_salt length. 32 hex chars = 128 bits of entropy from
# `openssl rand -hex 32`, which is the README's recommendation and what
# example.yaml documents. Anything shorter is functionally a typo or a
# placeholder and should be rejected at config load.
MIN_SALT_LENGTH = 32


class ConfigError(ValueError):
    """Raised when a config file is structurally valid YAML but violates a
    Praxis de-id policy (e.g. patient_id_salt too short). Distinct from
    ValueError so downstream tooling can catch and present specifically."""


@dataclass(frozen=True)
class CsvSourceConfig:
    patients_file: Path | None
    appointments_file: Path | None
    providers_file: Path | None
    procedures_file: Path | None
    referrals_file: Path | None
    invoices_file: Path | None


@dataclass(frozen=True)
class OutputConfig:
    type: str  # "csv" or "api"
    directory: Path | None
    api_endpoint: str | None
    api_key: str | None


@dataclass(frozen=True)
class DeidConfig:
    patient_id_salt: str
    small_n_threshold: int


@dataclass(frozen=True)
class AuditConfig:
    log_path: Path


@dataclass(frozen=True)
class Config:
    practice_id: str
    source: CsvSourceConfig
    output: OutputConfig
    deidentification: DeidConfig
    audit: AuditConfig


def load_config(path: str | Path) -> Config:
    """Load and validate a config YAML. Raises ValueError on missing/bad fields."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config root must be a mapping")

    practice_id = _require_str(raw, "practice_id")

    src_raw = _require_mapping(raw, "source")
    src_type = _require_str(src_raw, "type")
    if src_type != "csv":
        raise ValueError(f"only source.type=csv is supported in v0.1, got {src_type!r}")
    src = CsvSourceConfig(
        patients_file=_optional_path(src_raw, "patients_file"),
        appointments_file=_optional_path(src_raw, "appointments_file"),
        providers_file=_optional_path(src_raw, "providers_file"),
        procedures_file=_optional_path(src_raw, "procedures_file"),
        referrals_file=_optional_path(src_raw, "referrals_file"),
        invoices_file=_optional_path(src_raw, "invoices_file"),
    )

    out_raw = _require_mapping(raw, "output")
    out_type = _require_str(out_raw, "type")
    if out_type not in {"csv", "api"}:
        raise ValueError(f"output.type must be 'csv' or 'api', got {out_type!r}")
    output = OutputConfig(
        type=out_type,
        directory=_optional_path(out_raw, "directory"),
        api_endpoint=_optional_str(raw, "api_endpoint"),
        api_key=_optional_str(raw, "api_key"),
    )
    if out_type == "csv" and output.directory is None:
        raise ValueError("output.type=csv requires output.directory")
    if out_type == "api" and not (output.api_endpoint and output.api_key):
        raise ValueError("output.type=api requires top-level api_endpoint and api_key")

    deid_raw = _require_mapping(raw, "deidentification")
    salt = _require_str(deid_raw, "patient_id_salt")
    if len(salt) < MIN_SALT_LENGTH:
        raise ConfigError(
            f"deidentification.patient_id_salt must be at least {MIN_SALT_LENGTH} "
            f"characters (got {len(salt)}); use `openssl rand -hex 32` to generate one."
        )
    # SECURITY_AUDIT.md #5: `procedure_categorization` was previously loaded
    # here as a string and silently ignored downstream — there was no default
    # mapping. We now reject the field with an actionable message instead of
    # implying it does something. Practices needing code-to-category mapping
    # should pre-process upstream until vertical defaults ship.
    if "procedure_categorization" in deid_raw:
        raise ConfigError(
            "deidentification.procedure_categorization is no longer supported "
            "(it was never wired up). Remove the field from your config; "
            "pre-categorize procedures upstream of the de-id tool until "
            "vertical-specific default mappings ship."
        )
    deid = DeidConfig(
        patient_id_salt=salt,
        small_n_threshold=int(deid_raw.get("small_n_threshold", 5)),
    )
    if deid.small_n_threshold < 1:
        raise ValueError("deidentification.small_n_threshold must be >= 1")

    audit_raw = _require_mapping(raw, "audit")
    audit = AuditConfig(log_path=Path(_require_str(audit_raw, "log_path")).expanduser())

    return Config(
        practice_id=practice_id,
        source=src,
        output=output,
        deidentification=deid,
        audit=audit,
    )


# --- helpers ---------------------------------------------------------------

def _require_mapping(d: dict[str, Any], key: str) -> dict[str, Any]:
    v = d.get(key)
    if not isinstance(v, dict):
        raise ValueError(f"{key} must be a mapping")
    return v


def _require_str(d: dict[str, Any], key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise ValueError(f"{key} must be a non-empty string")
    return v


def _optional_str(d: dict[str, Any], key: str) -> str | None:
    v = d.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError(f"{key} must be a string when present")
    return v


def _optional_path(d: dict[str, Any], key: str) -> Path | None:
    v = _optional_str(d, key)
    return Path(v).expanduser() if v else None
