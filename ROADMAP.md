# PACbot Roadmap

Forward-looking development plan from **v1.3.0**.
Priority: bug fixes, clean code, maintainability, usability — then new features.

---

## Phase 1 — Bug Fixes & Reliability (v1.4.0)

Harden existing features before adding new ones.

- [ ] Improve mithril mining reliability — detect occupied mines and plundered troops
- [x] Improve titan rally miss detection — handle titan walking away, detect miss + retry
- [ ] Teleport system improvements — more reliable targeting and validation
- [x] Error recovery — stuck-state detection, disconnect handling, popup resilience
- [x] AP Recovery popup handling — detect game-opened AP popup during EG depart, restore AP inline
- [ ] Image region audit — verify all `IMAGE_REGIONS` in vision.py are still accurate. Known widening needed: heal.png, depart.png, stationed.png, mithril_depart.png
- [x] Settings validation — validate `settings.json` on startup, catch invalid/corrupt values
- [ ] Fix join_rally success rate (33%) — `jr_slot_to_depart` transition at 0%, war screen scroll settle broken
- [ ] Fix rally_titan instant failures (53%) — search menu not opening reliably (31-50% success)
- [ ] Fix heal flow — all heal transitions at 0%, verify `heal.png` template matches current game UI
- [ ] Tune timed_wait budgets — `jr_backout_close_x` needs 3.5s (currently 0.5-1.5s), `verify_bl_screen` needs 2.5s

## Phase 2 — Testing & Quality (v1.4.0)

Build confidence that everything works before shipping updates.

### Critical Test Gaps (P0)
- [x] `test_combat.py` — _check_dead, _find_green_pixel, _detect_player_at_eg, teleport (28 tests). Still missing: attack, phantom_clash, reinforce_throne, target
- [ ] `test_evil_guard.py` — rally_eg 7-phase state machine, _handle_ap_popup, probe_priest (850 LOC, 0% coverage)
- [ ] `test_titans.py` — rally_titan, restore_ap flow, gem limit logic, _close_ap_menu (425 LOC, 0% coverage)
- [x] `test_territory.py` — _classify_square_team, _get_border_color, _has_flag, _is_adjacent_to_my_territory, attack_territory, auto_occupy_loop (66 tests)

### Major Test Gaps (P1)
- [ ] Expand `test_quests.py` — check_quests orchestration, tower quest flow, claim rewards, OCR parsing
- [ ] Expand `test_rallies.py` — join_rally (649 LOC untested), join_war_rallies, _ocr_error_banner
- [ ] `test_farming.py` — mine_mithril, mine_mithril_if_due interval logic, _set_gather_level, gather.png template tap (gather_gold updated to use wait_for_image_and_tap)

### Infrastructure
- [x] Audit existing test suite — no bloat found, 418 tests in 40s, well-structured
- [ ] Add live testing suite — integration tests that run against a real emulator
- [ ] Establish pre-release checklist — full test pass, live smoke test, version bump verification
- [ ] Actionable test data — coverage reports, structured failure output, clear pass/fail signals
- [ ] Keep CLAUDE.md current — ensure AI has full codebase context for efficient development
- [ ] Better debug data collection — add failure screenshots to: join_rally (with reason), rally_titan early bail-out, read_ap None returns, heal_all template misses
- [ ] Automatic log/stats/debug uploading to droplet
- [ ] Telemetry consent prompt — explicit opt-in dialog on first run (never silent, never pre-checked)
- [ ] Data scrubbing — strip device IPs, file paths, player names before upload
- [ ] Screenshot masking — black out chat area and name regions before staging
- [ ] Clear submitted data from local machine after successful upload (no duplicates)
- [ ] Settings UI for telemetry — tier selection, "View queued data" button, opt-out

## Security Hardening (ongoing)

Audit performed Feb 2026. No critical vulnerabilities — no shell injection, no eval/exec/pickle,
Jinja2 auto-escaping active, all subprocess calls use list args without shell=True.

### Completed
- [x] Redact `relay_secret` from bug report ZIPs (shows `***REDACTED***`)
- [x] Validate device IDs in `/tasks/start` against known devices whitelist
- [x] Fix DOM XSS — replace `innerHTML` with `textContent`/DOM creation in dashboard JS
- [x] Switch `/api/bug-report` from GET to POST (prevent prefetch/crawler triggers)
- [x] Pin all dependency versions in `requirements.txt`
- [x] Add zip-slip protection to auto-updater `extractall()`
- [x] Zero-config relay — auto-derive URL/secret/bot-name from license key, no user-facing config
- [x] Remove bot enumeration from relay landing page
- [x] Fix settings.json wipe on update — added to `PRESERVE_FILES` in updater.py

- [x] Switch relay tunnel to `wss://` (TLS) — nginx + Let's Encrypt on `1453.life`
- [x] Move relay secret from URL query param to `Authorization: Bearer` header
- [x] Run relay server as non-root `pacbot` user in systemd
- [x] Atomic settings write — temp file + `os.replace()` prevents corruption on crash

### Remaining (prioritized)
- [ ] Add CSRF protection to POST endpoints
- [ ] Add integrity verification (SHA-256) to auto-updater downloads
- [x] Atomic settings write — temp file + `os.replace()` prevents corruption on crash

## Monetization Prep (when needed)

Not urgent — current license system (machine-bound keys + Google Sheets) is sufficient for
a small trusted user base. Revisit when charging money or user count grows significantly.

### Distribution
- [ ] Make GitHub repo private (prevents unlicensed downloads of source)
- [ ] Private download server — users authenticate with license key to get release ZIPs
- [ ] Update `updater.py` to send license key as auth header instead of hitting GitHub API
- [ ] Consider Nuitka compilation for releases (native .exe, no Python install needed) — blocked by AV false positive risk; revisit when code-signing certificate is affordable

### IP Protection (current state)
- [x] Machine-bound license keys (HMAC + hardware fingerprint) — can't copy `.license_key` between PCs
- [x] Remote key validation against Google Sheets (instant revoke)
- [x] License check on startup with 3-attempt limit
- [x] Dev mode bypass for `.git` repos (doesn't affect end users)
- [ ] Replace Google Sheets with server-side API (when: need usage tracking, auto-provisioning, or professional storefront)
- [ ] Periodic re-validation during runtime (when: piracy becomes an actual problem, not preemptively)

## Phase 3 — UI & Project Cleanup (v1.5.0)

Clean up the interface and codebase structure for long-term maintainability.

- [x] Web dashboard — mobile-friendly Flask remote control (toggle switches, action chips, device cards)
- [x] Status text system — Title Case, expanded abbreviations, phase-specific statuses for rally_eg
- [ ] Clean up settings layout — currently cluttered
- [ ] Reorganize "More Actions" section
- [ ] Clean up file and folder structure — organize `elements/`, consolidate debug dirs
- [x] Refactor main.py — extract task runners into shared `runners.py`, settings into `settings.py`
- [x] Split actions.py (~3600 lines) into `actions/` package (quests, rallies, combat, titans, evil_guard, farming)
- [x] Eliminate dashboard duplication — `web/dashboard.py` now imports from `runners.py` and `settings.py`

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

## Phase 6 — UX & Notifications

Quality-of-life features that make the bot feel polished.

### Quick Wins
- [ ] QR code for phone connection — render scannable QR in console/pywebview window on startup (LAN URL). No more typing IP addresses. (`qrcode` library, ~3 lines)
- [ ] Session summary — on stop or on demand, show recap: runtime, rally count, gathers, heals, errors. Data already in `StatsTracker`, just needs a `summarize()` method and a dashboard card/endpoint

### Notifications
- [ ] Discord webhook alerts — POST to a user-configured webhook URL on key events (bot stopped on errors, EG complete, license issues). No bot token needed, just a URL in settings
- [ ] Push notifications via ntfy.sh — free, no-account push notifications to phone. One HTTP POST per alert. Users subscribe to their own topic. Lighter than Discord, easier opt-in/out
- [ ] Notification settings — let users pick which events trigger alerts (errors only, rally completions, all activity) and which channel (Discord, ntfy, both, none)

### Security
- [ ] Auto-screenshot on license fail — save console screenshot + timestamp after 3 invalid key attempts. Breadcrumb trail for key sharing/brute-force detection
