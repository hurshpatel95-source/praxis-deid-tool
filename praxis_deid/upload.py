"""Output writers: CSV file emission OR direct API POST to Praxis.

CSV mode produces six files matching the schema Praxis CsvUploadAdapter
parses (see praxis-app/lib/adapters/csv-upload.ts). The practice can then
SFTP / email / manually upload them — or run the API mode for direct push.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any


def write_csvs(
    out_dir: Path,
    *,
    patients: Iterable[Any],
    appointments: Iterable[Any],
    providers: Iterable[Any],
    procedures: Iterable[Any],
    referrals: Iterable[Any],
    invoices: Iterable[Any],
) -> dict[str, Path]:
    """Write the six canonical CSVs into out_dir. practice_id is stripped from
    every row — the receiving CsvUploadAdapter injects practice_id from its
    own config, and including it here would make the file double-track-able.

    Returns the mapping of entity name -> file path written.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {}
    paths["patients"] = _write(out_dir / "patients.csv", patients)
    paths["appointments"] = _write(out_dir / "appointments.csv", appointments)
    paths["providers"] = _write(out_dir / "providers.csv", providers)
    paths["procedures"] = _write(out_dir / "procedures.csv", procedures)
    paths["referrals"] = _write(out_dir / "referrals.csv", referrals)
    paths["invoices"] = _write(out_dir / "invoices.csv", invoices)
    return paths


def _write(path: Path, rows: Iterable[Any]) -> Path:
    rows = list(rows)
    if not rows:
        # Still write the header. The receiver tolerates empty bodies.
        # We can't know the columns without a sample row, so write nothing.
        path.write_text("", encoding="utf-8")
        return path

    sample = rows[0]
    cols = [f.name for f in fields(sample) if f.name != "practice_id"]

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        for r in rows:
            d = asdict(r)
            d.pop("practice_id", None)
            # Booleans stringify to "True"/"False"; force lowercase to match the TS reader.
            for k, v in list(d.items()):
                if isinstance(v, bool):
                    d[k] = "true" if v else "false"
                elif v is None:
                    d[k] = ""
            writer.writerow(d)
    return path


def post_to_api(api_endpoint: str, api_key: str, payload: dict[str, Any]) -> None:
    """Stub for direct API POST. Actual ingestion endpoint lands when Praxis
    exposes one — until then practices use CSV mode + the existing
    CsvUploadAdapter path. This stub exists so config validation works."""
    raise NotImplementedError(
        "API output mode not yet implemented; use output.type=csv until Praxis "
        "exposes /api/ingest. See README."
    )
