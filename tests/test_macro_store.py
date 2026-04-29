"""Unit tests for the macro_series persistence layer.

Pure tests (no DB): we mock the SQLAlchemy ``Session`` and verify the
shape of the upsert payload, the SQL parameter binding, and the
return-value transformations (``pd.Series`` shape, ``Decimal`` precision).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

from stockscan.data.macro_store import (
    get_macro_series,
    latest_macro_value,
    upsert_macro_series,
)
from stockscan.data.providers.base import MacroRow


def _row(d: date, value: float) -> MacroRow:
    return MacroRow(
        series_code="BAMLH0A0HYM2",
        as_of_date=d,
        value=Decimal(str(value)),
        source="fred",
    )


# -----------------------------------------------------------------------
# upsert_macro_series
# -----------------------------------------------------------------------
class TestUpsertMacroSeries:
    def test_returns_zero_for_empty_input(self):
        sess = MagicMock()
        n = upsert_macro_series([], session=sess)
        assert n == 0
        sess.execute.assert_not_called()

    def test_passes_canonical_payload_to_executemany(self):
        sess = MagicMock()
        rows = [
            _row(date(2026, 4, 20), 3.45),
            _row(date(2026, 4, 21), 3.52),
            _row(date(2026, 4, 23), 3.61),
        ]
        n = upsert_macro_series(rows, session=sess)
        assert n == 3
        # SQLAlchemy's executemany API: session.execute(stmt, list_of_dicts).
        assert sess.execute.call_count == 1
        _stmt, payload = sess.execute.call_args[0]
        assert isinstance(payload, list)
        assert len(payload) == 3
        first = payload[0]
        assert first["series_code"] == "BAMLH0A0HYM2"
        assert first["as_of_date"] == date(2026, 4, 20)
        assert first["value"] == Decimal("3.45")
        assert first["source"] == "fred"

    def test_idempotent_friendly_payload_shape(self):
        """Each row must include exactly the columns our INSERT names —
        ON CONFLICT DO UPDATE depends on the value/source columns being
        present in the bind map."""
        sess = MagicMock()
        upsert_macro_series([_row(date(2026, 4, 20), 3.45)], session=sess)
        _stmt, payload = sess.execute.call_args[0]
        assert set(payload[0].keys()) == {"series_code", "as_of_date", "value", "source"}


# -----------------------------------------------------------------------
# get_macro_series
# -----------------------------------------------------------------------
class TestGetMacroSeries:
    def _mock_session_returning(self, mappings: list[dict[str, object]]) -> MagicMock:
        sess = MagicMock()
        result = MagicMock()
        result.mappings.return_value.all.return_value = mappings
        sess.execute.return_value = result
        return sess

    def test_returns_date_indexed_float_series(self):
        sess = self._mock_session_returning(
            [
                {"as_of_date": date(2026, 4, 20), "value": Decimal("3.45")},
                {"as_of_date": date(2026, 4, 21), "value": Decimal("3.52")},
            ]
        )
        s = get_macro_series("BAMLH0A0HYM2", date(2026, 4, 20), date(2026, 4, 30), session=sess)
        assert isinstance(s, pd.Series)
        assert s.dtype == float
        assert s.name == "BAMLH0A0HYM2"
        assert len(s) == 2
        assert s.index[0] == pd.Timestamp("2026-04-20")
        assert s.iloc[0] == 3.45

    def test_empty_result_returns_empty_named_float_series(self):
        sess = self._mock_session_returning([])
        s = get_macro_series("X", date(2026, 4, 20), date(2026, 4, 30), session=sess)
        assert isinstance(s, pd.Series)
        assert s.empty
        assert s.dtype == float
        assert s.name == "X"

    def test_passes_correct_bind_params(self):
        sess = self._mock_session_returning([])
        get_macro_series(
            "BAMLH0A0HYM2",
            date(2026, 4, 20),
            date(2026, 4, 30),
            session=sess,
        )
        _stmt, params = sess.execute.call_args[0]
        assert params["series_code"] == "BAMLH0A0HYM2"
        assert params["start"] == date(2026, 4, 20)
        assert params["end"] == date(2026, 4, 30)


# -----------------------------------------------------------------------
# latest_macro_value
# -----------------------------------------------------------------------
class TestLatestMacroValue:
    def test_returns_decimal_when_row_present(self):
        sess = MagicMock()
        # SimpleNamespace mimics a SQLAlchemy Row's attribute access.
        sess.execute.return_value.first.return_value = SimpleNamespace(
            value=Decimal("3.61"), as_of_date=date(2026, 4, 23)
        )
        v = latest_macro_value("BAMLH0A0HYM2", date(2026, 4, 25), session=sess)
        assert v == Decimal("3.61")

    def test_returns_none_when_no_row(self):
        sess = MagicMock()
        sess.execute.return_value.first.return_value = None
        assert latest_macro_value("X", date(2026, 4, 25), session=sess) is None

    def test_passes_as_of_through(self):
        sess = MagicMock()
        sess.execute.return_value.first.return_value = None
        latest_macro_value("X", date(2026, 4, 25), session=sess)
        _stmt, params = sess.execute.call_args[0]
        assert params["series_code"] == "X"
        assert params["as_of"] == date(2026, 4, 25)
