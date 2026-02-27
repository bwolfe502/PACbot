#!/bin/bash
# Install an APK into the running emulator.
# Usage: bash install-apk.sh /path/to/game.apk

APK_PATH="$1"

if [ -z "$APK_PATH" ] || [ ! -f "$APK_PATH" ]; then
    echo "Usage: bash install-apk.sh /path/to/game.apk"
    exit 1
fi

DEVICE="127.0.0.1:5555"

echo "Connecting to emulator..."
adb connect "$DEVICE" > /dev/null 2>&1

echo "Installing: $APK_PATH"
echo "(This may take a few minutes for large APKs)"
adb -s "$DEVICE" install -r "$APK_PATH"

echo ""
echo "Done! The app should now be installed on the emulator."
echo "View it at: http://localhost:6080"
