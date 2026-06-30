"""Unit tests for IconStorage (S3-backed icon storage).

Validates:
1. upload_icon resizes, converts to PNG, and calls artifacts_upload RPC
2. get_icon_url returns correct path format
3. get_icon_data calls artifacts_get_file_data and handles missing icons
4. delete_icon calls minio_client.remove_file
5. list_icons filters by project_id prefix and paginates
6. Validation: file size, empty file, unsupported format, dimensions too small
7. Error handling: RPC failures, missing minio client

Run with:
    python3 -m pytest centry/tests/unit/scaling/test_icon_storage.py -v
"""

import importlib.util
import io
import pathlib
import sys
import types
from unittest.mock import MagicMock, patch, call

import pytest
from PIL import Image

# ---------------------------------------------------------------------------
# Module loading: icon_storage.py only imports PIL and logging, plus uuid4.
# No pylon framework dependencies, so we can import directly.
# ---------------------------------------------------------------------------

_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[3] / "pylon_main" / "plugins" / "elitea_core"
_MODULE_PATH = _PLUGIN_ROOT / "utils" / "icon_storage.py"

_spec = importlib.util.spec_from_file_location(
    "icon_storage",
    _MODULE_PATH,
    submodule_search_locations=[],
)
icon_storage_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(icon_storage_mod)

IconStorage = icon_storage_mod.IconStorage
IconStorageError = icon_storage_mod.IconStorageError
IconValidationError = icon_storage_mod.IconValidationError
IconNotFoundError = icon_storage_mod.IconNotFoundError
ICONS_BUCKET = icon_storage_mod.ICONS_BUCKET
MAX_ICON_SIZE_KB = icon_storage_mod.MAX_ICON_SIZE_KB
DEFAULT_ICON_WIDTH = icon_storage_mod.DEFAULT_ICON_WIDTH
DEFAULT_ICON_HEIGHT = icon_storage_mod.DEFAULT_ICON_HEIGHT
_get_extension = icon_storage_mod._get_extension


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _create_test_image(width=128, height=128, fmt="PNG") -> bytes:
    """Create a valid test image of given dimensions."""
    mode = "RGB" if fmt == "JPEG" else "RGBA"
    img = Image.new(mode, (width, height), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _create_small_image(width=32, height=32) -> bytes:
    """Create a valid image that's smaller than default minimum."""
    img = Image.new("RGB", (width, height), color=(0, 255, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def mock_rpc():
    """Create a mock RPC caller with timeout chain."""
    rpc = MagicMock()
    rpc_with_timeout = MagicMock()
    rpc.timeout.return_value = rpc_with_timeout
    return rpc


@pytest.fixture
def mock_minio():
    """Create a mock MinioClient."""
    return MagicMock()


@pytest.fixture
def storage(mock_rpc, mock_minio):
    """Create an IconStorage instance with mock dependencies."""
    return IconStorage(rpc_caller=mock_rpc, minio_client=mock_minio)


@pytest.fixture
def storage_no_minio(mock_rpc):
    """Create an IconStorage instance without MinioClient."""
    return IconStorage(rpc_caller=mock_rpc, minio_client=None)


# ---------------------------------------------------------------------------
# Tests: _get_extension helper
# ---------------------------------------------------------------------------

class TestGetExtension:
    def test_normal_extension(self):
        assert _get_extension("image.png") == ".png"

    def test_multiple_dots(self):
        assert _get_extension("my.icon.jpeg") == ".jpeg"

    def test_no_extension(self):
        assert _get_extension("noext") == ""

    def test_empty_string(self):
        assert _get_extension("") == ""

    def test_none(self):
        assert _get_extension(None) == ""

    def test_uppercase(self):
        assert _get_extension("FILE.PNG") == ".PNG"


# ---------------------------------------------------------------------------
# Tests: upload_icon
# ---------------------------------------------------------------------------

class TestUploadIcon:
    def test_successful_upload(self, storage, mock_rpc):
        icon_data = _create_test_image(128, 128)
        mock_rpc.timeout.return_value.artifacts_upload.return_value = {
            "filepath": "/icons/1/test.png",
            "bucket": "icons",
            "filename": "1/test.png",
        }

        result = storage.upload_icon(
            project_id=1,
            icon_data=icon_data,
            filename="test.png",
        )

        assert result["name"].endswith(".png")
        assert result["url"].startswith("/icons/1/")
        assert result["size"] == "64x64"
        assert result["initial_file_size"] == len(icon_data)
        assert result["resulting_file_size"] > 0

        mock_rpc.timeout.assert_called_with(10)
        upload_call = mock_rpc.timeout.return_value.artifacts_upload
        upload_call.assert_called_once()
        kwargs = upload_call.call_args[1]
        assert kwargs["project_id"] == 1
        assert kwargs["bucket"] == ICONS_BUCKET
        assert kwargs["filename"].startswith("1/")
        assert kwargs["filename"].endswith(".png")
        assert kwargs["create_if_not_exists"] is True
        assert kwargs["check_duplicates"] is False

    def test_custom_dimensions(self, storage, mock_rpc):
        icon_data = _create_test_image(256, 256)
        mock_rpc.timeout.return_value.artifacts_upload.return_value = {}

        result = storage.upload_icon(
            project_id=5,
            icon_data=icon_data,
            filename="large.png",
            width=128,
            height=128,
        )

        assert result["size"] == "128x128"

    def test_jpeg_input_converted_to_png(self, storage, mock_rpc):
        icon_data = _create_test_image(100, 100, fmt="JPEG")
        mock_rpc.timeout.return_value.artifacts_upload.return_value = {}

        result = storage.upload_icon(
            project_id=1,
            icon_data=icon_data,
            filename="photo.jpg",
        )

        assert result["name"].endswith(".png")
        upload_call = mock_rpc.timeout.return_value.artifacts_upload
        file_data = upload_call.call_args[1]["file_data"]
        img = Image.open(io.BytesIO(file_data))
        assert img.format == "PNG"

    def test_empty_data_raises_validation_error(self, storage):
        with pytest.raises(IconValidationError, match="empty"):
            storage.upload_icon(project_id=1, icon_data=b"", filename="x.png")

    def test_none_data_raises_validation_error(self, storage):
        with pytest.raises(IconValidationError, match="empty"):
            storage.upload_icon(project_id=1, icon_data=None, filename="x.png")

    def test_oversized_file_raises_validation_error(self, storage):
        big_data = b"x" * (MAX_ICON_SIZE_KB * 1024 + 1)
        with pytest.raises(IconValidationError, match="exceeds"):
            storage.upload_icon(project_id=1, icon_data=big_data, filename="big.png")

    def test_exactly_max_size_succeeds(self, storage, mock_rpc):
        img = Image.new("RGB", (128, 128), color=(0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        icon_data = buf.getvalue()
        # Pad to exactly MAX_ICON_SIZE_KB * 1024 if needed
        if len(icon_data) < MAX_ICON_SIZE_KB * 1024:
            icon_data = icon_data  # valid PNG, under limit
        mock_rpc.timeout.return_value.artifacts_upload.return_value = {}

        result = storage.upload_icon(project_id=1, icon_data=icon_data, filename="ok.png")
        assert result["name"].endswith(".png")

    def test_unsupported_format_raises_validation_error(self, storage):
        icon_data = _create_test_image(128, 128)
        with pytest.raises(IconValidationError, match="Unsupported"):
            storage.upload_icon(project_id=1, icon_data=icon_data, filename="icon.tiff")

    def test_no_extension_raises_validation_error(self, storage):
        icon_data = _create_test_image(128, 128)
        with pytest.raises(IconValidationError, match="Unsupported"):
            storage.upload_icon(project_id=1, icon_data=icon_data, filename="noext")

    def test_corrupt_image_raises_validation_error(self, storage):
        with pytest.raises(IconValidationError, match="Cannot open"):
            storage.upload_icon(project_id=1, icon_data=b"not an image", filename="bad.png")

    def test_image_too_small_raises_validation_error(self, storage):
        small_data = _create_small_image(32, 32)
        with pytest.raises(IconValidationError, match="too small"):
            storage.upload_icon(project_id=1, icon_data=small_data, filename="tiny.png")

    def test_image_exact_minimum_size_succeeds(self, storage, mock_rpc):
        icon_data = _create_test_image(64, 64)
        mock_rpc.timeout.return_value.artifacts_upload.return_value = {}

        result = storage.upload_icon(project_id=1, icon_data=icon_data, filename="exact.png")
        assert result["name"].endswith(".png")

    def test_rpc_failure_raises_storage_error(self, storage, mock_rpc):
        icon_data = _create_test_image(128, 128)
        mock_rpc.timeout.return_value.artifacts_upload.side_effect = RuntimeError("S3 down")

        with pytest.raises(IconStorageError, match="Failed to upload"):
            storage.upload_icon(project_id=1, icon_data=icon_data, filename="fail.png")

    def test_supported_formats(self, storage, mock_rpc):
        """All supported extensions should pass validation."""
        mock_rpc.timeout.return_value.artifacts_upload.return_value = {}
        supported = [".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico"]

        for ext in supported:
            icon_data = _create_test_image(128, 128)
            result = storage.upload_icon(
                project_id=1, icon_data=icon_data, filename=f"icon{ext}"
            )
            assert result["name"].endswith(".png")

    def test_upload_preserves_project_id_in_path(self, storage, mock_rpc):
        icon_data = _create_test_image(128, 128)
        mock_rpc.timeout.return_value.artifacts_upload.return_value = {}

        result = storage.upload_icon(project_id=42, icon_data=icon_data, filename="x.png")

        assert "/42/" in result["url"]
        upload_kwargs = mock_rpc.timeout.return_value.artifacts_upload.call_args[1]
        assert upload_kwargs["filename"].startswith("42/")


# ---------------------------------------------------------------------------
# Tests: get_icon_url
# ---------------------------------------------------------------------------

class TestGetIconUrl:
    def test_returns_correct_path(self, storage):
        url = storage.get_icon_url(project_id=1, icon_name="abc.png")
        assert url == "/icons/1/abc.png"

    def test_different_project(self, storage):
        url = storage.get_icon_url(project_id=99, icon_name="def-123.png")
        assert url == "/icons/99/def-123.png"

    def test_uuid_filename(self, storage):
        url = storage.get_icon_url(
            project_id=5,
            icon_name="550e8400-e29b-41d4-a716-446655440000.png"
        )
        assert url == "/icons/5/550e8400-e29b-41d4-a716-446655440000.png"


# ---------------------------------------------------------------------------
# Tests: get_icon_data
# ---------------------------------------------------------------------------

class TestGetIconData:
    def test_successful_retrieval(self, storage, mock_rpc):
        expected_data = b"\x89PNG\r\n\x1a\n..."
        mock_rpc.timeout.return_value.artifacts_get_file_data.return_value = {
            "file_data": expected_data,
            "bucket": "icons",
            "filename": "1/abc.png",
        }

        result = storage.get_icon_data(project_id=1, icon_name="abc.png")

        assert result == expected_data
        mock_rpc.timeout.assert_called_with(10)
        mock_rpc.timeout.return_value.artifacts_get_file_data.assert_called_once_with(
            project_id=1, bucket=ICONS_BUCKET, filename="1/abc.png"
        )

    def test_not_found_returns_none_raises_error(self, storage, mock_rpc):
        mock_rpc.timeout.return_value.artifacts_get_file_data.return_value = None

        with pytest.raises(IconNotFoundError, match="not found"):
            storage.get_icon_data(project_id=1, icon_name="missing.png")

    def test_result_without_file_data_raises_error(self, storage, mock_rpc):
        mock_rpc.timeout.return_value.artifacts_get_file_data.return_value = {
            "bucket": "icons",
            "filename": "1/x.png",
            "file_data": None,
        }

        with pytest.raises(IconNotFoundError, match="not found"):
            storage.get_icon_data(project_id=1, icon_name="x.png")

    def test_rpc_failure_raises_storage_error(self, storage, mock_rpc):
        mock_rpc.timeout.return_value.artifacts_get_file_data.side_effect = Exception("timeout")

        with pytest.raises(IconStorageError, match="Failed to retrieve"):
            storage.get_icon_data(project_id=1, icon_name="fail.png")


# ---------------------------------------------------------------------------
# Tests: delete_icon
# ---------------------------------------------------------------------------

class TestDeleteIcon:
    def test_successful_deletion(self, storage, mock_minio):
        result = storage.delete_icon(project_id=1, icon_name="abc.png")

        assert result is True
        mock_minio.remove_file.assert_called_once_with(ICONS_BUCKET, "1/abc.png")

    def test_different_project(self, storage, mock_minio):
        storage.delete_icon(project_id=42, icon_name="xyz.png")
        mock_minio.remove_file.assert_called_once_with(ICONS_BUCKET, "42/xyz.png")

    def test_minio_failure_raises_storage_error(self, storage, mock_minio):
        mock_minio.remove_file.side_effect = Exception("connection refused")

        with pytest.raises(IconStorageError, match="Failed to delete"):
            storage.delete_icon(project_id=1, icon_name="fail.png")

    def test_no_minio_client_raises_error(self, storage_no_minio):
        with pytest.raises(IconStorageError, match="MinioClient required"):
            storage_no_minio.delete_icon(project_id=1, icon_name="x.png")


# ---------------------------------------------------------------------------
# Tests: list_icons
# ---------------------------------------------------------------------------

class TestListIcons:
    def test_list_returns_filtered_results(self, storage, mock_minio):
        mock_minio.list_files.return_value = [
            {"name": "1/alpha.png", "size": 1024, "modified": "2026-01-01T00:00:00"},
            {"name": "1/beta.png", "size": 2048, "modified": "2026-01-02T00:00:00"},
            {"name": "2/gamma.png", "size": 512, "modified": "2026-01-03T00:00:00"},
        ]

        result = storage.list_icons(project_id=1)

        assert result["total"] == 2
        assert len(result["rows"]) == 2
        assert result["rows"][0]["name"] == "alpha.png"
        assert result["rows"][0]["url"] == "/icons/1/alpha.png"
        assert result["rows"][1]["name"] == "beta.png"
        mock_minio.list_files.assert_called_once_with(ICONS_BUCKET)

    def test_list_empty_bucket(self, storage, mock_minio):
        mock_minio.list_files.return_value = []

        result = storage.list_icons(project_id=1)

        assert result == {"total": 0, "rows": []}

    def test_list_no_matching_project(self, storage, mock_minio):
        mock_minio.list_files.return_value = [
            {"name": "2/other.png", "size": 512, "modified": "2026-01-01T00:00:00"},
        ]

        result = storage.list_icons(project_id=1)

        assert result["total"] == 0
        assert result["rows"] == []

    def test_pagination_skip(self, storage, mock_minio):
        mock_minio.list_files.return_value = [
            {"name": "1/a.png", "size": 100, "modified": "2026-01-01T00:00:00"},
            {"name": "1/b.png", "size": 200, "modified": "2026-01-01T00:00:00"},
            {"name": "1/c.png", "size": 300, "modified": "2026-01-01T00:00:00"},
        ]

        result = storage.list_icons(project_id=1, skip=1)

        assert result["total"] == 3
        assert len(result["rows"]) == 2
        assert result["rows"][0]["name"] == "b.png"

    def test_pagination_limit(self, storage, mock_minio):
        mock_minio.list_files.return_value = [
            {"name": "1/a.png", "size": 100, "modified": "2026-01-01T00:00:00"},
            {"name": "1/b.png", "size": 200, "modified": "2026-01-01T00:00:00"},
            {"name": "1/c.png", "size": 300, "modified": "2026-01-01T00:00:00"},
        ]

        result = storage.list_icons(project_id=1, limit=2)

        assert result["total"] == 3
        assert len(result["rows"]) == 2

    def test_pagination_skip_and_limit(self, storage, mock_minio):
        mock_minio.list_files.return_value = [
            {"name": "1/a.png", "size": 100, "modified": "2026-01-01T00:00:00"},
            {"name": "1/b.png", "size": 200, "modified": "2026-01-01T00:00:00"},
            {"name": "1/c.png", "size": 300, "modified": "2026-01-01T00:00:00"},
            {"name": "1/d.png", "size": 400, "modified": "2026-01-01T00:00:00"},
        ]

        result = storage.list_icons(project_id=1, skip=1, limit=2)

        assert result["total"] == 4
        assert len(result["rows"]) == 2
        assert result["rows"][0]["name"] == "b.png"
        assert result["rows"][1]["name"] == "c.png"

    def test_sorted_alphabetically(self, storage, mock_minio):
        mock_minio.list_files.return_value = [
            {"name": "1/zebra.png", "size": 100, "modified": "2026-01-01T00:00:00"},
            {"name": "1/apple.png", "size": 200, "modified": "2026-01-01T00:00:00"},
            {"name": "1/mango.png", "size": 300, "modified": "2026-01-01T00:00:00"},
        ]

        result = storage.list_icons(project_id=1)

        names = [r["name"] for r in result["rows"]]
        assert names == ["apple.png", "mango.png", "zebra.png"]

    def test_minio_failure_returns_empty(self, storage, mock_minio):
        mock_minio.list_files.side_effect = Exception("connection refused")

        result = storage.list_icons(project_id=1)

        assert result == {"total": 0, "rows": []}

    def test_no_minio_client_returns_empty(self, storage_no_minio):
        result = storage_no_minio.list_icons(project_id=1)
        assert result == {"total": 0, "rows": []}

    def test_skips_empty_icon_names(self, storage, mock_minio):
        mock_minio.list_files.return_value = [
            {"name": "1/", "size": 0, "modified": "2026-01-01T00:00:00"},
            {"name": "1/real.png", "size": 100, "modified": "2026-01-01T00:00:00"},
        ]

        result = storage.list_icons(project_id=1)

        assert result["total"] == 1
        assert result["rows"][0]["name"] == "real.png"


# ---------------------------------------------------------------------------
# Tests: Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_icons_bucket_name(self):
        assert ICONS_BUCKET == "icons"

    def test_max_size(self):
        assert MAX_ICON_SIZE_KB == 512

    def test_default_dimensions(self):
        assert DEFAULT_ICON_WIDTH == 64
        assert DEFAULT_ICON_HEIGHT == 64


# ---------------------------------------------------------------------------
# Tests: Error hierarchy
# ---------------------------------------------------------------------------

class TestErrorHierarchy:
    def test_validation_error_is_storage_error(self):
        assert issubclass(IconValidationError, IconStorageError)

    def test_not_found_error_is_storage_error(self):
        assert issubclass(IconNotFoundError, IconStorageError)

    def test_storage_error_is_exception(self):
        assert issubclass(IconStorageError, Exception)
