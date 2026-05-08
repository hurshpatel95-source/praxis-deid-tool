"""CLI dispatch for `praxis-deid extract ...`.

Wired into `praxis_deid/cli.py` via add_extract_subparser + dispatch.

Subcommand:

    praxis-deid extract --extension <A|B|C|D|E|F|all>
                        --connection <db-url>
                        --output <dir>
                        [--practice-id ...]
                        [--salt-env-var ...]
                        [--mapping-dir mappings/open_dental]
                        [--since YYYY-MM]
                        [--until YYYY-MM]
                        [--limit N]
                        [--fixture-json path.json]   # for tests / dry runs

Behaviour:

  * Loads the per-extension mapping config(s) from --mapping-dir.
  * Builds ONE Deidentifier (== one salt) shared across every extension
    in the run, so cross-extension patient HMACs match.
  * For each requested extension: connects to the PMS (if --connection
    is set), runs the extractor, writes the canonical CSV to
    <output>/<run_id>/<csv_name>, scans for un-banded $$$ leaks.
  * Emits a single audit log envelope summarising the run.

Dry-run / test mode:

  * `--fixture-json` takes a JSON file shaped like
        {
          "treatment_plans_raw": [{...row...}, ...],
          "claims_raw":          [{...row...}, ...],
          ...
        }
    The CLI uses this in place of any DB. This is how the smoke test
    `praxis-deid extract --extension all --fixture-json ...` works
    without a real Open Dental install.

  * `--connection` (when not paired with --fixture-json) attempts to
    import a DB driver lazily and raise a friendly error if missing.
    The driver path is documented in README so the practice's IT team
    can install only what they need.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..audit import get_tool_version, write_run_record
from ..deidentify import Deidentifier
from .base import (
    BaseExtractor,
    ExtractorError,
    Filter,
    assert_no_exact_dollars_in_csv,
    load_mapping_config,
)


# Canonical mapping: letter -> (canonical_schema_name, csv_filename, extractor_class).
def _extractor_registry() -> dict[str, tuple[str, str, Any]]:
    # Deferred imports so a single failed extractor module doesn't break
    # the whole CLI surface.
    from .extension_a_treatment_plans import TreatmentPlansExtractor
    from .extension_b_claims import ClaimsExtractor
    from .extension_c_schedule_capacity import CapacityExtractor
    from .extension_d_payments import PaymentsExtractor
    from .extension_e_timekeeping import TimekeepingExtractor
    from .extension_f_patients import PatientsExtensionExtractor

    return {
        "A": ("treatment_plans_raw", "treatment_plans_raw.csv", TreatmentPlansExtractor),
        "B": ("claims_raw", "claims_raw.csv", ClaimsExtractor),
        "C": ("schedule_capacity_raw", "schedule_capacity_raw.csv", CapacityExtractor),
        "D": ("payments_raw", "payments_raw.csv", PaymentsExtractor),
        "E": ("timekeeping_raw", "timekeeping_raw.csv", TimekeepingExtractor),
        "F": ("patients_raw_extension", "patients_extension.csv", PatientsExtensionExtractor),
    }


# Mapping config filenames per extension letter.
_MAPPING_FILENAMES = {
    "A": "A_treatment_plans_raw.json",
    "B": "B_claims_raw.json",
    "C": "C_schedule_capacity_raw.json",
    "D": "D_payments_raw.json",
    "E": "E_timekeeping_raw.json",
    "F": "F_patients_raw_extension.json",
}


def add_extract_subparser(sub: argparse._SubParsersAction[Any]) -> None:
    p = sub.add_parser(
        "extract",
        help="Pull data from a PMS and emit a Phase-C canonical CSV (Extensions A-F)",
        description=(
            "Practice-side extractor for Phase-C canonical CSVs. Reads the "
            "PMS database (e.g. Open Dental) per the hand-curated mapping "
            "config, applies HIPAA Safe Harbor de-id, and writes a canonical "
            "CSV per extension. Use --fixture-json for a dry run with "
            "synthetic rows (no DB needed)."
        ),
    )
    p.add_argument(
        "--extension",
        required=True,
        choices=("A", "B", "C", "D", "E", "F", "all"),
        help="Which extension to extract. 'all' runs A-F sequentially.",
    )
    p.add_argument(
        "--connection",
        default=None,
        help=(
            "DBAPI URL for the PMS, e.g. mysql+mysqlconnector://user:pwd@host:3306/opendental. "
            "Mutually exclusive with --fixture-json."
        ),
    )
    p.add_argument(
        "--fixture-json",
        type=Path,
        default=None,
        help=(
            "Path to a JSON file with pre-fetched rows keyed by canonical "
            "schema name. Used for tests and dry runs. Mutually exclusive "
            "with --connection."
        ),
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Directory to write canonical CSVs into. Defaults to "
            "~/.praxis-deid/output/<run_id>/."
        ),
    )
    p.add_argument(
        "--practice-id",
        default=None,
        help=(
            "Practice UUID. If omitted, read from PRAXIS_PRACTICE_ID env var. "
            "MUST be at least 8 chars."
        ),
    )
    p.add_argument(
        "--salt-env-var",
        default="PRAXIS_DEID_SALT",
        help="Environment variable name holding the practice salt (>=32 chars).",
    )
    p.add_argument(
        "--mapping-dir",
        type=Path,
        default=Path("mappings/open_dental"),
        help="Directory containing per-extension mapping JSONs.",
    )
    p.add_argument("--since", default=None, help="Lower-bound month YYYY-MM.")
    p.add_argument("--until", default=None, help="Upper-bound month YYYY-MM.")
    p.add_argument("--limit", type=int, default=None, help="Per-extension row cap.")
    p.add_argument(
        "--audit-log",
        type=Path,
        default=None,
        help="Audit log path. Defaults to ~/.praxis-deid/audit.log.",
    )
    p.add_argument(
        "--upload",
        action="store_true",
        help="(stub) After writing CSVs, POST them via the existing upload pipeline.",
    )


def dispatch(args: argparse.Namespace) -> int:
    return _cmd_extract(args)


def _cmd_extract(args: argparse.Namespace) -> int:
    if args.connection and args.fixture_json:
        print(
            "error: --connection and --fixture-json are mutually exclusive",
            flush=True,
        )
        return 2
    if not args.connection and not args.fixture_json:
        print(
            "error: provide either --connection (live DB) or --fixture-json (dry run)",
            flush=True,
        )
        return 2

    practice_id = args.practice_id or os.environ.get("PRAXIS_PRACTICE_ID")
    if not practice_id or len(practice_id) < 8:
        print(
            "error: --practice-id (or PRAXIS_PRACTICE_ID env var) must be set and >= 8 chars",
            flush=True,
        )
        return 2

    salt = os.environ.get(args.salt_env_var)
    if not salt:
        print(
            f"error: salt env var {args.salt_env_var!r} is not set",
            flush=True,
        )
        return 2
    if len(salt) < 32:
        print(
            f"error: salt must be >= 32 chars (got {len(salt)}); "
            "use `openssl rand -hex 32`",
            flush=True,
        )
        return 2

    extensions = (
        ("A", "B", "C", "D", "E", "F") if args.extension == "all" else (args.extension,)
    )

    run_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    output_dir = args.output or Path.home() / ".praxis-deid" / "output" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    audit_log_path = args.audit_log or Path.home() / ".praxis-deid" / "audit.log"

    filt = Filter(since_month=args.since, until_month=args.until, limit=args.limit)

    # Build the row source: either fixture-backed or live-DB-backed.
    fixture: dict[str, list[dict[str, Any]]] = {}
    if args.fixture_json:
        try:
            fixture = json.loads(args.fixture_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            print(f"error: --fixture-json is not valid JSON: {err}", flush=True)
            return 2
        if not isinstance(fixture, dict):
            print("error: --fixture-json must be an object keyed by schema name", flush=True)
            return 2

    # ONE Deidentifier across every extension in this run -> cross-extension HMAC stability.
    deid = Deidentifier(practice_id=practice_id, salt=salt, small_n_threshold=1)

    registry = _extractor_registry()
    per_extension_summary: dict[str, dict[str, Any]] = {}

    for letter in extensions:
        canonical_name, csv_filename, extractor_cls = registry[letter]
        mapping_path = args.mapping_dir / _MAPPING_FILENAMES[letter]
        try:
            mapping = load_mapping_config(mapping_path)
        except ExtractorError as err:
            print(f"[{letter}] mapping config invalid: {err}", flush=True)
            return 3

        if args.fixture_json:
            rows_for_this = fixture.get(canonical_name, [])
            row_source = _make_fixture_row_source(rows_for_this)
        else:
            row_source = _make_live_row_source(args.connection, mapping)

        extractor: BaseExtractor = extractor_cls(
            mapping_config=mapping,
            deidentifier=deid,
            row_source=row_source,
            output_dir=output_dir,
        )
        rows = extractor.extract(filt)
        out_path = extractor._dump_to_csv(rows, csv_filename)
        # Belt-and-braces: scan the written CSV for un-banded $$$ leaks.
        try:
            assert_no_exact_dollars_in_csv(out_path)
        except ExtractorError as err:
            print(f"[{letter}] DOLLAR-LEAK GUARD TRIPPED: {err}", flush=True)
            return 4
        per_extension_summary[letter] = {
            "canonical_schema": canonical_name,
            "csv": str(out_path),
            "rows_out": len(rows),
            "rows_dropped": extractor.dropped_rows,
            "drop_reasons": extractor.drop_reasons,
        }

    write_run_record(
        audit_log_path,
        {
            "tool_version": get_tool_version(),
            "practice_id": practice_id,
            "run_id": run_id,
            "command": "extract",
            "extensions": list(extensions),
            "output_dir": str(output_dir),
            "filter": {
                "since_month": filt.since_month,
                "until_month": filt.until_month,
                "limit": filt.limit,
            },
            "per_extension": per_extension_summary,
        },
    )

    print(
        f"praxis-deid extract: run_id={run_id} output={output_dir} "
        f"extensions={','.join(extensions)}"
    )
    for letter, summary in per_extension_summary.items():
        print(
            f"  [{letter}] {summary['canonical_schema']}: "
            f"{summary['rows_out']} rows -> {Path(summary['csv']).name} "
            f"(dropped {summary['rows_dropped']})"
        )

    return 0


# -------------------------------------------------------------------------
# Row source factories
# -------------------------------------------------------------------------


def _make_fixture_row_source(
    rows: list[Mapping[str, Any]],
) -> Any:
    """Returns a RowSource that ignores its arguments and yields the
    given rows verbatim. Tests and the --fixture-json CLI flag both use
    this; the locked Deidentifier never sees the fixture itself."""
    captured = list(rows)

    def _rs(table: str, columns: list[str], filter: Filter | None) -> Iterable[Mapping[str, Any]]:
        return iter(captured)

    return _rs


def _make_live_row_source(connection_url: str, mapping: Any) -> Any:
    """Build a RowSource that queries a real PMS DB.

    Lazy-imports a driver. The default expectation is mysql-connector-python
    for Open Dental; other PMSs would supply their own factory.

    For Phase-C the live driver is intentionally a stub — Hursh's first
    deployment will run via --fixture-json (an Open Dental SQL dump
    materialized to JSON by a separate tool). The driver factory is left
    here as the seam for a future agent to implement; raising a clear
    NotImplementedError keeps misconfiguration loud.
    """
    raise NotImplementedError(
        "Live-DB extraction is not enabled in this Phase-C build. "
        "Run with --fixture-json <path>, or wire a per-PMS driver via "
        "praxis_deid.extractors.cli._make_live_row_source. The hand-curated "
        "mapping configs at mappings/<pms>/ describe the join graph; the "
        "missing piece is the driver+SQL builder, intentionally deferred so "
        "Phase-C ships with the deterministic logic verified end-to-end."
    )
