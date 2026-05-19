"""Phase-D connector base class.

A ``DBConnector`` is the I/O layer for the Phase-C extractor pipeline.
Its only responsibility is: given a (source table, column list, optional
WHERE clause), yield row dicts. All de-identification, Safe Harbor
banding, and canonical-row assembly happen DOWNSTREAM in
``BaseExtractor`` — connectors handle only the I/O.

Architecture:

    BaseExtractor
        -> per-extension subclass
        -> RowSource callable
            -> DBConnector.fetch_rows(...)
                -> MysqlConnector   (Open Dental etc.)
                -> MssqlConnector   (Dentrix etc.)
                -> PostgresConnector (future PMSs)
                -> JsonFixtureConnector (tests + dry runs)

Locked v0.1 modules consumed (NEVER modified):
    - praxis_deid.deidentify
    - praxis_deid.safe_harbor
    - praxis_deid.hashing

SQL-safety contract (enforced by every concrete connector):

  * ALL queries use parameterized binding (SQLAlchemy ``text()`` +
    ``:bind_param`` style). Never f-string user input into a query.
  * Table names + column names are validated against the connector's
    ``list_tables()`` / ``list_columns()`` allowlist before use. The
    allowlist is built from ``INFORMATION_SCHEMA`` (or the fixture
    JSON's top-level keys for the fixture connector).
  * ``where_clause`` (if any) is rejected if it contains ``;``, ``--``,
    ``/*``, ``*/``, or DDL/DML keywords (``DROP``, ``TRUNCATE``,
    ``DELETE``, ``UPDATE``, ``INSERT``, ``ALTER``, etc.).

Credential handling:

  * Connection URLs (which contain passwords) are NEVER logged.
  * ``connection_redacted`` returns a host-only view safe for audit
    envelopes — the password and (optionally) the user are scrubbed.
  * Connectors are explicit-lifecycle: ``connect()`` opens, ``close()``
    closes. Use as a context manager (``__enter__``/``__exit__`` are
    provided by this base) and the lifecycle is guaranteed.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

# -------------------------------------------------------------------------
# Errors
# -------------------------------------------------------------------------


class ConnectionError(RuntimeError):
    """Raised when a connector cannot establish or use its connection.

    Distinct from ``ExtractorError`` (which is a mapping-config or row-
    transformation failure). A ``ConnectionError`` is always I/O-layer:
    bad URL, bad credentials, driver missing, host unreachable, etc.
    """


# -------------------------------------------------------------------------
# SQL-safety primitives (shared with extractors.base._scan_for_forbidden_sql,
# kept in sync intentionally — DRY would couple two security gates).
# -------------------------------------------------------------------------


# Forbidden SUBSTRINGS in any WHERE clause we accept from caller code.
# Belt-and-braces against accidental concatenation of attacker-controlled
# strings into a query — though we always use parameterized binds.
_FORBIDDEN_WHERE_SUBSTRINGS: tuple[str, ...] = (";", "--", "/*", "*/")

# Whole-word DDL/DML keywords that have no business in a read-only WHERE.
_FORBIDDEN_WHERE_KEYWORDS: frozenset[str] = frozenset(
    {
        "DROP",
        "TRUNCATE",
        "DELETE",
        "UPDATE",
        "INSERT",
        "ALTER",
        "GRANT",
        "REVOKE",
        "CREATE",
        "REPLACE",
        "EXEC",
        "EXECUTE",
        "CALL",
        "MERGE",
        "ATTACH",
        "DETACH",
    }
)


def validate_where_clause(where_clause: str | None) -> None:
    """Raise ``ConnectionError`` if ``where_clause`` violates the contract.

    A WHERE clause is allowed to reference bind parameters (``:since``),
    column names, and operators — but never to contain statement
    terminators, comment markers, or DDL/DML keywords. Concrete
    connectors call this before threading the clause into ``text()``.
    """
    if where_clause is None:
        return
    text = str(where_clause)
    for bad in _FORBIDDEN_WHERE_SUBSTRINGS:
        if bad in text:
            raise ConnectionError(
                f"forbidden SQL substring {bad!r} in where_clause: {text!r}"
            )
    # Strip single-quoted string literals so 'drop' inside a literal
    # doesn't false-trigger.
    stripped = re.sub(r"'([^']|'')*'", "''", text)
    for kw in _FORBIDDEN_WHERE_KEYWORDS:
        if re.search(rf"\b{kw}\b", stripped, flags=re.IGNORECASE):
            raise ConnectionError(
                f"forbidden SQL keyword {kw!r} in where_clause: {text!r}"
            )


# Valid identifier (table or column name) per SQL-standard-ish rules.
# Letters, digits, underscores; cannot start with a digit. Length capped
# to discourage absurd inputs. Concrete connectors reject any candidate
# that doesn't match.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def is_valid_identifier(name: str) -> bool:
    """True if ``name`` is a syntactically safe table or column identifier."""
    if not isinstance(name, str):
        return False
    return bool(_IDENT_RE.match(name))


def validate_identifier(name: str, *, kind: str) -> None:
    """Raise ``ConnectionError`` if ``name`` isn't a safe identifier."""
    if not is_valid_identifier(name):
        raise ConnectionError(
            f"invalid {kind} identifier: {name!r} "
            "(must match [A-Za-z_][A-Za-z0-9_]*, max 128 chars)"
        )


def redact_connection_url(url: str) -> str:
    """Return a host-only view of a connection URL, safe for audit logs.

    The password is scrubbed; the user is preserved (it's not a secret
    by itself, and helps a HIPAA reviewer answer "which DB user ran this
    pull"). Examples::

        mysql+mysqlconnector://praxis_ro:secret@host:3306/db
            -> mysql+mysqlconnector://praxis_ro:***@host:3306/db

        mssql+pyodbc://praxis_ro:p%40ss@host/db?driver=ODBC+...
            -> mssql+pyodbc://praxis_ro:***@host/db?driver=ODBC+...

        fixture-json:///abs/path/to/file.json
            -> fixture-json:///abs/path/to/file.json
    """
    if url is None:
        return ""
    # Match "scheme://user:password@rest" and replace password with ***.
    # Be permissive about scheme + user character classes; passwords often
    # contain URL-encoded specials.
    m = re.match(r"^([^:]+://)([^:/@]+):([^@]*)@(.+)$", url)
    if m:
        return f"{m.group(1)}{m.group(2)}:***@{m.group(4)}"
    return url


# -------------------------------------------------------------------------
# DBConnector
# -------------------------------------------------------------------------


class DBConnector(ABC):
    """Abstract base for live-DB connectors used by the extractor pipeline.

    Subclasses MUST implement:
      * ``connect()`` — opens the underlying engine/connection.
      * ``list_tables()`` — schema-only sweep (used by the wizard).
      * ``list_columns(table)`` — schema-only column listing.
      * ``fetch_rows(...)`` — yields row dicts.
      * ``close()`` — releases resources.
      * ``pms_dialect`` property — one of 'mysql','mssql','postgres','fixture'.

    The base class provides:
      * ``__enter__`` / ``__exit__`` so the connector works as a context
        manager.
      * ``redacted_url`` for audit logging.
      * Shared validation helpers (``validate_where_clause``,
        ``validate_identifier``).
    """

    #: Concrete subclasses set this. Used by ``BaseExtractor`` and the
    #: audit log to pick the right mapping-config flavor (e.g. Open
    #: Dental on MySQL vs. Dentrix on MSSQL have different table names).
    pms_dialect: str = ""

    def __init__(self, connection_url: str) -> None:
        self.connection_url = connection_url
        self.redacted_url = redact_connection_url(connection_url)
        self._connected = False

    # --- lifecycle ---------------------------------------------------------

    @abstractmethod
    def connect(self) -> None:
        """Open the underlying engine/connection. Idempotent — calling
        twice is a no-op."""

    @abstractmethod
    def close(self) -> None:
        """Release resources. Idempotent."""

    def __enter__(self) -> DBConnector:
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # --- schema introspection ---------------------------------------------

    @abstractmethod
    def list_tables(self) -> list[str]:
        """Return the names of every table the connector can read.

        Schema metadata only — NEVER reads a user-data row. Used by the
        wizard's schema-only sweep.
        """

    @abstractmethod
    def list_columns(self, table_name: str) -> list[tuple[str, str]]:
        """Return ``[(column_name, column_type), ...]`` for ``table_name``.

        Schema-only. Raises ``ConnectionError`` if ``table_name`` isn't
        in ``list_tables()``.
        """

    # --- row fetch ---------------------------------------------------------

    @abstractmethod
    def fetch_rows(
        self,
        *,
        table_name: str,
        columns: list[str],
        where_clause: str | None = None,
        bind_params: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield rows as dicts.

        Contract:
          * ``table_name`` MUST appear in ``list_tables()``. Concrete
            connectors validate this against the allowlist before use.
          * Every name in ``columns`` MUST appear in
            ``list_columns(table_name)``. Concrete connectors validate
            this before use.
          * ``where_clause`` (if any) is a SQLAlchemy-textual fragment
            that may reference ``:name`` bind parameters from
            ``bind_params``. It is validated by
            ``validate_where_clause`` to reject ``;``, ``--``, ``/*``,
            ``*/``, and DDL/DML keywords.
          * ``bind_params`` are passed verbatim to SQLAlchemy's
            ``Connection.execute(text(...), bind_params)``. NEVER
            string-interpolated into the SQL.
          * Rows yielded are plain dicts keyed by qualified column name
            (``"treatplan.PatNum"`` style); the connector adds the table
            prefix so the extractor's row dict shape matches the
            existing fixture-JSON contract.
        """

    # --- helpers usable by subclasses + tests -----------------------------

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise ConnectionError(
                f"{type(self).__name__} not connected; call connect() first"
            )

    def _validate_query_inputs(
        self,
        *,
        table_name: str,
        columns: list[str],
        where_clause: str | None,
        limit: int | None,
        known_tables: list[str] | None = None,
        known_columns: list[str] | None = None,
    ) -> None:
        """Defense-in-depth check before any query is built.

        Concrete connectors call this from ``fetch_rows`` before
        constructing the SQL. The ``known_tables`` / ``known_columns``
        arguments let the connector pass in pre-computed allowlists to
        avoid an extra round-trip per query.
        """
        validate_identifier(table_name, kind="table")
        for c in columns:
            validate_identifier(c, kind="column")
        validate_where_clause(where_clause)
        if limit is not None and (not isinstance(limit, int) or limit < 0):
            raise ConnectionError(
                f"limit must be a non-negative int or None, got {limit!r}"
            )
        if known_tables is not None and table_name not in known_tables:
            raise ConnectionError(
                f"table {table_name!r} not in allowlist; "
                f"known tables: {sorted(known_tables)[:8]}..."
            )
        if known_columns is not None:
            unknown = [c for c in columns if c not in known_columns]
            if unknown:
                raise ConnectionError(
                    f"columns {unknown!r} not in allowlist for "
                    f"table {table_name!r}"
                )


__all__ = [
    "ConnectionError",
    "DBConnector",
    "is_valid_identifier",
    "redact_connection_url",
    "validate_identifier",
    "validate_where_clause",
]
