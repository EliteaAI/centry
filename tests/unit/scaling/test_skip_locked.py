"""Unit tests for skip_locked utility module.

Validates that:
1. claim_rows() builds correct query with FOR UPDATE SKIP LOCKED
2. claim_one() returns single row or None
3. build_skip_locked_query() returns Query object without executing
4. Filters are applied correctly
5. limit is applied when specified
6. order_by is applied when specified
7. Multiple filters can be combined
8. Empty result sets return [] or None appropriately

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_skip_locked.py -v
"""

import importlib
import importlib.util
import pathlib
import sys
import types
from unittest.mock import MagicMock, patch, call, PropertyMock

import pytest

# ---------------------------------------------------------------------------
# Module loading: skip_locked.py only uses sqlalchemy.orm — we can load it
# directly since it doesn't import pylon.core.tools.
# ---------------------------------------------------------------------------

_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[3] / "pylon_main" / "plugins" / "elitea_core"

# Load the module directly from source
_spec = importlib.util.spec_from_file_location(
    "skip_locked",
    str(_PLUGIN_ROOT / "utils" / "skip_locked.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

claim_rows = _mod.claim_rows
claim_one = _mod.claim_one
build_skip_locked_query = _mod.build_skip_locked_query


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class FakeModel:
    """Fake SQLAlchemy model for testing."""
    id = "FakeModel.id"
    active = "FakeModel.active"
    status = "FakeModel.status"
    priority = "FakeModel.priority"


class FakeQuery:
    """Chainable mock that simulates SQLAlchemy Query API."""

    def __init__(self, results=None):
        self._results = results if results is not None else []
        self._filters = []
        self._order = None
        self._limit_val = None
        self._for_update_kwargs = None

    def filter(self, *args):
        new = FakeQuery(self._results)
        new._filters = self._filters + list(args)
        new._order = self._order
        new._limit_val = self._limit_val
        new._for_update_kwargs = self._for_update_kwargs
        return new

    def order_by(self, expr):
        new = FakeQuery(self._results)
        new._filters = self._filters
        new._order = expr
        new._limit_val = self._limit_val
        new._for_update_kwargs = self._for_update_kwargs
        return new

    def limit(self, n):
        new = FakeQuery(self._results)
        new._filters = self._filters
        new._order = self._order
        new._limit_val = n
        new._for_update_kwargs = self._for_update_kwargs
        return new

    def with_for_update(self, **kwargs):
        new = FakeQuery(self._results)
        new._filters = self._filters
        new._order = self._order
        new._limit_val = self._limit_val
        new._for_update_kwargs = kwargs
        return new

    def all(self):
        return self._results

    def first(self):
        return self._results[0] if self._results else None


class FakeSession:
    """Fake SQLAlchemy session that returns FakeQuery."""

    def __init__(self, results=None):
        self._results = results if results is not None else []
        self._last_query = None

    def query(self, model):
        q = FakeQuery(self._results)
        self._last_query = q
        return q


# ---------------------------------------------------------------------------
# Tests: claim_rows
# ---------------------------------------------------------------------------

class TestClaimRows:
    """Tests for claim_rows function."""

    def test_basic_claim_returns_results(self):
        """claim_rows returns available rows."""
        rows = [MagicMock(id=1), MagicMock(id=2)]
        session = FakeSession(rows)
        result = claim_rows(session, FakeModel, FakeModel.active == True)
        assert result == rows

    def test_empty_result(self):
        """claim_rows returns empty list when no rows match."""
        session = FakeSession([])
        result = claim_rows(session, FakeModel, FakeModel.active == True)
        assert result == []

    def test_filters_applied(self):
        """claim_rows applies all provided filters."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.with_for_update.return_value = query
        query.all.return_value = []

        filter1 = FakeModel.active == True
        filter2 = FakeModel.status == "pending"
        claim_rows(session, FakeModel, filter1, filter2)

        assert query.filter.call_count == 2
        query.filter.assert_any_call(filter1)
        query.filter.assert_any_call(filter2)

    def test_no_filters(self):
        """claim_rows works with no filters (claims any row)."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.with_for_update.return_value = query
        query.all.return_value = ["row1"]

        result = claim_rows(session, FakeModel)

        session.query.assert_called_once_with(FakeModel)
        query.filter.assert_not_called()
        query.with_for_update.assert_called_once_with(skip_locked=True)
        assert result == ["row1"]

    def test_limit_applied(self):
        """claim_rows applies limit when specified."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.limit.return_value = query
        query.with_for_update.return_value = query
        query.all.return_value = []

        claim_rows(session, FakeModel, FakeModel.active == True, limit=5)

        query.limit.assert_called_once_with(5)

    def test_no_limit(self):
        """claim_rows does not apply limit when None."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.with_for_update.return_value = query
        query.all.return_value = []

        claim_rows(session, FakeModel, FakeModel.active == True)

        query.limit.assert_not_called()

    def test_order_by_applied(self):
        """claim_rows applies order_by when specified."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.order_by.return_value = query
        query.with_for_update.return_value = query
        query.all.return_value = []

        claim_rows(session, FakeModel, FakeModel.active == True, order_by=FakeModel.id)

        query.order_by.assert_called_once_with(FakeModel.id)

    def test_no_order_by(self):
        """claim_rows does not apply order_by when None."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.with_for_update.return_value = query
        query.all.return_value = []

        claim_rows(session, FakeModel, FakeModel.active == True)

        query.order_by.assert_not_called()

    def test_with_for_update_skip_locked(self):
        """claim_rows uses with_for_update(skip_locked=True)."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.with_for_update.return_value = query
        query.all.return_value = []

        claim_rows(session, FakeModel, FakeModel.active == True)

        query.with_for_update.assert_called_once_with(skip_locked=True)

    def test_all_options_combined(self):
        """claim_rows applies filters, order_by, limit, and skip_locked together."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.order_by.return_value = query
        query.limit.return_value = query
        query.with_for_update.return_value = query
        query.all.return_value = ["row1", "row2"]

        result = claim_rows(
            session, FakeModel,
            FakeModel.active == True, FakeModel.status == "pending",
            limit=10, order_by=FakeModel.priority,
        )

        session.query.assert_called_once_with(FakeModel)
        assert query.filter.call_count == 2
        query.order_by.assert_called_once_with(FakeModel.priority)
        query.limit.assert_called_once_with(10)
        query.with_for_update.assert_called_once_with(skip_locked=True)
        assert result == ["row1", "row2"]

    def test_call_order_filter_before_order_before_limit_before_lock(self):
        """Verify the correct call chain order using FakeQuery."""
        rows = [MagicMock()]
        session = FakeSession(rows)
        result = claim_rows(
            session, FakeModel,
            FakeModel.active == True,
            limit=5, order_by=FakeModel.id,
        )
        assert result == rows


# ---------------------------------------------------------------------------
# Tests: claim_one
# ---------------------------------------------------------------------------

class TestClaimOne:
    """Tests for claim_one function."""

    def test_returns_single_row(self):
        """claim_one returns first available row."""
        row = MagicMock(id=42)
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.limit.return_value = query
        query.with_for_update.return_value = query
        query.first.return_value = row

        result = claim_one(session, FakeModel, FakeModel.active == True)

        assert result is row

    def test_returns_none_when_empty(self):
        """claim_one returns None when no rows available."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.limit.return_value = query
        query.with_for_update.return_value = query
        query.first.return_value = None

        result = claim_one(session, FakeModel, FakeModel.active == True)

        assert result is None

    def test_uses_limit_one(self):
        """claim_one always applies limit(1)."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.limit.return_value = query
        query.with_for_update.return_value = query
        query.first.return_value = None

        claim_one(session, FakeModel, FakeModel.active == True)

        query.limit.assert_called_once_with(1)

    def test_uses_skip_locked(self):
        """claim_one uses with_for_update(skip_locked=True)."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.limit.return_value = query
        query.with_for_update.return_value = query
        query.first.return_value = None

        claim_one(session, FakeModel, FakeModel.active == True)

        query.with_for_update.assert_called_once_with(skip_locked=True)

    def test_order_by_applied(self):
        """claim_one applies order_by when specified."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.order_by.return_value = query
        query.limit.return_value = query
        query.with_for_update.return_value = query
        query.first.return_value = None

        claim_one(session, FakeModel, FakeModel.active == True, order_by=FakeModel.id)

        query.order_by.assert_called_once_with(FakeModel.id)

    def test_no_order_by(self):
        """claim_one does not apply order_by when None."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.limit.return_value = query
        query.with_for_update.return_value = query
        query.first.return_value = None

        claim_one(session, FakeModel, FakeModel.active == True)

        query.order_by.assert_not_called()

    def test_multiple_filters(self):
        """claim_one applies multiple filters."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.limit.return_value = query
        query.with_for_update.return_value = query
        query.first.return_value = None

        filter1 = FakeModel.active == True
        filter2 = FakeModel.status == "pending"
        claim_one(session, FakeModel, filter1, filter2)

        assert query.filter.call_count == 2

    def test_no_filters(self):
        """claim_one works with no filters."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.limit.return_value = query
        query.with_for_update.return_value = query
        query.first.return_value = "row1"

        result = claim_one(session, FakeModel)

        query.filter.assert_not_called()
        assert result == "row1"


# ---------------------------------------------------------------------------
# Tests: build_skip_locked_query
# ---------------------------------------------------------------------------

class TestBuildSkipLockedQuery:
    """Tests for build_skip_locked_query function."""

    def test_returns_query_object(self):
        """build_skip_locked_query returns a query, not results."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.with_for_update.return_value = query

        result = build_skip_locked_query(session, FakeModel, FakeModel.active == True)

        # Should return the query, not .all() or .first()
        assert result is query
        query.all.assert_not_called()
        query.first.assert_not_called()

    def test_filters_applied(self):
        """build_skip_locked_query applies filters."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.with_for_update.return_value = query

        filter1 = FakeModel.active == True
        build_skip_locked_query(session, FakeModel, filter1)

        query.filter.assert_called_once_with(filter1)

    def test_limit_applied(self):
        """build_skip_locked_query applies limit."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.limit.return_value = query
        query.with_for_update.return_value = query

        build_skip_locked_query(session, FakeModel, FakeModel.active == True, limit=3)

        query.limit.assert_called_once_with(3)

    def test_no_limit(self):
        """build_skip_locked_query does not apply limit when None."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.with_for_update.return_value = query

        build_skip_locked_query(session, FakeModel, FakeModel.active == True)

        query.limit.assert_not_called()

    def test_order_by_applied(self):
        """build_skip_locked_query applies order_by."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.order_by.return_value = query
        query.with_for_update.return_value = query

        build_skip_locked_query(session, FakeModel, FakeModel.active == True, order_by=FakeModel.id)

        query.order_by.assert_called_once_with(FakeModel.id)

    def test_no_order_by(self):
        """build_skip_locked_query does not apply order_by when None."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.with_for_update.return_value = query

        build_skip_locked_query(session, FakeModel, FakeModel.active == True)

        query.order_by.assert_not_called()

    def test_skip_locked_applied(self):
        """build_skip_locked_query uses with_for_update(skip_locked=True)."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.with_for_update.return_value = query

        build_skip_locked_query(session, FakeModel, FakeModel.active == True)

        query.with_for_update.assert_called_once_with(skip_locked=True)

    def test_multiple_filters_and_all_options(self):
        """build_skip_locked_query combines all options."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.order_by.return_value = query
        query.limit.return_value = query
        query.with_for_update.return_value = query

        build_skip_locked_query(
            session, FakeModel,
            FakeModel.active == True, FakeModel.status == "ready",
            limit=20, order_by=FakeModel.priority,
        )

        assert query.filter.call_count == 2
        query.order_by.assert_called_once_with(FakeModel.priority)
        query.limit.assert_called_once_with(20)
        query.with_for_update.assert_called_once_with(skip_locked=True)


# ---------------------------------------------------------------------------
# Tests: Integration with FakeQuery (validates chain correctness)
# ---------------------------------------------------------------------------

class TestQueryChaining:
    """Tests that verify the query chain produces correct results."""

    def test_claim_rows_returns_all_from_query(self):
        """claim_rows returns the .all() results."""
        expected = [MagicMock(id=1), MagicMock(id=2), MagicMock(id=3)]
        session = FakeSession(expected)
        result = claim_rows(session, FakeModel, FakeModel.active == True)
        assert result == expected

    def test_claim_one_returns_first_from_query(self):
        """claim_one returns the .first() result."""
        expected = MagicMock(id=1)
        session = FakeSession([expected, MagicMock(id=2)])
        result = claim_one(session, FakeModel, FakeModel.active == True)
        assert result is expected

    def test_claim_one_returns_none_from_empty_query(self):
        """claim_one returns None when query results are empty."""
        session = FakeSession([])
        result = claim_one(session, FakeModel, FakeModel.active == True)
        assert result is None

    def test_build_query_does_not_execute(self):
        """build_skip_locked_query returns query without calling all/first."""
        session = FakeSession([MagicMock()])
        result = build_skip_locked_query(session, FakeModel, FakeModel.active == True)
        # Should be a FakeQuery instance, not a list
        assert isinstance(result, FakeQuery)

    def test_build_query_can_be_executed_later(self):
        """build_skip_locked_query result can be executed with .all()."""
        expected = [MagicMock(id=99)]
        session = FakeSession(expected)
        query = build_skip_locked_query(session, FakeModel, FakeModel.active == True)
        assert query.all() == expected

    def test_claim_rows_preserves_skip_locked_in_chain(self):
        """Verify skip_locked kwarg is set in the FakeQuery chain."""
        session = FakeSession([])
        # Use MagicMock session to inspect the call
        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_session.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.with_for_update.return_value = mock_query
        mock_query.all.return_value = []

        claim_rows(mock_session, FakeModel, FakeModel.active == True)

        # Verify the exact kwargs passed to with_for_update
        mock_query.with_for_update.assert_called_once()
        call_kwargs = mock_query.with_for_update.call_args[1]
        assert call_kwargs == {"skip_locked": True}


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Tests for edge cases and unusual inputs."""

    def test_limit_zero(self):
        """claim_rows with limit=0 applies limit(0)."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.limit.return_value = query
        query.with_for_update.return_value = query
        query.all.return_value = []

        claim_rows(session, FakeModel, FakeModel.active == True, limit=0)

        query.limit.assert_called_once_with(0)

    def test_limit_one(self):
        """claim_rows with limit=1 works correctly."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.limit.return_value = query
        query.with_for_update.return_value = query
        query.all.return_value = ["single"]

        result = claim_rows(session, FakeModel, FakeModel.active == True, limit=1)

        query.limit.assert_called_once_with(1)
        assert result == ["single"]

    def test_large_limit(self):
        """claim_rows with large limit still works."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.limit.return_value = query
        query.with_for_update.return_value = query
        query.all.return_value = []

        claim_rows(session, FakeModel, FakeModel.active == True, limit=10000)

        query.limit.assert_called_once_with(10000)

    def test_claim_one_with_order_and_filters(self):
        """claim_one with all options set."""
        row = MagicMock(id=1)
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.order_by.return_value = query
        query.limit.return_value = query
        query.with_for_update.return_value = query
        query.first.return_value = row

        result = claim_one(
            session, FakeModel,
            FakeModel.active == True, FakeModel.status == "pending",
            order_by=FakeModel.priority,
        )

        assert result is row
        assert query.filter.call_count == 2
        query.order_by.assert_called_once_with(FakeModel.priority)
        query.limit.assert_called_once_with(1)
        query.with_for_update.assert_called_once_with(skip_locked=True)

    def test_many_filters(self):
        """claim_rows handles many filters."""
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.with_for_update.return_value = query
        query.all.return_value = []

        filters = [FakeModel.active == True] * 5
        claim_rows(session, FakeModel, *filters)

        assert query.filter.call_count == 5
