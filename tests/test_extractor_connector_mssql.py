"""Tests for :class:`MssqlConnector` (Dentrix + any-MSSQL).

End-to-end pyodbc+freetds isn't testable without a real SQL Server, so
the assertions here are:

  * Dialect-specific identifier quoting (``[name]``)
  * Per-dialect introspection queries (``SCHEMA_NAME()``)
  * ``TOP N`` injection instead of ``LIMIT N``
  * READ UNCOMMITTED isolation-level pre-flight
  * pyodbc-style ``timeout`` connect arg (not ``connect_timeout``)
  * Credential redaction parity with the other live connectors
  * Mocked engine round-trip verifying the SQL string shape

The real driver path is exercised by Hursh during cousin-onboarding,
not in unit tests.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from praxis_deid.extractors.connectors import (
    ConnectionError,
    MssqlConnector,
)

# -------------------------------------------------------------------------
# Plain assertions: dialect, quoting, queries, isolation
# -------------------------------------------------------------------------


def test_mssql_dialect_is_mssql() -> None:
    assert MssqlConnector("mssql+pyodbc://u:p@h/DTX").pms_dialect == "mssql"


def test_mssql_uses_bracket_quoting() -> None:
    c = MssqlConnector("mssql+pyodbc://u:p@h/DTX")
    assert c._quote_identifier("treatment") == "[treatment]"
    assert c._quote_identifier("PatNum") == "[PatNum]"


def test_mssql_list_tables_query_uses_schema_name() -> None:
    q = MssqlConnector("mssql+pyodbc://u:p@h/DTX")._list_tables_query()
    assert "INFORMATION_SCHEMA.TABLES" in q
    assert "SCHEMA_NAME()" in q


def test_mssql_list_columns_query_uses_schema_name_and_bind() -> None:
    q = MssqlConnector("mssql+pyodbc://u:p@h/DTX")._list_columns_query()
    assert "SCHEMA_NAME()" in q
    assert ":table_name" in q


def test_mssql_uses_pyodbc_timeout_not_connect_timeout() -> None:
    """pyodbc accepts ``timeout`` (login timeout), not ``connect_timeout``."""
    c = MssqlConnector("mssql+pyodbc://u:p@h/DTX")
    args = c._connect_args()
    assert "timeout" in args
    assert "connect_timeout" not in args


def test_mssql_limit_clause_returns_empty_top_handled_elsewhere() -> None:
    """MSSQL has no LIMIT; the per-query SQL must use ``TOP N``."""
    c = MssqlConnector("mssql+pyodbc://u:p@h/DTX")
    assert c._limit_clause(10) == ""


def test_mssql_redacts_password() -> None:
    c = MssqlConnector("mssql+pyodbc://ro:hunter2@h/DTX?driver=ODBC+Driver+17")
    assert "hunter2" not in c.redacted_url
    assert "***" in c.redacted_url


# -------------------------------------------------------------------------
# Mocked driver: verify TOP N injection + READ UNCOMMITTED pre-flight
# -------------------------------------------------------------------------


def _wire_fake_sa() -> tuple[Any, Any, Any]:
    """Build a (fake_sa, fake_engine, fake_conn) trio for mocking."""
    fake_sa = MagicMock()
    fake_engine = MagicMock()
    fake_sa.create_engine.return_value = fake_engine
    fake_conn = MagicMock()
    fake_engine.connect.return_value.__enter__.return_value = fake_conn
    fake_engine.connect.return_value.__exit__.return_value = False
    fake_sa.text.side_effect = lambda s: s  # return the string verbatim
    return fake_sa, fake_engine, fake_conn


def test_mssql_pre_flight_sets_read_uncommitted_isolation() -> None:
    c = MssqlConnector("mssql+pyodbc://u:p@h/DTX")
    fake_sa, fake_engine, fake_conn = _wire_fake_sa()
    with patch(
        "praxis_deid.extractors.connectors._sqlalchemy_base._require_driver",
        return_value=None,
    ), patch(
        "praxis_deid.extractors.connectors._sqlalchemy_base._import_sqlalchemy",
        return_value=fake_sa,
    ), patch(
        "praxis_deid.extractors.connectors.mssql._import_sqlalchemy",
        return_value=fake_sa,
    ):
        c.connect()
    # The pre-flight should have executed SET TRANSACTION ISOLATION LEVEL
    # READ UNCOMMITTED at least once.
    executed = [
        call.args[0] for call in fake_conn.execute.call_args_list
    ]
    assert any(
        "READ UNCOMMITTED" in str(s) for s in executed
    ), f"no READ UNCOMMITTED in: {executed}"


def test_mssql_fetch_rows_injects_top_n_not_limit() -> None:
    """The SQL string passed to text() must contain TOP N and NOT LIMIT N."""
    c = MssqlConnector("mssql+pyodbc://u:p@h/DTX")
    fake_sa, fake_engine, fake_conn = _wire_fake_sa()
    # Make list_tables / list_columns return predictable allowlists.
    with patch(
        "praxis_deid.extractors.connectors._sqlalchemy_base._require_driver",
        return_value=None,
    ), patch(
        "praxis_deid.extractors.connectors._sqlalchemy_base._import_sqlalchemy",
        return_value=fake_sa,
    ), patch(
        "praxis_deid.extractors.connectors.mssql._import_sqlalchemy",
        return_value=fake_sa,
    ):
        c.connect()
        c._table_cache = ["patient"]
        c._column_cache["patient"] = [("PatNum", "varchar"), ("LName", "varchar")]
        # Make the result iterator yield one fake row.
        fake_result = MagicMock()
        fake_result.mappings.return_value = iter(
            [{"PatNum": "PT-1", "LName": "Doe"}]
        )
        fake_conn.execute.return_value = fake_result
        rows = list(
            c.fetch_rows(
                table_name="patient",
                columns=["PatNum", "LName"],
                limit=5,
            )
        )
    assert rows == [{"patient.PatNum": "PT-1", "patient.LName": "Doe"}]
    # Find the SELECT call (not the pre-flight ISOLATION call).
    select_calls = [
        call.args[0]
        for call in fake_conn.execute.call_args_list
        if "SELECT" in str(call.args[0]).upper()
    ]
    assert select_calls, "no SELECT was issued"
    select_sql = select_calls[-1]
    assert "TOP 5" in select_sql
    assert "LIMIT" not in select_sql.upper()


def test_mssql_fetch_rows_no_limit_omits_top() -> None:
    c = MssqlConnector("mssql+pyodbc://u:p@h/DTX")
    fake_sa, fake_engine, fake_conn = _wire_fake_sa()
    with patch(
        "praxis_deid.extractors.connectors._sqlalchemy_base._require_driver",
        return_value=None,
    ), patch(
        "praxis_deid.extractors.connectors._sqlalchemy_base._import_sqlalchemy",
        return_value=fake_sa,
    ), patch(
        "praxis_deid.extractors.connectors.mssql._import_sqlalchemy",
        return_value=fake_sa,
    ):
        c.connect()
        c._table_cache = ["patient"]
        c._column_cache["patient"] = [("PatNum", "varchar")]
        fake_result = MagicMock()
        fake_result.mappings.return_value = iter([])
        fake_conn.execute.return_value = fake_result
        list(c.fetch_rows(table_name="patient", columns=["PatNum"]))
    select_calls = [
        call.args[0]
        for call in fake_conn.execute.call_args_list
        if "SELECT" in str(call.args[0]).upper()
    ]
    select_sql = select_calls[-1]
    assert "TOP" not in select_sql


def test_mssql_fetch_rows_uses_bracket_quoting_for_identifiers() -> None:
    c = MssqlConnector("mssql+pyodbc://u:p@h/DTX")
    fake_sa, fake_engine, fake_conn = _wire_fake_sa()
    with patch(
        "praxis_deid.extractors.connectors._sqlalchemy_base._require_driver",
        return_value=None,
    ), patch(
        "praxis_deid.extractors.connectors._sqlalchemy_base._import_sqlalchemy",
        return_value=fake_sa,
    ), patch(
        "praxis_deid.extractors.connectors.mssql._import_sqlalchemy",
        return_value=fake_sa,
    ):
        c.connect()
        c._table_cache = ["patient"]
        c._column_cache["patient"] = [("PatNum", "varchar")]
        fake_result = MagicMock()
        fake_result.mappings.return_value = iter([])
        fake_conn.execute.return_value = fake_result
        list(c.fetch_rows(table_name="patient", columns=["PatNum"]))
    select_calls = [
        str(call.args[0])
        for call in fake_conn.execute.call_args_list
        if "SELECT" in str(call.args[0]).upper()
    ]
    select_sql = select_calls[-1]
    assert "[patient]" in select_sql
    assert "[PatNum]" in select_sql


def test_mssql_rejects_unknown_table_in_fetch() -> None:
    c = MssqlConnector("mssql+pyodbc://u:p@h/DTX")
    fake_sa, _fake_engine, fake_conn = _wire_fake_sa()
    with patch(
        "praxis_deid.extractors.connectors._sqlalchemy_base._require_driver",
        return_value=None,
    ), patch(
        "praxis_deid.extractors.connectors._sqlalchemy_base._import_sqlalchemy",
        return_value=fake_sa,
    ), patch(
        "praxis_deid.extractors.connectors.mssql._import_sqlalchemy",
        return_value=fake_sa,
    ):
        c.connect()
        c._table_cache = ["patient"]  # allowlist
        c._column_cache["patient"] = [("PatNum", "varchar")]
        with pytest.raises(ConnectionError, match="not in"):
            list(c.fetch_rows(table_name="not_real", columns=["PatNum"]))


def test_mssql_missing_driver_raises_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    real_import_module = importlib.import_module

    def fake_import_module(name: str, *a, **k):  # type: ignore[no-untyped-def]
        if name == "pyodbc":
            raise ImportError("simulated")
        return real_import_module(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    c = MssqlConnector("mssql+pyodbc://u:p@h/DTX")
    with pytest.raises(ConnectionError, match=r"pyodbc.*\[mssql\]"):
        c.connect()
