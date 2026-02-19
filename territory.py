import cv2
import numpy as np
import time
import random
import tkinter as tk
from PIL import Image, ImageTk

import config
from config import (SQUARE_SIZE, GRID_OFFSET_X, GRID_OFFSET_Y,
                    GRID_WIDTH, GRID_HEIGHT, THRONE_SQUARES, BORDER_COLORS)
from vision import load_screenshot, tap_image, adb_tap, tap_tower_until_attack_menu, get_template
from navigation import navigate
from troops import troops_avail, all_troops_home, heal_all
from actions import teleport

# ============================================================
# TERRITORY SQUARE MANAGER GUI
# ============================================================

def open_territory_manager(device):
    """Open a visual interface to manually select squares to attack or ignore"""

    # Take a screenshot of territory screen
    if not navigate("territory_screen", device):
        print(f"[{device}] Failed to navigate to territory screen")
        return

    time.sleep(1)
    full_image = load_screenshot(device)

    if full_image is None:
        print(f"[{device}] Failed to load screenshot")
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
    manager = tk.Toplevel()
    manager.title(f"Territory Square Manager - {device}")

    # Instructions
    instructions = tk.Label(
        manager,
        text="Click squares: GREEN = Force Attack | RED = Ignore | None = Auto",
        font=("Arial", 9),
        bg="lightgray",
        pady=5
    )
    instructions.pack(fill=tk.X)

    # Stats display
    stats_var = tk.StringVar()
    stats_var.set(f"Attack: {len(config.MANUAL_ATTACK_SQUARES)} | Ignore: {len(config.MANUAL_IGNORE_SQUARES)}")
    stats_label = tk.Label(manager, textvariable=stats_var, font=("Arial", 8))
    stats_label.pack(pady=2)

    # Create canvas with the territory image
    canvas_frame = tk.Frame(manager)
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
            print(f"Cannot select throne square ({row}, {col})")
            return

        # Toggle state: None -> Attack -> Ignore -> None
        if (row, col) in config.MANUAL_ATTACK_SQUARES:
            config.MANUAL_ATTACK_SQUARES.remove((row, col))
            config.MANUAL_IGNORE_SQUARES.add((row, col))
            print(f"Square ({row}, {col}) set to IGNORE")
        elif (row, col) in config.MANUAL_IGNORE_SQUARES:
            config.MANUAL_IGNORE_SQUARES.remove((row, col))
            print(f"Square ({row}, {col}) set to AUTO-DETECT")
        else:
            config.MANUAL_ATTACK_SQUARES.add((row, col))
            print(f"Square ({row}, {col}) set to FORCE ATTACK")

        # Update display
        draw_overlay()
        stats_var.set(f"Attack: {len(config.MANUAL_ATTACK_SQUARES)} | Ignore: {len(config.MANUAL_IGNORE_SQUARES)}")

    canvas.bind("<Button-1>", on_canvas_click)

    # Buttons
    button_frame = tk.Frame(manager)
    button_frame.pack(pady=5)

    def clear_all():
        config.MANUAL_ATTACK_SQUARES.clear()
        config.MANUAL_IGNORE_SQUARES.clear()
        draw_overlay()
        stats_var.set(f"Attack: {len(config.MANUAL_ATTACK_SQUARES)} | Ignore: {len(config.MANUAL_IGNORE_SQUARES)}")
        print("Cleared all manual selections")

    def clear_attack():
        config.MANUAL_ATTACK_SQUARES.clear()
        draw_overlay()
        stats_var.set(f"Attack: {len(config.MANUAL_ATTACK_SQUARES)} | Ignore: {len(config.MANUAL_IGNORE_SQUARES)}")
        print("Cleared manual attack selections")

    def clear_ignore():
        config.MANUAL_IGNORE_SQUARES.clear()
        draw_overlay()
        stats_var.set(f"Attack: {len(config.MANUAL_ATTACK_SQUARES)} | Ignore: {len(config.MANUAL_IGNORE_SQUARES)}")
        print("Cleared manual ignore selections")

    tk.Button(button_frame, text="Clear All", command=clear_all, width=10, bg="orange", font=("Arial", 8)).pack(side=tk.LEFT, padx=2)
    tk.Button(button_frame, text="Clear Attack", command=clear_attack, width=10, font=("Arial", 8)).pack(side=tk.LEFT, padx=2)
    tk.Button(button_frame, text="Clear Ignore", command=clear_ignore, width=10, font=("Arial", 8)).pack(side=tk.LEFT, padx=2)
    tk.Button(button_frame, text="Close", command=manager.destroy, width=10, font=("Arial", 8)).pack(side=tk.LEFT, padx=2)

    # Draw initial overlay
    draw_overlay()

    # Just destroy on close
    manager.protocol("WM_DELETE_WINDOW", manager.destroy)

# ============================================================
# TERRITORY ATTACK SYSTEM
# ============================================================

def attack_territory(device, debug=False):
    """Full territory attack workflow: heal, verify troops home, navigate, attack"""
    print(f"[{device}] Starting territory attack workflow...")

    # Step 1: Navigate to map screen
    if not navigate("map_screen", device):
        print(f"[{device}] Failed to navigate to map screen")
        return False

    # Step 2: Heal all troops
    print(f"[{device}] Healing troops...")
    heal_all(device)
    time.sleep(2)

    # Step 3: Verify all troops are home
    print(f"[{device}] Checking if all troops are home...")
    if not all_troops_home(device):
        print(f"[{device}] Not all troops are home! Aborting territory attack.")
        return False

    print(f"[{device}] All troops confirmed home. Proceeding...")

    # Step 4: Navigate to territory screen
    if not navigate("territory_screen", device):
        print(f"[{device}] Failed to navigate to territory screen")
        return False

    time.sleep(1)

    # Step 5: Take screenshot and analyze grid
    image = load_screenshot(device)

    if image is None:
        print(f"[{device}] Failed to load screenshot")
        return False

    if debug:
        debug_img = image.copy()

    def get_square_center(row, col):
        """Get pixel coordinates of square center"""
        x = int(GRID_OFFSET_X + col * SQUARE_SIZE + SQUARE_SIZE / 2)
        y = int(GRID_OFFSET_Y + row * SQUARE_SIZE + SQUARE_SIZE / 2)
        return x, y

    def get_border_color(row, col):
        """Sample the BORDER pixels - avoid clock obstruction for top rows"""
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

    def classify_square_team(bgr):
        """Determine team based on border color - find closest match"""
        b, g, r = bgr

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
        elif best_team in config.ENEMY_TEAMS and min_distance <= 70:
            return best_team
        elif best_team == config.MY_TEAM_COLOR and min_distance <= 90:
            return best_team
        elif min_distance <= 55:
            return best_team

        if best_team == "unknown" and config.MY_TEAM_COLOR in distances and distances[config.MY_TEAM_COLOR] <= 95:
            return config.MY_TEAM_COLOR

        return "unknown"

    def has_flag(row, col):
        """Check if a square has a flag using red flag color #FF5D5A"""
        x = int(GRID_OFFSET_X + col * SQUARE_SIZE)
        y = int(GRID_OFFSET_Y + row * SQUARE_SIZE)
        w = int(SQUARE_SIZE)
        h = int(SQUARE_SIZE)

        square = image[y:y+h, x:x+w]

        red_flag_mask = cv2.inRange(square, (75, 80, 240), (105, 110, 255))
        red_pixels = cv2.countNonZero(red_flag_mask)

        return red_pixels > 15

    def is_adjacent_to_my_territory(row, col):
        """Check if square is DIRECTLY next to my territory"""
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

            border_color = get_border_color(r, c)
            team = classify_square_team(border_color)

            if team == config.MY_TEAM_COLOR:
                return True

        return False

    # Build list of valid targets
    print(f"[{device}] Scanning grid for targets...")
    print(f"[{device}] My team: {config.MY_TEAM_COLOR}, Attacking: {config.ENEMY_TEAMS}")

    targets = []
    enemy_squares = []
    adjacent_enemies = []
    flagged_squares = []

    for row in range(GRID_HEIGHT):
        for col in range(GRID_WIDTH):
            if (row, col) in THRONE_SQUARES:
                continue

            border_color = get_border_color(row, col)
            team = classify_square_team(border_color)

            if team in config.ENEMY_TEAMS:
                enemy_squares.append((row, col))

                if is_adjacent_to_my_territory(row, col):
                    adjacent_enemies.append((row, col))

                    if not has_flag(row, col):
                        targets.append((row, col))
                    else:
                        flagged_squares.append((row, col))

    print(f"[{device}] Enemy squares detected: {len(enemy_squares)}")
    print(f"[{device}] Enemy squares adjacent to my territory: {len(adjacent_enemies)}")
    print(f"[{device}] Flagged squares: {len(flagged_squares)}")
    print(f"[{device}] Valid targets (no flag): {len(targets)}")

    # Apply manual overrides
    print(f"[{device}] Applying manual overrides...")
    print(f"[{device}] Manual attack squares: {len(config.MANUAL_ATTACK_SQUARES)}")
    print(f"[{device}] Manual ignore squares: {len(config.MANUAL_IGNORE_SQUARES)}")

    if config.MANUAL_ATTACK_SQUARES:
        targets = list(config.MANUAL_ATTACK_SQUARES)
        print(f"[{device}] Using ONLY manual attack squares (ignoring auto-detect)")
    else:
        targets = [t for t in targets if t not in config.MANUAL_IGNORE_SQUARES]

    print(f"[{device}] Final targets after manual overrides: {len(targets)}")

    # Create debug visualization
    if debug:
        for row in range(GRID_HEIGHT):
            for col in range(GRID_WIDTH):
                x, y = get_square_center(row, col)

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
        print(f"Saved debug image")

    if targets:
        target_row, target_col = random.choice(targets)
        click_x, click_y = get_square_center(target_row, target_col)

        print(f"[{device}] Attacking square ({target_row}, {target_col})")

        # Remember this square PER DEVICE
        config.LAST_ATTACKED_SQUARE[device] = (target_row, target_col)

        adb_tap(device, click_x, click_y)

        return True
    else:
        print(f"[{device}] No valid targets found")
        return False

# ============================================================
# AUTO OCCUPY SYSTEM
# ============================================================

def _occupy_stopped(device):
    """Check if auto occupy was stopped. Prints a message on first detection."""
    if not config.auto_occupy_running:
        print(f"[{device}] Auto occupy stop requested, aborting...")
        return True
    return False

def _occupy_sleep(seconds):
    """Sleep in 1-second chunks, returning True immediately if stopped."""
    for _ in range(seconds):
        if not config.auto_occupy_running:
            return True
        time.sleep(1)
    return False

def auto_occupy_loop(device):
    """Auto occupy loop: attack territory -> teleport -> click square -> attack -> wait"""
    print(f"[{device}] Auto occupy started")

    while config.auto_occupy_running:
        try:
            # Check for dead.png and tap it if it exists
            print(f"[{device}] Checking for dead.png...")
            if tap_image("dead.png", device):
                print(f"[{device}] Found and clicked dead.png")
                time.sleep(2)

            if _occupy_stopped(device):
                break

            # Check if all troops are home
            if not all_troops_home(device):
                print(f"[{device}] Troops not home, waiting...")
                if _occupy_sleep(10):
                    break
                continue

            print(f"[{device}] === Starting auto occupy cycle ===")

            # Step 1: Attack territory
            print(f"[{device}] Step 1: Attacking territory...")
            if not attack_territory(device, debug=False):
                print(f"[{device}] Failed to attack territory, skipping cycle")
                if _occupy_sleep(10):
                    break
                continue

            if _occupy_stopped(device):
                break
            time.sleep(2)

            # Double-check troops are home before teleporting
            print(f"[{device}] Double-checking troops are home before teleport...")
            if not all_troops_home(device):
                print(f"[{device}] Troops not home! Skipping teleport.")
                if _occupy_sleep(10):
                    break
                continue

            if _occupy_stopped(device):
                break

            # Step 2: Teleport
            print(f"[{device}] Step 2: Teleporting...")
            if not teleport(device):
                print(f"[{device}] Teleport failed, skipping cycle")
                if _occupy_sleep(10):
                    break
                continue

            if _occupy_stopped(device):
                break
            time.sleep(2)

            # Step 3: Navigate back to territory screen and click the square we attacked
            if device in config.LAST_ATTACKED_SQUARE:
                target_row, target_col = config.LAST_ATTACKED_SQUARE[device]
                print(f"[{device}] Step 3: Navigating to territory screen to click square ({target_row}, {target_col})...")

                if not navigate("territory_screen", device):
                    print(f"[{device}] Failed to navigate to territory screen")
                    if _occupy_sleep(10):
                        break
                    continue

                if _occupy_stopped(device):
                    break
                time.sleep(1)

                # Calculate click position for the last attacked square
                click_x = int(GRID_OFFSET_X + target_col * SQUARE_SIZE + SQUARE_SIZE / 2)
                click_y = int(GRID_OFFSET_Y + target_row * SQUARE_SIZE + SQUARE_SIZE / 2)

                print(f"[{device}] Clicking square ({target_row}, {target_col}) at ({click_x}, {click_y})")
                adb_tap(device, click_x, click_y)

                tap_tower_until_attack_menu(device, timeout=10)
            else:
                print(f"[{device}] No last attacked square remembered, skipping territory click")

            if _occupy_stopped(device):
                break

            # Step 4: Attack
            time.sleep(1)
            print(f"[{device}] Step 4: Attacking...")

            if config.AUTO_HEAL_ENABLED:
                heal_all(device)

            troops = troops_avail(device)

            if troops > config.MIN_TROOPS_AVAILABLE:
                tap_image("depart.png", device)
                time.sleep(1)
                tap_image("depart.png", device)
            else:
                print(f"[{device}] Not enough troops available (have {troops}, need more than {config.MIN_TROOPS_AVAILABLE})")

            time.sleep(2)

            print(f"[{device}] Cycle complete, waiting 10 seconds...")

            # Wait 10 seconds before next cycle
            if _occupy_sleep(10):
                break

        except Exception as e:
            print(f"[{device}] Error in auto occupy loop: {e}")
            import traceback
            traceback.print_exc()
            if _occupy_sleep(5):
                break

    print(f"[{device}] Auto occupy stopped")

# ============================================================
# DEBUG FUNCTIONS
# ============================================================

def sample_specific_squares(device):
    """Sample specific squares to understand current colors"""
    image = load_screenshot(device)

    def get_square_color(row, col):
        x = int(GRID_OFFSET_X + col * SQUARE_SIZE + SQUARE_SIZE / 4)
        y = int(GRID_OFFSET_Y + row * SQUARE_SIZE + SQUARE_SIZE / 4)
        w = int(SQUARE_SIZE / 2)
        h = int(SQUARE_SIZE / 2)

        square_sample = image[y:y+h, x:x+w]
        avg_color = cv2.mean(square_sample)[:3]

        return avg_color

    print("\n=== SAMPLING KNOWN SQUARES ===")

    print("\nKnown YELLOW squares:")
    yellow_samples = [(0,0), (0,5), (5,5), (10,10)]
    for r, c in yellow_samples:
        color = get_square_color(r, c)
        b, g, red = [int(x) for x in color]
        print(f"  ({r},{c}): B={b} G={g} R={red}")

    print("\nKnown GREEN squares:")
    green_samples = [(0,12), (0,18), (5,20), (10,23)]
    for r, c in green_samples:
        color = get_square_color(r, c)
        b, g, red = [int(x) for x in color]
        print(f"  ({r},{c}): B={b} G={g} R={red}")

    print("\nKnown RED squares:")
    red_samples = [(12,0), (15,5), (20,10), (23,5)]
    for r, c in red_samples:
        color = get_square_color(r, c)
        b, g, red = [int(x) for x in color]
        print(f"  ({r},{c}): B={b} G={g} R={red}")

    print("\nKnown BLUE squares:")
    blue_samples = [(12,13), (18,18), (20,20), (23,20)]
    for r, c in blue_samples:
        color = get_square_color(r, c)
        b, g, red = [int(x) for x in color]
        print(f"  ({r},{c}): B={b} G={g} R={red}")
