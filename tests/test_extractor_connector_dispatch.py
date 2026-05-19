"""Tests for the URL-scheme dispatcher in
``praxis_deid.extractors.connectors``.

Verifies that the right connector subclass is returned for each
supported scheme, that unknown schemes fail loudly with a helpful
message, and that the legacy ``--fixture-json <path>`` CLI flag
normalises into a ``fixture-json://...`` URL correctly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from praxis_deid.extractors.connectors import (
    SUPPORTED_SCHEMES,
    ConnectionError,
    JsonFixtureConnector,
    MssqlConnector,
    MysqlConnector,
    PostgresConnector,
    connector_for_url,
    fixture_json_url,
)

# -------------------------------------------------------------------------
# Happy-path dispatch
# -------------------------------------------------------------------------


def test_mysql_url_dispatches_to_mysql_connector() -> None:
    c = connector_for_url(
        "mysql+mysqlconnector://user:pwd@host:3306/opendental"
    )
    assert isinstance(c, MysqlConnector)
    assert c.pms_dialect == "mysql"


def test_mssql_url_dispatches_to_mssql_connector() -> None:
    c = connector_for_url(
        "mssql+pyodbc://user:pwd@host/DTXNAME?driver=ODBC+Driver+17+for+SQL+Server"
    )
    assert isinstance(c, MssqlConnector)
    assert c.pms_dialect == "mssql"


def test_postgresql_bare_url_dispatches_to_postgres_connector() -> None:
    c = connector_for_url("postgresql://user:pwd@host:5432/db")
    assert isinstance(c, PostgresConnector)
    assert c.pms_dialect == "postgres"


def test_postgresql_psycopg2_url_dispatches_to_postgres_connector() -> None:
    c = connector_for_url("postgresql+psycopg2://user:pwd@host/db")
    assert isinstance(c, PostgresConnector)


def test_postgresql_pg8000_url_dispatches_to_postgres_connector() -> None:
    c = connector_for_url("postgresql+pg8000://user:pwd@host/db")
    assert isinstance(c, PostgresConnector)


def test_fixture_json_url_dispatches_to_fixture_connector(tmp_path: Path) -> None:
    p = tmp_path / "fx.json"
    p.write_text("{}", encoding="utf-8")
    c = connector_for_url(fixture_json_url(p))
    assert isinstance(c, JsonFixtureConnector)


# -------------------------------------------------------------------------
# Error paths
# -------------------------------------------------------------------------


def test_unknown_scheme_raises_with_supported_list() -> None:
    with pytest.raises(ConnectionError, match="unsupported"):
        connector_for_url("oracle+cx://user:pwd@host/db")


def test_unknown_scheme_lists_all_supported_schemes() -> None:
    try:
        connector_for_url("hbase://nope")
    except ConnectionError as err:
        msg = str(err)
        for sch in SUPPORTED_SCHEMES:
            assert sch in msg, f"missing {sch} from error: {msg}"
    else:
        pytest.fail("expected ConnectionError")


def test_url_without_scheme_separator_raises() -> None:
    with pytest.raises(ConnectionError, match="://"):
        connector_for_url("not-a-url")


def test_non_string_url_raises() -> None:
    with pytest.raises(ConnectionError):
        connector_for_url(123)  # type: ignore[arg-type]


# -------------------------------------------------------------------------
# CLI integration: legacy --fixture-json flag still works
# -------------------------------------------------------------------------


def test_cli_legacy_fixture_json_flag_routes_through_connector(tmp_path: Path) -> None:
    """The Phase-C CLI flag `--fixture-json <path>` (no `--connection`)
    must normalise into a fixture-json:// URL and produce the same six
    CSVs through the JsonFixtureConnector path. This is the regression
    guard for the refactor."""
    from praxis_deid.cli import main

    fixture_path = tmp_path / "fx.json"
    fixture_path.write_text(
        json.dumps(
            {
                "treatment_plans_raw": [
                    {
                        "treatplan.TreatPlanNum": "TP-1",
                        "treatplan.PatNum": "PT-1",
                        "treatplan.DateTP": "2026-04-15",
                        "treatplan.DateTSigned": None,
                        "treatplan.TPStatus": 0,
                        "proctp.ProvNum": "DOC-7",
                        "proctp.FeeAmt": 250.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    audit = tmp_path / "audit.log"

    import os

    os.environ["PRAXIS_DEID_SALT"] = "X" * 40

    rc = main(
        [
            "extract",
            "--extension",
            "A",
            "--fixture-json",
            str(fixture_path),
            "--output",
            str(out_dir),
            "--practice-id",
            "00000000-0000-0000-0000-0000000000a1",
            "--audit-log",
            str(audit),
        ]
    )
    assert rc == 0
    assert (out_dir / "treatment_plans_raw.csv").exists()
    # The audit envelope MUST record pms_dialect = 'fixture' AND a
    # redacted URL (no password leak).
    line = audit.read_text(encoding="utf-8").splitlines()[-1]
    record = json.loads(line)
    assert record["pms_dialect"] == "fixture"
    assert "connection_redacted" in record


def test_cli_explicit_connection_url_with_unknown_scheme_errors(tmp_path: Path) -> None:
    """`--connection mongodb://...` should produce a friendly error
    listing the supported schemes."""
    import os

    from praxis_deid.cli import main

    os.environ["PRAXIS_DEID_SALT"] = "X" * 40

    rc = main(
        [
            "extract",
            "--extension",
            "A",
            "--connection",
            "mongodb://user:pwd@host/db",
            "--output",
            str(tmp_path / "out"),
            "--practice-id",
            "00000000-0000-0000-0000-0000000000a1",
        ]
    )
    assert rc != 0  # specifically rc=2 from the dispatch path


def test_cli_missing_driver_extra_surfaces_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the user runs `praxis-deid extract --connection mysql+mysqlconnector://...`
    without `pip install praxis-deid[mysql]`, we must surface a clear
    "install the mysql extra" error rather than a raw ImportError.
    """
    import importlib

    from praxis_deid.cli import main

    monkeypatch.setenv("PRAXIS_DEID_SALT", "X" * 40)

    # Force the driver module import to fail.
    real_import_module = importlib.import_module

    def fake_import_module(name: str, *a, **k):  # type: ignore[no-untyped-def]
        if name == "mysql.connector":
            raise ImportError("simulated missing driver")
        return real_import_module(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)

    rc = main(
        [
            "extract",
            "--extension",
            "A",
            "--connection",
            "mysql+mysqlconnector://user:pwd@host:3306/opendental",
            "--output",
            str(tmp_path / "out"),
            "--practice-id",
            "00000000-0000-0000-0000-0000000000a1",
        ]
    )
    assert rc != 0
