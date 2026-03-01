import subprocess
import cv2
import time
import random
import os
import re
import platform
import numpy as np
from datetime import datetime

import threading

import config
from config import adb_path, BUTTONS, ADB_COMMAND_TIMEOUT
from botlog import get_logger, stats

# Thread-local storage for find_image best score (avoids race between device threads)
_thread_local = threading.local()

# ============================================================
# OCR BACKEND — platform-specific
# ============================================================
#
# Two OCR backends are supported:
#
#   macOS:   Apple Vision framework (via pyobjc-framework-Vision)
#            - Uses the native VNRecognizeTextRequest API
#            - Hardware-accelerated, ~30ms per call (Accurate mode)
#            - No model downloads needed — ships with macOS
#            - Requires: pyobjc-framework-Vision (installed automatically on macOS)
#
#   Windows: EasyOCR (deep learning, PyTorch-based)
#            - Uses a neural network OCR model (~100MB download on first run)
#            - ~500-2000ms per call on CPU
#            - Requires: easyocr, torch (installed via requirements.txt)
#
# Both backends expose the same interface through ocr_read().
# When modifying OCR behavior, update BOTH backends to keep them in sync.
# The easiest way to verify: search for "BACKEND:" comments in this section.
# ============================================================

_USE_APPLE_VISION = platform.system() == "Darwin"

# --- BACKEND: EasyOCR (Windows) ---
# EasyOCR reader is initialized lazily and cached globally.
# Thread-safe via double-checked locking.
# _ocr_infer_lock serializes readtext() calls across device threads to prevent
# per-thread MKL/MKLDNN scratch buffer accumulation (PyTorch issue #64412).
_ocr_reader = None
_ocr_lock = threading.Lock()
_ocr_infer_lock = threading.Lock()

def _get_ocr_reader():
    """Get the EasyOCR reader instance (Windows only).

    Lazy-initialized on first call. Downloads OCR models on first run.
    On macOS, this is never called — Apple Vision is used instead.
    """
    global _ocr_reader
    if _ocr_reader is None:
        with _ocr_lock:
            if _ocr_reader is None:
                # Cap oneDNN/MKLDNN primitive cache BEFORE importing torch.
                # Default is 1024 entries — each unique input shape compiles a
                # new kernel (~MB each). EasyOCR feeds variable-size crops, so
                # the cache fills with stale kernels and bloats to multi-GB.
                import os as _os
                _os.environ.setdefault("ONEDNN_PRIMITIVE_CACHE_CAPACITY", "8")
                # Legacy name (PyTorch < 1.8)
                _os.environ.setdefault("LRU_CACHE_CAPACITY", "8")

                import warnings
                warnings.filterwarnings("ignore", message=".*pin_memory.*")
                warnings.filterwarnings("ignore", message=".*GPU.*")
                import torch
                import easyocr

                # Limit PyTorch intra-op parallelism. Multiple device threads
                # already provide inter-op parallelism; letting each also spawn
                # N_cores intra-op threads causes oversubscription and bloat.
                torch.set_num_threads(2)

                _log = get_logger("vision")
                _log.info("Initializing EasyOCR (first run may download models)...")
                _ocr_reader = easyocr.Reader(['en'], gpu=False, verbose=False)
                _log.info("EasyOCR ready (MKLDNN cache cap=8, threads=2).")
    return _ocr_reader

# --- BACKEND: Apple Vision (macOS) ---

def _apple_vision_ocr(image, allowlist=None):
    """Run OCR using Apple's Vision framework.

    Takes a grayscale or BGR numpy array, returns list of (text, confidence) tuples.
    The allowlist parameter is applied as a post-filter (Vision doesn't support
    character allowlists natively, but its accuracy is high enough that filtering
    after recognition works well).

    Only called on macOS — guarded by _USE_APPLE_VISION flag.
    """
    import Vision
    import Quartz

    # Convert numpy array to CGImage via PNG bytes
    _, png_bytes = cv2.imencode(".png", image)
    data = Quartz.CFDataCreate(None, png_bytes.tobytes(), len(png_bytes))
    image_source = Quartz.CGImageSourceCreateWithData(data, None)
    cg_image = Quartz.CGImageSourceCreateImageAtIndex(image_source, 0, None)

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, None)
    success, error = handler.performRequests_error_([request], None)
    if not success:
        get_logger("vision").warning("Apple Vision OCR failed: %s", error)
        return []

    results = []
    for obs in request.results():
        candidate = obs.topCandidates_(1)[0]
        text = candidate.string()
        confidence = candidate.confidence()

        # Apply allowlist filter if specified
        if allowlist:
            text = "".join(c for c in text if c in allowlist)

        if text:
            results.append((text, confidence))

    return results


def warmup_ocr():
    """Pre-initialize the OCR engine in the background so the first real call is fast.

    On Windows: loads EasyOCR + downloads models (can take 10-30s on first run).
    On macOS: triggers Apple Vision framework initialization (~5-7s first call,
              then ~50ms per call). Without this, the first quest check would stall.

    Call this from main.py at startup in a background thread.
    """
    _log = get_logger("vision")
    if _USE_APPLE_VISION:
        _log.info("Warming up Apple Vision OCR...")
        # Trigger framework load with a tiny dummy image
        dummy = np.zeros((10, 100), dtype=np.uint8)
        cv2.putText(dummy, "init", (2, 8), cv2.FONT_HERSHEY_SIMPLEX, 0.3, 255, 1)
        _apple_vision_ocr(dummy)
        _log.info("Apple Vision OCR ready.")
    else:
        _log.info("Warming up EasyOCR engine in background...")
        _get_ocr_reader()
        _log.info("EasyOCR ready.")


def ocr_read(image, allowlist=None, detail=0):
    """Unified OCR interface — works on both macOS (Apple Vision) and Windows (EasyOCR).

    Args:
        image: Preprocessed image (grayscale numpy array, already upscaled).
        allowlist: Optional string of allowed characters (e.g. "0123456789/").
        detail: 0 = return list of text strings only.
                1 = return list of (bbox, text, confidence) tuples (EasyOCR format).

    Returns:
        detail=0: List of recognized text strings.
        detail=1: List of (bbox, text, confidence) tuples.

    When modifying this function, ensure BOTH backends produce compatible output.
    """
    if _USE_APPLE_VISION:
        # --- BACKEND: Apple Vision (macOS) ---
        results = _apple_vision_ocr(image, allowlist=allowlist)
        if detail == 0:
            return [text for text, conf in results]
        else:
            # Return EasyOCR-compatible format: (bbox, text, confidence)
            # bbox is set to None since Apple Vision uses different coordinate systems
            # and no caller currently uses bbox from detail=1 results.
            return [(None, text, conf) for text, conf in results]
    else:
        # --- BACKEND: EasyOCR (Windows) ---
        # Serialize inference to prevent concurrent MKL thread-local state
        # accumulation across device threads (PyTorch issue #64412).
        reader = _get_ocr_reader()
        with _ocr_infer_lock:
            if detail == 0:
                return reader.readtext(image, allowlist=allowlist, detail=0)
            else:
                return reader.readtext(image, allowlist=allowlist, detail=1)

# ============================================================
# CLICK TRAIL (debug tap logging)
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLICKS_DIR = os.path.join(SCRIPT_DIR, "debug", "clicks")
FAILURES_DIR = os.path.join(SCRIPT_DIR, "debug", "failures")
os.makedirs(CLICKS_DIR, exist_ok=True)
os.makedirs(FAILURES_DIR, exist_ok=True)

_click_seq = 0
_click_seq_lock = threading.Lock()

def _cleanup_clicks_dir(max_files=config.CLICK_TRAIL_MAX):
    try:
        files = sorted(
            [os.path.join(CLICKS_DIR, f) for f in os.listdir(CLICKS_DIR) if f.endswith(".png")],
            key=os.path.getmtime
        )
        while len(files) > max_files:
            os.remove(files.pop(0))
    except Exception as e:
        get_logger("vision").warning("Click trail cleanup failed: %s", e)

def _save_click_trail(screen, device, x, y, label="tap"):
    """Save a screenshot with a marker at the tap point for debugging."""
    global _click_seq
    if not config.CLICK_TRAIL_ENABLED:
        return
    try:
        with _click_seq_lock:
            _click_seq += 1
            seq = _click_seq
        annotated = screen.copy()
        cv2.circle(annotated, (int(x), int(y)), 30, (0, 0, 255), 3)
        cv2.circle(annotated, (int(x), int(y)), 5, (0, 0, 255), -1)
        cv2.putText(annotated, label, (int(x) + 35, int(y) - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_device = device.replace(":", "_")
        filename = f"{seq:03d}_{timestamp}_{safe_device}_{label}.png"
        cv2.imwrite(os.path.join(CLICKS_DIR, filename), annotated)
        _cleanup_clicks_dir()
    except Exception as e:
        get_logger("vision", device).warning("Click trail save failed: %s", e)

def clear_click_trail():
    """Clear all click trail images. Call at the start of an action run."""
    global _click_seq
    with _click_seq_lock:
        _click_seq = 0
    try:
        for f in os.listdir(CLICKS_DIR):
            if f.endswith(".png"):
                os.remove(os.path.join(CLICKS_DIR, f))
    except Exception as e:
        get_logger("vision").warning("Click trail clear failed: %s", e)

# ============================================================
# FAILURE SCREENSHOTS (persistent — never auto-deleted)
# ============================================================

def _cleanup_failures_dir(max_files=config.FAILURE_SCREENSHOT_MAX):
    """Keep failure screenshots bounded but generous. These are NOT rotated
    aggressively like click trails — they persist across sessions for
    post-mortem analysis. Only the oldest are pruned once we exceed the limit."""
    try:
        files = sorted(
            [os.path.join(FAILURES_DIR, f) for f in os.listdir(FAILURES_DIR) if f.endswith(".png")],
            key=os.path.getmtime
        )
        while len(files) > max_files:
            os.remove(files.pop(0))
    except Exception as e:
        get_logger("vision").warning("Failure screenshot cleanup failed: %s", e)

def save_failure_screenshot(device, label, screen=None):
    """Save a persistent failure screenshot for post-mortem diagnosis.

    Unlike click trail images, these are stored in debug/failures/ and are
    NOT cleared between runs. They persist until 200 accumulate, then only
    the oldest are pruned. Use this at every failure point in critical flows
    so there's always a visual record of what the screen looked like when
    something went wrong.

    Returns the saved filepath, or None on error.
    """
    log = get_logger("vision", device)
    try:
        if screen is None:
            screen = load_screenshot(device)
        if screen is None:
            log.warning("Cannot save failure screenshot — screenshot failed")
            return None
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_device = device.replace(":", "_")
        safe_label = label.replace(" ", "_").replace("/", "_")
        filename = f"{timestamp}_{safe_device}_{safe_label}.png"
        filepath = os.path.join(FAILURES_DIR, filename)
        cv2.imwrite(filepath, screen)
        log.info("Failure screenshot saved: debug/failures/%s", filename)
        _cleanup_failures_dir()
        return filepath
    except Exception as e:
        log.warning("Failed to save failure screenshot: %s", e)
        return None

# ============================================================
# TEMPLATE CACHE
# ============================================================

_template_cache = {}

def get_template(image_path):
    """Load a template image, caching it for reuse."""
    if image_path not in _template_cache:
        img = cv2.imread(image_path)
        if img is None:
            get_logger("vision").warning("Template not found: %s", image_path)
        _template_cache[image_path] = img
        if len(_template_cache) % 25 == 0:
            get_logger("vision").debug("Template cache size: %d entries", len(_template_cache))
    return _template_cache[image_path]

# ============================================================
# SCREENSHOT HELPERS
# ============================================================

def load_screenshot(device):
    """Take a screenshot and return the image directly in memory (no disk I/O)."""
    log = get_logger("vision", device)
    t0 = time.time()
    try:
        result = subprocess.run(
            [adb_path, "-s", device, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=ADB_COMMAND_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        log.warning("Screenshot timed out after %ds (ADB hung?)", ADB_COMMAND_TIMEOUT)
        stats.record_adb_timing(device, "screenshot", float(ADB_COMMAND_TIMEOUT), success=False)
        return None
    elapsed = time.time() - t0
    if result.returncode != 0 or not result.stdout:
        log.warning("Screenshot failed (returncode=%d, %.2fs)", result.returncode, elapsed)
        stats.record_adb_timing(device, "screenshot", elapsed, success=False)
        return None
    img_array = np.frombuffer(result.stdout, dtype=np.uint8)
    image = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if image is None:
        log.warning("Failed to decode screenshot (%.2fs)", elapsed)
        stats.record_adb_timing(device, "screenshot", elapsed, success=False)
        return image
    stats.record_adb_timing(device, "screenshot", elapsed)
    if elapsed > 3.0:
        log.warning("Screenshot slow: %.2fs (ADB may be degrading)", elapsed)
    return image

# ============================================================
# OCR (Optical Character Recognition)
# ============================================================

def read_text(screen, region=None, allowlist=None, device=None):
    """Read text from a screenshot using OCR.
    screen: CV2 image (BGR).
    region: optional (x1, y1, x2, y2) to read only a portion of the screen.
    allowlist: optional string of allowed characters (e.g. '0123456789' for numbers only).
    device: optional device ID for logging context.
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

    # Uses ocr_read() which dispatches to Apple Vision (macOS) or EasyOCR (Windows)
    results = ocr_read(gray, allowlist=allowlist, detail=1)
    # Extract text and confidence from detail=1 results: (bbox, text, confidence)
    texts = [entry[1] for entry in results]
    if results:
        confidences = [entry[2] for entry in results]
        avg_conf = sum(confidences) / len(confidences)
        min_conf = min(confidences)
        log = get_logger("vision", device)
        if min_conf < 0.5:
            log.warning("OCR low confidence: avg=%.0f%%, min=%.0f%%, text='%s'",
                        avg_conf * 100, min_conf * 100, " ".join(texts).strip())
        elif avg_conf < 0.7:
            log.debug("OCR moderate confidence: avg=%.0f%%, min=%.0f%%, text='%s'",
                       avg_conf * 100, min_conf * 100, " ".join(texts).strip())
    return " ".join(texts).strip()


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
    return read_text(screen, region=region, allowlist=allowlist, device=device)


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
    log = get_logger("vision", device)
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

        # Uses ocr_read() which dispatches to Apple Vision (macOS) or EasyOCR (Windows)
        results = ocr_read(thresh, allowlist="0123456789/", detail=0)
        raw = " ".join(results).strip()

        match = re.search(r"(\d+)/(\d+)", raw)
        if match:
            current = int(match.group(1))
            maximum = int(match.group(2))
            log.debug("AP: %d/%d", current, maximum)
            return current, maximum

        # Probably showing the timer right now, wait and retry
        if attempt < retries - 1:
            time.sleep(2)

    log.warning("Could not read AP after %d attempts", retries)
    return None


# ============================================================
# TEMPLATE MATCHING
# ============================================================

def get_last_best():
    """Get the best match score from the last find_image call on this thread."""
    return getattr(_thread_local, 'last_best', 0.0)

def _match_in_region(screen, button, region):
    """Run matchTemplate on a cropped region, return (max_val, full_screen_loc)."""
    x1, y1, x2, y2 = region
    cropped = screen[y1:y2, x1:x2]
    if cropped.shape[0] < button.shape[0] or cropped.shape[1] < button.shape[1]:
        return 0.0, (0, 0)
    result = cv2.matchTemplate(cropped, button, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    # Translate back to full-screen coordinates
    return max_val, (max_loc[0] + x1, max_loc[1] + y1)


def find_image(screen, image_name, threshold=0.8, region=None, device=None):
    """Find an image template on screen.
    Returns (max_val, max_loc, h, w) on match, or None on failure.
    On failure, the best score is stored per-thread via get_last_best().
    If device is provided, records hit position to stats for region analysis.

    When a region is provided, tries the cropped search first for speed.
    If that misses, falls back to a full-screen search so that UI shifts
    (different devices, resolutions, or game updates) don't cause hard failures.
    A fallback hit logs a warning — check stats to widen the region.
    """
    _thread_local.last_best = 0.0
    button = get_template(f"elements/{image_name}")
    if screen is None or button is None:
        return None

    log = get_logger("vision", device)
    h, w = button.shape[:2]

    if region:
        # Fast path: search in cropped region first
        max_val, loc = _match_in_region(screen, button, region)
        _thread_local.last_best = max_val
        if max_val > threshold:
            if device:
                stats.record_template_hit(
                    device, image_name, loc[0] + w // 2, loc[1] + h // 2, max_val)
            return max_val, loc, h, w

        # Some templates must ONLY match in their region (no fallback)
        # to prevent false positives elsewhere on screen.
        if image_name in _REGION_STRICT:
            return None

        # Fallback: search full screen in case the element moved outside the region
        result = cv2.matchTemplate(screen, button, cv2.TM_CCOEFF_NORMED)
        _, max_val_full, _, max_loc_full = cv2.minMaxLoc(result)
        _thread_local.last_best = max(max_val, max_val_full)
        if max_val_full > threshold:
            log.warning("REGION MISS for %s — found via full-screen fallback at (%d, %d). "
                        "Consider widening IMAGE_REGIONS.",
                        image_name, max_loc_full[0] + w // 2, max_loc_full[1] + h // 2)
            if device:
                stats.record_template_hit(
                    device, image_name, max_loc_full[0] + w // 2, max_loc_full[1] + h // 2, max_val_full)
            return max_val_full, max_loc_full, h, w
        return None

    result = cv2.matchTemplate(screen, button, cv2.TM_CCOEFF_NORMED)
    min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)
    _thread_local.last_best = max_val

    if max_val > threshold:
        if device:
            stats.record_template_hit(
                device, image_name, max_loc[0] + w // 2, max_loc[1] + h // 2, max_val)
        return max_val, max_loc, h, w
    return None

def find_all_matches(screen, image_name, threshold=0.8, min_distance=50, device=None):
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
    log = get_logger("vision", device)
    log.debug("find_all_matches(%s): %d raw hits, %d unique (threshold=%.0f%%)",
              image_name, len(points), len(unique), threshold * 100)
    return unique

# ============================================================
# INPUT FUNCTIONS
# ============================================================

def adb_tap(device, x, y):
    """Send a tap command via ADB."""
    t0 = time.time()
    try:
        subprocess.run([adb_path, "-s", device, "shell", "input", "tap", str(x), str(y)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=ADB_COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        get_logger("vision", device).warning("adb_tap timed out after %ds (ADB hung?)", ADB_COMMAND_TIMEOUT)
        stats.record_adb_timing(device, "tap", float(ADB_COMMAND_TIMEOUT), success=False)
        return
    elapsed = time.time() - t0
    stats.record_adb_timing(device, "tap", elapsed)
    if elapsed > 3.0:
        get_logger("vision", device).warning("adb_tap slow: %.2fs", elapsed)

def adb_swipe(device, x1, y1, x2, y2, duration_ms=300):
    """Send a swipe command via ADB."""
    t0 = time.time()
    try:
        subprocess.run([adb_path, "-s", device, "shell", "input", "swipe",
                        str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=ADB_COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        get_logger("vision", device).warning("adb_swipe timed out after %ds (ADB hung?)", ADB_COMMAND_TIMEOUT)
        stats.record_adb_timing(device, "swipe", float(ADB_COMMAND_TIMEOUT), success=False)
        return
    elapsed = time.time() - t0
    stats.record_adb_timing(device, "swipe", elapsed)
    if elapsed > 3.0:
        get_logger("vision", device).warning("adb_swipe slow: %.2fs", elapsed)

def adb_keyevent(device, keycode):
    """Send a key event via ADB (e.g. KEYCODE_BACK=4, KEYCODE_HOME=3)."""
    t0 = time.time()
    try:
        subprocess.run([adb_path, "-s", device, "shell", "input", "keyevent", str(keycode)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=ADB_COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        get_logger("vision", device).warning("adb_keyevent timed out after %ds", ADB_COMMAND_TIMEOUT)
        stats.record_adb_timing(device, "keyevent", float(ADB_COMMAND_TIMEOUT), success=False)
        return
    elapsed = time.time() - t0
    stats.record_adb_timing(device, "keyevent", elapsed)

def tap(button_name, device):
    """Tap a button by its name from the BUTTONS dictionary"""
    log = get_logger("vision", device)
    x = BUTTONS[button_name]["x"]
    y = BUTTONS[button_name]["y"]
    adb_tap(device, x, y)
    log.debug("Tapped %s at %d, %d", button_name, x, y)

# Templates that must ONLY match within their IMAGE_REGIONS region.
# No full-screen fallback — prevents false positives elsewhere on screen.
_REGION_STRICT = {
    "heal.png",
}

# Region constraints for templates that should only match in specific screen areas.
# Values are (x1, y1, x2, y2) defining the search region.
# Screen is 1080x1920 — lower-left quadrant = (0, 960, 540, 1920)
IMAGE_REGIONS = {
    # Existing
    "heal.png":                   (0, 960, 540, 1920),     # lower-left quadrant

    # Map screen buttons
    "bl_button.png":              (540, 0, 1080, 960),     # top-right quadrant

    # Rally screen
    "search.png":                 (0, 960, 1080, 1920),    # lower half
    "scroll_or_not.png":          (0, 960, 1080, 1920),    # lower half

    # Rally type icons (war screen list)
    "rally/castle.png":           (0, 0, 540, 1920),       # left half
    "rally/pass.png":             (0, 0, 540, 1920),       # left half
    "rally/tower.png":            (0, 0, 540, 1920),       # left half
    "rally/groot.png":            (0, 0, 540, 1920),       # left half
    "rally/join.png":             (540, 0, 1080, 1920),    # right half

    # Rally detail
    "slot.png":                   (0, 0, 360, 1920),       # left third

    # Quest screen
    "aq_claim.png":               (830, 640, 1069, 1920),  # tight x: 198w tpl @ x:949-950

    # Troop status icons (left-side list)
    "statuses/stationing.png":    (0, 0, 360, 1920),       # left third
    "statuses/battling.png":      (0, 0, 360, 1920),       # left third
    "statuses/marching.png":      (0, 0, 360, 1920),       # left third
    "statuses/returning.png":     (0, 0, 360, 1920),       # left third
    "stationed.png":              (0, 0, 360, 1920),       # left third
    "defending.png":              (0, 0, 360, 1920),       # left third

    # Tower popup
    "detail_button.png":          (0, 960, 540, 1920),        # lower-left quadrant

    # Deploy screen
    "depart.png":                 (0, 800, 1080, 1650),       # mid-to-lower; hits at y:873-1586

    # Back arrow (top-left corner)
    "back_arrow.png":             (0, 9, 145, 137),          # tight: 104x88 tpl @ fixed (73,73)
}

# Per-template tap offsets from center (dx, dy).
# Use when a UI element (e.g. chat bubble) overlaps the template center.
TAP_OFFSETS = {
    "depart.png":         (75, 0),    # slight right offset to dodge chat bubble overlay
    "mithril_depart.png": (75, 0),
}

# Templates eligible for dynamic region learning.
# After enough hits, the search region auto-narrows to observed positions + padding.
# Templates NOT in this set always use their static IMAGE_REGIONS entry (or full-screen).
DYNAMIC_REGION_TEMPLATES = {
    "rally_titan_select.png",
    "rally_eg_select.png",
    "search.png",
    "depart.png",
    "mithril_depart.png",
    "map_screen.png",
    "aq_claim.png",
}

# Minimum hits before trusting dynamic region (until then, full-screen search).
_DYNAMIC_MIN_HITS = 3
# Padding around the observed bounding box (pixels).  Enough to handle minor
# variation without eating into full-screen search cost.
_DYNAMIC_PADDING = 120


def get_dynamic_region(device, image_name):
    """Compute a search region from accumulated hit position data.

    Returns (x1, y1, x2, y2) clipped to screen bounds, or None if not enough
    data yet — caller should fall back to full-screen search.
    """
    if image_name not in DYNAMIC_REGION_TEMPLATES:
        return None
    bounds = stats.get_template_hit_bounds(device, image_name)
    if bounds is None:
        return None
    min_x, min_y, max_x, max_y, count = bounds
    if count < _DYNAMIC_MIN_HITS:
        return None

    # Get template size so the region encloses the full template, not just centers
    tpl = get_template(f"elements/{image_name}")
    if tpl is None:
        return None
    th, tw = tpl.shape[:2]
    half_w, half_h = tw // 2, th // 2

    # Build padded region around observed center positions
    x1 = max(0, min_x - half_w - _DYNAMIC_PADDING)
    y1 = max(0, min_y - half_h - _DYNAMIC_PADDING)
    x2 = min(1080, max_x + half_w + _DYNAMIC_PADDING)
    y2 = min(1920, max_y + half_h + _DYNAMIC_PADDING)

    # If a static region exists, take the union so we never go tighter than static
    static = IMAGE_REGIONS.get(image_name)
    if static:
        sx1, sy1, sx2, sy2 = static
        x1 = min(x1, sx1)
        y1 = min(y1, sy1)
        x2 = max(x2, sx2)
        y2 = max(y2, sy2)

    return (x1, y1, x2, y2)


def tap_image(image_name, device, threshold=0.8):
    """Find an image on screen and tap it"""
    log = get_logger("vision", device)
    screen = load_screenshot(device)
    # Dynamic region (learned from hits) > static IMAGE_REGIONS > full-screen
    region = get_dynamic_region(device, image_name) or IMAGE_REGIONS.get(image_name)
    match = find_image(screen, image_name, threshold=threshold, region=region, device=device)

    if match:
        max_val, max_loc, h, w = match
        center_x = max_loc[0] + w // 2
        center_y = max_loc[1] + h // 2
        dx, dy = TAP_OFFSETS.get(image_name, (0, 0))
        center_x += dx
        center_y += dy
        if screen is not None:
            _save_click_trail(screen, device, center_x, center_y, image_name.replace(".png", ""))
        adb_tap(device, center_x, center_y)
        log.debug("Tapped %s at (%d, %d), confidence %.0f%%", image_name, center_x, center_y, max_val * 100)
        return True
    else:
        best_val = get_last_best()
        log.debug("Couldn't find %s (best: %.0f%%, need: %.0f%%)", image_name, best_val * 100, threshold * 100)
        stats.record_template_miss(device, image_name, best_val)
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
    log = get_logger("vision", device)
    start_time = time.time()
    while time.time() - start_time < timeout:
        if tap_image(image_name, device, threshold=threshold):
            return True
        time.sleep(0.5)
    log.debug("Timed out waiting for %s after %ds", image_name, timeout)
    return False

def timed_wait(device, condition_fn, budget_s, label, stop_check=None):
    """Poll condition_fn every ~150ms; return as soon as it's True.

    If condition_fn is never True within budget_s, sleeps the full budget
    (same as time.sleep).  If condition_fn becomes True early, exits
    immediately — saving the remaining wait time.

    condition_fn: callable() -> bool  (called repeatedly until True or budget expires)
    stop_check: optional callable() -> bool; if True, abort immediately.
    Returns True if condition was met within budget, False otherwise.
    """
    start = time.time()
    while time.time() - start < budget_s:
        if stop_check and stop_check():
            return False
        if condition_fn():
            actual = time.time() - start
            stats.record_transition_time(device, label, actual, budget_s, True)
            return True
        time.sleep(0.15)
    # Condition never met within effective budget
    stats.record_transition_time(device, label, budget_s, budget_s, False)
    return False


def tap_tower_until_attack_menu(device, tower_x=540, tower_y=900, timeout=10):
    """Tap the tower repeatedly until the attack button menu appears"""
    log = get_logger("vision", device)
    start_time = time.time()
    attempt = 0
    while time.time() - start_time < timeout:
        attempt += 1
        log.debug("Tapping tower at (%d, %d), attempt %d...", tower_x, tower_y, attempt)
        adb_tap(device, tower_x, tower_y)
        time.sleep(1)
        if tap_image("attack_button.png", device):
            log.debug("Attack menu opened and attack button tapped!")
            return True
    log.debug("Timed out waiting for attack menu after %ds", timeout)
    return False
