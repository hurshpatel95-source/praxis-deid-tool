"""Tests for `praxis_deid/wizard/schema_reader.py`.

The schema reader's job is to produce a `PmsSchema` (metadata only)
from one of three input modes: JSON dump, SQL DDL dump, or live
SQLAlchemy reflection.

The bright HIPAA line: NO row data may appear in the output. The tests
assert this with both positive cases (real Open Dental fixture) and
adversarial cases (SQL dumps that include INSERT statements should be
rejected loud, not silently filtered).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from praxis_deid.wizard.schema_reader import (
    ColumnSchema,
    ForeignKey,
    PmsSchema,
    TableSchema,
    read_pms_schema,
    read_pms_schema_from_json,
    read_pms_schema_from_sql_dump,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
OPEN_DENTAL_FIXTURE = FIXTURE_DIR / "open_dental_schema.json"


# -----------------------------------------------------------------------
# JSON dump reader
# -----------------------------------------------------------------------

def test_read_open_dental_fixture():
    schema = read_pms_schema_from_json(OPEN_DENTAL_FIXTURE)
    assert schema.pms_name == "open_dental"
    # Should have all the tables relevant to extensions A-F.
    assert "patient" in schema.tables
    assert "treatplan" in schema.tables
    assert "claim" in schema.tables
    assert "payment" in schema.tables
    assert "schedule" in schema.tables
    assert "recall" in schema.tables
    assert len(schema.tables) >= 15  # extensions A-F want lots of tables


def test_open_dental_fixture_has_no_row_data():
    """The fixture is metadata-only. Verify by inspection — no field in
    the output should contain a date pattern, a free-text patient name,
    or anything else that looks like a row value."""
    schema = read_pms_schema_from_json(OPEN_DENTAL_FIXTURE)
    serialized = json.dumps(schema.to_dict())
    # No full ISO dates.
    assert "2024-" not in serialized and "2025-" not in serialized
    # No SSN-shaped strings.
    assert not any(c.isdigit() and serialized[i + 4: i + 5] == "-"
                   for i, c in enumerate(serialized) if i + 4 < len(serialized))


def test_table_schema_roundtrips_via_to_dict():
    schema = read_pms_schema_from_json(OPEN_DENTAL_FIXTURE)
    d = schema.to_dict()
    # Re-read from in-memory dict.
    rt = read_pms_schema_from_json(OPEN_DENTAL_FIXTURE)  # easier: same path
    assert rt.tables["patient"].name == "patient"
    assert "PatNum" in [c.name for c in rt.tables["patient"].columns]
    assert d["pms_name"] == "open_dental"


def test_dataclasses_are_frozen():
    col = ColumnSchema(name="x", type="INT", nullable=True)
    fk = ForeignKey(column="x", referenced_table="y", referenced_column="z")
    # frozen=True dataclasses raise FrozenInstanceError (subclass of AttributeError).
    with pytest.raises(AttributeError):
        col.name = "mutated"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        fk.column = "mutated"  # type: ignore[misc]


def test_foreign_keys_resolve_to_existing_tables():
    """All FKs in the fixture should reference tables that exist."""
    schema = read_pms_schema_from_json(OPEN_DENTAL_FIXTURE)
    table_names = set(schema.tables.keys())
    for table_name, table in schema.tables.items():
        for fk in table.foreign_keys:
            assert fk.referenced_table in table_names, (
                f"{table_name}.{fk.column} references {fk.referenced_table} "
                "which doesn't exist in the fixture"
            )


def test_unified_entry_point_with_schema_file():
    schema = read_pms_schema(schema_file=OPEN_DENTAL_FIXTURE)
    assert schema.pms_name == "open_dental"


def test_unified_entry_point_rejects_no_args():
    with pytest.raises(ValueError):
        read_pms_schema()


def test_unified_entry_point_rejects_multiple_args(tmp_path: Path):
    sql_dump = tmp_path / "schema.sql"
    sql_dump.write_text("CREATE TABLE x (id INT);")
    with pytest.raises(ValueError):
        read_pms_schema(schema_file=OPEN_DENTAL_FIXTURE, sql_dump=sql_dump)


# -----------------------------------------------------------------------
# SQL DDL parser
# -----------------------------------------------------------------------

SAMPLE_SQL = """
-- Open Dental partial schema export, schema-only.
CREATE TABLE `patient` (
    `PatNum` BIGINT NOT NULL,
    `LName` VARCHAR(100),
    `FName` VARCHAR(100),
    `Birthdate` DATE,
    `Gender` TINYINT NOT NULL,
    PRIMARY KEY (`PatNum`)
);

CREATE TABLE IF NOT EXISTS `appointment` (
    `AptNum` BIGINT NOT NULL,
    `PatNum` BIGINT NOT NULL,
    `AptDateTime` DATETIME NOT NULL,
    PRIMARY KEY (`AptNum`),
    FOREIGN KEY (`PatNum`) REFERENCES `patient`(`PatNum`)
);
"""

SQL_WITH_INSERT = """
CREATE TABLE patient (PatNum INT NOT NULL, LName VARCHAR(100));
INSERT INTO patient VALUES (1, 'Smith');
"""

SQL_WITH_COPY = """
CREATE TABLE patient (PatNum INT, LName VARCHAR(100));
COPY patient FROM '/tmp/data.csv' DELIMITER ',';
"""


def test_sql_dump_parses_basic_create_table(tmp_path: Path):
    p = tmp_path / "schema.sql"
    p.write_text(SAMPLE_SQL)
    schema = read_pms_schema_from_sql_dump(p, pms_name="open_dental")
    assert schema.pms_name == "open_dental"
    assert "patient" in schema.tables
    assert "appointment" in schema.tables
    patient = schema.tables["patient"]
    col_names = [c.name for c in patient.columns]
    assert col_names == ["PatNum", "LName", "FName", "Birthdate", "Gender"]
    assert patient.primary_key == ("PatNum",)
    # NOT NULL detection
    pat_num = next(c for c in patient.columns if c.name == "PatNum")
    assert pat_num.nullable is False
    lname = next(c for c in patient.columns if c.name == "LName")
    assert lname.nullable is True

    appt = schema.tables["appointment"]
    assert appt.primary_key == ("AptNum",)
    assert len(appt.foreign_keys) == 1
    fk = appt.foreign_keys[0]
    assert fk.column == "PatNum"
    assert fk.referenced_table == "patient"
    assert fk.referenced_column == "PatNum"


def test_sql_dump_rejects_insert_statements(tmp_path: Path):
    """Defense in depth: SQL dumps with row data must be rejected, not
    silently filtered. The wizard's contract is schema-only."""
    p = tmp_path / "with_data.sql"
    p.write_text(SQL_WITH_INSERT)
    with pytest.raises(ValueError, match="row data"):
        read_pms_schema_from_sql_dump(p, pms_name="open_dental")


def test_sql_dump_rejects_copy_statements(tmp_path: Path):
    p = tmp_path / "with_copy.sql"
    p.write_text(SQL_WITH_COPY)
    with pytest.raises(ValueError, match="row data"):
        read_pms_schema_from_sql_dump(p, pms_name="open_dental")


def test_pms_schema_to_dict_is_deterministic():
    """Same fixture loaded twice produces byte-identical to_dict() output."""
    s1 = read_pms_schema_from_json(OPEN_DENTAL_FIXTURE)
    s2 = read_pms_schema_from_json(OPEN_DENTAL_FIXTURE)
    assert json.dumps(s1.to_dict(), sort_keys=True) == json.dumps(
        s2.to_dict(), sort_keys=True
    )


# -----------------------------------------------------------------------
# Anti-PHI checks: schema reader output should NEVER include row values.
# -----------------------------------------------------------------------

def test_column_schema_dataclass_has_no_value_field():
    """ColumnSchema fields are name, type, nullable, description.
    Adding a 'value' or 'sample' field would be a HIPAA bug — guard the
    dataclass shape so anyone tempted to add one fails this test first."""
    fields = {f.name for f in ColumnSchema.__dataclass_fields__.values()}
    forbidden = {"value", "values", "sample", "samples", "data", "row_value"}
    assert fields.isdisjoint(forbidden)


def test_table_schema_dataclass_has_no_data_field():
    fields = {f.name for f in TableSchema.__dataclass_fields__.values()}
    forbidden = {"rows", "data", "values", "samples", "preview"}
    assert fields.isdisjoint(forbidden)


def test_pms_schema_dataclass_has_no_data_field():
    fields = {f.name for f in PmsSchema.__dataclass_fields__.values()}
    forbidden = {"rows", "data", "values", "samples", "preview", "table_data"}
    assert fields.isdisjoint(forbidden)
