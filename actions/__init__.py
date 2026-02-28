"""PACbot game actions package.

Re-exports all public action functions so that ``from actions import X``
continues to work throughout the codebase.

Submodules:
    _helpers    — cross-module shared state (_last_depart_slot, _interruptible_sleep)
    quests      — quest system, tracking, tower quest
    rallies     — rally joining, war rallies, rally owner blacklist
    combat      — attack, phantom clash, reinforce, target, teleport
    titans      — titan rally, AP restoration
    evil_guard  — Evil Guard rally (dark priests + boss)
    farming     — gold mining, mithril gathering
"""

# -- quests --
from actions.quests import (
    check_quests, get_quest_tracking_state, reset_quest_tracking,
    occupy_tower, recall_tower_troop,
    # State + internals (used by tests)
    _classify_quest_text, _deduplicate_quests, _get_actionable_quests,
    _all_quests_visually_complete,
    _track_quest_progress, _record_rally_started, _effective_remaining,
    _quest_rallies_pending, _quest_last_seen, _quest_target,
    _quest_pending_since, _quest_rally_slots, _tower_quest_state,
    _is_troop_defending, _navigate_to_tower, _run_tower_quest,
    _wait_for_rallies, _ocr_quest_rows, _claim_quest_rewards,
    PENDING_TIMEOUT_S,
)

# -- rallies --
from actions.rallies import (
    join_rally, join_war_rallies, reset_rally_blacklist,
    # State + internals (used by tests)
    _rally_owner_blacklist, _rally_owner_failures,
    _record_rally_owner_failure, _blacklist_rally_owner,
    _clear_rally_owner_failures, _is_rally_owner_blacklisted,
    RALLY_BLACKLIST_THRESHOLD, RALLY_BLACKLIST_EXPIRY_S,
)

# -- combat --
from actions.combat import (
    attack, phantom_clash_attack, reinforce_throne,
    target, teleport,
    _detect_player_at_eg,
)

# -- titans --
from actions.titans import restore_ap, rally_titan

# -- evil guard --
from actions.evil_guard import (
    rally_eg, search_eg_reset, test_eg_positions,
    EG_PRIEST_POSITIONS,
)

# -- farming --
from actions.farming import (
    mine_mithril, mine_mithril_if_due,
    gather_gold, gather_gold_loop,
)

# -- shared state --
from actions._helpers import _last_depart_slot
