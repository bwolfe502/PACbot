"""Cross-module shared state and utilities for the actions package.

Leaf module — no dependencies on other action submodules.

Key exports:
    _interruptible_sleep — cooperative sleep with stop_check
    _last_depart_slot    — mutable dict tracking which troop slot just departed
"""

import time


# Troop slot tracking — which troop slot was last used to depart.
# Written by rallies.join_rally, titans.rally_titan, evil_guard.rally_eg
# Read by quests._record_rally_started, cleared by quests.reset_quest_tracking
_last_depart_slot = {}  # {device: slot_id}


def _interruptible_sleep(seconds, stop_check):
    """Sleep for `seconds`, checking stop_check every 0.5s.
    Returns True if stop_check triggered (i.e., should abort)."""
    if not stop_check:
        time.sleep(seconds)
        return False
    end = time.time() + seconds
    while time.time() < end:
        if stop_check():
            return True
        time.sleep(min(0.5, max(0, end - time.time())))
    return False
