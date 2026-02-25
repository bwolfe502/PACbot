import pytest
import sys
import os

# Add project root to path so tests can import modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def mock_device():
    """A fake ADB device ID for tests."""
    return "127.0.0.1:9999"


@pytest.fixture
def mock_device_b():
    """A second fake ADB device ID for multi-device tests."""
    return "127.0.0.1:8888"


@pytest.fixture(autouse=True)
def reset_quest_state():
    """Clear quest tracking and rally blacklist dicts before each test."""
    from actions import (
        _quest_rallies_pending, _quest_last_seen, _quest_pending_since,
        _rally_owner_blacklist, _rally_owner_failures,
    )
    _quest_rallies_pending.clear()
    _quest_last_seen.clear()
    _quest_pending_since.clear()
    _rally_owner_blacklist.clear()
    _rally_owner_failures.clear()
    yield
    _quest_rallies_pending.clear()
    _quest_last_seen.clear()
    _quest_pending_since.clear()
    _rally_owner_blacklist.clear()
    _rally_owner_failures.clear()
