"""Shared infrastructure for all Phase-C per-extension extractors.

Design constraints (locked at Phase-C kickoff, per `Phase-C` brief):

  * NO raw SQL string injection from mapping configs. The mapping config
    supplies whitelisted column expressions; the extractor parses them
    into either (a) safe parameterised queries via a row-source callable
    or (b) deterministic Python expressions evaluated against already-
    fetched row dicts. We never hand a config-derived SQL fragment to
    a DBAPI cursor's execute() string.

  * Mapping configs that contain semicolons, SQL comment markers
    (``--``, ``/*``, ``*/``), or DDL/DML keywords (``drop``, ``truncate``,
    ``delete``, ``update``, ``insert``, ``alter``, ``grant``, ``revoke``)
    are rejected at load time with an ``ExtractorError``. This is checked
    before any DB activity.

  * Every numeric output that represents per-record dollars passes
    through ``safe_harbor.amount_to_band``. Tests assert no exact dollar
    value > 1000 leaks into output CSVs.

  * The same ``Deidentifier`` instance (== same salt) is reused across
    every extractor in a single run, so a patient_source_id HMAC's to
    the SAME external_id whether seen in patients_raw, claims_raw,
    payments_raw, treatment_plans_raw, etc.

  * Errors flow through ``ExtractorError`` — never silently swallowed.

  * The base never imports a DB driver. The DB connection is a duck-typed
    object passed in by the CLI; the base only ever asks the
    ``RowSource`` callable for already-materialized row dicts. This makes
    every extractor unit-testable with pure-Python synthetic rows (no
    sqlite, no MySQL container, etc.).

Extractor layering:

    BaseExtractor
        -> per-extension subclass (TreatmentPlansExtractor, ClaimsExtractor,
           CapacityExtractor, PaymentsExtractor, TimekeepingExtractor,
           PatientsExtensionExtractor)
        -> .extract(filter) yields canonical row dataclasses
        -> ._dump_to_csv writes to the output directory

Locked v0.1 modules consumed (NEVER modified):
    - praxis_deid.deidentify.Deidentifier  (HMAC-stable patient IDs)
    - praxis_deid.safe_harbor              (banding primitives)
    - praxis_deid.hashing.stable_external_id
    - praxis_deid.schema  (existing canonical row dataclasses)
    - praxis_deid.audit
"""

from __future__ import annotations

import csv
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from ..deidentify import Deidentifier
from ..hashing import stable_external_id
from ..safe_harbor import (
    REVENUE_BANDS,
    amount_to_band,
    date_to_month,
)
from ..wizard.canonical_schemas import (
    CanonicalSchema,
    get_schema,
)

# -------------------------------------------------------------------------
# Errors
# -------------------------------------------------------------------------


class ExtractorError(RuntimeError):
    """Single error class for all Phase-C extraction failures.

    Flow:
      * Mapping config violates SQL-safety guard -> ExtractorError at load
      * Mapping config references an unknown canonical column -> ExtractorError
      * Required canonical column has no mapping -> ExtractorError
      * A per-row transform (band, date, hmac) fails -> ExtractorError per row
      * Output CSV directory is unwritable -> ExtractorError
    """


# -------------------------------------------------------------------------
# Filter for `extract(filter=...)` — month-bounded windows + row caps
# -------------------------------------------------------------------------


@dataclass(frozen=True)
class Filter:
    """Bounding box for an extract() call.

    Args:
        since_month: 'YYYY-MM' lower bound, inclusive. None = unbounded.
        until_month: 'YYYY-MM' upper bound, inclusive. None = unbounded.
        limit: row cap per source query. None = unbounded.

    The bounds are MONTH-LEVEL because Safe Harbor strips day component;
    operating at month grain at the extractor side aligns with the
    canonical contract and avoids any chance of a date-based subset pull
    leaving the practice with day-resolution dates.
    """

    since_month: str | None = None
    until_month: str | None = None
    limit: int | None = None

    def __post_init__(self) -> None:
        for label, value in (("since_month", self.since_month), ("until_month", self.until_month)):
            if value is None:
                continue
            if not _MONTH_RE.match(value):
                raise ExtractorError(
                    f"Filter.{label} must be 'YYYY-MM' or None, got {value!r}"
                )
        if self.limit is not None and self.limit < 0:
            raise ExtractorError(f"Filter.limit must be >= 0 or None, got {self.limit}")
        if self.since_month and self.until_month and self.since_month > self.until_month:
            raise ExtractorError(
                f"Filter.since_month {self.since_month!r} > until_month "
                f"{self.until_month!r}"
            )


_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# -------------------------------------------------------------------------
# RowSource: how the extractor gets rows from the practice's PMS
# -------------------------------------------------------------------------

# A RowSource is a callable that takes (table_name, columns, filter, limit)
# and yields dicts of {column_name: value}. The CLI wires this to a real
# DBAPI cursor; tests wire it to a list of synthetic dicts. The extractor
# itself never sees the DB driver.
RowSource = Callable[[str, "list[str]", "Filter | None"], Iterable[Mapping[str, Any]]]


# -------------------------------------------------------------------------
# MappingConfig: parsed + safety-checked view of mappings/<pms>/<X>.json
# -------------------------------------------------------------------------


# Forbidden patterns for SQL-injection defence. These are scanned against
# every source_expression in the mapping config before any extraction
# runs. Hits raise ExtractorError("forbidden SQL pattern ...").
_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    ";",
    "--",
    "/*",
    "*/",
)
# Case-insensitive whole-word DDL/DML keywords. Detected after stripping
# string literals so 'drop the ball' inside a CASE doesn't trip.
_FORBIDDEN_KEYWORDS: frozenset[str] = frozenset(
    {
        "DROP",
        "TRUNCATE",
        "DELETE",
        "UPDATE",
        "INSERT",
        "ALTER",
        "GRANT",
        "REVOKE",
        "CREATE",
        "REPLACE",
        "EXEC",
        "EXECUTE",
        "CALL",
        "MERGE",
        "ATTACH",
        "DETACH",
    }
)


def _strip_sql_strings(expr: str) -> str:
    """Drop single-quoted string literals so keyword scanning doesn't
    false-trigger on user-facing text inside CASE branches like
    ``THEN 'paid'``."""
    return re.sub(r"'([^']|'')*'", "''", expr)


def _scan_for_forbidden_sql(expr: str, *, location: str) -> None:
    """Raise ExtractorError if `expr` contains any forbidden pattern.

    Called against every source_expression in a mapping config before
    extraction starts. Belt-and-braces against accidentally hand-edited
    or maliciously substituted mapping configs.
    """
    if expr is None:
        return
    text = str(expr)
    for bad in _FORBIDDEN_SUBSTRINGS:
        if bad in text:
            raise ExtractorError(
                f"forbidden SQL pattern {bad!r} in source_expression at "
                f"{location}: {text!r}"
            )
    cleaned = _strip_sql_strings(text)
    # Whole-word match, case-insensitive.
    for kw in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", cleaned, flags=re.IGNORECASE):
            raise ExtractorError(
                f"forbidden SQL keyword {kw!r} in source_expression at "
                f"{location}: {text!r}"
            )


@dataclass(frozen=True)
class ColumnMapping:
    """Single column-mapping entry from the JSON config."""

    canonical_column: str
    source_expression: str
    confidence: float
    needs_review: bool
    notes: str = ""


@dataclass(frozen=True)
class MappingConfig:
    """Parsed + safety-validated view of a `mappings/<pms>/<X>.json` file.

    Construction validates:
      * Every column-mapping passes ``_scan_for_forbidden_sql``.
      * The referenced canonical schema exists in
        ``praxis_deid.wizard.canonical_schemas``.
      * Every required canonical column has a mapping.
    """

    canonical_schema_name: str
    pms: str
    column_mappings: dict[str, ColumnMapping]
    join_graph: tuple[dict[str, str], ...]
    transformations: dict[str, str]
    notes: tuple[str, ...]
    canonical_schema: CanonicalSchema = field(repr=False)

    def get_source_expression(self, canonical_column: str) -> str:
        m = self.column_mappings.get(canonical_column)
        if m is None:
            raise ExtractorError(
                f"no mapping for canonical column {canonical_column!r} "
                f"in {self.canonical_schema_name}"
            )
        return m.source_expression

    @property
    def required_columns(self) -> tuple[str, ...]:
        return self.canonical_schema.required_columns


def load_mapping_config(path: str | Path) -> MappingConfig:
    """Load + safety-check a mapping config JSON.

    Raises ExtractorError on any structural or safety violation. The error
    message identifies the offending column / keyword so a hand-editor
    can fix the file.
    """
    p = Path(path)
    if not p.exists():
        raise ExtractorError(f"mapping config not found: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ExtractorError(f"mapping config {p} is not valid JSON: {err}") from err

    if not isinstance(raw, dict):
        raise ExtractorError(f"mapping config root must be an object: {p}")

    schema_name = raw.get("canonical_schema")
    if not isinstance(schema_name, str):
        raise ExtractorError(f"mapping config {p} missing 'canonical_schema'")
    try:
        schema = get_schema(schema_name)
    except KeyError as err:
        raise ExtractorError(
            f"mapping config {p} references unknown canonical schema "
            f"{schema_name!r}"
        ) from err

    pms = raw.get("_meta", {}).get("pms") or raw.get("pms") or "unknown"

    raw_column_mappings = raw.get("column_mappings")
    if not isinstance(raw_column_mappings, dict):
        raise ExtractorError(
            f"mapping config {p}: 'column_mappings' must be an object"
        )

    column_mappings: dict[str, ColumnMapping] = {}
    schema_columns = {c.name for c in schema.columns}
    for canonical_col, body in raw_column_mappings.items():
        if not isinstance(body, dict):
            raise ExtractorError(
                f"mapping config {p}: column_mappings.{canonical_col} must be an object"
            )
        if canonical_col not in schema_columns:
            raise ExtractorError(
                f"mapping config {p}: column_mappings.{canonical_col} is "
                f"not a column of canonical schema {schema_name!r}"
            )
        source_expression = body.get("source_expression", "")
        _scan_for_forbidden_sql(
            source_expression,
            location=f"{p}::{canonical_col}",
        )
        # Also scan transformations (they're SQL too).
        column_mappings[canonical_col] = ColumnMapping(
            canonical_column=canonical_col,
            source_expression=str(source_expression),
            confidence=float(body.get("confidence", 0.0)),
            needs_review=bool(body.get("needs_review", False)),
            notes=str(body.get("notes", "")),
        )

    # Required-column gate. A required canonical column MUST have a mapping
    # entry — even if the source_expression is "NULL" (signalling unmappable).
    # An entirely missing entry means the practice's wizard / hand-editor
    # forgot the column, and we should fail hard.
    for required in schema.required_columns:
        if required not in column_mappings:
            raise ExtractorError(
                f"mapping config {p}: required canonical column "
                f"{required!r} has no mapping entry"
            )

    join_graph_raw = raw.get("join_graph", []) or []
    if not isinstance(join_graph_raw, list):
        raise ExtractorError(
            f"mapping config {p}: 'join_graph' must be a list"
        )
    join_graph: list[dict[str, str]] = []
    for j in join_graph_raw:
        if not isinstance(j, dict):
            raise ExtractorError(
                f"mapping config {p}: every join_graph entry must be an object"
            )
        # Whitelist join_type to LEFT/INNER/RIGHT/FULL (no CROSS, no semicolons).
        join_type = str(j.get("join_type", "INNER")).upper()
        if join_type not in {"LEFT", "INNER", "RIGHT", "FULL"}:
            raise ExtractorError(
                f"mapping config {p}: invalid join_type {join_type!r} "
                "(must be LEFT/INNER/RIGHT/FULL)"
            )
        for k, v in j.items():
            if isinstance(v, str):
                _scan_for_forbidden_sql(
                    v, location=f"{p}::join_graph::{k}"
                )
        join_graph.append({k: str(v) for k, v in j.items()})

    transformations_raw = raw.get("transformations", {}) or {}
    if not isinstance(transformations_raw, dict):
        raise ExtractorError(
            f"mapping config {p}: 'transformations' must be an object"
        )
    transformations: dict[str, str] = {}
    for k, v in transformations_raw.items():
        # Comment-style transformations like "/* practice-specific ... */"
        # are documentary placeholders. They must not be scanned (the /*
        # markers would trip the guard); we recognise the pattern and
        # store empty-string instead.
        sv = "" if isinstance(v, str) and v.lstrip().startswith("/*") else str(v)
        if sv:
            _scan_for_forbidden_sql(sv, location=f"{p}::transformations::{k}")
        transformations[str(k)] = sv

    notes_raw = raw.get("notes", []) or []
    notes = tuple(str(n) for n in notes_raw)

    return MappingConfig(
        canonical_schema_name=schema_name,
        pms=str(pms),
        column_mappings=column_mappings,
        join_graph=tuple(join_graph),
        transformations=transformations,
        notes=notes,
        canonical_schema=schema,
    )


# -------------------------------------------------------------------------
# Source-expression evaluator (row-dict bound, never SQL execution)
# -------------------------------------------------------------------------


_TABLE_COL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")


def resolve_simple_reference(
    source_expression: str,
    row: Mapping[str, Any],
) -> Any:
    """Resolve a `table.column` expression against a fetched row dict.

    The row dict is keyed by qualified-column names like `"treatplan.PatNum"`.
    This is the contract the RowSource produces.

    Special cases:
      * Empty / whitespace-only / "NULL" -> Python None
      * Single-quoted string literal -> the literal value
      * Otherwise: simple `table.column` reference; KeyError if not in row.

    Anything more complex (CASE, sub-SELECT, function calls) is left
    unresolved; the caller must implement the per-extension semantics.
    """
    if source_expression is None:
        return None
    expr = source_expression.strip()
    if expr == "" or expr.upper() == "NULL":
        return None
    # Quoted string literal: 'patient', 'self_pay', etc.
    if len(expr) >= 2 and expr[0] == "'" and expr[-1] == "'":
        return expr[1:-1].replace("''", "'")
    # Simple table.column.
    m = _TABLE_COL_RE.fullmatch(expr)
    if m:
        key = f"{m.group(1)}.{m.group(2)}"
        if key not in row:
            # Try unqualified fallback (some PMSs flatten the join).
            unq = m.group(2)
            if unq in row:
                return row[unq]
            raise ExtractorError(
                f"row missing column {key!r} required by mapping; "
                f"row keys: {sorted(row.keys())[:8]}..."
            )
        return row[key]
    # Not a simple reference — caller handles it.
    return _COMPLEX_EXPRESSION


_COMPLEX_EXPRESSION = object()  # sentinel for "not a simple ref"


def is_simple_expression(expr: str) -> bool:
    """Returns True if resolve_simple_reference would handle `expr`."""
    if expr is None:
        return True
    s = expr.strip()
    if s == "" or s.upper() == "NULL":
        return True
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return True
    return bool(_TABLE_COL_RE.fullmatch(s))


# -------------------------------------------------------------------------
# Hipaa-handling dispatcher
# -------------------------------------------------------------------------


def apply_hipaa_handling(
    value: Any,
    *,
    hipaa_handling: str,
    deidentifier: Deidentifier,
) -> Any:
    """Map a raw value through the Safe Harbor primitive named by
    `hipaa_handling` (per CanonicalColumn.hipaa_handling).

    Defers to the locked `safe_harbor.py` and `hashing.py` modules — does
    NOT re-implement banding/HMAC/month-truncation. If the locked modules
    change semantics, every extractor inherits the change for free.
    """
    if value is None or value == "":
        # NULL preserves through every handler. The canonical CSV writer
        # serialises None -> empty cell.
        return None

    if hipaa_handling == "hmac":
        # Reuse the same Deidentifier salt as v0.1 patient-IDs so cross-
        # extension joins survive de-identification.
        return stable_external_id(deidentifier._salt, value)

    if hipaa_handling == "month":
        s = str(value)
        # Trim a trailing time component if present (datetime-style).
        if " " in s:
            s = s.split(" ", 1)[0]
        if "T" in s:
            s = s.split("T", 1)[0]
        return date_to_month(s)

    if hipaa_handling == "band":
        # All per-record dollar amounts MUST go through this path. Tests
        # assert no exact dollar > 1000 leaks downstream.
        try:
            num = float(value)
        except (TypeError, ValueError) as err:
            raise ExtractorError(
                f"hipaa_handling=band requires numeric, got {value!r}"
            ) from err
        return amount_to_band(num)

    if hipaa_handling == "category":
        # The mapping config supplies a transformations entry that maps
        # source values to canonical categories. We only do a pass-
        # through stringify here; the per-extractor subclass calls into
        # _resolve_category for any actual lookup.
        return str(value)

    if hipaa_handling == "passthrough":
        return value

    raise ExtractorError(
        f"unknown hipaa_handling {hipaa_handling!r}"
    )


# -------------------------------------------------------------------------
# BaseExtractor
# -------------------------------------------------------------------------


class BaseExtractor(ABC):
    """Abstract base for the 5 per-extension extractors (A-E) plus F."""

    #: Subclasses set this to the canonical schema name they emit (e.g.
    #: 'treatment_plans_raw'). Used for sanity-checking the loaded
    #: mapping config matches the extractor.
    canonical_schema_name: str = ""

    def __init__(
        self,
        mapping_config: MappingConfig,
        deidentifier: Deidentifier,
        row_source: RowSource,
        *,
        output_dir: Path | None = None,
    ) -> None:
        if not self.canonical_schema_name:
            raise ExtractorError(
                f"{type(self).__name__}.canonical_schema_name must be set"
            )
        if mapping_config.canonical_schema_name != self.canonical_schema_name:
            raise ExtractorError(
                f"mapping config produces {mapping_config.canonical_schema_name!r}, "
                f"but {type(self).__name__} extracts {self.canonical_schema_name!r}"
            )
        self.config = mapping_config
        self.deidentifier = deidentifier
        self.row_source = row_source
        self.output_dir = Path(output_dir) if output_dir else None
        self.dropped_rows = 0
        self.drop_reasons: dict[str, int] = {}

    # --- abstract API ------------------------------------------------------

    @abstractmethod
    def extract(self, filter: Filter | None = None) -> list[Any]:
        """Yield the canonical rows (subclass-specific dataclasses).

        Implementation contract:
          1. Pull rows from self.row_source.
          2. Apply column mappings -> raw canonical values.
          3. Run each value through apply_hipaa_handling.
          4. Validate with the schema dataclass.
          5. Append to result list. On row-level error: increment
             self.dropped_rows + self.drop_reasons; do not abort.
        """

    # --- shared infrastructure --------------------------------------------

    def _resolve(
        self,
        canonical_column: str,
        row: Mapping[str, Any],
    ) -> Any:
        """Resolve a row's source expression for one canonical column.

        Subclasses may override for canonical columns whose source_expression
        is a CASE / sub-SELECT and therefore not a simple reference. The
        default raises ExtractorError so subclasses don't silently miss
        complex expressions.
        """
        expr = self.config.get_source_expression(canonical_column)
        resolved = resolve_simple_reference(expr, row)
        if resolved is _COMPLEX_EXPRESSION:
            raise ExtractorError(
                f"complex source_expression for {canonical_column!r} "
                f"({expr!r}) — extractor must override _resolve to handle it"
            )
        return resolved

    def _hipaa(
        self,
        canonical_column: str,
        value: Any,
    ) -> Any:
        """Apply the canonical column's hipaa_handling policy to `value`."""
        col = next(
            (c for c in self.config.canonical_schema.columns if c.name == canonical_column),
            None,
        )
        if col is None:
            raise ExtractorError(
                f"canonical column {canonical_column!r} not in schema "
                f"{self.canonical_schema_name!r}"
            )
        return apply_hipaa_handling(
            value,
            hipaa_handling=col.hipaa_handling,
            deidentifier=self.deidentifier,
        )

    def _drop(self, reason: str) -> None:
        self.dropped_rows += 1
        self.drop_reasons[reason] = self.drop_reasons.get(reason, 0) + 1

    def _filter_to_period(
        self,
        rows: Iterable[Mapping[str, Any]],
        date_qualified_column: str,
        filter: Filter | None,
    ) -> Iterable[Mapping[str, Any]]:
        """Filter rows by `date_qualified_column` against the Filter's
        since_month/until_month/limit. Operates on the row level so the
        RowSource doesn't need to know about months.
        """
        if filter is None:
            return rows
        out: list[Mapping[str, Any]] = []
        for row in rows:
            v = row.get(date_qualified_column)
            if v is None or v == "":
                # No date — keep it; let the canonical validator decide.
                out.append(row)
                continue
            try:
                month = date_to_month(_truncate_to_date(str(v)))
            except ValueError:
                continue
            if filter.since_month and month < filter.since_month:
                continue
            if filter.until_month and month > filter.until_month:
                continue
            out.append(row)
            if filter.limit is not None and len(out) >= filter.limit:
                break
        return out

    def _dump_to_csv(
        self,
        rows: list[Any],
        filename: str,
    ) -> Path:
        """Write canonical rows to ``output_dir/filename`` as CSV.

        ``output_dir`` MUST be set (raises ExtractorError otherwise). The
        CSV uses dataclass field order; None becomes an empty cell;
        booleans serialise to lower-case 'true'/'false' to match the
        cloud CsvUploadAdapter contract.

        Returns the written path.
        """
        if self.output_dir is None:
            raise ExtractorError(
                f"{type(self).__name__}: output_dir not set; cannot dump CSV"
            )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / filename

        if not rows:
            path.write_text("", encoding="utf-8")
            return path

        sample = rows[0]
        if not is_dataclass(sample):
            raise ExtractorError(
                f"_dump_to_csv expects dataclass rows; got {type(sample).__name__}"
            )
        col_names = [f.name for f in fields(sample) if f.name != "practice_id"]

        with path.open("w", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=col_names)
            writer.writeheader()
            for r in rows:
                d = asdict(r)
                d.pop("practice_id", None)
                for k, v in list(d.items()):
                    if isinstance(v, bool):
                        d[k] = "true" if v else "false"
                    elif v is None:
                        d[k] = ""
                writer.writerow(d)
        return path


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------


def _truncate_to_date(s: str) -> str:
    """Take 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DDTHH:MM:SS' or 'YYYY-MM-DD'
    and return the YYYY-MM-DD portion. ValueError if shorter than 10 chars
    (date_to_month will reject).
    """
    s = s.strip()
    if len(s) < 7:
        return s
    if " " in s:
        return s.split(" ", 1)[0]
    if "T" in s:
        return s.split("T", 1)[0]
    return s


def assert_no_exact_dollars_in_csv(path: Path) -> None:
    """Defensive scanner: walk every cell of a written CSV and raise
    ExtractorError if any cell that looks like a numeric dollar amount
    larger than 1000 has slipped through (i.e. an unbanded amount).

    This is the belt-and-braces enforcement of the BAA invariant
    "per-record amounts are banded; only sums-across-many-records are
    exact" (BAA_INVARIANTS.md §I.5). Tests run this on every produced CSV.
    """
    if not path.exists() or path.stat().st_size == 0:
        return
    # Numeric-looking cells with no $-band prefix.
    suspect = re.compile(r"^\d{4,}\.?\d*$")
    with path.open(newline="", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for row_num, row in enumerate(reader, start=2):  # row 1 is header
            for col, cell in row.items():
                if not isinstance(cell, str):
                    continue
                cell = cell.strip()
                if not cell:
                    continue
                if suspect.match(cell):
                    # Now the actual cutoff: only raise if > 1000.
                    try:
                        val = float(cell)
                    except ValueError:
                        continue
                    if val > 1000:
                        raise ExtractorError(
                            f"un-banded numeric value {val} in {path.name} "
                            f"row {row_num} column {col!r}; per-record dollars "
                            "MUST flow through amount_to_band"
                        )


def all_revenue_bands() -> tuple[str, ...]:
    """Re-export so subclasses don't have to reach into safe_harbor."""
    return REVENUE_BANDS
