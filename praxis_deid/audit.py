"""Local audit log writer.

Append-only newline-delimited JSON. Every run writes a single envelope
documenting what was processed and what crossed the wire to Praxis cloud.
Salt is NEVER logged — even hash inputs are aggregated (counts only).

Retention is the practice's responsibility. The path is configured via
audit.log_path in the YAML config; rotate via standard logrotate.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def _json_default(obj: object) -> object:
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)  # type: ignore[arg-type]
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"not serializable: {type(obj)!r}")
