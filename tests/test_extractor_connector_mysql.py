"""Tests for :class:`MysqlConnector` (Open Dental + any-MySQL).

Tests live-DB behavior via two paths:

  * Mocked SQLAlchemy engine — verifies the connector calls
    create_engine with the right URL, executes the right introspection
    queries against INFORMATION_SCHEMA, and applies the MySQL identifier
    quoting (backticks).

  * SQLite-backed shim — verifies fetch_rows assembly + validation +
    row dict shape on a real SQLAlchemy engine, by swapping the
    information-schema queries for sqlite_master equivalents. This
    proves the query-construction code works against a real driver
    end-to-end without needing a MySQL container.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from praxis_deid.extractors.connectors import (
    ConnectionError,
    MysqlConnector,
)
from praxis_deid.extractors.connectors._sqlalchemy_base import SqlAlchemyConnector

# -------------------------------------------------------------------------
# Plain assertions: dialect, quoting, queries
# -------------------------------------------------------------------------


def test_mysql_dialect_is_mysql() -> None:
    assert MysqlConnector("mysql+mysqlconnector://u:p@h/db").pms_dialect == "mysql"


def test_mysql_uses_backtick_quoting() -> None:
    c = MysqlConnector("mysql+mysqlconnector://u:p@h/db")
    assert c._quote_identifier("treatplan") == "`treatplan`"
    assert c._quote_identifier("PatNum") == "`PatNum`"


def test_mysql_list_tables_query_uses_information_schema() -> None:
    q = MysqlConnector("mysql+mysqlconnector://u:p@h/db")._list_tables_query()
    assert "INFORMATION_SCHEMA.TABLES" in q
    assert "DATABASE()" in q


def test_mysql_list_columns_query_uses_information_schema_and_bind() -> None:
    q = MysqlConnector("mysql+mysqlconnector://u:p@h/db")._list_columns_query()
    assert "INFORMATION_SCHEMA.COLUMNS" in q
    assert ":table_name" in q
    # NEVER an f-string interpolation of the table name.
    assert "%s" not in q  # mysqlconnector's positional placeholder absent


def test_mysql_redacts_password_in_redacted_url() -> None:
    c = MysqlConnector("mysql+mysqlconnector://ro:secret@h:3306/opendental")
    assert "secret" not in c.redacted_url
    assert "***" in c.redacted_url


def test_mysql_uses_ansi_limit_clause() -> None:
    c = MysqlConnector("mysql+mysqlconnector://u:p@h/db")
    assert c._limit_clause(10) == " LIMIT 10"


def test_mysql_connect_args_include_timeout() -> None:
    c = MysqlConnector("mysql+mysqlconnector://u:p@h/db")
    args = c._connect_args()
    assert "connect_timeout" in args
    assert args["connect_timeout"] >= 1


# -------------------------------------------------------------------------
# Mocked-driver tests: verify create_engine called once, with the URL
# -------------------------------------------------------------------------


def test_mysql_connect_calls_create_engine_with_url() -> None:
    c = MysqlConnector("mysql+mysqlconnector://u:p@h:3306/db")
    with patch(
        "praxis_deid.extractors.connectors._sqlalchemy_base._require_driver",
        return_value=None,
    ):
        # Patch sqlalchemy.create_engine to a MagicMock so no real DB
        # round-trip is attempted.
        fake_sa = MagicMock()
        fake_engine = MagicMock()
        fake_sa.create_engine.return_value = fake_engine
        # Context manager around fake_engine.connect()
        fake_engine.connect.return_value.__enter__.return_value = MagicMock()
        fake_engine.connect.return_value.__exit__.return_value = False
        with patch(
            "praxis_deid.extractors.connectors._sqlalchemy_base._import_sqlalchemy",
            return_value=fake_sa,
        ):
            c.connect()
        # create_engine was called with the original URL.
        assert fake_sa.create_engine.call_args[0][0] == (
            "mysql+mysqlconnector://u:p@h:3306/db"
        )


def test_mysql_connect_failure_surfaces_as_connection_error_without_url() -> None:
    """If the driver raises, the connector wraps it as a ConnectionError
    that contains the REDACTED URL — never the password."""
    c = MysqlConnector("mysql+mysqlconnector://u:supersecret@h/db")
    with patch(
        "praxis_deid.extractors.connectors._sqlalchemy_base._require_driver",
        return_value=None,
    ):
        fake_sa = MagicMock()
        fake_sa.create_engine.side_effect = RuntimeError("bad host")
        with patch(
            "praxis_deid.extractors.connectors._sqlalchemy_base._import_sqlalchemy",
            return_value=fake_sa,
        ):
            with pytest.raises(ConnectionError) as excinfo:
                c.connect()
            assert "supersecret" not in str(excinfo.value)


# -------------------------------------------------------------------------
# Real round-trip via a SQLite shim subclass (proves fetch_rows works
# against a real SQLAlchemy engine end-to-end)
# -------------------------------------------------------------------------


class _SqliteShimConnector(SqlAlchemyConnector):
    """MysqlConnector-shaped connector backed by SQLite for tests."""

    pms_dialect = "mysql"  # we're proxying for the mysql path
    required_driver_module = "sqlite3"  # always present in stdlib
    required_extras_name = "mysql"

    def _quote_identifier(self, name: str) -> str:
        # SQLite accepts double-quoted identifiers (ANSI) or backticks.
        return f'"{name}"'

    def _connect_args(self) -> dict[str, Any]:
        # SQLite's pysqlite driver doesn't accept ``connect_timeout``,
        # so we suppress the default. The fact this hook is overridable
        # is exactly what lets the per-dialect connectors (mysql, mssql)
        # supply their own driver-specific kwargs.
        return {}

    def _list_tables_query(self) -> str:
        return "SELECT name AS TABLE_NAME FROM sqlite_master WHERE type='table'"

    def _list_columns_query(self) -> str:
        # SQLite has no INFORMATION_SCHEMA but pragma_table_info gives the same shape.
        return (
            "SELECT name AS COLUMN_NAME, type AS DATA_TYPE "
            "FROM pragma_table_info(:table_name)"
        )


@pytest.fixture
def sqlite_conn(tmp_path: Any) -> _SqliteShimConnector:
    """Build a sqlite-backed shim with a small treatplan-shaped table."""
    import sqlalchemy as sa

    db_path = tmp_path / "test.db"
    eng = sa.create_engine(f"sqlite:///{db_path}")
    with eng.begin() as c:
        c.execute(
            sa.text(
                "CREATE TABLE treatplan ("
                "TreatPlanNum TEXT PRIMARY KEY, PatNum TEXT, DateTP TEXT)"
            )
        )
        c.execute(
            sa.text(
                "INSERT INTO treatplan (TreatPlanNum, PatNum, DateTP) VALUES "
                "('TP-1', 'PT-1', '2026-04-15'), "
                "('TP-2', 'PT-2', '2026-04-20')"
            )
        )
    eng.dispose()

    conn = _SqliteShimConnector(f"sqlite:///{db_path}")
    conn.connect()
    return conn


def test_sqlite_shim_lists_tables(sqlite_conn: _SqliteShimConnector) -> None:
    tables = sqlite_conn.list_tables()
    assert "treatplan" in tables
    sqlite_conn.close()


def test_sqlite_shim_lists_columns(sqlite_conn: _SqliteShimConnector) -> None:
    cols = sqlite_conn.list_columns("treatplan")
    names = {c for c, _t in cols}
    assert names == {"TreatPlanNum", "PatNum", "DateTP"}
    sqlite_conn.close()


def test_sqlite_shim_fetch_rows_round_trip(sqlite_conn: _SqliteShimConnector) -> None:
    rows = list(
        sqlite_conn.fetch_rows(
            table_name="treatplan",
            columns=["TreatPlanNum", "PatNum", "DateTP"],
        )
    )
    sqlite_conn.close()
    assert len(rows) == 2
    # Row dicts come back keyed "treatplan.<column>" so the extractor's
    # existing row-shape contract is preserved.
    assert rows[0]["treatplan.TreatPlanNum"] == "TP-1"
    assert rows[0]["treatplan.PatNum"] == "PT-1"


def test_sqlite_shim_fetch_rows_respects_limit(
    sqlite_conn: _SqliteShimConnector,
) -> None:
    rows = list(
        sqlite_conn.fetch_rows(
            table_name="treatplan",
            columns=["TreatPlanNum"],
            limit=1,
        )
    )
    sqlite_conn.close()
    assert len(rows) == 1


def test_sqlite_shim_fetch_rows_with_where_and_binds(
    sqlite_conn: _SqliteShimConnector,
) -> None:
    rows = list(
        sqlite_conn.fetch_rows(
            table_name="treatplan",
            columns=["TreatPlanNum"],
            where_clause="PatNum = :pat",
            bind_params={"pat": "PT-1"},
        )
    )
    sqlite_conn.close()
    assert len(rows) == 1
    assert rows[0]["treatplan.TreatPlanNum"] == "TP-1"


def test_sqlite_shim_rejects_unknown_table(sqlite_conn: _SqliteShimConnector) -> None:
    with pytest.raises(ConnectionError, match="not in allowlist|not in schema"):
        list(sqlite_conn.fetch_rows(table_name="notreal", columns=["x"]))
    sqlite_conn.close()


def test_sqlite_shim_rejects_unknown_column(sqlite_conn: _SqliteShimConnector) -> None:
    with pytest.raises(ConnectionError, match="not in allowlist"):
        list(
            sqlite_conn.fetch_rows(
                table_name="treatplan",
                columns=["NotARealColumn"],
            )
        )
    sqlite_conn.close()


def test_sqlite_shim_rejects_injection_in_where(
    sqlite_conn: _SqliteShimConnector,
) -> None:
    with pytest.raises(ConnectionError, match="forbidden SQL"):
        list(
            sqlite_conn.fetch_rows(
                table_name="treatplan",
                columns=["TreatPlanNum"],
                where_clause="1=1; DROP TABLE treatplan",
            )
        )
    sqlite_conn.close()


def test_sqlite_shim_close_is_idempotent(sqlite_conn: _SqliteShimConnector) -> None:
    sqlite_conn.close()
    sqlite_conn.close()  # no error
