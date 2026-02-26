import cv2
import numpy as np
import os
import time
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Tuple

import config
from vision import (load_screenshot, tap_image, adb_tap, logged_tap,
                    get_template, save_failure_screenshot, timed_wait)
from navigation import navigate
from config import Screen
from botlog import get_logger, timed_action


# ============================================================
# TROOP STATUS DATA MODEL
# ============================================================

class TroopAction(Enum):
    HOME = "Home"
    DEFENDING = "Defending"
    OCCUPYING = "Occupying"
    MARCHING = "Marching"
    RETURNING = "Returning"
    STATIONING = "Stationing"
    GATHERING = "Gathering"
    RALLYING = "Rallying"
    BATTLING = "Battling"
    ADVENTURING = "Adventuring"


@dataclass
class TroopStatus:
    """Status of a single troop slot."""
    action: TroopAction
    seconds_remaining: Optional[int] = None   # None for HOME troops
    read_at: float = field(default_factory=time.time)

    @property
    def deadline(self) -> Optional[float]:
        """Epoch timestamp when this troop will be free."""
        if self.seconds_remaining is None:
            return None
        return self.read_at + self.seconds_remaining

    @property
    def time_left(self) -> Optional[int]:
        """Seconds remaining right now (decreases over time). 0 if past deadline."""
        if self.deadline is None:
            return None
        return max(0, int(self.deadline - time.time()))

    @property
    def is_home(self) -> bool:
        return self.action == TroopAction.HOME

    def __repr__(self):
        if self.is_home:
            return "TroopStatus(HOME)"
        left = self.time_left
        if left is not None:
            m, s = divmod(left, 60)
            return f"TroopStatus({self.action.value}, {m}:{s:02d} left)"
        return f"TroopStatus({self.action.value})"


@dataclass
class DeviceTroopSnapshot:
    """Complete troop state for one device at a point in time."""
    device: str
    troops: List[TroopStatus]
    read_at: float = field(default_factory=time.time)

    @property
    def home_count(self) -> int:
        return sum(1 for t in self.troops if t.is_home)

    @property
    def deployed_count(self) -> int:
        return len(self.troops) - self.home_count

    def troops_by_action(self, action: TroopAction) -> List[TroopStatus]:
        return [t for t in self.troops if t.action == action]

    def any_doing(self, action: TroopAction) -> bool:
        return any(t.action == action for t in self.troops)

    def soonest_free(self) -> Optional[TroopStatus]:
        """Deployed troop that will be free soonest, or None if all home."""
        deployed = [t for t in self.troops if not t.is_home and t.deadline is not None]
        if not deployed:
            return None
        return min(deployed, key=lambda t: t.deadline)

    @property
    def age_seconds(self) -> float:
        """How many seconds ago this snapshot was taken."""
        return time.time() - self.read_at


# ============================================================
# TROOP STATUS PARSING
# ============================================================

# Keywords for fuzzy OCR matching (lowercase)
_ACTION_KEYWORDS = {
    TroopAction.DEFENDING:  ["defending", "defend"],
    TroopAction.OCCUPYING:  ["occupying", "occupy"],
    TroopAction.MARCHING:   ["marching", "march"],
    TroopAction.RETURNING:  ["returning", "return"],
    TroopAction.STATIONING: ["stationing", "station"],
    TroopAction.GATHERING:  ["gathering", "gather"],
    TroopAction.RALLYING:   ["rallying", "rally"],
    TroopAction.BATTLING:   ["battling", "battle"],
    TroopAction.ADVENTURING: ["adventuring", "adventur"],
}


def _parse_timer(text: str) -> Optional[int]:
    """Parse 'MM:SS' or 'H:MM:SS' timer text to total seconds."""
    text = text.strip()
    # H:MM:SS
    m = re.match(r"(\d+):(\d{1,2}):(\d{2})", text)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    # MM:SS
    m = re.match(r"(\d{1,2}):(\d{2})", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


def _classify_action(text: str) -> Optional[TroopAction]:
    """Match OCR text to a TroopAction via keyword lookup."""
    lower = text.lower()
    for action, keywords in _ACTION_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return action
    return None


# ============================================================
# TROOP STATUS STORAGE (thread-safe)
# ============================================================

_troop_status_lock = threading.Lock()
_troop_status: Dict[str, DeviceTroopSnapshot] = {}


def _store_snapshot(device: str, snapshot: DeviceTroopSnapshot):
    with _troop_status_lock:
        _troop_status[device] = snapshot


def _get_snapshot(device: str) -> Optional[DeviceTroopSnapshot]:
    with _troop_status_lock:
        return _troop_status.get(device)


# ============================================================
# TROOP MANAGEMENT
# ============================================================

# Pixel positions and patterns for troop detection
_TROOP_X = 160
_TROOP_COLOR = np.array([107, 247, 255])
_TROOP_TOLERANCE = 10

# Y positions for each troop count pattern
# 0 troops: all 5 slots occupied at these Y positions
# 1 troop: 4 slots at shifted positions, etc.
_SLOT_PATTERNS = {
    0: {"match": [640, 800, 960, 1110, 1270], "no_match": []},
    1: {"match": [720, 880, 1040, 1200], "no_match": []},
    2: {"match": [800, 960, 1110], "no_match": [640, 1270]},
    3: {"match": [880, 1040], "no_match": [720, 1200]},
    4: {"match": [960], "no_match": [640, 800, 1110, 1270]},
}

def troops_avail(device):
    """Check how many troops are available (0-5) by checking pixel colors.
    Only valid on map_screen — verifies using the screenshot before reading pixels."""
    log = get_logger("troops", device)
    screen = load_screenshot(device)

    if screen is None:
        log.warning("Failed to load screenshot for troops check")
        return 0

    # Verify we're on map_screen using the same screenshot (no extra ADB call).
    # Troop pixel positions only make sense on the map screen.
    map_tpl = get_template("elements/map_screen.png")
    if map_tpl is not None:
        result = cv2.matchTemplate(screen, map_tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)
        if max_val < 0.8:
            log.warning("troops_avail called but not on map_screen (best: %.0f%%) — returning 0", max_val * 100)
            return 0

    def is_yellow(y):
        pixel = screen[y, _TROOP_X].astype(np.int16)
        return np.all(np.abs(pixel - _TROOP_COLOR) < _TROOP_TOLERANCE)

    # Pixel patterns are calibrated for 5-troop accounts.
    # For accounts with fewer total troops, adjust the detected count.
    total = config.DEVICE_TOTAL_TROOPS.get(device, 5)
    offset = 5 - total  # e.g. 4-troop account → offset 1

    for count, pattern in _SLOT_PATTERNS.items():
        if (all(is_yellow(y) for y in pattern["match"]) and
            all(not is_yellow(y) for y in pattern["no_match"])):
            adjusted = max(0, count - offset)
            log.debug("Troops available: %d%s", adjusted,
                      f" (raw {count}, account has {total})" if offset else "")
            return adjusted

    adjusted = max(0, 5 - offset)
    log.debug("Troops available: %d%s", adjusted,
              f" (raw 5, account has {total})" if offset else "")
    return adjusted

def all_troops_home(device):
    """Check if all troops are home (troops_avail matches account total)"""
    total = config.DEVICE_TOTAL_TROOPS.get(device, 5)
    return troops_avail(device) == total

@timed_action("heal_all")
def heal_all(device):
    """Check if we're on map_screen and if heal is needed, then heal all troops.
    Repeats until no more heal button is found (all troops healed)."""
    log = get_logger("troops", device)
    if not navigate(Screen.MAP, device):
        log.warning("Failed to navigate to map screen for healing")
        return False

    healed_any = False
    for _ in range(config.MAX_HEAL_ITERATIONS):
        if not tap_image("heal.png", device):
            break
        healed_any = True
        log.debug("Starting heal sequence...")
        timed_wait(device, lambda: False, 1, "heal_dialog_open")
        logged_tap(device, 700, 1460, "heal_all_btn")
        timed_wait(device, lambda: False, 1, "heal_confirm_ready")
        logged_tap(device, 542, 1425, "heal_confirm")
        timed_wait(device, lambda: False, 1, "heal_result_show")
        logged_tap(device, 1000, 200, "heal_close")
        timed_wait(device, lambda: False, 2, "heal_close_settle")
    else:
        log.warning("Heal loop hit safety cap (%d iterations) — possible stuck UI", config.MAX_HEAL_ITERATIONS)
        save_failure_screenshot(device, "heal_stuck_ui")

    if healed_any:
        log.info("Heal sequence complete — all troops healed")
        # Navigate back to map_screen to ensure clean state
        navigate(Screen.MAP, device)
    else:
        log.debug("No healing needed")
    return True


# ============================================================
# TROOP STATUS QUERY API
# ============================================================
# These functions query the cached troop status snapshot.
# The reading logic (how to populate the snapshot from the screen)
# is not yet implemented — it needs careful study of the game's
# troop panel layout before it can be built.

def get_troop_status(device) -> Optional[DeviceTroopSnapshot]:
    """Get the most recently cached troop status (no new screenshot)."""
    return _get_snapshot(device)


def next_troop_free_in(device) -> Optional[int]:
    """Seconds until next troop is free, based on cached status.
    Returns 0 if a troop is already home, None if no data."""
    snapshot = _get_snapshot(device)
    if snapshot is None:
        return None
    if snapshot.home_count > 0:
        return 0
    soonest = snapshot.soonest_free()
    if soonest is None:
        return None
    return soonest.time_left


def is_any_troop_doing(device, action: TroopAction) -> Optional[bool]:
    """Check if any troop is performing a specific action (cached data).
    Returns None if no data."""
    snapshot = _get_snapshot(device)
    if snapshot is None:
        return None
    return snapshot.any_doing(action)


# ============================================================
# STATUS ICON TEMPLATES
# ============================================================

_STATUS_ICON_DIR = os.path.join(os.path.dirname(__file__), "elements", "statuses")

# Map action → template filename (lazy-loaded)
_STATUS_ICON_FILES = {
    TroopAction.RETURNING:   "returning.png",
    TroopAction.STATIONING:  "stationing.png",
    TroopAction.GATHERING:   "gathering.png",
    TroopAction.RALLYING:    "rallying.png",
    TroopAction.DEFENDING:   "defending.png",
    TroopAction.MARCHING:    "marching.png",
    TroopAction.BATTLING:    "battling.png",
    TroopAction.OCCUPYING:   "occupying.png",
    # ADVENTURING shares the gathering icon — disambiguated by text if needed
}

_status_templates: Dict[TroopAction, np.ndarray] = {}
_status_templates_loaded = False


def _load_status_templates():
    """Lazy-load all status icon templates from elements/statuses/."""
    global _status_templates_loaded
    if _status_templates_loaded:
        return
    for action, filename in _STATUS_ICON_FILES.items():
        path = os.path.join(_STATUS_ICON_DIR, filename)
        tpl = cv2.imread(path)
        if tpl is not None:
            _status_templates[action] = tpl
        else:
            log = get_logger("troops")
            log.warning("Missing status icon template: %s", path)
    _status_templates_loaded = True


_ICON_MATCH_THRESHOLD = 0.65
_CARD_HEIGHT = 160


def _match_status_icon(card_img: np.ndarray) -> Tuple[Optional[TroopAction], float]:
    """Match a card image against all status icon templates.
    Returns (best_action, best_score) or (None, 0.0) if no match."""
    _load_status_templates()
    best_action = None
    best_score = 0.0
    # Search the top portion of the card where the TR icon lives
    icon_region = card_img[0:50, 100:170]
    for action, tpl in _status_templates.items():
        result = cv2.matchTemplate(icon_region, tpl, cv2.TM_CCOEFF_NORMED)
        _, score, _, _ = cv2.minMaxLoc(result)
        if score > best_score:
            best_score = score
            best_action = action
    if best_score < _ICON_MATCH_THRESHOLD:
        return None, best_score
    return best_action, best_score


# ============================================================
# MAP PANEL STATUS READING
# ============================================================

def read_panel_statuses(device, screen=None) -> Optional[DeviceTroopSnapshot]:
    """Read troop statuses from the map screen panel via icon template matching.

    Takes a screenshot (or reuses `screen`), detects how many troops are deployed,
    then matches each deployed card's TR icon against known status templates.
    Stores and returns a DeviceTroopSnapshot.
    """
    log = get_logger("troops", device)

    if screen is None:
        screen = load_screenshot(device)
    if screen is None:
        log.warning("read_panel_statuses: no screenshot")
        return None

    # Verify map_screen
    map_tpl = get_template("elements/map_screen.png")
    if map_tpl is not None:
        result = cv2.matchTemplate(screen, map_tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)
        if max_val < 0.8:
            log.warning("read_panel_statuses: not on map_screen (%.0f%%)", max_val * 100)
            return None

    # Determine available/deployed count using pixel check on this screenshot
    total = config.DEVICE_TOTAL_TROOPS.get(device, 5)
    offset = 5 - total

    def is_yellow(y):
        pixel = screen[y, _TROOP_X].astype(np.int16)
        return np.all(np.abs(pixel - _TROOP_COLOR) < _TROOP_TOLERANCE)

    avail_raw = 5  # Default: no pattern matched = all home
    for count, pattern in _SLOT_PATTERNS.items():
        if (all(is_yellow(y) for y in pattern["match"]) and
                all(not is_yellow(y) for y in pattern["no_match"])):
            avail_raw = count
            break

    avail = max(0, avail_raw - offset)
    deployed_count = total - avail

    if deployed_count == 0:
        # All home — no cards to read
        troops = [TroopStatus(action=TroopAction.HOME) for _ in range(total)]
        snapshot = DeviceTroopSnapshot(device=device, troops=troops)
        _store_snapshot(device, snapshot)
        return snapshot

    # Get the Y midpoints for the deployed cards
    # _SLOT_PATTERNS[avail_raw]["match"] gives the midpoints of deployed cards
    card_midpoints = _SLOT_PATTERNS.get(avail_raw, {}).get("match", [])
    if not card_midpoints:
        log.warning("read_panel_statuses: no card midpoints for avail_raw=%d", avail_raw)
        return None

    troops = []
    now = time.time()

    for mid_y in card_midpoints:
        card_top = mid_y - _CARD_HEIGHT // 2
        card_bottom = card_top + _CARD_HEIGHT
        # Crop the card from the screenshot (x=10 to x=180)
        card_img = screen[card_top:card_bottom, 10:180]

        action, score = _match_status_icon(card_img)
        if action is not None:
            log.debug("Card at y=%d: %s (%.0f%%)", mid_y, action.value, score * 100)
            troops.append(TroopStatus(action=action, read_at=now))
        else:
            log.warning("Card at y=%d: unknown icon (best %.0f%%)", mid_y, score * 100)
            troops.append(TroopStatus(action=TroopAction.MARCHING, read_at=now))

    # Pad with HOME troops for available slots
    for _ in range(avail):
        troops.append(TroopStatus(action=TroopAction.HOME, read_at=now))

    snapshot = DeviceTroopSnapshot(device=device, troops=troops, read_at=now)
    _store_snapshot(device, snapshot)
    log.debug("Panel status: %s", snapshot.troops)
    return snapshot


# ============================================================
# PORTRAIT TRACKING (runtime, per-device)
# ============================================================

_portrait_lock = threading.Lock()
_portraits: Dict[str, Dict[int, np.ndarray]] = {}  # {device: {slot_id: portrait_array}}

# Portrait safe zone within a card (between TL/TR icons, above text overlay)
_PORTRAIT_Y1 = 5
_PORTRAIT_Y2 = 65
_PORTRAIT_X1 = 45
_PORTRAIT_X2 = 120


def capture_portrait(screen: np.ndarray, card_top: int) -> np.ndarray:
    """Crop the portrait safe zone from a card on the map panel."""
    return screen[card_top + _PORTRAIT_Y1:card_top + _PORTRAIT_Y2,
                  _PORTRAIT_X1:_PORTRAIT_X2].copy()


def store_portrait(device: str, slot_id: int, portrait: np.ndarray):
    """Store a portrait reference for a troop slot."""
    with _portrait_lock:
        if device not in _portraits:
            _portraits[device] = {}
        _portraits[device][slot_id] = portrait


def identify_troop(device: str, portrait: np.ndarray) -> Optional[int]:
    """Match a portrait against stored portraits for this device.
    Returns the slot_id with best score above 0.5, or None."""
    with _portrait_lock:
        device_portraits = _portraits.get(device, {})

    if not device_portraits:
        return None

    best_slot = None
    best_score = 0.0
    for slot_id, stored in device_portraits.items():
        if stored.shape != portrait.shape:
            continue
        result = cv2.matchTemplate(portrait, stored, cv2.TM_CCOEFF_NORMED)
        _, score, _, _ = cv2.minMaxLoc(result)
        if score > best_score:
            best_score = score
            best_slot = slot_id

    if best_score > 0.5:
        return best_slot
    return None


# ============================================================
# DEPARTURE SCREEN — TRIANGLE DETECTION
# ============================================================

# On the departure screen, a white triangle at x=185-225 marks which troop is selected.
# The left panel shows all troops (not just deployed) at ~178px spacing starting at y≈699.
_DEPART_TRIANGLE_X1 = 185
_DEPART_TRIANGLE_X2 = 225
_DEPART_TRIANGLE_BRIGHTNESS = 200  # All channels > this
_DEPART_SLOT_Y_POSITIONS = [699, 877, 1055, 1234, 1412]  # Troop 1-5 centers
_DEPART_SLOT_SPACING = 178


def detect_selected_troop(device, screen=None) -> Optional[int]:
    """Detect which troop slot is selected on the departure screen.
    Returns slot index 1-5, or None if no triangle found."""
    log = get_logger("troops", device)

    if screen is None:
        screen = load_screenshot(device)
    if screen is None:
        return None

    # Scan for white pixel cluster at x=185-225
    white_ys = []
    for y in range(400, 1450):
        strip = screen[y, _DEPART_TRIANGLE_X1:_DEPART_TRIANGLE_X2]
        if np.any(np.all(strip > _DEPART_TRIANGLE_BRIGHTNESS, axis=1)):
            white_ys.append(y)

    if not white_ys:
        log.debug("detect_selected_troop: no triangle found")
        return None

    # Cluster and find the main group (20-80px tall)
    clusters = []
    start = white_ys[0]
    prev = white_ys[0]
    for y in white_ys[1:]:
        if y - prev > 5:
            clusters.append((start, prev))
            start = y
        prev = y
    clusters.append((start, prev))

    # Take the largest cluster
    best = max(clusters, key=lambda c: c[1] - c[0])
    center_y = (best[0] + best[1]) // 2

    # Map to nearest slot position
    closest_slot = min(range(5), key=lambda i: abs(_DEPART_SLOT_Y_POSITIONS[i] - center_y))
    slot_id = closest_slot + 1  # 1-indexed

    log.debug("Triangle at y=%d-%d (center=%d) → troop %d", best[0], best[1], center_y, slot_id)
    return slot_id


def capture_departing_portrait(device, screen=None) -> Optional[Tuple[int, np.ndarray]]:
    """Capture the portrait of the troop being sent on the departure screen.
    Returns (slot_id, portrait_array) or None if detection fails."""
    log = get_logger("troops", device)

    if screen is None:
        screen = load_screenshot(device)
    if screen is None:
        return None

    slot_id = detect_selected_troop(device, screen)
    if slot_id is None:
        log.warning("capture_departing_portrait: could not detect selected troop")
        return None

    # The departure screen shows ALL troops in the left panel.
    # Card center Y for this slot:
    slot_center_y = _DEPART_SLOT_Y_POSITIONS[slot_id - 1]
    card_top = slot_center_y - _CARD_HEIGHT // 2

    portrait = capture_portrait(screen, card_top)
    store_portrait(device, slot_id, portrait)
    log.debug("Captured portrait for troop %d (card_top=%d)", slot_id, card_top)
    return slot_id, portrait
