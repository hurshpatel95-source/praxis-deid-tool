"""Tests for :class:`PostgresConnector` (future-PMS support).

The real pg8000 driver is OS-independent (pure Python) but we still
mock the engine here — these tests verify the per-dialect query
construction, not driver behavior.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from praxis_deid.extractors.connectors import (
    ConnectionError,
    PostgresConnector,
)


def test_postgres_dialect_is_postgres() -> None:
    assert PostgresConnector("postgresql://u:p@h/db").pms_dialect == "postgres"


def test_postgres_uses_ansi_double_quote_quoting() -> None:
    c = PostgresConnector("postgresql://u:p@h/db")
    assert c._quote_identifier("patient") == '"patient"'


def test_postgres_list_tables_query_scoped_to_current_schema() -> None:
    q = PostgresConnector("postgresql://u:p@h/db")._list_tables_query()
    assert "information_schema.tables" in q
    assert "current_schema" in q
    assert "BASE TABLE" in q


def test_postgres_list_columns_query_uses_bind() -> None:
    q = PostgresConnector("postgresql://u:p@h/db")._list_columns_query()
    assert ":table_name" in q
    assert "information_schema.columns" in q


def test_postgres_uses_ansi_limit() -> None:
    c = PostgresConnector("postgresql://u:p@h/db")
    assert c._limit_clause(5) == " LIMIT 5"


def test_postgres_pg8000_url_uses_timeout_kwarg() -> None:
    """pg8000.connect takes ``timeout=N``, not ``connect_timeout``."""
    c = PostgresConnector("postgresql+pg8000://u:p@h:5432/db")
    args = c._connect_args()
    assert "timeout" in args
    assert "connect_timeout" not in args


def test_postgres_psycopg2_url_uses_connect_timeout_kwarg() -> None:
    """psycopg2 takes ``connect_timeout=N``."""
    c = PostgresConnector("postgresql+psycopg2://u:p@h:5432/db")
    args = c._connect_args()
    assert "connect_timeout" in args
    assert "timeout" not in args


def test_postgres_redacts_password() -> None:
    c = PostgresConnector("postgresql+pg8000://ro:s3cret@h:5432/db")
    assert "s3cret" not in c.redacted_url
    assert "***" in c.redacted_url


def _wire_fake_sa() -> tuple[Any, Any, Any]:
    fake_sa = MagicMock()
    fake_engine = MagicMock()
    fake_sa.create_engine.return_value = fake_engine
    fake_conn = MagicMock()
    fake_engine.connect.return_value.__enter__.return_value = fake_conn
    fake_engine.connect.return_value.__exit__.return_value = False
    fake_sa.text.side_effect = lambda s: s
    return fake_sa, fake_engine, fake_conn


def test_postgres_fetch_rows_includes_ansi_limit_clause() -> None:
    c = PostgresConnector("postgresql://u:p@h/db")
    fake_sa, _fake_engine, fake_conn = _wire_fake_sa()
    with patch(
        "praxis_deid.extractors.connectors._sqlalchemy_base._require_driver",
        return_value=None,
    ), patch(
        "praxis_deid.extractors.connectors._sqlalchemy_base._import_sqlalchemy",
        return_value=fake_sa,
    ):
        c.connect()
        c._table_cache = ["patient"]
        c._column_cache["patient"] = [("PatNum", "text")]
        fake_result = MagicMock()
        fake_result.mappings.return_value = iter(
            [{"PatNum": "PT-1"}]
        )
        fake_conn.execute.return_value = fake_result
        rows = list(
            c.fetch_rows(
                table_name="patient",
                columns=["PatNum"],
                limit=7,
            )
        )
    assert rows == [{"patient.PatNum": "PT-1"}]
    select_calls = [
        str(call.args[0])
        for call in fake_conn.execute.call_args_list
        if "SELECT" in str(call.args[0]).upper()
    ]
    sql = select_calls[-1]
    assert "LIMIT 7" in sql
    assert "TOP" not in sql.upper()


def test_postgres_missing_driver_raises_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    real_import_module = importlib.import_module

    def fake_import_module(name: str, *a, **k):  # type: ignore[no-untyped-def]
        if name == "pg8000":
            raise ImportError("simulated")
        return real_import_module(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    c = PostgresConnector("postgresql+pg8000://u:p@h/db")
    with pytest.raises(ConnectionError, match=r"pg8000.*\[postgres\]"):
        c.connect()
