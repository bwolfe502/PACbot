"""Tests for troop detection and status tracking (troops.py).

Tests the pixel-based troop counting logic by constructing synthetic
screenshots with yellow pixels planted at known Y positions,
plus unit tests for the troop status data model, parser, and classifier,
icon template matching, portrait tracking, and triangle detection.
"""

import time
import numpy as np
import cv2
from unittest.mock import patch, MagicMock

import config
from troops import (troops_avail, all_troops_home, _TROOP_X, _TROOP_COLOR, _SLOT_PATTERNS,
                    TroopAction, TroopStatus, DeviceTroopSnapshot,
                    _parse_timer, _classify_action,
                    _store_snapshot, _get_snapshot, get_troop_status,
                    next_troop_free_in, is_any_troop_doing,
                    _match_status_icon, _CARD_HEIGHT, _ICON_MATCH_THRESHOLD,
                    read_panel_statuses,
                    capture_portrait, store_portrait, identify_troop,
                    _portraits, _portrait_lock,
                    detect_selected_troop, capture_departing_portrait,
                    _DEPART_TRIANGLE_X1, _DEPART_TRIANGLE_X2,
                    _DEPART_SLOT_Y_POSITIONS)


def _make_screen_with_yellow_at(y_positions):
    """Create a synthetic 1920x1080 BGR screenshot with yellow pixels at given Y coords."""
    screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
    for y in y_positions:
        screen[y, _TROOP_X] = _TROOP_COLOR
    return screen


class TestTroopsAvail:
    """Test troop counting with mocked screenshots."""

    @patch("troops.get_template", return_value=None)  # Skip map_screen check
    @patch("troops.load_screenshot")
    def test_zero_troops_all_occupied(self, mock_screenshot, mock_template):
        # Pattern 0: all 5 slots occupied → 0 troops available
        screen = _make_screen_with_yellow_at([640, 800, 960, 1110, 1270])
        mock_screenshot.return_value = screen
        assert troops_avail("dev1") == 0

    @patch("troops.get_template", return_value=None)
    @patch("troops.load_screenshot")
    def test_one_troop(self, mock_screenshot, mock_template):
        screen = _make_screen_with_yellow_at([720, 880, 1040, 1200])
        mock_screenshot.return_value = screen
        assert troops_avail("dev1") == 1

    @patch("troops.get_template", return_value=None)
    @patch("troops.load_screenshot")
    def test_two_troops(self, mock_screenshot, mock_template):
        screen = _make_screen_with_yellow_at([800, 960, 1110])
        mock_screenshot.return_value = screen
        assert troops_avail("dev1") == 2

    @patch("troops.get_template", return_value=None)
    @patch("troops.load_screenshot")
    def test_three_troops(self, mock_screenshot, mock_template):
        screen = _make_screen_with_yellow_at([880, 1040])
        mock_screenshot.return_value = screen
        assert troops_avail("dev1") == 3

    @patch("troops.get_template", return_value=None)
    @patch("troops.load_screenshot")
    def test_four_troops(self, mock_screenshot, mock_template):
        screen = _make_screen_with_yellow_at([960])
        mock_screenshot.return_value = screen
        assert troops_avail("dev1") == 4

    @patch("troops.get_template", return_value=None)
    @patch("troops.load_screenshot")
    def test_five_troops_no_match(self, mock_screenshot, mock_template):
        # No yellow pixels at any pattern position → falls through to 5
        screen = _make_screen_with_yellow_at([])
        mock_screenshot.return_value = screen
        assert troops_avail("dev1") == 5

    @patch("troops.get_template", return_value=None)
    @patch("troops.load_screenshot")
    def test_screenshot_failure(self, mock_screenshot, mock_template):
        mock_screenshot.return_value = None
        assert troops_avail("dev1") == 0


class TestTroopsAvailOffset:
    """Test the offset math for accounts with fewer than 5 total troops."""

    @patch("troops.get_template", return_value=None)
    @patch("troops.load_screenshot")
    def test_four_troop_account_all_home(self, mock_screenshot, mock_template):
        config.DEVICE_TOTAL_TROOPS["dev1"] = 4
        # Pattern 1 (raw=1) means 4 slots occupied; offset=1 → adjusted = max(0, 1-1) = 0
        screen = _make_screen_with_yellow_at([720, 880, 1040, 1200])
        mock_screenshot.return_value = screen
        assert troops_avail("dev1") == 0
        config.DEVICE_TOTAL_TROOPS.pop("dev1", None)

    @patch("troops.get_template", return_value=None)
    @patch("troops.load_screenshot")
    def test_four_troop_account_one_available(self, mock_screenshot, mock_template):
        config.DEVICE_TOTAL_TROOPS["dev1"] = 4
        # Pattern 2 (raw=2): offset=1 → adjusted = max(0, 2-1) = 1
        screen = _make_screen_with_yellow_at([800, 960, 1110])
        mock_screenshot.return_value = screen
        assert troops_avail("dev1") == 1
        config.DEVICE_TOTAL_TROOPS.pop("dev1", None)


class TestAllTroopsHome:
    @patch("troops.troops_avail")
    def test_all_home_default_5(self, mock_avail):
        config.DEVICE_TOTAL_TROOPS.pop("dev1", None)
        mock_avail.return_value = 5
        assert all_troops_home("dev1") is True

    @patch("troops.troops_avail")
    def test_not_all_home(self, mock_avail):
        config.DEVICE_TOTAL_TROOPS.pop("dev1", None)
        mock_avail.return_value = 3
        assert all_troops_home("dev1") is False

    @patch("troops.troops_avail")
    def test_custom_total(self, mock_avail):
        config.DEVICE_TOTAL_TROOPS["dev1"] = 4
        mock_avail.return_value = 4
        assert all_troops_home("dev1") is True
        config.DEVICE_TOTAL_TROOPS.pop("dev1", None)


# ============================================================
# TIMER PARSER TESTS
# ============================================================

class TestParseTimer:
    def test_mm_ss(self):
        assert _parse_timer("5:23") == 323

    def test_mm_ss_leading_zero(self):
        assert _parse_timer("05:03") == 303

    def test_h_mm_ss(self):
        assert _parse_timer("1:05:00") == 3900

    def test_zero(self):
        assert _parse_timer("00:00") == 0

    def test_large_timer(self):
        assert _parse_timer("2:30:45") == 9045

    def test_garbage(self):
        assert _parse_timer("abc") is None

    def test_empty(self):
        assert _parse_timer("") is None

    def test_whitespace(self):
        assert _parse_timer("  5:23  ") == 323

    def test_partial(self):
        assert _parse_timer("5") is None


# ============================================================
# ACTION CLASSIFIER TESTS
# ============================================================

class TestClassifyAction:
    def test_rallying(self):
        assert _classify_action("Rallying") == TroopAction.RALLYING

    def test_defending_lowercase(self):
        assert _classify_action("defending") == TroopAction.DEFENDING

    def test_gathering_mixed_case(self):
        assert _classify_action("Gathering") == TroopAction.GATHERING

    def test_marching(self):
        assert _classify_action("Marching") == TroopAction.MARCHING

    def test_returning(self):
        assert _classify_action("Returning") == TroopAction.RETURNING

    def test_occupying(self):
        assert _classify_action("Occupying") == TroopAction.OCCUPYING

    def test_stationing(self):
        assert _classify_action("Stationing") == TroopAction.STATIONING

    def test_battling(self):
        assert _classify_action("Battling") == TroopAction.BATTLING

    def test_partial_keyword_rally(self):
        assert _classify_action("rally something") == TroopAction.RALLYING

    def test_unknown(self):
        assert _classify_action("xyzzy") is None

    def test_empty(self):
        assert _classify_action("") is None


# ============================================================
# TROOP STATUS DATA MODEL TESTS
# ============================================================

class TestTroopStatus:
    def test_home_troop(self):
        t = TroopStatus(action=TroopAction.HOME)
        assert t.is_home is True
        assert t.deadline is None
        assert t.time_left is None

    def test_deployed_troop(self):
        now = time.time()
        t = TroopStatus(action=TroopAction.RALLYING, seconds_remaining=300, read_at=now)
        assert t.is_home is False
        assert t.deadline is not None
        assert abs(t.deadline - (now + 300)) < 1
        assert t.time_left is not None
        assert 299 <= t.time_left <= 300

    def test_time_left_decays(self):
        t = TroopStatus(action=TroopAction.GATHERING, seconds_remaining=5,
                        read_at=time.time() - 3)
        # 5 seconds remaining, read 3 seconds ago → ~2 seconds left
        assert t.time_left is not None
        assert 1 <= t.time_left <= 3

    def test_time_left_past_deadline(self):
        t = TroopStatus(action=TroopAction.MARCHING, seconds_remaining=10,
                        read_at=time.time() - 20)
        # 10 seconds remaining, read 20 seconds ago → past deadline
        assert t.time_left == 0

    def test_repr_home(self):
        t = TroopStatus(action=TroopAction.HOME)
        assert "HOME" in repr(t)

    def test_repr_deployed(self):
        t = TroopStatus(action=TroopAction.RALLYING, seconds_remaining=300)
        r = repr(t)
        assert "Rallying" in r
        assert "left" in r


class TestDeviceTroopSnapshot:
    def _make_snapshot(self):
        now = time.time()
        return DeviceTroopSnapshot(
            device="dev1",
            troops=[
                TroopStatus(action=TroopAction.HOME, read_at=now),
                TroopStatus(action=TroopAction.HOME, read_at=now),
                TroopStatus(action=TroopAction.RALLYING, seconds_remaining=120, read_at=now),
                TroopStatus(action=TroopAction.GATHERING, seconds_remaining=300, read_at=now),
                TroopStatus(action=TroopAction.DEFENDING, seconds_remaining=600, read_at=now),
            ],
            read_at=now,
        )

    def test_home_count(self):
        s = self._make_snapshot()
        assert s.home_count == 2

    def test_deployed_count(self):
        s = self._make_snapshot()
        assert s.deployed_count == 3

    def test_any_doing_rallying(self):
        s = self._make_snapshot()
        assert s.any_doing(TroopAction.RALLYING) is True

    def test_any_doing_battling(self):
        s = self._make_snapshot()
        assert s.any_doing(TroopAction.BATTLING) is False

    def test_troops_by_action(self):
        s = self._make_snapshot()
        home = s.troops_by_action(TroopAction.HOME)
        assert len(home) == 2

    def test_soonest_free(self):
        s = self._make_snapshot()
        soonest = s.soonest_free()
        assert soonest is not None
        assert soonest.action == TroopAction.RALLYING  # 120s < 300s < 600s

    def test_soonest_free_all_home(self):
        now = time.time()
        s = DeviceTroopSnapshot(
            device="dev1",
            troops=[TroopStatus(action=TroopAction.HOME, read_at=now)],
            read_at=now,
        )
        assert s.soonest_free() is None

    def test_age_seconds(self):
        s = DeviceTroopSnapshot(device="dev1", troops=[], read_at=time.time() - 10)
        assert 9 <= s.age_seconds <= 11


# ============================================================
# STORAGE AND QUERY API TESTS
# ============================================================

class TestStorageAndAPI:
    def _cleanup(self):
        # Clear stored state for test device
        from troops import _troop_status, _troop_status_lock
        with _troop_status_lock:
            _troop_status.pop("test_dev", None)

    def test_store_and_get(self):
        self._cleanup()
        now = time.time()
        snapshot = DeviceTroopSnapshot(
            device="test_dev",
            troops=[TroopStatus(action=TroopAction.HOME, read_at=now)],
            read_at=now,
        )
        _store_snapshot("test_dev", snapshot)
        result = _get_snapshot("test_dev")
        assert result is snapshot
        self._cleanup()

    def test_get_missing_device(self):
        self._cleanup()
        assert _get_snapshot("nonexistent_dev") is None

    def test_get_troop_status_returns_cached(self):
        self._cleanup()
        now = time.time()
        snapshot = DeviceTroopSnapshot(
            device="test_dev",
            troops=[TroopStatus(action=TroopAction.HOME, read_at=now)],
            read_at=now,
        )
        _store_snapshot("test_dev", snapshot)
        assert get_troop_status("test_dev") is snapshot
        self._cleanup()

    def test_next_troop_free_in_home(self):
        self._cleanup()
        now = time.time()
        _store_snapshot("test_dev", DeviceTroopSnapshot(
            device="test_dev",
            troops=[TroopStatus(action=TroopAction.HOME, read_at=now)],
            read_at=now,
        ))
        assert next_troop_free_in("test_dev") == 0
        self._cleanup()

    def test_next_troop_free_in_deployed(self):
        self._cleanup()
        now = time.time()
        _store_snapshot("test_dev", DeviceTroopSnapshot(
            device="test_dev",
            troops=[
                TroopStatus(action=TroopAction.RALLYING, seconds_remaining=60, read_at=now),
                TroopStatus(action=TroopAction.DEFENDING, seconds_remaining=300, read_at=now),
            ],
            read_at=now,
        ))
        result = next_troop_free_in("test_dev")
        assert result is not None
        assert 59 <= result <= 60
        self._cleanup()

    def test_next_troop_free_in_no_data(self):
        self._cleanup()
        assert next_troop_free_in("test_dev") is None

    def test_is_any_troop_doing(self):
        self._cleanup()
        now = time.time()
        _store_snapshot("test_dev", DeviceTroopSnapshot(
            device="test_dev",
            troops=[
                TroopStatus(action=TroopAction.RALLYING, seconds_remaining=60, read_at=now),
            ],
            read_at=now,
        ))
        assert is_any_troop_doing("test_dev", TroopAction.RALLYING) is True
        assert is_any_troop_doing("test_dev", TroopAction.BATTLING) is False
        self._cleanup()

    def test_is_any_troop_doing_no_data(self):
        self._cleanup()
        assert is_any_troop_doing("test_dev", TroopAction.RALLYING) is None


# ============================================================
# ADVENTURING CLASSIFIER TEST
# ============================================================

class TestClassifyAdventuring:
    def test_adventuring(self):
        assert _classify_action("Adventuring") == TroopAction.ADVENTURING

    def test_adventur_partial(self):
        assert _classify_action("adventur") == TroopAction.ADVENTURING


# ============================================================
# STATUS ICON MATCHING TESTS
# ============================================================

def _make_card_with_icon(icon_img, icon_y=3, icon_x=130):
    """Create a synthetic card image with a small icon placed at the given offset."""
    card = np.zeros((_CARD_HEIGHT, 170, 3), dtype=np.uint8)
    h, w = icon_img.shape[:2]
    card[icon_y:icon_y + h, icon_x:icon_x + w] = icon_img
    return card


class TestMatchStatusIcon:
    """Test _match_status_icon with synthetic templates."""

    def _make_icon(self, color=(200, 100, 50)):
        """Create a small 21x22 synthetic icon."""
        icon = np.zeros((21, 22, 3), dtype=np.uint8)
        icon[:] = color
        return icon

    @patch("troops._status_templates", {})
    @patch("troops._status_templates_loaded", True)
    def test_no_templates_returns_none(self):
        card = np.zeros((_CARD_HEIGHT, 170, 3), dtype=np.uint8)
        action, score = _match_status_icon(card)
        assert action is None

    @patch("troops._status_templates_loaded", True)
    def test_perfect_match(self):
        icon = self._make_icon((180, 60, 30))
        card = _make_card_with_icon(icon)
        with patch("troops._status_templates", {TroopAction.RALLYING: icon}):
            action, score = _match_status_icon(card)
            assert action == TroopAction.RALLYING
            assert score > 0.99

    @patch("troops._status_templates_loaded", True)
    def test_best_of_multiple(self):
        icon_rally = self._make_icon((180, 60, 30))
        icon_march = self._make_icon((50, 200, 100))
        # Place the rally icon in the card
        card = _make_card_with_icon(icon_rally)
        templates = {
            TroopAction.RALLYING: icon_rally,
            TroopAction.MARCHING: icon_march,
        }
        with patch("troops._status_templates", templates):
            action, score = _match_status_icon(card)
            assert action == TroopAction.RALLYING

    @patch("troops._status_templates_loaded", True)
    def test_below_threshold_returns_none(self):
        # Use a patterned icon (gradient) so TM_CCOEFF_NORMED has variance to work with
        icon = np.zeros((21, 22, 3), dtype=np.uint8)
        for i in range(21):
            icon[i, :] = (i * 10, 200 - i * 5, i * 8)
        # Card with very different pattern — noise
        rng = np.random.RandomState(42)
        card = rng.randint(0, 50, (_CARD_HEIGHT, 170, 3), dtype=np.uint8)
        with patch("troops._status_templates", {TroopAction.RALLYING: icon}):
            action, score = _match_status_icon(card)
            assert action is None
            assert score < _ICON_MATCH_THRESHOLD


# ============================================================
# READ PANEL STATUSES TESTS
# ============================================================

class TestReadPanelStatuses:
    """Test read_panel_statuses with synthetic screenshots and mocked templates."""

    def _make_panel_screen(self, avail_count, icon_imgs=None):
        """Build a synthetic 1920x1080 screenshot with yellow pixels at the right
        pattern positions and optional icon images placed in card regions."""
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        # Place yellow pixels for the avail_count pattern
        pattern = _SLOT_PATTERNS[avail_count]
        for y in pattern["match"]:
            screen[y, _TROOP_X] = _TROOP_COLOR
        # Place icons in each deployed card
        if icon_imgs:
            for i, (mid_y, icon) in enumerate(zip(pattern["match"], icon_imgs)):
                card_top = mid_y - _CARD_HEIGHT // 2
                h, w = icon.shape[:2]
                screen[card_top + 3:card_top + 3 + h, 10 + 130:10 + 130 + w] = icon
        return screen

    @patch("troops.get_template", return_value=None)  # Skip map_screen check
    @patch("troops._status_templates_loaded", True)
    def test_all_home(self, mock_tpl):
        """All 5 troops home → snapshot with 5 HOME statuses."""
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)  # No yellow pixels
        config.DEVICE_TOTAL_TROOPS.pop("test_panel", None)
        result = read_panel_statuses("test_panel", screen=screen)
        assert result is not None
        assert result.home_count == 5
        assert len(result.troops) == 5
        assert all(t.is_home for t in result.troops)

    @patch("troops.get_template", return_value=None)
    @patch("troops._status_templates_loaded", True)
    def test_three_deployed(self, mock_tpl):
        """2 available, 3 deployed with recognizable icons."""
        icon = np.full((21, 22, 3), (180, 60, 30), dtype=np.uint8)
        templates = {TroopAction.GATHERING: icon}
        config.DEVICE_TOTAL_TROOPS.pop("test_panel", None)
        screen = self._make_panel_screen(2, icon_imgs=[icon, icon, icon])
        with patch("troops._status_templates", templates):
            result = read_panel_statuses("test_panel", screen=screen)
        assert result is not None
        assert result.deployed_count == 3
        assert result.home_count == 2
        deployed = [t for t in result.troops if not t.is_home]
        assert all(t.action == TroopAction.GATHERING for t in deployed)

    @patch("troops.get_template", return_value=None)
    @patch("troops._status_templates_loaded", True)
    def test_unknown_icon_defaults_to_marching(self, mock_tpl):
        """Unrecognized icon falls back to MARCHING."""
        config.DEVICE_TOTAL_TROOPS.pop("test_panel", None)
        screen = self._make_panel_screen(4)  # 1 deployed, no icon placed
        with patch("troops._status_templates", {}):
            result = read_panel_statuses("test_panel", screen=screen)
        assert result is not None
        deployed = [t for t in result.troops if not t.is_home]
        assert len(deployed) == 1
        assert deployed[0].action == TroopAction.MARCHING

    @patch("troops.get_template", return_value=None)
    @patch("troops._status_templates_loaded", True)
    def test_screenshot_none(self, mock_tpl):
        result = read_panel_statuses("test_panel", screen=None)
        # load_screenshot is called, but we didn't mock it → returns None
        # Actually it will try to call real ADB. Let's mock it.
        assert result is None

    @patch("troops.load_screenshot", return_value=None)
    @patch("troops.get_template", return_value=None)
    @patch("troops._status_templates_loaded", True)
    def test_screenshot_fails(self, mock_tpl, mock_ss):
        result = read_panel_statuses("test_panel")
        assert result is None

    @patch("troops.get_template", return_value=None)
    @patch("troops._status_templates_loaded", True)
    def test_stores_snapshot(self, mock_tpl):
        """Verify snapshot is stored and retrievable via get_troop_status."""
        from troops import _troop_status, _troop_status_lock
        with _troop_status_lock:
            _troop_status.pop("test_panel_store", None)
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        config.DEVICE_TOTAL_TROOPS.pop("test_panel_store", None)
        with patch("troops._status_templates", {}):
            result = read_panel_statuses("test_panel_store", screen=screen)
        cached = get_troop_status("test_panel_store")
        assert cached is result
        with _troop_status_lock:
            _troop_status.pop("test_panel_store", None)


# ============================================================
# PORTRAIT TRACKING TESTS
# ============================================================

class TestPortraitTracking:
    def _cleanup(self):
        with _portrait_lock:
            _portraits.pop("test_dev", None)

    def test_capture_portrait_crops_correct_region(self):
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        # Put a distinctive color in the portrait zone
        card_top = 500
        screen[card_top + 5:card_top + 65, 45:120] = (100, 150, 200)
        portrait = capture_portrait(screen, card_top)
        assert portrait.shape == (60, 75, 3)
        assert np.all(portrait == (100, 150, 200))

    def test_store_and_identify_roundtrip(self):
        self._cleanup()
        portrait = np.random.randint(0, 256, (60, 75, 3), dtype=np.uint8)
        store_portrait("test_dev", 1, portrait)
        # Identify the same portrait
        result = identify_troop("test_dev", portrait)
        assert result == 1
        self._cleanup()

    def test_identify_different_portrait_no_match(self):
        self._cleanup()
        # Use patterned images so TM_CCOEFF_NORMED has variance
        rng = np.random.RandomState(10)
        portrait_a = rng.randint(0, 256, (60, 75, 3), dtype=np.uint8)
        rng2 = np.random.RandomState(99)
        portrait_b = rng2.randint(0, 256, (60, 75, 3), dtype=np.uint8)
        store_portrait("test_dev", 1, portrait_a)
        result = identify_troop("test_dev", portrait_b)
        assert result is None
        self._cleanup()

    def test_identify_no_stored_portraits(self):
        self._cleanup()
        portrait = np.zeros((60, 75, 3), dtype=np.uint8)
        result = identify_troop("test_dev", portrait)
        assert result is None

    def test_identify_best_match(self):
        self._cleanup()
        # Use patterned images with distinct random seeds
        rng1 = np.random.RandomState(10)
        p1 = rng1.randint(0, 256, (60, 75, 3), dtype=np.uint8)
        rng2 = np.random.RandomState(99)
        p2 = rng2.randint(0, 256, (60, 75, 3), dtype=np.uint8)
        store_portrait("test_dev", 1, p1)
        store_portrait("test_dev", 2, p2)
        # Query with p2 — should match slot 2
        result = identify_troop("test_dev", p2)
        assert result == 2
        self._cleanup()

    def test_identify_shape_mismatch_skipped(self):
        self._cleanup()
        p_stored = np.zeros((60, 75, 3), dtype=np.uint8)
        p_query = np.zeros((30, 40, 3), dtype=np.uint8)
        store_portrait("test_dev", 1, p_stored)
        result = identify_troop("test_dev", p_query)
        assert result is None
        self._cleanup()


# ============================================================
# TRIANGLE DETECTION TESTS
# ============================================================

class TestDetectSelectedTroop:
    def _make_screen_with_triangle(self, center_y):
        """Create a synthetic screen with a white triangle cluster at center_y."""
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        # Place white pixels at x=195-215, spanning ~30px around center_y
        for y in range(center_y - 15, center_y + 15):
            for x in range(_DEPART_TRIANGLE_X1 + 5, _DEPART_TRIANGLE_X2 - 5):
                screen[y, x] = (255, 255, 255)
        return screen

    @patch("troops.load_screenshot")
    def test_troop_1(self, mock_ss):
        screen = self._make_screen_with_triangle(_DEPART_SLOT_Y_POSITIONS[0])
        mock_ss.return_value = screen
        result = detect_selected_troop("test_dev", screen=screen)
        assert result == 1

    @patch("troops.load_screenshot")
    def test_troop_3(self, mock_ss):
        screen = self._make_screen_with_triangle(_DEPART_SLOT_Y_POSITIONS[2])
        mock_ss.return_value = screen
        result = detect_selected_troop("test_dev", screen=screen)
        assert result == 3

    @patch("troops.load_screenshot")
    def test_troop_5(self, mock_ss):
        screen = self._make_screen_with_triangle(_DEPART_SLOT_Y_POSITIONS[4])
        mock_ss.return_value = screen
        result = detect_selected_troop("test_dev", screen=screen)
        assert result == 5

    @patch("troops.load_screenshot")
    def test_no_triangle(self, mock_ss):
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        mock_ss.return_value = screen
        result = detect_selected_troop("test_dev", screen=screen)
        assert result is None

    @patch("troops.load_screenshot", return_value=None)
    def test_no_screenshot(self, mock_ss):
        result = detect_selected_troop("test_dev")
        assert result is None

    @patch("troops.load_screenshot")
    def test_off_white_triangle(self, mock_ss):
        """Triangle with off-white pixels (e.g. 230,230,230) still detected."""
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        center_y = _DEPART_SLOT_Y_POSITIONS[1]
        for y in range(center_y - 15, center_y + 15):
            for x in range(_DEPART_TRIANGLE_X1 + 5, _DEPART_TRIANGLE_X2 - 5):
                screen[y, x] = (230, 230, 230)
        result = detect_selected_troop("test_dev", screen=screen)
        assert result == 2


class TestCaptureDepartingPortrait:
    def _cleanup(self):
        with _portrait_lock:
            _portraits.pop("test_dev", None)

    @patch("troops.load_screenshot")
    def test_captures_and_stores(self, mock_ss):
        self._cleanup()
        center_y = _DEPART_SLOT_Y_POSITIONS[0]
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        # White triangle
        for y in range(center_y - 15, center_y + 15):
            for x in range(_DEPART_TRIANGLE_X1 + 5, _DEPART_TRIANGLE_X2 - 5):
                screen[y, x] = (255, 255, 255)
        # Distinctive portrait in the portrait zone
        card_top = center_y - _CARD_HEIGHT // 2
        screen[card_top + 5:card_top + 65, 45:120] = (50, 100, 150)
        mock_ss.return_value = screen

        result = capture_departing_portrait("test_dev", screen=screen)
        assert result is not None
        slot_id, portrait = result
        assert slot_id == 1
        assert portrait.shape == (60, 75, 3)
        assert np.all(portrait == (50, 100, 150))
        # Verify it was stored
        stored = identify_troop("test_dev", portrait)
        assert stored == 1
        self._cleanup()

    @patch("troops.load_screenshot")
    def test_no_triangle_returns_none(self, mock_ss):
        screen = np.zeros((1920, 1080, 3), dtype=np.uint8)
        mock_ss.return_value = screen
        result = capture_departing_portrait("test_dev", screen=screen)
        assert result is None
