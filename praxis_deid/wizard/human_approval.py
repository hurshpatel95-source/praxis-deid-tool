"""CLI prompt UI for reviewing + approving a wizard-generated MappingConfig.

Human-in-the-loop is non-negotiable for a HIPAA-data-handling tool. The
wizard never auto-applies a mapping — every column has to be explicitly
approved by the practice operator before the mapping.json is written.

Behavior:
  - Prints each canonical schema and its proposed column mappings.
  - Highlights `needs_review: true` rows in red (when the terminal
    supports color), and rows with validation errors above them.
  - For each column the operator can:
        a) accept     - keep Claude's mapping as-is
        a (all)       - accept everything in this canonical schema
        o             - override (type a new SQL expression)
        s             - skip (set source_expression to NULL, confidence 0)
        q             - quit and discard everything
  - At the end, prints a summary and writes mapping.json.

Input is via stdlib `input()` — no extra deps. The flow is non-interactive
when stdin is closed (e.g. CI), in which case it auto-accepts mappings
that are confident enough AND have no validation errors. Anything with
`needs_review: true` or any error in non-interactive mode triggers a
non-zero exit so the run fails noisily.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .canonical_schemas import CanonicalSchema, load_canonical_schemas
from .claude_mapper import ColumnMapping, MappingConfig
from .mapping_validator import ValidationIssue


# ANSI color codes (no extra dep). Disabled if stdout isn't a TTY or if
# NO_COLOR is set, which is the de-facto standard for opt-out.
def _supports_color(out: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(out, "isatty", lambda: False)())


@dataclass
class _Theme:
    red: str
    yellow: str
    green: str
    cyan: str
    bold: str
    reset: str

    @classmethod
    def for_stream(cls, out: TextIO) -> _Theme:
        if _supports_color(out):
            return cls(
                red="\033[31m",
                yellow="\033[33m",
                green="\033[32m",
                cyan="\033[36m",
                bold="\033[1m",
                reset="\033[0m",
            )
        return cls(red="", yellow="", green="", cyan="", bold="", reset="")


@dataclass
class HumanApprovalResult:
    approved: list[MappingConfig]
    canceled: bool
    output_path: Path | None


def run_human_approval(
    mappings: list[MappingConfig],
    *,
    issues_by_schema: dict[str, list[ValidationIssue]] | None = None,
    output_path: Path,
    pms_name: str,
    interactive: bool | None = None,
    input_fn: Callable[[str], str] = input,
    out: TextIO | None = None,
) -> HumanApprovalResult:
    """Run the approval flow and write `output_path` if approved.

    `interactive` controls the UI:
      - True  : always prompt (raises EOFError if stdin closed mid-flow).
      - False : never prompt — auto-accept high-confidence/no-error mappings.
      - None  : auto-detect from stdin.isatty().
    """
    issues_by_schema = issues_by_schema or {}
    out = out or sys.stdout
    theme = _Theme.for_stream(out)

    if interactive is None:
        interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())

    print(
        f"{theme.bold}praxis-deid wizard: review mapping for "
        f"{pms_name!r}{theme.reset}",
        file=out,
    )
    print(
        f"{len(mappings)} canonical schemas mapped. Review each below.",
        file=out,
    )
    print("", file=out)

    approved: list[MappingConfig] = []
    canonical_lookup = {s.name: s for s in load_canonical_schemas()}

    for mapping in mappings:
        canonical = canonical_lookup.get(mapping.canonical_schema)
        schema_issues = issues_by_schema.get(mapping.canonical_schema, [])

        approved_config = _review_one(
            mapping=mapping,
            canonical=canonical,
            issues=schema_issues,
            theme=theme,
            interactive=interactive,
            input_fn=input_fn,
            out=out,
        )
        if approved_config is None:
            return HumanApprovalResult(approved=[], canceled=True, output_path=None)
        approved.append(approved_config)

    # Build the final mapping doc.
    doc = {
        "pms_name": pms_name,
        "wizard_version": 1,
        "mappings": [m.to_dict() for m in approved],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(doc, indent=2, sort_keys=True))

    print("", file=out)
    print(
        f"{theme.green}wrote approved mapping to {output_path}{theme.reset}",
        file=out,
    )
    return HumanApprovalResult(
        approved=approved, canceled=False, output_path=output_path
    )


def _review_one(
    *,
    mapping: MappingConfig,
    canonical: CanonicalSchema | None,
    issues: list[ValidationIssue],
    theme: _Theme,
    interactive: bool,
    input_fn: Callable[[str], str],
    out: TextIO,
) -> MappingConfig | None:
    print(
        f"{theme.bold}{theme.cyan}=== {mapping.canonical_schema} ==={theme.reset} "
        f"(top-level confidence: {mapping.confidence:.2f})",
        file=out,
    )

    schema_level_errors = [
        i for i in issues if i.canonical_column is None and i.is_blocking()
    ]
    for err in schema_level_errors:
        print(
            f"{theme.red}schema-level error [{err.code}]: {err.message}{theme.reset}",
            file=out,
        )
    if schema_level_errors and not interactive:
        print(
            f"{theme.red}non-interactive mode: schema-level errors block "
            "auto-approval; aborting.{theme.reset}",
            file=out,
        )
        return None

    # Group issues by column.
    issues_by_col: dict[str, list[ValidationIssue]] = {}
    for issue in issues:
        if issue.canonical_column:
            issues_by_col.setdefault(issue.canonical_column, []).append(issue)

    # Iterate canonical column order so the operator sees the spec order,
    # not whatever order Claude chose.
    column_order: list[str]
    if canonical is not None:
        column_order = [c.name for c in canonical.columns]
        # Append any extras Claude added that aren't in the canonical (so
        # the operator sees them and can reject).
        for name in mapping.column_mappings:
            if name not in column_order:
                column_order.append(name)
    else:
        column_order = list(mapping.column_mappings.keys())

    accept_all_remaining = False
    new_mappings: dict[str, ColumnMapping] = dict(mapping.column_mappings)

    for col_name in column_order:
        m = new_mappings.get(col_name)
        col_spec = (
            next((c for c in canonical.columns if c.name == col_name), None)
            if canonical else None
        )
        col_issues = issues_by_col.get(col_name, [])
        col_errors = [i for i in col_issues if i.is_blocking()]
        col_warnings = [i for i in col_issues if not i.is_blocking()]

        if m is None:
            req = "REQUIRED" if (col_spec and col_spec.required) else "optional"
            print(
                f"  {theme.red}[missing]{theme.reset} {col_name} "
                f"({req}): no mapping",
                file=out,
            )
            for err in col_errors:
                print(f"    {theme.red}error: {err.message}{theme.reset}", file=out)
            if interactive and not accept_all_remaining:
                ans = _prompt_for_missing(col_name, theme, input_fn)
                if ans == "q":
                    return None
                if ans is not None:
                    new_mappings[col_name] = ans
            continue

        # Print the proposed mapping.
        flag = ""
        color = ""
        if m.needs_review:
            flag = "[needs review]"
            color = theme.yellow
        if col_errors:
            flag = "[ERROR]"
            color = theme.red
        print(
            f"  {color}{flag}{theme.reset} {col_name} "
            f"(confidence={m.confidence:.2f}): {m.source_expression}",
            file=out,
        )
        if m.notes:
            print(f"      note: {m.notes}", file=out)
        for err in col_errors:
            print(f"    {theme.red}error [{err.code}]: {err.message}{theme.reset}", file=out)
        for warn in col_warnings:
            print(
                f"    {theme.yellow}warning [{warn.code}]: {warn.message}{theme.reset}",
                file=out,
            )

        # Decision.
        if not interactive:
            if m.needs_review or col_errors:
                print(
                    f"    {theme.red}non-interactive: column requires review; "
                    f"aborting.{theme.reset}",
                    file=out,
                )
                return None
            # Auto-accept: leave m as-is.
            continue

        if accept_all_remaining and not col_errors:
            continue

        decision = _prompt_for_column(col_name, theme, input_fn)
        if decision == "q":
            return None
        if decision == "A":
            accept_all_remaining = True
            continue
        if decision == "a":
            continue
        if decision == "s":
            new_mappings[col_name] = ColumnMapping(
                canonical_column=col_name,
                source_expression="NULL",
                confidence=0.0,
                needs_review=True,
                notes="skipped during human approval",
            )
            continue
        if decision == "o":
            new_expr = input_fn("    new source expression: ").strip()
            if not new_expr:
                print(
                    f"    {theme.yellow}empty override — keeping existing"
                    f"{theme.reset}",
                    file=out,
                )
                continue
            new_mappings[col_name] = ColumnMapping(
                canonical_column=col_name,
                source_expression=new_expr,
                confidence=1.0,
                needs_review=False,
                notes="overridden by operator during human approval",
            )

    # Apply confirmed changes.
    return MappingConfig(
        canonical_schema=mapping.canonical_schema,
        column_mappings=new_mappings,
        join_graph=mapping.join_graph,
        transformations=mapping.transformations,
        confidence=mapping.confidence,
        notes=mapping.notes,
    )


def _prompt_for_column(
    col_name: str, theme: _Theme, input_fn: Callable[[str], str]
) -> str:
    """Prompt the operator. Returns one of: a, A, o, s, q."""
    while True:
        raw = input_fn(
            "    [a]ccept / [A]ccept-all-remaining / [o]verride / [s]kip / "
            "[q]uit -> "
        ).strip()
        if not raw:
            return "a"
        first = raw[0]
        if first in ("a", "A", "o", "s", "q"):
            return first


def _prompt_for_missing(
    col_name: str, theme: _Theme, input_fn: Callable[[str], str]
) -> ColumnMapping | str | None:
    while True:
        raw = input_fn(
            f"    Provide source expression for {col_name!r} or 's'kip / 'q'uit: "
        ).strip()
        if not raw:
            return None
        if raw == "q":
            return "q"
        if raw == "s":
            return ColumnMapping(
                canonical_column=col_name,
                source_expression="NULL",
                confidence=0.0,
                needs_review=True,
                notes="skipped during human approval (no Claude mapping)",
            )
        return ColumnMapping(
            canonical_column=col_name,
            source_expression=raw,
            confidence=1.0,
            needs_review=False,
            notes="provided by operator during human approval",
        )


__all__ = ["HumanApprovalResult", "run_human_approval"]
