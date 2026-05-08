"""`praxis-deid wizard` subcommand — orchestrates a wizard run.

Wires the four wizard modules together:

    schema_reader -> claude_mapper -> mapping_validator -> human_approval

This is the only file in `wizard/` that prints to stdout outside of the
human_approval flow. Errors go to stderr and return non-zero exit codes
so wrappers (Make, CI, Ansible playbooks) can detect failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .canonical_schemas import CANONICAL_SCHEMAS
from .claude_mapper import (
    DEFAULT_MODEL,
    ClaudeMapper,
    PhiDetectedError,
)
from .human_approval import run_human_approval
from .mapping_validator import (
    ValidationSeverity,
    issues_summary,
    validate_mappings,
)
from .schema_reader import read_pms_schema


def add_wizard_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Attach the wizard subcommand to the main `praxis-deid` parser.

    Called from `praxis_deid/cli.py`.
    """
    wizard = subparsers.add_parser(
        "wizard",
        help="Schema-mapping setup wizard for new PMS connections",
        description=(
            "Runs the Claude-API-assisted PMS-to-canonical schema mapper. "
            "Reads SCHEMA METADATA ONLY from the source PMS — never row "
            "data — and produces a mapping.json the de-id tool consumes "
            "to extract the 6 canonical CSVs."
        ),
    )
    wizard_sub = wizard.add_subparsers(dest="wizard_cmd", required=True)

    run = wizard_sub.add_parser(
        "run",
        help="Run a wizard mapping cycle",
        description=(
            "Read the source PMS schema, ask Claude to propose a mapping "
            "to the 6 canonical CSVs, run structural validation, prompt "
            "the operator for approval, and write mapping.json."
        ),
    )

    src = run.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--schema-file",
        type=Path,
        help="Path to a PmsSchema JSON dump (preferred for offline review)",
    )
    src.add_argument(
        "--sql-dump",
        type=Path,
        help="Path to a DDL-only SQL dump file (no row data)",
    )

    run.add_argument(
        "--pms",
        required=False,
        help=(
            "PMS short name (e.g. open_dental, dentrix). Required for "
            "--sql-dump; optional for --schema-file (read from the JSON)."
        ),
    )
    run.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Where to write the approved mapping.json",
    )
    run.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Claude model id (default: {DEFAULT_MODEL})",
    )
    run.add_argument(
        "--recorded-response",
        type=Path,
        help=(
            "Replay a previously-recorded Claude response from JSON "
            "(used in tests; skips the live API call)"
        ),
    )
    run.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Skip the prompt-based approval flow. Auto-approves only if "
            "all mappings have confidence >= 0.7, no needs_review flags, "
            "and no validation errors."
        ),
    )

    list_schemas = wizard_sub.add_parser(
        "list-schemas",
        help="Print the 6 canonical schemas the wizard maps to",
    )
    list_schemas.add_argument(
        "--json", action="store_true", help="Emit JSON instead of human-readable"
    )

    run.set_defaults(_handler=_handle_run)
    list_schemas.set_defaults(_handler=_handle_list_schemas)


def dispatch(args: argparse.Namespace) -> int:
    """Dispatch a parsed wizard subcommand. Called by the main CLI."""
    handler = getattr(args, "_handler", None)
    if handler is None:
        print("error: unknown wizard subcommand", file=sys.stderr)
        return 2
    return handler(args)


# -----------------------------------------------------------------------
# Handlers
# -----------------------------------------------------------------------

def _handle_run(args: argparse.Namespace) -> int:
    # 1. Read source PMS schema (metadata only).
    try:
        if args.schema_file is not None:
            pms_schema = read_pms_schema(schema_file=args.schema_file)
            pms_name = args.pms or pms_schema.pms_name
        else:
            if not args.pms:
                print(
                    "error: --pms is required when using --sql-dump",
                    file=sys.stderr,
                )
                return 2
            pms_schema = read_pms_schema(sql_dump=args.sql_dump, pms_name=args.pms)
            pms_name = args.pms
    except FileNotFoundError as err:
        print(f"error: schema source not found: {err}", file=sys.stderr)
        return 2
    except ValueError as err:
        print(f"error: schema source invalid: {err}", file=sys.stderr)
        return 2

    print(
        f"praxis-deid wizard: read {len(pms_schema.tables)} tables "
        f"from {pms_name!r}",
        file=sys.stderr,
    )

    # 2. Ask Claude for a mapping. PhiGuard runs inside ClaudeMapper.
    mapper = ClaudeMapper(
        model=args.model,
        recorded_response_path=args.recorded_response,
    )
    try:
        mappings = mapper.map_schema(pms_schema, canonical_schemas=CANONICAL_SCHEMAS)
    except PhiDetectedError as err:
        print(f"error: PhiGuard refused payload: {err}", file=sys.stderr)
        return 3
    except RuntimeError as err:
        # Most commonly: missing ANTHROPIC_API_KEY.
        print(f"error: Claude API call failed: {err}", file=sys.stderr)
        return 4
    except Exception as err:  # pragma: no cover - network errors
        print(f"error: Claude mapping failed: {err}", file=sys.stderr)
        return 4

    # Print Anthropic token usage so the operator sees the cost.
    if mapper.last_input_tokens or mapper.last_output_tokens:
        print(
            f"praxis-deid wizard: Claude usage — "
            f"{mapper.last_input_tokens} input tokens, "
            f"{mapper.last_output_tokens} output tokens",
            file=sys.stderr,
        )

    # 3. Validate.
    issues = validate_mappings(mappings, pms_schema=pms_schema)
    summary = issues_summary(issues)
    print(
        "praxis-deid wizard: validation — "
        f"{summary[ValidationSeverity.ERROR.value]} errors, "
        f"{summary[ValidationSeverity.WARNING.value]} warnings, "
        f"{summary[ValidationSeverity.INFO.value]} info",
        file=sys.stderr,
    )

    issues_by_schema: dict[str, list] = {}
    for issue in issues:
        issues_by_schema.setdefault(issue.canonical_schema, []).append(issue)

    # 4. Human approval (or auto-approve in non-interactive mode).
    result = run_human_approval(
        mappings,
        issues_by_schema=issues_by_schema,
        output_path=args.output,
        pms_name=pms_name,
        interactive=False if args.non_interactive else None,
    )

    if result.canceled:
        print("praxis-deid wizard: canceled; no mapping written.", file=sys.stderr)
        return 5

    return 0


def _handle_list_schemas(args: argparse.Namespace) -> int:
    if args.json:
        print(
            json.dumps(
                [s.to_prompt_dict() for s in CANONICAL_SCHEMAS],
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    for schema in CANONICAL_SCHEMAS:
        print(f"=== Extension {schema.extension_letter}: {schema.name} ===")
        print(f"   {schema.description}")
        if schema.extends:
            print(f"   (extends {schema.extends})")
        for col in schema.columns:
            req = "required" if col.required else "optional"
            print(f"   - {col.name}: {col.type} ({req}) — {col.description.splitlines()[0]}")
        print("")
    return 0


__all__ = ["add_wizard_subparser", "dispatch"]


# Allow `python -m praxis_deid.wizard.cli ...` for development convenience.
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="praxis-deid-wizard")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add_wizard_subparser(sub)
    args = parser.parse_args(argv)
    return dispatch(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
