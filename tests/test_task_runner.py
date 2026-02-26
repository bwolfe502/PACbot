"""Tests for task runner logic (main.py).

Tests sleep_interval, launch_task/stop_task, run_once, run_repeat,
and load_settings/save_settings. All game actions are mocked.
"""

import json
import os
import threading
import time
from unittest.mock import patch, MagicMock

import config
from main import (
    sleep_interval, launch_task, stop_task, stop_all_tasks_matching,
    run_once, run_repeat, load_settings, save_settings, DEFAULTS,
)


# ============================================================
# sleep_interval
# ============================================================

class TestSleepInterval:
    @patch("main.time.sleep")
    def test_no_variation(self, mock_sleep):
        """Sleeps exactly `base` seconds when variation is 0."""
        stop = MagicMock(return_value=False)
        sleep_interval(3, 0, stop)
        assert mock_sleep.call_count == 3

    @patch("main.time.sleep")
    def test_stop_check_exits_early(self, mock_sleep):
        """Exits immediately when stop_check returns True."""
        call_count = 0

        def stop():
            nonlocal call_count
            call_count += 1
            return call_count > 1  # True on second call

        sleep_interval(10, 0, stop)
        # Should have slept only once before stop_check fired
        assert mock_sleep.call_count == 1

    @patch("main.random.randint", return_value=2)
    @patch("main.time.sleep")
    def test_with_variation(self, mock_sleep, mock_randint):
        """Sleeps base + variation seconds."""
        stop = MagicMock(return_value=False)
        sleep_interval(5, 3, stop)
        # base=5, randint returns 2, so actual=7
        assert mock_sleep.call_count == 7


# ============================================================
# launch_task / stop_task / stop_all_tasks_matching
# ============================================================

class TestTaskManagement:
    def setup_method(self):
        config.running_tasks.clear()

    def teardown_method(self):
        config.running_tasks.clear()

    def test_launch_creates_entry(self):
        stop_event = threading.Event()
        func = MagicMock()
        launch_task("dev1", "test_task", func, stop_event, args=("dev1", stop_event))
        assert "dev1_test_task" in config.running_tasks
        info = config.running_tasks["dev1_test_task"]
        assert info["stop_event"] is stop_event
        assert info["thread"].daemon is True
        # Clean up
        stop_event.set()
        info["thread"].join(timeout=2)

    def test_stop_task_sets_event(self):
        stop_event = threading.Event()
        config.running_tasks["dev1_test"] = {"thread": MagicMock(), "stop_event": stop_event}
        stop_task("dev1_test")
        assert stop_event.is_set()

    def test_stop_task_missing_key(self):
        """Stopping a non-existent task should not crash."""
        stop_task("nonexistent_task")  # Should not raise

    def test_stop_all_matching(self):
        ev1 = threading.Event()
        ev2 = threading.Event()
        ev3 = threading.Event()
        config.running_tasks["dev1_auto_quest"] = {"thread": MagicMock(), "stop_event": ev1}
        config.running_tasks["dev2_auto_quest"] = {"thread": MagicMock(), "stop_event": ev2}
        config.running_tasks["dev1_auto_titan"] = {"thread": MagicMock(), "stop_event": ev3}

        stop_all_tasks_matching("_auto_quest")

        assert ev1.is_set()
        assert ev2.is_set()
        assert not ev3.is_set()  # Should NOT be stopped


# ============================================================
# run_once
# ============================================================

class TestRunOnce:
    @patch("main.config.get_device_lock")
    def test_success(self, mock_get_lock):
        mock_get_lock.return_value = threading.Lock()
        func = MagicMock()
        run_once("dev1", "test_task", func)
        func.assert_called_once_with("dev1")

    @patch("main.config.get_device_lock")
    def test_exception_caught(self, mock_get_lock):
        mock_get_lock.return_value = threading.Lock()
        func = MagicMock(side_effect=RuntimeError("test error"))
        run_once("dev1", "test_task", func)  # Should not raise


# ============================================================
# run_repeat
# ============================================================

class TestRunRepeat:
    @patch("main.sleep_interval")
    @patch("main.config.get_device_lock")
    def test_runs_and_stops(self, mock_get_lock, mock_sleep):
        """Runs function, then stop_event fires during sleep."""
        mock_get_lock.return_value = threading.Lock()
        stop_event = threading.Event()
        func = MagicMock()

        # Make sleep_interval set the stop event (simulating user stopping)
        def fake_sleep(base, variation, stop_check):
            stop_event.set()

        mock_sleep.side_effect = fake_sleep

        run_repeat("dev1", "test_task", func, 30, 0, stop_event)
        func.assert_called_once_with("dev1")

    @patch("main.sleep_interval")
    @patch("main.config.get_device_lock")
    def test_exception_caught(self, mock_get_lock, mock_sleep):
        mock_get_lock.return_value = threading.Lock()
        stop_event = threading.Event()
        func = MagicMock(side_effect=RuntimeError("boom"))

        run_repeat("dev1", "test_task", func, 30, 0, stop_event)
        # Should not raise — exception is caught and logged

    @patch("main.sleep_interval")
    @patch("main.config.get_device_lock")
    def test_multiple_iterations(self, mock_get_lock, mock_sleep):
        """Runs function multiple times before stop."""
        mock_get_lock.return_value = threading.Lock()
        stop_event = threading.Event()
        call_count = 0

        def counting_func(device):
            nonlocal call_count
            call_count += 1

        def fake_sleep(base, variation, stop_check):
            if call_count >= 3:
                stop_event.set()

        mock_sleep.side_effect = fake_sleep
        run_repeat("dev1", "test_task", counting_func, 10, 0, stop_event)
        assert call_count == 3


# ============================================================
# load_settings / save_settings
# ============================================================

class TestSettings:
    def test_missing_file_returns_defaults(self, tmp_path):
        with patch("main.SETTINGS_FILE", str(tmp_path / "nonexistent.json")):
            result = load_settings()
        assert result == DEFAULTS

    def test_round_trip(self, tmp_path):
        settings_file = str(tmp_path / "test_settings.json")
        custom = {**DEFAULTS, "min_troops": 42, "auto_heal": False}
        with patch("main.SETTINGS_FILE", settings_file):
            save_settings(custom)
            loaded = load_settings()
        assert loaded["min_troops"] == 42
        assert loaded["auto_heal"] is False
