"""Unit tests for the point-in-time shares-history extractor.

Pure parsing of the EODHD fundamentals blob — no DB — so this runs without
infrastructure. Covers the precedence chain (quarterly > annual >
Balance_Sheet fallback), the sharesMln fallback, and junk tolerance.
"""

from __future__ import annotations

from datetime import date

from stockscan.fundamentals.history import SharePoint, extract_shares_history


def test_quarterly_and_annual_merge_quarterly_wins_on_collision():
    payload = {
        "outstandingShares": {
            "annual": {
                "0": {"date": "2023", "dateFormatted": "2023-12-31", "shares": 1000},
                "1": {"date": "2022", "dateFormatted": "2022-12-31", "shares": 900},
            },
            "quarterly": {
                # Same date as an annual point — quarterly must win.
                "0": {"date": "2023-12-31", "dateFormatted": "2023-12-31", "shares": 1010},
                "1": {"date": "2023-09-30", "dateFormatted": "2023-09-30", "shares": 1005},
            },
        }
    }
    pts = extract_shares_history(payload)
    by_date = {p.period_date: p for p in pts}
    # Oldest-first ordering.
    assert [p.period_date for p in pts] == sorted(by_date)
    # Quarterly overwrote the colliding annual date.
    assert by_date[date(2023, 12, 31)].shares == 1010
    assert by_date[date(2023, 12, 31)].kind == "quarterly"
    # The non-colliding annual point survives.
    assert by_date[date(2022, 12, 31)] == SharePoint(date(2022, 12, 31), 900, "annual")
    # The extra quarterly point is present.
    assert by_date[date(2023, 9, 30)].shares == 1005


def test_shares_mln_fallback_when_shares_missing():
    payload = {
        "outstandingShares": {
            "quarterly": {
                "0": {"dateFormatted": "2024-03-31", "sharesMln": 1500.5, "shares": 0},
            }
        }
    }
    pts = extract_shares_history(payload)
    assert len(pts) == 1
    assert pts[0].shares == 1_500_500_000  # 1500.5e6 rounded


def test_balance_sheet_fallback_when_outstanding_absent():
    payload = {
        "Financials": {
            "Balance_Sheet": {
                "quarterly": {
                    "2024-03-31": {"date": "2024-03-31", "commonStockSharesOutstanding": "2000"},
                },
                "annual": {
                    "2023-12-31": {"date": "2023-12-31", "commonStockSharesOutstanding": "1900"},
                },
            }
        }
    }
    pts = extract_shares_history(payload)
    by_date = {p.period_date: p.shares for p in pts}
    assert by_date == {date(2024, 3, 31): 2000, date(2023, 12, 31): 1900}


def test_outstanding_preferred_over_balance_sheet():
    payload = {
        "outstandingShares": {
            "quarterly": {"0": {"dateFormatted": "2024-03-31", "shares": 2000}}
        },
        "Financials": {
            "Balance_Sheet": {
                "quarterly": {"2024-03-31": {"commonStockSharesOutstanding": "9999"}}
            }
        },
    }
    pts = extract_shares_history(payload)
    assert len(pts) == 1
    assert pts[0].shares == 2000  # outstandingShares wins; balance sheet ignored


def test_junk_and_empty_tolerated():
    assert extract_shares_history({}) == []
    assert extract_shares_history({"outstandingShares": "nonsense"}) == []
    payload = {
        "outstandingShares": {
            "quarterly": {
                "0": {"dateFormatted": "bad-date", "shares": 100},
                "1": {"dateFormatted": "2024-03-31", "shares": "NA"},
                "2": {"dateFormatted": "2024-06-30", "shares": -5},
                "3": {"dateFormatted": "2024-09-30", "shares": 1234},
            }
        }
    }
    pts = extract_shares_history(payload)
    # Only the one clean, positive, well-dated record survives.
    assert [(p.period_date, p.shares) for p in pts] == [(date(2024, 9, 30), 1234)]
