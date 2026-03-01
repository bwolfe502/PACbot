"""Tests for evil guard rally (actions/evil_guard.py).

Covers: marching-troop guard that prevents dispatching multiple troops
when the castle is far from the evil guard (long march times).

All ADB and vision calls are mocked — no emulator needed.
"""

from troops import TroopAction, TroopStatus, DeviceTroopSnapshot


# ============================================================
# Helpers
# ============================================================

def _make_snapshot(device, actions):
    """Build a DeviceTroopSnapshot from a list of TroopAction values."""
    troops = [TroopStatus(action=a) for a in actions]
    return DeviceTroopSnapshot(device=device, troops=troops)


# ============================================================
# Marching-troop guard: unit tests for the guard condition
# ============================================================

class TestMarchingTroopGuard:
    """The marching-troop guard checks read_panel_statuses() after a
    poll_troop_ready timeout.  If any troop is still Marching, the bot
    should stop dispatching more priests.

    These tests verify the guard condition logic directly via the
    DeviceTroopSnapshot / any_doing API that the guard relies on.
    """

    def test_marching_detected_in_snapshot(self, mock_device):
        """any_doing(MARCHING) returns True when a troop is marching."""
        snap = _make_snapshot(mock_device, [
            TroopAction.DEFENDING,
            TroopAction.MARCHING,
            TroopAction.HOME,
            TroopAction.HOME,
            TroopAction.HOME,
        ])
        assert snap.any_doing(TroopAction.MARCHING) is True

    def test_no_marching_in_snapshot(self, mock_device):
        """any_doing(MARCHING) returns False when no troop is marching."""
        snap = _make_snapshot(mock_device, [
            TroopAction.DEFENDING,
            TroopAction.STATIONING,
            TroopAction.HOME,
            TroopAction.HOME,
            TroopAction.HOME,
        ])
        assert snap.any_doing(TroopAction.MARCHING) is False

    def test_multiple_marching_detected(self, mock_device):
        """Guard fires when multiple troops are marching (the bug scenario)."""
        snap = _make_snapshot(mock_device, [
            TroopAction.DEFENDING,
            TroopAction.MARCHING,
            TroopAction.MARCHING,
            TroopAction.HOME,
            TroopAction.HOME,
        ])
        assert snap.any_doing(TroopAction.MARCHING) is True

    def test_rallying_not_confused_with_marching(self, mock_device):
        """RALLYING is a different state — guard should not trigger."""
        snap = _make_snapshot(mock_device, [
            TroopAction.DEFENDING,
            TroopAction.RALLYING,
            TroopAction.HOME,
            TroopAction.HOME,
            TroopAction.HOME,
        ])
        assert snap.any_doing(TroopAction.MARCHING) is False

    def test_returning_not_confused_with_marching(self, mock_device):
        """RETURNING is a different state — guard should not trigger."""
        snap = _make_snapshot(mock_device, [
            TroopAction.DEFENDING,
            TroopAction.RETURNING,
            TroopAction.HOME,
            TroopAction.HOME,
            TroopAction.HOME,
        ])
        assert snap.any_doing(TroopAction.MARCHING) is False


# ============================================================
# Guard present in code: verify the pattern exists in evil_guard.py
# ============================================================

class TestGuardCodePresence:
    """Verify that the marching-troop guard pattern is present in the
    evil_guard.py source — a simple code-level sanity check."""

    def test_guard_pattern_in_priest_loop(self):
        """The priest loop (P2-P5) should check for MARCHING after timeout."""
        import inspect
        from actions import evil_guard
        source = inspect.getsource(evil_guard.rally_eg)
        # The guard reads panel and checks for MARCHING after poll timeout
        assert "any_doing(TroopAction.MARCHING)" in source
        assert "stopping priest dispatch" in source

    def test_guard_pattern_in_retry_loop(self):
        """The retry loop should also check for MARCHING after timeout."""
        import inspect
        from actions import evil_guard
        source = inspect.getsource(evil_guard.rally_eg)
        # Both the main loop and retry loop should have the guard
        assert source.count("any_doing(TroopAction.MARCHING)") >= 2
