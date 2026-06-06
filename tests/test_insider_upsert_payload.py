"""Payload-shape tests for the insider upsert path.

The bug being locked here: when ``_pull_for_symbol`` queries the
provider with ``code=AAPL.US``, EODHD doesn't always echo the ticker
back into each record. ``upsert_transactions`` used to drop every such
record silently because it couldn't extract a symbol. The fix is the
``symbol=`` fallback kwarg — these tests verify it survives future
refactors.

We don't write to the DB here; instead we intercept the payload that
``upsert_transactions`` would have handed to ``session.execute(...)``
and inspect it directly. That keeps the test fast and independent of
Postgres.
"""

from __future__ import annotations

from typing import Any

import pytest

from stockscan.insider.store import upsert_transactions


class _CapturingSession:
    """Stub that captures the params list passed to ``execute``."""

    def __init__(self) -> None:
        self.captured: list[dict[str, Any]] = []

    def execute(self, _stmt: Any, params: list[dict[str, Any]] | None = None) -> Any:
        if isinstance(params, list):
            self.captured.extend(params)

        class _Result:
            rowcount = len(params or [])

        return _Result()


# Two realistic shapes — with and without a per-record ticker. The EODHD
# response has been seen both ways across various community examples.
_RECORD_WITH_CODE = {
    "code": "AAPL.US",
    "transactionDate": "2024-03-15",
    "transactionCode": "P",
    "transactionAmount": 1000,
    "transactionPrice": 175.50,
    "ownerName": "Smith, John",
    "ownerRelationship": "CEO",
    "postTransactionAmount": 50000,
    "reportDate": "2024-03-18",
}

_RECORD_WITHOUT_CODE = {
    # NO 'code' field — this is the case the bug was hitting.
    "transactionDate": "2024-03-15",
    "transactionCode": "P",
    "transactionAmount": 1000,
    "transactionPrice": 175.50,
    "ownerName": "Smith, John",
    "ownerRelationship": "CEO",
    "postTransactionAmount": 50000,
    "reportDate": "2024-03-18",
}


def test_upsert_uses_per_record_code_when_available() -> None:
    """When the response includes a ``code``, that wins over the kwarg."""
    s = _CapturingSession()
    n = upsert_transactions(
        [_RECORD_WITH_CODE], symbol="OVERRIDE_ME", session=s,
    )
    assert n == 1
    assert len(s.captured) == 1
    assert s.captured[0]["symbol"] == "AAPL"


def test_upsert_falls_back_to_kwarg_symbol() -> None:
    """When the response omits ``code``, the kwarg symbol is used.

    This is the regression test for the silent-drop bug.
    """
    s = _CapturingSession()
    n = upsert_transactions(
        [_RECORD_WITHOUT_CODE], symbol="AAOI", session=s,
    )
    assert n == 1
    assert len(s.captured) == 1
    assert s.captured[0]["symbol"] == "AAOI"


def test_upsert_drops_records_when_no_symbol_anywhere() -> None:
    """Without per-record code AND without the kwarg → skip the row."""
    s = _CapturingSession()
    n = upsert_transactions([_RECORD_WITHOUT_CODE], session=s)
    assert n == 0
    assert s.captured == []


def test_upsert_strips_exchange_suffix_from_fallback() -> None:
    """``symbol="AAOI.US"`` (with exchange) gets normalised to ``AAOI``."""
    s = _CapturingSession()
    upsert_transactions([_RECORD_WITHOUT_CODE], symbol="AAOI.US", session=s)
    assert s.captured[0]["symbol"] == "AAOI"


def test_upsert_normalises_case_on_fallback_symbol() -> None:
    """Lowercase kwarg gets uppercased — matches the canonical symbol form."""
    s = _CapturingSession()
    upsert_transactions([_RECORD_WITHOUT_CODE], symbol="aaoi", session=s)
    assert s.captured[0]["symbol"] == "AAOI"


def test_upsert_filters_non_p_s_codes() -> None:
    """Form-4 codes other than P/S (G for gift, A for grant) are skipped."""
    grant = dict(_RECORD_WITH_CODE)
    grant["transactionCode"] = "A"
    gift = dict(_RECORD_WITH_CODE)
    gift["transactionCode"] = "G"
    s = _CapturingSession()
    n = upsert_transactions([grant, gift, _RECORD_WITH_CODE], session=s)
    assert n == 1
    assert len(s.captured) == 1


def test_upsert_computes_value_from_shares_times_price() -> None:
    """Per-record value = shares × price (used by the UI's "$ buy value")."""
    s = _CapturingSession()
    upsert_transactions([_RECORD_WITH_CODE], session=s)
    row = s.captured[0]
    assert row["shares"] == 1000
    assert row["price"] == 175.50
    assert row["value"] == pytest.approx(175_500.0, abs=0.01)


@pytest.mark.parametrize("missing", ["transactionDate", "transactionCode"])
def test_upsert_drops_records_missing_required_fields(missing: str) -> None:
    """Records without date or code can't be deduped/stored — skip them."""
    bad = dict(_RECORD_WITH_CODE)
    del bad[missing]
    s = _CapturingSession()
    n = upsert_transactions([bad], session=s)
    assert n == 0


def test_upsert_handles_alternate_field_names() -> None:
    """Some community examples use snake_case; tolerate it."""
    alt = {
        "code": "MSFT.US",
        "transaction_date": "2024-03-15",
        "transaction_code": "S",
        "transaction_amount": 500,
        "transaction_price": 420.0,
        "owner_name": "Doe, Jane",
        "owner_relationship": "CFO",
    }
    s = _CapturingSession()
    n = upsert_transactions([alt], session=s)
    assert n == 1
    row = s.captured[0]
    assert row["symbol"] == "MSFT"
    assert row["insider_name"] == "Doe, Jane"
    assert row["insider_title"] == "CFO"
    assert row["transaction_code"] == "S"


def test_upsert_mixed_records_some_with_some_without_code() -> None:
    """A realistic per-symbol response: most records carry a code,
    a few don't. All must land — the kwarg covers the gaps."""
    s = _CapturingSession()
    n = upsert_transactions(
        [_RECORD_WITH_CODE, _RECORD_WITHOUT_CODE, _RECORD_WITH_CODE],
        symbol="AAPL",
        session=s,
    )
    assert n == 3
    symbols = {p["symbol"] for p in s.captured}
    assert symbols == {"AAPL"}
