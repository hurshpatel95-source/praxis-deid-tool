"""Belt-and-suspenders PHI scan over de-identified output.

The de-id pipeline is the load-bearing privacy boundary; this scan is a
post-hoc smoke test that the practice's IT admin can eyeball before
shipping the CSVs anywhere. It looks for the most embarrassing patterns
(SSN, email, phone, ZIP+4, a few date shapes) and reports matches by file
+ column. A clean scan does NOT prove de-identification — it just rules
out the obvious leaks.

We intentionally scan the OUTPUT CSVs, not the inputs. Inputs are
expected to contain PHI; that's the whole reason for the tool.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

# Conservative patterns — false positives are fine here (we'd rather flag
# a "looks like an SSN" string in an external_id and have the admin glance
# at it than miss a real leak).
_PATTERNS: dict[str, re.Pattern[str]] = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    # Reasonably permissive email; the digits of de-id'd ext IDs won't match.
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    # US phone formats. Bare 10-digit runs would false-positive on hash
    # prefixes, so require a separator.
    "phone": re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"),
    # ZIP+4 (5+4). Plain 5-digit ZIP is too noisy to flag.
    "zip_plus_four": re.compile(r"\b\d{5}-\d{4}\b"),
    # Day-resolution dates — Safe Harbor only allows YYYY-MM, so YYYY-MM-DD
    # in output is suspicious.
    "iso_date_with_day": re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
}


@dataclass(frozen=True)
class PhiHit:
    file: str
    column: str
    row_index: int  # 1-based, header is row 0
    pattern: str
    sample: str  # truncated value


@dataclass(frozen=True)
class PhiScanResult:
    file: str
    rows_scanned: int
    hits: list[PhiHit]
    error: str | None = None


def scan_output_csv(path: Path) -> PhiScanResult:
    """Scan a single output CSV. Returns rows_scanned + a list of hits.

    Rows-scanned is reported even for clean files so the UI can prove the
    scan actually ran.
    """
    if not path.exists():
        return PhiScanResult(file=str(path), rows_scanned=0, hits=[], error="missing")
    if path.stat().st_size == 0:
        return PhiScanResult(file=str(path), rows_scanned=0, hits=[])

    hits: list[PhiHit] = []
    rows = 0
    try:
        with path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, start=1):
                rows += 1
                for col, value in row.items():
                    if value is None or value == "":
                        continue
                    for name, pat in _PATTERNS.items():
                        if pat.search(value):
                            hits.append(
                                PhiHit(
                                    file=path.name,
                                    column=col or "<unnamed>",
                                    row_index=i,
                                    pattern=name,
                                    sample=value[:80],
                                )
                            )
    except (OSError, UnicodeDecodeError) as err:
        return PhiScanResult(file=str(path), rows_scanned=rows, hits=hits, error=str(err))
    return PhiScanResult(file=str(path), rows_scanned=rows, hits=hits)


def scan_output_dir(out_dir: Path) -> list[PhiScanResult]:
    """Scan every *.csv under out_dir. Stable order so UI rendering is deterministic."""
    if not out_dir.exists():
        return []
    return [scan_output_csv(p) for p in sorted(out_dir.glob("*.csv"))]
