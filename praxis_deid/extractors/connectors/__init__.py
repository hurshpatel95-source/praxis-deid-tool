"""Phase-D live-DB connector layer.

This package exposes the four ``DBConnector`` implementations that the
``praxis-deid extract`` subcommand dispatches to:

  * :class:`MysqlConnector`     — Open Dental + any MySQL/MariaDB PMS
  * :class:`MssqlConnector`     — Dentrix + any MS SQL Server PMS
  * :class:`PostgresConnector`  — any PostgreSQL-backed PMS (future)
  * :class:`JsonFixtureConnector` — refactored fixture-JSON path

The CLI builds a connector via :func:`connector_for_url`, which inspects
the URL scheme and constructs the right subclass. All four implement
the same :class:`DBConnector` interface so the rest of the extractor
pipeline is dialect-agnostic.

Example::

    >>> conn = connector_for_url(
    ...     "mysql+mysqlconnector://praxis_ro:pwd@host:3306/opendental"
    ... )
    >>> with conn:
    ...     tables = conn.list_tables()
    ...     for row in conn.fetch_rows(
    ...         table_name="treatplan",
    ...         columns=["TreatPlanNum", "PatNum", "DateTP"],
    ...     ):
    ...         ...
"""

from __future__ import annotations

from pathlib import Path

from .base import (
    ConnectionError,
    DBConnector,
    is_valid_identifier,
    redact_connection_url,
    validate_identifier,
    validate_where_clause,
)
from .json_fixture import JsonFixtureConnector
from .mssql import MssqlConnector
from .mysql import MysqlConnector
from .postgres import PostgresConnector

# Supported URL schemes, ordered so the dispatcher can show a useful
# error listing them all if an unknown scheme is supplied.
SUPPORTED_SCHEMES: tuple[str, ...] = (
    "mysql+mysqlconnector",
    "mssql+pyodbc",
    "postgresql",
    "postgresql+psycopg2",
    "postgresql+pg8000",
    "fixture-json",
)


def connector_for_url(url: str) -> DBConnector:
    """Build the right :class:`DBConnector` subclass for ``url``.

    The URL scheme (the bit before ``://``) drives the dispatch:

    +-----------------------------------+--------------------------+
    | Scheme prefix                     | Connector                |
    +===================================+==========================+
    | ``mysql+mysqlconnector``          | :class:`MysqlConnector`  |
    +-----------------------------------+--------------------------+
    | ``mssql+pyodbc``                  | :class:`MssqlConnector`  |
    +-----------------------------------+--------------------------+
    | ``postgresql`` (any sub-driver)   | :class:`PostgresConnector` |
    +-----------------------------------+--------------------------+
    | ``fixture-json``                  | :class:`JsonFixtureConnector` |
    +-----------------------------------+--------------------------+

    Raises :class:`ConnectionError` on an unknown scheme.
    """
    if not isinstance(url, str) or "://" not in url:
        raise ConnectionError(
            f"connection URL must contain '://'; got {url!r}. "
            f"Supported schemes: {', '.join(SUPPORTED_SCHEMES)}"
        )
    scheme = url.split("://", 1)[0]
    if scheme == "mysql+mysqlconnector":
        return MysqlConnector(url)
    if scheme == "mssql+pyodbc":
        return MssqlConnector(url)
    if scheme in ("postgresql", "postgresql+psycopg2", "postgresql+pg8000"):
        return PostgresConnector(url)
    if scheme == "fixture-json":
        return JsonFixtureConnector(url)
    raise ConnectionError(
        f"unsupported connection URL scheme {scheme!r}. "
        f"Supported: {', '.join(SUPPORTED_SCHEMES)}"
    )


def fixture_json_url(path: Path | str) -> str:
    """Convert a filesystem path into a ``fixture-json://...`` URL.

    Used by the CLI to normalise the legacy ``--fixture-json <path>``
    flag into a URL before dispatch.
    """
    p = Path(path)
    if p.is_absolute():
        return f"fixture-json://{p}"
    return f"fixture-json://./{p}"


__all__ = [
    "ConnectionError",
    "DBConnector",
    "JsonFixtureConnector",
    "MssqlConnector",
    "MysqlConnector",
    "PostgresConnector",
    "SUPPORTED_SCHEMES",
    "connector_for_url",
    "fixture_json_url",
    "is_valid_identifier",
    "redact_connection_url",
    "validate_identifier",
    "validate_where_clause",
]
