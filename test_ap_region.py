"""Test the read_ap OCR against the saved screenshot."""
import cv2
import re
from vision import read_text, _AP_REGION

screen = cv2.imread("screenshot_emulator-5554.png")
raw = read_text(screen, region=_AP_REGION, allowlist="0123456789/")
match = re.search(r"(\d+)/(\d+)", raw)
if match:
    print(f"AP: {match.group(1)}/{match.group(2)}")
else:
    print(f"No AP found (raw: {repr(raw)})")
