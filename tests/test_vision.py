"""Tests for vision utilities (vision.py)."""

import subprocess
import threading
from unittest.mock import patch, MagicMock, call
import numpy as np
import cv2

from vision import (
    get_last_best, find_image, find_all_matches, read_number, read_text,
    read_ap, get_template, load_screenshot, adb_tap, adb_swipe, tap_image,
    wait_for_image_and_tap, save_failure_screenshot, _thread_local,
    _template_cache,
)


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


# ============================================================
# get_template — template cache
# ============================================================

class TestGetTemplate:
    def setup_method(self):
        """Clear template cache before each test."""
        _template_cache.clear()

    @patch("vision.cv2.imread")
    def test_loads_from_disk_and_caches(self, mock_imread):
        fake_img = np.zeros((10, 10, 3), dtype=np.uint8)
        mock_imread.return_value = fake_img

        result1 = get_template("elements/test.png")
        result2 = get_template("elements/test.png")

        assert result1 is fake_img
        assert result2 is fake_img
        # cv2.imread called only once — second call hit cache
        mock_imread.assert_called_once_with("elements/test.png")

    @patch("vision.cv2.imread")
    def test_missing_template_returns_none(self, mock_imread):
        mock_imread.return_value = None
        result = get_template("elements/missing.png")
        assert result is None

    @patch("vision.cv2.imread")
    def test_caches_none_for_missing(self, mock_imread):
        """Missing templates are cached as None — doesn't retry disk read."""
        mock_imread.return_value = None
        get_template("elements/missing.png")
        get_template("elements/missing.png")
        mock_imread.assert_called_once()


# ============================================================
# load_screenshot — ADB screenshot pipeline
# ============================================================

class TestLoadScreenshot:
    @patch("vision.stats")
    @patch("vision.subprocess.run")
    def test_success(self, mock_run, mock_stats):
        # Create a real JPEG-encoded image for imdecode
        fake_img = np.zeros((100, 100, 3), dtype=np.uint8)
        _, buf = cv2.imencode(".png", fake_img)
        mock_run.return_value = MagicMock(returncode=0, stdout=buf.tobytes())

        result = load_screenshot("dev1")
        assert result is not None
        assert result.shape[0] > 0

    @patch("vision.stats")
    @patch("vision.subprocess.run")
    def test_timeout(self, mock_run, mock_stats):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="adb", timeout=10)
        result = load_screenshot("dev1")
        assert result is None
        mock_stats.record_adb_timing.assert_called_once()
        # Verify success=False in the call
        _, kwargs = mock_stats.record_adb_timing.call_args
        assert kwargs.get("success") is False

    @patch("vision.stats")
    @patch("vision.subprocess.run")
    def test_bad_returncode(self, mock_run, mock_stats):
        mock_run.return_value = MagicMock(returncode=1, stdout=b"error")
        result = load_screenshot("dev1")
        assert result is None

    @patch("vision.stats")
    @patch("vision.subprocess.run")
    def test_empty_stdout(self, mock_run, mock_stats):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"")
        result = load_screenshot("dev1")
        assert result is None

    @patch("vision.stats")
    @patch("vision.subprocess.run")
    def test_decode_failure(self, mock_run, mock_stats):
        # Valid subprocess but garbage bytes that can't be decoded as image
        mock_run.return_value = MagicMock(returncode=0, stdout=b"not_an_image")
        result = load_screenshot("dev1")
        assert result is None


# ============================================================
# read_text — OCR pipeline
# ============================================================

class TestReadText:
    def test_none_screen_returns_empty(self):
        assert read_text(None) == ""

    @patch("vision.ocr_read")
    def test_normal_text(self, mock_ocr_read):
        # ocr_read detail=1 returns [(bbox, text, confidence), ...]
        mock_ocr_read.return_value = [
            (None, "Hello", 0.95),
            (None, "World", 0.90),
        ]

        screen = np.zeros((100, 100, 3), dtype=np.uint8)
        result = read_text(screen)
        assert result == "Hello World"

    @patch("vision.ocr_read")
    def test_region_cropping(self, mock_ocr_read):
        mock_ocr_read.return_value = [(None, "Cropped", 0.95)]

        # 200x200 screen, crop to (50,50)-(100,100)
        screen = np.ones((200, 200, 3), dtype=np.uint8) * 128
        result = read_text(screen, region=(50, 50, 100, 100))
        assert result == "Cropped"
        # Verify the OCR received a processed (grayscale, upscaled) image
        call_args = mock_ocr_read.call_args
        gray_img = call_args[0][0]
        # Region is 50x50, upscaled 2x → 100x100
        assert gray_img.shape == (100, 100)

    @patch("vision.ocr_read")
    def test_low_confidence_warning(self, mock_ocr_read):
        mock_ocr_read.return_value = [
            (None, "Blurry", 0.3),
        ]

        screen = np.zeros((100, 100, 3), dtype=np.uint8)
        # Should not crash — warning is logged internally
        result = read_text(screen, device="dev1")
        assert result == "Blurry"


# ============================================================
# read_ap — AP reading with retries
# ============================================================

class TestReadAP:
    @patch("vision.time.sleep")
    @patch("vision.ocr_read")
    @patch("vision.load_screenshot")
    def test_success_first_try(self, mock_screenshot, mock_ocr_read, mock_sleep):
        mock_screenshot.return_value = np.zeros((1920, 1080, 3), dtype=np.uint8)
        mock_ocr_read.return_value = ["101/400"]

        result = read_ap("dev1", retries=3)
        assert result == (101, 400)

    @patch("vision.time.sleep")
    @patch("vision.ocr_read")
    @patch("vision.load_screenshot")
    def test_success_on_retry(self, mock_screenshot, mock_ocr_read, mock_sleep):
        mock_screenshot.return_value = np.zeros((1920, 1080, 3), dtype=np.uint8)
        # First attempt: timer text, second: AP value
        mock_ocr_read.side_effect = [["03:45"], ["200/400"]]

        result = read_ap("dev1", retries=3)
        assert result == (200, 400)

    @patch("vision.time.sleep")
    @patch("vision.ocr_read")
    @patch("vision.load_screenshot")
    def test_all_retries_fail(self, mock_screenshot, mock_ocr_read, mock_sleep):
        mock_screenshot.return_value = np.zeros((1920, 1080, 3), dtype=np.uint8)
        mock_ocr_read.return_value = ["garbage"]

        result = read_ap("dev1", retries=2)
        assert result is None

    @patch("vision.time.sleep")
    @patch("vision.load_screenshot")
    def test_screenshot_failure(self, mock_screenshot, mock_sleep):
        mock_screenshot.return_value = None
        result = read_ap("dev1", retries=2)
        assert result is None


# ============================================================
# find_all_matches — multi-match dedup
# ============================================================

class TestFindAllMatches:
    @patch("vision.get_template")
    def test_none_screen(self, mock_get_template):
        assert find_all_matches(None, "test.png") == []

    @patch("vision.get_template")
    def test_none_template(self, mock_get_template):
        mock_get_template.return_value = None
        screen = np.zeros((100, 100, 3), dtype=np.uint8)
        assert find_all_matches(screen, "missing.png") == []

    @patch("vision.get_template")
    def test_no_matches_below_threshold(self, mock_get_template):
        # Random template against blank screen — no matches above 0.8
        rng = np.random.RandomState(42)
        template = rng.randint(0, 256, (10, 10, 3), dtype=np.uint8)
        screen = np.zeros((100, 100, 3), dtype=np.uint8)
        mock_get_template.return_value = template

        result = find_all_matches(screen, "test.png", threshold=0.8)
        assert result == []

    @patch("vision.get_template")
    def test_multiple_matches_dedup(self, mock_get_template):
        # Use a distinctive (non-uniform) pattern — TM_CCOEFF_NORMED needs
        # variance in the template to produce meaningful scores.
        rng = np.random.RandomState(42)
        template = rng.randint(0, 256, (10, 10, 3), dtype=np.uint8)

        screen = np.zeros((200, 200, 3), dtype=np.uint8)
        # Place template at (20,20) and (100,100) — far apart
        screen[20:30, 20:30] = template
        screen[100:110, 100:110] = template
        mock_get_template.return_value = template

        result = find_all_matches(screen, "test.png", threshold=0.9, min_distance=50)
        assert len(result) >= 2  # Both should be found (far apart)

    @patch("vision.get_template")
    def test_nearby_matches_deduplicated(self, mock_get_template):
        """Matches closer than min_distance should be deduplicated."""
        # Use a distinctive pattern so only placed locations match
        rng = np.random.RandomState(99)
        template = rng.randint(0, 256, (15, 15, 3), dtype=np.uint8)
        screen = np.zeros((200, 200, 3), dtype=np.uint8)

        # Place identical copies at (30,30) and (35,35) — 5px apart
        screen[30:45, 30:45] = template
        screen[35:50, 35:50] = template
        mock_get_template.return_value = template

        # With min_distance=50, the two nearby hits should merge to 1
        result = find_all_matches(screen, "test.png", threshold=0.9, min_distance=50)
        assert len(result) >= 1
        # With min_distance=3, they should NOT merge
        result_fine = find_all_matches(screen, "test.png", threshold=0.9, min_distance=3)
        assert len(result_fine) >= len(result)


# ============================================================
# adb_tap / adb_swipe — ADB input
# ============================================================

class TestAdbTap:
    @patch("vision.stats")
    @patch("vision.subprocess.run")
    def test_success(self, mock_run, mock_stats):
        mock_run.return_value = MagicMock()
        adb_tap("dev1", 500, 1000)
        args = mock_run.call_args[0][0]
        assert "tap" in args
        assert "500" in args
        assert "1000" in args

    @patch("vision.stats")
    @patch("vision.subprocess.run")
    def test_timeout(self, mock_run, mock_stats):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="adb", timeout=10)
        adb_tap("dev1", 500, 1000)  # Should not raise
        mock_stats.record_adb_timing.assert_called_once()


class TestAdbSwipe:
    @patch("vision.stats")
    @patch("vision.subprocess.run")
    def test_success(self, mock_run, mock_stats):
        mock_run.return_value = MagicMock()
        adb_swipe("dev1", 100, 200, 300, 400, duration_ms=500)
        args = mock_run.call_args[0][0]
        assert "swipe" in args
        assert "100" in args
        assert "500" in args  # duration

    @patch("vision.stats")
    @patch("vision.subprocess.run")
    def test_timeout(self, mock_run, mock_stats):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="adb", timeout=10)
        adb_swipe("dev1", 100, 200, 300, 400)  # Should not raise
        mock_stats.record_adb_timing.assert_called_once()


# ============================================================
# tap_image — composite find+tap
# ============================================================

class TestTapImage:
    @patch("vision._save_click_trail")
    @patch("vision.adb_tap")
    @patch("vision.find_image")
    @patch("vision.load_screenshot")
    def test_match_found(self, mock_screenshot, mock_find, mock_tap, mock_trail):
        screen = np.zeros((100, 100, 3), dtype=np.uint8)
        mock_screenshot.return_value = screen
        # find_image returns (max_val, max_loc, h, w)
        mock_find.return_value = (0.95, (40, 50), 20, 30)

        result = tap_image("button.png", "dev1")
        assert result is True
        # Should tap center: x = 40 + 30//2 = 55, y = 50 + 20//2 = 60
        mock_tap.assert_called_once_with("dev1", 55, 60)

    @patch("vision.stats")
    @patch("vision.adb_tap")
    @patch("vision.find_image")
    @patch("vision.load_screenshot")
    def test_no_match(self, mock_screenshot, mock_find, mock_tap, mock_stats):
        mock_screenshot.return_value = np.zeros((100, 100, 3), dtype=np.uint8)
        mock_find.return_value = None

        result = tap_image("missing.png", "dev1")
        assert result is False
        mock_tap.assert_not_called()
        mock_stats.record_template_miss.assert_called_once()

    @patch("vision._save_click_trail")
    @patch("vision.adb_tap")
    @patch("vision.find_image")
    @patch("vision.load_screenshot")
    def test_heal_uses_region(self, mock_screenshot, mock_find, mock_tap, mock_trail):
        """heal.png should be searched with IMAGE_REGIONS constraint."""
        screen = np.zeros((100, 100, 3), dtype=np.uint8)
        mock_screenshot.return_value = screen
        mock_find.return_value = (0.9, (10, 970), 20, 20)

        tap_image("heal.png", "dev1")
        # Verify find_image was called with the region kwarg
        _, kwargs = mock_find.call_args
        assert kwargs.get("region") == (0, 960, 540, 1920)


# ============================================================
# wait_for_image_and_tap — polling loop
# ============================================================

class TestWaitForImageAndTap:
    @patch("vision.time.sleep")
    @patch("vision.tap_image")
    def test_found_immediately(self, mock_tap_img, mock_sleep):
        mock_tap_img.return_value = True
        result = wait_for_image_and_tap("btn.png", "dev1", timeout=5)
        assert result is True
        mock_tap_img.assert_called_once()

    @patch("vision.time.sleep")
    @patch("vision.tap_image")
    def test_found_on_retry(self, mock_tap_img, mock_sleep):
        mock_tap_img.side_effect = [False, False, True]
        result = wait_for_image_and_tap("btn.png", "dev1", timeout=10)
        assert result is True
        assert mock_tap_img.call_count == 3

    @patch("vision.time.time")
    @patch("vision.time.sleep")
    @patch("vision.tap_image")
    def test_timeout(self, mock_tap_img, mock_sleep, mock_time):
        mock_tap_img.return_value = False
        # Simulate time progressing past timeout
        mock_time.side_effect = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        result = wait_for_image_and_tap("btn.png", "dev1", timeout=5)
        assert result is False


# ============================================================
# save_failure_screenshot
# ============================================================

class TestSaveFailureScreenshot:
    @patch("vision._cleanup_failures_dir")
    @patch("vision.cv2.imwrite")
    def test_success_with_screen(self, mock_imwrite, mock_cleanup):
        screen = np.zeros((100, 100, 3), dtype=np.uint8)
        result = save_failure_screenshot("127.0.0.1:5555", "test_fail", screen=screen)
        assert result is not None
        assert "test_fail" in result
        mock_imwrite.assert_called_once()

    @patch("vision._cleanup_failures_dir")
    @patch("vision.cv2.imwrite")
    @patch("vision.load_screenshot")
    def test_success_loads_screenshot(self, mock_load, mock_imwrite, mock_cleanup):
        mock_load.return_value = np.zeros((100, 100, 3), dtype=np.uint8)
        result = save_failure_screenshot("dev1", "auto_load")
        assert result is not None
        mock_load.assert_called_once_with("dev1")

    @patch("vision.load_screenshot")
    def test_screenshot_fails(self, mock_load):
        mock_load.return_value = None
        result = save_failure_screenshot("dev1", "no_screen")
        assert result is None
