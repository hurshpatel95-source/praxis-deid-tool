"""CLI entrypoint for the de-identification tool.

  praxis-deid run --config /etc/praxis-deid/config.yaml

Steps:
  1. Load + validate config.
  2. For each non-null source_file: stream rows through Deidentifier.
  3. finalize() applies small-N suppression.
  4. Write output (CSV or API).
  5. Append run audit record.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .audit import write_run_record
from .config import Config, load_config
from .deidentify import Deidentifier
from .sources import iter_csv_rows
from .upload import post_to_api, write_csvs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="praxis-deid", description="Praxis practice-side de-identification tool")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run a de-identification cycle")
    run.add_argument("--config", required=True, type=Path, help="Path to config YAML")
    run.add_argument("--dry-run", action="store_true", help="Process but do not write or upload")

    sub.add_parser("version", help="Print version")

    args = parser.parse_args(argv)

    if args.cmd == "version":
        from . import __version__
        print(__version__)
        return 0

    if args.cmd == "run":
        return _cmd_run(args.config, dry_run=args.dry_run)

    return 1


def _cmd_run(config_path: Path, *, dry_run: bool) -> int:
    cfg = load_config(config_path)

    deid = Deidentifier(
        practice_id=cfg.practice_id,
        salt=cfg.deidentification.patient_id_salt,
        small_n_threshold=cfg.deidentification.small_n_threshold,
    )

    _ingest_optional(cfg.source.patients_file, deid.add_patient)
    _ingest_optional(cfg.source.appointments_file, deid.add_appointment)
    _ingest_optional(cfg.source.providers_file, deid.add_provider)
    _ingest_optional(cfg.source.procedures_file, deid.add_procedure)
    _ingest_optional(cfg.source.referrals_file, deid.add_referral)
    _ingest_optional(cfg.source.invoices_file, deid.add_invoice)

    patients, appointments, providers, procedures, referrals, invoices = deid.finalize()

    output_summary: dict[str, object] = {}
    if dry_run:
        output_summary["mode"] = "dry_run"
    elif cfg.output.type == "csv":
        assert cfg.output.directory is not None
        paths = write_csvs(
            cfg.output.directory,
            patients=patients,
            appointments=appointments,
            providers=providers,
            procedures=procedures,
            referrals=referrals,
            invoices=invoices,
        )
        output_summary = {"mode": "csv", "files": {k: str(v) for k, v in paths.items()}}
    else:
        assert cfg.output.api_endpoint and cfg.output.api_key
        post_to_api(
            cfg.output.api_endpoint,
            cfg.output.api_key,
            {
                "practice_id": cfg.practice_id,
                "patients": [_to_dict(p) for p in patients],
                "appointments": [_to_dict(a) for a in appointments],
                "providers": [_to_dict(p) for p in providers],
                "procedures": [_to_dict(p) for p in procedures],
                "referrals": [_to_dict(r) for r in referrals],
                "invoices": [_to_dict(i) for i in invoices],
            },
        )
        output_summary = {"mode": "api", "endpoint": cfg.output.api_endpoint}

    write_run_record(
        cfg.audit.log_path,
        {
            "practice_id": cfg.practice_id,
            "stats": {
                "patients_in": deid.stats.patients_in,
                "patients_out": len(patients),
                "appointments_in": deid.stats.appointments_in,
                "appointments_out": len(appointments),
                "providers_out": len(providers),
                "procedures_in": deid.stats.procedures_in,
                "procedures_out": len(procedures),
                "referrals_out": len(referrals),
                "invoices_out": len(invoices),
                "rows_dropped": deid.stats.rows_dropped,
                "drop_reasons": deid.stats.drop_reasons,
                "small_n_suppressions": deid.stats.small_n_suppressions,
            },
            "output": output_summary,
        },
    )

    print(
        f"de-id run complete: {len(patients)} patients, {len(appointments)} appointments, "
        f"{len(procedures)} procedures, {len(invoices)} invoices "
        f"(dropped {deid.stats.rows_dropped}, small-N suppressed {deid.stats.small_n_suppressions})"
    )
    return 0


def _ingest_optional(path: Path | None, add_fn: object) -> None:
    if path is None:
        return
    callable_fn = add_fn  # type: ignore[assignment]
    for row in iter_csv_rows(path):
        callable_fn(row)  # type: ignore[misc]


def _to_dict(obj: object) -> dict[str, object]:
    from dataclasses import asdict
    return asdict(obj)  # type: ignore[arg-type]


if __name__ == "__main__":
    sys.exit(main())
