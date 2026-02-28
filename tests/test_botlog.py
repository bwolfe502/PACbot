"""Tests for StatsTracker, timed_action, and get_logger (botlog.py)."""

import logging
import pytest
from unittest.mock import patch, MagicMock

from botlog import StatsTracker, timed_action, get_logger, stats


# ============================================================
# StatsTracker
# ============================================================

class TestStatsTrackerRecordAction:
    def setup_method(self):
        self.tracker = StatsTracker()

    def test_single_success(self):
        self.tracker.record_action("dev1", "rally", True, 5.0)
        entry = self.tracker._data["dev1"]["actions"]["rally"]
        assert entry["attempts"] == 1
        assert entry["successes"] == 1
        assert entry["failures"] == 0
        assert entry["total_time_s"] == 5.0

    def test_single_failure(self):
        self.tracker.record_action("dev1", "rally", False, 3.0, "timeout")
        entry = self.tracker._data["dev1"]["actions"]["rally"]
        assert entry["attempts"] == 1
        assert entry["successes"] == 0
        assert entry["failures"] == 1
        assert entry["last_failure"] == "timeout"

    def test_mixed_results(self):
        self.tracker.record_action("dev1", "rally", True, 5.0)
        self.tracker.record_action("dev1", "rally", True, 4.0)
        self.tracker.record_action("dev1", "rally", False, 2.0, "lost")
        entry = self.tracker._data["dev1"]["actions"]["rally"]
        assert entry["attempts"] == 3
        assert entry["successes"] == 2
        assert entry["failures"] == 1
        assert entry["total_time_s"] == 11.0

    def test_error_list_capped_at_50(self):
        for i in range(55):
            self.tracker.record_action("dev1", "rally", False, 1.0, f"err_{i}")
        errors = self.tracker._data["dev1"]["errors"]
        assert len(errors) == 50
        # Should keep the most recent 50
        assert errors[-1]["error"] == "err_54"
        assert errors[0]["error"] == "err_5"

    def test_multiple_devices_isolated(self):
        self.tracker.record_action("dev1", "rally", True, 5.0)
        self.tracker.record_action("dev2", "rally", False, 3.0)
        assert self.tracker._data["dev1"]["actions"]["rally"]["successes"] == 1
        assert self.tracker._data["dev2"]["actions"]["rally"]["failures"] == 1


class TestStatsTrackerTemplateMiss:
    def setup_method(self):
        self.tracker = StatsTracker()

    def test_records_miss_and_caps_scores(self):
        # Single miss recorded correctly
        self.tracker.record_template_miss("dev1", "slot.png", 0.3)
        entry = self.tracker._data["dev1"]["template_misses"]["slot.png"]
        assert entry["count"] == 1
        assert entry["best_scores"] == [0.3]

        # After 15 misses, scores list is capped at 10 (keeps most recent)
        for i in range(14):
            self.tracker.record_template_miss("dev1", "slot.png", i * 0.05)
        assert entry["count"] == 15
        assert len(entry["best_scores"]) == 10


class TestStatsTrackerNavFailure:
    def setup_method(self):
        self.tracker = StatsTracker()

    def test_records_failure(self):
        self.tracker.record_nav_failure("dev1", "map_screen", "war_screen")
        assert self.tracker._data["dev1"]["nav_failures"]["map_screen->war_screen"] == 1

    def test_increments(self):
        self.tracker.record_nav_failure("dev1", "map_screen", "war_screen")
        self.tracker.record_nav_failure("dev1", "map_screen", "war_screen")
        assert self.tracker._data["dev1"]["nav_failures"]["map_screen->war_screen"] == 2


class TestStatsTrackerSummary:
    def test_empty(self):
        tracker = StatsTracker()
        tracker._data.clear()  # clear any seeded data from previous sessions
        assert "No activity" in tracker.summary()

    def test_with_data(self):
        tracker = StatsTracker()
        tracker.record_action("dev1", "rally", True, 5.0)
        tracker.record_action("dev1", "rally", False, 2.0, "fail")
        summary = tracker.summary()
        assert "dev1" in summary
        assert "rally" in summary
        assert "50%" in summary  # 1/2 success


class TestStatsTrackerSave:
    def test_save_creates_json(self, tmp_path):
        tracker = StatsTracker()
        tracker.record_action("dev1", "rally", True, 5.0)

        with patch("botlog.STATS_DIR", str(tmp_path)):
            tracker.save()

        files = list(tmp_path.glob("session_*.json"))
        assert len(files) == 1

        import json
        data = json.loads(files[0].read_text())
        assert "version" in data
        assert "session_start" in data
        assert "devices" in data
        assert "dev1" in data["devices"]
        # Check avg_time_s was computed
        rally = data["devices"]["dev1"]["actions"]["rally"]
        assert rally["avg_time_s"] == 5.0


# ============================================================
# timed_action decorator
# ============================================================

class TestTimedAction:
    def test_success_records_stats(self):
        tracker = StatsTracker()

        with patch("botlog.stats", tracker):
            @timed_action("test_action")
            def my_func(device):
                return True

            result = my_func("dev1")

        assert result is True
        entry = tracker._data["dev1"]["actions"]["test_action"]
        assert entry["successes"] == 1

    def test_false_return_records_failure(self):
        tracker = StatsTracker()

        with patch("botlog.stats", tracker):
            @timed_action("test_action")
            def my_func(device):
                return False

            result = my_func("dev1")

        assert result is False
        entry = tracker._data["dev1"]["actions"]["test_action"]
        assert entry["failures"] == 1

    def test_none_return_records_failure(self):
        tracker = StatsTracker()

        with patch("botlog.stats", tracker):
            @timed_action("test_action")
            def my_func(device):
                return None

            result = my_func("dev1")

        assert result is None
        entry = tracker._data["dev1"]["actions"]["test_action"]
        assert entry["failures"] == 1

    def test_exception_records_and_reraises(self):
        tracker = StatsTracker()

        with patch("botlog.stats", tracker):
            @timed_action("test_action")
            def my_func(device):
                raise ValueError("boom")

            with pytest.raises(ValueError, match="boom"):
                my_func("dev1")

        entry = tracker._data["dev1"]["actions"]["test_action"]
        assert entry["failures"] == 1
        assert entry["last_failure"] == "boom"

    def test_truthy_non_bool_is_success(self):
        """0, '', [] are treated as success (only False/None are failures)."""
        tracker = StatsTracker()

        with patch("botlog.stats", tracker):
            @timed_action("test_action")
            def returns_zero(device):
                return 0

            returns_zero("dev1")

        entry = tracker._data["dev1"]["actions"]["test_action"]
        assert entry["successes"] == 1


# ============================================================
# get_logger
# ============================================================

class TestGetLogger:
    def test_with_device(self):
        adapter = get_logger("test_module", "emulator-5584")
        assert adapter.extra["device"] == "emulator-5584"

    def test_without_device(self):
        adapter = get_logger("test_module")
        assert adapter.extra["device"] == "system"

    def test_returns_logger_adapter(self):
        adapter = get_logger("test_module", "dev1")
        assert isinstance(adapter, logging.LoggerAdapter)
