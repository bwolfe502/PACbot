"""
Analyze EG boss screenshot to find dark priest hat positions by color matching.
Target color: #849EEF (RGB 132, 158, 239) -> BGR (239, 158, 132)

Step 1: Navigate to EG boss view using ADB
Step 2: Take screenshot
Step 3: Analyze colors
"""
import subprocess
import cv2
import numpy as np
import os
import sys
import time

ADB = r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe"
DEVICE = "emulator-5584"
DEBUG_DIR = "C:/Users/bwolf/Desktop/PACbot/debug"
os.makedirs(DEBUG_DIR, exist_ok=True)

def adb_cmd(*args):
    """Run an ADB command and return output."""
    cmd = [ADB, "-s", DEVICE] + list(args)
    return subprocess.run(cmd, capture_output=True, timeout=10)

def tap(x, y):
    """Tap at coordinates."""
    adb_cmd("shell", "input", "tap", str(x), str(y))
    time.sleep(0.5)

def take_screenshot(name="screenshot"):
    """Take a screenshot and return as cv2 image."""
    outpath = os.path.join(DEBUG_DIR, f"{name}.png")
    result = subprocess.run(
        [ADB, "-s", DEVICE, "exec-out", "screencap", "-p"],
        capture_output=True, timeout=10
    )
    with open(outpath, "wb") as f:
        f.write(result.stdout)
    img = cv2.imread(outpath)
    print(f"Screenshot saved: {outpath} ({img.shape[1]}x{img.shape[0]})" if img is not None else f"FAILED to read {outpath}")
    return img, outpath

def navigate_to_eg():
    """Navigate to the EG boss map view."""
    print("=== Navigating to EG boss view ===")

    # Tap SEARCH button (bottom-right area)
    print("Tapping Search button...")
    tap(700, 1800)
    time.sleep(1.5)

    # Tap Rally tab
    print("Tapping Rally tab...")
    tap(850, 560)
    time.sleep(1)

    # Tap EG select (use approximate position for the EG icon in rally search)
    # We'll try to find it by tapping in the general area
    print("Tapping EG select...")
    # Look for rally_eg_select.png position
    tap(540, 960)
    time.sleep(1)

    # Tap Search
    print("Tapping Search...")
    tap(540, 1340)
    time.sleep(2)

    # Tap on the EG boss in center
    print("Tapping EG boss at center (540, 665)...")
    tap(540, 665)
    time.sleep(2)

def analyze_screenshot(img, name_prefix=""):
    """Full color analysis on an image."""
    if img is None:
        print("ERROR: No image to analyze")
        return

    h, w = img.shape[:2]
    print(f"\nImage size: {w}x{h}")

    # Target color in BGR: (239, 158, 132)
    target_bgr = np.array([239, 158, 132], dtype=np.uint8)

    print(f"Target BGR: {target_bgr} (from #849EEF)")

    results = {}

    for tol_name, tolerance in [("tight_15", 15), ("medium_25", 25), ("wide_35", 35), ("wider_45", 45)]:
        lower = np.clip(target_bgr.astype(int) - int(tol_name.split("_")[1]), 0, 255).astype(np.uint8)
        upper = np.clip(target_bgr.astype(int) + int(tol_name.split("_")[1]), 0, 255).astype(np.uint8)

        mask = cv2.inRange(img, lower, upper)
        match_count = cv2.countNonZero(mask)

        # Morphological cleanup
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask_clean = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        mask_clean = cv2.morphologyEx(mask_clean, cv2.MORPH_OPEN, kernel, iterations=1)

        cv2.imwrite(os.path.join(DEBUG_DIR, f"{name_prefix}mask_{tol_name}.png"), mask_clean)

        contours, _ = cv2.findContours(mask_clean, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        clusters = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < 15:
                continue
            M = cv2.moments(cnt)
            if M["m00"] == 0:
                continue
            cx = int(M["m10"] / M["m00"])
            cy = int(M["m01"] / M["m00"])
            x, y, bw, bh = cv2.boundingRect(cnt)
            clusters.append({
                'center': (cx, cy),
                'area': area,
                'bbox': (x, y, bw, bh),
                'contour': cnt
            })

        clusters.sort(key=lambda c: c['area'], reverse=True)

        print(f"\n--- Tolerance: {tol_name} ---")
        print(f"  Matching pixels: {match_count}")
        print(f"  Clusters (area>=15): {len(clusters)}")

        # Group nearby clusters
        groups = []
        used = set()
        for i, cl in enumerate(clusters):
            if i in used:
                continue
            group = [cl]
            used.add(i)
            for j, cl2 in enumerate(clusters):
                if j in used:
                    continue
                dist = np.sqrt((cl['center'][0] - cl2['center'][0])**2 +
                               (cl['center'][1] - cl2['center'][1])**2)
                if dist < 60:
                    group.append(cl2)
                    used.add(j)
            total_area = sum(c['area'] for c in group)
            gcx = int(sum(c['center'][0] * c['area'] for c in group) / total_area)
            gcy = int(sum(c['center'][1] * c['area'] for c in group) / total_area)
            groups.append({
                'center': (gcx, gcy),
                'total_area': total_area,
                'num_clusters': len(group),
            })

        groups.sort(key=lambda g: g['total_area'], reverse=True)

        print(f"  Grouped clusters: {len(groups)}")
        for i, g in enumerate(groups[:15]):
            print(f"    Group {i+1}: center=({g['center'][0]}, {g['center'][1]}), "
                  f"area={g['total_area']}, sub={g['num_clusters']}")

        results[tol_name] = {
            'mask': mask_clean,
            'clusters': clusters,
            'groups': groups,
            'match_count': match_count
        }

        # Draw result image
        result_img = img.copy()
        overlay = result_img.copy()
        overlay[mask_clean > 0] = [0, 255, 0]
        result_img = cv2.addWeighted(overlay, 0.4, result_img, 0.6, 0)

        for i, g in enumerate(groups[:15]):
            gcx, gcy = g['center']
            cv2.circle(result_img, (gcx, gcy), 15, (0, 0, 255), 3)
            cv2.putText(result_img, f"G{i+1}({gcx},{gcy}) a={g['total_area']}",
                       (gcx+20, gcy-10),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 2)

        cv2.imwrite(os.path.join(DEBUG_DIR, f"{name_prefix}detected_{tol_name}.png"), result_img)

    # Also draw existing EG_PRIEST_POSITIONS for comparison
    EG_PRIEST_POSITIONS = [
        (540, 665),   # P1: EG boss tap
        (172, 895),   # P2: left-center
        (259, 1213),  # P3: lower-left
        (817, 1213),  # P4: lower-right
        (929, 919),   # P5: right-center
        (540, 913),   # P6: center (final attack)
    ]

    compare_img = img.copy()
    for i, (px, py) in enumerate(EG_PRIEST_POSITIONS):
        label = f"P{i+1}"
        color = (0, 0, 255) if i == 0 else (255, 0, 0) if i == 5 else (0, 255, 0)
        cv2.circle(compare_img, (px, py), 20, color, 3)
        cv2.putText(compare_img, f"{label}({px},{py})", (px+25, py-5),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

    # Also overlay the best mask (medium_25)
    best_mask = results["medium_25"]["mask"]
    overlay2 = compare_img.copy()
    overlay2[best_mask > 0] = [0, 255, 255]  # Yellow for target color
    compare_img = cv2.addWeighted(overlay2, 0.3, compare_img, 0.7, 0)

    cv2.imwrite(os.path.join(DEBUG_DIR, f"{name_prefix}comparison.png"), compare_img)

    # Sample colors at existing positions
    print("\n" + "=" * 70)
    print("COLOR SAMPLING AT EXISTING EG_PRIEST_POSITIONS:")
    print("=" * 70)

    for i, (px, py) in enumerate(EG_PRIEST_POSITIONS):
        if 0 <= py-15 and py+15 < h and 0 <= px-15 and px+15 < w:
            region = img[py-15:py+15, px-15:px+15]
            avg_bgr = region.mean(axis=(0,1))
            print(f"  P{i+1} ({px},{py}): avg BGR = ({avg_bgr[0]:.0f}, {avg_bgr[1]:.0f}, {avg_bgr[2]:.0f})")

            # Count target-color pixels in 60x60 around position
            region60 = img[max(0,py-30):min(h,py+30), max(0,px-30):min(w,px+30)]
            target_lower = np.clip(target_bgr.astype(int) - 25, 0, 255).astype(np.uint8)
            target_upper = np.clip(target_bgr.astype(int) + 25, 0, 255).astype(np.uint8)
            local_mask = cv2.inRange(region60, target_lower, target_upper)
            local_count = cv2.countNonZero(local_mask)
            print(f"       -> {local_count} target-color pixels in 60x60 region")
        else:
            print(f"  P{i+1} ({px},{py}): OUT OF BOUNDS")

    # Look for "1" labels (dark rounded rectangles)
    print("\n" + "=" * 70)
    print("LOOKING FOR '1' LABEL BADGES:")
    print("=" * 70)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # Focus on game area (skip UI)
    game_top, game_bot = 400, 1400
    dark_mask = cv2.inRange(gray[game_top:game_bot, :], 20, 70)

    kernel2 = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    dark_mask = cv2.morphologyEx(dark_mask, cv2.MORPH_CLOSE, kernel2, iterations=2)

    dark_contours, _ = cv2.findContours(dark_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    badge_img = img.copy()
    badges = []
    for cnt in dark_contours:
        area = cv2.contourArea(cnt)
        if 200 < area < 5000:
            x, y, bw, bh = cv2.boundingRect(cnt)
            aspect = bw / max(bh, 1)
            if 0.4 < aspect < 3.5:
                M = cv2.moments(cnt)
                if M["m00"] > 0:
                    cx = int(M["m10"] / M["m00"])
                    cy = int(M["m01"] / M["m00"]) + game_top
                    badges.append({'center': (cx, cy), 'area': area, 'bbox': (x, y+game_top, bw, bh)})
                    cv2.rectangle(badge_img, (x, y+game_top), (x+bw, y+game_top+bh), (0, 255, 255), 2)
                    cv2.putText(badge_img, f"a={area}", (x, y+game_top-5),
                               cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 255, 255), 1)

    badges.sort(key=lambda b: b['center'][1])
    print(f"Badge candidates: {len(badges)}")
    for i, b in enumerate(badges[:20]):
        print(f"  Badge {i+1}: center={b['center']}, area={b['area']}, bbox={b['bbox']}")

    cv2.imwrite(os.path.join(DEBUG_DIR, f"{name_prefix}badges.png"), badge_img)

    # HSV analysis for the hat color
    print("\n" + "=" * 70)
    print("HSV ANALYSIS:")
    print("=" * 70)

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    target_pixel = np.uint8([[target_bgr]])
    target_hsv = cv2.cvtColor(target_pixel, cv2.COLOR_BGR2HSV)[0][0]
    print(f"Target HSV: H={target_hsv[0]}, S={target_hsv[1]}, V={target_hsv[2]}")

    # HSV-based mask (might be more robust)
    h_tol, s_tol, v_tol = 10, 40, 40
    hsv_lower = np.array([max(0, target_hsv[0]-h_tol), max(0, target_hsv[1]-s_tol), max(0, target_hsv[2]-v_tol)])
    hsv_upper = np.array([min(179, target_hsv[0]+h_tol), min(255, target_hsv[1]+s_tol), min(255, target_hsv[2]+v_tol)])
    hsv_mask = cv2.inRange(hsv, hsv_lower, hsv_upper)
    hsv_count = cv2.countNonZero(hsv_mask)
    print(f"HSV matching pixels: {hsv_count}")

    hsv_mask_clean = cv2.morphologyEx(hsv_mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    hsv_mask_clean = cv2.morphologyEx(hsv_mask_clean, cv2.MORPH_OPEN, kernel, iterations=1)
    cv2.imwrite(os.path.join(DEBUG_DIR, f"{name_prefix}mask_hsv.png"), hsv_mask_clean)

    hsv_contours, _ = cv2.findContours(hsv_mask_clean, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    hsv_clusters = []
    for cnt in hsv_contours:
        area = cv2.contourArea(cnt)
        if area < 15:
            continue
        M = cv2.moments(cnt)
        if M["m00"] == 0:
            continue
        cx = int(M["m10"] / M["m00"])
        cy = int(M["m01"] / M["m00"])
        hsv_clusters.append({'center': (cx, cy), 'area': area})

    hsv_clusters.sort(key=lambda c: c['area'], reverse=True)
    print(f"HSV clusters (area>=15): {len(hsv_clusters)}")
    for i, cl in enumerate(hsv_clusters[:15]):
        print(f"  HSV Cluster {i+1}: center=({cl['center'][0]}, {cl['center'][1]}), area={cl['area']}")

    return results

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    # Check if we can use an existing screenshot passed as argument
    if len(sys.argv) > 1:
        img_path = sys.argv[1]
        print(f"Using provided screenshot: {img_path}")
        img = cv2.imread(img_path)
        if img is None:
            print(f"ERROR: Could not load {img_path}")
            sys.exit(1)
        analyze_screenshot(img, "provided_")
    else:
        # Take a fresh screenshot from the emulator
        print("Taking fresh screenshot from emulator...")
        img, path = take_screenshot("eg_analysis_raw")
        if img is not None:
            print(f"\nAnalyzing current screen...")
            analyze_screenshot(img, "current_")
        else:
            print("ERROR: Could not take screenshot")
            sys.exit(1)

    print("\n" + "=" * 70)
    print("ALL DEBUG IMAGES SAVED TO:", DEBUG_DIR)
    print("=" * 70)
