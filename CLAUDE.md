# 9Bot — AI Technical Reference

Android game automation bot: ADB screenshots + OpenCV template matching + EasyOCR.
Runs on Windows with BlueStacks or MuMu Player emulators. GUI built with tkinter.

## Module Map

| File | Purpose | Key exports |
|------|---------|-------------|
| `run_web.py` | Web-only entry point (primary) | `main` (pywebview + browser fallback) |
| `startup.py` | Shared initialization & shutdown | `initialize`, `shutdown`, `apply_settings`, `create_bug_report_zip`, `get_relay_config`, `device_hash`, `generate_device_token`, `generate_device_ro_token`, `validate_device_token`, `upload_bug_report`, `start_auto_upload`, `stop_auto_upload`, `upload_status` |
| `main.py` | Legacy GUI entry point (deprecated) | Tkinter app, `create_gui()` |
| `runners.py` | Shared task runners | `run_auto_quest`, `run_auto_titan`, `run_auto_groot`, `run_auto_pass`, `run_auto_occupy`, `run_auto_reinforce`, `run_auto_mithril`, `run_auto_gold`, `run_repeat`, `run_once`, `launch_task`, `stop_task`, `force_stop_all`, `stop_all_tasks_matching` |
| `settings.py` | Settings persistence | `DEFAULTS`, `load_settings`, `save_settings`, `SETTINGS_FILE` |
| `actions/` | Game actions package (7 submodules) | Re-exports all public functions via `__init__.py` |
| `actions/quests.py` | Quest system + tower quest + PVP attack | `check_quests`, `get_quest_tracking_state`, `get_quest_last_checked`, `reset_quest_tracking`, `occupy_tower`, `recall_tower_troop` |
| `actions/rallies.py` | Rally joining + blacklist | `join_rally`, `join_war_rallies`, `reset_rally_blacklist` |
| `actions/combat.py` | Attacks, targeting, teleport | `attack`, `phantom_clash_attack`, `reinforce_throne`, `target`, `teleport`, `teleport_benchmark` |
| `actions/titans.py` | Titan rally + AP restore | `rally_titan`, `restore_ap`, `_restore_ap_from_open_menu`, `_close_ap_menu`, `_MAX_TITAN_SEARCH_ATTEMPTS` |
| `actions/evil_guard.py` | Evil Guard attack sequence | `rally_eg`, `search_eg_reset`, `test_eg_positions`, `_handle_ap_popup` |
| `actions/farming.py` | Gold + mithril gathering | `mine_mithril`, `mine_mithril_if_due`, `gather_gold`, `gather_gold_loop` |
| `actions/_helpers.py` | Shared state + utilities | `_interruptible_sleep`, `_last_depart_slot` |
| `vision.py` | Screenshots, template matching, OCR, ADB input | `load_screenshot`, `find_image`, `find_all_matches`, `tap_image`, `wait_for_image_and_tap`, `read_text`, `read_number`, `read_ap`, `adb_tap`, `adb_swipe`, `adb_keyevent`, `timed_wait`, `tap`, `logged_tap`, `get_last_best`, `save_failure_screenshot`, `tap_tower_until_attack_menu`, `warmup_ocr` |
| `navigation.py` | Screen detection + state-machine navigation | `check_screen`, `navigate` |
| `troops.py` | Troop counting (pixel), status model (OCR), healing | `troops_avail`, `all_troops_home`, `heal_all`, `read_panel_statuses`, `get_troop_status`, `detect_selected_troop`, `capture_portrait`, `store_portrait`, `identify_troop`, `TroopAction`, `TroopStatus`, `DeviceTroopSnapshot` |
| `territory.py` | Territory grid analysis + auto-occupy | `attack_territory`, `auto_occupy_loop`, `open_territory_manager`, `diagnose_grid`, `scan_territory_coordinates`, `scan_test_squares` |
| `config.py` | Global mutable state, enums, constants | `QuestType`, `RallyType`, `Screen`, ADB path, thresholds, team colors, `alert_queue` |
| `devices.py` | ADB device detection + emulator window mapping | `auto_connect_emulators`, `get_devices`, `get_emulator_instances` |
| `botlog.py` | Logging, metrics, timing | `setup_logging`, `get_logger`, `set_console_verbose`, `StatsTracker`, `timed_action`, `stats`, `BOT_VERSION` |
| `web/dashboard.py` | Flask web dashboard (mobile remote control) | `create_app`, routes, auto-mode toggles |
| `license.py` | Machine-bound license keys | `get_license_key`, `activate_key`, `validate_license` |
| `tunnel.py` | WebSocket relay tunnel | `start_tunnel`, `stop_tunnel`, `tunnel_status` |
| `updater.py` | Auto-update from GitHub releases | `check_and_update`, `get_latest_release`, `get_current_version` |

## Dependency Graph

```
run_web.py (primary entry point)
  ├─ startup (initialize, shutdown)
  ├─ web/dashboard (create_app)
  └─ tunnel (optional relay)

main.py (legacy GUI — deprecated)
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
| `ALL_TEAMS` | ["yellow", "green", "red", "blue"] | All territory team colors |

## Mutable Global State (config.py)

All session-scoped, reset on restart:
- `DEVICE_TOTAL_TROOPS[device]` — Total troops per device (default 5)
- `LAST_ATTACKED_SQUARE[device]` — Last territory attack target
- `MANUAL_ATTACK_SQUARES` / `MANUAL_IGNORE_SQUARES` — Territory overrides (set of (row, col))
- `MIN_TROOPS_AVAILABLE` — Minimum troop threshold
- `AUTO_HEAL_ENABLED`, `AUTO_RESTORE_AP_ENABLED` — Feature toggles
- `DEVICE_STATUS[device]` — Current status message shown in GUI
- `MY_TEAM_COLOR`, `ENEMY_TEAMS` — Territory team config (`set_territory_config(my_team)` auto-derives enemies from `ALL_TEAMS`)
- `running_tasks` — Dict of active task_key → threading.Event (stop signals)
- `auto_occupy_running`, `auto_occupy_thread` — Territory auto-occupy state
- `MITHRIL_ENABLED_DEVICES` — Set of device IDs with mithril mining active (per-device toggle)
- `MITHRIL_INTERVAL`, `LAST_MITHRIL_TIME`, `MITHRIL_DEPLOY_TIME` — Mithril mining timing state
- `EG_RALLY_OWN_ENABLED`, `TITAN_RALLY_OWN_ENABLED` — If False, only join rallies — never start own
- `GATHER_ENABLED`, `GATHER_MINE_LEVEL`, `GATHER_MAX_TROOPS` — Gold gathering config
- `TOWER_QUEST_ENABLED` — Occupy tower for alliance quest
- `CLICK_TRAIL_ENABLED` — Save click trail screenshots
- `BUTTONS` — Dict mapping button names to `{"x": int, "y": int}` coordinates (used by `vision.tap()`)

## Architecture Patterns

### Threading & Task Launching (runners.py + main.py)
- `run_web.py` uses werkzeug `make_server` in a daemon thread, with pywebview blocking the main thread (or browser fallback with infinite sleep loop)
- Legacy `main.py`: Main thread runs Tkinter event loop (GUI)
- Worker threads: Daemon threads per action, launched on button click
- `launch_task(device, task_name, target_func, stop_event, args)` — Spawns daemon thread (in `runners.py`)
- `stop_task(task_key)` — Sets the stop event and immediately sets device status to `"Stopping {label}..."` (in `runners.py`). `_MODE_LABELS` dict maps mode keys to human-readable names (e.g. `"auto_quest"` → `"Auto Quest"`). `stop_all_tasks_matching(suffix)` for bulk stop.
- `force_stop_all()` — Force-kills every running task thread immediately using `ctypes.pythonapi.PyThreadState_SetAsyncExc` to inject `SystemExit` into each thread at the next Python bytecode instruction. Sets stop events first (cooperative), then force-kills, then clears `running_tasks` and `DEVICE_STATUS`. Used by `stop_all()` in `web/dashboard.py` for the Stop All button.
- Per-device lock: `config.get_device_lock(device)` prevents concurrent tasks on same device
- Stop signals: `threading.Event()` stored in `config.running_tasks[task_key]`
- `TASK_FUNCTIONS` dict maps GUI labels → callable functions
- Looping is managed by `runners.py` task runners (`run_once` / `run_repeat`), not by actions. Actions accept a `stop_check` callback for cooperative cancellation
- `runners.py` is shared by both `main.py` (GUI) and `web/dashboard.py` (Flask) — no duplication
- Thread-local storage in vision.py for `get_last_best()` template scores
- **Error recovery**: Auto runners wrap their main loop in try/except, logging errors and continuing. Navigation failures retry after a short delay.
- **Smart idle status**: `_deployed_status(device)` in `run_auto_quest` reads the troop snapshot and shows "Gathering/Defending..." instead of generic "Waiting for Troops..." when all troops are deployed.
- **Periodic quest check**: `run_auto_quest` calls `check_quests` every 60s (`_QUEST_CHECK_INTERVAL`) even when all troops are deployed, to detect quest completion and recall troops promptly.
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
- `warmup_ocr()` — pre-initializes OCR in background thread at startup (downloads EasyOCR models
  on first run, ~10-30s; macOS triggers Apple Vision framework warmup)

**Memory hardening** (Windows/EasyOCR):
- `_ocr_infer_lock` — serializes `readtext()` calls across device threads. Prevents PyTorch
  MKL/MKLDNN scratch buffer accumulation per thread (each thread allocates its own buffers).
- `ONEDNN_PRIMITIVE_CACHE_CAPACITY=8` env var (default 1024) — caps compiled kernel cache.
  Each unique OCR crop shape compiles a new kernel (~MB each); variable-size crops caused multi-GB bloat.
- `torch.set_num_threads(2)` — limits intra-op parallelism. Device threads already provide
  inter-op parallelism; default thread count causes oversubscription.
- `gc.collect()` called in `StatsTracker` auto-save timer (every 5 min) to release deferred OCR memory.

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
- `diagnose_grid(device)`: diagnostic tool — screenshots all 576 squares, classifies each using the same
  `_get_border_color` + `_classify_square_team` pipeline as `attack_territory`, logs a 24-row character grid
  (Y/G/R/B/?/T), team counts, unknown BGR values with nearest-color distances, and saves annotated debug
  image to `debug/territory_diag_{device}.png`. `sample_specific_squares` is retained as an alias for
  backward compatibility.

### Territory Coordinate Scanner (territory.py)
Maps grid squares to world coordinates by clicking each square (which teleports to its tower location)
and OCR-reading the coordinates from the MAP screen.

- `_COORD_OCR_REGION = (0, 1750, 1080, 1870)` — OCR crop region for coordinate text on MAP screen
- `_COORD_DB_PATH = "data/territory_coordinates.json"` — persistent coordinate database
- `_parse_coordinates(text)` — extracts `(x, y)` from OCR text like `"x:150, y:7050"`, handles common artifacts
- `scan_territory_coordinates(device, squares=None, save_screenshots=True)` — full scan: navigates to
  TERRITORY, clicks each square, OCR-reads coordinates, saves to JSON database. Merges with existing data.
  Screenshots saved to `debug/territory_coords/` for calibration. Defaults to all non-throne squares (572).
- `scan_test_squares(device)` — quick calibration: scans only 4 corner squares `(0,0), (0,23), (23,0), (23,23)`

**Coordinate patterns**: Column correlates with world X (col 0 ~ 150, col 23 ~ 7050). Row correlates
inversely with world Y (row 0 ~ 7050, row 23 ~ 150-200). ~300 world units per grid step, but not uniform
due to terrain. Mountain passes are strategic locations not on the territory grid.

### Rally Owner Blacklist (actions/rallies.py)
- `_ocr_rally_owner()` reads "{Name}'s Troop" from war screen card
- `_ocr_error_banner()` detects in-game error banners → instant blacklist
- 2 consecutive failures without error text → blacklist owner
- 30-minute expiry, reset on auto-quest start
- Per-device, session-scoped

### Quest Dispatch (actions/quests.py)
**Dispatch priority chain**: PVP attack → Tower quest → EG/Titan rallies → pending rally wait → gather gold.
PVP and Tower run first because they're quick single-troop dispatches (no AP, no waiting).
PVP dispatches a troop then continues to other quests while it marches (non-blocking).
Gold gathering is blocked while titan/EG rallies are in-flight (pending). The bot waits for
rally completion instead of deploying gather troops, preserving troop availability for retries.

**Gold mining gates**: Gold is the lowest priority action. Two additional guards prevent premature mining:
1. **PVP gate**: If a PVP quest is available but not yet dispatched (not on cooldown), gold mining
   is skipped. `_all_quests_visually_complete()` checks PVP cooldown; the actionable path also
   checks `_pvp_last_dispatch` before the `has_gather` branch.
2. **Pending rally gate**: If titan/EG rallies are in-flight, gold is blocked (preserves troop
   availability for retries).

**Stray troop recovery**: `_recall_stray_stationed(device)` runs at the start of each `check_quests`
cycle (on MAP screen). If the troop snapshot shows any STATIONING troops (stuck from a failed EG
rally), it taps `statuses/stationing.png` on the panel to center the map, then taps `stationed.png`
→ `return.png` to send the troop home.

**EG troop gate**: `_eg_troops_available(device)` requires 2 troops not gathering or defending.
Troops that are rallying, marching, returning, etc. are counted as available since they'll free up.
Falls back to `troops_avail() >= 2` if no troop snapshot exists.

**Tower quest**: `_navigate_to_tower()` opens target menu, taps **Friend tab** `(540, 330)`, looks for
`friend_marker.png`, then taps the marker to center the map on the tower. `occupy_tower()` then
taps the tower, reinforces, and departs. `recall_tower_troop()` recalls when the quest completes.
`_run_tower_quest()` handles three cases: no tower quests (recall stray defender), all complete
(recall), active quest (deploy if needed). Uses `_tower_quest_state` dict to track bot-deployed
troops within a session.

**Snapshot freshness & `_is_troop_defending_relaxed`**: `_is_troop_defending(device)` uses a 30s
snapshot freshness window, which is too short for `_run_tower_quest` — quest OCR takes 60+ seconds,
so by the time `_run_tower_quest` runs the snapshot from `check_quests` start is stale and we're
on the ALLIANCE_QUEST screen (can't read panel). `_is_troop_defending_relaxed(device)` extends the
window to 120s, which covers the typical quest-check duration. A defending troop's status is stable
(parked at a tower), so accepting older snapshots is safe. Used in `_run_tower_quest` (all three
branches) and `_all_quests_visually_complete` (tower quest gold-mining gate).

**PVP attack**: `_attack_pvp_tower()` uses `target()` (Enemy tab + `target_marker.png`) to navigate
to an enemy tower, then `tap_tower_until_attack_menu()` to open the attack menu, and `depart.png`
to send 1 troop. Troop availability is checked **after** `target()` (which navigates to MAP) —
`troops_avail()` requires MAP screen pixels, so checking earlier would always read 0. The PVP
attack panel renders depart at ~74% confidence (vs 80%+ for rally panels), so the depart tap
uses threshold 0.7. A single march completes the full 500M quest target in one go. A 10-minute
cooldown (`_PVP_COOLDOWN_S = 600`) prevents re-dispatch while the troop marches. PVP on cooldown
doesn't block gold mining (`_all_quests_visually_complete` is aware).

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
`settings.json` stores user preferences (auto-heal, AP options, intervals, territory teams,
`remote_access` toggle, `device_settings` per-device overrides). Loaded on startup, saved on
quit/restart. `DEFAULTS` dict provides fallback values. Shared by both `main.py` (GUI) and
`web/dashboard.py` (Flask). `updater.py` preserves `settings.json` across auto-updates
(`PRESERVE_FILES`).

### Web Dashboard (web/dashboard.py)
Mobile-friendly Flask app for remote control from any browser. `run_web.py` is now the primary
entry point — launches the Flask server in a daemon thread with pywebview providing a native
window (falls back to opening in the system browser if pywebview is unavailable). A phone access
banner displays the LAN URL for mobile remote control.

**Enable**: access `http://<your-ip>:8080` (started automatically by `run_web.py`).

**Architecture**:
- `create_app()` factory returns Flask app; started via werkzeug `make_server` in `run_web.py`
- Imports shared task runners from `runners.py` and settings from `settings.py` — no duplication
- `AUTO_RUNNERS` dict maps auto-mode keys → runner lambdas
- `TASK_FUNCTIONS` dict maps one-shot action names → callable functions
- Device list cached for 15s (`_DEVICE_CACHE_TTL`) to avoid spamming ADB on every poll
- CSS cache busting: `style.css?v=N` in `base.html` — bump on every CSS change
- Device ID validation: `/tasks/start` rejects device IDs not in `get_devices()` whitelist
- XSS prevention: dashboard JS uses `textContent` / DOM creation (no `innerHTML` for dynamic data)
- Relay auto-config: index route calls `get_relay_config()` to show remote URL when relay is active
- Thread safety: `_task_start_lock` prevents TOCTOU race on `running_tasks` during concurrent task starts
- Device ID validation: per-device settings routes (`/settings/device/<id>`) reject unknown device IDs

**Pages**: Dashboard (`/`), Settings (`/settings`), Debug (`/debug`), Logs (`/logs`), Territory Grid (`/territory`), Device View (`/d/<dhash>?token=...`)

**API endpoints**:
- `GET /api/status` — device statuses, troop snapshots, quest tracking, quest age (seconds since last check), mithril timer, active tasks, tunnel status (polled every 3s)
- `POST /api/devices/refresh` — reconnect ADB devices
- `POST /tasks/start` — launch auto-mode or one-shot task
- `POST /tasks/stop` — stop a specific task
- `POST /tasks/stop-all` — force-kill all tasks immediately (thread injection via `PyThreadState_SetAsyncExc`)
- `POST /settings` — save settings form
- `POST /api/restart` — save settings, stop all, `os.execv` restart
- `POST /api/quit` — graceful process termination (`os._exit(0)`)
- `GET /api/logs` — last 150 log lines as JSON
- `POST /api/bug-report` — generate and download bug report ZIP
- `GET /api/screenshot?device=<id>&download=1` — live device screenshot as PNG (download=1 forces file save)
- `GET /api/stream?device=<id>&fps=5&quality=30` — MJPEG live video stream (`multipart/x-mixed-replace`)
- `GET /api/qr?url=<encoded>` — generate QR code PNG (box_size=12, border=2) for dashboard modal
- `GET /api/territory/grid` — territory grid state (colors, flags, adjacency)
- `POST /api/territory/squares` — update manual attack/ignore squares
- `POST /tasks/stop-mode` — stop all tasks for an auto-mode
- `GET /settings/device/<device_id>` — per-device settings form
- `POST /settings/device/<device_id>` — save per-device settings
- `POST /settings/device/<device_id>/reset` — reset per-device settings to defaults

**Device-scoped routes** (per-device access for shared users):
- `GET /d/<dhash>?token=...` — filtered dashboard (single device)
- `GET /d/<dhash>/api/status` — status for one device only
- `GET /d/<dhash>/api/screenshot` — screenshot of this device
- `GET /d/<dhash>/api/stream` — MJPEG stream of this device
- `POST /d/<dhash>/tasks/start` — start task (full access only)
- `POST /d/<dhash>/tasks/stop` — stop task (full access only)
- `POST /d/<dhash>/tasks/stop-all` — stop all tasks (full access only)
- `POST /d/<dhash>/tasks/stop-mode` — stop auto-mode tasks (full access only)

**Dashboard UI components**:
- **Device card**: status dot (pulsing green when active), status text (color-coded), troop summary pills (grouped by action, colored, centered), quest pills with age timer, mithril countdown timer (purple accent)
- **Access banners**: LAN + relay URL banners with tap-to-copy, QR code button, and tunnel status indicator (dot + label, updated via polling: green=connected, amber=connecting, red=disconnected, gray=disabled)
- **Share button**: per-device "Share" button in device header, opens modal with Full Control / View Only tabs, shareable URL + QR code
- **Collapsible controls**: click "Controls" header to toggle between full toggle switches and compact colored status pills (green when active)
- **Auto mode toggles**: iOS-style toggle switches in responsive grid (`auto-fit`), grouped by category (Combat/Farming/Events)
- **Action chips**: minimal bordered buttons in 3-column grid, farm actions (blue accent, includes Gather Gold as one-shot), war actions (red accent), debug actions (purple accent, `.action-chip-debug`)
- **Live View**: full-width button per device, MJPEG stream with polling fallback for older relay
- **Running tasks list**: active task names with circular stop (×) buttons
- **Bottom bar**: Stop All, Refresh, Restart (amber), Quit (muted gray) — owner only

**Auto mode groups** (vary by game mode):
- Broken Lands (`bl`): Combat (Pass Battle, Occupy Towers, Reinforce Throne) + Farming (Auto Quest, Rally Titans, Mine Mithril)
- Home Server (`rw`): Events (Join Groot) + Farming (Rally Titans, Mine Mithril) + Combat (Reinforce Throne)

**Templates**: `base.html` (nav, shared JS), `index.html` (dashboard + device view), `settings.html` (with per-device tabs), `debug.html` (debug actions), `logs.html`

### Relay Tunnel (tunnel.py + relay/)
WebSocket relay for remote access — lets users control 9Bot from outside the LAN.

**Zero-config**: Relay auto-configures from the license key. No user-facing URL/secret/bot-name
settings. `get_relay_config(settings)` in `startup.py` returns `(relay_url, relay_secret, bot_name)`
or `None`. Bot name is `SHA256(license_key)[:10]` — unique per user, stable, unguessable.

**Architecture**: `tunnel.py` opens a `wss://` WebSocket to the relay server (`1453.life`,
DigitalOcean droplet). Browser hits `https://1453.life/bot_name/` → nginx terminates TLS →
relay forwards HTTP requests through the tunnel to `localhost:8080` (Flask dashboard).
Reconnects with exponential backoff (5s→60s cap). Secret sent via `Authorization: Bearer`
header (not URL query param).

**Server stack**: nginx (TLS termination, Let's Encrypt auto-renew) → aiohttp relay on port 8090.
Service runs as non-root `9bot` user via systemd.

**Settings**: Single `remote_access` boolean (default `True`). When `False`, relay is disabled.
Relay URL and secret are base64-obfuscated constants in `startup.py` (not in settings.json).

**Status**: `tunnel_status()` returns `"disabled"` / `"connecting"` / `"connected"` / `"disconnected"`.
Exposed in `/api/status` response and shown as a dot + label in the remote access banner.

**MJPEG streaming**: Relay supports long-lived streaming responses via a custom protocol extension.
`tunnel.py` detects `/api/stream` paths and sends `stream_start`/`stream_chunk`/`stream_end`
messages over the WebSocket. The relay server (`relay_server.py`) uses `web.StreamResponse` to
forward chunks to the browser. `cancel_stream` messages handle cleanup when the browser disconnects.

**Relay server** (`relay/relay_server.py`): asyncio WebSocket server, routes `/bot_name/...`
to the connected bot's tunnel. Landing page shows no bot names (prevents enumeration).

### Bug Report Auto-Upload (startup.py + relay/relay_server.py)
Opt-in periodic upload of bug report ZIPs to the relay droplet via direct HTTPS POST
(not through the WebSocket tunnel, which has a 16MB message limit).

**Settings**: `auto_upload_logs` (bool, default `False`), `upload_interval_hours` (int, default 24, min 1, max 168). Requires restart.

**Client** (`startup.py`):
- `upload_bug_report(settings=None)` — creates zip via `create_bug_report_zip(clear_debug=False)`,
  POSTs to `https://1453.life/_upload?bot={bot_name}` with Bearer auth. Returns `(ok, message)`.
- `start_auto_upload(settings)` / `stop_auto_upload()` — daemon thread, sleeps for interval then uploads.
- `upload_status()` — returns `{"enabled", "interval_hours", "last_upload", "error", "next_upload_in_s"}`.
- `create_bug_report_zip(clear_debug=True)` — `clear_debug=False` skips debug file cleanup (used by
  periodic uploads to keep files intact for continued debugging).

**Server** (`relay/relay_server.py`):
- `POST /_upload?bot={name}` — multipart file upload, Bearer auth, 150MB limit, saves to
  `UPLOAD_DIR/{bot_name}/bugreport_{timestamp}.zip`. Prunes to keep last 10 per bot.
- `GET /_admin?secret=XXX` — HTML admin page listing all bots with uploads, download/delete buttons.
- `GET/DELETE /_admin/uploads/{bot}/{file}` — download or delete a specific upload.
- `DELETE /_admin/uploads/{bot}` — delete all uploads for a bot.
- Env var `UPLOAD_DIR` (default `/opt/9bot-relay/uploads`).

**Dashboard**:
- `POST /api/upload-logs` — manual trigger, returns `{"ok", "message"}`.
- Upload status included in `GET /api/status` response (`upload` field).
- Settings page: toggle + interval in Remote Access card.
- Debug page: "Upload Logs" button with feedback.

### Per-Device Access Control (startup.py + web/dashboard.py)
Token-based shareable URLs that give others access to a specific device.

**Share link format**: `https://1453.life/{bot_name}/d/{device_hash}?token={token}`
- `device_hash` = `SHA256(device_id)[:8]` — short, URL-safe, doesn't expose IP/port
- Full control token = `SHA256(license_key + ":" + device_id)[:16]` — deterministic, no database
- Read-only token = `SHA256(license_key + ":ro:" + device_id)[:16]` — monitoring only

**Access levels**:
- `"full"` — can start/stop tasks, toggle auto modes, use action chips
- `"readonly"` — can view status, troops, quests, live view; no controls or actions
- `None` — invalid token, returns 403

**Implementation**:
- `generate_device_token(device_id)` / `generate_device_ro_token(device_id)` in `startup.py`
- `validate_device_token(device_id, token)` returns `"full"`, `"readonly"`, or `None`
- `require_device_token` decorator validates token and injects `device`/`token`/`readonly` kwargs
- `require_full_access` decorator rejects readonly tokens with 403 on write routes
- Friend view: filtered dashboard (one device card), no settings/debug/restart/quit access
- Readonly view: status pills only (no toggles), no actions section, no stop buttons, no bottom bar

**Per-device settings overrides** (`settings.json`):
```json
{
  "device_settings": {
    "127.0.0.1:5585": {
      "my_team": "blue",
      "auto_restore_ap": false
    }
  }
}
```
- `config.get_device_config(device, key)` checks per-device override, falls back to global
- `config.get_device_enemy_teams(device)` derives enemies from per-device team color
- Settings page has device tabs for editing per-device overrides (owner only)

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
- Red (`#ff6b6b`): stopping — status contains `"Stopping"`
- Amber (`#ffb74d`): waiting — status contains `"Waiting"`
- Gray (`#aab`): navigating — status contains `"Navigating"`
- Default gray (`#667`): idle

**Stopping status**: When `stop_task()` is called, it immediately sets `DEVICE_STATUS[device]`
to `"Stopping {label}..."` (e.g. `"Stopping Auto Quest..."`). This gives instant visual feedback
in the dashboard while the thread winds down. `force_stop_all()` skips this — it kills threads
immediately and clears all statuses.

**Optimistic toggle tracking** (dashboard JS): `_stoppingModes` map prevents the 3-second status
poll from flipping a toggle back ON while the task thread is still alive but stopping. When a user
turns off an auto mode, the toggle stays OFF immediately. Once the task disappears from the active
task list, the stopping state is cleared. Applies to both full toggle switches and collapsed pills.

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
| `debug/territory_coords/` | Coordinate scan screenshots | Per-square, overwritten on rescan |
| `data/` | Territory coordinate database (`territory_coordinates.json`) | Persistent, merged on rescan |

## Tests

```bash
py -m pytest          # run all ~737 tests
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
| `test_check_quests_helpers.py` | `_deduplicate_quests`, `_get_actionable_quests`, `_all_quests_visually_complete` (PVP cooldown), `_attack_pvp_tower` (handler unit tests), PVP dispatch integration (PVP blocks gold mining, cooldown allows gold), `_eg_troops_available` (snapshot-based: gathering/defending tied up, rallying/marching available, fallback), `_recall_stray_stationed` (success, no-op, panel miss, map miss, stop check) (`actions.quests`) |
| `test_classify_quest.py` | `_classify_quest_text` OCR classification (all QuestType values) (`actions.quests`) |
| `test_combat.py` | `_check_dead`, `_find_green_pixel`, `_detect_player_at_eg`, `teleport` (happy path, timeout, dead detection, cancel) (`actions.combat`) |
| `test_territory.py` | `_classify_square_team` (exact/noisy colors, thresholds, team configs), `_get_border_color` (sampling, clock avoidance), `_has_flag` (red pixel detection), `_is_adjacent_to_my_territory` (adjacency, throne, edges), `attack_territory` (full workflow), `auto_occupy_loop` (cycle, stop signal), blue calibration (observed values, boundary distances), `set_territory_config` (auto-derived enemies, threshold changes), red edge cases (near-threshold values), clock overlay row 0 (dimming gaps), `diagnose_grid` (smoke tests) (`territory`) |
| `test_gather_gold.py` | Gather gold flow (gather.png template tap, depart verification, retry logic), loop troop deployment with retry (`actions.farming`) |
| `test_tower_quest.py` | Tower/fortress quest occupy, recall, navigation (friend tab + friend_marker) (`actions.quests`) |
| `test_settings_validation.py` | `validate_settings` — type checks, range/choice validation, device_troops, warnings, schema sync |
| `test_task_runner.py` | `sleep_interval`, `launch_task`/`stop_task` (stopping status), `force_stop_all`, run_once, run_repeat, consecutive error recovery, settings load/save (`runners`) |
| `test_relay_config.py` | `get_relay_config` auto-derive (SHA256 bot name, stability, uniqueness), disabled states (no key, import error, toggle off), defaults integration (`startup`) |
| `test_device_token.py` | `device_hash` (deterministic, URL-safe), `generate_device_token` (per-device, per-key, no-license), `generate_device_ro_token` (different from full, deterministic), `validate_device_token` (full→"full", readonly→"readonly", wrong→None, no-license→None) (`startup`) |
| `test_device_config.py` | Per-device configuration overrides (`config`) |
| `test_evil_guard.py` | Evil Guard rally, `_handle_ap_popup`, probe priest (`actions.evil_guard`) |
| `test_tunnel.py` | WebSocket relay tunnel (`tunnel`) |
| `test_relay_upload.py` | Relay upload helpers: `_format_size`, `_safe_bot_name` (validation), `_prune_uploads` (retention). Skipped without aiohttp (`relay.relay_server`) |
| `test_web_dashboard.py` | Route tests (index, settings, debug, logs, tasks), API tests (`/api/status` incl. tunnel + upload status, `/api/logs`, `/api/upload-logs`, device refresh), task start/stop/stop-all, auto-mode exclusivity, territory grid API, bug report, firewall helper, `sleep_interval`, `run_once`, `launch_task`, `cleanup_dead_tasks`, `apply_settings`, `create_bug_report_zip` (clear_debug param), `upload_bug_report`, `upload_status`, `start_auto_upload`/`stop_auto_upload` (`web.dashboard`, `startup`) |

### Test Conventions
- Fixtures in `conftest.py`: `mock_device` ("127.0.0.1:9999"), `mock_device_b` ("127.0.0.1:8888")
- `reset_quest_state` autouse fixture calls `reset_quest_tracking()` + `reset_rally_blacklist()` before each test
- All ADB calls and screenshots are mocked via `unittest.mock.patch`
- Mock patches target the submodule where the function is used (e.g. `actions.farming.navigate`, not `actions.navigate`)
- Tests import directly from submodules (e.g. `from actions.quests import check_quests`)
- Test names: `test_<function>_<scenario>` (e.g. `test_find_image_returns_none_below_threshold`)
- Use `@pytest.mark.parametrize` for related test cases that vary only by input/expected values

## Git Workflow

- `master` — tagged releases only (v1.1.0, ..., v2.0.0)
- `dev` — integration branch, always working
- Feature branches: `feature/*`, `fix/*`, `cleanup/*` → PR into dev
- Conventional commits: `feat:`, `fix:`, `refactor:`, `test:` prefix
- Current version: see `version.txt`

## Project Files

```
9Bot/
├── CLAUDE.md            # AI technical reference (this file)
├── ROADMAP.md           # Development roadmap
├── TESTING.md           # Tester protocol (bug reporting + active testing guide)
├── run_web.py           # Primary entry point (web + pywebview)
├── startup.py           # Shared initialization (used by run_web.py + main.py)
├── main.py              # Legacy GUI entry point (deprecated)
├── runners.py           # Shared task runners (used by main.py + dashboard)
├── settings.py          # Settings persistence (used by main.py + dashboard)
├── actions/             # Game actions package
│   ├── __init__.py      # Re-exports all public functions
│   ├── _helpers.py      # Shared state (_last_depart_slot, _interruptible_sleep)
│   ├── quests.py        # Quest system + tower quest
│   ├── rallies.py       # Rally joining + blacklist
│   ├── combat.py        # Attacks, targeting, teleport
│   ├── titans.py        # Titan rally + AP restore
│   ├── evil_guard.py    # Evil Guard attack sequence
│   └── farming.py       # Gold + mithril gathering
├── vision.py            # CV + OCR + ADB input
├── navigation.py        # Screen detection + nav
├── troops.py            # Troop counting/status/healing
├── territory.py         # Territory grid + auto-occupy
├── config.py            # Enums, constants, global state
├── devices.py           # ADB device detection
├── botlog.py            # Logging + metrics
├── license.py           # Machine-bound license keys
├── tunnel.py            # WebSocket relay tunnel client
├── run.bat              # User entry point (venv + launch)
├── requirements.txt     # Python dependencies (pinned versions)
├── settings.json        # User settings (auto-generated)
├── version.txt          # Current version string
├── elements/            # Template images for matching
│   └── statuses/        # Troop status icon templates
├── platform-tools/      # Bundled ADB executable
├── web/                 # Flask web dashboard
│   ├── dashboard.py     # App factory, routes
│   ├── static/
│   │   └── style.css    # Mobile-first dark CSS (cache-busted ?v=N)
│   └── templates/
│       ├── base.html    # Nav, shared JS (fmtTime, quest labels, action classes)
│       ├── index.html   # Dashboard: device cards, toggles, actions, running list
│       ├── settings.html # Settings form
│       ├── debug.html   # Debug actions (Check Screen, Check Troops, Diagnose Grid, Scan Corner Coords)
│       └── logs.html    # Log viewer
├── relay/               # Relay server (deployed on droplet)
│   └── relay_server.py  # asyncio WebSocket relay server
├── data/                # Persistent data files (territory coordinates)
├── updater.py           # Auto-update from GitHub releases (zip-slip protected)
├── tests/               # pytest suite (~737 tests)
├── logs/                # Log files
├── stats/               # Session stats JSON
└── debug/               # Debug screenshots
    ├── clicks/          # Click trail images
    └── failures/        # Failure screenshots
```
