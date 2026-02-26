# PACbot Development Roadmap

## Phase 1: Housekeeping [DONE]
- [x] Tag v1.1.0 on release commit
- [x] Delete dead code (analyze_priests.py, nav_to_eg.py, keygen.py, test_ap_region.py, test_ocr.py)
- [x] Remove unused imports in main.py (traceback, subprocess, zipfile)
- [x] Fix bare except clauses in devices.py
- [x] Consolidate magic numbers into config.py constants
- Branch: `cleanup/phase1-housekeeping` (merged to dev)

## Bug Fix: Rally owner blacklist [DONE]
- [x] OCR rally owner name from war screen cards
- [x] Blacklist owners after failed depart (2 failures or error detection)
- [x] 30-minute expiry + reset on auto-quest start
- [x] 24 tests
- Branch: `fix/rally-owner-blacklist` (merged to dev)
- **Needs live testing** — OCR regions estimated from screenshot, may need tuning

## Phase 2: Code Quality [DONE]
- [x] Add QuestType, RallyType, Screen enums (str, Enum) in config.py
- [x] Migrate ~250 string literals across 6 source files to enum values
- [x] Break up check_quests (171 → ~65 lines) into 4 helpers
- [x] Recapture slot.png template (tighter crop, 107x77 vs 156x150)
- [x] 15 new tests for extracted helpers
- Branch: `cleanup/phase2-code-quality` (merged to dev)

## Phase 3: Test Coverage [PARTIAL]
- [x] Tests for vision.py (find_image, read_text, read_number, read_ap, get_template, adb_tap, adb_swipe)
- [x] Tests for devices.py (auto_connect_emulators, get_devices, get_emulator_instances)
- [x] Tests for task runner logic in main.py (sleep_interval, launch/stop, run_once, run_repeat, settings)
- [ ] Tests for territory.py (17 functions, zero tests)
- Current total: 290 tests

## Phase 4: New Features [TODO]
- [ ] Complete troop status reading (stubs in troops.py)
- [ ] (user feature ideas TBD)
