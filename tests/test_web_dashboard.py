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
    run_once, TASK_FUNCTIONS, AUTO_RUNNERS,
    _load_settings, _save_settings, _apply_settings,
    DEFAULTS, ensure_firewall_open,
)
from runners import sleep_interval


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
        assert b"9Bot" in resp.data

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


class TestApiStatusTunnel:
    """Tunnel status field in /api/status response."""

    @patch("web.dashboard.get_devices", return_value=[])
    @patch("web.dashboard.get_emulator_instances", return_value={})
    @patch("web.dashboard.tunnel_status", return_value="connected")
    def test_tunnel_connected(self, mock_ts, mock_inst, mock_devs, client):
        data = json.loads(client.get("/api/status").data)
        assert data["tunnel"] == "connected"

    @patch("web.dashboard.get_devices", return_value=[])
    @patch("web.dashboard.get_emulator_instances", return_value={})
    @patch("web.dashboard.tunnel_status", return_value="connecting")
    def test_tunnel_connecting(self, mock_ts, mock_inst, mock_devs, client):
        data = json.loads(client.get("/api/status").data)
        assert data["tunnel"] == "connecting"

    @patch("web.dashboard.get_devices", return_value=[])
    @patch("web.dashboard.get_emulator_instances", return_value={})
    @patch("web.dashboard.tunnel_status", return_value="disconnected")
    def test_tunnel_disconnected(self, mock_ts, mock_inst, mock_devs, client):
        data = json.loads(client.get("/api/status").data)
        assert data["tunnel"] == "disconnected"

    @patch("web.dashboard.get_devices", return_value=[])
    @patch("web.dashboard.get_emulator_instances", return_value={})
    @patch("web.dashboard.tunnel_status", return_value="disabled")
    def test_tunnel_disabled(self, mock_ts, mock_inst, mock_devs, client):
        data = json.loads(client.get("/api/status").data)
        assert data["tunnel"] == "disabled"


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
    def test_refresh_redirects_to_index(self, mock_inst, mock_devs, mock_connect, client):
        resp = client.post("/api/devices/refresh")
        assert resp.status_code == 302
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
        rule_name = "9Bot Web Dashboard (TCP 8080)"
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


# ---------------------------------------------------------------------------
# Territory grid API tests
# ---------------------------------------------------------------------------

class TestTerritoryPage:
    def test_territory_page_returns_200(self, client):
        resp = client.get("/territory")
        assert resp.status_code == 200
        assert b"territory" in resp.data.lower()


class TestTerritoryGridApi:
    def test_get_grid_returns_json(self, client):
        config.MANUAL_ATTACK_SQUARES = {(0, 1), (2, 3)}
        config.MANUAL_IGNORE_SQUARES = {(5, 6)}
        resp = client.get("/api/territory/grid")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "attack" in data
        assert "ignore" in data
        assert "throne" in data
        assert len(data["attack"]) == 2
        assert len(data["ignore"]) == 1
        assert [11, 11] in data["throne"]

    def test_get_grid_empty(self, client):
        config.MANUAL_ATTACK_SQUARES = set()
        config.MANUAL_IGNORE_SQUARES = set()
        resp = client.get("/api/territory/grid")
        data = json.loads(resp.data)
        assert data["attack"] == []
        assert data["ignore"] == []

    def test_post_grid_saves_squares(self, client):
        config.MANUAL_ATTACK_SQUARES = set()
        config.MANUAL_IGNORE_SQUARES = set()
        resp = client.post("/api/territory/grid",
                           data=json.dumps({
                               "attack": [[1, 2], [3, 4]],
                               "ignore": [[5, 6]],
                           }),
                           content_type="application/json")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert (1, 2) in config.MANUAL_ATTACK_SQUARES
        assert (3, 4) in config.MANUAL_ATTACK_SQUARES
        assert (5, 6) in config.MANUAL_IGNORE_SQUARES

    def test_post_grid_clears_previous(self, client):
        config.MANUAL_ATTACK_SQUARES = {(9, 9)}
        config.MANUAL_IGNORE_SQUARES = {(8, 8)}
        resp = client.post("/api/territory/grid",
                           data=json.dumps({"attack": [], "ignore": []}),
                           content_type="application/json")
        assert resp.status_code == 200
        assert len(config.MANUAL_ATTACK_SQUARES) == 0
        assert len(config.MANUAL_IGNORE_SQUARES) == 0


# ---------------------------------------------------------------------------
# Bug report endpoint tests
# ---------------------------------------------------------------------------

class TestBugReportApi:
    @patch("startup.create_bug_report_zip")
    def test_bug_report_returns_zip(self, mock_zip, client):
        mock_zip.return_value = (b"PK\x03\x04fake_zip_data", "9bot_bugreport_test.zip")
        resp = client.post("/api/bug-report")
        assert resp.status_code == 200
        assert resp.content_type == "application/zip"
        assert b"PK" in resp.data  # zip magic bytes


# ---------------------------------------------------------------------------
# Debug page tests
# ---------------------------------------------------------------------------

class TestDebugPage:
    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={})
    def test_debug_page_returns_200(self, mock_inst, mock_devs, client):
        resp = client.get("/debug")
        assert resp.status_code == 200
        assert b"debug" in resp.data.lower()


# ---------------------------------------------------------------------------
# startup.py tests
# ---------------------------------------------------------------------------

class TestApplySettings:
    def test_apply_settings_sets_config_globals(self):
        from startup import apply_settings
        apply_settings({
            "auto_heal": False,
            "auto_restore_ap": True,
            "min_troops": 3,
            "my_team": "red",
            "mithril_interval": 25,
            "gather_enabled": False,
            "gather_mine_level": 5,
            "gather_max_troops": 2,
            "tower_quest_enabled": True,
            "device_troops": {"127.0.0.1:9999": 4},
        })
        assert config.AUTO_HEAL_ENABLED is False
        assert config.AUTO_RESTORE_AP_ENABLED is True
        assert config.MIN_TROOPS_AVAILABLE == 3
        assert config.MY_TEAM_COLOR == "red"
        assert config.MITHRIL_INTERVAL == 25
        assert config.DEVICE_TOTAL_TROOPS.get("127.0.0.1:9999") == 4

    def test_apply_settings_defaults(self):
        from startup import apply_settings
        apply_settings({})
        assert config.AUTO_HEAL_ENABLED is True
        assert config.MIN_TROOPS_AVAILABLE == 0


class TestCreateBugReportZip:
    @patch("devices.get_devices", return_value=["127.0.0.1:9999"])
    def test_creates_valid_zip(self, mock_devs):
        import zipfile
        import io
        from startup import create_bug_report_zip
        zip_bytes, filename = create_bug_report_zip()
        assert filename.startswith("9bot_bugreport_")
        assert filename.endswith(".zip")
        # Verify it's a valid zip
        buf = io.BytesIO(zip_bytes)
        with zipfile.ZipFile(buf, "r") as zf:
            names = zf.namelist()
            assert "report_info.txt" in names

    @patch("devices.get_devices", return_value=[])
    def test_includes_system_info(self, mock_devs):
        import zipfile
        import io
        from startup import create_bug_report_zip
        zip_bytes, _ = create_bug_report_zip()
        buf = io.BytesIO(zip_bytes)
        with zipfile.ZipFile(buf, "r") as zf:
            info = zf.read("report_info.txt").decode("utf-8")
            assert "9Bot Bug Report" in info
            assert "Python:" in info
            assert "OS:" in info


class TestCreateBugReportZipClearDebug:
    """Test clear_debug parameter on create_bug_report_zip."""

    @patch("devices.get_devices", return_value=[])
    @patch("startup._clear_debug_files")
    def test_clear_debug_true_calls_cleanup(self, mock_clear, mock_devs):
        from startup import create_bug_report_zip
        create_bug_report_zip(clear_debug=True)
        mock_clear.assert_called_once()

    @patch("devices.get_devices", return_value=[])
    @patch("startup._clear_debug_files")
    def test_clear_debug_false_skips_cleanup(self, mock_clear, mock_devs):
        from startup import create_bug_report_zip
        create_bug_report_zip(clear_debug=False)
        mock_clear.assert_not_called()

    @patch("devices.get_devices", return_value=[])
    @patch("startup._clear_debug_files")
    def test_clear_debug_default_is_true(self, mock_clear, mock_devs):
        from startup import create_bug_report_zip
        create_bug_report_zip()
        mock_clear.assert_called_once()


class TestUploadBugReport:
    """Test upload_bug_report() in startup.py."""

    @patch("startup.get_relay_config", return_value=None)
    def test_no_relay_returns_failure(self, mock_cfg):
        from startup import upload_bug_report
        ok, msg = upload_bug_report(settings={})
        assert ok is False
        assert "not configured" in msg.lower()

    @patch("startup.create_bug_report_zip",
           return_value=(b"PK\x03\x04fake", "test.zip"))
    @patch("startup.get_relay_config",
           return_value=("wss://example.com/ws/tunnel", "secret123", "bot42"))
    def test_successful_upload(self, mock_cfg, mock_zip):
        import startup
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.object(startup, "upload_bug_report", wraps=startup.upload_bug_report):
            with patch("requests.post", return_value=mock_resp) as mock_post:
                ok, msg = startup.upload_bug_report(settings={})
        assert ok is True
        assert "successful" in msg.lower()
        # Verify clear_debug=False was passed
        mock_zip.assert_called_once_with(clear_debug=False)
        # Verify URL derived from relay URL
        call_args = mock_post.call_args
        assert "example.com/_upload" in call_args[0][0]

    @patch("startup.create_bug_report_zip",
           return_value=(b"PK\x03\x04fake", "test.zip"))
    @patch("startup.get_relay_config",
           return_value=("wss://example.com/ws/tunnel", "secret123", "bot42"))
    def test_http_error_returns_failure(self, mock_cfg, mock_zip):
        from startup import upload_bug_report
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch("requests.post", return_value=mock_resp):
            ok, msg = upload_bug_report(settings={})
        assert ok is False
        assert "500" in msg

    @patch("startup.create_bug_report_zip",
           return_value=(b"PK\x03\x04fake", "test.zip"))
    @patch("startup.get_relay_config",
           return_value=("wss://example.com/ws/tunnel", "secret123", "bot42"))
    def test_network_error_returns_failure(self, mock_cfg, mock_zip):
        from startup import upload_bug_report
        with patch("requests.post", side_effect=ConnectionError("timeout")):
            ok, msg = upload_bug_report(settings={})
        assert ok is False
        assert "timeout" in msg.lower()


class TestUploadStatus:
    def test_disabled_when_no_thread(self):
        import startup
        startup._upload_thread = None
        result = startup.upload_status()
        assert result["enabled"] is False

    def test_shows_last_upload_time(self):
        import startup
        from datetime import datetime
        startup._last_upload_time = datetime(2026, 3, 1, 12, 0, 0)
        startup._last_upload_error = None
        result = startup.upload_status()
        assert result["last_upload"] is not None
        assert result["error"] is None
        startup._last_upload_time = None

    def test_shows_error(self):
        import startup
        startup._last_upload_error = "Connection refused"
        result = startup.upload_status()
        assert result["error"] == "Connection refused"
        startup._last_upload_error = None


class TestAutoUploadThread:
    def test_start_and_stop(self):
        import startup
        startup.start_auto_upload({"upload_interval_hours": 1})
        assert startup._upload_thread is not None
        assert startup._upload_thread.is_alive()
        startup.stop_auto_upload()
        assert startup._upload_thread is None

    def test_stop_when_not_started(self):
        import startup
        startup._upload_thread = None
        startup.stop_auto_upload()  # should not raise


class TestUploadLogsApi:
    """Test /api/upload-logs route."""

    @patch("startup.upload_bug_report", return_value=(True, "Upload successful"))
    def test_upload_success(self, mock_upload, client):
        resp = client.post("/api/upload-logs")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert "successful" in data["message"].lower()

    @patch("startup.upload_bug_report",
           return_value=(False, "Relay not configured"))
    def test_upload_failure(self, mock_upload, client):
        resp = client.post("/api/upload-logs")
        data = json.loads(resp.data)
        assert data["ok"] is False


class TestApiStatusUpload:
    """Upload status field in /api/status response."""

    @patch("web.dashboard.get_devices", return_value=[])
    @patch("web.dashboard.get_emulator_instances", return_value={})
    @patch("web.dashboard._upload_status", return_value={"enabled": True})
    def test_upload_status_included(self, mock_us, mock_inst, mock_devs, client):
        data = json.loads(client.get("/api/status").data)
        assert "upload" in data
        assert data["upload"]["enabled"] is True

    @patch("web.dashboard.get_devices", return_value=[])
    @patch("web.dashboard.get_emulator_instances", return_value={})
    @patch("web.dashboard._upload_status", return_value={"enabled": False})
    def test_upload_disabled(self, mock_us, mock_inst, mock_devs, client):
        data = json.loads(client.get("/api/status").data)
        assert data["upload"]["enabled"] is False


class TestUploadSettings:
    """Test that upload settings are parsed from form."""

    def test_defaults_include_upload_keys(self):
        assert "auto_upload_logs" in DEFAULTS
        assert "upload_interval_hours" in DEFAULTS
        assert DEFAULTS["auto_upload_logs"] is False
        assert DEFAULTS["upload_interval_hours"] == 24


class TestGetRamGb:
    def test_returns_string(self):
        from startup import _get_ram_gb
        result = _get_ram_gb()
        assert isinstance(result, str)
        # Should be either "X.X GB" or "unknown"
        assert "GB" in result or result == "unknown"


# ---------------------------------------------------------------------------
# Device-scoped route tests (Phase 1: per-device access control)
# ---------------------------------------------------------------------------

class TestDeviceScopedRoutes:
    """Routes under /d/<dhash>/ require a valid device token."""

    DEVICE = "127.0.0.1:9999"

    @staticmethod
    def _get_hash_and_token(device_id):
        from startup import device_hash, generate_device_token
        return device_hash(device_id), generate_device_token(device_id)

    @patch("license.get_license_key", return_value="test-key-xyz")
    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={"127.0.0.1:9999": "MuMu"})
    def test_device_index_valid_token(self, _inst, _devs, _key, client):
        dhash, token = self._get_hash_and_token(self.DEVICE)
        resp = client.get(f"/d/{dhash}?token={token}")
        assert resp.status_code == 200
        assert b"MuMu" in resp.data

    @patch("license.get_license_key", return_value="test-key-xyz")
    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={"127.0.0.1:9999": "MuMu"})
    def test_device_index_invalid_token(self, _inst, _devs, _key, client):
        dhash, _ = self._get_hash_and_token(self.DEVICE)
        resp = client.get(f"/d/{dhash}?token=0000000000000000")
        assert resp.status_code == 403

    @patch("license.get_license_key", return_value="test-key-xyz")
    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={"127.0.0.1:9999": "MuMu"})
    def test_device_index_missing_token(self, _inst, _devs, _key, client):
        dhash, _ = self._get_hash_and_token(self.DEVICE)
        resp = client.get(f"/d/{dhash}")
        assert resp.status_code == 403

    @patch("license.get_license_key", return_value="test-key-xyz")
    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={"127.0.0.1:9999": "MuMu"})
    def test_device_index_unknown_hash(self, _inst, _devs, _key, client):
        resp = client.get("/d/deadbeef?token=0000000000000000")
        assert resp.status_code == 404

    @patch("license.get_license_key", return_value="test-key-xyz")
    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={"127.0.0.1:9999": "MuMu"})
    def test_device_index_hides_settings_nav(self, _inst, _devs, _key, client):
        """Friend view should not show Settings or Restart."""
        dhash, token = self._get_hash_and_token(self.DEVICE)
        resp = client.get(f"/d/{dhash}?token={token}")
        assert b"/settings" not in resp.data
        assert b"Restart" not in resp.data

    @patch("license.get_license_key", return_value="test-key-xyz")
    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={"127.0.0.1:9999": "MuMu"})
    def test_device_api_status_valid(self, _inst, _devs, _key, client):
        dhash, token = self._get_hash_and_token(self.DEVICE)
        resp = client.get(f"/d/{dhash}/api/status?token={token}")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "devices" in data
        # Should only have the one scoped device
        assert len(data["devices"]) == 1
        assert data["devices"][0]["id"] == self.DEVICE

    @patch("license.get_license_key", return_value="test-key-xyz")
    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={"127.0.0.1:9999": "MuMu"})
    def test_device_stop_all(self, _inst, _devs, _key, client):
        dhash, token = self._get_hash_and_token(self.DEVICE)
        resp = client.post(f"/d/{dhash}/tasks/stop-all?token={token}")
        # Should redirect (302) after stopping
        assert resp.status_code in (200, 302)

    @patch("license.get_license_key", return_value="test-key-xyz")
    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={"127.0.0.1:9999": "MuMu"})
    def test_owner_index_has_share_button(self, _inst, _devs, _key, client):
        """Owner view should show Share button on device cards."""
        resp = client.get("/")
        assert b"Share" in resp.data


class TestDeviceSettingsRoutes:
    """Device-specific settings pages."""

    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={"127.0.0.1:9999": "MuMu"})
    def test_device_settings_page_200(self, _inst, _devs, client):
        resp = client.get("/settings/device/127.0.0.1:9999")
        assert resp.status_code == 200
        assert b"Override" in resp.data

    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={"127.0.0.1:9999": "MuMu"})
    def test_device_settings_save(self, _inst, _devs, client):
        resp = client.post("/settings/device/127.0.0.1:9999", data={
            "override_auto_heal": "on",
            "auto_heal": "on",
            "override_min_troops": "on",
            "min_troops": "3",
        })
        assert resp.status_code == 302  # redirect back

    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={"127.0.0.1:9999": "MuMu"})
    def test_device_settings_reset(self, _inst, _devs, client):
        resp = client.post("/settings/device/127.0.0.1:9999/reset")
        assert resp.status_code == 302

    @patch("web.dashboard.get_devices", return_value=["127.0.0.1:9999"])
    @patch("web.dashboard.get_emulator_instances", return_value={"127.0.0.1:9999": "MuMu"})
    def test_global_settings_has_device_tabs(self, _inst, _devs, client):
        """Global settings page should show device tabs."""
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert b"settings-tab" in resp.data
