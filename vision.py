import subprocess
import cv2
import time
import random
import os
import re
import numpy as np
from datetime import datetime

import warnings
import logging

# Suppress noisy warnings from torch/easyocr before importing
warnings.filterwarnings("ignore", message=".*pin_memory.*")
warnings.filterwarnings("ignore", message=".*GPU.*")
logging.getLogger("easyocr").setLevel(logging.ERROR)

import easyocr

import config
from config import adb_path, BUTTONS

# Initialize EasyOCR reader once (downloads models on first run)
_ocr_reader = None

def _get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is None:
        print("[OCR] Initializing EasyOCR (first run may download models)...")
        _ocr_reader = easyocr.Reader(['en'], gpu=False, verbose=False)
        print("[OCR] Ready.")
    return _ocr_reader

# ============================================================
# CLICK TRAIL (debug tap logging)
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLICKS_DIR = os.path.join(SCRIPT_DIR, "debug", "clicks")
os.makedirs(CLICKS_DIR, exist_ok=True)

_click_seq = 0

def _cleanup_clicks_dir(max_files=20):
    try:
        files = sorted(
            [os.path.join(CLICKS_DIR, f) for f in os.listdir(CLICKS_DIR) if f.endswith(".png")],
            key=os.path.getmtime
        )
        while len(files) > max_files:
            os.remove(files.pop(0))
    except Exception:
        pass

def _save_click_trail(screen, device, x, y, label="tap"):
    """Save a screenshot with a marker at the tap point for debugging."""
    global _click_seq
    if not config.CLICK_TRAIL_ENABLED:
        return
    try:
        _click_seq += 1
        annotated = screen.copy()
        cv2.circle(annotated, (int(x), int(y)), 30, (0, 0, 255), 3)
        cv2.circle(annotated, (int(x), int(y)), 5, (0, 0, 255), -1)
        cv2.putText(annotated, label, (int(x) + 35, int(y) - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        timestamp = datetime.now().strftime("%H%M%S")
        safe_device = device.replace(":", "_")
        filename = f"{_click_seq:03d}_{timestamp}_{safe_device}_{label}.png"
        cv2.imwrite(os.path.join(CLICKS_DIR, filename), annotated)
        _cleanup_clicks_dir()
    except Exception:
        pass

def clear_click_trail():
    """Clear all click trail images. Call at the start of an action run."""
    global _click_seq
    _click_seq = 0
    try:
        for f in os.listdir(CLICKS_DIR):
            if f.endswith(".png"):
                os.remove(os.path.join(CLICKS_DIR, f))
    except Exception:
        pass

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

# ============================================================
# OCR (Optical Character Recognition)
# ============================================================

def read_text(screen, region=None, allowlist=None):
    """Read text from a screenshot using OCR.
    screen: CV2 image (BGR).
    region: optional (x1, y1, x2, y2) to read only a portion of the screen.
    allowlist: optional string of allowed characters (e.g. '0123456789' for numbers only).
    Returns the recognized text string (stripped of leading/trailing whitespace).
    """
    if screen is None:
        return ""

    img = screen
    if region:
        x1, y1, x2, y2 = region
        img = screen[y1:y2, x1:x2]

    # Convert to grayscale and upscale for better OCR accuracy
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

    reader = _get_ocr_reader()
    results = reader.readtext(gray, allowlist=allowlist, detail=0)
    return " ".join(results).strip()


def read_number(screen, region=None):
    """Read a number from the screen. Returns the integer value, or None if no number found."""
    raw = read_text(screen, region=region, allowlist="0123456789,.")
    # Strip commas, periods, and spaces used as thousands separators
    cleaned = raw.replace(",", "").replace(".", "").replace(" ", "")
    if cleaned.isdigit():
        return int(cleaned)
    return None


def read_text_from_device(device, region=None, allowlist=None):
    """Convenience: take a screenshot from a device and read text from it."""
    screen = load_screenshot(device)
    return read_text(screen, region=region, allowlist=allowlist)


def read_number_from_device(device, region=None):
    """Convenience: take a screenshot from a device and read a number from it."""
    screen = load_screenshot(device)
    return read_number(screen, region=region)


# Region where AP is displayed (bottom-right, under SEARCH button)
_AP_REGION = (600, 1850, 1080, 1920)

def read_ap(device, retries=5):
    """Read current AP from the bottom-right of the screen.
    The display cycles between AP (e.g. '101/400') and a timer every few seconds,
    so this retries up to `retries` times with a short delay between attempts.
    Uses thresholding to isolate white text from the green background.
    Returns (current, max) tuple, or None if AP couldn't be read.
    """
    for attempt in range(retries):
        screen = load_screenshot(device)
        if screen is None:
            time.sleep(1)
            continue

        # Crop, grayscale, upscale, then threshold to isolate white text
        x1, y1, x2, y2 = _AP_REGION
        img = screen[y1:y2, x1:x2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        _, thresh = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)

        reader = _get_ocr_reader()
        results = reader.readtext(thresh, allowlist="0123456789/", detail=0)
        raw = " ".join(results).strip()

        match = re.search(r"(\d+)/(\d+)", raw)
        if match:
            current = int(match.group(1))
            maximum = int(match.group(2))
            print(f"[{device}] AP: {current}/{maximum}")
            return current, maximum

        # Probably showing the timer right now, wait and retry
        if attempt < retries - 1:
            time.sleep(2)

    print(f"[{device}] Could not read AP after {retries} attempts")
    return None


# ============================================================
# TEMPLATE MATCHING
# ============================================================

def find_image(screen, image_name, threshold=0.8, region=None):
    """Find an image template on screen. Returns (max_val, max_loc, h, w) or None.
    region: optional (x1, y1, x2, y2) to restrict search area. Coordinates are in full-screen space."""
    button = get_template(f"elements/{image_name}")
    if screen is None or button is None:
        return None

    if region:
        x1, y1, x2, y2 = region
        cropped = screen[y1:y2, x1:x2]
        result = cv2.matchTemplate(cropped, button, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)
        if max_val > threshold:
            h, w = button.shape[:2]
            # Translate back to full-screen coordinates
            return max_val, (max_loc[0] + x1, max_loc[1] + y1), h, w
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

# Region constraints for templates that should only match in specific screen areas.
# Values are (x1, y1, x2, y2) defining the search region.
# Screen is 1080x1920 — lower-left quadrant = (0, 960, 540, 1920)
IMAGE_REGIONS = {
    "heal.png": (0, 960, 540, 1920),
}

def tap_image(image_name, device, threshold=0.8):
    """Find an image on screen and tap it"""
    screen = load_screenshot(device)
    region = IMAGE_REGIONS.get(image_name)
    match = find_image(screen, image_name, threshold=threshold, region=region)

    if match:
        max_val, max_loc, h, w = match
        center_x = max_loc[0] + w // 2
        center_y = max_loc[1] + h // 2
        if screen is not None:
            _save_click_trail(screen, device, center_x, center_y, image_name.replace(".png", ""))
        adb_tap(device, center_x, center_y)
        print(f"[{device}] Found and tapped {image_name} at ({center_x}, {center_y})")
        return True
    else:
        print(f"[{device}] Couldn't find {image_name}")
        return False

def logged_tap(device, x, y, label="coord_tap"):
    """Tap a coordinate and save a click trail screenshot for debugging."""
    if config.CLICK_TRAIL_ENABLED:
        screen = load_screenshot(device)
        if screen is not None:
            _save_click_trail(screen, device, x, y, label)
    adb_tap(device, x, y)

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
