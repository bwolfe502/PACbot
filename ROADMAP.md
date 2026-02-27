# PACbot Roadmap

Forward-looking development plan from **v1.3.0**.
Priority: bug fixes, clean code, maintainability, usability — then new features.

---

## Phase 1 — Bug Fixes & Reliability (v1.4.0)

Harden existing features before adding new ones.

- [ ] Improve mithril mining reliability — detect occupied mines and plundered troops
- [ ] Improve titan rally miss detection — handle titan walking away, detect miss + retry
- [ ] Teleport system improvements — more reliable targeting and validation
- [ ] Error recovery — stuck-state detection, disconnect handling, popup resilience
- [ ] Image region audit — verify all `IMAGE_REGIONS` in vision.py are still accurate
- [ ] Settings validation — validate `settings.json` on startup, catch invalid/corrupt values

## Phase 2 — Testing & Quality (v1.4.0)

Build confidence that everything works before shipping updates.

- [ ] Territory.py test coverage — 17 functions, currently zero tests
- [ ] Audit existing test suite — remove bloated/redundant tests, ensure every test is meaningful
- [ ] Add live testing suite — integration tests that run against a real emulator
- [ ] Establish pre-release checklist — full test pass, live smoke test, version bump verification
- [ ] Actionable test data — coverage reports, structured failure output, clear pass/fail signals
- [ ] Keep CLAUDE.md current — ensure AI has full codebase context for efficient development

## Phase 3 — UI & Project Cleanup (v1.5.0)

Clean up the interface and codebase structure for long-term maintainability.

- [ ] Clean up settings layout — currently cluttered
- [ ] Reorganize "More Actions" section
- [ ] Clean up file and folder structure — organize `elements/`, consolidate debug dirs
- [ ] Better status display and multi-device management
- [ ] Refactor main.py — extract GUI from task runner logic
- [ ] Split actions.py (~3000 lines) into focused modules (rally, quest, utility actions)

## Phase 4 — Quest Expansion (v1.6.0)

Extend auto quest to handle more quest types. The classification infrastructure already exists
(`_classify_quest_text` recognizes all types) — they just need to be wired up.

- [ ] Add TOWER quests to auto quest loop
- [ ] Add GATHER (gold mining) to auto quest loop
- [ ] Add PVP to auto quest loop
- [ ] Expand `_classify_quest_text` and `_get_actionable_quests` for new types
- [ ] New template images for tower/gather/PvP quest UI elements

## Phase 5 — New Automations (v1.7.0)

Entirely new game automations.

- [ ] Automatic frost giant function
- [ ] Automatic lava haka spawning
