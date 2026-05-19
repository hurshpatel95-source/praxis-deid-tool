"""MySQL / MariaDB connector — primary target: Open Dental.

Open Dental ships with a MariaDB backend by default, and the
``opendental`` schema is what every practice's IT install lays down.
This connector also covers any other MySQL/MariaDB-based PMS (Eaglesoft
runs on MSSQL, not MySQL; Curve Dental is web-only — but any future
MySQL-based PMS works here without code changes, only a new mapping
config).

URL form (per SQLAlchemy)::

    mysql+mysqlconnector://praxis_ro:secret@dentalsrv:3306/opendental

Driver extra: ``pip install praxis-deid[mysql]`` pulls in
``mysql-connector-python``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._sqlalchemy_base import SqlAlchemyConnector

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.engine import Connection


class MysqlConnector(SqlAlchemyConnector):
    """Open Dental / any-MySQL connector."""

    pms_dialect = "mysql"
    required_driver_module = "mysql.connector"
    required_extras_name = "mysql"

    # MySQL identifier quoting is backticks.
    def _quote_identifier(self, name: str) -> str:
        return f"`{name}`"

    def _list_tables_query(self) -> str:
        # Scoped to the current database via DATABASE(). No bind param
        # needed — DATABASE() resolves on the server side.
        return (
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'"
        )

    def _list_columns_query(self) -> str:
        return (
            "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name "
            "ORDER BY ORDINAL_POSITION"
        )

    def _pre_flight(self, conn: Connection) -> None:  # noqa: ARG002
        # MySQL doesn't need a special read-only setup; the per-DB-user
        # GRANT controls that. We could SET TRANSACTION ISOLATION LEVEL
        # REPEATABLE READ but the default is fine for short-lived
        # cursors. No-op is correct.
        return None


__all__ = ["MysqlConnector"]
