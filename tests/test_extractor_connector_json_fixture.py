"""Tests for :class:`JsonFixtureConnector`.

This is the refactor-of-existing-code connector — it wraps the same
fixture-JSON shape Phase-C ships, behind the uniform DBConnector
interface. The Phase-C e2e tests (``test_extractor_cli_e2e.py``)
already validate the through-the-CLI behavior; these tests pin the
connector's own contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from praxis_deid.extractors.connectors import (
    ConnectionError,
    JsonFixtureConnector,
    connector_for_url,
    fixture_json_url,
)

FIXTURE = {
    "treatment_plans_raw": [
        {"treatplan.TreatPlanNum": "TP-1", "treatplan.PatNum": "PT-1"},
        {"treatplan.TreatPlanNum": "TP-2", "treatplan.PatNum": "PT-2"},
    ],
    "claims_raw": [
        {"claim.ClaimNum": "CLM-1", "claim.PatNum": "PT-1"},
    ],
}


@pytest.fixture
def fixture_file(tmp_path: Path) -> Path:
    p = tmp_path / "fx.json"
    p.write_text(json.dumps(FIXTURE), encoding="utf-8")
    return p


# -------------------------------------------------------------------------
# URL parsing + lifecycle
# -------------------------------------------------------------------------


def test_fixture_json_url_builds_absolute_url(tmp_path: Path) -> None:
    url = fixture_json_url(tmp_path / "fx.json")
    assert url.startswith("fixture-json://")
    assert str(tmp_path / "fx.json") in url


def test_fixture_json_url_builds_relative_url() -> None:
    url = fixture_json_url(Path("rel/fx.json"))
    assert url.startswith("fixture-json://./")


def test_connect_loads_fixture_from_file(fixture_file: Path) -> None:
    url = fixture_json_url(fixture_file)
    conn = JsonFixtureConnector(url)
    conn.connect()
    try:
        tables = conn.list_tables()
    finally:
        conn.close()
    assert "treatment_plans_raw" in tables
    assert "claims_raw" in tables


def test_connect_idempotent(fixture_file: Path) -> None:
    url = fixture_json_url(fixture_file)
    conn = JsonFixtureConnector(url)
    conn.connect()
    conn.connect()  # second call: no-op, no error
    conn.close()


def test_close_drops_fixture_reference(fixture_file: Path) -> None:
    url = fixture_json_url(fixture_file)
    conn = JsonFixtureConnector(url)
    conn.connect()
    assert conn.list_tables()
    conn.close()
    with pytest.raises(ConnectionError, match="not connected"):
        conn.list_tables()


def test_context_manager_lifecycle(fixture_file: Path) -> None:
    url = fixture_json_url(fixture_file)
    with JsonFixtureConnector(url) as conn:
        assert conn.list_tables()


# -------------------------------------------------------------------------
# Inline-fixture constructor (used by tests that don't want to write a file)
# -------------------------------------------------------------------------


def test_inline_fixture_short_circuits_path_parsing() -> None:
    conn = JsonFixtureConnector("fixture-json://./ignored.json", inline_fixture=FIXTURE)
    conn.connect()
    assert "treatment_plans_raw" in conn.list_tables()
    conn.close()


# -------------------------------------------------------------------------
# Error paths
# -------------------------------------------------------------------------


def test_connect_raises_on_missing_file(tmp_path: Path) -> None:
    url = fixture_json_url(tmp_path / "nope.json")
    conn = JsonFixtureConnector(url)
    with pytest.raises(ConnectionError, match="not found"):
        conn.connect()


def test_connect_raises_on_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    url = fixture_json_url(p)
    conn = JsonFixtureConnector(url)
    with pytest.raises(ConnectionError, match="valid JSON"):
        conn.connect()


def test_connect_raises_on_non_object_root(tmp_path: Path) -> None:
    p = tmp_path / "arr.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    url = fixture_json_url(p)
    conn = JsonFixtureConnector(url)
    with pytest.raises(ConnectionError, match="object keyed by schema"):
        conn.connect()


def test_connect_raises_on_non_list_value(tmp_path: Path) -> None:
    p = tmp_path / "bad_shape.json"
    p.write_text(json.dumps({"treatment_plans_raw": {"not": "a list"}}), encoding="utf-8")
    url = fixture_json_url(p)
    conn = JsonFixtureConnector(url)
    with pytest.raises(ConnectionError, match="must map to a list"):
        conn.connect()


def test_url_with_wrong_scheme_raises() -> None:
    conn = JsonFixtureConnector("notfixture://nope")
    with pytest.raises(ConnectionError, match="fixture-json://"):
        conn.connect()


def test_dispatch_returns_json_fixture_connector(fixture_file: Path) -> None:
    url = fixture_json_url(fixture_file)
    c = connector_for_url(url)
    assert isinstance(c, JsonFixtureConnector)
    assert c.pms_dialect == "fixture"


# -------------------------------------------------------------------------
# Schema introspection + fetch_rows
# -------------------------------------------------------------------------


def test_list_columns_unions_keys_across_rows() -> None:
    fx = {
        "tbl": [
            {"a": 1, "b": 2},
            {"a": 1, "c": 3},
        ]
    }
    conn = JsonFixtureConnector("fixture-json://./x", inline_fixture=fx)
    conn.connect()
    try:
        cols = conn.list_columns("tbl")
    finally:
        conn.close()
    names = {c for c, _t in cols}
    assert names == {"a", "b", "c"}


def test_list_columns_raises_on_unknown_table() -> None:
    conn = JsonFixtureConnector(
        "fixture-json://./x", inline_fixture={"t": []}
    )
    conn.connect()
    with pytest.raises(ConnectionError, match="not in fixture"):
        conn.list_columns("nope")
    conn.close()


def test_fetch_rows_yields_dicts() -> None:
    conn = JsonFixtureConnector("fixture-json://./x", inline_fixture=FIXTURE)
    conn.connect()
    try:
        rows = list(
            conn.fetch_rows(
                table_name="treatment_plans_raw",
                columns=["treatplan.TreatPlanNum"],
            )
        )
    finally:
        conn.close()
    assert len(rows) == 2
    assert rows[0]["treatplan.TreatPlanNum"] == "TP-1"


def test_fetch_rows_respects_limit() -> None:
    fx = {"t": [{"x": i} for i in range(10)]}
    conn = JsonFixtureConnector("fixture-json://./x", inline_fixture=fx)
    conn.connect()
    try:
        rows = list(conn.fetch_rows(table_name="t", columns=["x"], limit=3))
    finally:
        conn.close()
    assert len(rows) == 3


def test_fetch_rows_rejects_unknown_table() -> None:
    conn = JsonFixtureConnector(
        "fixture-json://./x", inline_fixture={"t": []}
    )
    conn.connect()
    with pytest.raises(ConnectionError, match="not in fixture"):
        list(conn.fetch_rows(table_name="nope", columns=[]))
    conn.close()


def test_pms_dialect_is_fixture() -> None:
    assert JsonFixtureConnector("fixture-json://./x").pms_dialect == "fixture"
