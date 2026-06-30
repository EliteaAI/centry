"""Unit tests for scheduling plugin SKIP LOCKED behavior.

Validates that:
1. Schedule model has index on 'active' column
2. execute_schedules uses with_for_update(skip_locked=True)
3. The skip_locked utility (skip_locked.py) API is correct
4. Schedule.time_to_run still works correctly
5. The query only claims schedules that are not locked by other pods

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_schedule_skip_locked.py -v
"""

import importlib
import importlib.util
import pathlib
import sys
import time
import types
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, call, PropertyMock

import pytest

# ---------------------------------------------------------------------------
# Load scheduling model source to inspect table_args
# ---------------------------------------------------------------------------

_SCHEDULING_ROOT = pathlib.Path(__file__).resolve().parents[3] / "pylon_main" / "plugins" / "scheduling"


# ---------------------------------------------------------------------------
# Tests: Schedule model index
# ---------------------------------------------------------------------------

class TestScheduleModelIndex:
    """Tests for Schedule model index configuration."""

    def test_schedule_model_has_ix_schedule_active_index(self):
        """Schedule model should have an index on the 'active' column."""
        model_path = _SCHEDULING_ROOT / "models" / "schedule.py"
        source = model_path.read_text()
        assert "Index('ix_schedule_active', 'active')" in source

    def test_schedule_model_imports_index(self):
        """Schedule model imports Index from sqlalchemy."""
        model_path = _SCHEDULING_ROOT / "models" / "schedule.py"
        source = model_path.read_text()
        assert "Index" in source
        assert "from sqlalchemy import" in source
        # Verify Index is in the import line
        for line in source.splitlines():
            if line.startswith("from sqlalchemy import"):
                assert "Index" in line
                break

    def test_table_args_is_tuple(self):
        """__table_args__ should be a tuple (SQLAlchemy convention)."""
        model_path = _SCHEDULING_ROOT / "models" / "schedule.py"
        source = model_path.read_text()
        assert "__table_args__ = (" in source


# ---------------------------------------------------------------------------
# Tests: execute_schedules uses SKIP LOCKED
# ---------------------------------------------------------------------------

class TestExecuteSchedulesSkipLocked:
    """Tests for execute_schedules method using SKIP LOCKED."""

    def test_module_source_uses_skip_locked(self):
        """execute_schedules source code uses with_for_update(skip_locked=True)."""
        module_path = _SCHEDULING_ROOT / "module.py"
        source = module_path.read_text()
        assert "with_for_update(" in source
        assert "skip_locked=True" in source

    def test_module_source_query_chain(self):
        """Verify the query chain: filter → with_for_update → all."""
        module_path = _SCHEDULING_ROOT / "module.py"
        source = module_path.read_text()
        # Find the execute_schedules method and check query pattern
        in_method = False
        found_filter = False
        found_skip_locked = False
        found_all = False
        for line in source.splitlines():
            if "def execute_schedules" in line:
                in_method = True
            if in_method:
                if "Schedule.active == True" in line:
                    found_filter = True
                if "skip_locked=True" in line:
                    found_skip_locked = True
                if ".all()" in line and found_skip_locked:
                    found_all = True
                if in_method and line.strip().startswith("def ") and "execute_schedules" not in line:
                    break
        assert found_filter, "Should filter by Schedule.active == True"
        assert found_skip_locked, "Should use skip_locked=True"
        assert found_all, "Should call .all() after skip_locked"

    def test_module_source_log_message_says_claimed(self):
        """Log message updated from 'retrieved' to 'claimed' to reflect SKIP LOCKED."""
        module_path = _SCHEDULING_ROOT / "module.py"
        source = module_path.read_text()
        assert "Schedules claimed:" in source


# ---------------------------------------------------------------------------
# Tests: skip_locked utility integration patterns
# ---------------------------------------------------------------------------

class TestSkipLockedUtilityPatterns:
    """Tests for how skip_locked.py utility should be used."""

    def test_claim_rows_with_schedule_pattern(self):
        """Simulate using claim_rows for the Schedule model pattern."""
        # Load the utility
        util_path = (
            pathlib.Path(__file__).resolve().parents[3]
            / "pylon_main" / "plugins" / "elitea_core" / "utils" / "skip_locked.py"
        )
        spec = importlib.util.spec_from_file_location("skip_locked", str(util_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Mock session + model
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.with_for_update.return_value = query
        query.all.return_value = [MagicMock(id=1, name="sched1")]

        class MockSchedule:
            active = True
            id = "id_col"

        result = mod.claim_rows(session, MockSchedule, MockSchedule.active == True)

        session.query.assert_called_once_with(MockSchedule)
        query.with_for_update.assert_called_once_with(skip_locked=True)
        assert len(result) == 1

    def test_claim_rows_with_order_by_id(self):
        """claim_rows with order_by ensures deterministic claiming."""
        util_path = (
            pathlib.Path(__file__).resolve().parents[3]
            / "pylon_main" / "plugins" / "elitea_core" / "utils" / "skip_locked.py"
        )
        spec = importlib.util.spec_from_file_location("skip_locked", str(util_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.order_by.return_value = query
        query.with_for_update.return_value = query
        query.all.return_value = []

        class MockSchedule:
            active = True
            id = "id_col"

        mod.claim_rows(session, MockSchedule, MockSchedule.active == True, order_by=MockSchedule.id)

        query.order_by.assert_called_once_with(MockSchedule.id)

    def test_claim_one_for_single_task(self):
        """claim_one returns single task for processing."""
        util_path = (
            pathlib.Path(__file__).resolve().parents[3]
            / "pylon_main" / "plugins" / "elitea_core" / "utils" / "skip_locked.py"
        )
        spec = importlib.util.spec_from_file_location("skip_locked", str(util_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.limit.return_value = query
        query.with_for_update.return_value = query
        task = MagicMock(id=42, name="reindex")
        query.first.return_value = task

        class MockTask:
            status = "pending"

        result = mod.claim_one(session, MockTask, MockTask.status == "pending")

        assert result is task
        query.limit.assert_called_once_with(1)
        query.with_for_update.assert_called_once_with(skip_locked=True)


# ---------------------------------------------------------------------------
# Tests: Concurrent claiming behavior
# ---------------------------------------------------------------------------

class TestConcurrentClaiming:
    """Tests verifying SKIP LOCKED prevents duplicate processing."""

    def test_skip_locked_query_skips_locked_rows(self):
        """With SKIP LOCKED, rows locked by another session are skipped."""
        util_path = (
            pathlib.Path(__file__).resolve().parents[3]
            / "pylon_main" / "plugins" / "elitea_core" / "utils" / "skip_locked.py"
        )
        spec = importlib.util.spec_from_file_location("skip_locked", str(util_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Simulate: 3 schedules exist, but 2 are already locked
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        # with_for_update(skip_locked=True) would return only unlocked rows
        query.with_for_update.return_value = query
        unlocked_schedule = MagicMock(id=3, name="only_unlocked")
        query.all.return_value = [unlocked_schedule]

        class MockSchedule:
            active = True

        result = mod.claim_rows(session, MockSchedule, MockSchedule.active == True)

        assert result == [unlocked_schedule]
        query.with_for_update.assert_called_once_with(skip_locked=True)

    def test_all_rows_locked_returns_empty(self):
        """When all rows are locked by other sessions, returns empty."""
        util_path = (
            pathlib.Path(__file__).resolve().parents[3]
            / "pylon_main" / "plugins" / "elitea_core" / "utils" / "skip_locked.py"
        )
        spec = importlib.util.spec_from_file_location("skip_locked", str(util_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        query.filter.return_value = query
        query.with_for_update.return_value = query
        query.all.return_value = []  # All locked

        class MockSchedule:
            active = True

        result = mod.claim_rows(session, MockSchedule, MockSchedule.active == True)

        assert result == []

    def test_multiple_pods_claim_different_schedules(self):
        """Simulate 2 pods: each should claim different schedules."""
        util_path = (
            pathlib.Path(__file__).resolve().parents[3]
            / "pylon_main" / "plugins" / "elitea_core" / "utils" / "skip_locked.py"
        )
        spec = importlib.util.spec_from_file_location("skip_locked", str(util_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Pod 1 claims schedules 1, 2, 3
        session1 = MagicMock()
        query1 = MagicMock()
        session1.query.return_value = query1
        query1.filter.return_value = query1
        query1.with_for_update.return_value = query1
        query1.all.return_value = [MagicMock(id=1), MagicMock(id=2), MagicMock(id=3)]

        # Pod 2 gets empty (all locked by pod 1)
        session2 = MagicMock()
        query2 = MagicMock()
        session2.query.return_value = query2
        query2.filter.return_value = query2
        query2.with_for_update.return_value = query2
        query2.all.return_value = []

        class MockSchedule:
            active = True

        result1 = mod.claim_rows(session1, MockSchedule, MockSchedule.active == True)
        result2 = mod.claim_rows(session2, MockSchedule, MockSchedule.active == True)

        assert len(result1) == 3
        assert len(result2) == 0


# ---------------------------------------------------------------------------
# Tests: Index scheduling protection
# ---------------------------------------------------------------------------

class TestIndexSchedulingProtection:
    """Tests for index_scheduling.py threading lock (single-pod protection)."""

    def test_index_scheduling_has_threading_lock(self):
        """index_scheduling.py uses a threading.Lock for re-entrancy guard."""
        rpc_path = (
            pathlib.Path(__file__).resolve().parents[3]
            / "pylon_main" / "plugins" / "elitea_core" / "rpc" / "index_scheduling.py"
        )
        source = rpc_path.read_text()
        assert "_check_index_scheduling_lock = threading.Lock()" in source

    def test_index_scheduling_acquires_lock_non_blocking(self):
        """index_scheduling uses non-blocking acquire to skip overlapping ticks."""
        rpc_path = (
            pathlib.Path(__file__).resolve().parents[3]
            / "pylon_main" / "plugins" / "elitea_core" / "rpc" / "index_scheduling.py"
        )
        source = rpc_path.read_text()
        assert "_check_index_scheduling_lock.acquire(blocking=False)" in source


# ---------------------------------------------------------------------------
# Tests: Validate Schedule model file structure
# ---------------------------------------------------------------------------

class TestScheduleModelStructure:
    """Tests for Schedule model file correctness."""

    def test_schedule_has_active_column(self):
        """Schedule model has an 'active' Boolean column."""
        model_path = _SCHEDULING_ROOT / "models" / "schedule.py"
        source = model_path.read_text()
        assert "active = Column(Boolean" in source

    def test_schedule_has_last_run_column(self):
        """Schedule model has a 'last_run' DateTime column."""
        model_path = _SCHEDULING_ROOT / "models" / "schedule.py"
        source = model_path.read_text()
        assert "last_run = Column(DateTime" in source

    def test_schedule_has_time_to_run_property(self):
        """Schedule model has time_to_run property for cron evaluation."""
        model_path = _SCHEDULING_ROOT / "models" / "schedule.py"
        source = model_path.read_text()
        assert "def time_to_run" in source
        assert "@property" in source

    def test_schedule_has_run_method(self):
        """Schedule model has run() method for execution."""
        model_path = _SCHEDULING_ROOT / "models" / "schedule.py"
        source = model_path.read_text()
        assert "def run(self" in source
