import cv2
import numpy as np
import os
import time
import random

import config
from config import (SQUARE_SIZE, GRID_OFFSET_X, GRID_OFFSET_Y,
                    GRID_WIDTH, GRID_HEIGHT, THRONE_SQUARES, BORDER_COLORS, Screen)
from vision import load_screenshot, tap_image, adb_tap, tap_tower_until_attack_menu, get_template, save_failure_screenshot
from navigation import navigate
from troops import troops_avail, all_troops_home, heal_all
from actions import teleport
from botlog import get_logger, timed_action

_log = get_logger("territory")

# ============================================================
# TERRITORY GRID HELPERS (extracted for testability)
# ============================================================

def _get_square_center(row, col):
    """Get pixel coordinates of square center."""
    x = int(GRID_OFFSET_X + col * SQUARE_SIZE + SQUARE_SIZE / 2)
    y = int(GRID_OFFSET_Y + row * SQUARE_SIZE + SQUARE_SIZE / 2)
    return x, y


def _get_border_color(image, row, col):
    """Sample the BORDER pixels of a square — avoids clock obstruction for top rows.

    Returns average BGR color as a tuple of three floats.
    """
    x = int(GRID_OFFSET_X + col * SQUARE_SIZE)
    y = int(GRID_OFFSET_Y + row * SQUARE_SIZE)

    border_pixels = []

    # For row 0 specifically (heavily obscured by clock)
    if row == 0:
        for y_offset in range(2, int(SQUARE_SIZE / 4), 3):
            sample_y = y + y_offset
            if sample_y < image.shape[0]:
                for x_offset in [5, 10, 15, 20, 25, 30, 35]:
                    sample_x = x + x_offset
                    if sample_x < image.shape[1]:
                        border_pixels.append(image[sample_y, sample_x])

    # For row 1 (partially obscured by clock)
    elif row == 1:
        for offset in [5, 10, 15, 20, 25, 30, 35]:
            if y + offset < image.shape[0] and x < image.shape[1]:
                border_pixels.append(image[y + offset, x])
        bottom_y = int(y + SQUARE_SIZE - 1)
        for offset in [5, 10, 15, 20, 25, 30]:
            if bottom_y < image.shape[0] and x + offset < image.shape[1]:
                border_pixels.append(image[bottom_y, x + offset])

    # For all other rows (not obscured)
    else:
        for offset in [8, 15, 22, 30]:
            if x + offset < image.shape[1] and y < image.shape[0]:
                border_pixels.append(image[y, x + offset])
        for offset in [8, 15, 22, 30]:
            if y + offset < image.shape[0] and x < image.shape[1]:
                border_pixels.append(image[y + offset, x])

    if border_pixels:
        avg = np.mean(border_pixels, axis=0)
        return tuple(avg)
    return (0, 0, 0)


def _classify_square_team(bgr, device=None):
    """Determine team based on border color — find closest Euclidean match.

    Thresholds:
    - Green: <= 70 (neutral, always recognized)
    - Enemy teams: <= 70
    - Own team (MY_TEAM_COLOR): <= 90 (lenient — we want to find our own borders)
    - Any team: <= 55 (tight fallback)
    - Fallback own team: <= 95 (last resort)
    """
    b, g, r = bgr

    my_team = config.get_device_config(device, "my_team") if device else config.MY_TEAM_COLOR
    enemy_teams = config.get_device_enemy_teams(device) if device else config.ENEMY_TEAMS

    min_distance = float('inf')
    best_team = "unknown"

    distances = {}
    for team, (target_b, target_g, target_r) in BORDER_COLORS.items():
        distance = ((b - target_b)**2 + (g - target_g)**2 + (r - target_r)**2)**0.5
        distances[team] = distance

        if distance < min_distance:
            min_distance = distance
            best_team = team

    if best_team == "green" and min_distance <= 70:
        return "green"
    elif best_team in enemy_teams and min_distance <= 70:
        return best_team
    elif best_team == my_team and min_distance <= 90:
        return best_team
    elif min_distance <= 55:
        return best_team

    if best_team == "unknown" and my_team in distances and distances[my_team] <= 95:
        return my_team

    return "unknown"


def _has_flag(image, row, col):
    """Check if a square has a flag using red flag color #FF5D5A.

    Returns True if more than 15 red pixels are found in the square.
    """
    x = int(GRID_OFFSET_X + col * SQUARE_SIZE)
    y = int(GRID_OFFSET_Y + row * SQUARE_SIZE)
    w = int(SQUARE_SIZE)
    h = int(SQUARE_SIZE)

    square = image[y:y+h, x:x+w]

    red_flag_mask = cv2.inRange(square, (75, 80, 240), (105, 110, 255))
    red_pixels = cv2.countNonZero(red_flag_mask)

    return red_pixels > 15


def _is_adjacent_to_my_territory(image, row, col, device=None):
    """Check if square is DIRECTLY next to own territory."""
    my_team = config.get_device_config(device, "my_team") if device else config.MY_TEAM_COLOR
    neighbors = [
        (row-1, col),
        (row+1, col),
        (row, col-1),
        (row, col+1),
    ]

    for r, c in neighbors:
        if not (0 <= r < GRID_HEIGHT and 0 <= c < GRID_WIDTH):
            continue
        if (r, c) in THRONE_SQUARES:
            continue

        border_color = _get_border_color(image, r, c)
        team = _classify_square_team(border_color, device=device)

        if team == my_team:
            return True

    return False


# ============================================================
# TERRITORY SQUARE MANAGER GUI
# ============================================================

def open_territory_manager(device):
    """Open a visual interface to manually select squares to attack or ignore"""
    import tkinter as tk
    import customtkinter as ctk
    from PIL import Image, ImageTk

    log = get_logger("territory", device)

    # Take a screenshot of territory screen
    if not navigate(Screen.TERRITORY, device):
        log.warning("Failed to navigate to territory screen")
        return

    time.sleep(1)
    full_image = load_screenshot(device)

    if full_image is None:
        log.error("Failed to load screenshot")
        return

    # Crop to just the grid area with small padding
    grid_pixel_width = int(GRID_WIDTH * SQUARE_SIZE)
    grid_pixel_height = int(GRID_HEIGHT * SQUARE_SIZE)
    padding = 10

    crop_x1 = max(0, GRID_OFFSET_X - padding)
    crop_y1 = max(0, GRID_OFFSET_Y - padding)
    crop_x2 = min(full_image.shape[1], GRID_OFFSET_X + grid_pixel_width + padding)
    crop_y2 = min(full_image.shape[0], GRID_OFFSET_Y + grid_pixel_height + padding)

    image = full_image[crop_y1:crop_y2, crop_x1:crop_x2]

    # Adjust offsets for cropped image
    adjusted_offset_x = GRID_OFFSET_X - crop_x1
    adjusted_offset_y = GRID_OFFSET_Y - crop_y1

    # Create manager window
    manager = ctk.CTkToplevel()
    manager.title(f"Territory Square Manager - {device}")
    manager.configure(fg_color="#0c0c18")

    # Instructions
    ctk.CTkLabel(
        manager,
        text="Click squares: GREEN = Force Attack | RED = Ignore | None = Auto",
        font=ctk.CTkFont(family="Segoe UI", size=11),
        text_color="#e0e0f0", fg_color="#14142a",
        corner_radius=6, height=30
    ).pack(fill=tk.X, padx=6, pady=(6, 2))

    # Stats display
    stats_var = tk.StringVar()
    stats_var.set(f"Attack: {len(config.MANUAL_ATTACK_SQUARES)} | Ignore: {len(config.MANUAL_IGNORE_SQUARES)}")
    ctk.CTkLabel(manager, textvariable=stats_var,
                 font=ctk.CTkFont(family="Segoe UI", size=10),
                 text_color="#8899aa").pack(pady=2)

    # Create canvas with the territory image
    canvas_frame = ctk.CTkFrame(manager, fg_color="transparent")
    canvas_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    # Scale down to 0.75x
    display_scale = 0.75
    display_image = cv2.resize(image, None, fx=display_scale, fy=display_scale, interpolation=cv2.INTER_LINEAR)
    display_image = cv2.cvtColor(display_image, cv2.COLOR_BGR2RGB)

    # Convert to PhotoImage
    pil_image = Image.fromarray(display_image)
    photo = ImageTk.PhotoImage(pil_image)

    # Set window size based on image
    window_width = display_image.shape[1] + 20
    window_height = display_image.shape[0] + 120
    manager.geometry(f"{window_width}x{window_height}")

    canvas = tk.Canvas(
        canvas_frame,
        width=display_image.shape[1],
        height=display_image.shape[0],
        highlightthickness=0
    )
    canvas.pack()

    # Set background image
    canvas.create_image(0, 0, anchor=tk.NW, image=photo)
    canvas.image = photo  # Keep a reference

    # Draw grid overlay - store items in list for faster deletion
    overlay_items = []

    def draw_overlay():
        """Draw colored overlays for manual selections"""
        nonlocal overlay_items

        # Clear existing overlays
        for item_id in overlay_items:
            canvas.delete(item_id)
        overlay_items.clear()

        for row in range(GRID_HEIGHT):
            for col in range(GRID_WIDTH):
                if (row, col) in THRONE_SQUARES:
                    continue

                x = int((adjusted_offset_x + col * SQUARE_SIZE) * display_scale)
                y = int((adjusted_offset_y + row * SQUARE_SIZE) * display_scale)
                w = int(SQUARE_SIZE * display_scale)
                h = int(SQUARE_SIZE * display_scale)

                color = None
                if (row, col) in config.MANUAL_ATTACK_SQUARES:
                    color = "green"
                elif (row, col) in config.MANUAL_IGNORE_SQUARES:
                    color = "red"

                if color:
                    rect_id = canvas.create_rectangle(
                        x, y, x + w, y + h,
                        outline=color,
                        width=2,
                        fill=color,
                        stipple="gray50"
                    )
                    overlay_items.append(rect_id)

    def on_canvas_click(event):
        """Handle clicks on the canvas"""
        # Convert click to grid position
        click_x = event.x / display_scale
        click_y = event.y / display_scale

        col = int((click_x - adjusted_offset_x) / SQUARE_SIZE)
        row = int((click_y - adjusted_offset_y) / SQUARE_SIZE)

        # Validate bounds
        if not (0 <= row < GRID_HEIGHT and 0 <= col < GRID_WIDTH):
            return

        if (row, col) in THRONE_SQUARES:
            _log.debug("Cannot select throne square (%d, %d)", row, col)
            return

        # Toggle state: None -> Attack -> Ignore -> None
        if (row, col) in config.MANUAL_ATTACK_SQUARES:
            config.MANUAL_ATTACK_SQUARES.remove((row, col))
            config.MANUAL_IGNORE_SQUARES.add((row, col))
            _log.debug("Square (%d, %d) set to IGNORE", row, col)
        elif (row, col) in config.MANUAL_IGNORE_SQUARES:
            config.MANUAL_IGNORE_SQUARES.remove((row, col))
            _log.debug("Square (%d, %d) set to AUTO-DETECT", row, col)
        else:
            config.MANUAL_ATTACK_SQUARES.add((row, col))
            _log.debug("Square (%d, %d) set to FORCE ATTACK", row, col)

        # Update display
        draw_overlay()
        stats_var.set(f"Attack: {len(config.MANUAL_ATTACK_SQUARES)} | Ignore: {len(config.MANUAL_IGNORE_SQUARES)}")

    canvas.bind("<Button-1>", on_canvas_click)

    # Buttons
    button_frame = ctk.CTkFrame(manager, fg_color="transparent")
    button_frame.pack(pady=5)

    def clear_all():
        config.MANUAL_ATTACK_SQUARES.clear()
        config.MANUAL_IGNORE_SQUARES.clear()
        draw_overlay()
        stats_var.set(f"Attack: {len(config.MANUAL_ATTACK_SQUARES)} | Ignore: {len(config.MANUAL_IGNORE_SQUARES)}")
        _log.debug("Cleared all manual selections")

    def clear_attack():
        config.MANUAL_ATTACK_SQUARES.clear()
        draw_overlay()
        stats_var.set(f"Attack: {len(config.MANUAL_ATTACK_SQUARES)} | Ignore: {len(config.MANUAL_IGNORE_SQUARES)}")
        _log.debug("Cleared manual attack selections")

    def clear_ignore():
        config.MANUAL_IGNORE_SQUARES.clear()
        draw_overlay()
        stats_var.set(f"Attack: {len(config.MANUAL_ATTACK_SQUARES)} | Ignore: {len(config.MANUAL_IGNORE_SQUARES)}")
        _log.debug("Cleared manual ignore selections")

    _btn_kw = dict(font=ctk.CTkFont(family="Segoe UI", size=10),
                   fg_color="#1e3a5f", hover_color="#1a3a4a",
                   text_color="#e0e0f0", corner_radius=8, height=28, width=90)
    ctk.CTkButton(button_frame, text="Clear All", command=clear_all,
                  fg_color="#c62828", hover_color="#d32f2f",
                  font=ctk.CTkFont(family="Segoe UI", size=10),
                  text_color="#ffffff", corner_radius=8, height=28, width=90).pack(side=tk.LEFT, padx=2)
    ctk.CTkButton(button_frame, text="Clear Attack", command=clear_attack, **_btn_kw).pack(side=tk.LEFT, padx=2)
    ctk.CTkButton(button_frame, text="Clear Ignore", command=clear_ignore, **_btn_kw).pack(side=tk.LEFT, padx=2)
    ctk.CTkButton(button_frame, text="Close", command=manager.destroy, **_btn_kw).pack(side=tk.LEFT, padx=2)

    # Draw initial overlay
    draw_overlay()

    # Just destroy on close
    manager.protocol("WM_DELETE_WINDOW", manager.destroy)

# ============================================================
# TERRITORY ATTACK SYSTEM
# ============================================================

@timed_action("attack_territory")
def attack_territory(device, debug=False):
    """Full territory attack workflow: heal, verify troops home, navigate, attack"""
    log = get_logger("territory", device)
    log.info("Starting territory attack workflow...")

    # Step 1: Navigate to map screen
    if not navigate(Screen.MAP, device):
        log.warning("Failed to navigate to map screen")
        return False

    # Step 2: Heal all troops
    log.info("Healing troops...")
    heal_all(device)
    time.sleep(2)

    # Step 3: Verify all troops are home
    log.info("Checking if all troops are home...")
    if not all_troops_home(device):
        log.warning("Not all troops are home! Aborting territory attack.")
        return False

    log.info("All troops confirmed home. Proceeding...")

    # Step 4: Navigate to territory screen
    if not navigate(Screen.TERRITORY, device):
        log.warning("Failed to navigate to territory screen")
        return False

    time.sleep(1)

    # Step 5: Take screenshot and analyze grid
    image = load_screenshot(device)

    if image is None:
        log.error("Failed to load screenshot")
        return False

    if debug:
        debug_img = image.copy()

    # Build list of valid targets
    log.info("Scanning grid for targets...")
    my_team = config.get_device_config(device, "my_team")
    enemy_teams = config.get_device_enemy_teams(device)
    log.debug("My team: %s, Attacking: %s", my_team, enemy_teams)

    targets = []
    enemy_squares = []
    adjacent_enemies = []
    flagged_squares = []

    for row in range(GRID_HEIGHT):
        for col in range(GRID_WIDTH):
            if (row, col) in THRONE_SQUARES:
                continue

            border_color = _get_border_color(image, row, col)
            team = _classify_square_team(border_color, device=device)

            if team in enemy_teams:
                enemy_squares.append((row, col))

                if _is_adjacent_to_my_territory(image, row, col, device=device):
                    adjacent_enemies.append((row, col))

                    if not _has_flag(image, row, col):
                        targets.append((row, col))
                    else:
                        flagged_squares.append((row, col))

    log.debug("Enemy squares detected: %d", len(enemy_squares))
    log.debug("Enemy squares adjacent to my territory: %d", len(adjacent_enemies))
    log.debug("Flagged squares: %d", len(flagged_squares))
    log.debug("Valid targets (no flag): %d", len(targets))

    # Apply manual overrides
    log.debug("Applying manual overrides...")
    log.debug("Manual attack squares: %d", len(config.MANUAL_ATTACK_SQUARES))
    log.debug("Manual ignore squares: %d", len(config.MANUAL_IGNORE_SQUARES))

    if config.MANUAL_ATTACK_SQUARES:
        targets = list(config.MANUAL_ATTACK_SQUARES)
        log.info("Using ONLY manual attack squares (ignoring auto-detect)")
    else:
        targets = [t for t in targets if t not in config.MANUAL_IGNORE_SQUARES]

    log.debug("Final targets after manual overrides: %d", len(targets))

    # Create debug visualization
    if debug:
        for row in range(GRID_HEIGHT):
            for col in range(GRID_WIDTH):
                x, y = _get_square_center(row, col)

                cv2.putText(debug_img, f"{row},{col}", (x-15, y),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.25, (255, 255, 255), 1)

                if (row, col) in targets:
                    cv2.circle(debug_img, (x, y), 8, (0, 255, 0), -1)
                elif (row, col) in flagged_squares:
                    cv2.line(debug_img, (x-8, y-8), (x+8, y+8), (0, 0, 255), 2)
                    cv2.line(debug_img, (x-8, y+8), (x+8, y-8), (0, 0, 255), 2)
                elif (row, col) in adjacent_enemies:
                    cv2.circle(debug_img, (x, y), 6, (0, 255, 255), 2)

        cv2.imwrite(f"territory_debug_{device}.png", debug_img)
        log.debug("Saved debug image")

    if targets:
        target_row, target_col = random.choice(targets)
        click_x, click_y = _get_square_center(target_row, target_col)

        log.info("Attacking square (%d, %d)", target_row, target_col)

        # Remember this square PER DEVICE
        config.LAST_ATTACKED_SQUARE[device] = (target_row, target_col)

        adb_tap(device, click_x, click_y)

        return True
    else:
        log.warning("No valid targets found")
        return False

# ============================================================
# AUTO OCCUPY SYSTEM
# ============================================================

def _occupy_stopped(device):
    """Check if auto occupy was stopped. Prints a message on first detection."""
    if not config.auto_occupy_running:
        log = get_logger("territory", device)
        log.info("Auto occupy stop requested, aborting...")
        return True
    return False

def _occupy_sleep(seconds):
    """Sleep in 1-second chunks, returning True immediately if stopped."""
    for _ in range(seconds):
        if not config.auto_occupy_running:
            return True
        time.sleep(1)
    return False

@timed_action("auto_occupy")
def auto_occupy_loop(device):
    """Auto occupy loop: attack territory -> teleport -> click square -> attack -> wait"""
    log = get_logger("territory", device)
    log.info("Auto occupy started")

    while config.auto_occupy_running:
        try:
            # Check for dead.png and tap it if it exists
            log.debug("Checking for dead.png...")
            if tap_image("dead.png", device):
                log.info("Found and clicked dead.png")
                time.sleep(2)

            if _occupy_stopped(device):
                break

            # Check if all troops are home
            if not all_troops_home(device):
                log.info("Troops not home, waiting...")
                if _occupy_sleep(10):
                    break
                continue

            log.info("=== Starting auto occupy cycle ===")

            # Step 1: Attack territory
            log.info("Step 1: Attacking territory...")
            if not attack_territory(device, debug=False):
                log.warning("Failed to attack territory, skipping cycle")
                if _occupy_sleep(10):
                    break
                continue

            if _occupy_stopped(device):
                break
            time.sleep(2)

            # Double-check troops are home before teleporting
            log.info("Double-checking troops are home before teleport...")
            if not all_troops_home(device):
                log.warning("Troops not home! Skipping teleport.")
                if _occupy_sleep(10):
                    break
                continue

            if _occupy_stopped(device):
                break

            # Step 2: Teleport
            log.info("Step 2: Teleporting...")
            if not teleport(device):
                log.warning("Teleport failed, skipping cycle")
                if _occupy_sleep(10):
                    break
                continue

            if _occupy_stopped(device):
                break
            time.sleep(2)

            # Step 3: Navigate back to territory screen and click the square we attacked
            if device in config.LAST_ATTACKED_SQUARE:
                target_row, target_col = config.LAST_ATTACKED_SQUARE[device]
                log.info("Step 3: Navigating to territory screen to click square (%d, %d)...", target_row, target_col)

                if not navigate(Screen.TERRITORY, device):
                    log.warning("Failed to navigate to territory screen")
                    if _occupy_sleep(10):
                        break
                    continue

                if _occupy_stopped(device):
                    break
                time.sleep(1)

                # Calculate click position for the last attacked square
                click_x = int(GRID_OFFSET_X + target_col * SQUARE_SIZE + SQUARE_SIZE / 2)
                click_y = int(GRID_OFFSET_Y + target_row * SQUARE_SIZE + SQUARE_SIZE / 2)

                log.debug("Clicking square (%d, %d) at (%d, %d)", target_row, target_col, click_x, click_y)
                adb_tap(device, click_x, click_y)

                tap_tower_until_attack_menu(device, timeout=10)
            else:
                log.warning("No last attacked square remembered, skipping territory click")

            if _occupy_stopped(device):
                break

            # Step 4: Attack
            time.sleep(1)
            log.info("Step 4: Attacking...")

            if config.get_device_config(device, "auto_heal"):
                heal_all(device)

            troops = troops_avail(device)
            min_troops = config.get_device_config(device, "min_troops")

            if troops > min_troops:
                tap_image("depart.png", device)
                time.sleep(1)
                tap_image("depart.png", device)
            else:
                log.warning("Not enough troops available (have %d, need more than %d)", troops, min_troops)

            time.sleep(2)

            log.info("Cycle complete, waiting 10 seconds...")

            # Wait 10 seconds before next cycle
            if _occupy_sleep(10):
                break

        except Exception as e:
            log.error("Error in auto occupy loop: %s", e, exc_info=True)
            save_failure_screenshot(device, "occupy_exception")
            if _occupy_sleep(5):
                break

    log.info("Auto occupy stopped")

# ============================================================
# DEBUG FUNCTIONS
# ============================================================

def diagnose_grid(device):
    """Full grid diagnostic — classifies all 576 squares, saves debug image and report.

    Navigates to territory screen, screenshots, then runs the same border-color
    classification pipeline as attack_territory.  Outputs:
      - Per-team counts and grid map to INFO log
      - Unknown squares with BGR values to DEBUG log
      - Color-coded debug image to debug/territory_diag_{device}.png
    """
    log = get_logger("territory", device)

    # Navigate to territory screen
    if not navigate(Screen.TERRITORY, device):
        log.warning("diagnose_grid: failed to navigate to territory screen")
        return

    time.sleep(1)
    image = load_screenshot(device)
    if image is None:
        log.error("diagnose_grid: failed to load screenshot")
        return

    debug_img = image.copy()

    # --- Classify every square ---------------------------------------------------
    team_counts = {}          # team_name -> count
    unknown_details = []      # (row, col, bgr_tuple, best_team, distance)
    grid_map = []             # list of 24 strings, each 24 chars

    # Dot colors for the debug image (BGR)
    DOT_COLORS = {
        "yellow": (0, 220, 255),
        "green":  (0, 200, 0),
        "red":    (0, 0, 255),
        "blue":   (255, 150, 100),
        "unknown": (128, 128, 128),
    }
    TEAM_CHAR = {"yellow": "Y", "green": "G", "red": "R", "blue": "B", "unknown": "?"}

    for row in range(GRID_HEIGHT):
        row_chars = []
        for col in range(GRID_WIDTH):
            if (row, col) in THRONE_SQUARES:
                row_chars.append("T")
                continue

            bgr = _get_border_color(image, row, col)
            team = _classify_square_team(bgr, device=device)
            team_counts[team] = team_counts.get(team, 0) + 1
            row_chars.append(TEAM_CHAR.get(team, "?"))

            # Collect unknown details for threshold tuning
            if team == "unknown":
                # Compute closest known color distance
                best_team = "unknown"
                best_dist = float("inf")
                for t, (tb, tg, tr) in BORDER_COLORS.items():
                    d = ((bgr[0]-tb)**2 + (bgr[1]-tg)**2 + (bgr[2]-tr)**2)**0.5
                    if d < best_dist:
                        best_dist = d
                        best_team = t
                unknown_details.append((row, col, tuple(int(v) for v in bgr),
                                        best_team, round(best_dist, 1)))

            # Draw colored dot on debug image
            cx, cy = _get_square_center(row, col)
            color = DOT_COLORS.get(team, DOT_COLORS["unknown"])
            cv2.circle(debug_img, (cx, cy), 6, color, -1)
            cv2.circle(debug_img, (cx, cy), 6, (0, 0, 0), 1)  # outline

        grid_map.append("".join(row_chars))

    # --- Flag & adjacency stats (enemy squares only) -----------------------------
    enemy_teams = set(config.get_device_enemy_teams(device))
    enemy_count = sum(v for k, v in team_counts.items() if k in enemy_teams)
    flagged = 0
    adjacent = 0
    valid_targets = 0
    for row in range(GRID_HEIGHT):
        for col in range(GRID_WIDTH):
            if (row, col) in THRONE_SQUARES:
                continue
            bgr = _get_border_color(image, row, col)
            team = _classify_square_team(bgr, device=device)
            if team in enemy_teams:
                is_adj = _is_adjacent_to_my_territory(image, row, col, device=device)
                has_flg = _has_flag(image, row, col)
                if is_adj:
                    adjacent += 1
                    if has_flg:
                        flagged += 1
                        # Mark flagged on debug image
                        cx, cy = _get_square_center(row, col)
                        cv2.line(debug_img, (cx-7, cy-7), (cx+7, cy+7), (0, 0, 200), 2)
                        cv2.line(debug_img, (cx-7, cy+7), (cx+7, cy-7), (0, 0, 200), 2)
                    else:
                        valid_targets += 1
                        # Mark valid targets on debug image
                        cx, cy = _get_square_center(row, col)
                        cv2.circle(debug_img, (cx, cy), 9, (0, 255, 0), 2)

    # --- Save debug image --------------------------------------------------------
    safe_device = device.replace(":", "_").replace(".", "_")
    debug_path = os.path.join("debug", f"territory_diag_{safe_device}.png")
    os.makedirs("debug", exist_ok=True)
    cv2.imwrite(debug_path, debug_img)

    # --- Log results -------------------------------------------------------------
    log.info("=== TERRITORY GRID DIAGNOSTIC ===")
    log.info("Team config: my_team=%s, enemies=%s", config.get_device_config(device, "my_team"), config.get_device_enemy_teams(device))
    log.info("Classification counts: %s", dict(sorted(team_counts.items())))
    log.info("Enemy: %d total, %d adjacent, %d flagged, %d valid targets",
             enemy_count, adjacent, flagged, valid_targets)
    log.info("Grid map (Y=yellow G=green R=red B=blue ?=unknown T=throne):")
    for i, row_str in enumerate(grid_map):
        log.info("  row %2d: %s", i, row_str)

    if unknown_details:
        log.info("Unknown squares (%d) — nearest color & distance:", len(unknown_details))
        for row, col, bgr, nearest, dist in sorted(unknown_details, key=lambda x: x[4]):
            log.info("  (%2d,%2d) BGR=%-18s nearest=%-7s dist=%.1f",
                     row, col, bgr, nearest, dist)
    else:
        log.info("No unknown squares — all 576 classified!")

    log.info("Debug image saved: %s", debug_path)
    log.info("=== END DIAGNOSTIC ===")

    # Return to map screen
    navigate(Screen.MAP, device)


# Backwards-compatible alias
sample_specific_squares = diagnose_grid


# ============================================================
# TERRITORY COORDINATE SCANNER
# ============================================================

# Region where world coordinates appear on MAP screen (bottom area)
# Initial broad region — will narrow after first calibration run
_COORD_OCR_REGION = (0, 1750, 1080, 1870)

_COORD_DB_PATH = os.path.join("data", "territory_coordinates.json")


def _parse_coordinates(text):
    """Extract (x, y) world coordinates from OCR text like 'x:150, y:7050'.

    Handles common OCR artifacts: 'X' vs 'x', missing colons, extra spaces.
    Returns (x, y) tuple or None if parsing fails.
    """
    import re
    # Normalize common OCR issues
    text = text.replace(" ", "").lower()
    match = re.search(r"x[:\.]?(\d+)[,\s]*y[:\.]?(\d+)", text)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def scan_territory_coordinates(device, squares=None, save_screenshots=True):
    """Scan territory squares to map grid positions to world coordinates.

    Clicks each territory square, which teleports to the tower's map location,
    then OCR-reads the world coordinates from the MAP screen.

    Args:
        device: ADB device ID
        squares: List of (row, col) tuples to scan. None = all non-throne squares.
        save_screenshots: Save a screenshot per square for calibration/debugging.

    Results are saved to data/territory_coordinates.json.
    """
    from vision import load_screenshot, read_text

    log = get_logger("territory", device)
    log.info("=== TERRITORY COORDINATE SCAN ===")

    # Load existing database if any
    coord_db = {}
    if os.path.isfile(_COORD_DB_PATH):
        try:
            with open(_COORD_DB_PATH, "r") as f:
                import json
                coord_db = json.load(f)
        except Exception:
            pass

    if squares is None:
        squares = [(r, c) for r in range(GRID_HEIGHT) for c in range(GRID_WIDTH)
                    if (r, c) not in THRONE_SQUARES]

    log.info("Scanning %d squares...", len(squares))
    scanned = 0
    failed = 0

    for row, col in squares:
        # Navigate to territory screen
        if not navigate(Screen.TERRITORY, device):
            log.warning("Failed to navigate to territory, aborting scan")
            break

        time.sleep(0.5)

        # Click the square
        cx, cy = _get_square_center(row, col)
        log.debug("Clicking square (%d, %d) at pixel (%d, %d)", row, col, cx, cy)
        adb_tap(device, cx, cy)

        # Wait for MAP transition
        time.sleep(2)

        # Take screenshot
        screen = load_screenshot(device)
        if screen is None:
            log.warning("Screenshot failed for square (%d, %d)", row, col)
            failed += 1
            continue

        # Save debug screenshot if requested
        if save_screenshots:
            safe_device = device.replace(":", "_").replace(".", "_")
            shot_dir = os.path.join("debug", "territory_coords")
            os.makedirs(shot_dir, exist_ok=True)
            shot_path = os.path.join(shot_dir, f"{safe_device}_{row:02d}_{col:02d}.png")
            cv2.imwrite(shot_path, screen)

        # OCR the coordinate region
        text = read_text(screen, region=_COORD_OCR_REGION,
                         allowlist="0123456789xy:,. ", device=device)
        coords = _parse_coordinates(text)

        key = f"{row},{col}"
        if coords:
            coord_db[key] = {"x": coords[0], "y": coords[1]}
            log.info("  (%2d,%2d) → x:%d, y:%d", row, col, coords[0], coords[1])
            scanned += 1
        else:
            log.warning("  (%2d,%2d) → OCR failed: '%s'", row, col, text)
            coord_db[key] = {"x": None, "y": None, "ocr_raw": text}
            failed += 1

    # Save database
    os.makedirs(os.path.dirname(_COORD_DB_PATH), exist_ok=True)
    import json
    with open(_COORD_DB_PATH, "w") as f:
        json.dump(coord_db, f, indent=2, sort_keys=True)

    log.info("Scan complete: %d succeeded, %d failed", scanned, failed)
    log.info("Database saved to %s (%d total entries)", _COORD_DB_PATH, len(coord_db))
    log.info("=== END COORDINATE SCAN ===")

    # Return to map
    navigate(Screen.MAP, device)


def scan_test_squares(device):
    """Quick scan of just the 4 corner squares to calibrate OCR region.

    Run this first to verify coordinate reading works before doing a full scan.
    Screenshots saved to debug/territory_coords/ for visual inspection.
    """
    corners = [(0, 0), (0, 23), (23, 0), (23, 23)]
    scan_territory_coordinates(device, squares=corners, save_screenshots=True)
