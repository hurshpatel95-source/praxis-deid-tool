"""PostgreSQL connector — future-PMS support.

Phase-C ships Open Dental (MySQL) and Phase-D adds Dentrix (MSSQL). The
postgres connector is the third leg so any future PostgreSQL-backed PMS
(or a Praxis-side data warehouse mirror) plugs in for free.

URL forms (per SQLAlchemy)::

    postgresql://praxis_ro:secret@pmsserver:5432/pmsdb
    postgresql+pg8000://praxis_ro:secret@pmsserver:5432/pmsdb
    postgresql+psycopg2://praxis_ro:secret@pmsserver:5432/pmsdb

Driver extra: ``pip install praxis-deid[postgres]`` pulls in ``pg8000``
(pure-Python, no C extension). Practices that already have psycopg2
installed can also use ``postgresql+psycopg2://...`` — both drivers
work transparently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._sqlalchemy_base import SqlAlchemyConnector

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.engine import Connection


class PostgresConnector(SqlAlchemyConnector):
    """PostgreSQL connector."""

    pms_dialect = "postgres"
    required_driver_module = "pg8000"
    required_extras_name = "postgres"

    def _connect_args(self) -> dict[str, Any]:
        """pg8000 uses ``timeout``; psycopg2 uses ``connect_timeout``.

        We inspect the URL to pick the right keyword. Defaults to
        psycopg2's style if the URL doesn't specify a sub-driver.
        """
        if "+pg8000" in self.connection_url:
            return {"timeout": self.connect_timeout_seconds}
        return {"connect_timeout": self.connect_timeout_seconds}

    def _list_tables_query(self) -> str:
        # Schema-scoped; ``current_schema`` is usually 'public' but we
        # don't hard-code that. Excludes views to match the MySQL +
        # MSSQL behavior (BASE TABLE only).
        return (
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema "
            "AND table_type = 'BASE TABLE'"
        )

    def _list_columns_query(self) -> str:
        return (
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema AND table_name = :table_name "
            "ORDER BY ordinal_position"
        )

    def _pre_flight(self, conn: Connection) -> None:  # noqa: ARG002
        # Postgres doesn't need a special read-only setup; the per-DB-
        # user GRANT controls that.
        return None


__all__ = ["PostgresConnector"]
