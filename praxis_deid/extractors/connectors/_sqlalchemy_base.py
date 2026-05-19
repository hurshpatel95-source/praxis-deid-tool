"""Shared SQLAlchemy machinery for the three live-DB connectors.

Open Dental (MySQL), Dentrix (MSSQL), and the future-PMS PostgreSQL
connector all use SQLAlchemy 2.x as their DBAPI abstraction. The only
real differences between them are:

  * the driver-install extra (``mysql-connector-python``, ``pyodbc``,
    ``pg8000``);
  * the ``INFORMATION_SCHEMA`` dialect (MSSQL uses ``SCHEMA_NAME()``;
    MySQL uses ``DATABASE()``; PostgreSQL uses ``current_schema``);
  * the per-dialect connection-pre-flight (MSSQL likes
    ``SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED`` so it doesn't
    take locks against a live Dentrix install).

This module factors out the 90% that's identical, so each per-dialect
connector is a thin subclass that supplies the dialect-specific bits.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from .base import ConnectionError as _ConnectionError
from .base import DBConnector

if TYPE_CHECKING:  # pragma: no cover — type-checking only
    from sqlalchemy.engine import Connection, Engine


def _import_sqlalchemy() -> Any:
    """Lazy-import SQLAlchemy with a friendly error if missing."""
    try:
        return importlib.import_module("sqlalchemy")
    except ImportError as err:  # pragma: no cover — env-dependent
        raise _ConnectionError(
            "SQLAlchemy is required for live-DB connectors. "
            "Install one of the connector extras: "
            "`pip install praxis-deid[mysql]` / `[mssql]` / `[postgres]` / "
            "`[all-connectors]`."
        ) from err


def _require_driver(module_name: str, extras_name: str) -> None:
    """Lazy-check a DBAPI driver and surface a friendly install hint."""
    try:
        importlib.import_module(module_name)
    except ImportError as err:  # pragma: no cover — env-dependent
        raise _ConnectionError(
            f"driver {module_name!r} is not installed. Install the "
            f"connector extra: `pip install praxis-deid[{extras_name}]`."
        ) from err


class SqlAlchemyConnector(DBConnector):
    """Half-abstract SQLAlchemy-backed connector.

    Subclasses set:
      * ``pms_dialect``    — 'mysql' / 'mssql' / 'postgres'.
      * ``required_driver_module`` — Python module name to lazy-check.
      * ``required_extras_name`` — the ``[mysql]`` / ``[mssql]`` / ``[postgres]``
        extras-name to suggest in the missing-driver error.
      * ``_list_tables_query()`` — SQL string returning a single column
        of table names, scoped to the current schema.
      * ``_list_columns_query()`` — SQL string with one bind param
        (``:table_name``), returning ``(column_name, data_type)`` rows.

    Subclasses may override:
      * ``_pre_flight(conn)`` — run dialect-specific connection setup
        (e.g. set isolation level). Default is a no-op.
    """

    required_driver_module: str = ""
    required_extras_name: str = ""

    # Connection timeout (seconds). SQLAlchemy passes this to the
    # underlying DBAPI via ``connect_args``.
    connect_timeout_seconds: int = 30

    def __init__(self, connection_url: str) -> None:
        super().__init__(connection_url)
        self._engine: Engine | None = None
        self._table_cache: list[str] | None = None
        # Per-table column cache. Built lazily on first list_columns / fetch.
        self._column_cache: dict[str, list[tuple[str, str]]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        if self._connected:
            return
        _require_driver(self.required_driver_module, self.required_extras_name)
        sa = _import_sqlalchemy()
        connect_args = self._connect_args()
        try:
            self._engine = sa.create_engine(
                self.connection_url,
                pool_size=1,
                max_overflow=0,
                connect_args=connect_args,
                # Read-only intent — we never issue mutations. Still
                # belt-and-braces; the practice should also use a
                # read-only DB user.
                future=True,
            )
            # Eager pre-flight: ensure the URL is parseable + reachable.
            with self._engine.connect() as conn:
                self._pre_flight(conn)
        except _ConnectionError:
            raise
        except Exception as err:
            # Surface as ConnectionError without leaking the URL.
            raise _ConnectionError(
                f"could not connect via {self.redacted_url}: "
                f"{type(err).__name__}: {err}"
            ) from err
        self._connected = True

    def close(self) -> None:
        if self._engine is not None:
            try:
                self._engine.dispose()
            except Exception:  # noqa: S110 — defensive on close; pragma: no cover
                pass
            self._engine = None
        self._table_cache = None
        self._column_cache = {}
        self._connected = False

    # ------------------------------------------------------------------
    # Driver / dialect hooks
    # ------------------------------------------------------------------

    def _connect_args(self) -> dict[str, Any]:
        """``connect_args=...`` passed to ``create_engine``.

        Default applies the connect-timeout via the most-common DBAPI
        keyword (``connect_timeout``); subclasses override if the
        driver uses a different name (pyodbc uses ``timeout``).
        """
        return {"connect_timeout": self.connect_timeout_seconds}

    def _pre_flight(self, conn: Connection) -> None:
        """Hook for dialect-specific connection setup. Default no-op."""

    def _list_tables_query(self) -> str:
        raise NotImplementedError

    def _list_columns_query(self) -> str:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Schema introspection (shared)
    # ------------------------------------------------------------------

    def list_tables(self) -> list[str]:
        self._ensure_connected()
        if self._table_cache is not None:
            return list(self._table_cache)
        sa = _import_sqlalchemy()
        assert self._engine is not None
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(self._list_tables_query())).all()
        tables = sorted({str(r[0]) for r in rows if r[0] is not None})
        self._table_cache = tables
        return list(tables)

    def list_columns(self, table_name: str) -> list[tuple[str, str]]:
        self._ensure_connected()
        if table_name in self._column_cache:
            return list(self._column_cache[table_name])
        # Validate the requested name against the table allowlist.
        known = self.list_tables()
        if table_name not in known:
            raise _ConnectionError(
                f"table {table_name!r} not in schema; "
                f"known tables (sample): {known[:8]}..."
            )
        sa = _import_sqlalchemy()
        assert self._engine is not None
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(self._list_columns_query()),
                {"table_name": table_name},
            ).all()
        result = [(str(r[0]), str(r[1])) for r in rows]
        self._column_cache[table_name] = result
        return list(result)

    # ------------------------------------------------------------------
    # Row fetch (shared)
    # ------------------------------------------------------------------

    def fetch_rows(
        self,
        *,
        table_name: str,
        columns: list[str],
        where_clause: str | None = None,
        bind_params: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        self._ensure_connected()
        # Build the allowlists once per query.
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
        sql = f"SELECT {quoted_cols} FROM {quoted_table}"  # noqa: S608 — names validated above
        if where_clause:
            sql += f" WHERE {where_clause}"
        if limit is not None:
            # LIMIT N appended; safe because we validated the int.
            sql += self._limit_clause(int(limit))

        params = dict(bind_params or {})
        with self._engine.connect() as conn:
            self._pre_flight(conn)
            result = conn.execute(sa.text(sql), params)
            for row in result.mappings():
                # SQLAlchemy returns a Row-mapping keyed by column name.
                # Re-key into "table.column" form so the dict shape
                # matches the existing fixture-JSON contract.
                out: dict[str, Any] = {}
                for col in columns:
                    out[f"{table_name}.{col}"] = row.get(col)
                yield out

    # ------------------------------------------------------------------
    # Identifier quoting (overridden per dialect for the corner cases)
    # ------------------------------------------------------------------

    def _quote_identifier(self, name: str) -> str:
        """Quote an identifier safely for this dialect.

        The base uses double-quotes (SQL standard). MySQL overrides to
        backticks; MSSQL uses square brackets. ``name`` MUST already
        be validated by ``validate_identifier`` — this method does NOT
        re-escape.
        """
        return f'"{name}"'

    def _limit_clause(self, limit: int) -> str:
        """Per-dialect ``LIMIT`` suffix.

        Default is the ANSI ``LIMIT N`` (MySQL/Postgres). MSSQL has no
        such clause and overrides to inject ``TOP N`` into the SELECT
        instead.
        """
        return f" LIMIT {limit}"


__all__ = ["SqlAlchemyConnector"]
