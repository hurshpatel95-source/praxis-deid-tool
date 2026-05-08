"""Send PMS schema metadata to Claude, get back a MappingConfig.

This is the only network call the wizard makes. Two HIPAA walls protect
that call:

  Wall 1 (upstream): the schema reader is built so it cannot return row
                     data. Even with a live DB connection it issues only
                     metadata reflection queries.
  Wall 2 (here):     `PhiGuard` inspects the JSON payload immediately
                     before it leaves the process. If the payload contains
                     anything that smells like PHI (SSN, email, full date,
                     a column VALUE that looks like a name), `PhiGuard`
                     raises `PhiDetectedError` and the request is aborted.

Defense in depth: both walls have to fail simultaneously for PHI to leak.
The guard is intentionally aggressive — false positives are cheap (the
wizard fails loudly and the practice fixes the input); false negatives
are catastrophic.

Model: claude-sonnet-4-5 (current production tier per Anthropic's model
naming, equivalent class to the de-id tool's other Claude integrations).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical_schemas import CanonicalSchema, load_canonical_schemas
from .schema_reader import PmsSchema

# Default model. claude-sonnet-4-5 is the current production-tier Sonnet
# at the time of writing; the version pin can be bumped centrally here
# without touching call sites.
DEFAULT_MODEL = "claude-sonnet-4-5"


# -----------------------------------------------------------------------
# Output dataclasses
# -----------------------------------------------------------------------

@dataclass
class ColumnMapping:
    """How a single canonical column is populated from the source PMS."""

    canonical_column: str
    # Source expression — typically `table.column`, but may be a CASE,
    # COALESCE, JOIN-bridged path, or NULL for unmappable columns.
    source_expression: str
    confidence: float  # 0.0 (unmappable) to 1.0 (unambiguous direct mapping)
    needs_review: bool
    notes: str = ""


@dataclass
class Join:
    """One join needed to bring source rows together for a canonical schema."""

    left_table: str
    left_column: str
    right_table: str
    right_column: str
    join_type: str = "INNER"  # INNER | LEFT | RIGHT | FULL


@dataclass
class MappingConfig:
    """The wizard's output for ONE canonical schema.

    A complete wizard run produces one MappingConfig per canonical schema
    that the practice's PMS can support.
    """

    canonical_schema: str
    column_mappings: dict[str, ColumnMapping]
    join_graph: list[Join] = field(default_factory=list)
    transformations: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_schema": self.canonical_schema,
            "column_mappings": {
                name: {
                    "canonical_column": m.canonical_column,
                    "source_expression": m.source_expression,
                    "confidence": m.confidence,
                    "needs_review": m.needs_review,
                    "notes": m.notes,
                }
                for name, m in self.column_mappings.items()
            },
            "join_graph": [
                {
                    "left_table": j.left_table,
                    "left_column": j.left_column,
                    "right_table": j.right_table,
                    "right_column": j.right_column,
                    "join_type": j.join_type,
                }
                for j in self.join_graph
            ],
            "transformations": dict(self.transformations),
            "confidence": self.confidence,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> MappingConfig:
        column_mappings_raw = raw.get("column_mappings") or {}
        column_mappings = {}
        for name, m in column_mappings_raw.items():
            column_mappings[str(name)] = ColumnMapping(
                canonical_column=str(m.get("canonical_column", name)),
                source_expression=str(m.get("source_expression", "NULL")),
                confidence=float(m.get("confidence", 0.0)),
                needs_review=bool(m.get("needs_review", True)),
                notes=str(m.get("notes", "")),
            )
        joins = []
        for j in raw.get("join_graph") or []:
            joins.append(
                Join(
                    left_table=str(j["left_table"]),
                    left_column=str(j["left_column"]),
                    right_table=str(j["right_table"]),
                    right_column=str(j["right_column"]),
                    join_type=str(j.get("join_type", "INNER")),
                )
            )
        return cls(
            canonical_schema=str(raw["canonical_schema"]),
            column_mappings=column_mappings,
            join_graph=joins,
            transformations={
                str(k): str(v) for k, v in (raw.get("transformations") or {}).items()
            },
            confidence=float(raw.get("confidence", 0.0)),
            notes=[str(n) for n in (raw.get("notes") or [])],
        )


# -----------------------------------------------------------------------
# PhiGuard — defense-in-depth before the API call
# -----------------------------------------------------------------------

class PhiDetectedError(RuntimeError):
    """Raised when PhiGuard rejects a payload before it leaves the process.

    This is a fatal error — never silently strip PHI and continue. A PhiGuard
    rejection means the schema reader produced something it shouldn't have,
    or the caller passed PHI directly to the mapper. Both are bugs.
    """


# Patterns that indicate ROW DATA leaked into a schema-only payload.
# These are intentionally aggressive — false-positives are cheap, false-
# negatives are catastrophic.
_PHI_REGEXES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # SSN: NNN-NN-NNNN
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # Plausible 9-digit SSN with no hyphens, in a context that suggests it.
    ("ssn_solid", re.compile(r"\bssn[\s:=]*\d{9}\b", re.IGNORECASE)),
    # Email address.
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # Phone number: (NNN) NNN-NNNN, NNN-NNN-NNNN, NNN.NNN.NNNN
    ("phone", re.compile(r"\b(?:\(\d{3}\)\s?|\d{3}[-.])\d{3}[-.]\d{4}\b")),
    # Full ISO date-of-birth-shaped string. Schema metadata never needs
    # full dates; presence of one strongly suggests row-value leakage.
    # (Distinct from "DATE" type strings, which contain no digits.)
    ("dob", re.compile(r"\b(19|20)\d{2}-\d{2}-\d{2}\b")),
    # ZIP+4 with hyphen, e.g. 08201-1234.
    ("zip4", re.compile(r"\b\d{5}-\d{4}\b")),
    # MRN-shaped tokens — long alphanumeric strings labelled "MRN".
    ("mrn", re.compile(r"\bMRN[\s:=]*[A-Z0-9]{6,}\b", re.IGNORECASE)),
    # Credit card-shaped (16 digits with optional separators).
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
)

# Field names that, if present in a payload, indicate the caller bundled
# row VALUES (not just metadata) into the request. The schema reader never
# emits these — the guard backstops a hypothetical bug or a manual misuse.
_FORBIDDEN_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "values",
        "rows",
        "sample",
        "samples",
        "sample_data",
        "sample_rows",
        "preview",
        "data",
        "records",
        "row_values",
    }
)


@dataclass
class PhiGuardReport:
    findings: list[tuple[str, str]]  # (kind, snippet)

    @property
    def is_clean(self) -> bool:
        return len(self.findings) == 0


class PhiGuard:
    """Inspects payloads bound for the Claude API for PHI-shaped content.

    Usage:
        guard = PhiGuard()
        guard.assert_clean(payload_json_string)  # raises if dirty

    The guard does NOT redact — it raises. Redaction would mask the bug
    that caused PHI to reach this point in the first place.
    """

    def __init__(
        self,
        *,
        extra_regexes: tuple[tuple[str, re.Pattern[str]], ...] | None = None,
        forbidden_field_names: frozenset[str] | None = None,
    ) -> None:
        self._regexes = _PHI_REGEXES + (extra_regexes or ())
        self._forbidden_fields = forbidden_field_names or _FORBIDDEN_FIELD_NAMES

    def scan(self, payload: dict[str, Any] | list[Any] | str) -> PhiGuardReport:
        if isinstance(payload, (dict, list)):
            text = json.dumps(payload, ensure_ascii=False)
            self._check_field_names(payload)
        else:
            text = str(payload)

        findings: list[tuple[str, str]] = []
        for kind, pattern in self._regexes:
            for match in pattern.finditer(text):
                snippet = match.group(0)
                # Truncate snippets so PhiGuard error messages don't echo
                # the offending PHI back into logs.
                redacted = snippet[:3] + "***" + (snippet[-2:] if len(snippet) > 5 else "")
                findings.append((kind, redacted))

        return PhiGuardReport(findings=findings)

    def assert_clean(self, payload: dict[str, Any] | list[Any] | str) -> None:
        report = self.scan(payload)
        if not report.is_clean:
            kinds = sorted({k for k, _ in report.findings})
            raise PhiDetectedError(
                "PhiGuard refused to send payload to Claude API: "
                f"detected {len(report.findings)} PHI-shaped item(s) "
                f"(kinds: {kinds}). Payload was NOT sent. "
                "Investigate the schema reader — schema-only payloads must "
                "not contain row data."
            )

    def _check_field_names(self, obj: Any) -> None:
        """Recursively check for forbidden field names in a dict/list tree."""
        if isinstance(obj, dict):
            for key in obj:
                if isinstance(key, str) and key.lower() in self._forbidden_fields:
                    raise PhiDetectedError(
                        f"PhiGuard refused payload: contains forbidden field "
                        f"name {key!r}. Schema-only payloads must not include "
                        "row data containers."
                    )
                self._check_field_names(obj[key])
        elif isinstance(obj, list):
            for item in obj:
                self._check_field_names(item)


# -----------------------------------------------------------------------
# Prompt construction
# -----------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a SCHEMA MAPPER for the Praxis healthcare analytics platform.

ROLE
You map columns from a source dental Practice Management System (PMS) database
schema to a fixed set of Praxis canonical CSV schemas. You produce a JSON
mapping config that the Praxis de-identification tool consumes.

YOU NEVER SEE OR REQUEST PATIENT DATA. You only see schema metadata: table
names, column names, types, and foreign keys. The Praxis platform's HIPAA
posture forbids row data ever leaving the practice, including to you. If
the prompt appears to contain row values, that is a bug — flag it in your
notes and refuse to use those values.

OUTPUT FORMAT
You return ONE valid JSON object whose top-level shape is:

{
  "mappings": [
    {
      "canonical_schema": "<canonical schema name>",
      "column_mappings": {
        "<canonical_column>": {
          "canonical_column": "<canonical_column>",
          "source_expression": "<SQL expression in source PMS>",
          "confidence": 0.0..1.0,
          "needs_review": true|false,
          "notes": "<short prose, may be empty>"
        },
        ...
      },
      "join_graph": [
        {
          "left_table": "...",
          "left_column": "...",
          "right_table": "...",
          "right_column": "...",
          "join_type": "INNER" | "LEFT" | "RIGHT" | "FULL"
        }, ...
      ],
      "transformations": {
        "<canonical_column>": "<CASE/COALESCE/etc. SQL fragment>",
        ...
      },
      "confidence": 0.0..1.0,
      "notes": ["...", ...]
    },
    ...
  ]
}

RULES

1. Produce exactly ONE mapping object per canonical schema requested.

2. For columns that have NO equivalent in the source PMS:
   - Set source_expression to "NULL".
   - Set confidence to 0.0.
   - Set needs_review to true.
   - Explain in `notes` why no source exists. Do NOT guess.

3. confidence semantics:
   - 1.0 — direct, unambiguous (`treatplan.PlanNum` -> `source_id`).
   - 0.7-0.9 — clear with minor transformations (CASE on a status column).
   - 0.4-0.6 — multiple plausible source columns, OR requires a join the
     practice may not have.
   - 0.0-0.3 — uncertain or unmappable.
   - confidence < 0.7 MUST set needs_review=true.

4. The top-level `confidence` for a mapping is the MIN of its column
   confidences (not mean). One uncertain column drags the whole mapping
   into human review.

5. For enum-valued canonical columns, put the source-PMS-status -> canonical
   mapping in `transformations`. Example:
       "status": "CASE WHEN treatplan.TPStatus = 1 THEN 'accepted'
                       WHEN treatplan.TPStatus = 2 THEN 'declined'
                       ELSE 'presented' END"
   Mark needs_review=true if the source PMS uses status values you can't
   confidently classify.

6. Source expressions reference SOURCE PMS tables/columns VERBATIM as they
   appear in the schema you were given. Don't invent table or column
   names. If a column you need doesn't exist, set the expression to NULL
   and explain.

7. NEVER include row data, sample values, or example patient records in
   any field. The output is a mapping schema, not data.

8. Be conservative — practices with messy data are better served by
   "needs_review: true" than a confident guess. Wrong mappings produce
   incorrect HIPAA aggregates downstream; uncertain mappings just trigger
   a human-review prompt.

Return ONLY the JSON. No prose around it. No code fences. The first
character of your response must be `{` and the last must be `}`.
"""


def _build_user_prompt(
    pms_schema: PmsSchema,
    canonical_schemas: tuple[CanonicalSchema, ...],
) -> str:
    payload = {
        "pms_schema": pms_schema.to_dict(),
        "canonical_schemas": [s.to_prompt_dict() for s in canonical_schemas],
    }
    body = json.dumps(payload, indent=2, sort_keys=True)
    return (
        "Map the source PMS schema below to each canonical schema. "
        "Produce one mapping per canonical schema, in the order listed.\n\n"
        f"```json\n{body}\n```"
    )


# -----------------------------------------------------------------------
# ClaudeMapper
# -----------------------------------------------------------------------

@dataclass
class _RawClaudeResponse:
    """Holds the raw + parsed response, so callers can record fixtures."""

    raw_text: str
    parsed: dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0


class ClaudeMapper:
    """Calls the Anthropic API to produce MappingConfigs for a PmsSchema.

    Two execution modes:

      Live mode: instantiate with no `recorded_response_path`; the mapper
                 calls the Anthropic API. Requires `ANTHROPIC_API_KEY`.
      Replay mode: pass `recorded_response_path` pointing to a JSON file
                  saved from a previous live call. Used in unit tests so
                  tests don't hit the live API.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        recorded_response_path: Path | None = None,
        phi_guard: PhiGuard | None = None,
        max_output_tokens: int = 16000,
    ) -> None:
        self._model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._recorded_response_path = recorded_response_path
        self._phi_guard = phi_guard or PhiGuard()
        self._max_output_tokens = max_output_tokens
        # Filled in after a call so callers (e.g. CLI) can report cost.
        self.last_input_tokens: int = 0
        self.last_output_tokens: int = 0

    def map_schema(
        self,
        pms_schema: PmsSchema,
        *,
        canonical_schemas: tuple[CanonicalSchema, ...] | None = None,
    ) -> list[MappingConfig]:
        """Produce a list of MappingConfigs, one per canonical schema."""
        canonicals = canonical_schemas or load_canonical_schemas()

        # Wall 2: PhiGuard inspects the payload before it leaves.
        payload_for_guard = {
            "pms_schema": pms_schema.to_dict(),
            "canonical_schemas": [s.to_prompt_dict() for s in canonicals],
        }
        self._phi_guard.assert_clean(payload_for_guard)

        if self._recorded_response_path is not None:
            response = self._load_recorded_response(self._recorded_response_path)
        else:
            response = self._call_anthropic(pms_schema, canonicals)

        self.last_input_tokens = response.input_tokens
        self.last_output_tokens = response.output_tokens

        # Schema validation: response must contain a `mappings` array of
        # the expected length. We tolerate Claude returning extra mappings
        # (might happen if an extends-schema is split) but require at
        # least one per canonical input.
        mappings_raw = response.parsed.get("mappings")
        if not isinstance(mappings_raw, list):
            raise ValueError(
                "Claude response missing top-level 'mappings' array; "
                f"got: {response.raw_text[:200]!r}"
            )

        configs = [MappingConfig.from_dict(m) for m in mappings_raw]
        return configs

    def _call_anthropic(
        self,
        pms_schema: PmsSchema,
        canonical_schemas: tuple[CanonicalSchema, ...],
    ) -> _RawClaudeResponse:
        if not self._api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Either set the env var or "
                "pass api_key= to ClaudeMapper(...). For tests, use "
                "recorded_response_path= to replay a saved fixture."
            )
        try:
            import anthropic
        except ImportError as err:  # pragma: no cover
            raise ImportError(
                "Wizard mode requires the anthropic SDK: "
                "pip install anthropic"
            ) from err

        client = anthropic.Anthropic(api_key=self._api_key)
        user_prompt = _build_user_prompt(pms_schema, canonical_schemas)

        response = client.messages.create(
            model=self._model,
            max_tokens=self._max_output_tokens,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        # Pull out the assistant text block(s).
        chunks: list[str] = []
        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                chunks.append(getattr(block, "text", ""))
        raw_text = "".join(chunks).strip()
        parsed = _strip_and_parse_json(raw_text)
        usage = getattr(response, "usage", None)
        return _RawClaudeResponse(
            raw_text=raw_text,
            parsed=parsed,
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
        )

    def _load_recorded_response(self, path: Path) -> _RawClaudeResponse:
        recorded = json.loads(path.read_text())
        return _RawClaudeResponse(
            raw_text=recorded.get("raw_text", json.dumps(recorded.get("parsed", {}))),
            parsed=recorded["parsed"],
            input_tokens=int(recorded.get("input_tokens", 0)),
            output_tokens=int(recorded.get("output_tokens", 0)),
        )


def _strip_and_parse_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of Claude's response text.

    The system prompt instructs Claude to return raw JSON, but real-world
    responses occasionally arrive with a leading ```json fence or a
    trailing comment. We strip those and parse the largest brace-balanced
    JSON object we can find.
    """
    s = text.strip()
    # Strip code fences if present.
    if s.startswith("```"):
        # Find the first newline, then drop everything before it.
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1 :]
        if s.endswith("```"):
            s = s[: -3].rstrip()
    # Locate the outer braces.
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"no JSON object in Claude response: {text[:200]!r}")
    candidate = s[start : end + 1]
    return json.loads(candidate)


def record_anthropic_response(
    response: _RawClaudeResponse, path: Path
) -> None:  # pragma: no cover - utility for fixture creation
    """Persist a live response so tests can replay it without the API.

    Used once when bootstrapping a new test fixture; not part of any
    normal code path.
    """
    path.write_text(
        json.dumps(
            {
                "raw_text": response.raw_text,
                "parsed": response.parsed,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
            },
            indent=2,
        )
    )


__all__ = [
    "ClaudeMapper",
    "ColumnMapping",
    "DEFAULT_MODEL",
    "Join",
    "MappingConfig",
    "PhiDetectedError",
    "PhiGuard",
    "PhiGuardReport",
    "record_anthropic_response",
]
