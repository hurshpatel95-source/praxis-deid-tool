"""Wizard-1 validation gate: live wizard run vs Open Dental ground truth.

Runs the wizard against the bundled Open Dental schema fixture and diffs
the output column-by-column against the audited ground truth in
`tests/fixtures/expected_mapping_open_dental.json`.

Two modes:
    --live       Calls Anthropic API. Requires ANTHROPIC_API_KEY.
                 Costs ~$0.07 per run.
    --replay     Uses the recorded fixture in
                 tests/fixtures/anthropic_responses/. No network. Free.
                 (Default — safe to run anywhere.)

Output: a JSON report at /tmp/wizard_validation_report.json (or --output)
plus a stdout summary. Exit code 0 if the gate passes, 1 if it fails, 2
if the wizard itself errored out.

Gate criteria (all must pass):
    1. Every canonical schema in ground truth has a matching mapping in
       wizard output.
    2. ≥95% of columns share the same source TABLE between live and
       ground truth (parsed from `source_expression`).
    3. 100% of high-confidence ground-truth columns (gt confidence ≥ 0.9)
       have an exact `source_expression` match.
    4. ≥80% of columns share the same `needs_review` flag.
    5. No critical-error tokens in the wizard's output (e.g. the known
       `claimproc.Status = 2` mistake — the gate flags this explicitly).

Usage:
    python3 scripts/validate_wizard.py --replay
    python3 scripts/validate_wizard.py --live
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SCHEMA_FIXTURE = REPO / "tests/fixtures/open_dental_schema.json"
RECORDED_FIXTURE = REPO / "tests/fixtures/anthropic_responses/open_dental_treatment_plans.json"
GROUND_TRUTH = REPO / "tests/fixtures/expected_mapping_open_dental.json"

# Critical-error markers that indicate a known wrong mapping in the live
# output. Each entry is (regex, schema, column, description). The gate
# fails if any wizard output triggers one of these.
CRITICAL_ERROR_MARKERS: list[tuple[re.Pattern[str], str, str, str]] = [
    (
        re.compile(r"claimproc\.Status\s*=\s*2"),
        "claims_raw",
        "payment_date",
        "claimproc.Status=2 is Preauth, not Received. Use Status=1.",
    ),
]

# Gate thresholds.
TABLE_AGREEMENT_MIN = 0.95
HIGH_CONF_EXACT_MIN = 1.00
NEEDS_REVIEW_AGREEMENT_MIN = 0.80


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

_TABLE_RE = re.compile(r"\b([a-z][a-z_]+)\.\w+", re.IGNORECASE)


def extract_source_tables(expression: str) -> set[str]:
    """Pull the source-table names referenced by a source_expression.

    Handles: `treatplan.PlanNum`, joined CASE expressions, sub-SELECTs,
    UNION'd branches. Returns lowercased table names. Excludes obvious
    non-tables (e.g. SQL keywords accidentally matched).
    """
    if not expression or expression.strip().upper() in {"NULL", ""}:
        return set()
    matches = _TABLE_RE.findall(expression)
    sql_keywords = {
        "case", "when", "then", "else", "end", "and", "or", "not", "in",
        "is", "null", "select", "from", "where", "group", "by", "having",
        "order", "limit", "as", "on", "join", "left", "right", "inner",
        "outer", "full", "cross", "union", "all", "distinct", "exists",
        "between", "like", "interval", "date", "datetime", "timestamp",
    }
    return {m.lower() for m in matches if m.lower() not in sql_keywords}


@dataclass
class ColumnDiff:
    schema: str
    column: str
    gt_expression: str
    wz_expression: str
    gt_confidence: float
    wz_confidence: float
    gt_needs_review: bool
    wz_needs_review: bool
    table_match: bool
    exact_match: bool
    needs_review_match: bool
    notes: list[str] = field(default_factory=list)


@dataclass
class ValidationReport:
    mode: str
    schemas_in_gt: int
    schemas_in_wz: int
    schemas_missing_in_wz: list[str] = field(default_factory=list)
    schemas_extra_in_wz: list[str] = field(default_factory=list)
    column_diffs: list[ColumnDiff] = field(default_factory=list)
    critical_errors: list[dict[str, str]] = field(default_factory=list)
    passed: bool = False

    def summary_metrics(self) -> dict[str, Any]:
        total = len(self.column_diffs)
        if total == 0:
            return {
                "total_columns": 0,
                "table_agreement": 0.0,
                "exact_match": 0.0,
                "needs_review_agreement": 0.0,
                "high_confidence_exact_match": 0.0,
            }
        table_match = sum(1 for d in self.column_diffs if d.table_match)
        exact = sum(1 for d in self.column_diffs if d.exact_match)
        nr = sum(1 for d in self.column_diffs if d.needs_review_match)
        high_conf = [d for d in self.column_diffs if d.gt_confidence >= 0.9]
        high_conf_exact = sum(1 for d in high_conf if d.exact_match)
        return {
            "total_columns": total,
            "table_agreement": table_match / total,
            "exact_match": exact / total,
            "needs_review_agreement": nr / total,
            "high_confidence_columns": len(high_conf),
            "high_confidence_exact_match": (
                high_conf_exact / len(high_conf) if high_conf else 1.0
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "passed": self.passed,
            "schemas_in_gt": self.schemas_in_gt,
            "schemas_in_wz": self.schemas_in_wz,
            "schemas_missing_in_wz": self.schemas_missing_in_wz,
            "schemas_extra_in_wz": self.schemas_extra_in_wz,
            "metrics": self.summary_metrics(),
            "thresholds": {
                "table_agreement_min": TABLE_AGREEMENT_MIN,
                "high_conf_exact_min": HIGH_CONF_EXACT_MIN,
                "needs_review_agreement_min": NEEDS_REVIEW_AGREEMENT_MIN,
            },
            "critical_errors": self.critical_errors,
            "column_diffs": [
                {
                    "schema": d.schema,
                    "column": d.column,
                    "gt_expression": d.gt_expression,
                    "wz_expression": d.wz_expression,
                    "gt_confidence": d.gt_confidence,
                    "wz_confidence": d.wz_confidence,
                    "gt_needs_review": d.gt_needs_review,
                    "wz_needs_review": d.wz_needs_review,
                    "table_match": d.table_match,
                    "exact_match": d.exact_match,
                    "needs_review_match": d.needs_review_match,
                    "notes": d.notes,
                }
                for d in self.column_diffs
            ],
        }


# -------------------------------------------------------------------------
# Diff engine
# -------------------------------------------------------------------------


def diff_mappings(
    gt_mappings: list[dict],
    wz_mappings: list[dict],
    mode: str,
) -> ValidationReport:
    gt_by_schema = {m["canonical_schema"]: m for m in gt_mappings}
    wz_by_schema = {m["canonical_schema"]: m for m in wz_mappings}
    report = ValidationReport(
        mode=mode,
        schemas_in_gt=len(gt_by_schema),
        schemas_in_wz=len(wz_by_schema),
        schemas_missing_in_wz=sorted(set(gt_by_schema) - set(wz_by_schema)),
        schemas_extra_in_wz=sorted(set(wz_by_schema) - set(gt_by_schema)),
    )

    for schema_name in sorted(set(gt_by_schema) & set(wz_by_schema)):
        gt = gt_by_schema[schema_name]
        wz = wz_by_schema[schema_name]
        gt_cols = gt.get("column_mappings") or {}
        wz_cols = wz.get("column_mappings") or {}
        for col_name in sorted(set(gt_cols) | set(wz_cols)):
            gt_col = gt_cols.get(col_name) or {}
            wz_col = wz_cols.get(col_name) or {}
            gt_expr = (gt_col.get("source_expression") or "NULL").strip()
            wz_expr = (wz_col.get("source_expression") or "NULL").strip()
            gt_tables = extract_source_tables(gt_expr)
            wz_tables = extract_source_tables(wz_expr)
            table_match = (
                (not gt_tables and not wz_tables)
                or bool(gt_tables & wz_tables)
            )
            exact_match = gt_expr == wz_expr
            gt_nr = bool(gt_col.get("needs_review", False))
            wz_nr = bool(wz_col.get("needs_review", False))
            diff = ColumnDiff(
                schema=schema_name,
                column=col_name,
                gt_expression=gt_expr,
                wz_expression=wz_expr,
                gt_confidence=float(gt_col.get("confidence", 0.0)),
                wz_confidence=float(wz_col.get("confidence", 0.0)),
                gt_needs_review=gt_nr,
                wz_needs_review=wz_nr,
                table_match=table_match,
                exact_match=exact_match,
                needs_review_match=gt_nr == wz_nr,
            )
            if col_name not in gt_cols:
                diff.notes.append("EXTRA — column not in ground truth")
            elif col_name not in wz_cols:
                diff.notes.append("MISSING — column absent from wizard output")
            report.column_diffs.append(diff)

            for pat, p_schema, p_col, p_desc in CRITICAL_ERROR_MARKERS:
                if (
                    schema_name == p_schema
                    and col_name == p_col
                    and pat.search(wz_expr or "")
                ):
                    report.critical_errors.append({
                        "schema": schema_name,
                        "column": col_name,
                        "pattern": pat.pattern,
                        "description": p_desc,
                        "wz_expression": wz_expr,
                    })

    metrics = report.summary_metrics()
    report.passed = (
        not report.schemas_missing_in_wz
        and metrics["table_agreement"] >= TABLE_AGREEMENT_MIN
        and metrics["high_confidence_exact_match"] >= HIGH_CONF_EXACT_MIN
        and metrics["needs_review_agreement"] >= NEEDS_REVIEW_AGREEMENT_MIN
        and not report.critical_errors
    )
    return report


# -------------------------------------------------------------------------
# Wizard runner
# -------------------------------------------------------------------------


def run_wizard(*, mode: str) -> list[dict]:
    """Run the wizard and return the parsed mappings as a list of dicts."""
    from praxis_deid.wizard.canonical_schemas import load_canonical_schemas
    from praxis_deid.wizard.claude_mapper import ClaudeMapper, DEFAULT_MODEL
    from praxis_deid.wizard.schema_reader import read_pms_schema_from_json

    pms_schema = read_pms_schema_from_json(SCHEMA_FIXTURE)
    canonical = load_canonical_schemas()

    if mode == "replay":
        mapper = ClaudeMapper(
            model=DEFAULT_MODEL,
            recorded_response_path=RECORDED_FIXTURE,
        )
    else:  # live
        mapper = ClaudeMapper(model=DEFAULT_MODEL)

    configs = mapper.map_schema(pms_schema, canonical_schemas=canonical)
    return [c.to_dict() for c in configs]


# -------------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------------


def render_summary(report: ValidationReport) -> str:
    metrics = report.summary_metrics()
    lines = [
        f"=== Wizard-1 validation gate ({report.mode}) ===",
        f"schemas_in_gt:           {report.schemas_in_gt}",
        f"schemas_in_wizard_out:   {report.schemas_in_wz}",
    ]
    if report.schemas_missing_in_wz:
        lines.append(
            f"  MISSING from wizard: {', '.join(report.schemas_missing_in_wz)}"
        )
    if report.schemas_extra_in_wz:
        lines.append(
            f"  EXTRA in wizard:     {', '.join(report.schemas_extra_in_wz)}"
        )
    lines += [
        f"total_columns:           {metrics['total_columns']}",
        f"table_agreement:         {metrics['table_agreement']:.1%} "
        f"(threshold {TABLE_AGREEMENT_MIN:.0%})",
        f"exact_match:             {metrics['exact_match']:.1%}",
        f"high_confidence_exact:   {metrics['high_confidence_exact_match']:.1%} "
        f"(of {metrics['high_confidence_columns']} cols, threshold {HIGH_CONF_EXACT_MIN:.0%})",
        f"needs_review_agreement:  {metrics['needs_review_agreement']:.1%} "
        f"(threshold {NEEDS_REVIEW_AGREEMENT_MIN:.0%})",
    ]
    if report.critical_errors:
        lines.append("")
        lines.append(f"CRITICAL ERRORS: {len(report.critical_errors)}")
        for ce in report.critical_errors:
            lines.append(
                f"  - {ce['schema']}.{ce['column']}: {ce['description']}"
            )
            lines.append(f"      wizard wrote: {ce['wz_expression'][:120]}")
    lines += ["", "GATE: " + ("PASS" if report.passed else "FAIL")]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Wizard-1 validation gate")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--live", action="store_true",
        help="Call Anthropic API live. Requires ANTHROPIC_API_KEY.",
    )
    mode_group.add_argument(
        "--replay", action="store_true",
        help="Use recorded fixture (default).",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("/tmp/wizard_validation_report.json"),
        help="Where to write the JSON report (default /tmp/wizard_validation_report.json)",
    )
    args = parser.parse_args()

    mode = "live" if args.live else "replay"

    if not GROUND_TRUTH.exists():
        print(
            f"ERROR: ground truth missing at {GROUND_TRUTH}. "
            f"Run scripts/build_ground_truth.py first.",
            file=sys.stderr,
        )
        return 2

    gt_mappings = json.loads(GROUND_TRUTH.read_text())["mappings"]

    try:
        wz_mappings = run_wizard(mode=mode)
    except Exception as e:
        print(f"ERROR: wizard run failed: {e!r}", file=sys.stderr)
        return 2

    report = diff_mappings(gt_mappings, wz_mappings, mode=mode)
    args.output.write_text(json.dumps(report.to_dict(), indent=2) + "\n")
    print(render_summary(report))
    print(f"\nfull report: {args.output}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
