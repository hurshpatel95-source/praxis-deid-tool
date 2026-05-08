"""Structural checks on a MappingConfig produced by the Claude mapper.

The validator does NOT execute SQL — it can't, since the wizard has no
DB cursor in mapper-land by design. It only checks structural integrity:

  - Every required canonical column has a mapping (no silent gaps).
  - Every source-table reference in any source_expression points at a
    table that exists in the PmsSchema.
  - Every join in `join_graph` references real tables/columns.
  - Enum-valued canonical columns have transformations.
  - Confidence scores are well-formed (0..1) and consistency-checked
    (low column confidence -> needs_review=true).
  - Foreign-key columns reference valid source tables.

Validation outputs are `ValidationIssue` objects with severity. The CLI
treats `error` as blocking and `warning` as advisory; the human-approval
flow lets the practice override warnings but not errors.

Limitations: SQL expressions are matched by case-insensitive identifier
extraction (a `\\w+\\.\\w+` regex). This is good enough for typical
mappings — `treatplan.PlanNum`, `claim.ClaimNum`, etc. — and explicitly
not a full SQL parser. Practices with exotic expressions can override
warnings during human approval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .canonical_schemas import CanonicalSchema, load_canonical_schemas
from .claude_mapper import MappingConfig
from .schema_reader import PmsSchema


class ValidationSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class ValidationIssue:
    severity: ValidationSeverity
    canonical_schema: str
    canonical_column: str | None  # None for schema-level issues
    code: str
    message: str

    def is_blocking(self) -> bool:
        return self.severity == ValidationSeverity.ERROR


# -----------------------------------------------------------------------
# Source-expression analysis
# -----------------------------------------------------------------------

# Identifier-pair regex: matches `tablename.columnname` references in
# any SQL fragment. Backticks and double-quotes are stripped via the
# preprocessor below before the regex runs.
_TABLE_COLUMN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\b")
# Stand-alone identifier (e.g. "NULL", "TRUE", "FALSE", "CASE").
_SQL_KEYWORDS: frozenset[str] = frozenset(
    {
        "NULL",
        "TRUE",
        "FALSE",
        "CASE",
        "WHEN",
        "THEN",
        "ELSE",
        "END",
        "AND",
        "OR",
        "NOT",
        "IN",
        "IS",
        "LIKE",
        "BETWEEN",
        "COALESCE",
        "CAST",
        "AS",
        "DATE",
        "DATETIME",
        "INTEGER",
        "VARCHAR",
        "DECIMAL",
        "DISTINCT",
        "UNION",
        "ALL",
    }
)


def _strip_quotes(expr: str) -> str:
    """Remove backticks, double-quotes, and string literals so the
    table.column regex doesn't match identifiers inside string constants."""
    # Drop string literals 'foo bar' and 'it''s' — replace with '?'.
    no_strings = re.sub(r"'([^']|'')*'", "''", expr)
    # Drop backticks and double-quotes that wrap identifiers.
    return no_strings.replace("`", "").replace('"', "")


def _extract_table_refs(expr: str) -> set[tuple[str, str]]:
    """Return the set of (table, column) pairs referenced in expr."""
    cleaned = _strip_quotes(expr)
    return {
        (m.group(1), m.group(2))
        for m in _TABLE_COLUMN_RE.finditer(cleaned)
    }


def _is_null_expression(expr: str) -> bool:
    """A source_expression of just NULL signals 'not mappable'.

    The validator treats this as a non-error condition (the canonical
    field stays empty downstream); the human-approval UI surfaces it
    so the reviewer can decide whether it's acceptable for the metric.
    """
    return _strip_quotes(expr).strip().upper() in {"NULL", "''", "NONE"}


# -----------------------------------------------------------------------
# Validators
# -----------------------------------------------------------------------

def validate_mapping(
    mapping: MappingConfig,
    *,
    pms_schema: PmsSchema,
    canonical_schema: CanonicalSchema | None = None,
) -> list[ValidationIssue]:
    """Validate a single MappingConfig against its canonical spec + the
    source PMS schema. Returns a list of ValidationIssues, possibly empty.

    `canonical_schema`, if not provided, is looked up by name from the
    registry. If the canonical_schema name in the mapping isn't a known
    canonical schema, that itself is the only issue returned.
    """
    issues: list[ValidationIssue] = []

    if canonical_schema is None:
        canonical_schema = _lookup_canonical(mapping.canonical_schema)
        if canonical_schema is None:
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    canonical_schema=mapping.canonical_schema,
                    canonical_column=None,
                    code="unknown_canonical_schema",
                    message=(
                        f"mapping references unknown canonical schema "
                        f"{mapping.canonical_schema!r}"
                    ),
                )
            )
            return issues

    issues.extend(_check_required_columns(mapping, canonical_schema))
    issues.extend(_check_source_table_refs(mapping, pms_schema))
    issues.extend(_check_enum_transformations(mapping, canonical_schema))
    issues.extend(_check_confidence_consistency(mapping))
    issues.extend(_check_join_graph(mapping, pms_schema))
    issues.extend(_check_extra_columns(mapping, canonical_schema))
    return issues


def validate_mappings(
    mappings: list[MappingConfig], *, pms_schema: PmsSchema
) -> list[ValidationIssue]:
    """Validate every mapping in a wizard run."""
    all_issues: list[ValidationIssue] = []
    for mapping in mappings:
        all_issues.extend(validate_mapping(mapping, pms_schema=pms_schema))
    return all_issues


def _lookup_canonical(name: str) -> CanonicalSchema | None:
    for s in load_canonical_schemas():
        if s.name == name:
            return s
    return None


def _check_required_columns(
    mapping: MappingConfig, canonical: CanonicalSchema
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for col in canonical.columns:
        if not col.required:
            continue
        m = mapping.column_mappings.get(col.name)
        if m is None:
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    canonical_schema=canonical.name,
                    canonical_column=col.name,
                    code="required_column_missing",
                    message=(
                        f"required canonical column {col.name!r} has no mapping"
                    ),
                )
            )
            continue
        if _is_null_expression(m.source_expression):
            # A required column intentionally NULL'd is a hard problem.
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    canonical_schema=canonical.name,
                    canonical_column=col.name,
                    code="required_column_null",
                    message=(
                        f"required column {col.name!r} mapped to NULL — "
                        "the canonical extension cannot ship without this column"
                    ),
                )
            )
    return issues


def _check_extra_columns(
    mapping: MappingConfig, canonical: CanonicalSchema
) -> list[ValidationIssue]:
    """Flag mappings for columns that aren't in the canonical schema."""
    canonical_names = {c.name for c in canonical.columns}
    issues: list[ValidationIssue] = []
    for name in mapping.column_mappings:
        if name not in canonical_names:
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    canonical_schema=canonical.name,
                    canonical_column=name,
                    code="extra_column",
                    message=(
                        f"mapping defines column {name!r} which is not in the "
                        f"{canonical.name!r} canonical schema; will be ignored "
                        "downstream"
                    ),
                )
            )
    return issues


def _check_source_table_refs(
    mapping: MappingConfig, pms_schema: PmsSchema
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    known_tables = {name.lower(): name for name in pms_schema.tables}
    for col_name, m in mapping.column_mappings.items():
        if _is_null_expression(m.source_expression):
            continue
        refs = _extract_table_refs(m.source_expression)
        for table, column in refs:
            if table.upper() in _SQL_KEYWORDS:
                continue
            actual_table = known_tables.get(table.lower())
            if actual_table is None:
                issues.append(
                    ValidationIssue(
                        severity=ValidationSeverity.ERROR,
                        canonical_schema=mapping.canonical_schema,
                        canonical_column=col_name,
                        code="unknown_table",
                        message=(
                            f"source_expression for {col_name!r} references "
                            f"unknown table {table!r}"
                        ),
                    )
                )
                continue
            cols = {c.name.lower() for c in pms_schema.tables[actual_table].columns}
            if column.lower() not in cols:
                issues.append(
                    ValidationIssue(
                        severity=ValidationSeverity.ERROR,
                        canonical_schema=mapping.canonical_schema,
                        canonical_column=col_name,
                        code="unknown_column",
                        message=(
                            f"source_expression for {col_name!r} references "
                            f"{table}.{column} but column doesn't exist "
                            f"in source table {table!r}"
                        ),
                    )
                )
    return issues


def _check_enum_transformations(
    mapping: MappingConfig, canonical: CanonicalSchema
) -> list[ValidationIssue]:
    """Enum-typed canonical columns should have a transformation OR a
    direct source_expression that references a single source column —
    we can't enforce the latter, but we can flag missing transformations
    where the source_expression looks like a raw column ref (not a CASE).
    """
    issues: list[ValidationIssue] = []
    for col in canonical.columns:
        if col.type != "enum":
            continue
        m = mapping.column_mappings.get(col.name)
        if m is None or _is_null_expression(m.source_expression):
            continue
        has_transform = col.name in mapping.transformations
        looks_like_case = "CASE" in m.source_expression.upper()
        if not has_transform and not looks_like_case:
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    canonical_schema=canonical.name,
                    canonical_column=col.name,
                    code="enum_no_transformation",
                    message=(
                        f"enum column {col.name!r} has a direct source "
                        "expression but no transformation; verify source "
                        f"values match canonical enum {sorted(col.enum_values or ())}"
                    ),
                )
            )
    return issues


def _check_confidence_consistency(mapping: MappingConfig) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    # Top-level confidence range.
    if not 0.0 <= mapping.confidence <= 1.0:
        issues.append(
            ValidationIssue(
                severity=ValidationSeverity.ERROR,
                canonical_schema=mapping.canonical_schema,
                canonical_column=None,
                code="confidence_out_of_range",
                message=(
                    f"top-level confidence {mapping.confidence} is outside [0,1]"
                ),
            )
        )

    # Per-column consistency.
    for col_name, m in mapping.column_mappings.items():
        if not 0.0 <= m.confidence <= 1.0:
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    canonical_schema=mapping.canonical_schema,
                    canonical_column=col_name,
                    code="column_confidence_out_of_range",
                    message=(
                        f"column confidence {m.confidence} for {col_name!r} "
                        "is outside [0,1]"
                    ),
                )
            )
            continue
        # Low confidence MUST set needs_review.
        if m.confidence < 0.7 and not m.needs_review:
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    canonical_schema=mapping.canonical_schema,
                    canonical_column=col_name,
                    code="low_confidence_without_review",
                    message=(
                        f"column {col_name!r} has confidence {m.confidence:.2f} "
                        "but needs_review=false; expected needs_review=true "
                        "for confidence < 0.7"
                    ),
                )
            )
    return issues


def _check_join_graph(
    mapping: MappingConfig, pms_schema: PmsSchema
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    known_tables = {name.lower(): name for name in pms_schema.tables}
    for join in mapping.join_graph:
        for side in ("left", "right"):
            table = getattr(join, f"{side}_table")
            column = getattr(join, f"{side}_column")
            actual_table = known_tables.get(table.lower())
            if actual_table is None:
                issues.append(
                    ValidationIssue(
                        severity=ValidationSeverity.ERROR,
                        canonical_schema=mapping.canonical_schema,
                        canonical_column=None,
                        code="join_unknown_table",
                        message=(
                            f"join_graph entry references unknown {side}_table "
                            f"{table!r}"
                        ),
                    )
                )
                continue
            table_cols = {c.name.lower() for c in pms_schema.tables[actual_table].columns}
            if column.lower() not in table_cols:
                issues.append(
                    ValidationIssue(
                        severity=ValidationSeverity.ERROR,
                        canonical_schema=mapping.canonical_schema,
                        canonical_column=None,
                        code="join_unknown_column",
                        message=(
                            f"join_graph entry references {table}.{column} "
                            f"but column doesn't exist in source table {table!r}"
                        ),
                    )
                )
    return issues


def issues_summary(issues: list[ValidationIssue]) -> dict[str, int]:
    """Convenience for the CLI."""
    counts = {s.value: 0 for s in ValidationSeverity}
    for issue in issues:
        counts[issue.severity.value] += 1
    return counts


__all__ = [
    "ValidationIssue",
    "ValidationSeverity",
    "issues_summary",
    "validate_mapping",
    "validate_mappings",
]
