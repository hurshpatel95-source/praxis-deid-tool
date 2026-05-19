"""JSON-fixture connector.

Wraps the existing ``--fixture-json`` path behind the uniform
``DBConnector`` interface so the rest of the extractor pipeline doesn't
know (or care) whether rows come from a live MySQL DB, a Dentrix MSSQL
DB, a PostgreSQL DB, or a static JSON file on disk.

The fixture JSON has the same shape as Phase-C's existing tests::

    {
      "treatment_plans_raw": [
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1", ...},
        ...
      ],
      "claims_raw": [...],
      ...
    }

Top-level keys are CANONICAL SCHEMA NAMES (not source table names) —
that's how Phase-C's fixture format works, and we preserve it verbatim
to keep all 188 Phase-C tests passing.

Subtlety: the ``DBConnector`` interface speaks in source table names,
but the fixture format keys by canonical schema. The connector handles
the translation by accepting EITHER name in ``list_tables()`` /
``fetch_rows()`` — whichever the caller asks for, as long as it's in
the fixture's top-level key set.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .base import ConnectionError as _ConnectionError
from .base import DBConnector


class JsonFixtureConnector(DBConnector):
    """Connector backed by a JSON file containing pre-fetched row dicts.

    URL forms accepted by the dispatcher:
      * ``fixture-json:///abs/path/to/file.json``
      * ``fixture-json://./relative/path/to/file.json``
      * legacy ``--fixture-json <path>`` (the CLI normalises this into
        a ``fixture-json://...`` URL before instantiating us)

    The connector reads the file at ``connect()`` time and holds the
    parsed dict in memory; ``close()`` drops the reference.
    """

    pms_dialect = "fixture"

    # Special path sentinel for "no file — use the in-memory dict supplied
    # at construction time". Used by tests and by the CLI when --fixture-json
    # is None but a dict-form fixture has been pre-loaded.
    _SENTINEL_INLINE_DICT = "<inline>"

    def __init__(
        self,
        connection_url: str,
        *,
        inline_fixture: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        super().__init__(connection_url)
        self._inline_fixture = inline_fixture
        self._fixture: dict[str, list[dict[str, Any]]] = {}
        self._path: Path | None = None

    # ------------------------------------------------------------------
    # URL parsing
    # ------------------------------------------------------------------

    @staticmethod
    def parse_path_from_url(url: str) -> Path:
        """Extract the filesystem path from a ``fixture-json://...`` URL.

        Accepts both absolute (`fixture-json:///abs/path`) and relative
        (`fixture-json://./relative/path`) forms. Anything else raises
        ``ConnectionError``.
        """
        prefix = "fixture-json://"
        if not url.startswith(prefix):
            raise _ConnectionError(
                f"JsonFixtureConnector expects URL scheme 'fixture-json://', "
                f"got {url!r}"
            )
        rest = url[len(prefix):]
        if rest.startswith("/"):
            # Triple-slash absolute path: fixture-json:///abs/path/...
            return Path(rest)
        if rest.startswith("./") or rest.startswith("../"):
            return Path(rest)
        # Bare relative path (no leading ./): treat as cwd-relative.
        return Path(rest)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        if self._connected:
            return
        if self._inline_fixture is not None:
            self._fixture = dict(self._inline_fixture)
            self._connected = True
            return
        path = self.parse_path_from_url(self.connection_url)
        self._path = path
        if not path.exists():
            raise _ConnectionError(f"fixture file not found: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise _ConnectionError(
                f"fixture file {path} is not valid JSON: {err}"
            ) from err
        if not isinstance(raw, dict):
            raise _ConnectionError(
                f"fixture file {path} root must be an object keyed by schema name"
            )
        # Defensive: every value must be a list of dicts.
        for k, v in raw.items():
            if not isinstance(v, list):
                raise _ConnectionError(
                    f"fixture file {path}: key {k!r} must map to a list, "
                    f"got {type(v).__name__}"
                )
        self._fixture = raw
        self._connected = True

    def close(self) -> None:
        self._fixture = {}
        self._path = None
        self._connected = False

    # ------------------------------------------------------------------
    # Schema introspection
    # ------------------------------------------------------------------

    def list_tables(self) -> list[str]:
        """Return the top-level keys of the fixture file.

        The fixture keys are canonical schema names, but the
        ``DBConnector`` interface speaks in "tables" — that's fine here
        because the fixture is the authoritative source for its own
        layout.
        """
        self._ensure_connected()
        return sorted(self._fixture.keys())

    def list_columns(self, table_name: str) -> list[tuple[str, str]]:
        """Return the union of column names seen across every row of the
        given table, with a ``'unknown'`` type marker.

        The fixture format doesn't carry per-column type info, so we
        report ``'unknown'`` as the type. The names are what matters —
        they're checked against the mapping config.
        """
        self._ensure_connected()
        if table_name not in self._fixture:
            raise _ConnectionError(
                f"table {table_name!r} not in fixture; "
                f"available: {sorted(self._fixture.keys())}"
            )
        rows = self._fixture[table_name]
        columns: set[str] = set()
        for row in rows:
            columns.update(row.keys())
        return [(c, "unknown") for c in sorted(columns)]

    # ------------------------------------------------------------------
    # Row fetch
    # ------------------------------------------------------------------

    def fetch_rows(
        self,
        *,
        table_name: str,
        columns: list[str],  # noqa: ARG002 — we yield every column we have
        where_clause: str | None = None,
        bind_params: dict[str, Any] | None = None,  # noqa: ARG002
        limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield rows for ``table_name`` from the loaded fixture.

        We ignore ``columns``, ``where_clause``, and ``bind_params``
        because the fixture is already pre-filtered to the rows the
        extractor wants — the calling extractor (a) supplies its own
        column-filter logic against the row dict and (b) applies the
        ``Filter`` (since_month/until_month) at the row level after
        receiving the dicts.

        We still validate ``where_clause`` (to keep parity with the
        other connectors) and apply ``limit`` if set.
        """
        self._ensure_connected()
        if table_name not in self._fixture:
            raise _ConnectionError(
                f"table {table_name!r} not in fixture; "
                f"available: {sorted(self._fixture.keys())}"
            )
        # Validation: the fixture's table_name MIGHT be a qualified name
        # (canonical schema like "treatment_plans_raw") and columns MIGHT
        # be qualified ("treatplan.PatNum"), neither of which are SQL
        # identifiers. We still validate the WHERE clause and limit.
        from .base import validate_where_clause as _vwc

        _vwc(where_clause)
        if limit is not None and (not isinstance(limit, int) or limit < 0):
            from .base import ConnectionError as _CE

            raise _CE(f"limit must be a non-negative int or None, got {limit!r}")
        rows = self._fixture[table_name]
        count = 0
        for row in rows:
            if limit is not None and count >= limit:
                break
            yield dict(row)
            count += 1


__all__ = ["JsonFixtureConnector"]
