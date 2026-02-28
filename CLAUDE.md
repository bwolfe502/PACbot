# PACbot — AI Technical Reference

Android game automation bot: ADB screenshots + OpenCV template matching + EasyOCR.
Runs on Windows with BlueStacks or MuMu Player emulators. GUI built with tkinter.

## Module Map

| File | Purpose | Key exports |
|------|---------|-------------|
| `main.py` | GUI entry point | Tkinter app, `create_gui()` |
| `runners.py` | Shared task runners | `run_auto_quest`, `run_auto_titan`, `run_repeat`, `run_once`, `launch_task`, `stop_task` |
| `settings.py` | Settings persistence | `DEFAULTS`, `load_settings`, `save_settings`, `SETTINGS_FILE` |
| `actions/` | Game actions package (7 submodules) | Re-exports all public functions via `__init__.py` |
| `actions/quests.py` | Quest system + tower quest | `check_quests`, `get_quest_tracking_state`, `reset_quest_tracking`, `occupy_tower` |
| `actions/rallies.py` | Rally joining + blacklist | `join_rally`, `join_war_rallies`, `reset_rally_blacklist` |
| `actions/combat.py` | Attacks, targeting, teleport | `attack`, `phantom_clash_attack`, `reinforce_throne`, `target`, `teleport` |
| `actions/titans.py` | Titan rally + AP restore | `rally_titan`, `restore_ap`, `_restore_ap_from_open_menu`, `_close_ap_menu`, `_MAX_TITAN_SEARCH_ATTEMPTS` |
| `actions/evil_guard.py` | Evil Guard attack sequence | `rally_eg`, `search_eg_reset`, `test_eg_positions`, `_handle_ap_popup` |
| `actions/farming.py` | Gold + mithril gathering | `mine_mithril`, `gather_gold`, `gather_gold_loop` |
| `actions/_helpers.py` | Shared state + utilities | `_interruptible_sleep`, `_last_depart_slot` |
| `vision.py` | Screenshots, template matching, OCR, ADB input | `load_screenshot`, `find_image`, `find_all_matches`, `tap_image`, `wait_for_image_and_tap`, `read_text`, `read_number`, `read_ap`, `adb_tap`, `adb_swipe`, `adb_keyevent`, `timed_wait`, `tap`, `logged_tap`, `get_last_best`, `save_failure_screenshot` |
| `navigation.py` | Screen detection + state-machine navigation | `check_screen`, `navigate` |
| `troops.py` | Troop counting (pixel), status model (OCR), healing | `troops_avail`, `all_troops_home`, `heal_all`, `read_panel_statuses`, `get_troop_status`, `detect_selected_troop`, `capture_portrait`, `store_portrait`, `identify_troop`, `TroopAction`, `TroopStatus`, `DeviceTroopSnapshot` |
| `territory.py` | Territory grid analysis + auto-occupy | `attack_territory`, `auto_occupy_loop`, `open_territory_manager`, `sample_specific_squares` |
| `config.py` | Global mutable state, enums, constants | `QuestType`, `RallyType`, `Screen`, ADB path, thresholds, team colors, `alert_queue` |
| `devices.py` | ADB device detection + emulator window mapping | `auto_connect_emulators`, `get_devices`, `get_emulator_instances` |
| `botlog.py` | Logging, metrics, timing | `setup_logging`, `get_logger`, `set_console_verbose`, `StatsTracker`, `timed_action`, `stats`, `BOT_VERSION` |
| `web/dashboard.py` | Flask web dashboard (mobile remote control) | `create_app`, routes, auto-mode toggles |

## Dependency Graph

```
main.py (GUI)
  ├─ config, settings, runners
  ├─ devices
  ├─ navigation ──┬─ vision ── config, botlog
  ├─ vision       │
  ├─ troops ──────┤
  ├─ actions ─────┘
  ├─ territory ── actions (teleport)
  └─ botlog (standalone)

runners.py (shared task runners)
  ├─ config, settings
  ├─ actions (public API)
  └─ troops, navigation, territory

actions/ package (internal deps — no cycles)
  _helpers   → (leaf — no deps)
  farming    → (leaf — no action deps)
  combat     → _helpers
  titans     → _helpers
  rallies    → _helpers
  evil_guard → titans, combat, _helpers
  quests     → (lazy) rallies, combat, titans, evil_guard, farming, _helpers

web/dashboard.py (Flask)
  ├─ config, devices, navigation, vision, troops, actions, territory, botlog
  ├─ runners (shared task runners — no duplication)
  └─ settings (shared persistence — no duplication)
```

`botlog.py` and `config.py` have no internal dependencies (safe to import anywhere).

## Enums (config.py)

All enums inherit from `_StrEnum(str, Enum)` with a `__format__` override for Python 3.14 f-string compatibility.

```python
QuestType: TITAN, EVIL_GUARD, PVP, GATHER, FORTRESS, TOWER
RallyType: CASTLE, PASS, TOWER, GROOT
Screen:    MAP, BATTLE_LIST, ALLIANCE_QUEST, TROOP_DETAIL, TERRITORY,
           WAR, PROFILE, ALLIANCE, KINGDOM, UNKNOWN, LOGGED_OUT
```

## Key Constants (config.py)

| Constant | Value | Purpose |
|----------|-------|---------|
| `ADB_COMMAND_TIMEOUT` | 10 | Timeout (seconds) for all ADB shell calls |
| `SCREEN_MATCH_THRESHOLD` | 0.8 | Template confidence for screen detection |
| `MAX_RALLY_ATTEMPTS` | 15 | Max iterations in rally join loop |
| `MAX_HEAL_ITERATIONS` | 20 | Max heal_all cycles (5 troops + safety buffer) |
| `QUEST_PENDING_TIMEOUT` | 360 | Seconds before pending rally expires (6 min) |
| `RALLY_PANEL_WAIT_ENABLED` | True | Use troop panel to wait for rallies |
| `RALLY_WAIT_POLL_INTERVAL` | 5 | Seconds between panel status polls |
| `DEBUG_SCREENSHOT_MAX` | 50 | Rolling cap on debug screenshots |
| `CLICK_TRAIL_MAX` | 50 | Rolling cap on click trail images |
| `FAILURE_SCREENSHOT_MAX` | 200 | Cap on persistent failure screenshots |
| `SQUARE_SIZE` | 42.5 | Territory grid square dimension (px) |
| `GRID_WIDTH`, `GRID_HEIGHT` | 24, 24 | Territory grid dimensions |
| `THRONE_SQUARES` | (11,11), (11,12), (12,11), (12,12) | Untouchable throne cells |
| `AP_COST_RALLY_TITAN` | 20 | AP cost per titan rally |
| `AP_COST_EVIL_GUARD` | 70 | AP cost per evil guard rally |
| `MAX_CONSECUTIVE_ERRORS` | 5 | Stop task after N consecutive action exceptions |
| `MAX_CONSECUTIVE_NAV_FAILURES` | 10 | Stop task after N consecutive navigate() failures |

## Mutable Global State (config.py)

All session-scoped, reset on restart:
- `DEVICE_TOTAL_TROOPS[device]` — Total troops per device (default 5)
- `LAST_ATTACKED_SQUARE[device]` — Last territory attack target
- `MANUAL_ATTACK_SQUARES` / `MANUAL_IGNORE_SQUARES` — Territory overrides (set of (row, col))
- `MIN_TROOPS_AVAILABLE` — Minimum troop threshold
- `AUTO_HEAL_ENABLED`, `AUTO_RESTORE_AP_ENABLED` — Feature toggles
- `DEVICE_STATUS[device]` — Current status message shown in GUI
- `MY_TEAM_COLOR`, `ENEMY_TEAMS` — Territory team config
- `running_tasks` — Dict of active task_key → threading.Event (stop signals)
- `auto_occupy_running`, `auto_occupy_thread` — Territory auto-occupy state
- `MITHRIL_ENABLED`, `MITHRIL_INTERVAL`, `LAST_MITHRIL_TIME`, `MITHRIL_DEPLOY_TIME` — Mithril mining state
- `BUTTONS` — Dict mapping button names to `{"x": int, "y": int}` coordinates (used by `vision.tap()`)

## Architecture Patterns

### Threading & Task Launching (runners.py + main.py)
- Main thread: Tkinter event loop (GUI)
- Worker threads: Daemon threads per action, launched on button click
- `launch_task(device, task_name, target_func, stop_event, args)` — Spawns daemon thread (in `runners.py`)
- `stop_task(task_key)` — Sets the stop event; `stop_all_tasks_matching(suffix)` for bulk stop (in `runners.py`)
- Per-device lock: `config.get_device_lock(device)` prevents concurrent tasks on same device
- Stop signals: `threading.Event()` stored in `config.running_tasks[task_key]`
- `TASK_FUNCTIONS` dict maps GUI labels → callable functions
- Looping is managed by `runners.py` task runners (`run_once` / `run_repeat`), not by actions. Actions accept a `stop_check` callback for cooperative cancellation
- `runners.py` is shared by both `main.py` (GUI) and `web/dashboard.py` (Flask) — no duplication
- Thread-local storage in vision.py for `get_last_best()` template scores
- **Error recovery**: All auto runners track consecutive failures. After `MAX_CONSECUTIVE_ERRORS` (5) consecutive exceptions, the task stops. After `MAX_CONSECUTIVE_NAV_FAILURES` (10) consecutive navigate() failures, the task stops. Counters reset on any success. Backoff: `10 + N*5` seconds between retries.
- **ADB auto-reconnect**: `_try_reconnect(device)` in vision.py runs `adb connect` on TCP devices when `load_screenshot`/`adb_tap`/`adb_swipe` timeout. One retry after reconnect.

### Screen Resolution
Fixed **1080x1920** (portrait). All pixel coordinates, template regions, and OCR crop zones are calibrated to this resolution. Emulator must be set to this before running.

### Device Convention
Every game action takes `device` (ADB device ID string) as its **first argument**.
Device IDs are either `"127.0.0.1:<port>"` (TCP) or `"emulator-<port>"` (local ADB).

### Template Matching (vision.py)
- Templates stored in `elements/` directory as PNG files
- Uses `cv2.TM_CCOEFF_NORMED`, default threshold 0.8
- `IMAGE_REGIONS` dict constrains search area per template (faster than full-screen)
- Fallback to full-screen search if region miss (logs warning — region needs widening)
- Dynamic region learning: `StatsTracker` accumulates hit positions, auto-narrows search after 3+ hits
- `TAP_OFFSETS` dict: some templates need offset taps (e.g. depart.png +75px x to dodge chat overlay)
- `get_last_best()` returns thread-local best score on miss (useful for confidence logging)
- **Preferred over blind taps**: `wait_for_image_and_tap` replaces `logged_tap` where button position
  varies (e.g. `gather.png` in gold mine popup, where depart y-position varies: 950, 1128, 1307)

### OCR (vision.py)
- Windows: EasyOCR (deep learning, ~500-2000ms/call on CPU)
- macOS: Apple Vision framework (native, ~30ms/call)
- `read_text(screen, region, allowlist)` — text from screen region
- `read_number(screen, region)` — integer, handles comma/period thousands separators
- `read_ap(device, retries=5)` — returns `(current_ap, max_ap)` tuple

### Screen Navigation (navigation.py)
State machine via `navigate(target_screen, device)`:
1. `check_screen(device)` identifies current screen (matches all `SCREEN_TEMPLATES`, picks highest confidence)
2. Auto-dismisses popups (critical popups before screen check, soft popups after)
3. Routes to target screen via intermediate screens (e.g. MAP → ALLIANCE → WAR)
4. Verifies arrival with `_verify_screen()` (retries twice)
5. Recursion guard: max depth 3

**Unknown screen recovery** — `_recover_to_known_screen(device)` uses 4-phase escalation:
1. Template-based dismiss: close X, cancel button, back arrow (x2)
2. Android BACK key (`adb_keyevent(device, 4)`) — OS-level dismiss for popups without X
3. Center screen tap (540, 960) — dismiss transparent/click-through overlays
4. Nuclear: 3x BACK + center tap + 5s wait

`_last_unknown_info[device]` tracks the best template match when UNKNOWN is returned, enabling
"likely MAP" detection (70-79% score) for smarter recovery decisions.

### Adaptive Timing (vision.py + botlog.py)
`timed_wait(device, condition_fn, budget_s, label)`:
- Polls condition_fn every ~150ms until met or budget expires
- `StatsTracker.get_adaptive_budget()` can shorten budget based on P90 of observed transition times
- Config: min 8 samples, 80% success rate gate, 1.3x headroom, never below 40% of original budget
- Persists across sessions (loads from previous session stats file)

### Timed Action Decorator (botlog.py)
`@timed_action(action_name)` wraps game actions:
- Logs entry/exit with timing
- Records success/failure/duration to StatsTracker
- Saves failure screenshot on exception
- Expects `device` as first positional arg

### Troop System (troops.py)
**Counting** — Pixel-based: checks cyan color `[107, 247, 255]` at known Y positions on MAP screen. Returns 0-5.

**Status model** — `TroopStatus` dataclass with `TroopAction` enum (HOME, DEFENDING, OCCUPYING, MARCHING, RETURNING, STATIONING, GATHERING, RALLYING, BATTLING, ADVENTURING). `DeviceTroopSnapshot` holds full troop state with helpers like `home_count`, `deployed_count`, `soonest_free()`.

**Healing** — `heal_all(device)`: finds heal.png, taps through heal dialogs in a loop until no more heal buttons.

### Territory System (territory.py)
- 24x24 grid, squares are 42.5px
- Border color detection: sample pixels, match to `BORDER_COLORS` (yellow/green/red/blue) with tolerance
- Flag detection: red pixel analysis in square
- Adjacency check: only attack squares bordering own territory
- `MANUAL_ATTACK_SQUARES` / `MANUAL_IGNORE_SQUARES` override auto-detection
- `open_territory_manager(device)`: Tkinter window for visual square selection (click to cycle: none → attack → ignore)

### Rally Owner Blacklist (actions/rallies.py)
- `_ocr_rally_owner()` reads "{Name}'s Troop" from war screen card
- `_ocr_error_banner()` detects in-game error banners → instant blacklist
- 2 consecutive failures without error text → blacklist owner
- 30-minute expiry, reset on auto-quest start
- Per-device, session-scoped

### Titan Search Retry (actions/titans.py)
`rally_titan` searches for the titan, which centers the map on it, then blind-taps (540, 900)
to select it. If the titan walks off-center before the tap lands, the confirm popup never appears
and depart times out. The search → center-tap → depart-poll sequence is wrapped in a retry loop
(`_MAX_TITAN_SEARCH_ATTEMPTS = 3`). On miss, saves a debug screenshot, navigates back to MAP to
clear stale UI, then re-opens the rally menu and re-searches — re-centering the camera on the
titan's current position.

### AP Restoration (actions/titans.py + config.py)
Order: free restores → potions (small→large) → gems.
Controlled by `AP_USE_FREE`, `AP_USE_POTIONS`, `AP_ALLOW_LARGE_POTIONS`, `AP_USE_GEMS`, `AP_GEM_LIMIT`.

**Architecture**: The restoration logic is in `_restore_ap_from_open_menu(device, needed)` which assumes
the AP Recovery menu is already visible. Returns `(success, current_ap)`. `restore_ap()` wraps it with
menu navigation (MAP → search → AP button) and double-close. `_close_ap_menu(device, double_close=True)`
handles both cases: `True` for bot-opened menus (search menu behind), `False` for game-opened popups.

**Game-triggered AP popup**: When the game opens the AP Recovery popup (e.g. after tapping depart with
insufficient AP), `_handle_ap_popup(device, needed)` in `evil_guard.py` detects `apwindow.png`, restores
AP via `_restore_ap_from_open_menu`, and single-closes the popup. Used in `click_depart_with_fallback()`
(primary) and `poll_troop_ready()` (safety net).

### Settings Persistence (settings.py)
`settings.json` stores user preferences (auto-heal, AP options, intervals, territory teams).
Loaded on startup, saved on quit/restart. `DEFAULTS` dict provides fallback values.
Shared by both `main.py` (GUI) and `web/dashboard.py` (Flask).

### Web Dashboard (web/dashboard.py)
Mobile-friendly Flask app for remote control from any browser. Runs alongside tkinter GUI
in a background thread — both share the same process (`config.running_tasks`, `DEVICE_STATUS`, etc.).

**Enable**: `"web_dashboard": true` in `settings.json`, then access `http://<your-ip>:8080`.

**Architecture**:
- `create_app()` factory returns Flask app; started via `threading.Thread` in `main.py`
- Imports shared task runners from `runners.py` and settings from `settings.py` — no duplication
- `AUTO_RUNNERS` dict maps auto-mode keys → runner lambdas
- `TASK_FUNCTIONS` dict maps one-shot action names → callable functions
- Device list cached for 15s (`_DEVICE_CACHE_TTL`) to avoid spamming ADB on every poll
- CSS cache busting: `style.css?v=N` in `base.html` — bump on every CSS change

**Pages**: Dashboard (`/`), Settings (`/settings`), Logs (`/logs`)

**API endpoints**:
- `GET /api/status` — device statuses, troop snapshots, quest tracking, active tasks (polled every 3s)
- `POST /api/devices/refresh` — reconnect ADB devices
- `POST /tasks/start` — launch auto-mode or one-shot task
- `POST /tasks/stop` — stop a specific task
- `POST /tasks/stop-all` — stop all tasks
- `POST /settings` — save settings form
- `POST /api/restart` — save settings, stop all, `os.execv` restart
- `GET /api/logs` — last 150 log lines as JSON

**Dashboard UI components**:
- **Device card**: status dot (pulsing green when active), status text (color-coded), troop slots, quest pills
- **Auto mode toggles**: iOS-style toggle switches in 2-column grid, grouped by category (Combat/Farming/Events)
- **Action chips**: minimal bordered buttons in 3-column grid, farm actions (blue accent) and war actions (red accent)
- **Running tasks list**: active task names with circular stop (×) buttons
- **Bottom bar**: Stop All, Refresh, Restart — three equal compact buttons

**Auto mode groups** (vary by game mode):
- Broken Lands (`bl`): Combat (Pass Battle, Occupy Towers, Reinforce Throne) + Farming (Auto Quest, Rally Titans, Mine Mithril)
- Home Server (`rw`): Events (Join Groot) + Farming (Rally Titans, Mine Mithril) + Combat (Reinforce Throne)

**Templates**: `base.html` (nav, shared JS), `index.html` (dashboard), `settings.html`, `logs.html`

### Device Status System (config.py + all runners)
`config.DEVICE_STATUS[device]` holds the current status string displayed in both the tkinter GUI
and the web dashboard. Updated via `config.set_device_status(device, msg)`, cleared via
`config.clear_device_status(device)`.

**Conventions**:
- Title Case for all status strings: `"Rallying Titan..."`, `"Checking Quests..."`
- Expanded abbreviations: `"Evil Guard"` not `"EG"`
- Trailing ellipsis for active states: `"Mining Mithril..."`
- `"Idle"` when between cycles (default / fallback)

**Status text colors** (web dashboard JS classification):
- Cyan (`#64d8ff`): active/working — any status not matching below
- Amber (`#ffb74d`): waiting — status contains `"Waiting"`
- Gray (`#aab`): navigating — status contains `"Navigating"`
- Default gray (`#667`): idle

**rally_eg phase statuses** (detailed breakdown):
1. `"Searching for Evil Guard..."` — opening Evil Guard map
2. `"Killing Dark Priests (1/5)..."` — first priest probe attack
3. `"Marching to Dark Priest (1/5)..."` — waiting for first rally (long march)
4. `"Killing Dark Priests (N/5)..."` — priests 2-5 attack
5. `"Waiting for Rally (N/5)..."` — waiting for priests 2-5 rally completion
6. `"Retrying Missing Priests..."` — retry section for missed priests
7. `"Rallying Evil Guard..."` — final boss rally (P6)

## Debug & Observability

| Directory | Contents | Retention |
|-----------|----------|-----------|
| `logs/` | Rotating log files (5MB, 3 backups) | Auto-rotated |
| `debug/clicks/` | Click trail screenshots | Rolling, capped at `CLICK_TRAIL_MAX` |
| `debug/failures/` | Failure screenshots | Persistent, capped at 200 |
| `stats/` | Session stats JSON | Auto-saved every 5min, keeps 30 sessions |
| `debug/` | Debug screenshots (navigation) | Rolling, capped at `DEBUG_SCREENSHOT_MAX` |

## Tests

```bash
py -m pytest          # run all ~538 tests
py -m pytest -x       # stop on first failure
py -m pytest -k name  # filter by test name
```

No fixtures require a running emulator — all use mocked ADB/vision.

### Test Files

| File | Tests | Coverage |
|------|-------|----------|
| `test_vision.py` | `get_last_best`, `find_image`, `find_all_matches`, `read_number`, `read_text`, `read_ap`, `get_template`, `load_screenshot`, `adb_tap`, `adb_swipe`, `adb_keyevent`, `_try_reconnect`, ADB reconnect retry |
| `test_navigation.py` | `check_screen`, `navigate`, `_verify_screen`, `_recover_to_known_screen` (4-phase escalation: template dismiss, Android BACK key, center tap, nuclear) |
| `test_troops.py` | Troop pixel detection, status tracking, icon matching, portrait tracking, triangle detection, `heal_all` (nav failure, no-heal skip, single/multi/5-troop sequences, tap coordinates, wait budgets, safety cap, failure screenshots) |
| `test_botlog.py` | `StatsTracker`, `timed_action` decorator, `get_logger` |
| `test_config.py` | AP restore options clamping logic (gem limit bounds) |
| `test_devices.py` | `auto_connect_emulators`, `get_devices`, `get_emulator_instances` |
| `test_rally_blacklist.py` | Direct blacklist, failure thresholds, 30-min expiry, reset (`actions.rallies`) |
| `test_rally_wait.py` | Troop-tracked rally, slot tracking, panel-based waiting, false positive detection (`actions.quests`) |
| `test_quest_tracking.py` | Multi-device quest rally tracking, `_track_quest_progress`, `_record_rally_started` (`actions.quests`) |
| `test_check_quests_helpers.py` | `_deduplicate_quests`, `_get_actionable_quests` (`actions.quests`) |
| `test_classify_quest.py` | `_classify_quest_text` OCR classification (all QuestType values) (`actions.quests`) |
| `test_combat.py` | `_check_dead`, `_find_green_pixel`, `_detect_player_at_eg`, `teleport` (happy path, timeout, dead detection, cancel) (`actions.combat`) |
| `test_territory.py` | `_classify_square_team` (exact/noisy colors, thresholds, team configs), `_get_border_color` (sampling, clock avoidance), `_has_flag` (red pixel detection), `_is_adjacent_to_my_territory` (adjacency, throne, edges), `attack_territory` (full workflow), `auto_occupy_loop` (cycle, stop signal) (`territory`) |
| `test_gather_gold.py` | Gather gold flow (gather.png template tap, depart verification, retry logic), loop troop deployment with retry (`actions.farming`) |
| `test_tower_quest.py` | Tower/fortress quest occupy, recall, navigation (`actions.quests`) |
| `test_settings_validation.py` | `validate_settings` — type checks, range/choice validation, device_troops, warnings, schema sync |
| `test_task_runner.py` | `sleep_interval`, `launch_task`/`stop_task`, run_once, run_repeat, consecutive error recovery, settings load/save (`runners`) |

### Test Conventions
- Fixtures in `conftest.py`: `mock_device` ("127.0.0.1:9999"), `mock_device_b` ("127.0.0.1:8888")
- `reset_quest_state` autouse fixture calls `reset_quest_tracking()` + `reset_rally_blacklist()` before each test
- All ADB calls and screenshots are mocked via `unittest.mock.patch`
- Mock patches target the submodule where the function is used (e.g. `actions.farming.navigate`, not `actions.navigate`)
- Tests import directly from submodules (e.g. `from actions.quests import check_quests`)
- Test names: `test_<function>_<scenario>` (e.g. `test_find_image_returns_none_below_threshold`)
- Use `@pytest.mark.parametrize` for related test cases that vary only by input/expected values

## Git Workflow

- `master` — tagged releases only (v1.1.0, v1.2.0, v1.3.0)
- `dev` — integration branch, always working
- Feature branches: `feature/*`, `fix/*`, `cleanup/*` → PR into dev
- Conventional commits: `feat:`, `fix:`, `refactor:`, `test:` prefix
- Current version: see `version.txt`

## Project Files

```
PACbot/
├── CLAUDE.md            # AI technical reference (this file)
├── ROADMAP.md           # Development roadmap
├── TESTING.md           # Tester protocol (bug reporting + active testing guide)
├── main.py              # GUI entry point (~1830 lines)
├── runners.py           # Shared task runners (used by main.py + dashboard)
├── settings.py          # Settings persistence (used by main.py + dashboard)
├── actions/             # Game actions package
│   ├── __init__.py      # Re-exports all public functions
│   ├── _helpers.py      # Shared state (_last_depart_slot, _interruptible_sleep)
│   ├── quests.py        # Quest system + tower quest (~910 lines)
│   ├── rallies.py       # Rally joining + blacklist (~810 lines)
│   ├── combat.py        # Attacks, targeting, teleport (~390 lines)
│   ├── titans.py        # Titan rally + AP restore (~456 lines)
│   ├── evil_guard.py    # Evil Guard attack sequence (~850 lines)
│   └── farming.py       # Gold + mithril gathering (~350 lines)
├── vision.py            # CV + OCR + ADB input
├── navigation.py        # Screen detection + nav
├── troops.py            # Troop counting/status/healing
├── territory.py         # Territory grid + auto-occupy
├── config.py            # Enums, constants, global state
├── devices.py           # ADB device detection
├── botlog.py            # Logging + metrics
├── run.bat              # User entry point (venv + launch)
├── requirements.txt     # Python dependencies
├── settings.json        # User settings (auto-generated)
├── version.txt          # Current version string
├── elements/            # Template images for matching
│   └── statuses/        # Troop status icon templates
├── platform-tools/      # Bundled ADB executable
├── web/                 # Flask web dashboard
│   ├── dashboard.py     # App factory, routes (~600 lines)
│   ├── static/
│   │   └── style.css    # Mobile-first dark CSS (cache-busted ?v=N)
│   └── templates/
│       ├── base.html    # Nav, shared JS (fmtTime, quest labels, action classes)
│       ├── index.html   # Dashboard: device cards, toggles, actions, running list
│       ├── settings.html # Settings form
│       └── logs.html    # Log viewer
├── tests/               # pytest suite (~526 tests)
├── logs/                # Log files
├── stats/               # Session stats JSON
└── debug/               # Debug screenshots
    ├── clicks/          # Click trail images
    └── failures/        # Failure screenshots
```
