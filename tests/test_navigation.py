"""Tests for navigation state machine (navigation.py).

All ADB and vision calls are mocked — no emulator needed.
"""

import numpy as np
from unittest.mock import patch, MagicMock, call

from config import Screen
from navigation import check_screen, navigate, _verify_screen, _recover_to_known_screen


# ============================================================
# check_screen
# ============================================================

class TestCheckScreen:
    @patch("navigation.adb_tap")
    @patch("navigation.get_template")
    @patch("navigation.load_screenshot")
    def test_returns_best_match(self, mock_screenshot, mock_template, mock_tap):
        """Should return the screen name with the highest confidence above 0.8."""
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        mock_screenshot.return_value = screen

        # Return different templates for different names
        import cv2
        templates = {}
        for name in [Screen.MAP, Screen.BATTLE_LIST, Screen.ALLIANCE_QUEST, Screen.TROOP_DETAIL,
                      Screen.TERRITORY, Screen.WAR, Screen.PROFILE, Screen.ALLIANCE]:
            templates[f"elements/{name}.png"] = np.zeros((10, 10, 3), dtype=np.uint8)
        templates["elements/attention.png"] = np.zeros((10, 10, 3), dtype=np.uint8)
        mock_template.side_effect = lambda path: templates.get(path)

        # Mock matchTemplate to return high score for war_screen, low for others
        original_match = cv2.matchTemplate

        def fake_match(img, tpl, method):
            result = np.zeros((img.shape[0] - tpl.shape[0] + 1,
                               img.shape[1] - tpl.shape[1] + 1), dtype=np.float32)
            return result

        with patch("navigation.cv2.matchTemplate", side_effect=fake_match):
            with patch("navigation.cv2.minMaxLoc") as mock_minmax:
                # attention template: low
                # screen templates: return different scores
                # We need to control the sequence of minMaxLoc calls:
                # 1st call: attention template → low
                # Then for each screen template in order
                mock_minmax.side_effect = [
                    (0, 0.1, (0, 0), (0, 0)),  # attention
                    (0, 0.5, (0, 0), (0, 0)),  # map_screen
                    (0, 0.3, (0, 0), (0, 0)),  # bl_screen
                    (0, 0.85, (0, 0), (0, 0)), # aq_screen ← winner
                    (0, 0.2, (0, 0), (0, 0)),  # td_screen
                    (0, 0.1, (0, 0), (0, 0)),  # territory_screen
                    (0, 0.4, (0, 0), (0, 0)),  # war_screen
                    (0, 0.1, (0, 0), (0, 0)),  # profile_screen
                    (0, 0.1, (0, 0), (0, 0)),  # alliance_screen
                ]
                result = check_screen("dev1")

        assert result == Screen.ALLIANCE_QUEST

    @patch("navigation.get_template")
    @patch("navigation.load_screenshot")
    def test_screenshot_failure(self, mock_screenshot, mock_template):
        mock_screenshot.return_value = None
        assert check_screen("dev1") == Screen.UNKNOWN

    @patch("navigation.adb_tap")
    @patch("navigation.get_template")
    @patch("navigation.load_screenshot")
    def test_all_below_threshold_returns_unknown(self, mock_screenshot, mock_template, mock_tap):
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        mock_screenshot.return_value = screen
        mock_template.return_value = np.zeros((10, 10, 3), dtype=np.uint8)

        import cv2
        with patch("navigation.cv2.matchTemplate") as mock_match:
            result_arr = np.zeros((1911, 1071), dtype=np.float32)
            mock_match.return_value = result_arr
            with patch("navigation.cv2.minMaxLoc", return_value=(0, 0.3, (0, 0), (0, 0))):
                result = check_screen("dev1")

        assert result == Screen.UNKNOWN


# ============================================================
# _verify_screen
# ============================================================

class TestVerifyScreen:
    @patch("navigation._save_debug_screenshot")
    @patch("navigation.check_screen")
    def test_success_first_try(self, mock_check, mock_save):
        mock_check.return_value = Screen.MAP
        assert _verify_screen(Screen.MAP, "dev1") is True

    @patch("navigation._save_debug_screenshot")
    @patch("navigation.check_screen")
    def test_success_on_retry(self, mock_check, mock_save):
        mock_check.side_effect = [Screen.UNKNOWN, Screen.MAP]
        assert _verify_screen(Screen.MAP, "dev1") is True

    @patch("navigation.stats")
    @patch("navigation._save_debug_screenshot")
    @patch("navigation.check_screen")
    def test_failure_after_retries(self, mock_check, mock_save, mock_stats):
        mock_check.return_value = Screen.BATTLE_LIST
        assert _verify_screen(Screen.MAP, "dev1") is False
        mock_stats.record_nav_failure.assert_called_once()


# ============================================================
# navigate
# ============================================================

class TestNavigate:
    @patch("navigation.check_screen")
    def test_already_on_target(self, mock_check):
        mock_check.return_value = Screen.MAP
        assert navigate(Screen.MAP, "dev1") is True

    @patch("navigation._recover_to_known_screen")
    @patch("navigation.check_screen")
    def test_recursion_guard(self, mock_check, mock_recover):
        """Depth > 3 should return False immediately."""
        mock_check.return_value = Screen.UNKNOWN
        mock_recover.return_value = Screen.UNKNOWN
        assert navigate(Screen.MAP, "dev1", _depth=4) is False

    @patch("navigation._verify_screen")
    @patch("navigation.tap_image")
    @patch("navigation.check_screen")
    def test_map_to_bl(self, mock_check, mock_tap_img, mock_verify):
        mock_check.return_value = Screen.MAP
        mock_tap_img.return_value = True
        mock_verify.return_value = True
        assert navigate(Screen.BATTLE_LIST, "dev1") is True
        mock_tap_img.assert_called_with("bl_button.png", "dev1")

    @patch("navigation.adb_tap")
    @patch("navigation.check_screen")
    def test_td_to_map(self, mock_check, mock_tap):
        # First call: on td_screen. After tap: on map_screen.
        mock_check.side_effect = [Screen.TROOP_DETAIL, Screen.MAP]
        assert navigate(Screen.MAP, "dev1") is True
        mock_tap.assert_called_with("dev1", 990, 1850)

    @patch("navigation.adb_tap")
    @patch("navigation.check_screen")
    def test_alliance_to_map(self, mock_check, mock_tap):
        mock_check.side_effect = [Screen.ALLIANCE, Screen.MAP]
        assert navigate(Screen.MAP, "dev1") is True
        mock_tap.assert_called_with("dev1", 75, 75)

    @patch("navigation._recover_to_known_screen")
    @patch("navigation.check_screen")
    def test_unknown_screen_recovery_fails(self, mock_check, mock_recover):
        mock_check.return_value = Screen.UNKNOWN
        mock_recover.return_value = Screen.UNKNOWN
        assert navigate(Screen.MAP, "dev1") is False


# ============================================================
# _recover_to_known_screen
# ============================================================

class TestRecoverToKnownScreen:
    @patch("navigation.adb_tap")
    @patch("navigation.tap_image")
    @patch("navigation.check_screen")
    def test_first_strategy_succeeds(self, mock_check, mock_tap_img, mock_tap):
        # close_x template works on first try
        mock_tap_img.return_value = True
        mock_check.return_value = Screen.MAP
        result = _recover_to_known_screen("dev1")
        assert result == Screen.MAP

    @patch("navigation.adb_tap")
    @patch("navigation.tap_image")
    @patch("navigation.check_screen")
    def test_all_strategies_fail(self, mock_check, mock_tap_img, mock_tap):
        mock_tap_img.return_value = False
        mock_check.return_value = Screen.UNKNOWN
        result = _recover_to_known_screen("dev1")
        assert result == Screen.UNKNOWN

    @patch("navigation.adb_tap")
    @patch("navigation.tap_image")
    @patch("navigation.check_screen")
    def test_back_button_succeeds(self, mock_check, mock_tap_img, mock_tap):
        # First two strategies fail, back button succeeds
        mock_tap_img.return_value = False
        mock_check.side_effect = [Screen.UNKNOWN, Screen.UNKNOWN, Screen.BATTLE_LIST]
        result = _recover_to_known_screen("dev1")
        assert result == Screen.BATTLE_LIST
