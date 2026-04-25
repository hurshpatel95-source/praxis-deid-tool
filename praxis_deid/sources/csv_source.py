"""CSV source — yields raw row dicts from a single file.

Consumed by deidentify.Deidentifier.add_*. The file path is the practice's
PM export — the tool only ever reads, never writes back.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path


def iter_csv_rows(path: Path) -> Iterator[dict[str, str]]:
    """Yield each data row as a dict keyed by the header row's columns.

    Empty cells are returned as empty strings, never None — keeps downstream
    code from sprouting None-checks. Skips fully empty rows.
    """
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not any((v or "").strip() for v in row.values()):
                continue
            # Force str / "" for every value.
            yield {k: (v if v is not None else "") for k, v in row.items()}
