"""Navigate from war screen to EG boss map view."""
import subprocess
import cv2
import os
import time
import numpy as np

ADB = r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe"
DEVICE = "emulator-5584"
DEBUG_DIR = "C:/Users/bwolf/Desktop/PACbot/debug"
ELEMENTS_DIR = "C:/Users/bwolf/Desktop/PACbot/elements"

def adb(*args):
    return subprocess.run([ADB, "-s", DEVICE] + list(args), capture_output=True, timeout=10)

def tap(x, y, label=""):
    print(f"  tap({x}, {y}) {label}")
    adb("shell", "input", "tap", str(x), str(y))

def ss(name):
    outpath = os.path.join(DEBUG_DIR, f"{name}.png")
    r = subprocess.run([ADB, "-s", DEVICE, "exec-out", "screencap", "-p"], capture_output=True, timeout=10)
    with open(outpath, "wb") as f:
        f.write(r.stdout)
    img = cv2.imread(outpath)
    if img is not None:
        print(f"  [{name}] {img.shape[1]}x{img.shape[0]}")
    return img

def find_tmpl(screen, name, thresh=0.65):
    tmpl = cv2.imread(os.path.join(ELEMENTS_DIR, name))
    if tmpl is None or screen is None:
        return None
    result = cv2.matchTemplate(screen, tmpl, cv2.TM_CCOEFF_NORMED)
    _, mv, _, ml = cv2.minMaxLoc(result)
    if mv >= thresh:
        return (ml[0] + tmpl.shape[1]//2, ml[1] + tmpl.shape[0]//2, mv)
    return None

# Currently on war_screen with VICTORY banner
# Bottom nav shows 5 icons at y~1880
# Let me check: from navigation.py, war_screen exits via tap at specific coords
# Looking at navigation.py: war_screen -> map_screen: adb_tap(device, 990, 1850)
# That's the top-right area of bottom nav? No, 990 is quite far right.
# Actually looking at the code more carefully:

# From navigation.py line 224-228:
# if current == "td_screen" and target_screen != "map_screen":
#     adb_tap(device, 990, 1850)  # This goes somewhere from td_screen
# From line 236-240 (typically):
# war_screen -> map_screen path

# Let me just read the navigate function for map_screen transitions
# For now, let me try: the globe icon at far right of bottom nav

print("=== Step 0: Current state (war_screen with VICTORY) ===")
img = ss("step0")

# Tap the victory banner to dismiss it
print("\n=== Step 1: Dismiss victory ===")
tap(420, 380, "victory_tap")
time.sleep(2)
img = ss("step1_after_victory")

# Check close_x
match = find_tmpl(img, "close_x.png", 0.7)
if match:
    tap(match[0], match[1], "close_x")
    time.sleep(1)
    img = ss("step1b")

# Now try tapping bottom-right globe icon to get to map
print("\n=== Step 2: Go to world map (globe icon bottom-right) ===")
# The bottom nav icons at y~1880:
# 1. Castle (~65)  2. Helmet (~200)  3. Swords(~340)/TRAIN(~420)  4. Shield (~610)  5. Globe(~770)
tap(770, 1880, "globe_icon")
time.sleep(2.5)
img = ss("step2_after_globe")

# Check if on map
match = find_tmpl(img, "map_screen.png", 0.7)
if match:
    print("  On map screen!")
else:
    print("  Not on map screen yet, checking what we have...")
    # Maybe we need to look at what screen this is
    # Check if there's a SEARCH button visible (indicator of map screen)

# Let me check if we see the SEARCH text at bottom
# The map_screen template might need a lower threshold
match = find_tmpl(img, "map_screen.png", 0.5)
print(f"  map_screen best match: {match}")

# Try pressing back
print("\n=== Step 3: Try back/escape ===")
adb("shell", "input", "keyevent", "4")
time.sleep(2)
img = ss("step3_after_back")

match = find_tmpl(img, "map_screen.png", 0.5)
print(f"  map_screen match: {match}")

# Check all screen templates
print("\n=== Checking all screen templates ===")
for tname in ["map_screen", "bl_screen", "aq_screen", "td_screen", "territory_screen", "war_screen", "profile_screen", "alliance_screen"]:
    tmpl = cv2.imread(os.path.join(ELEMENTS_DIR, f"{tname}.png"))
    if tmpl is None:
        continue
    result = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED)
    _, mv, _, ml = cv2.minMaxLoc(result)
    print(f"  {tname}: {mv:.3f} at {ml}")

# Try different approach: look at what the map_screen template looks like
print("\n=== Checking map_screen template ===")
tmpl = cv2.imread(os.path.join(ELEMENTS_DIR, "map_screen.png"))
if tmpl is not None:
    print(f"  Template size: {tmpl.shape[1]}x{tmpl.shape[0]}")
    # Save it for visual inspection
    cv2.imwrite(os.path.join(DEBUG_DIR, "template_map_screen.png"), tmpl)

print("\nDone - check screenshots")
