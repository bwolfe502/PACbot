"""Tests for combat actions (actions/combat.py).

Covers: _check_dead, _find_green_pixel, _detect_player_at_eg, teleport.
All ADB and vision calls are mocked — no emulator needed.
"""

import json
import os
import time
import numpy as np
from unittest.mock import patch, MagicMock, call

import config
from config import Screen
from actions.combat import (
    _check_dead, _find_green_pixel, _detect_player_at_eg, teleport,
    _check_green_at_current_position,
    _strategy_random_pan, _strategy_big_pan, _strategy_edge_pan,
    _strategy_territory_guided, _COMPASS_DIRS,
    _run_trial, _print_benchmark_summary, teleport_benchmark,
    TeleportAttempt, TeleportTrial,
    _STRATEGIES, _DEFAULT_STRATEGIES,
)


# ============================================================
# _check_dead
# ============================================================

class TestCheckDead:
    def test_returns_true_when_dead_found(self, mock_device):
        """High-confidence dead.png match → tap it, return True."""
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        dead_img = np.zeros((50, 50, 3), dtype=np.uint8)
        with patch("actions.combat.cv2.matchTemplate") as mock_match, \
             patch("actions.combat.cv2.minMaxLoc") as mock_minmax, \
             patch("actions.combat.logged_tap") as mock_tap, \
             patch("actions.combat.time.sleep"):
            result_arr = np.zeros((1871, 1031), dtype=np.float32)
            mock_match.return_value = result_arr
            mock_minmax.return_value = (0, 0.98, (0, 0), (100, 200))

            result = _check_dead(screen, dead_img, mock_device)

        assert result is True
        mock_tap.assert_called_once_with(mock_device, 125, 225, "tp_dead_click")

    def test_returns_false_when_below_threshold(self, mock_device):
        """Low-confidence match → return False, no tap."""
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        dead_img = np.zeros((50, 50, 3), dtype=np.uint8)
        with patch("actions.combat.cv2.matchTemplate") as mock_match, \
             patch("actions.combat.cv2.minMaxLoc") as mock_minmax, \
             patch("actions.combat.logged_tap") as mock_tap:
            result_arr = np.zeros((1871, 1031), dtype=np.float32)
            mock_match.return_value = result_arr
            mock_minmax.return_value = (0, 0.50, (0, 0), (100, 200))

            result = _check_dead(screen, dead_img, mock_device)

        assert result is False
        mock_tap.assert_not_called()

    def test_returns_false_when_screen_none(self, mock_device):
        assert _check_dead(None, np.zeros((10, 10, 3), dtype=np.uint8), mock_device) is False

    def test_returns_false_when_dead_img_none(self, mock_device):
        assert _check_dead(np.zeros((100, 100, 3), dtype=np.uint8), None, mock_device) is False

    def test_returns_false_when_both_none(self, mock_device):
        assert _check_dead(None, None, mock_device) is False


# ============================================================
# _find_green_pixel
# ============================================================

class TestFindGreenPixel:
    """Tests for _find_green_pixel — scans y:100-800, x:50-1000 for the green
    teleport circle.  Requires >= 20 matching pixels (stride-5 sampled) to
    avoid false positives from small green UI elements.
    """

    def test_returns_true_when_green_circle_present(self):
        """Large green arc in the scan region should be detected."""
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        # Simulate a section of the green circle — 30x200 px arc
        screen[350:380, 200:400] = [0, 255, 0]

        assert _find_green_pixel(screen, (0, 255, 0))

    def test_returns_false_when_no_green(self):
        """All-black screen should not match."""
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        assert not _find_green_pixel(screen, (0, 255, 0))

    def test_returns_true_with_tolerance(self):
        """Near-green pixels within tolerance should still match."""
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        # Close to (0, 255, 0) but not exact — within default tolerance=20
        screen[300:330, 300:500] = [10, 240, 15]

        assert _find_green_pixel(screen, (0, 255, 0), tolerance=20)

    def test_returns_false_outside_tolerance(self):
        """Pixels too far from target color should not match."""
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        screen[300:330, 300:500] = [0, 0, 255]  # red, not green

        assert not _find_green_pixel(screen, (0, 255, 0), tolerance=20)

    def test_ignores_green_outside_scan_region(self):
        """Green pixels below y=800 (UI area) should not trigger detection."""
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        # Place large green block in the bottom UI area (below scan region)
        screen[900:1000, 200:400] = [0, 255, 0]

        assert not _find_green_pixel(screen, (0, 255, 0))

    def test_requires_minimum_pixel_count(self):
        """A few scattered green pixels should not trigger (< 20 threshold)."""
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        # Place only 3 green pixels on the stride-5 grid — well under 20
        screen[100, 50] = [0, 255, 0]
        screen[200, 100] = [0, 255, 0]
        screen[300, 150] = [0, 255, 0]

        assert not _find_green_pixel(screen, (0, 255, 0))

    def test_detects_circle_at_various_positions(self):
        """Green circle anywhere in the scan region should be detected."""
        for y_start in [150, 400, 650]:
            screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
            screen[y_start:y_start+30, 100:300] = [0, 255, 0]
            assert _find_green_pixel(screen, (0, 255, 0)), \
                f"Failed to detect circle at y={y_start}"


# ============================================================
# _detect_player_at_eg
# ============================================================

class TestDetectPlayerAtEg:
    def test_returns_true_when_both_colors_present(self):
        """Blue name + gold tag pixels → player detected."""
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        # Place blue name pixels (BGR: 255, 150, 66) near center
        screen[500:510, 500:520] = [255, 150, 66]  # 20+ blue pixels
        # Place gold tag pixels (BGR: 115, 215, 255)
        screen[520:525, 500:510] = [115, 215, 255]  # 10+ gold pixels

        result = _detect_player_at_eg(screen, 510, 510, box_size=200)
        assert result

    def test_returns_false_when_only_blue(self):
        """Only blue pixels, no gold → not a player (could be anything)."""
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        screen[500:510, 500:520] = [255, 150, 66]

        result = _detect_player_at_eg(screen, 510, 510, box_size=200)
        assert not result

    def test_returns_false_when_only_gold(self):
        """Only gold pixels, no blue → not a player."""
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        screen[500:510, 500:520] = [115, 215, 255]

        result = _detect_player_at_eg(screen, 510, 510, box_size=200)
        assert not result

    def test_returns_false_on_empty_screen(self):
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        assert not _detect_player_at_eg(screen, 540, 960)


# ============================================================
# teleport — full function tests
# ============================================================

def _make_time_counter(step=0.1):
    """Return a callable that increments by `step` each call. Never runs out."""
    state = [0.0]
    def fake_time():
        val = state[0]
        state[0] += step
        return val
    return fake_time


class TestTeleport:
    """Tests for the teleport() function.

    _check_dead is mocked directly for most tests since it's already
    unit-tested above. This avoids the zeros-screen-matches-zeros-template
    false positive from cv2.matchTemplate.

    time.time is replaced with an auto-incrementing counter to avoid
    StopIteration from fixed side_effect lists (the @timed_action
    decorator also calls time.time at entry/exit).
    """

    @patch("actions.combat._check_dead", return_value=False)
    @patch("actions.combat.save_failure_screenshot")
    @patch("actions.combat.find_image")
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.time.time")
    @patch("actions.combat.adb_swipe")
    @patch("actions.combat.logged_tap")
    @patch("actions.combat.load_screenshot")
    @patch("actions.combat.get_template")
    @patch("actions.combat.check_screen", return_value=Screen.MAP)
    @patch("actions.combat.heal_all")
    @patch("actions.combat.all_troops_home", return_value=True)
    @patch("actions.combat.clear_click_trail")
    def test_happy_path_green_found_first_attempt(
        self, mock_clear, mock_troops_home, mock_heal, mock_check,
        mock_template, mock_screenshot, mock_tap, mock_swipe,
        mock_time, mock_sleep, mock_find, mock_save_fail, mock_dead,
        mock_device
    ):
        """Green pixel found on first attempt → tap confirm → return True."""
        config.AUTO_HEAL_ENABLED = True
        mock_template.return_value = np.zeros((50, 50, 3), dtype=np.uint8)
        mock_time.side_effect = _make_time_counter(step=0.1)

        # Screen with green in the detection region
        green_screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        green_screen[624:724, 471:571] = [0, 255, 0]
        mock_screenshot.return_value = green_screen

        result = teleport(mock_device)

        assert result is True
        mock_troops_home.assert_called_once_with(mock_device)
        mock_heal.assert_called_once_with(mock_device)

    @patch("actions.combat.all_troops_home", return_value=False)
    def test_returns_false_when_troops_deployed(self, mock_troops, mock_device):
        """Troops not home → return False immediately."""
        result = teleport(mock_device)
        assert result is False

    @patch("actions.combat.check_screen", return_value=Screen.BATTLE_LIST)
    @patch("actions.combat.heal_all")
    @patch("actions.combat.all_troops_home", return_value=True)
    def test_returns_false_when_not_on_map(self, mock_troops, mock_heal,
                                            mock_check, mock_device):
        """Not on MAP screen → return False."""
        config.AUTO_HEAL_ENABLED = False
        result = teleport(mock_device)
        assert result is False

    @patch("actions.combat.check_screen", return_value=Screen.MAP)
    @patch("actions.combat.heal_all")
    @patch("actions.combat.all_troops_home", return_value=True)
    def test_heal_all_called_when_enabled(self, mock_troops, mock_heal,
                                           mock_check, mock_device):
        """AUTO_HEAL_ENABLED → heal_all called before screen check."""
        config.AUTO_HEAL_ENABLED = True
        # Let it fail at screen check stage (we just want to verify heal was called)
        mock_check.return_value = Screen.BATTLE_LIST
        teleport(mock_device)
        mock_heal.assert_called_once_with(mock_device)

    @patch("actions.combat.check_screen", return_value=Screen.MAP)
    @patch("actions.combat.heal_all")
    @patch("actions.combat.all_troops_home", return_value=True)
    def test_heal_all_not_called_when_disabled(self, mock_troops, mock_heal,
                                                mock_check, mock_device):
        """AUTO_HEAL_ENABLED=False → heal_all not called."""
        config.AUTO_HEAL_ENABLED = False
        mock_check.return_value = Screen.BATTLE_LIST
        teleport(mock_device)
        mock_heal.assert_not_called()

    @patch("actions.combat._check_dead", return_value=False)
    @patch("actions.combat.save_failure_screenshot")
    @patch("actions.combat.find_image", return_value=None)
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.time.time")
    @patch("actions.combat.adb_swipe")
    @patch("actions.combat.logged_tap")
    @patch("actions.combat.load_screenshot")
    @patch("actions.combat.get_template")
    @patch("actions.combat.check_screen", return_value=Screen.MAP)
    @patch("actions.combat.heal_all")
    @patch("actions.combat.all_troops_home", return_value=True)
    @patch("actions.combat.clear_click_trail")
    def test_timeout_after_max_attempts(
        self, mock_clear, mock_troops_home, mock_heal, mock_check,
        mock_template, mock_screenshot, mock_tap, mock_swipe,
        mock_time, mock_sleep, mock_find, mock_save_fail, mock_dead,
        mock_device
    ):
        """No green found after max_attempts → failure screenshot → return False."""
        config.AUTO_HEAL_ENABLED = False
        mock_template.return_value = np.zeros((50, 50, 3), dtype=np.uint8)

        # All-black screen — no green pixels
        mock_screenshot.return_value = np.zeros((1920, 1080, 3), dtype=np.uint8)

        # Step=0.5 means each time.time() call advances 0.5s.
        # 15 attempts × ~6 calls per attempt = ~90 calls × 0.5s = 45s < 90s timeout,
        # so all 15 attempts run before the time limit.
        mock_time.side_effect = _make_time_counter(step=0.5)

        result = teleport(mock_device)

        assert result is False
        mock_save_fail.assert_called_once_with(mock_device, "teleport_timeout")

    @patch("actions.combat.save_failure_screenshot")
    @patch("actions.combat.find_image", return_value=None)
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.time.time")
    @patch("actions.combat.adb_swipe")
    @patch("actions.combat.logged_tap")
    @patch("actions.combat.load_screenshot")
    @patch("actions.combat.get_template")
    @patch("actions.combat.check_screen", return_value=Screen.MAP)
    @patch("actions.combat.heal_all")
    @patch("actions.combat.all_troops_home", return_value=True)
    @patch("actions.combat.clear_click_trail")
    def test_dead_detected_mid_loop_returns_false(
        self, mock_clear, mock_troops_home, mock_heal, mock_check,
        mock_template, mock_screenshot, mock_tap, mock_swipe,
        mock_time, mock_sleep, mock_find, mock_save_fail, mock_device
    ):
        """dead.png found during the search loop → return False."""
        config.AUTO_HEAL_ENABLED = False
        mock_template.return_value = np.zeros((50, 50, 3), dtype=np.uint8)
        mock_screenshot.return_value = np.zeros((1920, 1080, 3), dtype=np.uint8)
        mock_time.side_effect = _make_time_counter(step=0.1)

        # _check_dead: first call (pre-loop) returns False, second (in loop) True
        with patch("actions.combat._check_dead") as mock_dead:
            mock_dead.side_effect = [False, True]
            result = teleport(mock_device)

        assert result is False

    @patch("actions.combat._check_dead", return_value=False)
    @patch("actions.combat.save_failure_screenshot")
    @patch("actions.combat.find_image", return_value=None)
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.time.time")
    @patch("actions.combat.adb_swipe")
    @patch("actions.combat.logged_tap")
    @patch("actions.combat.load_screenshot")
    @patch("actions.combat.get_template")
    @patch("actions.combat.check_screen", return_value=Screen.MAP)
    @patch("actions.combat.heal_all")
    @patch("actions.combat.all_troops_home", return_value=True)
    @patch("actions.combat.clear_click_trail")
    def test_green_found_on_third_attempt(
        self, mock_clear, mock_troops_home, mock_heal, mock_check,
        mock_template, mock_screenshot, mock_tap, mock_swipe,
        mock_time, mock_sleep, mock_find, mock_save_fail, mock_dead,
        mock_device
    ):
        """Green not found on first two attempts, found on third → return True."""
        config.AUTO_HEAL_ENABLED = False
        mock_template.return_value = np.zeros((50, 50, 3), dtype=np.uint8)
        mock_time.side_effect = _make_time_counter(step=0.1)

        black_screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        green_screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        green_screen[624:724, 471:571] = [0, 255, 0]

        # Return black for first ~10 screenshots, then green
        ss_calls = [0]
        def ss_side_effect(device):
            ss_calls[0] += 1
            return black_screen if ss_calls[0] <= 10 else green_screen
        mock_screenshot.side_effect = ss_side_effect

        result = teleport(mock_device)

        assert result is True

    @patch("actions.combat._check_dead", return_value=False)
    @patch("actions.combat.save_failure_screenshot")
    @patch("actions.combat.find_image", return_value=None)
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.time.time")
    @patch("actions.combat.adb_swipe")
    @patch("actions.combat.logged_tap")
    @patch("actions.combat.load_screenshot")
    @patch("actions.combat.get_template")
    @patch("actions.combat.check_screen", return_value=Screen.MAP)
    @patch("actions.combat.heal_all")
    @patch("actions.combat.all_troops_home", return_value=True)
    @patch("actions.combat.clear_click_trail")
    def test_timeout_by_elapsed_time(
        self, mock_clear, mock_troops_home, mock_heal, mock_check,
        mock_template, mock_screenshot, mock_tap, mock_swipe,
        mock_time, mock_sleep, mock_find, mock_save_fail, mock_dead,
        mock_device
    ):
        """90-second timeout fires before max_attempts reached → return False."""
        config.AUTO_HEAL_ENABLED = False
        mock_template.return_value = np.zeros((50, 50, 3), dtype=np.uint8)
        mock_screenshot.return_value = np.zeros((1920, 1080, 3), dtype=np.uint8)

        # Large step = time advances fast, so 90s limit is hit before 15 attempts
        mock_time.side_effect = _make_time_counter(step=10.0)

        result = teleport(mock_device)

        assert result is False
        mock_save_fail.assert_called_once_with(mock_device, "teleport_timeout")

    @patch("actions.combat._check_dead", return_value=False)
    @patch("actions.combat.save_failure_screenshot")
    @patch("actions.combat.find_image")
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.time.time")
    @patch("actions.combat.adb_swipe")
    @patch("actions.combat.logged_tap")
    @patch("actions.combat.load_screenshot")
    @patch("actions.combat.get_template")
    @patch("actions.combat.check_screen", return_value=Screen.MAP)
    @patch("actions.combat.heal_all")
    @patch("actions.combat.all_troops_home", return_value=True)
    @patch("actions.combat.clear_click_trail")
    def test_cancel_button_tapped_on_failure(
        self, mock_clear, mock_troops_home, mock_heal, mock_check,
        mock_template, mock_screenshot, mock_tap, mock_swipe,
        mock_time, mock_sleep, mock_find, mock_save_fail, mock_dead,
        mock_device
    ):
        """When green not found and cancel.png visible, tap it."""
        config.AUTO_HEAL_ENABLED = False
        mock_template.return_value = np.zeros((50, 50, 3), dtype=np.uint8)
        mock_screenshot.return_value = np.zeros((1920, 1080, 3), dtype=np.uint8)

        # find_image returns cancel.png match: (score, (x, y), h, w)
        cancel_match = (0.9, (300, 1600), 50, 120)
        mock_find.return_value = cancel_match

        # Step=0.5 so the inner green-check loop (3s budget) actually runs
        mock_time.side_effect = _make_time_counter(step=0.5)

        result = teleport(mock_device)

        assert result is False
        # Verify cancel was tapped: x=300+120//2=360, y=1600+50//2=1625
        cancel_calls = [c for c in mock_tap.call_args_list
                        if len(c.args) >= 4 and c.args[3] == "tp_cancel"]
        assert len(cancel_calls) >= 1

    @patch("actions.combat._check_dead", return_value=False)
    @patch("actions.combat.save_failure_screenshot")
    @patch("actions.combat.find_image", return_value=None)
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.time.time")
    @patch("actions.combat.adb_swipe")
    @patch("actions.combat.logged_tap")
    @patch("actions.combat.load_screenshot", return_value=None)
    @patch("actions.combat.get_template", return_value=None)
    @patch("actions.combat.check_screen", return_value=Screen.MAP)
    @patch("actions.combat.heal_all")
    @patch("actions.combat.all_troops_home", return_value=True)
    @patch("actions.combat.clear_click_trail")
    def test_handles_none_screenshot_gracefully(
        self, mock_clear, mock_troops_home, mock_heal, mock_check,
        mock_template, mock_screenshot, mock_tap, mock_swipe,
        mock_time, mock_sleep, mock_find, mock_save_fail, mock_dead,
        mock_device
    ):
        """load_screenshot returning None mid-loop should not crash."""
        config.AUTO_HEAL_ENABLED = False
        mock_time.side_effect = _make_time_counter(step=10.0)

        result = teleport(mock_device)
        assert result is False


# ============================================================
# _check_green_at_current_position
# ============================================================

class TestCheckGreenAtCurrentPosition:
    """Tests for the extracted helper that long-presses, taps TELEPORT,
    and polls for the green boundary circle."""

    @patch("actions.combat.save_failure_screenshot", return_value="/tmp/green.png")
    @patch("actions.combat.find_image")
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.time.time")
    @patch("actions.combat.logged_tap")
    @patch("actions.combat.adb_swipe")
    @patch("actions.combat.load_screenshot")
    @patch("actions.combat._check_dead", return_value=False)
    def test_green_found_first_check(
        self, mock_dead, mock_screenshot, mock_swipe, mock_tap,
        mock_time, mock_sleep, mock_find, mock_save, mock_device
    ):
        """Green circle on first poll → returns (True, path, elapsed)."""
        mock_time.side_effect = _make_time_counter(step=0.1)
        green_screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        green_screen[350:380, 200:400] = [0, 255, 0]
        mock_screenshot.return_value = green_screen

        result, ss_path, elapsed = _check_green_at_current_position(
            mock_device, np.zeros((50, 50, 3), dtype=np.uint8))

        assert result is True
        assert ss_path == "/tmp/green.png"
        # Long-press opens context menu
        mock_swipe.assert_called_once_with(mock_device, 540, 1400, 540, 1400, 1000)
        # TELEPORT button tapped
        mock_tap.assert_called_once_with(mock_device, 780, 1400, "tp_search_btn")

    @patch("actions.combat.save_failure_screenshot")
    @patch("actions.combat.find_image")
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.time.time")
    @patch("actions.combat.logged_tap")
    @patch("actions.combat.adb_swipe")
    @patch("actions.combat.load_screenshot")
    @patch("actions.combat._check_dead", return_value=False)
    def test_no_green_cancel_visible(
        self, mock_dead, mock_screenshot, mock_swipe, mock_tap,
        mock_time, mock_sleep, mock_find, mock_save, mock_device
    ):
        """No green found, cancel.png visible → taps cancel, returns (False, None, elapsed)."""
        mock_time.side_effect = _make_time_counter(step=1.5)
        mock_screenshot.return_value = np.zeros((1920, 1080, 3), dtype=np.uint8)
        # find_image returns cancel match: (score, (x, y), h, w)
        mock_find.return_value = (0.9, (300, 1600), 50, 120)

        result, ss_path, elapsed = _check_green_at_current_position(
            mock_device, np.zeros((50, 50, 3), dtype=np.uint8))

        assert result is False
        assert ss_path is None
        # Verify cancel was tapped: x=300+60=360, y=1600+25=1625
        cancel_calls = [c for c in mock_tap.call_args_list
                        if len(c.args) >= 4 and c.args[3] == "tp_cancel"]
        assert len(cancel_calls) == 1
        assert cancel_calls[0].args[1:3] == (360, 1625)

    @patch("actions.combat.save_failure_screenshot")
    @patch("actions.combat.find_image", return_value=None)
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.time.time")
    @patch("actions.combat.logged_tap")
    @patch("actions.combat.adb_swipe")
    @patch("actions.combat.load_screenshot")
    @patch("actions.combat._check_dead", return_value=False)
    def test_no_green_no_cancel(
        self, mock_dead, mock_screenshot, mock_swipe, mock_tap,
        mock_time, mock_sleep, mock_find, mock_save, mock_device
    ):
        """No green found, no cancel button → returns (False, None, elapsed)."""
        mock_time.side_effect = _make_time_counter(step=1.5)
        mock_screenshot.return_value = np.zeros((1920, 1080, 3), dtype=np.uint8)

        result, ss_path, elapsed = _check_green_at_current_position(
            mock_device, np.zeros((50, 50, 3), dtype=np.uint8))

        assert result is False
        assert ss_path is None
        # No tp_cancel tap since cancel.png wasn't found
        cancel_calls = [c for c in mock_tap.call_args_list
                        if len(c.args) >= 4 and c.args[3] == "tp_cancel"]
        assert len(cancel_calls) == 0

    @patch("actions.combat.save_failure_screenshot")
    @patch("actions.combat.find_image")
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.time.time")
    @patch("actions.combat.logged_tap")
    @patch("actions.combat.adb_swipe")
    @patch("actions.combat.load_screenshot")
    @patch("actions.combat._check_dead", return_value=True)
    def test_dead_detected(
        self, mock_dead, mock_screenshot, mock_swipe, mock_tap,
        mock_time, mock_sleep, mock_find, mock_save, mock_device
    ):
        """dead.png found → returns (None, None, elapsed)."""
        mock_time.side_effect = _make_time_counter(step=0.1)
        mock_screenshot.return_value = np.zeros((1920, 1080, 3), dtype=np.uint8)

        result, ss_path, elapsed = _check_green_at_current_position(
            mock_device, np.zeros((50, 50, 3), dtype=np.uint8))

        assert result is None
        assert ss_path is None

    @patch("actions.combat.save_failure_screenshot")
    @patch("actions.combat.find_image", return_value=None)
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.time.time")
    @patch("actions.combat.logged_tap")
    @patch("actions.combat.adb_swipe")
    @patch("actions.combat.load_screenshot", return_value=None)
    @patch("actions.combat._check_dead", return_value=False)
    def test_none_screenshot_no_crash(
        self, mock_dead, mock_screenshot, mock_swipe, mock_tap,
        mock_time, mock_sleep, mock_find, mock_save, mock_device
    ):
        """load_screenshot returns None every time → returns (False, None, elapsed).
        Does not try to find cancel.png since screen is None."""
        mock_time.side_effect = _make_time_counter(step=1.5)

        result, ss_path, elapsed = _check_green_at_current_position(
            mock_device, np.zeros((50, 50, 3), dtype=np.uint8))

        assert result is False
        assert ss_path is None
        # find_image should not be called since screen stayed None
        mock_find.assert_not_called()


# ============================================================
# teleport dry_run mode
# ============================================================

class TestTeleportDryRun:
    """Tests for teleport(device, dry_run=True)."""

    @patch("actions.combat._check_green_at_current_position")
    @patch("actions.combat._check_dead", return_value=False)
    @patch("actions.combat.tap_image")
    @patch("actions.combat.save_failure_screenshot")
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.time.time")
    @patch("actions.combat.adb_swipe")
    @patch("actions.combat.logged_tap")
    @patch("actions.combat.load_screenshot")
    @patch("actions.combat.get_template")
    @patch("actions.combat.check_screen", return_value=Screen.MAP)
    @patch("actions.combat.heal_all")
    @patch("actions.combat.all_troops_home")
    @patch("actions.combat.clear_click_trail")
    def test_dry_run_skips_troop_check_and_heal(
        self, mock_clear, mock_troops, mock_heal, mock_check,
        mock_template, mock_screenshot, mock_tap, mock_swipe,
        mock_time, mock_sleep, mock_save_fail, mock_tap_img,
        mock_dead, mock_green, mock_device
    ):
        """dry_run=True → all_troops_home and heal_all NOT called."""
        config.AUTO_HEAL_ENABLED = True
        mock_time.side_effect = _make_time_counter(step=0.1)
        mock_template.return_value = np.zeros((50, 50, 3), dtype=np.uint8)
        mock_screenshot.return_value = np.zeros((1920, 1080, 3), dtype=np.uint8)
        mock_green.return_value = (True, "/tmp/green.png", 1.0)

        result = teleport(mock_device, dry_run=True)

        assert result is True
        mock_troops.assert_not_called()
        mock_heal.assert_not_called()

    @patch("actions.combat._check_green_at_current_position")
    @patch("actions.combat._check_dead", return_value=False)
    @patch("actions.combat.tap_image")
    @patch("actions.combat.save_failure_screenshot")
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.time.time")
    @patch("actions.combat.adb_swipe")
    @patch("actions.combat.logged_tap")
    @patch("actions.combat.load_screenshot")
    @patch("actions.combat.get_template")
    @patch("actions.combat.check_screen", return_value=Screen.MAP)
    @patch("actions.combat.heal_all")
    @patch("actions.combat.all_troops_home")
    @patch("actions.combat.clear_click_trail")
    def test_dry_run_cancels_on_green(
        self, mock_clear, mock_troops, mock_heal, mock_check,
        mock_template, mock_screenshot, mock_tap, mock_swipe,
        mock_time, mock_sleep, mock_save_fail, mock_tap_img,
        mock_dead, mock_green, mock_device
    ):
        """dry_run=True + green found → taps cancel.png, returns True."""
        config.AUTO_HEAL_ENABLED = False
        mock_time.side_effect = _make_time_counter(step=0.1)
        mock_template.return_value = np.zeros((50, 50, 3), dtype=np.uint8)
        mock_screenshot.return_value = np.zeros((1920, 1080, 3), dtype=np.uint8)
        mock_green.return_value = (True, "/tmp/green.png", 1.0)

        result = teleport(mock_device, dry_run=True)

        assert result is True
        mock_tap_img.assert_called_once_with("cancel.png", mock_device)
        # Confirm tap should NOT be called (no tp_confirm logged_tap)
        confirm_calls = [c for c in mock_tap.call_args_list
                         if len(c.args) >= 4 and c.args[3] == "tp_confirm"]
        assert len(confirm_calls) == 0

    @patch("actions.combat.check_screen", return_value=Screen.BATTLE_LIST)
    @patch("actions.combat.clear_click_trail")
    def test_dry_run_not_on_map_returns_false(
        self, mock_clear, mock_check, mock_device
    ):
        """dry_run=True but not on MAP → returns False."""
        result = teleport(mock_device, dry_run=True)
        assert result is False


# ============================================================
# Strategy functions
# ============================================================

class TestStrategies:
    """Tests for teleport benchmark camera-positioning strategies."""

    @patch("actions.combat.time.sleep")
    @patch("actions.combat.adb_swipe")
    def test_random_pan_coordinates_in_range(self, mock_swipe, mock_sleep,
                                              mock_device):
        """random_pan: swipe endpoint within x:[100,980], y:[500,1400]."""
        _strategy_random_pan(mock_device, 0)
        mock_swipe.assert_called_once()
        _, x_end, y_end, _ = mock_swipe.call_args.args[2:]
        assert 100 <= x_end <= 980
        assert 500 <= y_end <= 1400

    @patch("actions.combat.time.sleep")
    @patch("actions.combat.adb_swipe")
    def test_big_pan_coordinates_in_range(self, mock_swipe, mock_sleep,
                                           mock_device):
        """big_pan: swipe endpoint within x:[100,980], y:[300,1600]."""
        _strategy_big_pan(mock_device, 0)
        mock_swipe.assert_called_once()
        _, x_end, y_end, _ = mock_swipe.call_args.args[2:]
        assert 100 <= x_end <= 980
        assert 300 <= y_end <= 1600

    @patch("actions.combat.time.sleep")
    @patch("actions.combat.adb_swipe")
    def test_edge_pan_cycles_compass_directions(self, mock_swipe, mock_sleep,
                                                  mock_device):
        """edge_pan: attempt_num 0 uses N, 8 wraps back to N."""
        # Attempt 0 → direction N = (0, -1) → x stays at 540, y goes up
        _strategy_edge_pan(mock_device, 0)
        args = mock_swipe.call_args.args
        assert args[1] == 540  # start_x
        assert args[2] == 960  # start_y
        assert args[3] == 540  # end_x stays centered (dx=0)
        assert args[4] < 960   # end_y goes up (dy=-1)

        mock_swipe.reset_mock()

        # Attempt 2 → direction E = (1, 0) → x goes right, y stays
        _strategy_edge_pan(mock_device, 2)
        args = mock_swipe.call_args.args
        assert args[3] > 540   # end_x goes right (dx=1)
        assert args[4] == 960  # end_y stays centered (dy=0)

    @patch("actions.combat.time.sleep")
    @patch("actions.combat.adb_tap")
    @patch("actions.combat.navigate")
    def test_territory_guided_happy_path(self, mock_nav, mock_tap,
                                          mock_sleep, mock_device):
        """territory_guided: navigates to TERRITORY, taps square, returns to MAP."""
        mock_nav.return_value = True

        with patch("actions.combat.random.randint") as mock_rand:
            # First two calls: row=5, col=5 (not throne)
            mock_rand.side_effect = [5, 5, 450]
            _strategy_territory_guided(mock_device, 0)

        assert mock_nav.call_count == 2
        mock_nav.assert_any_call(Screen.TERRITORY, mock_device)
        mock_nav.assert_any_call(Screen.MAP, mock_device)
        mock_tap.assert_called_once()  # tapped the grid square

    @patch("actions.combat._strategy_random_pan")
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.navigate")
    def test_territory_guided_nav_failure_falls_back(
        self, mock_nav, mock_sleep, mock_fallback, mock_device
    ):
        """territory_guided: TERRITORY nav fails → falls back to random_pan."""
        mock_nav.side_effect = [False, True]  # TERRITORY fails, MAP succeeds

        _strategy_territory_guided(mock_device, 0)

        mock_fallback.assert_called_once_with(mock_device, 0)


# ============================================================
# _run_trial
# ============================================================

class TestRunTrial:
    """Tests for the benchmark trial runner."""

    @patch("actions.combat.tap_image")
    @patch("actions.combat._check_green_at_current_position")
    @patch("actions.combat._check_dead", return_value=False)
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.time.time")
    @patch("actions.combat.logged_tap")
    @patch("actions.combat.load_screenshot")
    @patch("actions.combat.get_template")
    @patch("actions.combat.navigate", return_value=True)
    def test_success_first_attempt(
        self, mock_nav, mock_template, mock_screenshot, mock_tap,
        mock_time, mock_sleep, mock_dead, mock_green, mock_tap_img,
        mock_device
    ):
        """Green found on first attempt → TeleportTrial with success=True."""
        mock_time.side_effect = _make_time_counter(step=0.1)
        mock_template.return_value = np.zeros((50, 50, 3), dtype=np.uint8)
        mock_screenshot.return_value = np.zeros((1920, 1080, 3), dtype=np.uint8)
        mock_green.return_value = (True, "/tmp/ss.png", 1.0)

        strategy_fn = MagicMock()
        trial = _run_trial(mock_device, "test_strat", strategy_fn, 1)

        assert trial.success is True
        assert trial.strategy == "test_strat"
        assert trial.trial_num == 1
        assert trial.total_attempts == 1
        assert len(trial.attempts) == 1
        assert trial.attempts[0].success is True
        # Cancel tapped (benchmark always dry-runs)
        mock_tap_img.assert_called_with("cancel.png", mock_device)

    @patch("actions.combat._check_green_at_current_position")
    @patch("actions.combat._check_dead", return_value=False)
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.time.time")
    @patch("actions.combat.logged_tap")
    @patch("actions.combat.load_screenshot")
    @patch("actions.combat.get_template")
    @patch("actions.combat.navigate", return_value=True)
    def test_all_attempts_fail(
        self, mock_nav, mock_template, mock_screenshot, mock_tap,
        mock_time, mock_sleep, mock_dead, mock_green, mock_device
    ):
        """No green found after max attempts → TeleportTrial with success=False."""
        mock_time.side_effect = _make_time_counter(step=0.5)
        mock_template.return_value = np.zeros((50, 50, 3), dtype=np.uint8)
        mock_screenshot.return_value = np.zeros((1920, 1080, 3), dtype=np.uint8)
        mock_green.return_value = (False, None, 1.0)

        strategy_fn = MagicMock()
        trial = _run_trial(mock_device, "test_strat", strategy_fn, 1,
                           max_attempts=3, timeout_s=90)

        assert trial.success is False
        assert trial.total_attempts == 3
        assert len(trial.attempts) == 3
        assert all(not a.success for a in trial.attempts)

    @patch("actions.combat._check_green_at_current_position")
    @patch("actions.combat._check_dead", return_value=False)
    @patch("actions.combat.time.sleep")
    @patch("actions.combat.time.time")
    @patch("actions.combat.logged_tap")
    @patch("actions.combat.load_screenshot")
    @patch("actions.combat.get_template")
    @patch("actions.combat.navigate", return_value=True)
    def test_dead_aborts_trial(
        self, mock_nav, mock_template, mock_screenshot, mock_tap,
        mock_time, mock_sleep, mock_dead, mock_green, mock_device
    ):
        """dead.png detected during trial → abort with success=False."""
        mock_time.side_effect = _make_time_counter(step=0.1)
        mock_template.return_value = np.zeros((50, 50, 3), dtype=np.uint8)
        mock_screenshot.return_value = np.zeros((1920, 1080, 3), dtype=np.uint8)
        mock_green.return_value = (None, None, 1.0)  # dead detected

        strategy_fn = MagicMock()
        trial = _run_trial(mock_device, "test_strat", strategy_fn, 1)

        assert trial.success is False
        assert trial.total_attempts == 1

    @patch("actions.combat.navigate", return_value=False)
    def test_nav_failure_returns_empty_trial(self, mock_nav, mock_device):
        """Failed to navigate to MAP → trial with 0 attempts."""
        strategy_fn = MagicMock()
        trial = _run_trial(mock_device, "test_strat", strategy_fn, 1)

        assert trial.success is False
        assert trial.total_attempts == 0
        assert trial.strategy == "test_strat"
        strategy_fn.assert_not_called()


# ============================================================
# teleport_benchmark + _print_benchmark_summary
# ============================================================

class TestTeleportBenchmark:
    """Tests for the benchmark orchestrator and summary printer."""

    @patch("actions.combat._run_trial")
    def test_invalid_strategy_logs_error(self, mock_trial, mock_device):
        """Unknown strategy name → logs error, returns without running."""
        teleport_benchmark(mock_device, strategies=["nonexistent"])
        mock_trial.assert_not_called()

    @patch("actions.combat._print_benchmark_summary")
    @patch("actions.combat._run_trial")
    def test_runs_correct_number_of_trials(self, mock_trial, mock_summary,
                                            mock_device, tmp_path):
        """2 strategies × 2 trials = 4 _run_trial calls."""
        mock_trial.return_value = TeleportTrial(
            strategy="random_pan", trial_num=1, success=True,
            total_attempts=3, total_time_s=10.0)

        with patch("actions.combat.os.makedirs"), \
             patch("builtins.open", MagicMock()):
            teleport_benchmark(mock_device, trials_per_strategy=2,
                               strategies=["random_pan", "big_pan"])

        assert mock_trial.call_count == 4
        mock_summary.assert_called_once()

    @patch("actions.combat._print_benchmark_summary")
    @patch("actions.combat._run_trial")
    def test_saves_json_results(self, mock_trial, mock_summary,
                                 mock_device, tmp_path):
        """Results are saved to stats/teleport_benchmark_*.json."""
        mock_trial.return_value = TeleportTrial(
            strategy="random_pan", trial_num=1, success=True,
            total_attempts=1, total_time_s=5.0)

        stats_dir = str(tmp_path / "stats")
        with patch("actions.combat.os.makedirs") as mock_mkdir:
            # Capture the actual json.dump call
            import builtins
            written = {}
            original_open = builtins.open
            def fake_open(path, mode="r", **kwargs):
                if "teleport_benchmark" in str(path) and mode == "w":
                    written["path"] = path
                    return MagicMock(__enter__=lambda s: MagicMock(
                        write=lambda d: written.update({"data": d})),
                        __exit__=lambda *a: None)
                return original_open(path, mode, **kwargs)

            with patch("builtins.open", side_effect=fake_open):
                teleport_benchmark(mock_device, trials_per_strategy=1,
                                   strategies=["random_pan"])

        assert "path" in written
        assert "teleport_benchmark" in written["path"]


class TestPrintBenchmarkSummary:
    """Tests for _print_benchmark_summary output."""

    def test_formats_summary_table(self, capsys):
        """Summary table includes strategy name, win count, and percentages."""
        trials = [
            TeleportTrial(strategy="random_pan", trial_num=1, success=True,
                          total_attempts=3, total_time_s=15.0),
            TeleportTrial(strategy="random_pan", trial_num=2, success=False,
                          total_attempts=15, total_time_s=90.0),
            TeleportTrial(strategy="big_pan", trial_num=1, success=True,
                          total_attempts=1, total_time_s=5.0),
            TeleportTrial(strategy="big_pan", trial_num=2, success=True,
                          total_attempts=2, total_time_s=8.0),
        ]

        _print_benchmark_summary(trials)

        output = capsys.readouterr().out
        assert "random_pan" in output
        assert "big_pan" in output
        assert "Teleport Benchmark Results" in output
        # random_pan: 1/2 = 50%
        assert "50%" in output
        # big_pan: 2/2 = 100%
        assert "100%" in output

    def test_handles_all_failures(self, capsys):
        """All trials failed → 0% win rate, 0.0s avg time."""
        trials = [
            TeleportTrial(strategy="random_pan", trial_num=1, success=False,
                          total_attempts=15, total_time_s=90.0),
        ]

        _print_benchmark_summary(trials)

        output = capsys.readouterr().out
        assert "random_pan" in output
        assert "0%" in output
