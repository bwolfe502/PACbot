"""Tests for web/dashboard.py — Flask routes, task launching, settings."""

import json
import sys
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

# Mock tkinter/customtkinter before importing modules that need them
# (territory.py imports tkinter which is unavailable in headless CI)
if "tkinter" not in sys.modules:
    sys.modules["tkinter"] = MagicMock()
if "customtkinter" not in sys.modules:
    sys.modules["customtkinter"] = MagicMock()
if "PIL.ImageTk" not in sys.modules:
    sys.modules["PIL.ImageTk"] = MagicMock()

import config
from web.dashboard import (
    create_app, launch_task, stop_task, stop_all, cleanup_dead_tasks,
    sleep_interval, run_once, TASK_FUNCTIONS, AUTO_RUNNERS,
    _load_settings, _save_settings, _apply_settings,
    DEFAULTS, ensure_firewall_open,
)


@pytest.fixture
def app():
    """Create a Flask test app."""
    application = create_app()
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture(autouse=True)
def clean_running_tasks():
    """Clear running_tasks before and after each test."""
    config.running_tasks.clear()
    config.DEVICE_STATUS.clear()
    yield
    # Stop all tasks and clean up
    for key, info in list(config.running_tasks.items()):
        if isinstance(info, dict) and "stop_event" in info:
            info["stop_event"].set()
    config.running_tasks.clear()
    config.DEVICE_STATUS.clear()


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------

class TestIndexRoute:
    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={"127.0.0.1:9999": "MuMu"})
    def test_index_returns_200(self, mock_instances, mock_devs, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"PACbot" in resp.data

    @patch("web.dashboard.get_devices", return_value=[])
    @patch("web.dashboard.get_emulator_instances", return_value={})
    def test_index_no_devices(self, mock_instances, mock_devs, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"No devices found" in resp.data

    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={"127.0.0.1:9999": "MuMu"})
    def test_index_shows_device_name(self, mock_instances, mock_devs, client):
        resp = client.get("/")
        assert b"MuMu" in resp.data


class TestTasksRoute:
    def test_tasks_redirects_to_index(self, client):
        resp = client.get("/tasks")
        assert resp.status_code == 302


class TestSettingsRoute:
    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={})
    def test_settings_page_returns_200(self, mock_instances, mock_devs, client):
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert b"Settings" in resp.data

    @patch("web.dashboard.get_devices", return_value=[])
    @patch("web.dashboard.get_emulator_instances", return_value={})
    @patch("web.dashboard._save_settings")
    @patch("web.dashboard._apply_settings")
    def test_save_settings_post(self, mock_apply, mock_save, mock_inst,
                                mock_devs, client):
        resp = client.post("/settings", data={
            "auto_heal": "on",
            "min_troops": "2",
            "titan_interval": "45",
            "mode": "bl",
        })
        assert resp.status_code == 302
        mock_apply.assert_called_once()
        mock_save.assert_called_once()

    @patch("web.dashboard.get_devices", return_value=[])
    @patch("web.dashboard.get_emulator_instances", return_value={})
    @patch("web.dashboard._save_settings")
    @patch("web.dashboard._apply_settings")
    def test_save_settings_validates(self, mock_apply, mock_save, mock_inst,
                                     mock_devs, client):
        """Settings are validated before saving."""
        resp = client.post("/settings", data={
            "min_troops": "999",  # way above max
            "mode": "bl",
        })
        assert resp.status_code == 302
        saved = mock_save.call_args[0][0]
        # validate_settings should have clamped min_troops to default
        assert saved["min_troops"] in (0, 5)  # either default or max


class TestLogsRoute:
    def test_logs_page_returns_200(self, client):
        resp = client.get("/logs")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# API route tests
# ---------------------------------------------------------------------------

class TestApiStatus:
    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={"127.0.0.1:9999": "MuMu"})
    @patch("web.dashboard.get_troop_status", return_value=None)
    @patch("web.dashboard.get_quest_tracking_state", return_value=[])
    def test_returns_json(self, mock_quest, mock_troop, mock_inst, mock_devs, client):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "devices" in data
        assert "tasks" in data
        assert len(data["devices"]) == 1
        assert data["devices"][0]["id"] == "127.0.0.1:9999"

    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={})
    @patch("web.dashboard.get_troop_status", return_value=None)
    @patch("web.dashboard.get_quest_tracking_state", return_value=[])
    def test_shows_active_tasks(self, mock_quest, mock_troop, mock_inst,
                                mock_devs, client):
        # Simulate a running task
        ev = threading.Event()
        t = threading.Thread(target=lambda: ev.wait(), daemon=True)
        t.start()
        config.running_tasks["127.0.0.1:9999_auto_quest"] = {
            "thread": t, "stop_event": ev
        }
        try:
            resp = client.get("/api/status")
            data = json.loads(resp.data)
            assert "127.0.0.1:9999_auto_quest" in data["tasks"]
        finally:
            ev.set()
            t.join(timeout=1)


class TestApiLogs:
    @patch("os.path.isfile", return_value=False)
    def test_returns_empty_when_no_log(self, mock_isfile, client):
        resp = client.get("/api/logs")
        data = json.loads(resp.data)
        assert data["lines"] == []


class TestApiRefreshDevices:
    @patch("web.dashboard.auto_connect_emulators")
    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={"127.0.0.1:9999": "MuMu"})
    def test_refresh_returns_devices(self, mock_inst, mock_devs, mock_connect, client):
        resp = client.post("/api/devices/refresh")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["devices"]) == 1
        mock_connect.assert_called_once()


# ---------------------------------------------------------------------------
# Task start/stop routes
# ---------------------------------------------------------------------------

class TestStartTask:
    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={})
    def test_start_oneshot_task(self, mock_inst, mock_devs, client):
        with patch.dict(TASK_FUNCTIONS, {"Heal All": MagicMock()}):
            resp = client.post("/tasks/start", data={
                "device": "127.0.0.1:9999",
                "task_name": "Heal All",
                "task_type": "oneshot",
            })
        assert resp.status_code == 302
        # Task should have been launched
        assert any("once:Heal All" in k for k in config.running_tasks)

    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={})
    def test_start_auto_mode(self, mock_inst, mock_devs, client):
        # Mock the runner to avoid actually running tasks
        mock_runner = MagicMock()
        with patch.dict(AUTO_RUNNERS, {"auto_titan": mock_runner}):
            resp = client.post("/tasks/start", data={
                "device": "127.0.0.1:9999",
                "task_name": "auto_titan",
                "task_type": "auto",
            })
        assert resp.status_code == 302
        assert "127.0.0.1:9999_auto_titan" in config.running_tasks

    @patch("web.dashboard.get_devices", return_value=[])
    @patch("web.dashboard.get_emulator_instances", return_value={})
    def test_start_no_device_redirects(self, mock_inst, mock_devs, client):
        resp = client.post("/tasks/start", data={
            "device": "",
            "task_name": "Heal All",
            "task_type": "oneshot",
        })
        assert resp.status_code == 302
        assert len(config.running_tasks) == 0

    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={})
    def test_start_auto_skips_if_already_running(self, mock_inst, mock_devs, client):
        # Pre-populate a running task
        ev = threading.Event()
        t = threading.Thread(target=lambda: ev.wait(), daemon=True)
        t.start()
        config.running_tasks["127.0.0.1:9999_auto_titan"] = {
            "thread": t, "stop_event": ev
        }
        try:
            mock_runner = MagicMock()
            with patch.dict(AUTO_RUNNERS, {"auto_titan": mock_runner}):
                resp = client.post("/tasks/start", data={
                    "device": "127.0.0.1:9999",
                    "task_name": "auto_titan",
                    "task_type": "auto",
                })
            # Should not have started a new task
            mock_runner.assert_not_called()
        finally:
            ev.set()
            t.join(timeout=1)


class TestStopTask:
    def test_stop_task(self, client):
        ev = threading.Event()
        t = threading.Thread(target=lambda: ev.wait(), daemon=True)
        t.start()
        config.running_tasks["127.0.0.1:9999_test"] = {
            "thread": t, "stop_event": ev
        }
        resp = client.post("/tasks/stop", data={"task_key": "127.0.0.1:9999_test"})
        assert resp.status_code == 302
        assert ev.is_set()
        t.join(timeout=1)


class TestStopMode:
    def test_stop_mode_across_devices(self, client):
        ev1 = threading.Event()
        ev2 = threading.Event()
        t1 = threading.Thread(target=lambda: ev1.wait(), daemon=True)
        t2 = threading.Thread(target=lambda: ev2.wait(), daemon=True)
        t1.start()
        t2.start()
        config.running_tasks["dev1_auto_quest"] = {"thread": t1, "stop_event": ev1}
        config.running_tasks["dev2_auto_quest"] = {"thread": t2, "stop_event": ev2}
        resp = client.post("/tasks/stop-mode", data={"mode_key": "auto_quest"})
        assert resp.status_code == 302
        assert ev1.is_set()
        assert ev2.is_set()
        t1.join(timeout=1)
        t2.join(timeout=1)


class TestStopAll:
    def test_stop_all(self, client):
        ev = threading.Event()
        t = threading.Thread(target=lambda: ev.wait(), daemon=True)
        t.start()
        config.running_tasks["dev_task"] = {"thread": t, "stop_event": ev}
        config.DEVICE_STATUS["dev"] = "Running..."
        resp = client.post("/tasks/stop-all")
        assert resp.status_code == 302
        assert ev.is_set()
        assert len(config.DEVICE_STATUS) == 0
        t.join(timeout=1)


# ---------------------------------------------------------------------------
# Task runner unit tests
# ---------------------------------------------------------------------------

class TestSleepInterval:
    def test_basic_sleep(self):
        start = time.time()
        sleep_interval(1, 0, lambda: False)
        elapsed = time.time() - start
        assert elapsed >= 0.9

    def test_stop_check_breaks_early(self):
        ev = threading.Event()
        ev.set()
        start = time.time()
        sleep_interval(60, 0, ev.is_set)
        elapsed = time.time() - start
        assert elapsed < 5


class TestRunOnce:
    def test_calls_function(self):
        mock_fn = MagicMock()
        device = "127.0.0.1:9999"
        with patch("web.dashboard.config.get_device_lock") as mock_lock:
            mock_lock.return_value = MagicMock(__enter__=MagicMock(), __exit__=MagicMock())
            run_once(device, "TestAction", mock_fn)
        mock_fn.assert_called_once_with(device)

    def test_clears_status_on_error(self):
        def raise_error(device):
            raise RuntimeError("test error")
        device = "127.0.0.1:9999"
        with patch("web.dashboard.config.get_device_lock") as mock_lock:
            mock_lock.return_value = MagicMock(__enter__=MagicMock(), __exit__=MagicMock())
            run_once(device, "TestAction", raise_error)
        assert config.DEVICE_STATUS.get(device) is None


class TestLaunchTask:
    def test_launch_registers_in_running_tasks(self):
        ev = threading.Event()
        ev.set()  # immediately stop
        launch_task("dev1", "test_task", lambda: None, ev)
        assert "dev1_test_task" in config.running_tasks
        info = config.running_tasks["dev1_test_task"]
        assert info["stop_event"] is ev
        assert info["thread"] is not None


class TestCleanupDeadTasks:
    def test_removes_dead_threads(self):
        t = threading.Thread(target=lambda: None, daemon=True)
        t.start()
        t.join(timeout=1)  # let it finish
        ev = threading.Event()
        config.running_tasks["dead_task"] = {"thread": t, "stop_event": ev}
        cleanup_dead_tasks()
        assert "dead_task" not in config.running_tasks

    def test_keeps_alive_threads(self):
        ev = threading.Event()
        t = threading.Thread(target=lambda: ev.wait(), daemon=True)
        t.start()
        config.running_tasks["alive_task"] = {"thread": t, "stop_event": ev}
        cleanup_dead_tasks()
        assert "alive_task" in config.running_tasks
        ev.set()
        t.join(timeout=1)


# ---------------------------------------------------------------------------
# Firewall helper tests
# ---------------------------------------------------------------------------

class TestEnsureFirewallOpen:
    @patch("web.dashboard.sys")
    def test_non_windows_returns_true(self, mock_sys):
        mock_sys.platform = "linux"
        assert ensure_firewall_open(8080) is True

    @patch("web.dashboard.sys")
    def test_rule_already_exists(self, mock_sys):
        mock_sys.platform = "win32"
        rule_name = "PACbot Web Dashboard (TCP 8080)"
        mock_result = MagicMock(returncode=0, stdout=f"Rule Name: {rule_name}\n")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            assert ensure_firewall_open(8080) is True
            # Should only check, not add
            mock_run.assert_called_once()
            assert "show" in mock_run.call_args[0][0]

    @patch("web.dashboard.sys")
    def test_adds_rule_successfully(self, mock_sys):
        mock_sys.platform = "win32"
        check_result = MagicMock(returncode=1, stdout="")
        add_result = MagicMock(returncode=0)
        with patch("subprocess.run", side_effect=[check_result, add_result]) as mock_run:
            assert ensure_firewall_open(8080) is True
            assert mock_run.call_count == 2
            add_call_args = mock_run.call_args_list[1][0][0]
            assert "add" in add_call_args
            assert "localport=8080" in add_call_args

    @patch("web.dashboard.sys")
    def test_add_rule_fails_no_admin(self, mock_sys):
        mock_sys.platform = "win32"
        check_result = MagicMock(returncode=1, stdout="")
        add_result = MagicMock(returncode=1)
        with patch("subprocess.run", side_effect=[check_result, add_result]):
            assert ensure_firewall_open(8080) is False

    @patch("web.dashboard.sys")
    def test_netsh_not_found(self, mock_sys):
        mock_sys.platform = "win32"
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert ensure_firewall_open(8080) is False


# ---------------------------------------------------------------------------
# Exclusivity (auto modes)
# ---------------------------------------------------------------------------

class TestAutoModeExclusivity:
    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={})
    def test_auto_gold_stops_auto_quest(self, mock_inst, mock_devs, client):
        """Starting auto_gold should stop conflicting auto_quest."""
        ev_quest = threading.Event()
        t = threading.Thread(target=lambda: ev_quest.wait(), daemon=True)
        t.start()
        config.running_tasks["127.0.0.1:9999_auto_quest"] = {
            "thread": t, "stop_event": ev_quest
        }
        mock_runner = MagicMock()
        with patch.dict(AUTO_RUNNERS, {"auto_gold": mock_runner}):
            client.post("/tasks/start", data={
                "device": "127.0.0.1:9999",
                "task_name": "auto_gold",
                "task_type": "auto",
            })
        # auto_quest stop event should be set (exclusivity)
        assert ev_quest.is_set()
        t.join(timeout=1)
