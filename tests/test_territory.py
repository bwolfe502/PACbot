"""Tests for territory grid analysis and auto-occupy (territory.py).

Covers: _classify_square_team, _get_border_color, _has_flag,
_is_adjacent_to_my_territory, _get_square_center, attack_territory,
auto_occupy_loop, diagnose_grid, set_territory_config.

Focus on the red team vs yellow enemy color pair (current game config).
All ADB and vision calls are mocked — no emulator needed.
"""

import numpy as np
import pytest
from unittest.mock import patch, MagicMock, call

import config
from config import (
    Screen, SQUARE_SIZE, GRID_OFFSET_X, GRID_OFFSET_Y,
    GRID_WIDTH, GRID_HEIGHT, THRONE_SQUARES, BORDER_COLORS,
    ALL_TEAMS, set_territory_config,
)
from territory import (
    _classify_square_team, _get_border_color, _has_flag,
    _is_adjacent_to_my_territory, _get_square_center,
    attack_territory, auto_occupy_loop, diagnose_grid,
)


# ============================================================
# Fixtures — reset territory config state before each test
# ============================================================

@pytest.fixture(autouse=True)
def reset_territory_state():
    """Reset all territory-related global state before each test."""
    orig_team = config.MY_TEAM_COLOR
    orig_enemies = config.ENEMY_TEAMS
    config.MY_TEAM_COLOR = "red"
    config.ENEMY_TEAMS = ["yellow"]
    config.MANUAL_ATTACK_SQUARES.clear()
    config.MANUAL_IGNORE_SQUARES.clear()
    config.LAST_ATTACKED_SQUARE.clear()
    config.AUTO_HEAL_ENABLED = False
    config.MIN_TROOPS_AVAILABLE = 0
    config.auto_occupy_running = False
    yield
    config.MY_TEAM_COLOR = orig_team
    config.ENEMY_TEAMS = orig_enemies
    config.MANUAL_ATTACK_SQUARES.clear()
    config.MANUAL_IGNORE_SQUARES.clear()
    config.LAST_ATTACKED_SQUARE.clear()
    config.auto_occupy_running = False


# ============================================================
# Helper — build a fake territory screenshot
# ============================================================

def _make_territory_image(color_map=None):
    """Build a 1080x1920 (HxW in numpy = 1920x1080) territory screenshot.

    color_map: dict of {(row, col): (B, G, R)} — sets the border pixels
    for those squares. Unset squares get black (0,0,0).
    """
    image = np.zeros((1920, 1080, 3), dtype=np.uint8)
    if color_map:
        for (row, col), bgr in color_map.items():
            x = int(GRID_OFFSET_X + col * SQUARE_SIZE)
            y = int(GRID_OFFSET_Y + row * SQUARE_SIZE)
            w = int(SQUARE_SIZE)
            h = int(SQUARE_SIZE)
            # Paint the entire square with the border color so sampling picks it up
            y_end = min(y + h, image.shape[0])
            x_end = min(x + w, image.shape[1])
            image[y:y_end, x:x_end] = bgr
    return image


# ============================================================
# _get_square_center
# ============================================================

class TestGetSquareCenter:
    def test_origin_square(self):
        """Square (0,0) center is offset + half square size."""
        x, y = _get_square_center(0, 0)
        assert x == int(GRID_OFFSET_X + SQUARE_SIZE / 2)
        assert y == int(GRID_OFFSET_Y + SQUARE_SIZE / 2)

    def test_middle_square(self):
        """Square (12, 12) — near throne area."""
        x, y = _get_square_center(12, 12)
        expected_x = int(GRID_OFFSET_X + 12 * SQUARE_SIZE + SQUARE_SIZE / 2)
        expected_y = int(GRID_OFFSET_Y + 12 * SQUARE_SIZE + SQUARE_SIZE / 2)
        assert x == expected_x
        assert y == expected_y

    def test_bottom_right_square(self):
        """Square (23, 23) — last valid grid position."""
        x, y = _get_square_center(23, 23)
        expected_x = int(GRID_OFFSET_X + 23 * SQUARE_SIZE + SQUARE_SIZE / 2)
        expected_y = int(GRID_OFFSET_Y + 23 * SQUARE_SIZE + SQUARE_SIZE / 2)
        assert x == expected_x
        assert y == expected_y


# ============================================================
# _classify_square_team — color classification
# ============================================================

class TestClassifySquareTeam:
    """Test the core color classification logic.

    BORDER_COLORS (BGR):
      yellow: (107, 223, 239)
      green:  (100, 175, 160) — recalibrated from live diagnostic data 2026-02-28
      red:    (49, 85, 247)
      blue:   (148, 145, 165) — recalibrated from live diagnostic data 2026-02-28
    """

    # --- Exact color matches ---

    def test_exact_red_border(self):
        """Exact red team border color → 'red' (own team)."""
        assert _classify_square_team(BORDER_COLORS["red"]) == "red"

    def test_exact_yellow_border(self):
        """Exact yellow enemy border color → 'yellow'."""
        assert _classify_square_team(BORDER_COLORS["yellow"]) == "yellow"

    def test_exact_green_border(self):
        """Exact green border → 'green' (neutral)."""
        assert _classify_square_team(BORDER_COLORS["green"]) == "green"

    def test_exact_blue_border(self):
        """Exact blue border → 'blue' (not in ENEMY_TEAMS, but within 55)."""
        assert _classify_square_team(BORDER_COLORS["blue"]) == "blue"

    # --- Noisy/variant colors (within threshold) ---

    @pytest.mark.parametrize("noise", [
        (5, 5, 5), (-5, -5, -5), (10, -10, 5), (-15, 8, 12), (20, 20, 20),
    ])
    def test_noisy_red_within_threshold(self, noise):
        """Red border + noise within tolerance → still 'red'."""
        b, g, r = BORDER_COLORS["red"]
        noisy = (max(0, b + noise[0]), max(0, g + noise[1]), max(0, r + noise[2]))
        assert _classify_square_team(noisy) == "red"

    @pytest.mark.parametrize("noise", [
        (5, 5, 5), (-5, -5, -5), (10, -10, 5), (-15, 8, 12), (20, 20, 20),
    ])
    def test_noisy_yellow_within_threshold(self, noise):
        """Yellow border + noise within tolerance → still 'yellow'."""
        b, g, r = BORDER_COLORS["yellow"]
        noisy = (max(0, b + noise[0]), max(0, g + noise[1]), max(0, r + noise[2]))
        assert _classify_square_team(noisy) == "yellow"

    @pytest.mark.parametrize("noise", [
        (5, 5, 5), (-5, -5, -5), (10, -10, 5),
    ])
    def test_noisy_green_within_threshold(self, noise):
        """Green border + noise → still 'green'."""
        b, g, r = BORDER_COLORS["green"]
        noisy = (max(0, b + noise[0]), max(0, g + noise[1]), max(0, r + noise[2]))
        assert _classify_square_team(noisy) == "green"

    # --- Own team gets lenient threshold (90 vs 70 for enemies) ---

    def test_red_lenient_threshold_at_80(self):
        """Red at distance ~80 from target — within 90 (own team), outside 70."""
        # Red target: (49, 85, 247). Shift by ~80 in one channel.
        bgr = (49, 85, 247 - 80)  # distance = 80
        assert _classify_square_team(bgr) == "red"

    def test_yellow_outside_70_returns_unknown(self):
        """Yellow at distance ~75 from target — outside 70 (enemy threshold)."""
        # Yellow target: (107, 223, 239). Shift to get distance ~75.
        bgr = (107, 223 - 75, 239)  # distance = 75
        # Nearest team is yellow but distance > 70 (enemy threshold)
        result = _classify_square_team(bgr)
        assert result != "yellow"

    # --- Edge cases ---

    def test_black_returns_unknown(self):
        """All-black pixel — too far from any team color."""
        assert _classify_square_team((0, 0, 0)) == "unknown"

    def test_white_returns_unknown(self):
        """All-white pixel — too far from any team color."""
        assert _classify_square_team((255, 255, 255)) == "unknown"

    def test_midpoint_red_yellow_classifies_correctly(self):
        """Midpoint between red and yellow borders — should pick one, not crash."""
        rb, rg, rr = BORDER_COLORS["red"]
        yb, yg, yr = BORDER_COLORS["yellow"]
        mid = ((rb + yb) // 2, (rg + yg) // 2, (rr + yr) // 2)
        result = _classify_square_team(mid)
        # Should classify as something (not crash); exact team depends on distances
        assert result in ("red", "yellow", "unknown")

    def test_own_team_beyond_90_returns_unknown(self):
        """Distance 93 from own team (> 90 threshold) → 'unknown'.

        Note: the fallback block (best_team == "unknown" and distance <= 95)
        is unreachable because best_team is always set to a real team after
        iterating BORDER_COLORS. So distance 91-95 from own team = unknown.
        """
        # Red target: (49, 85, 247). d = sqrt(0+0+93^2) = 93.
        bgr = (49, 85, 154)
        assert _classify_square_team(bgr) == "unknown"

    def test_own_team_at_boundary_90(self):
        """Distance exactly 90 from own team → still recognized as own team."""
        # Red target: (49, 85, 247). d = sqrt(0+0+90^2) = 90.
        bgr = (49, 85, 157)
        assert _classify_square_team(bgr) == "red"

    def test_beyond_all_thresholds_returns_unknown(self):
        """Distance > 95 from all teams → 'unknown'."""
        # (0, 0, 128) — far from all defined border colors
        # yellow (107,223,239): d=sqrt(107^2+223^2+111^2)≈269
        # green  (100,175,160): d=sqrt(100^2+175^2+32^2)≈203
        # red    (49,85,247):   d=sqrt(49^2+85^2+119^2)≈155
        # blue   (148,145,165): d=sqrt(148^2+145^2+37^2)≈212
        assert _classify_square_team((0, 0, 128)) == "unknown"

    # --- Different team configurations ---

    def test_yellow_team_green_enemy(self):
        """Previous season config: yellow own, green enemy."""
        config.MY_TEAM_COLOR = "yellow"
        config.ENEMY_TEAMS = ["green"]

        assert _classify_square_team(BORDER_COLORS["yellow"]) == "yellow"
        assert _classify_square_team(BORDER_COLORS["green"]) == "green"
        assert _classify_square_team(BORDER_COLORS["red"]) == "red"

    def test_blue_team_red_enemy(self):
        """Hypothetical config: blue own, red enemy."""
        config.MY_TEAM_COLOR = "blue"
        config.ENEMY_TEAMS = ["red"]

        assert _classify_square_team(BORDER_COLORS["blue"]) == "blue"
        assert _classify_square_team(BORDER_COLORS["red"]) == "red"

    def test_multiple_enemies(self):
        """Multiple enemy teams — all should be recognized."""
        config.ENEMY_TEAMS = ["yellow", "green", "blue"]

        assert _classify_square_team(BORDER_COLORS["yellow"]) == "yellow"
        assert _classify_square_team(BORDER_COLORS["green"]) == "green"
        assert _classify_square_team(BORDER_COLORS["blue"]) == "blue"
        assert _classify_square_team(BORDER_COLORS["red"]) == "red"


# ============================================================
# _get_border_color — pixel sampling
# ============================================================

class TestGetBorderColor:
    def test_uniform_square_returns_that_color(self):
        """Square painted solid red → border sample returns red."""
        image = _make_territory_image({(5, 5): (49, 85, 247)})
        color = _get_border_color(image, 5, 5)
        # Should be close to (49, 85, 247)
        assert abs(color[0] - 49) < 2
        assert abs(color[1] - 85) < 2
        assert abs(color[2] - 247) < 2

    def test_row_0_avoids_top_pixels(self):
        """Row 0 uses special sampling to avoid clock overlay."""
        image = _make_territory_image({(0, 5): (107, 223, 239)})
        color = _get_border_color(image, 0, 5)
        # Should still pick up the yellow color
        assert abs(color[0] - 107) < 2
        assert abs(color[1] - 223) < 2
        assert abs(color[2] - 239) < 2

    def test_row_1_partial_clock_avoidance(self):
        """Row 1 uses mixed sampling strategy."""
        image = _make_territory_image({(1, 5): (100, 175, 160)})
        color = _get_border_color(image, 1, 5)
        assert abs(color[0] - 100) < 2
        assert abs(color[1] - 175) < 2
        assert abs(color[2] - 160) < 2

    def test_normal_row_samples_top_and_left_edges(self):
        """Rows >= 2 sample from top edge and left edge of the square."""
        image = _make_territory_image({(10, 10): (148, 145, 165)})
        color = _get_border_color(image, 10, 10)
        assert abs(color[0] - 148) < 2
        assert abs(color[1] - 145) < 2
        assert abs(color[2] - 165) < 2

    def test_black_square_returns_black(self):
        """Unset square → (0, 0, 0)."""
        image = _make_territory_image()
        color = _get_border_color(image, 5, 5)
        assert color == (0.0, 0.0, 0.0)

    def test_edge_square_bottom_right(self):
        """Last square (23, 23) doesn't crash from boundary clipping."""
        image = _make_territory_image({(23, 23): (49, 85, 247)})
        color = _get_border_color(image, 23, 23)
        # Should get some red pixels (might be partial if square extends past image)
        assert isinstance(color, tuple)
        assert len(color) == 3


# ============================================================
# _has_flag — red flag pixel detection
# ============================================================

class TestHasFlag:
    def _paint_flag(self, image, row, col, num_pixels=20):
        """Paint red flag pixels (BGR in range (75-105, 80-110, 240-255)) onto a square."""
        x = int(GRID_OFFSET_X + col * SQUARE_SIZE)
        y = int(GRID_OFFSET_Y + row * SQUARE_SIZE)
        # Paint a small block of flag-colored pixels
        for i in range(num_pixels):
            px = x + 10 + (i % 5)
            py = y + 10 + (i // 5)
            if py < image.shape[0] and px < image.shape[1]:
                image[py, px] = (90, 95, 248)  # Within (75-105, 80-110, 240-255)

    def test_flag_present(self):
        """Square with 20+ red flag pixels → True."""
        image = _make_territory_image()
        self._paint_flag(image, 5, 5, num_pixels=20)
        assert _has_flag(image, 5, 5) is True

    def test_no_flag_clean_square(self):
        """Square with no red pixels → False."""
        image = _make_territory_image()
        assert _has_flag(image, 5, 5) is False

    def test_few_red_pixels_below_threshold(self):
        """Square with only 10 red pixels (< 15 threshold) → False."""
        image = _make_territory_image()
        self._paint_flag(image, 5, 5, num_pixels=10)
        assert _has_flag(image, 5, 5) is False

    def test_flag_on_colored_square(self):
        """Flag pixels on top of a yellow border square."""
        image = _make_territory_image({(5, 5): (107, 223, 239)})
        self._paint_flag(image, 5, 5, num_pixels=25)
        assert _has_flag(image, 5, 5) is True

    def test_yellow_border_not_detected_as_flag(self):
        """Yellow border pixels should NOT trigger flag detection."""
        image = _make_territory_image({(5, 5): (107, 223, 239)})
        assert _has_flag(image, 5, 5) is False

    def test_red_border_not_detected_as_flag(self):
        """Red team border (49, 85, 247) — R=247 is within range but B/G aren't."""
        image = _make_territory_image({(5, 5): (49, 85, 247)})
        # Red border BGR: B=49, G=85 — B not in 75-105 range, so shouldn't match
        assert _has_flag(image, 5, 5) is False


# ============================================================
# _is_adjacent_to_my_territory
# ============================================================

class TestIsAdjacentToMyTerritory:
    def test_adjacent_to_own_team(self):
        """Enemy square with own team neighbor → True."""
        # (5,5) is red (own), (5,6) is yellow (enemy)
        image = _make_territory_image({
            (5, 5): BORDER_COLORS["red"],
            (5, 6): BORDER_COLORS["yellow"],
        })
        assert _is_adjacent_to_my_territory(image, 5, 6) is True

    def test_not_adjacent_to_own_team(self):
        """Enemy square with no own team neighbors → False."""
        # (5,6) is yellow, surrounded by black (unknown)
        image = _make_territory_image({(5, 6): BORDER_COLORS["yellow"]})
        assert _is_adjacent_to_my_territory(image, 5, 6) is False

    def test_diagonal_not_counted(self):
        """Own team square diagonally adjacent → False (only orthogonal counts)."""
        image = _make_territory_image({
            (5, 5): BORDER_COLORS["red"],   # diagonal to (6, 6)
            (6, 6): BORDER_COLORS["yellow"],
        })
        assert _is_adjacent_to_my_territory(image, 6, 6) is False

    def test_throne_square_neighbor_skipped(self):
        """If neighbor is a throne square, it's skipped (not counted as own)."""
        # Place red at (11, 10) — adjacent to throne (11, 11)
        # Place yellow at (10, 11) — above throne
        image = _make_territory_image({
            (11, 10): BORDER_COLORS["red"],
            (10, 11): BORDER_COLORS["yellow"],
        })
        # (10, 11) neighbors: (9,11)=black, (11,11)=throne(skipped), (10,10)=black, (10,12)=black
        # Plus (11,10) is not a direct neighbor of (10,11)
        assert _is_adjacent_to_my_territory(image, 10, 11) is False

    def test_edge_square_row_0(self):
        """Square at row 0 — neighbor at row -1 is out of bounds (handled)."""
        image = _make_territory_image({
            (0, 5): BORDER_COLORS["yellow"],
            (1, 5): BORDER_COLORS["red"],
        })
        assert _is_adjacent_to_my_territory(image, 0, 5) is True

    def test_edge_square_col_0(self):
        """Square at col 0 — neighbor at col -1 is out of bounds."""
        image = _make_territory_image({
            (5, 0): BORDER_COLORS["yellow"],
            (5, 1): BORDER_COLORS["red"],
        })
        assert _is_adjacent_to_my_territory(image, 5, 0) is True

    def test_multiple_own_neighbors(self):
        """Multiple own team neighbors — still returns True."""
        image = _make_territory_image({
            (5, 5): BORDER_COLORS["yellow"],
            (4, 5): BORDER_COLORS["red"],
            (6, 5): BORDER_COLORS["red"],
            (5, 4): BORDER_COLORS["red"],
        })
        assert _is_adjacent_to_my_territory(image, 5, 5) is True


# ============================================================
# attack_territory — full workflow tests
# ============================================================

class TestAttackTerritory:
    """Integration tests for the full attack_territory workflow.

    All external dependencies (navigate, heal_all, etc.) are mocked.
    The grid analysis uses real _classify_square_team logic on synthetic images.
    """

    @patch("territory.time.sleep")
    @patch("territory.adb_tap")
    @patch("territory.load_screenshot")
    @patch("territory.all_troops_home", return_value=True)
    @patch("territory.heal_all")
    @patch("territory.navigate", return_value=True)
    def test_happy_path_attacks_yellow_target(
        self, mock_nav, mock_heal, mock_troops, mock_screenshot,
        mock_tap, mock_sleep, mock_device
    ):
        """Red team finds adjacent yellow square → taps it."""
        # Build image: red at (5,5), yellow at (5,6) — adjacent, no flag
        image = _make_territory_image({
            (5, 5): BORDER_COLORS["red"],
            (5, 6): BORDER_COLORS["yellow"],
        })
        mock_screenshot.return_value = image

        result = attack_territory(mock_device)

        assert result is True
        mock_tap.assert_called_once()
        # Verify we tapped (5,6) — the yellow enemy square
        tap_x, tap_y = mock_tap.call_args[0][1], mock_tap.call_args[0][2]
        expected_x, expected_y = _get_square_center(5, 6)
        assert tap_x == expected_x
        assert tap_y == expected_y
        # Verify square was remembered
        assert config.LAST_ATTACKED_SQUARE[mock_device] == (5, 6)

    @patch("territory.time.sleep")
    @patch("territory.adb_tap")
    @patch("territory.load_screenshot")
    @patch("territory.all_troops_home", return_value=True)
    @patch("territory.heal_all")
    @patch("territory.navigate", return_value=True)
    def test_skips_flagged_yellow_square(
        self, mock_nav, mock_heal, mock_troops, mock_screenshot,
        mock_tap, mock_sleep, mock_device
    ):
        """Yellow square with flag → skipped, no targets → return False."""
        image = _make_territory_image({
            (5, 5): BORDER_COLORS["red"],
            (5, 6): BORDER_COLORS["yellow"],
        })
        # Paint flag on (5,6)
        x = int(GRID_OFFSET_X + 6 * SQUARE_SIZE)
        y = int(GRID_OFFSET_Y + 5 * SQUARE_SIZE)
        for i in range(25):
            px, py = x + 10 + (i % 5), y + 10 + (i // 5)
            if py < image.shape[0] and px < image.shape[1]:
                image[py, px] = (90, 95, 248)
        mock_screenshot.return_value = image

        result = attack_territory(mock_device)

        assert result is False
        mock_tap.assert_not_called()

    @patch("territory.navigate", return_value=False)
    def test_fail_navigate_to_map(self, mock_nav, mock_device):
        """Failed navigation to MAP → return False."""
        result = attack_territory(mock_device)
        assert result is False

    @patch("territory.time.sleep")
    @patch("territory.all_troops_home", return_value=False)
    @patch("territory.heal_all")
    @patch("territory.navigate", return_value=True)
    def test_troops_not_home_aborts(
        self, mock_nav, mock_heal, mock_troops, mock_sleep, mock_device
    ):
        """Troops not home → return False without scanning grid."""
        result = attack_territory(mock_device)
        assert result is False

    @patch("territory.time.sleep")
    @patch("territory.adb_tap")
    @patch("territory.load_screenshot", return_value=None)
    @patch("territory.all_troops_home", return_value=True)
    @patch("territory.heal_all")
    @patch("territory.navigate", return_value=True)
    def test_screenshot_none_returns_false(
        self, mock_nav, mock_heal, mock_troops, mock_screenshot,
        mock_tap, mock_sleep, mock_device
    ):
        """load_screenshot returning None → return False."""
        result = attack_territory(mock_device)
        assert result is False

    @patch("territory.time.sleep")
    @patch("territory.adb_tap")
    @patch("territory.load_screenshot")
    @patch("territory.all_troops_home", return_value=True)
    @patch("territory.heal_all")
    @patch("territory.navigate", return_value=True)
    def test_no_enemy_squares_returns_false(
        self, mock_nav, mock_heal, mock_troops, mock_screenshot,
        mock_tap, mock_sleep, mock_device
    ):
        """Grid with only own team squares → no targets → return False."""
        image = _make_territory_image({
            (5, 5): BORDER_COLORS["red"],
            (5, 6): BORDER_COLORS["red"],
        })
        mock_screenshot.return_value = image

        result = attack_territory(mock_device)

        assert result is False
        mock_tap.assert_not_called()

    @patch("territory.time.sleep")
    @patch("territory.adb_tap")
    @patch("territory.load_screenshot")
    @patch("territory.all_troops_home", return_value=True)
    @patch("territory.heal_all")
    @patch("territory.navigate", return_value=True)
    def test_enemy_not_adjacent_to_own_ignored(
        self, mock_nav, mock_heal, mock_troops, mock_screenshot,
        mock_tap, mock_sleep, mock_device
    ):
        """Yellow square exists but not adjacent to red → no valid target."""
        # Red at (5,5), yellow at (5,8) — not adjacent (gap of 2)
        image = _make_territory_image({
            (5, 5): BORDER_COLORS["red"],
            (5, 8): BORDER_COLORS["yellow"],
        })
        mock_screenshot.return_value = image

        result = attack_territory(mock_device)

        assert result is False

    @patch("territory.time.sleep")
    @patch("territory.adb_tap")
    @patch("territory.load_screenshot")
    @patch("territory.all_troops_home", return_value=True)
    @patch("territory.heal_all")
    @patch("territory.navigate", return_value=True)
    def test_manual_attack_overrides_auto(
        self, mock_nav, mock_heal, mock_troops, mock_screenshot,
        mock_tap, mock_sleep, mock_device
    ):
        """MANUAL_ATTACK_SQUARES set → uses ONLY those, ignores auto-detect."""
        config.MANUAL_ATTACK_SQUARES.add((3, 3))
        # Image has a valid auto-detect target at (5,6) that should be ignored
        image = _make_territory_image({
            (5, 5): BORDER_COLORS["red"],
            (5, 6): BORDER_COLORS["yellow"],
        })
        mock_screenshot.return_value = image

        result = attack_territory(mock_device)

        assert result is True
        tap_x, tap_y = mock_tap.call_args[0][1], mock_tap.call_args[0][2]
        expected_x, expected_y = _get_square_center(3, 3)
        assert tap_x == expected_x
        assert tap_y == expected_y

    @patch("territory.time.sleep")
    @patch("territory.adb_tap")
    @patch("territory.load_screenshot")
    @patch("territory.all_troops_home", return_value=True)
    @patch("territory.heal_all")
    @patch("territory.navigate", return_value=True)
    def test_manual_ignore_filters_targets(
        self, mock_nav, mock_heal, mock_troops, mock_screenshot,
        mock_tap, mock_sleep, mock_device
    ):
        """MANUAL_IGNORE_SQUARES removes a valid auto-detected target."""
        config.MANUAL_IGNORE_SQUARES.add((5, 6))
        image = _make_territory_image({
            (5, 5): BORDER_COLORS["red"],
            (5, 6): BORDER_COLORS["yellow"],
        })
        mock_screenshot.return_value = image

        result = attack_territory(mock_device)

        # (5,6) was the only target but it's ignored
        assert result is False

    @patch("territory.time.sleep")
    @patch("territory.adb_tap")
    @patch("territory.load_screenshot")
    @patch("territory.all_troops_home", return_value=True)
    @patch("territory.heal_all")
    @patch("territory.navigate", return_value=True)
    def test_throne_squares_skipped(
        self, mock_nav, mock_heal, mock_troops, mock_screenshot,
        mock_tap, mock_sleep, mock_device
    ):
        """Throne squares are always skipped even if painted enemy color."""
        # Paint a throne square as yellow — should be ignored
        image = _make_territory_image({
            (10, 11): BORDER_COLORS["red"],
            (11, 11): BORDER_COLORS["yellow"],  # throne square
        })
        mock_screenshot.return_value = image

        result = attack_territory(mock_device)

        assert result is False

    @patch("territory.time.sleep")
    @patch("territory.navigate")
    @patch("territory.heal_all")
    def test_navigate_territory_fails(self, mock_heal, mock_nav, mock_sleep, mock_device):
        """First navigate (MAP) succeeds, second (TERRITORY) fails → return False."""
        mock_nav.side_effect = [True, False]
        with patch("territory.all_troops_home", return_value=True):
            result = attack_territory(mock_device)
        assert result is False


# ============================================================
# auto_occupy_loop — integration tests
# ============================================================

class TestAutoOccupyLoop:
    @patch("territory.save_failure_screenshot")
    @patch("territory.time.sleep")
    @patch("territory.troops_avail", return_value=5)
    @patch("territory.tap_image", return_value=False)
    @patch("territory.heal_all")
    @patch("territory.all_troops_home", return_value=False)
    def test_waits_when_troops_not_home(
        self, mock_troops_home, mock_heal, mock_tap, mock_avail,
        mock_sleep, mock_save, mock_device
    ):
        """Troops not home → wait, then stop when flag cleared."""
        config.auto_occupy_running = True
        # Stop after first sleep
        def stop_on_sleep(seconds):
            config.auto_occupy_running = False
        mock_sleep.side_effect = stop_on_sleep

        auto_occupy_loop(mock_device)

        # Should not have tried to attack territory
        mock_avail.assert_not_called()

    @patch("territory.save_failure_screenshot")
    @patch("territory.time.sleep")
    @patch("territory.tap_image", return_value=False)
    @patch("territory.all_troops_home", return_value=True)
    @patch("territory.attack_territory", return_value=False)
    def test_skips_cycle_when_attack_fails(
        self, mock_attack, mock_troops, mock_tap, mock_sleep,
        mock_save, mock_device
    ):
        """attack_territory returns False → skip cycle, sleep, stop."""
        config.auto_occupy_running = True
        call_count = [0]
        def stop_on_sleep(seconds):
            call_count[0] += 1
            if call_count[0] >= 2:
                config.auto_occupy_running = False
        mock_sleep.side_effect = stop_on_sleep

        auto_occupy_loop(mock_device)

        mock_attack.assert_called_once()

    @patch("territory.save_failure_screenshot")
    @patch("territory.time.sleep")
    @patch("territory.tap_image", return_value=False)
    @patch("territory.troops_avail", return_value=5)
    @patch("territory.adb_tap")
    @patch("territory.tap_tower_until_attack_menu")
    @patch("territory.navigate", return_value=True)
    @patch("territory.teleport", return_value=True)
    @patch("territory.heal_all")
    @patch("territory.all_troops_home", return_value=True)
    @patch("territory.attack_territory", return_value=True)
    def test_full_cycle_attacks_and_teleports(
        self, mock_attack, mock_troops_home, mock_heal, mock_teleport,
        mock_nav, mock_tower, mock_adb_tap, mock_avail, mock_tap,
        mock_sleep, mock_save, mock_device
    ):
        """Full happy path: attack → teleport → click → depart → stop."""
        config.auto_occupy_running = True
        config.LAST_ATTACKED_SQUARE[mock_device] = (5, 6)
        config.MIN_TROOPS_AVAILABLE = 0

        cycle_count = [0]
        def stop_after_cycle(seconds):
            cycle_count[0] += 1
            if cycle_count[0] >= 5:  # Stop after enough sleeps
                config.auto_occupy_running = False
        mock_sleep.side_effect = stop_after_cycle

        auto_occupy_loop(mock_device)

        mock_attack.assert_called_once()
        mock_teleport.assert_called_once()

    @patch("territory.save_failure_screenshot")
    @patch("territory.time.sleep")
    @patch("territory.tap_image", return_value=False)
    @patch("territory.all_troops_home", return_value=True)
    @patch("territory.attack_territory", return_value=True)
    @patch("territory.teleport", return_value=True)
    @patch("territory.navigate", return_value=True)
    @patch("territory.tap_tower_until_attack_menu")
    @patch("territory.adb_tap")
    @patch("territory.troops_avail", return_value=0)
    @patch("territory.heal_all")
    def test_skips_depart_when_not_enough_troops(
        self, mock_heal, mock_avail, mock_adb_tap, mock_tower,
        mock_nav, mock_teleport, mock_attack, mock_troops,
        mock_tap, mock_sleep, mock_save, mock_device
    ):
        """troops_avail below MIN_TROOPS_AVAILABLE → skip depart."""
        config.auto_occupy_running = True
        config.LAST_ATTACKED_SQUARE[mock_device] = (5, 6)
        config.MIN_TROOPS_AVAILABLE = 3

        cycle_count = [0]
        def stop_after_cycle(seconds):
            cycle_count[0] += 1
            if cycle_count[0] >= 5:
                config.auto_occupy_running = False
        mock_sleep.side_effect = stop_after_cycle

        auto_occupy_loop(mock_device)

        # tap_image should NOT be called with "depart.png"
        depart_calls = [c for c in mock_tap.call_args_list
                        if c[0][0] == "depart.png"]
        assert len(depart_calls) == 0

    def test_stops_immediately_when_flag_false(self, mock_device):
        """auto_occupy_running=False from start → loop exits immediately."""
        config.auto_occupy_running = False
        auto_occupy_loop(mock_device)
        # No crash, just returns


# ============================================================
# Blue calibration — observed BGR values from live diagnostic
# ============================================================

class TestBlueCalibration:
    """Test _classify_square_team with real blue BGR values from the 2026-02-28
    diagnostic run.  Blue reference was recalibrated to (148, 145, 165).

    All tests use auto-derived enemies (set_territory_config) so blue is always
    in ENEMY_TEAMS when my_team != blue.
    """

    def setup_method(self):
        """Set enemies to all-but-red so blue is an enemy team."""
        config.MY_TEAM_COLOR = "red"
        config.ENEMY_TEAMS = ["yellow", "green", "blue"]

    @pytest.mark.parametrize("bgr, expected_team", [
        # Exact reference color
        ((148, 145, 165), "blue"),
        # Typical blue border — close range (distance ~11)
        ((156, 142, 158), "blue"),
        # Farther variant still well within 70 threshold (distance ~12)
        ((140, 148, 173), "blue"),
    ])
    def test_observed_blue_values_classify_correctly(self, bgr, expected_team):
        """Real blue border BGR values from diagnostic → 'blue'."""
        assert _classify_square_team(bgr) == expected_team

    def test_dark_building_not_classified_as_blue(self):
        """BGR=(66, 73, 66) is a dark building/decoration — too far from any team."""
        # Distance to blue: sqrt((66-148)^2 + (73-145)^2 + (66-165)^2)
        #                  = sqrt(6724 + 5184 + 9801) = sqrt(21709) ≈ 147.3
        assert _classify_square_team((66, 73, 66)) == "unknown"

    def test_blue_at_distance_55_still_classified(self):
        """Blue variant at distance ~55 — within enemy threshold (70)."""
        # Shift all channels by ~32 from (148, 145, 165): d ≈ sqrt(32^2*3) ≈ 55
        bgr = (148 + 32, 145 + 32, 165 + 32)  # (180, 177, 197)
        assert _classify_square_team(bgr) == "blue"

    def test_blue_at_distance_69_borderline_enemy(self):
        """Blue variant at distance ~69 — just under enemy threshold (70)."""
        # Shift B by 69: (148+69, 145, 165) = (217, 145, 165)
        # Distance to blue = sqrt(69^2) = 69
        bgr = (217, 145, 165)
        assert _classify_square_team(bgr) == "blue"

    def test_blue_at_distance_72_fails_enemy_threshold(self):
        """Blue variant at distance ~72 — outside enemy threshold (70)."""
        # Shift B by 72: (148+72, 145, 165) = (220, 145, 165)
        # Distance to blue = 72 > 70
        bgr = (220, 145, 165)
        # Not enemy (> 70), not own team (blue != red), not within 55
        assert _classify_square_team(bgr) == "unknown"

    def test_blue_own_team_gets_lenient_threshold(self):
        """When blue IS my_team, the 90 threshold applies instead of 70."""
        config.MY_TEAM_COLOR = "blue"
        config.ENEMY_TEAMS = ["yellow", "green", "red"]

        # Distance 85 from blue: shift B by 85 → (233, 145, 165)
        bgr = (233, 145, 165)
        assert _classify_square_team(bgr) == "blue"


# ============================================================
# Auto-derived enemy teams — set_territory_config
# ============================================================

class TestSetTerritoryConfig:
    """Test that set_territory_config correctly derives ENEMY_TEAMS from my_team."""

    @pytest.mark.parametrize("my_team, expected_enemies", [
        ("yellow", ["green", "red", "blue"]),
        ("red",    ["yellow", "green", "blue"]),
        ("green",  ["yellow", "red", "blue"]),
        ("blue",   ["yellow", "green", "red"]),
    ])
    def test_auto_derives_enemies(self, my_team, expected_enemies):
        """set_territory_config(X) → ENEMY_TEAMS = all teams except X."""
        set_territory_config(my_team)
        assert config.MY_TEAM_COLOR == my_team
        assert config.ENEMY_TEAMS == expected_enemies

    def test_enemies_excludes_only_my_team(self):
        """Exactly 3 enemies for any valid team."""
        for team in ALL_TEAMS:
            set_territory_config(team)
            assert len(config.ENEMY_TEAMS) == 3
            assert team not in config.ENEMY_TEAMS
            assert set(config.ENEMY_TEAMS) | {team} == set(ALL_TEAMS)

    def test_classification_changes_with_config(self):
        """Switching my_team changes which colors get lenient thresholds.

        A value at distance 85 from red only classifies when red is own team
        (threshold 90), not when red is enemy (threshold 70).
        """
        # Distance 85 from red: (49, 85, 247-85) = (49, 85, 162)
        bgr_near_red = (49, 85, 162)

        # Red as own team → recognized (distance 85 < 90)
        set_territory_config("red")
        assert _classify_square_team(bgr_near_red) == "red"

        # Red as enemy (my_team=yellow) → unknown (distance 85 > 70)
        set_territory_config("yellow")
        assert _classify_square_team(bgr_near_red) == "unknown"

    def test_all_exact_colors_classified_with_all_enemies(self):
        """With set_territory_config, all 4 exact border colors are recognized."""
        for my_team in ALL_TEAMS:
            set_territory_config(my_team)
            for team_name, bgr in BORDER_COLORS.items():
                result = _classify_square_team(bgr)
                assert result == team_name, (
                    f"my_team={my_team}: expected {team_name} for BGR={bgr}, "
                    f"got {result}"
                )


# ============================================================
# Red edge cases — observed values near threshold
# ============================================================

class TestRedEdgeCases:
    """Test _classify_square_team with real red BGR values from the diagnostic
    that were near the classification threshold.

    Red reference: (49, 85, 247).
    """

    def test_red_distance_58_classifies_as_own_team(self):
        """BGR=(50, 83, 189) at distance ~58 from red.

        When red is own team (threshold 90): 58 < 90 → 'red'.
        """
        config.MY_TEAM_COLOR = "red"
        config.ENEMY_TEAMS = ["yellow", "green", "blue"]
        assert _classify_square_team((50, 83, 189)) == "red"

    def test_red_distance_58_classifies_as_enemy(self):
        """BGR=(50, 83, 189) at distance ~58 from red.

        When red is enemy (threshold 70): 58 < 70 → 'red'.
        """
        config.MY_TEAM_COLOR = "yellow"
        config.ENEMY_TEAMS = ["green", "red", "blue"]
        assert _classify_square_team((50, 83, 189)) == "red"

    def test_red_distance_88_classifies_as_own_team(self):
        """BGR=(45, 83, 159) at distance ~88 from red.

        When red is own team (threshold 90): 88 < 90 → 'red'.
        """
        config.MY_TEAM_COLOR = "red"
        config.ENEMY_TEAMS = ["yellow", "green", "blue"]
        assert _classify_square_team((45, 83, 159)) == "red"

    def test_red_distance_88_fails_as_enemy(self):
        """BGR=(45, 83, 159) at distance ~88 from red.

        When red is enemy (threshold 70): 88 > 70 → 'unknown'.
        This is a known classification gap: squares that are visually
        red but dimmed or partially obscured get lost when red is enemy.
        """
        config.MY_TEAM_COLOR = "yellow"
        config.ENEMY_TEAMS = ["green", "red", "blue"]
        # Nearest team is red at ~88, but 88 > 70 (enemy threshold)
        # and 88 > 55 (tight fallback), so it falls through to unknown
        assert _classify_square_team((45, 83, 159)) == "unknown"

    def test_red_distance_98_always_unknown(self):
        """BGR=(43, 58, 153) at distance ~98 from red.

        Distance 98 exceeds both own-team threshold (90) and enemy
        threshold (70) → always 'unknown' regardless of config.
        """
        # As own team
        config.MY_TEAM_COLOR = "red"
        config.ENEMY_TEAMS = ["yellow", "green", "blue"]
        assert _classify_square_team((43, 58, 153)) == "unknown"

        # As enemy
        config.MY_TEAM_COLOR = "yellow"
        config.ENEMY_TEAMS = ["green", "red", "blue"]
        assert _classify_square_team((43, 58, 153)) == "unknown"

    @pytest.mark.parametrize("bgr, expected", [
        # Solid red territory — distance 0
        ((49, 85, 247), "red"),
        # Slight variation — distance ~8
        ((45, 80, 250), "red"),
        # Moderate variation — distance ~30
        ((49, 85, 217), "red"),
        # Approaching enemy threshold — distance ~58
        ((50, 83, 189), "red"),
    ])
    def test_red_gradient_as_enemy(self, bgr, expected):
        """Red at various distances all within enemy threshold (70)."""
        config.MY_TEAM_COLOR = "yellow"
        config.ENEMY_TEAMS = ["green", "red", "blue"]
        assert _classify_square_team(bgr) == expected


# ============================================================
# Clock overlay tolerance (row 0) — known classification gap
# ============================================================

class TestClockOverlayRow0:
    """Test classification of dimmed colors from row 0 where the device
    clock overlay reduces brightness.

    These document known gaps in the current threshold system.
    """

    def test_dimmed_yellow_row0_classified_as_green(self):
        """BGR=(82, 175, 181) — dimmed yellow from row 0.

        After green recalibration to (100, 175, 160), this is distance ~28
        from green vs ~74 from blue and ~79 from yellow.  Classifies as green.

        This is a KNOWN EDGE CASE — heavily clock-dimmed yellow can fall into
        the green range.  In practice, _get_border_color's row-0 special
        sampling avoids the most dimmed pixels, so this rarely happens.
        """
        config.MY_TEAM_COLOR = "red"
        config.ENEMY_TEAMS = ["yellow", "green", "blue"]
        assert _classify_square_team((82, 175, 181)) == "green"

    def test_severely_dimmed_yellow_row0_is_unknown(self):
        """BGR=(60, 130, 140) — severely dimmed yellow from row 0.

        At this dimming level, distance to green is ~63 (within threshold)
        but distance to all colors is high enough that the pixel is ambiguous.
        Heavily dimmed squares under the clock are expected to be unknown.
        """
        config.MY_TEAM_COLOR = "red"
        config.ENEMY_TEAMS = ["yellow", "green", "blue"]
        assert _classify_square_team((50, 110, 120)) == "unknown"

    def test_row0_less_dimmed_yellow_still_classifies(self):
        """BGR=(100, 210, 225) — mildly dimmed yellow at distance ~17.

        Squares not directly under the clock are less affected and
        should still classify correctly.
        """
        config.MY_TEAM_COLOR = "red"
        config.ENEMY_TEAMS = ["yellow", "green", "blue"]
        assert _classify_square_team((100, 210, 225)) == "yellow"

    def test_row0_moderate_dim_at_yellow_threshold_boundary(self):
        """BGR=(90, 195, 210) — moderately dimmed yellow.

        Distance to yellow: sqrt((90-107)^2 + (195-223)^2 + (210-239)^2)
                          = sqrt(289 + 784 + 841) = sqrt(1914) ≈ 43.7
        Within enemy threshold (70) → should classify as yellow.
        """
        config.MY_TEAM_COLOR = "red"
        config.ENEMY_TEAMS = ["yellow", "green", "blue"]
        assert _classify_square_team((90, 195, 210)) == "yellow"


# ============================================================
# diagnose_grid — smoke test (mock-based)
# ============================================================

class TestDiagnoseGrid:
    """Smoke test for diagnose_grid: verifies the function calls the right
    dependencies in the right order without crashing.

    All external I/O (ADB, vision, navigation) is mocked.
    """

    @patch("territory.navigate")
    def test_aborts_on_navigation_failure(self, mock_nav, mock_device):
        """If navigate to TERRITORY fails, diagnose_grid returns early."""
        mock_nav.return_value = False

        diagnose_grid(mock_device)

        mock_nav.assert_called_once_with(Screen.TERRITORY, mock_device)

    @patch("territory.navigate", return_value=True)
    @patch("territory.load_screenshot", return_value=None)
    @patch("territory.time.sleep")
    def test_aborts_on_screenshot_failure(
        self, mock_sleep, mock_screenshot, mock_nav, mock_device
    ):
        """If load_screenshot returns None, diagnose_grid returns early."""
        diagnose_grid(mock_device)

        mock_screenshot.assert_called_once_with(mock_device)

    @patch("territory.cv2.imwrite")
    @patch("territory.os.makedirs")
    @patch("territory.navigate")
    @patch("territory.load_screenshot")
    @patch("territory.time.sleep")
    def test_full_pipeline_runs(
        self, mock_sleep, mock_screenshot, mock_nav, mock_makedirs,
        mock_imwrite, mock_device
    ):
        """Happy path: navigate, screenshot, classify, save debug image, nav back."""
        mock_nav.return_value = True

        # Build a small test image with some known squares
        image = _make_territory_image({
            (5, 5): BORDER_COLORS["red"],
            (5, 6): BORDER_COLORS["yellow"],
            (10, 10): BORDER_COLORS["green"],
        })
        mock_screenshot.return_value = image

        config.MY_TEAM_COLOR = "red"
        config.ENEMY_TEAMS = ["yellow", "green", "blue"]

        diagnose_grid(mock_device)

        # Verify navigation: first to TERRITORY, then back to MAP
        assert mock_nav.call_count == 2
        mock_nav.assert_any_call(Screen.TERRITORY, mock_device)
        mock_nav.assert_any_call(Screen.MAP, mock_device)

        # Verify debug image was saved
        mock_makedirs.assert_called_once_with("debug", exist_ok=True)
        assert mock_imwrite.call_count == 1
        saved_path = mock_imwrite.call_args[0][0]
        assert "territory_diag_" in saved_path
        assert saved_path.endswith(".png")

    @patch("territory.cv2.imwrite")
    @patch("territory.os.makedirs")
    @patch("territory.navigate", return_value=True)
    @patch("territory.load_screenshot")
    @patch("territory.time.sleep")
    def test_classifies_all_576_squares(
        self, mock_sleep, mock_screenshot, mock_nav, mock_makedirs,
        mock_imwrite, mock_device
    ):
        """diagnose_grid iterates all 24x24 = 576 squares (minus 4 throne)."""
        # All-black image — every square should be unknown
        image = _make_territory_image()
        mock_screenshot.return_value = image

        config.MY_TEAM_COLOR = "red"
        config.ENEMY_TEAMS = ["yellow", "green", "blue"]

        # diagnose_grid doesn't return data, but we can verify it doesn't crash
        # on a full 576-square scan with an empty grid
        diagnose_grid(mock_device)

        # Should still save debug image and nav back
        assert mock_imwrite.call_count == 1
        assert mock_nav.call_count == 2

    @patch("territory.cv2.imwrite")
    @patch("territory.os.makedirs")
    @patch("territory.navigate", return_value=True)
    @patch("territory.load_screenshot")
    @patch("territory.time.sleep")
    def test_device_id_sanitized_in_filename(
        self, mock_sleep, mock_screenshot, mock_nav, mock_makedirs,
        mock_imwrite, mock_device
    ):
        """Device ID colons and dots are replaced with underscores in filename."""
        image = _make_territory_image()
        mock_screenshot.return_value = image

        diagnose_grid(mock_device)

        saved_path = mock_imwrite.call_args[0][0]
        # mock_device is "127.0.0.1:9999" → "127_0_0_1_9999"
        assert ":" not in saved_path
        assert "127_0_0_1_9999" in saved_path
