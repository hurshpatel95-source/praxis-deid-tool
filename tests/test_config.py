"""Tests for config loading + validation.

Covers SECURITY_AUDIT.md finding #3: salt-length guard. The hand-rolled
validator previously accepted `patient_id_salt: "x"` despite README +
example.yaml documenting >= 32 chars. This file pins that policy.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from praxis_deid.config import MIN_SALT_LENGTH, ConfigError, load_config


def _base_cfg(salt: str, *, workdir: Path) -> dict:
    return {
        "practice_id": "00000000-0000-0000-0000-0000000000a1",
        "source": {
            "type": "csv",
            "patients_file": str(workdir / "patients.csv"),
        },
        "output": {
            "type": "csv",
            "directory": str(workdir / "out"),
        },
        "deidentification": {
            "patient_id_salt": salt,
            "small_n_threshold": 5,
        },
        "audit": {
            "log_path": str(workdir / "audit.log"),
        },
    }


def _write(tmp_path: Path, cfg: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump(cfg), encoding="utf-8")
    return p


def test_short_salt_rejected_with_actionable_message(tmp_path: Path) -> None:
    """SECURITY_AUDIT.md #3: `patient_id_salt: "x"` must NOT load. The
    error message must tell the operator how to fix it."""
    cfg_path = _write(tmp_path, _base_cfg("x", workdir=tmp_path))
    with pytest.raises(ConfigError) as exc:
        load_config(cfg_path)
    msg = str(exc.value)
    assert "patient_id_salt" in msg
    assert str(MIN_SALT_LENGTH) in msg
    # The fix instruction must be discoverable from the error itself.
    assert "openssl rand -hex 32" in msg


def test_salt_at_minimum_length_accepted(tmp_path: Path) -> None:
    salt = "a" * MIN_SALT_LENGTH
    cfg_path = _write(tmp_path, _base_cfg(salt, workdir=tmp_path))
    cfg = load_config(cfg_path)
    assert cfg.deidentification.patient_id_salt == salt


def test_salt_one_below_minimum_rejected(tmp_path: Path) -> None:
    salt = "a" * (MIN_SALT_LENGTH - 1)
    cfg_path = _write(tmp_path, _base_cfg(salt, workdir=tmp_path))
    with pytest.raises(ConfigError):
        load_config(cfg_path)


def test_long_random_salt_accepted(tmp_path: Path) -> None:
    # The README's recommended `openssl rand -hex 32` produces 64 chars.
    salt = "deadbeef" * 8
    cfg_path = _write(tmp_path, _base_cfg(salt, workdir=tmp_path))
    cfg = load_config(cfg_path)
    assert cfg.deidentification.patient_id_salt == salt


# --- SECURITY_AUDIT.md finding #5: procedure_categorization is gone ---------

def test_procedure_categorization_field_now_rejected(tmp_path: Path) -> None:
    """The `procedure_categorization: default` field used to be accepted
    silently and ignored. The README implied a default mapping existed; it
    didn't. The field is now rejected with an actionable message rather
    than implying behavior we don't ship."""
    cfg = _base_cfg("a" * MIN_SALT_LENGTH, workdir=tmp_path)
    cfg["deidentification"]["procedure_categorization"] = "default"
    cfg_path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError) as exc:
        load_config(cfg_path)
    assert "procedure_categorization" in str(exc.value)


def test_config_without_procedure_categorization_loads(tmp_path: Path) -> None:
    """Sanity: the absence of the now-removed field is the happy path."""
    cfg_path = _write(tmp_path, _base_cfg("a" * MIN_SALT_LENGTH, workdir=tmp_path))
    cfg = load_config(cfg_path)
    assert not hasattr(cfg.deidentification, "procedure_categorization")
