#!/bin/bash
# Start the Android emulator and wait for it to boot.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "Starting Android emulator..."
docker compose up -d

echo "Waiting for emulator to boot (this may take 1-3 minutes)..."
TIMEOUT=180
ELAPSED=0

while [ $ELAPSED -lt $TIMEOUT ]; do
    # Try connecting
    adb connect 127.0.0.1:5555 > /dev/null 2>&1 || true

    # Check if booted
    BOOT=$(adb -s 127.0.0.1:5555 shell getprop sys.boot_completed 2>/dev/null | tr -d '\r\n' || echo "")
    if [ "$BOOT" = "1" ]; then
        echo ""
        echo "================================"
        echo "  Emulator is ready!"
        echo "================================"
        echo ""
        echo "  ADB device:  127.0.0.1:5555"
        echo "  Game viewer:  http://localhost:6080"
        echo ""
        echo "  Install game: bash scripts/install-apk.sh /path/to/game.apk"
        echo ""
        exit 0
    fi

    sleep 5
    ELAPSED=$((ELAPSED + 5))
    echo "  Still booting... (${ELAPSED}s)"
done

echo ""
echo "ERROR: Emulator did not boot within ${TIMEOUT}s."
echo "Check logs: docker compose logs emulator"
exit 1
