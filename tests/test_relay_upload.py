"""Tests for relay_server.py — upload and admin endpoints.

Requires aiohttp to import relay_server.  All tests are skipped when aiohttp
is not installed (it lives on the droplet, not the dev machine).
"""

import os
import shutil
import tempfile

import pytest

try:
    from aiohttp import web  # noqa: F401
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

pytestmark = pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
class TestFormatSize:
    def test_bytes(self):
        from relay.relay_server import _format_size
        assert _format_size(500) == "500 B"

    def test_kilobytes(self):
        from relay.relay_server import _format_size
        assert "KB" in _format_size(5000)

    def test_megabytes(self):
        from relay.relay_server import _format_size
        assert "MB" in _format_size(5_000_000)


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
class TestSafeBotName:
    def test_valid_name(self):
        from relay.relay_server import _safe_bot_name
        assert _safe_bot_name("abc123def0") == "abc123def0"

    def test_slash_rejected(self):
        from relay.relay_server import _safe_bot_name
        with pytest.raises(web.HTTPBadRequest):
            _safe_bot_name("abc/def")

    def test_dotdot_rejected(self):
        from relay.relay_server import _safe_bot_name
        with pytest.raises(web.HTTPBadRequest):
            _safe_bot_name("abc..def")

    def test_empty_rejected(self):
        from relay.relay_server import _safe_bot_name
        with pytest.raises(web.HTTPBadRequest):
            _safe_bot_name("")

    def test_whitespace_only_rejected(self):
        from relay.relay_server import _safe_bot_name
        with pytest.raises(web.HTTPBadRequest):
            _safe_bot_name("   ")


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
class TestPruneUploads:
    def test_prune_keeps_newest(self):
        from relay.relay_server import _prune_uploads, MAX_UPLOADS_PER_BOT
        tmpdir = tempfile.mkdtemp()
        try:
            for i in range(MAX_UPLOADS_PER_BOT + 3):
                path = os.path.join(tmpdir, f"bugreport_{i:04d}.zip")
                with open(path, "w") as f:
                    f.write("x")
                os.utime(path, (i, i))
            _prune_uploads(tmpdir)
            remaining = [f for f in os.listdir(tmpdir) if f.endswith(".zip")]
            assert len(remaining) == MAX_UPLOADS_PER_BOT
            for f in remaining:
                num = int(f.split("_")[1].split(".")[0])
                assert num >= 3
        finally:
            shutil.rmtree(tmpdir)

    def test_prune_noop_under_limit(self):
        from relay.relay_server import _prune_uploads
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "bugreport_0001.zip")
            with open(path, "w") as f:
                f.write("x")
            _prune_uploads(tmpdir)
            assert os.path.isfile(path)
        finally:
            shutil.rmtree(tmpdir)

    def test_prune_missing_dir(self):
        from relay.relay_server import _prune_uploads
        _prune_uploads("/nonexistent/path")  # should not raise
