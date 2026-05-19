"""Microsoft SQL Server connector — primary target: Dentrix.

Dentrix runs on a SQL Server backend (named instance ``DTXNAME`` by
default). This connector also covers Eaglesoft and any other MSSQL-
based PMS — the schema mapping config is what varies, not the
connector.

URL form (per SQLAlchemy)::

    mssql+pyodbc://praxis_ro:secret@dentrixsrv/DTXNAME?driver=ODBC+Driver+17+for+SQL+Server

Driver extra: ``pip install praxis-deid[mssql]`` pulls in ``pyodbc``.
ODBC runtime is OS-dependent:

  * macOS dev:  ``brew install unixodbc freetds``
  * Linux prod: ``apt install unixodbc freetds-dev tdsodbc``
  * Windows:    Microsoft ODBC Driver 17 or 18 (download from
                learn.microsoft.com)

Read-only intent:
  We issue ``SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED`` at
  connect time to minimise lock contention against the live Dentrix
  install (the practice is using the DB while we pull from it). This
  is read-only by definition — UNCOMMITTED only ever reads, never
  writes. Practices should ALSO supply a read-only DB user; we
  enforce nothing at the connector level beyond "never emit a DML
  statement".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._sqlalchemy_base import SqlAlchemyConnector, _import_sqlalchemy

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.engine import Connection


class MssqlConnector(SqlAlchemyConnector):
    """Dentrix / any-MSSQL connector."""

    pms_dialect = "mssql"
    required_driver_module = "pyodbc"
    required_extras_name = "mssql"

    # MSSQL identifier quoting is square brackets.
    def _quote_identifier(self, name: str) -> str:
        return f"[{name}]"

    def _connect_args(self) -> dict[str, Any]:
        # pyodbc uses ``timeout`` (login timeout), not ``connect_timeout``.
        return {"timeout": self.connect_timeout_seconds}

    def _list_tables_query(self) -> str:
        # SCHEMA_NAME() = the user's default schema (usually 'dbo').
        return (
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = SCHEMA_NAME() AND TABLE_TYPE = 'BASE TABLE'"
        )

    def _list_columns_query(self) -> str:
        return (
            "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = SCHEMA_NAME() AND TABLE_NAME = :table_name "
            "ORDER BY ORDINAL_POSITION"
        )

    def _pre_flight(self, conn: Connection) -> None:
        sa = _import_sqlalchemy()
        # Minimize lock contention; we only read. Safe — no row can be
        # changed by us, by definition.
        try:
            conn.execute(sa.text("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED"))
        except Exception:  # noqa: S110 — driver-specific fallback; pragma: no cover
            # Some drivers refuse this inside a transaction; tolerate.
            pass

    # MSSQL has no LIMIT clause. We rewrite the SELECT to insert ``TOP N``.
    def _limit_clause(self, limit: int) -> str:
        # The base class appends this AS A SUFFIX; for MSSQL the proper
        # spot is between SELECT and the column list. We return "" here
        # and override fetch_rows() below to splice the TOP clause in.
        return ""

    # We override fetch_rows just enough to inject TOP N. Everything
    # else (validation, allowlist, row iteration) is reused from the
    # SqlAlchemyConnector base.
    def fetch_rows(
        self,
        *,
        table_name: str,
        columns: list[str],
        where_clause: str | None = None,
        bind_params: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> Any:
        self._ensure_connected()
        known_tables = self.list_tables()
        known_columns = [c for c, _t in self.list_columns(table_name)]
        self._validate_query_inputs(
            table_name=table_name,
            columns=columns,
            where_clause=where_clause,
            limit=limit,
            known_tables=known_tables,
            known_columns=known_columns,
        )
        sa = _import_sqlalchemy()
        assert self._engine is not None

        quoted_table = self._quote_identifier(table_name)
        quoted_cols = ", ".join(self._quote_identifier(c) for c in columns)
        top = f"TOP {int(limit)} " if limit is not None else ""
        sql = f"SELECT {top}{quoted_cols} FROM {quoted_table}"  # noqa: S608
        if where_clause:
            sql += f" WHERE {where_clause}"

        params = dict(bind_params or {})
        with self._engine.connect() as conn:
            self._pre_flight(conn)
            result = conn.execute(sa.text(sql), params)
            for row in result.mappings():
                out: dict[str, Any] = {}
                for col in columns:
                    out[f"{table_name}.{col}"] = row.get(col)
                yield out


__all__ = ["MssqlConnector"]
