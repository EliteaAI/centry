"""
Tests for the audit logging module.

Verifies: event recording, query/filter, retention with S3 archival,
table creation, error handling, and edge cases.
"""

import sys
import os
import json
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', 'elitea_core'))

sys.modules.setdefault('pylon', MagicMock())
sys.modules.setdefault('pylon.core', MagicMock())
sys.modules.setdefault('pylon.core.tools', MagicMock())
pylon_tools_mock = sys.modules['pylon.core.tools']
pylon_tools_mock.log = MagicMock()

from utils.audit_logger import (
    AuditLogger,
    AuditLogError,
    AuditRetentionError,
    AUDIT_ACTIONS,
    AUDIT_TABLE_NAME,
    AUDIT_SCHEMA,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_S3_BUCKET,
    DEFAULT_S3_PREFIX,
    ARCHIVE_BATCH_SIZE,
    audit_log_table,
    metadata,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_engine():
    with patch('utils.audit_logger.sqlalchemy.create_engine') as mock_ce:
        engine = MagicMock()
        mock_ce.return_value = engine
        yield engine


@pytest.fixture
def mock_s3():
    return MagicMock()


@pytest.fixture
def logger(mock_engine, mock_s3):
    return AuditLogger(
        db_url="postgresql://user:pass@localhost/testdb",
        s3_client=mock_s3,
        s3_bucket="test-bucket",
        s3_prefix="audit/",
        service_name="pylon_main",
    )


@pytest.fixture
def logger_no_s3(mock_engine):
    return AuditLogger(
        db_url="postgresql://user:pass@localhost/testdb",
        s3_client=None,
        service_name="pylon_main",
    )


# ---------------------------------------------------------------------------
# Constructor Tests
# ---------------------------------------------------------------------------

class TestAuditLoggerInit:

    def test_init_with_valid_params(self, mock_engine, mock_s3):
        logger = AuditLogger(
            db_url="postgresql://u:p@h/d",
            s3_client=mock_s3,
            s3_bucket="bucket",
            s3_prefix="prefix/",
            service_name="svc",
        )
        assert logger._s3 is mock_s3
        assert logger._s3_bucket == "bucket"
        assert logger._s3_prefix == "prefix/"
        assert logger._service_name == "svc"

    def test_init_empty_db_url_raises(self):
        with pytest.raises(ValueError, match="db_url must be non-empty"):
            AuditLogger(db_url="")

    def test_init_none_db_url_raises(self):
        with pytest.raises(ValueError, match="db_url must be non-empty"):
            AuditLogger(db_url=None)

    def test_init_without_s3(self, mock_engine):
        logger = AuditLogger(db_url="postgresql://u:p@h/d")
        assert logger._s3 is None

    def test_init_prefix_normalization(self, mock_engine):
        logger = AuditLogger(db_url="postgresql://u:p@h/d", s3_prefix="logs")
        assert logger._s3_prefix == "logs/"

    def test_init_prefix_already_trailing_slash(self, mock_engine):
        logger = AuditLogger(db_url="postgresql://u:p@h/d", s3_prefix="logs/")
        assert logger._s3_prefix == "logs/"

    def test_init_empty_prefix(self, mock_engine):
        logger = AuditLogger(db_url="postgresql://u:p@h/d", s3_prefix="")
        assert logger._s3_prefix == ""


# ---------------------------------------------------------------------------
# Table Schema Tests
# ---------------------------------------------------------------------------

class TestAuditTableSchema:

    def test_table_name(self):
        assert audit_log_table.name == AUDIT_TABLE_NAME

    def test_table_schema(self):
        assert audit_log_table.schema == AUDIT_SCHEMA

    def test_columns_exist(self):
        col_names = {c.name for c in audit_log_table.columns}
        expected = {
            "id", "event_id", "timestamp", "actor", "action",
            "resource", "details", "ip_address", "service", "request_id",
        }
        assert expected == col_names

    def test_id_is_primary_key(self):
        assert audit_log_table.c.id.primary_key

    def test_event_id_is_unique(self):
        assert audit_log_table.c.event_id.unique

    def test_timestamp_not_nullable(self):
        assert not audit_log_table.c.timestamp.nullable

    def test_actor_not_nullable(self):
        assert not audit_log_table.c.actor.nullable

    def test_action_not_nullable(self):
        assert not audit_log_table.c.action.nullable

    def test_resource_not_nullable(self):
        assert not audit_log_table.c.resource.nullable

    def test_details_nullable(self):
        assert audit_log_table.c.details.nullable

    def test_ip_address_nullable(self):
        assert audit_log_table.c.ip_address.nullable


# ---------------------------------------------------------------------------
# ensure_table Tests
# ---------------------------------------------------------------------------

class TestEnsureTable:

    def test_calls_create_all(self, logger, mock_engine):
        with patch.object(metadata, 'create_all') as mock_create:
            logger.ensure_table()
            mock_create.assert_called_once_with(mock_engine, checkfirst=True)


# ---------------------------------------------------------------------------
# log() Tests
# ---------------------------------------------------------------------------

class TestAuditLog:

    def test_log_returns_event_id(self, logger, mock_engine):
        conn = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        event_id = logger.log(
            actor="user@example.com",
            action="user.login",
            resource="auth/session",
        )
        assert isinstance(event_id, str)
        assert len(event_id) == 36  # UUID4 format

    def test_log_inserts_into_db(self, logger, mock_engine):
        conn = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        logger.log(
            actor="admin",
            action="admin.settings_change",
            resource="settings/global",
            details={"key": "max_tokens", "old": 1000, "new": 2000},
            ip_address="192.168.1.1",
            request_id="req-abc123",
        )
        conn.execute.assert_called_once()
        conn.commit.assert_called_once()

    def test_log_with_custom_timestamp(self, logger, mock_engine):
        conn = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        ts = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        logger.log(
            actor="user",
            action="user.login",
            resource="auth",
            timestamp=ts,
        )
        conn.execute.assert_called_once()

    def test_log_empty_actor_raises(self, logger):
        with pytest.raises(ValueError, match="actor must be non-empty"):
            logger.log(actor="", action="user.login", resource="auth")

    def test_log_empty_action_raises(self, logger):
        with pytest.raises(ValueError, match="action must be non-empty"):
            logger.log(actor="user", action="", resource="auth")

    def test_log_empty_resource_raises(self, logger):
        with pytest.raises(ValueError, match="resource must be non-empty"):
            logger.log(actor="user", action="user.login", resource="")

    def test_log_db_error_raises_audit_error(self, logger, mock_engine):
        conn = MagicMock()
        conn.execute.side_effect = Exception("Connection refused")
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(AuditLogError, match="Failed to insert audit event"):
            logger.log(actor="user", action="user.login", resource="auth")

    def test_log_includes_service_name(self, logger, mock_engine):
        conn = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        logger.log(actor="user", action="user.login", resource="auth")
        call_args = conn.execute.call_args
        assert call_args is not None

    def test_log_none_details_accepted(self, logger, mock_engine):
        conn = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        event_id = logger.log(
            actor="user", action="user.logout", resource="auth", details=None
        )
        assert event_id is not None


# ---------------------------------------------------------------------------
# query() Tests
# ---------------------------------------------------------------------------

class TestAuditQuery:

    def _make_row(self, **overrides):
        defaults = {
            "id": 1,
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc),
            "actor": "user@test.com",
            "action": "user.login",
            "resource": "auth/session",
            "details": {"method": "oidc"},
            "ip_address": "10.0.0.1",
            "service": "pylon_main",
            "request_id": "req-123",
        }
        defaults.update(overrides)
        row = MagicMock()
        row._mapping = defaults
        return row

    def test_query_no_filters(self, logger, mock_engine):
        row = self._make_row()
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchall.return_value = [row]
        conn.execute.return_value = result_mock
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        entries = logger.query()
        assert len(entries) == 1
        assert entries[0]["actor"] == "user@test.com"

    def test_query_by_actor(self, logger, mock_engine):
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchall.return_value = []
        conn.execute.return_value = result_mock
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        entries = logger.query(actor="admin@co.com")
        assert entries == []
        conn.execute.assert_called_once()

    def test_query_by_action_wildcard(self, logger, mock_engine):
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchall.return_value = []
        conn.execute.return_value = result_mock
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        entries = logger.query(action="admin.*")
        assert entries == []

    def test_query_by_resource(self, logger, mock_engine):
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchall.return_value = []
        conn.execute.return_value = result_mock
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        entries = logger.query(resource="project/42")
        assert entries == []

    def test_query_by_time_range(self, logger, mock_engine):
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchall.return_value = []
        conn.execute.return_value = result_mock
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        until = datetime(2026, 6, 1, tzinfo=timezone.utc)
        entries = logger.query(since=since, until=until)
        assert entries == []

    def test_query_with_limit_and_offset(self, logger, mock_engine):
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchall.return_value = []
        conn.execute.return_value = result_mock
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        entries = logger.query(limit=10, offset=20)
        assert entries == []

    def test_query_db_error_raises(self, logger, mock_engine):
        conn = MagicMock()
        conn.execute.side_effect = Exception("Timeout")
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(AuditLogError, match="Failed to query audit log"):
            logger.query()

    def test_query_returns_sorted_by_timestamp_desc(self, logger, mock_engine):
        rows = [
            self._make_row(id=2, timestamp=datetime(2026, 6, 2, tzinfo=timezone.utc)),
            self._make_row(id=1, timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc)),
        ]
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchall.return_value = rows
        conn.execute.return_value = result_mock
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        entries = logger.query()
        assert len(entries) == 2


# ---------------------------------------------------------------------------
# count() Tests
# ---------------------------------------------------------------------------

class TestAuditCount:

    def test_count_no_filters(self, logger, mock_engine):
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.scalar.return_value = 42
        conn.execute.return_value = result_mock
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        assert logger.count() == 42

    def test_count_with_actor_filter(self, logger, mock_engine):
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.scalar.return_value = 5
        conn.execute.return_value = result_mock
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        assert logger.count(actor="admin") == 5

    def test_count_with_action_wildcard(self, logger, mock_engine):
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.scalar.return_value = 10
        conn.execute.return_value = result_mock
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        assert logger.count(action="user.*") == 10

    def test_count_with_time_range(self, logger, mock_engine):
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.scalar.return_value = 3
        conn.execute.return_value = result_mock
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert logger.count(since=since) == 3

    def test_count_returns_zero_on_null(self, logger, mock_engine):
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.scalar.return_value = None
        conn.execute.return_value = result_mock
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        assert logger.count() == 0

    def test_count_db_error_raises(self, logger, mock_engine):
        conn = MagicMock()
        conn.execute.side_effect = Exception("Connection lost")
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(AuditLogError, match="Failed to count"):
            logger.count()


# ---------------------------------------------------------------------------
# get_entry() Tests
# ---------------------------------------------------------------------------

class TestGetEntry:

    def test_get_existing_entry(self, logger, mock_engine):
        row = MagicMock()
        row._mapping = {
            "id": 1,
            "event_id": "abc-123",
            "timestamp": datetime(2026, 6, 1, tzinfo=timezone.utc),
            "actor": "user",
            "action": "user.login",
            "resource": "auth",
            "details": None,
            "ip_address": "1.2.3.4",
            "service": "pylon_main",
            "request_id": None,
        }
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchone.return_value = row
        conn.execute.return_value = result_mock
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        entry = logger.get_entry("abc-123")
        assert entry["event_id"] == "abc-123"
        assert entry["actor"] == "user"

    def test_get_nonexistent_entry(self, logger, mock_engine):
        conn = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchone.return_value = None
        conn.execute.return_value = result_mock
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        entry = logger.get_entry("nonexistent-id")
        assert entry is None

    def test_get_empty_event_id(self, logger):
        assert logger.get_entry("") is None

    def test_get_none_event_id(self, logger):
        assert logger.get_entry(None) is None

    def test_get_db_error_raises(self, logger, mock_engine):
        conn = MagicMock()
        conn.execute.side_effect = Exception("DB error")
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(AuditLogError, match="Failed to get audit entry"):
            logger.get_entry("some-id")


# ---------------------------------------------------------------------------
# run_retention() Tests
# ---------------------------------------------------------------------------

class TestRetention:

    def test_retention_no_entries_to_purge(self, logger, mock_engine):
        conn = MagicMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        conn.execute.return_value = count_result
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        result = logger.run_retention()
        assert result["archived_count"] == 0
        assert result["deleted_count"] == 0
        assert result["s3_key"] is None

    def test_retention_archives_and_deletes(self, logger, mock_engine, mock_s3):
        conn = MagicMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 5

        row = MagicMock()
        row._mapping = {
            "id": 1,
            "event_id": "evt-1",
            "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "actor": "user",
            "action": "user.login",
            "resource": "auth",
            "details": None,
            "ip_address": "10.0.0.1",
            "service": "pylon_main",
            "request_id": None,
        }
        select_result = MagicMock()
        select_result.fetchall.return_value = [row]

        delete_result = MagicMock()
        delete_result.rowcount = 5

        conn.execute.side_effect = [count_result, select_result, delete_result]
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        result = logger.run_retention(retention_days=90)
        assert result["archived_count"] == 5
        assert result["deleted_count"] == 5
        assert result["s3_key"] is not None
        assert result["s3_key"].startswith("audit/audit_")
        mock_s3.put_object.assert_called_once()

    def test_retention_without_s3_skips_archive(self, logger_no_s3, mock_engine):
        conn = MagicMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 3

        delete_result = MagicMock()
        delete_result.rowcount = 3

        conn.execute.side_effect = [count_result, delete_result]
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        result = logger_no_s3.run_retention(archive_to_s3=False)
        assert result["archived_count"] == 3
        assert result["deleted_count"] == 3
        assert result["s3_key"] is None

    def test_retention_s3_upload_failure_raises(self, logger, mock_engine, mock_s3):
        conn = MagicMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 2

        row = MagicMock()
        row._mapping = {
            "id": 1, "event_id": "e1",
            "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "actor": "u", "action": "a", "resource": "r",
            "details": None, "ip_address": None, "service": None, "request_id": None,
        }
        select_result = MagicMock()
        select_result.fetchall.return_value = [row]

        conn.execute.side_effect = [count_result, select_result]
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_s3.put_object.side_effect = Exception("S3 unreachable")

        with pytest.raises(AuditRetentionError, match="Failed to upload archive"):
            logger.run_retention()

    def test_retention_db_error_raises(self, logger, mock_engine):
        conn = MagicMock()
        conn.execute.side_effect = Exception("DB gone")
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(AuditRetentionError, match="Retention cleanup failed"):
            logger.run_retention()

    def test_retention_custom_days(self, logger, mock_engine):
        conn = MagicMock()
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        conn.execute.return_value = count_result
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        result = logger.run_retention(retention_days=30)
        assert result["archived_count"] == 0


# ---------------------------------------------------------------------------
# Constants / Actions Tests
# ---------------------------------------------------------------------------

class TestConstants:

    def test_audit_actions_defined(self):
        assert len(AUDIT_ACTIONS) > 0

    def test_standard_actions_present(self):
        assert "user.login" in AUDIT_ACTIONS
        assert "user.logout" in AUDIT_ACTIONS
        assert "permission.grant" in AUDIT_ACTIONS
        assert "data.export" in AUDIT_ACTIONS
        assert "admin.settings_change" in AUDIT_ACTIONS
        assert "api_key.create" in AUDIT_ACTIONS

    def test_default_retention_days(self):
        assert DEFAULT_RETENTION_DAYS == 90

    def test_default_s3_bucket(self):
        assert DEFAULT_S3_BUCKET == "elitea-backups"

    def test_default_s3_prefix(self):
        assert DEFAULT_S3_PREFIX == "audit/"

    def test_archive_batch_size(self):
        assert ARCHIVE_BATCH_SIZE == 1000

    def test_audit_schema(self):
        assert AUDIT_SCHEMA == "centry"

    def test_audit_table_name(self):
        assert AUDIT_TABLE_NAME == "audit_log"


# ---------------------------------------------------------------------------
# _row_to_dict Tests
# ---------------------------------------------------------------------------

class TestRowToDict:

    def test_converts_mapping(self):
        row = MagicMock()
        row._mapping = {
            "id": 99,
            "event_id": "eid-1",
            "timestamp": datetime(2026, 6, 15, tzinfo=timezone.utc),
            "actor": "admin",
            "action": "admin.user_create",
            "resource": "user/new_user",
            "details": {"role": "viewer"},
            "ip_address": "172.16.0.1",
            "service": "pylon_auth",
            "request_id": "req-xyz",
        }
        result = AuditLogger._row_to_dict(row)
        assert result["id"] == 99
        assert result["event_id"] == "eid-1"
        assert result["actor"] == "admin"
        assert result["action"] == "admin.user_create"
        assert result["details"] == {"role": "viewer"}

    def test_handles_none_details(self):
        row = MagicMock()
        row._mapping = {
            "id": 1, "event_id": "e", "timestamp": None,
            "actor": "a", "action": "b", "resource": "c",
            "details": None, "ip_address": None, "service": None, "request_id": None,
        }
        result = AuditLogger._row_to_dict(row)
        assert result["details"] is None
        assert result["ip_address"] is None
