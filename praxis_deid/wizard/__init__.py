"""Wizard-1: Claude-API-assisted PMS schema mapping.

The wizard helps onboard a new practice management system (PMS) connection
to the de-identification tool. Given a PMS database connection or a schema
dump, it produces a `mapping.json` config describing how to extract each
of the 6 canonical CSVs from the PMS's tables.

Architectural invariant — the bright HIPAA line:
    The wizard reads SCHEMA METADATA ONLY (table names, column names,
    types, foreign keys). It NEVER reads or transmits row data. Two walls
    enforce this: (1) the schema reader is implemented to make it
    structurally impossible to fetch rows; (2) `PhiGuard` inspects every
    payload before it leaves for the Claude API and refuses anything
    that looks like PHI. Defense in depth.

Public surface:
    - load_canonical_schemas() - the 6 target schemas
    - read_pms_schema(...) - read PMS metadata from a JSON dump or live DB
    - ClaudeMapper - send schema-only payload to Claude, get a MappingConfig
    - validate_mapping() - structural checks on a MappingConfig
    - run_human_approval() - CLI prompt to review + approve mappings

The output `mapping.json` is consumed by the existing extraction pipeline
(out of scope for Wizard-1).
"""

from __future__ import annotations

from .canonical_schemas import (
    CANONICAL_SCHEMAS,
    CanonicalColumn,
    CanonicalSchema,
    load_canonical_schemas,
)
from .claude_mapper import (
    ClaudeMapper,
    ColumnMapping,
    Join,
    MappingConfig,
    PhiDetectedError,
    PhiGuard,
)
from .mapping_validator import (
    ValidationIssue,
    ValidationSeverity,
    validate_mapping,
)
from .schema_reader import (
    ColumnSchema,
    ForeignKey,
    PmsSchema,
    TableSchema,
    read_pms_schema,
)

__all__ = [
    "CANONICAL_SCHEMAS",
    "CanonicalColumn",
    "CanonicalSchema",
    "ClaudeMapper",
    "ColumnMapping",
    "ColumnSchema",
    "ForeignKey",
    "Join",
    "MappingConfig",
    "PhiDetectedError",
    "PhiGuard",
    "PmsSchema",
    "TableSchema",
    "ValidationIssue",
    "ValidationSeverity",
    "load_canonical_schemas",
    "read_pms_schema",
    "validate_mapping",
]
