#!/bin/bash
# Stop the Android emulator.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "Stopping emulator..."
docker compose down
adb disconnect 127.0.0.1:5555 2>/dev/null || true
echo "Emulator stopped."
