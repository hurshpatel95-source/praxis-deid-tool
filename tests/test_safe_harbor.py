"""Tests for safe_harbor primitives."""

import pytest

from praxis_deid.safe_harbor import (
    AGE_BANDS,
    DURATION_BANDS,
    REVENUE_BANDS,
    RESTRICTED_ZIP3_PREFIXES,
    age_to_band,
    amount_to_band,
    date_to_month,
    duration_to_band,
    zip_to_prefix,
)


class TestAgeToBand:
    def test_pediatric(self) -> None:
        assert age_to_band(0) == "0-17"
        assert age_to_band(17) == "0-17"

    def test_boundaries(self) -> None:
        assert age_to_band(18) == "18-30"
        assert age_to_band(30) == "18-30"
        assert age_to_band(31) == "31-45"
        assert age_to_band(45) == "31-45"
        assert age_to_band(46) == "46-60"
        assert age_to_band(60) == "46-60"
        assert age_to_band(61) == "61-75"
        assert age_to_band(75) == "61-75"

    def test_elderly_collapse(self) -> None:
        # Safe Harbor: 90+ collapses to a single bucket. We use 76+.
        assert age_to_band(76) == "76+"
        assert age_to_band(89) == "76+"
        assert age_to_band(90) == "76+"
        assert age_to_band(110) == "76+"

    def test_negative_treated_as_zero(self) -> None:
        assert age_to_band(-5) == "0-17"

    def test_every_band_reachable(self) -> None:
        # Sanity: every documented band is producible.
        produced = {age_to_band(a) for a in range(0, 100, 5)}
        for band in AGE_BANDS:
            assert band in produced, band


class TestZipToPrefix:
    def test_standard_zip(self) -> None:
        assert zip_to_prefix("08201") == "082"
        assert zip_to_prefix("19012") == "190"

    def test_zip_plus_4(self) -> None:
        assert zip_to_prefix("08201-1234") == "082"

    def test_restricted_prefix_suppressed(self) -> None:
        for restricted in RESTRICTED_ZIP3_PREFIXES:
            sample_zip = restricted + "00"
            assert zip_to_prefix(sample_zip) == "000", f"{restricted!r} not suppressed"

    def test_short_or_invalid_suppressed(self) -> None:
        assert zip_to_prefix("") == "000"
        assert zip_to_prefix(None) == "000"  # type: ignore[arg-type]
        assert zip_to_prefix("12") == "000"
        assert zip_to_prefix("ab") == "000"

    def test_strips_non_digits(self) -> None:
        assert zip_to_prefix("082-01") == "082"


class TestDateToMonth:
    def test_iso_date(self) -> None:
        assert date_to_month("2026-04-15") == "2026-04"

    def test_iso_datetime(self) -> None:
        assert date_to_month("2026-04-15T10:30:00") == "2026-04"

    def test_just_year_month(self) -> None:
        assert date_to_month("2026-04") == "2026-04"

    def test_unparseable(self) -> None:
        with pytest.raises(ValueError):
            date_to_month("nope")
        with pytest.raises(ValueError):
            date_to_month("2026/04/15")
        with pytest.raises(ValueError):
            date_to_month("2026-13-01")


class TestAmountToBand:
    def test_band_boundaries(self) -> None:
        assert amount_to_band(0) == "$0-100"
        assert amount_to_band(99.99) == "$0-100"
        assert amount_to_band(100) == "$100-500"
        assert amount_to_band(499) == "$100-500"
        assert amount_to_band(1000) == "$1000-5000"
        assert amount_to_band(50000) == "$50000+"
        assert amount_to_band(999999) == "$50000+"

    def test_negative_zero_floor(self) -> None:
        assert amount_to_band(-50) == "$0-100"

    def test_every_band_reachable(self) -> None:
        produced = {amount_to_band(a) for a in (10, 200, 700, 2000, 8000, 25000, 100000)}
        for b in REVENUE_BANDS:
            assert b in produced, b


class TestDurationToBand:
    def test_boundaries(self) -> None:
        assert duration_to_band(0) == "0-15min"
        assert duration_to_band(14) == "0-15min"
        assert duration_to_band(15) == "15-30min"
        assert duration_to_band(60) == "60-120min"
        assert duration_to_band(150) == "120+min"

    def test_every_band_reachable(self) -> None:
        produced = {duration_to_band(d) for d in (5, 20, 45, 90, 200)}
        for b in DURATION_BANDS:
            assert b in produced, b
