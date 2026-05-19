"""Phase-D security-layer tests.

Covers:
  * SQL-injection guards on table/column names + WHERE clauses.
  * Credential redaction (passwords scrubbed from any logged URL).
  * Allowlist enforcement: a connector refuses to query a table or
    column that's not in INFORMATION_SCHEMA / the fixture.
  * Audit log NEVER contains the full connection URL.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from praxis_deid.extractors.connectors import (
    ConnectionError,
    JsonFixtureConnector,
    is_valid_identifier,
    redact_connection_url,
    validate_identifier,
    validate_where_clause,
)

# -------------------------------------------------------------------------
# Identifier validation
# -------------------------------------------------------------------------


def test_valid_identifier_accepts_table_names() -> None:
    for n in ("treatplan", "ProcTP", "_temp", "x", "x9", "a_b_c"):
        assert is_valid_identifier(n)


def test_valid_identifier_rejects_qualified_names() -> None:
    assert not is_valid_identifier("treatplan.PatNum")


def test_valid_identifier_rejects_sql_injection() -> None:
    for n in (
        "treatplan; DROP TABLE patient",
        "treat'plan",
        "treatplan--",
        "/* x */",
        "1foo",  # starts with digit
        "",
        "x" * 200,  # too long
    ):
        assert not is_valid_identifier(n), f"should reject: {n!r}"


def test_validate_identifier_raises_with_kind() -> None:
    with pytest.raises(ConnectionError, match="invalid table identifier"):
        validate_identifier("bad; name", kind="table")
    with pytest.raises(ConnectionError, match="invalid column identifier"):
        validate_identifier("col--", kind="column")


def test_validate_identifier_accepts_safe_names() -> None:
    validate_identifier("PatNum", kind="column")
    validate_identifier("treatplan", kind="table")


# -------------------------------------------------------------------------
# WHERE-clause validation
# -------------------------------------------------------------------------


def test_validate_where_clause_accepts_safe_clause() -> None:
    validate_where_clause("DateTP >= :since AND DateTP < :until")


def test_validate_where_clause_accepts_none() -> None:
    validate_where_clause(None)


def test_validate_where_clause_rejects_semicolon() -> None:
    with pytest.raises(ConnectionError, match="forbidden SQL substring"):
        validate_where_clause("DateTP >= '2026-01-01'; DROP TABLE patient")


def test_validate_where_clause_rejects_comment_marker() -> None:
    with pytest.raises(ConnectionError, match="forbidden SQL substring"):
        validate_where_clause("DateTP >= '2026-01-01' -- hax")


def test_validate_where_clause_rejects_block_comment() -> None:
    with pytest.raises(ConnectionError, match="forbidden SQL substring"):
        validate_where_clause("DateTP >= /* x */ '2026-01-01'")


def test_validate_where_clause_rejects_dml_keywords() -> None:
    for kw in ("DROP", "TRUNCATE", "DELETE", "UPDATE", "INSERT", "ALTER"):
        with pytest.raises(ConnectionError, match="forbidden SQL keyword"):
            validate_where_clause(f"DateTP > 0 OR 1=1 OR {kw} TABLE x")


def test_validate_where_clause_keyword_inside_string_literal_ok() -> None:
    """A literal value 'DROP' inside a quoted string isn't a keyword."""
    # No exception — the keyword scan strips literals first.
    validate_where_clause("notes = 'we will DROP this someday'")


# -------------------------------------------------------------------------
# Credential redaction
# -------------------------------------------------------------------------


def test_redact_password_in_mysql_url() -> None:
    out = redact_connection_url(
        "mysql+mysqlconnector://praxis_ro:hunter2@dentalsrv:3306/opendental"
    )
    assert "hunter2" not in out
    assert "praxis_ro" in out  # username preserved (audit chain-of-custody)
    assert "***" in out
    assert "dentalsrv:3306/opendental" in out


def test_redact_password_in_mssql_url() -> None:
    out = redact_connection_url(
        "mssql+pyodbc://ro_user:p%40ssw0rd@srv/DTXNAME?driver=ODBC+Driver+17"
    )
    assert "p%40ssw0rd" not in out
    assert "***" in out


def test_redact_password_in_postgres_url() -> None:
    out = redact_connection_url(
        "postgresql+pg8000://praxis_ro:secret123@host:5432/db"
    )
    assert "secret123" not in out
    assert "***" in out


def test_redact_handles_url_without_password() -> None:
    """A fixture URL has no credentials; redaction is a no-op."""
    assert redact_connection_url("fixture-json:///abs/path/fx.json") == (
        "fixture-json:///abs/path/fx.json"
    )


def test_redact_handles_empty_string() -> None:
    assert redact_connection_url("") == ""


# -------------------------------------------------------------------------
# Allowlist enforcement (uses JsonFixtureConnector as a stand-in — the
# allowlist machinery is in the shared base)
# -------------------------------------------------------------------------


def test_fixture_connector_rejects_unknown_table() -> None:
    conn = JsonFixtureConnector(
        "fixture-json://./x", inline_fixture={"known_table": []}
    )
    conn.connect()
    with pytest.raises(ConnectionError, match="not in fixture"):
        list(conn.fetch_rows(table_name="unknown_table", columns=[]))
    conn.close()


def test_fixture_connector_rejects_injection_in_where() -> None:
    conn = JsonFixtureConnector(
        "fixture-json://./x", inline_fixture={"t": [{"a": 1}]}
    )
    conn.connect()
    with pytest.raises(ConnectionError, match="forbidden SQL"):
        list(
            conn.fetch_rows(
                table_name="t",
                columns=["a"],
                where_clause="1=1; DROP TABLE patient",
            )
        )
    conn.close()


# -------------------------------------------------------------------------
# Audit log: the full URL with password MUST NEVER appear
# -------------------------------------------------------------------------


def test_audit_log_contains_redacted_url_not_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: run extract via the CLI with a fixture-json URL and
    confirm the audit envelope has the redacted URL only (not the
    original)."""
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
                        "proctp.FeeAmt": 100.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PRAXIS_DEID_SALT", "X" * 40)

    from praxis_deid.cli import main

    audit = tmp_path / "audit.log"
    out_dir = tmp_path / "out"
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
    line = audit.read_text(encoding="utf-8").splitlines()[-1]
    record = json.loads(line)
    # Fixture URLs have no password but the redacted field is still present.
    assert "connection_redacted" in record
    assert record["pms_dialect"] == "fixture"
    # The audit envelope MUST NEVER contain raw connection details
    # beyond the redacted view; specifically a "connection_url" key
    # MUST NOT exist (it would expose a password if the user ran with
    # a live URL).
    assert "connection_url" not in record


def test_redacted_url_property_on_live_connectors() -> None:
    """Live connectors should expose a `redacted_url` attribute that
    never contains the password."""
    from praxis_deid.extractors.connectors import (
        MssqlConnector,
        MysqlConnector,
        PostgresConnector,
    )

    for cls, url in (
        (
            MysqlConnector,
            "mysql+mysqlconnector://praxis_ro:hunter2@host:3306/opendental",
        ),
        (
            MssqlConnector,
            "mssql+pyodbc://praxis_ro:hunter2@host/DTXNAME",
        ),
        (
            PostgresConnector,
            "postgresql+pg8000://praxis_ro:hunter2@host:5432/db",
        ),
    ):
        c = cls(url)
        assert "hunter2" not in c.redacted_url
        assert "***" in c.redacted_url
