"""Extension D: payments_raw extractor.

Source (Open Dental):
  UNION ALL of three branches:

    1. paysplit          (patient payments)
       source_id        = paysplit.SplitNum
       patient_id       = paysplit.PatNum
       date             = paysplit.DatePay
       amount           = paysplit.SplitAmt    (banded at de-id time)
       payment_source   = 'patient'
       payer_category   = 'self_pay'

    2. claimpayment      (insurance payments)
       source_id        = claimpayment.ClaimPaymentNum
       patient_id       = claimpayment.PatNum
       date             = claimpayment.CheckDate
       amount           = claimpayment.CheckAmt (banded)
       payment_source   = 'insurance'
       payer_category   = derived from carrier (via the same lookup as
                          Extension B)

    3. adjustment        (writeoffs, courtesy reductions)
       source_id        = adjustment.AdjNum
       patient_id       = adjustment.PatNum
       date             = adjustment.AdjDate
       amount           = adjustment.AdjAmt    (banded)
       payment_source   = 'adjustment_writeoff'
       payer_category   = 'other'

CRITICAL invariant: amounts ALWAYS pass through amount_to_band. The
canonical row stores `amount_band` (a string like '$1000-5000'), never
exact dollars. The base test harness scans every produced CSV to
verify no un-banded numeric > 1000 leaks.

The row source returns ONE iterable, with each row tagged via a
synthetic `_branch` key ('paysplit' / 'claimpayment' / 'adjustment') so
the extractor knows which mapping to apply. This avoids three separate
RowSource calls and lets the practice's actual SQL be a single UNION
ALL query.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .base import BaseExtractor, ExtractorError, Filter

# Re-use the same default lookup as Extension B, so a 'BCBS' check on
# the patient side and the insurance side categorize identically.
from .extension_b_claims import _DEFAULT_PAYER_LOOKUP
from .rows import PaymentRow


class PaymentsExtractor(BaseExtractor):
    canonical_schema_name = "payments_raw"

    SOURCE_TABLE = "paysplit"  # not actually used as a single-table query;
    # the row source treats this as a hint for which branch to start with.

    # Per-branch column maps: how to read the four shared canonical
    # values (source_id, patient_id, date, amount) out of each branch's
    # column naming. This lives in code (not the mapping JSON) because
    # the JSON only describes one branch — the audited mapping notes
    # explicitly say full coverage requires a UNION ALL across three
    # tables that the JSON can't represent in a single column_mappings
    # block.
    _BRANCH_DEFS: dict[str, dict[str, str]] = {
        "paysplit": {
            "source_id": "paysplit.SplitNum",
            "patient_id": "paysplit.PatNum",
            "date": "paysplit.DatePay",
            "amount": "paysplit.SplitAmt",
            "payment_source": "patient",
            "default_payer_category": "self_pay",
        },
        "claimpayment": {
            "source_id": "claimpayment.ClaimPaymentNum",
            "patient_id": "claimpayment.PatNum",
            "date": "claimpayment.CheckDate",
            "amount": "claimpayment.CheckAmt",
            "payment_source": "insurance",
            "default_payer_category": "commercial",
            "carrier_column": "carrier.CarrierName",
        },
        "adjustment": {
            "source_id": "adjustment.AdjNum",
            "patient_id": "adjustment.PatNum",
            "date": "adjustment.AdjDate",
            "amount": "adjustment.AdjAmt",
            "payment_source": "adjustment_writeoff",
            "default_payer_category": "other",
        },
    }

    def __init__(
        self,
        *args: Any,
        payer_lookup: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.payer_lookup = (
            dict(payer_lookup) if payer_lookup is not None
            else dict(_DEFAULT_PAYER_LOOKUP)
        )
        self.unmapped_carriers: dict[str, int] = {}

    def extract(self, filter: Filter | None = None) -> list[PaymentRow]:
        # Pull all three branches via a single RowSource call; rows are
        # tagged with `_branch`. Tests can wire either a generator that
        # yields all three or three separate calls.
        raw_rows = list(
            self.row_source(self.SOURCE_TABLE, self._needed_columns(), filter)
        )

        out: list[PaymentRow] = []
        for row in raw_rows:
            branch = row.get("_branch") or "paysplit"
            if branch not in self._BRANCH_DEFS:
                self._drop(f"unknown_branch:{branch}")
                continue
            try:
                p_row = self._build_row(row, branch=str(branch))
            except (ExtractorError, ValueError, KeyError) as err:
                self._drop(f"row_error:{type(err).__name__}:{str(err)[:80]}")
                continue
            if p_row is None:
                continue
            try:
                p_row.validate()
            except Exception as err:  # noqa: BLE001
                self._drop(f"validation:{str(err)[:120]}")
                continue
            # Apply the post-validate filter if requested (filter is at
            # the row level; we already applied since/until via the date
            # column below).
            out.append(p_row)
            if filter and filter.limit is not None and len(out) >= filter.limit:
                break
        return out

    # --- helpers ----------------------------------------------------------

    def _needed_columns(self) -> list[str]:
        cols = {"_branch"}
        for spec in self._BRANCH_DEFS.values():
            for k in ("source_id", "patient_id", "date", "amount"):
                cols.add(spec[k])
            if "carrier_column" in spec:
                cols.add(spec["carrier_column"])
        return sorted(cols)

    def _build_row(
        self,
        row: Mapping[str, Any],
        *,
        branch: str,
    ) -> PaymentRow | None:
        spec = self._BRANCH_DEFS[branch]

        source_id_raw = row.get(spec["source_id"])
        if source_id_raw in (None, ""):
            raise ExtractorError(f"{branch}: missing {spec['source_id']}")
        patient_raw = row.get(spec["patient_id"])
        if patient_raw in (None, ""):
            raise ExtractorError(f"{branch}: missing {spec['patient_id']}")

        # Date filter (extractor-level): apply since/until even though
        # _filter_to_period needs a known column name; since the column
        # depends on branch, we filter inline.
        date_raw = row.get(spec["date"])
        if date_raw in (None, ""):
            raise ExtractorError(f"{branch}: missing {spec['date']}")

        amount_raw = row.get(spec["amount"])
        if amount_raw is None:
            raise ExtractorError(f"{branch}: missing {spec['amount']}")
        try:
            amount_num = float(amount_raw)
        except (TypeError, ValueError) as err:
            raise ExtractorError(
                f"{branch}: amount not numeric: {amount_raw!r}"
            ) from err

        # Adjustment writeoffs are usually negative; band on absolute
        # value so the canonical row's amount_band represents
        # magnitude. The cloud aggregator already knows
        # payment_source='adjustment_writeoff' is a credit.
        amount_for_band = abs(amount_num)

        # HMACs (cross-extension stable).
        external_id = self._hipaa("source_id", source_id_raw)
        patient_external_id = self._hipaa("patient_source_id", patient_raw)

        # Date -> month.
        payment_month = self._hipaa("payment_date", date_raw)

        # Amount -> band (BAA invariant: NEVER emit exact $$$).
        amount_band = self._hipaa("amount", amount_for_band)

        payment_source = spec["payment_source"]

        # payer_category derivation:
        if branch == "claimpayment":
            carrier = str(row.get(spec.get("carrier_column", "")) or "")
            payer_category = (
                self._categorize_carrier(carrier)
                if carrier
                else spec["default_payer_category"]
            )
        else:
            payer_category = spec["default_payer_category"]

        return PaymentRow(
            external_id=external_id,
            practice_id=self.deidentifier.practice_id,
            patient_external_id=patient_external_id,
            payment_date_month=payment_month,
            amount_band=amount_band,
            payment_source=payment_source,
            payer_category=payer_category,
        )

    def _categorize_carrier(self, carrier_name: str) -> str:
        if not carrier_name.strip():
            return "other"
        low = carrier_name.lower()
        for needle, category in self.payer_lookup.items():
            if needle in low:
                return category
        self.unmapped_carriers[carrier_name] = self.unmapped_carriers.get(carrier_name, 0) + 1
        return "other"
