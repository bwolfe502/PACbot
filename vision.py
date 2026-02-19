import subprocess
import cv2
import time
import random
import numpy as np

from config import adb_path, BUTTONS

# ============================================================
# TEMPLATE CACHE
# ============================================================

_template_cache = {}

def get_template(image_path):
    """Load a template image, caching it for reuse."""
    if image_path not in _template_cache:
        img = cv2.imread(image_path)
        _template_cache[image_path] = img
    return _template_cache[image_path]

# ============================================================
# SCREENSHOT HELPERS
# ============================================================

def load_screenshot(device):
    """Take a screenshot and return the image directly in memory (no disk I/O)."""
    time.sleep(random.uniform(0.1, 0.3))
    result = subprocess.run(
        [adb_path, "-s", device, "exec-out", "screencap", "-p"],
        capture_output=True
    )
    if result.returncode != 0 or not result.stdout:
        print(f"[{device}] Screenshot failed")
        return None
    img_array = np.frombuffer(result.stdout, dtype=np.uint8)
    image = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if image is None:
        print(f"[{device}] Failed to decode screenshot")
    return image

def find_image(screen, image_name, threshold=0.8):
    """Find an image template on screen. Returns (max_val, max_loc, h, w) or None."""
    button = get_template(f"elements/{image_name}")
    if screen is None or button is None:
        return None

    result = cv2.matchTemplate(screen, button, cv2.TM_CCOEFF_NORMED)
    min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

    if max_val > threshold:
        h, w = button.shape[:2]
        return max_val, max_loc, h, w
    return None

def find_all_matches(screen, image_name, threshold=0.8, min_distance=50):
    """Find all non-overlapping matches of a template on screen."""
    template = get_template(f"elements/{image_name}")
    if screen is None or template is None:
        return []

    result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    loc = np.where(result >= threshold)
    points = list(zip(*loc[::-1]))  # (x, y) pairs

    if not points:
        return []

    # Deduplicate nearby matches
    unique = []
    for pt in points:
        if not unique or all(
            abs(pt[0] - u[0]) > min_distance or abs(pt[1] - u[1]) > min_distance
            for u in unique
        ):
            unique.append(pt)
    return unique

# ============================================================
# INPUT FUNCTIONS
# ============================================================

def adb_tap(device, x, y):
    """Send a tap command via ADB."""
    subprocess.run([adb_path, "-s", device, "shell", "input", "tap", str(x), str(y)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def adb_swipe(device, x1, y1, x2, y2, duration_ms=300):
    """Send a swipe command via ADB."""
    subprocess.run([adb_path, "-s", device, "shell", "input", "swipe",
                    str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def tap(button_name, device):
    """Tap a button by its name from the BUTTONS dictionary"""
    x = BUTTONS[button_name]["x"]
    y = BUTTONS[button_name]["y"]
    adb_tap(device, x, y)
    print(f"[{device}] Tapped {button_name} at {x}, {y}")

def tap_image(image_name, device, threshold=0.8):
    """Find an image on screen and tap it"""
    screen = load_screenshot(device)
    match = find_image(screen, image_name, threshold=threshold)

    if match:
        max_val, max_loc, h, w = match
        center_x = max_loc[0] + w // 2
        center_y = max_loc[1] + h // 2
        adb_tap(device, center_x, center_y)
        print(f"[{device}] Found and tapped {image_name} at ({center_x}, {center_y})")
        return True
    else:
        print(f"[{device}] Couldn't find {image_name}")
        return False

def wait_for_image_and_tap(image_name, device, timeout=5, threshold=0.8):
    """Wait for an image to appear on screen and tap it, with a timeout"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if tap_image(image_name, device, threshold=threshold):
            return True
        time.sleep(0.5)
    print(f"[{device}] Timed out waiting for {image_name} after {timeout}s")
    return False

def tap_tower_until_attack_menu(device, tower_x=540, tower_y=900, timeout=10):
    """Tap the tower repeatedly until the attack button menu appears"""
    start_time = time.time()
    attempt = 0
    while time.time() - start_time < timeout:
        attempt += 1
        print(f"[{device}] Tapping tower at ({tower_x}, {tower_y}), attempt {attempt}...")
        adb_tap(device, tower_x, tower_y)
        time.sleep(1)
        if tap_image("attack_button.png", device):
            print(f"[{device}] Attack menu opened and attack button tapped!")
            return True
    print(f"[{device}] Timed out waiting for attack menu after {timeout}s")
    return False
