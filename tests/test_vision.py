"""Tests for vision utilities (vision.py)."""

import threading
from unittest.mock import patch, MagicMock
import numpy as np

from vision import get_last_best, find_image, read_number, _thread_local


# ============================================================
# get_last_best / thread-local isolation
# ============================================================

class TestGetLastBest:
    def test_default_zero(self):
        # Clear any existing thread-local state
        if hasattr(_thread_local, 'last_best'):
            del _thread_local.last_best
        assert get_last_best() == 0.0

    def test_set_and_read(self):
        _thread_local.last_best = 0.75
        assert get_last_best() == 0.75

    def test_thread_isolation(self):
        """Two threads writing last_best don't interfere."""
        results = {}
        barrier = threading.Barrier(2)

        def worker(name, value):
            _thread_local.last_best = value
            barrier.wait()  # Ensure both threads have written
            results[name] = get_last_best()

        t1 = threading.Thread(target=worker, args=("t1", 0.9))
        t2 = threading.Thread(target=worker, args=("t2", 0.3))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert results["t1"] == 0.9
        assert results["t2"] == 0.3


class TestFindImageLastBest:
    """Verify find_image stores best score in thread-local."""

    @patch("vision.get_template")
    def test_miss_stores_best_score(self, mock_get_template):
        # Template has a distinct pattern; screen is random noise — won't match
        rng = np.random.RandomState(42)
        template = rng.randint(0, 256, (20, 20, 3), dtype=np.uint8)
        screen = np.zeros((100, 100, 3), dtype=np.uint8)
        mock_get_template.return_value = template

        result = find_image(screen, "test.png", threshold=0.8)
        assert result is None
        best = get_last_best()
        assert isinstance(best, float)
        assert best < 0.8  # Below threshold since template doesn't match

    @patch("vision.get_template")
    def test_none_screen_returns_none(self, mock_get_template):
        result = find_image(None, "test.png")
        assert result is None

    @patch("vision.get_template")
    def test_none_template_returns_none(self, mock_get_template):
        mock_get_template.return_value = None
        screen = np.zeros((100, 100, 3), dtype=np.uint8)
        result = find_image(screen, "missing.png")
        assert result is None


# ============================================================
# read_number
# ============================================================

class TestReadNumber:
    @patch("vision.read_text")
    def test_simple_number(self, mock_read_text):
        mock_read_text.return_value = "42"
        assert read_number(MagicMock()) == 42

    @patch("vision.read_text")
    def test_comma_thousands(self, mock_read_text):
        mock_read_text.return_value = "1,234"
        assert read_number(MagicMock()) == 1234

    @patch("vision.read_text")
    def test_period_thousands(self, mock_read_text):
        mock_read_text.return_value = "1.234"
        assert read_number(MagicMock()) == 1234

    @patch("vision.read_text")
    def test_spaces(self, mock_read_text):
        mock_read_text.return_value = "1 234"
        assert read_number(MagicMock()) == 1234

    @patch("vision.read_text")
    def test_non_numeric(self, mock_read_text):
        mock_read_text.return_value = "abc"
        assert read_number(MagicMock()) is None

    @patch("vision.read_text")
    def test_empty(self, mock_read_text):
        mock_read_text.return_value = ""
        assert read_number(MagicMock()) is None

    @patch("vision.read_text")
    def test_mixed(self, mock_read_text):
        mock_read_text.return_value = "12abc34"
        # After stripping commas/periods/spaces, "12abc34" is not all digits
        assert read_number(MagicMock()) is None
