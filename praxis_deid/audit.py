"""Local audit log writer.

Append-only newline-delimited JSON. Every run writes a single envelope
documenting what was processed and what crossed the wire to Praxis cloud.
Salt is NEVER logged — even hash inputs are aggregated (counts only).

SECURITY_AUDIT.md finding #4: per-run records now include input file
fingerprints (path, sha256, byte_count) for each role and the tool
version, so a HIPAA reviewer can answer "exactly which file was
processed on day X". File-level metadata only — never per-record content.

Retention is the practice's responsibility. The path is configured via
audit.log_path in the YAML config; rotate via standard logrotate.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Read in 1 MiB blocks; large enough to be efficient on practice exports
# (typically 10s of MB) without holding the full file in memory.
_HASH_BLOCK_SIZE = 1024 * 1024


def write_run_record(log_path: Path, record: dict[str, Any]) -> None:
    """Append a single audit envelope. Creates the parent directory if needed.

    The record must NOT contain raw source identifiers, the salt, or any PHI.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **record,
    }
    line = json.dumps(record, default=_json_default, separators=(",", ":"))
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    # Best-effort 0640 — owner read+write, group read.
    try:
        os.chmod(log_path, 0o640)
    except OSError:
        pass


def fingerprint_input_file(path: Path) -> dict[str, Any]:
    """Return forensic fingerprint of an input CSV.

    {path: str, sha256: str, byte_count: int} — enough for a HIPAA reviewer
    to prove exactly which export was processed. Reads the file in chunks
    so multi-GB inputs don't OOM. NEVER reads or returns row content.
    """
    h = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(_HASH_BLOCK_SIZE)
            if not chunk:
                break
            h.update(chunk)
            byte_count += len(chunk)
    return {
        "path": str(path),
        "sha256": h.hexdigest(),
        "byte_count": byte_count,
    }


def get_tool_version() -> str:
    """Return the installed praxis-deid version, falling back to the package
    __version__ when the package isn't installed via pip (e.g. running from
    a source checkout in CI / dev). Used in audit records for chain of
    custody."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("praxis-deid")
        except PackageNotFoundError:
            pass
    except ImportError:
        pass
    try:
        from . import __version__

        return __version__
    except ImportError:
        return "unknown"


def _json_default(obj: object) -> object:
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)  # type: ignore[arg-type]
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"not serializable: {type(obj)!r}")
