"""Read PMS database SCHEMA METADATA only — never row data.

This is the load-bearing HIPAA boundary. The wizard's whole legitimacy
rests on it being structurally impossible for this module to fetch row
values, even by accident.

Three input modes are supported:

  1. JSON dump file — pre-extracted schema, used in tests + offline review.
  2. SQL dump file — DDL-only export (CREATE TABLE statements). Parsed
     for table + column names. NOT a general-purpose SQL parser; supports
     a subset sufficient for typical PMS exports.
  3. Live SQLAlchemy connection — uses information_schema queries ONLY.
     Never SELECTs from the actual data tables. The connection object
     is never reused outside of metadata reflection.

The output is a normalized `PmsSchema` dataclass tree. Downstream, the
Claude mapper takes a `PmsSchema` and produces a mapping config; the
mapper has no path back to the live DB.

Architecturally, the data flow is:

    PMS DB ─── (information_schema only) ───> PmsSchema (in-memory)
                                                     │
                                                     v
                                                Claude API
                                                     │
                                                     v
                                                MappingConfig

The PMS DB connection is never seen by the Claude mapper. Even if the
mapper were compromised, it has no DB cursor to query.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ForeignKey:
    """A foreign key relationship between columns.

    All names are SOURCE-PMS names verbatim — no normalization, no
    transformation. The mapper sees what the practice's schema actually
    looks like.
    """

    column: str
    referenced_table: str
    referenced_column: str

    def to_dict(self) -> dict[str, str]:
        return {
            "column": self.column,
            "referenced_table": self.referenced_table,
            "referenced_column": self.referenced_column,
        }


@dataclass(frozen=True)
class ColumnSchema:
    """One column in a PMS table — metadata only."""

    name: str
    type: str  # source PMS type string, e.g. "VARCHAR(255)", "INT", "DATETIME"
    nullable: bool
    description: str | None = None  # PMS-doc-sourced description, if available

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "nullable": self.nullable,
            "description": self.description,
        }


@dataclass(frozen=True)
class TableSchema:
    """One table in a PMS database — metadata only."""

    name: str
    columns: tuple[ColumnSchema, ...]
    primary_key: tuple[str, ...] = field(default_factory=tuple)
    foreign_keys: tuple[ForeignKey, ...] = field(default_factory=tuple)
    indexes: tuple[str, ...] = field(default_factory=tuple)
    description: str | None = None

    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "columns": [c.to_dict() for c in self.columns],
            "primary_key": list(self.primary_key),
            "foreign_keys": [fk.to_dict() for fk in self.foreign_keys],
            "indexes": list(self.indexes),
        }


@dataclass(frozen=True)
class PmsSchema:
    """A complete PMS schema — metadata only.

    `tables` is a dict by table name for O(1) lookup. The order of insertion
    is preserved (Python 3.7+) so deterministic serialization Just Works.
    """

    pms_name: str  # "open_dental" | "dentrix" | "eaglesoft" | etc.
    tables: dict[str, TableSchema]
    source_description: str | None = None  # e.g. "snapshot from public docs, 2026-05"

    def to_dict(self) -> dict[str, Any]:
        return {
            "pms_name": self.pms_name,
            "source_description": self.source_description,
            "tables": {name: t.to_dict() for name, t in self.tables.items()},
        }


# -----------------------------------------------------------------------
# JSON dump reader
# -----------------------------------------------------------------------

def read_pms_schema_from_json(path: Path) -> PmsSchema:
    """Load a PmsSchema from a pre-extracted JSON dump.

    The JSON shape mirrors `PmsSchema.to_dict()` — see the test fixture
    `tests/fixtures/open_dental_schema.json` for an example.

    This is the path used by tests and by practices that want to review
    their schema offline before sending it to Claude.
    """
    raw = json.loads(path.read_text())
    return _pms_schema_from_dict(raw)


def _pms_schema_from_dict(raw: dict[str, Any]) -> PmsSchema:
    pms_name = str(raw["pms_name"])
    source_description = raw.get("source_description")
    tables_raw = raw.get("tables") or {}
    if not isinstance(tables_raw, dict):
        raise ValueError(
            "tables must be a dict keyed by table name; got "
            f"{type(tables_raw).__name__}"
        )

    tables: dict[str, TableSchema] = {}
    for table_name, table_raw in tables_raw.items():
        tables[str(table_name)] = _table_schema_from_dict(str(table_name), table_raw)

    return PmsSchema(
        pms_name=pms_name,
        tables=tables,
        source_description=source_description,
    )


def _table_schema_from_dict(name: str, raw: dict[str, Any]) -> TableSchema:
    columns_raw = raw.get("columns") or []
    columns = tuple(
        ColumnSchema(
            name=str(c["name"]),
            type=str(c["type"]),
            nullable=bool(c.get("nullable", True)),
            description=c.get("description"),
        )
        for c in columns_raw
    )
    fks_raw = raw.get("foreign_keys") or []
    foreign_keys = tuple(
        ForeignKey(
            column=str(fk["column"]),
            referenced_table=str(fk["referenced_table"]),
            referenced_column=str(fk["referenced_column"]),
        )
        for fk in fks_raw
    )
    return TableSchema(
        name=name,
        description=raw.get("description"),
        columns=columns,
        primary_key=tuple(str(p) for p in (raw.get("primary_key") or [])),
        foreign_keys=foreign_keys,
        indexes=tuple(str(i) for i in (raw.get("indexes") or [])),
    )


# -----------------------------------------------------------------------
# SQL DDL parser (best-effort, schema-only)
# -----------------------------------------------------------------------

# Match the OPENING of `CREATE TABLE [IF NOT EXISTS] [`schema`.]`name` (`.
# We don't try to parse arbitrary SQL — just enough to extract identifiers.
_CREATE_TABLE_RE = re.compile(
    r"""CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?
        (?:`?[\w]+`?\.)?            # optional schema prefix
        `?([\w]+)`?                  # table name (group 1)
        \s*\(""",
    re.IGNORECASE | re.VERBOSE,
)
_COLUMN_DEF_RE = re.compile(
    r"""^\s*
        `?([\w]+)`?                  # column name (group 1)
        \s+
        ([A-Za-z][\w()]*(?:\s+UNSIGNED)?(?:\s+ZEROFILL)?)  # type (group 2)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_NOT_NULL_RE = re.compile(r"\bNOT\s+NULL\b", re.IGNORECASE)
_PRIMARY_KEY_RE = re.compile(
    r"PRIMARY\s+KEY\s*\(\s*((?:`?[\w]+`?\s*,?\s*)+)\)", re.IGNORECASE
)
_FOREIGN_KEY_RE = re.compile(
    r"""FOREIGN\s+KEY\s*\(\s*`?([\w]+)`?\s*\)
        \s+REFERENCES\s+`?([\w]+)`?\s*\(\s*`?([\w]+)`?\s*\)""",
    re.IGNORECASE | re.VERBOSE,
)


def read_pms_schema_from_sql_dump(path: Path, *, pms_name: str) -> PmsSchema:
    """Best-effort parse of a DDL-only SQL dump.

    This is intentionally narrow: it understands `CREATE TABLE` statements,
    inline `NOT NULL`, `PRIMARY KEY (...)` clauses, and `FOREIGN KEY (...)
    REFERENCES ...` clauses. It ignores everything else (triggers,
    procedures, views, comments, vendor extensions).

    Refuses to parse anything that smells like data — `INSERT INTO`,
    `COPY`, `LOAD DATA`. If those statements appear, we abort with a
    clear error rather than silently letting row data into the analyzer.
    Schema-only is the contract.
    """
    text = path.read_text()

    # Defensive: any statement that ships row data terminates parsing.
    # The wizard's value prop is "schema only" — if a dump file contains
    # rows we fail loudly rather than try to filter them out.
    for forbidden in (
        r"\bINSERT\s+INTO\b",
        r"\bCOPY\s+\w+\s+FROM\b",
        r"\bLOAD\s+DATA\b",
        r"\bBULK\s+INSERT\b",
    ):
        if re.search(forbidden, text, re.IGNORECASE):
            raise ValueError(
                f"SQL dump contains row data (matched {forbidden!r}); refusing to "
                "parse. Re-export with --no-data / SCHEMA-ONLY and try again."
            )

    tables: dict[str, TableSchema] = {}

    # Walk the file picking off CREATE TABLE blocks, brace-matching to find
    # the matching close paren of the column list.
    pos = 0
    while True:
        m = _CREATE_TABLE_RE.search(text, pos)
        if m is None:
            break
        table_name = m.group(1)
        body_start = m.end()
        body_end = _find_matching_paren(text, body_start - 1)
        if body_end == -1:
            # Malformed; skip and advance so we don't infinite-loop.
            pos = body_start
            continue
        body = text[body_start:body_end]
        tables[table_name] = _parse_create_table_body(table_name, body)
        pos = body_end + 1

    return PmsSchema(pms_name=pms_name, tables=tables)


def _find_matching_paren(text: str, open_idx: int) -> int:
    """Return index of the close paren that balances text[open_idx]."""
    depth = 0
    in_string: str | None = None
    i = open_idx
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == in_string and text[i - 1] != "\\":
                in_string = None
        elif ch in ("'", '"', "`"):
            in_string = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _parse_create_table_body(table_name: str, body: str) -> TableSchema:
    columns: list[ColumnSchema] = []
    primary_key: tuple[str, ...] = ()
    foreign_keys: list[ForeignKey] = []

    # Split by commas at depth 0 — column defs and constraints alternate.
    parts = _split_top_level(body)
    for raw in parts:
        stripped = raw.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper.startswith(("PRIMARY KEY", "PRIMARY  KEY")):
            pk_match = _PRIMARY_KEY_RE.search(stripped)
            if pk_match:
                primary_key = tuple(
                    c.strip(" `") for c in pk_match.group(1).split(",") if c.strip()
                )
            continue
        if upper.startswith(("FOREIGN KEY", "CONSTRAINT")):
            fk_match = _FOREIGN_KEY_RE.search(stripped)
            if fk_match:
                foreign_keys.append(
                    ForeignKey(
                        column=fk_match.group(1),
                        referenced_table=fk_match.group(2),
                        referenced_column=fk_match.group(3),
                    )
                )
            continue
        if upper.startswith(("KEY ", "INDEX ", "UNIQUE", "CHECK", "CONSTRAINT")):
            continue
        col_match = _COLUMN_DEF_RE.match(stripped)
        if col_match is None:
            continue
        col_name = col_match.group(1)
        col_type = col_match.group(2).upper()
        nullable = _NOT_NULL_RE.search(stripped) is None
        columns.append(
            ColumnSchema(name=col_name, type=col_type, nullable=nullable)
        )

    return TableSchema(
        name=table_name,
        columns=tuple(columns),
        primary_key=primary_key,
        foreign_keys=tuple(foreign_keys),
    )


def _split_top_level(body: str) -> list[str]:
    """Split `body` on commas that aren't inside nested parens or strings."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_string: str | None = None
    for ch in body:
        if in_string:
            buf.append(ch)
            if ch == in_string:
                in_string = None
            continue
        if ch in ("'", '"', "`"):
            in_string = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
            continue
        if ch == ")":
            depth -= 1
            buf.append(ch)
            continue
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


# -----------------------------------------------------------------------
# Live DB reader (SQLAlchemy)
# -----------------------------------------------------------------------

def read_pms_schema_from_sqlalchemy(
    engine: Any,  # sqlalchemy.engine.Engine — Any to keep SA an optional dep
    *,
    pms_name: str,
    schema: str | None = None,
) -> PmsSchema:
    """Reflect a live database via SQLAlchemy's MetaData (information_schema).

    SQLAlchemy reflection uses information_schema (or vendor equivalents)
    under the hood. NO data tables are queried. We do NOT issue any SELECT
    against user data — we ONLY call `MetaData.reflect()`.

    SQLAlchemy is an optional dependency for this tool. If you don't have
    it installed, use the JSON or SQL-dump reader instead.
    """
    try:
        from sqlalchemy import MetaData
    except ImportError as err:  # pragma: no cover - dep optional
        raise ImportError(
            "read_pms_schema_from_sqlalchemy requires sqlalchemy: "
            "pip install 'praxis-deid[wizard]'"
        ) from err

    metadata = MetaData(schema=schema)
    metadata.reflect(bind=engine)

    tables: dict[str, TableSchema] = {}
    for sa_table in metadata.sorted_tables:
        columns = tuple(
            ColumnSchema(
                name=col.name,
                type=str(col.type),
                nullable=bool(col.nullable),
            )
            for col in sa_table.columns
        )
        pk = tuple(c.name for c in sa_table.primary_key.columns)
        fks: list[ForeignKey] = []
        for fk in sa_table.foreign_keys:
            ref_table = fk.column.table.name
            fks.append(
                ForeignKey(
                    column=fk.parent.name,
                    referenced_table=ref_table,
                    referenced_column=fk.column.name,
                )
            )
        indexes = tuple(idx.name or "" for idx in sa_table.indexes)
        tables[sa_table.name] = TableSchema(
            name=sa_table.name,
            columns=columns,
            primary_key=pk,
            foreign_keys=tuple(fks),
            indexes=indexes,
        )

    return PmsSchema(pms_name=pms_name, tables=tables)


# -----------------------------------------------------------------------
# Unified entry point
# -----------------------------------------------------------------------

def read_pms_schema(
    *,
    schema_file: Path | None = None,
    sql_dump: Path | None = None,
    sqlalchemy_engine: Any | None = None,
    pms_name: str | None = None,
) -> PmsSchema:
    """Single entry point for any of the three input modes.

    Exactly one of {schema_file, sql_dump, sqlalchemy_engine} must be set.
    `pms_name` is required for sql_dump and sqlalchemy_engine modes; the
    JSON dump carries its own pms_name field.
    """
    provided = sum(x is not None for x in (schema_file, sql_dump, sqlalchemy_engine))
    if provided != 1:
        raise ValueError(
            "read_pms_schema requires exactly one of "
            "{schema_file, sql_dump, sqlalchemy_engine}"
        )

    if schema_file is not None:
        return read_pms_schema_from_json(schema_file)
    if sql_dump is not None:
        if not pms_name:
            raise ValueError("pms_name is required when reading from a SQL dump")
        return read_pms_schema_from_sql_dump(sql_dump, pms_name=pms_name)
    assert sqlalchemy_engine is not None  # for type checker
    if not pms_name:
        raise ValueError("pms_name is required when reading from a SQLAlchemy engine")
    return read_pms_schema_from_sqlalchemy(sqlalchemy_engine, pms_name=pms_name)


# Convenience for tests / debug — never used in the main wizard path.
def schema_to_json(schema: PmsSchema) -> str:
    return json.dumps(schema.to_dict(), indent=2, sort_keys=True)


__all__ = [
    "ColumnSchema",
    "ForeignKey",
    "PmsSchema",
    "TableSchema",
    "read_pms_schema",
    "read_pms_schema_from_json",
    "read_pms_schema_from_sql_dump",
    "read_pms_schema_from_sqlalchemy",
    "schema_to_json",
]


# Used by claude_mapper for serialization — it imports asdict from here
# rather than pulling dataclasses everywhere.
def _to_dict(obj: Any) -> dict[str, Any]:
    return asdict(obj)
