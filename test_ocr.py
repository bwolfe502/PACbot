"""Quick test to verify OCR is working on a saved screenshot."""
import cv2
from vision import read_text, read_number

screen = cv2.imread("screenshot_emulator-5554.png")
if screen is None:
    print("ERROR: Could not load screenshot_emulator-5554.png")
    exit(1)

h, w = screen.shape[:2]
print(f"Screenshot loaded: {w}x{h}")
print()

# Tip: use generous regions (not too tight) for best results.
# Full-screen works well too - EasyOCR detects text regions automatically.
tests = [
    ("Top bar power",     (40, 0, 200, 60)),
    ("Diamond count",     (580, 0, 800, 60)),
    ("Full screen scan",  None),
]

for label, region in tests:
    result = read_text(screen, region=region)
    print(f"  {label:25s} => {repr(result)}")

print()
print("--- Number reading test ---")
power = read_number(screen, region=(40, 0, 200, 60))
print(f"  Power as int: {power}")

print()
print("OCR test complete!")
