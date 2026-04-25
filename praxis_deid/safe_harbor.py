"""HIPAA Safe Harbor primitives.

References:
  https://www.hhs.gov/hipaa/for-professionals/privacy/special-topics/de-identification/

The Safe Harbor method requires removing 18 specific identifiers AND that the
covered entity has no actual knowledge that the residual information could
identify an individual. This module implements the mechanical part — the
"actual knowledge" check is the practice's responsibility (Praxis cloud has
no source identifiers to combine, by design).
"""

from __future__ import annotations

# ZIP-3 prefixes representing populations < 20,000 per US Census.
# These must be suppressed (set to "000") under Safe Harbor §164.514(b)(2)(i)(B).
# Source: https://www.hhs.gov/hipaa/for-professionals/privacy/special-topics/de-identification/
# (HHS guidance, list as published; revised when Census updates).
RESTRICTED_ZIP3_PREFIXES: frozenset[str] = frozenset(
    {
        "036", "059", "063", "102", "203", "556", "692", "790",
        "821", "823", "830", "831", "878", "879", "884", "890", "893",
    }
)

# Age bands. Patients aged 90+ MUST be aggregated into a single "76+" or
# wider bucket per Safe Harbor §164.514(b)(2)(i)(C). We keep "76+" here.
# "unknown" is used for missing/future/unparseable DOBs — it preserves the
# row count for downstream aggregations without fabricating an age. Cloud
# canonical schema must accept "unknown" for round-trip ingestion.
AGE_BANDS: tuple[str, ...] = (
    "0-17", "18-30", "31-45", "46-60", "61-75", "76+", "unknown",
)


def age_to_band(age: int | None) -> str:
    """Map an exact age to its Safe Harbor band.

    Returns "unknown" for None or negative ages (e.g. future-dated DOB).
    This is intentional: silently clamping a -73 (data-quality bug) into
    "0-17" would fabricate a pediatric record. "unknown" preserves the
    row for aggregations but does not assert an age the source didn't
    provide. Impossibly old values (>=120) collapse to "76+" alongside
    the Safe Harbor 90+ requirement.
    """
    if age is None or age < 0:
        return "unknown"
    if age <= 17:
        return "0-17"
    if age <= 30:
        return "18-30"
    if age <= 45:
        return "31-45"
    if age <= 60:
        return "46-60"
    if age <= 75:
        return "61-75"
    return "76+"


def zip_to_prefix(zip_code: str | None) -> str:
    """Truncate to first 3 digits, suppressing restricted prefixes to '000'.

    Accepts ZIP+4 ('08201-1234'), 5-digit ZIPs, or partials. Non-numeric
    input or anything shorter than 3 digits is suppressed entirely.
    """
    if not zip_code:
        return "000"
    digits = "".join(ch for ch in zip_code if ch.isdigit())
    if len(digits) < 3:
        return "000"
    prefix = digits[:3]
    if prefix in RESTRICTED_ZIP3_PREFIXES:
        return "000"
    return prefix


def date_to_month(iso_date: str) -> str:
    """ISO date or datetime string -> 'YYYY-MM'.

    Day-level granularity is not allowed in canonical records. Time-of-day
    is also stripped. Raises ValueError on unparseable input.
    """
    if len(iso_date) < 7:
        raise ValueError(f"date too short: {iso_date!r}")
    year = iso_date[:4]
    sep = iso_date[4]
    month = iso_date[5:7]
    if sep != "-" or not year.isdigit() or not month.isdigit():
        raise ValueError(f"unparseable date: {iso_date!r}")
    if not (1 <= int(month) <= 12):
        raise ValueError(f"month out of range: {iso_date!r}")
    return f"{year}-{month}"


# Revenue bands: per-record amounts get bucketed; aggregate totals can be exact.
REVENUE_BANDS: tuple[str, ...] = (
    "$0-100",
    "$100-500",
    "$500-1000",
    "$1000-5000",
    "$5000-15000",
    "$15000-50000",
    "$50000+",
)


def amount_to_band(amount: float) -> str:
    """Bucket a per-record dollar amount. Treat negatives as 0."""
    a = max(0.0, float(amount))
    if a < 100:
        return "$0-100"
    if a < 500:
        return "$100-500"
    if a < 1000:
        return "$500-1000"
    if a < 5000:
        return "$1000-5000"
    if a < 15000:
        return "$5000-15000"
    if a < 50000:
        return "$15000-50000"
    return "$50000+"


# Duration bands for appointment lengths (in minutes).
DURATION_BANDS: tuple[str, ...] = ("0-15min", "15-30min", "30-60min", "60-120min", "120+min")


def duration_to_band(minutes: float) -> str:
    m = max(0.0, float(minutes))
    if m < 15:
        return "0-15min"
    if m < 30:
        return "15-30min"
    if m < 60:
        return "30-60min"
    if m < 120:
        return "60-120min"
    return "120+min"
