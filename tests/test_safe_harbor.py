"""Tests for safe_harbor primitives."""

import pytest

from praxis_deid.safe_harbor import (
    AGE_BANDS,
    DAYS_OF_WEEK,
    DURATION_BANDS,
    REVENUE_BANDS,
    RESTRICTED_ZIP3_PREFIXES,
    age_to_band,
    amount_to_band,
    date_to_day_of_week,
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

    def test_negative_treated_as_unknown(self) -> None:
        # Negative age (e.g. future-dated DOB clamped via today.year - dob.year)
        # MUST NOT silently bucket to "0-17" — that fabricates a pediatric
        # record from a data-quality bug. "unknown" preserves the row count
        # without asserting an age. See SECURITY_AUDIT.md finding #2.
        assert age_to_band(-5) == "unknown"
        assert age_to_band(-73) == "unknown"

    def test_none_returns_unknown(self) -> None:
        # NULL DOB at the call site passes None down -> "unknown" band.
        # See SECURITY_AUDIT.md finding #1.
        assert age_to_band(None) == "unknown"

    def test_every_band_reachable(self) -> None:
        # Sanity: every documented band is producible. "unknown" requires
        # None or negative input; the rest come from valid ages 0..100.
        produced = {age_to_band(a) for a in range(0, 100, 5)}
        produced.add(age_to_band(None))
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


class TestDateToDayOfWeek:
    def test_known_dates(self) -> None:
        # 2025-01-13 was a Monday; 2025-01-19 a Sunday. Sanity-check both ends.
        assert date_to_day_of_week("2025-01-13") == "mon"
        assert date_to_day_of_week("2025-01-14") == "tue"
        assert date_to_day_of_week("2025-01-15") == "wed"
        assert date_to_day_of_week("2025-01-16") == "thu"
        assert date_to_day_of_week("2025-01-17") == "fri"
        assert date_to_day_of_week("2025-01-18") == "sat"
        assert date_to_day_of_week("2025-01-19") == "sun"

    def test_iso_datetime_accepted(self) -> None:
        # YYYY-MM-DDThh:mm:ss is acceptable — day comes from the date part.
        assert date_to_day_of_week("2025-01-13T10:30:00") == "mon"

    def test_yyyy_mm_only_rejected(self) -> None:
        # Day-of-week cannot be derived from year+month alone — the de-id tool
        # must call this BEFORE date_to_month strips the day.
        with pytest.raises(ValueError):
            date_to_day_of_week("2025-01")

    def test_unparseable_rejected(self) -> None:
        with pytest.raises(ValueError):
            date_to_day_of_week("nope")
        with pytest.raises(ValueError):
            date_to_day_of_week("2025/01/13")  # wrong separators

    def test_every_day_reachable(self) -> None:
        # Walk a full week starting Monday 2025-01-13; every label is produced.
        produced = {date_to_day_of_week(f"2025-01-{13 + i:02d}") for i in range(7)}
        assert produced == set(DAYS_OF_WEEK)
