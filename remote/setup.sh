#!/bin/bash
# 9Bot Remote Environment — One-Time Setup
# Run on a fresh Linux server: sudo bash setup.sh
set -e

echo "=============================="
echo "  9Bot Remote Setup"
echo "=============================="
echo ""

# Must run as root (or sudo)
if [ "$EUID" -ne 0 ]; then
    echo "Please run with sudo: sudo bash setup.sh"
    exit 1
fi

REAL_USER="${SUDO_USER:-$USER}"

# ── 1. Docker ──
echo "[1/5] Checking Docker..."
if ! command -v docker &> /dev/null; then
    echo "  Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    usermod -aG docker "$REAL_USER"
    echo "  Docker installed."
else
    echo "  Docker already installed: $(docker --version)"
fi

# ── 2. Docker Compose ──
echo "[2/5] Checking Docker Compose..."
if ! docker compose version &> /dev/null 2>&1; then
    echo "  Installing Docker Compose plugin..."
    apt-get update -qq && apt-get install -y -qq docker-compose-plugin
    echo "  Docker Compose installed."
else
    echo "  Docker Compose already installed."
fi

# ── 3. KVM ──
echo "[3/5] Checking KVM support..."
if [ -e /dev/kvm ]; then
    echo "  KVM is available — emulator will use hardware acceleration."
else
    echo "  WARNING: /dev/kvm not found."
    echo "  The emulator will use software rendering (slower but works)."
    echo ""
    echo "  For KVM support:"
    echo "    - CPU must support virtualization (Intel VT-x / AMD-V)"
    echo "    - Virtualization must be enabled in BIOS"
    echo "    - Run: sudo modprobe kvm kvm_intel  (or kvm_amd)"
    echo ""
fi

# ── 4. Python ──
echo "[4/5] Checking Python..."
if command -v python3 &> /dev/null; then
    PY=$(python3 --version)
    echo "  Python found: $PY"
else
    echo "  Installing Python 3..."
    apt-get update -qq && apt-get install -y -qq python3 python3-pip python3-venv
    echo "  Python installed."
fi

# ── 5. ADB ──
echo "[5/5] Checking ADB..."
if command -v adb &> /dev/null; then
    echo "  ADB found: $(adb version | head -1)"
else
    echo "  Installing ADB..."
    apt-get update -qq && apt-get install -y -qq android-tools-adb 2>/dev/null || {
        echo "  Package not found, downloading from Google..."
        cd /tmp
        curl -sL https://dl.google.com/android/repository/platform-tools-latest-linux.zip -o pt.zip
        unzip -qo pt.zip
        cp platform-tools/adb /usr/local/bin/
        chmod +x /usr/local/bin/adb
        rm -rf pt.zip platform-tools
    }
    echo "  ADB installed."
fi

# ── Create 9Bot venv ──
echo ""
echo "Setting up 9Bot Python environment..."
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo "  Virtual environment created."
fi

source .venv/bin/activate

# Install core deps + flask + easyocr for Linux
pip install -q --upgrade pip
pip install -q -r requirements.txt 2>/dev/null || true
pip install -q flask easyocr
echo "  Python packages installed."

echo ""
echo "=============================="
echo "  Setup complete!"
echo "=============================="
echo ""
echo "Next steps:"
echo "  1. cd remote/"
echo "  2. docker compose up -d         (start emulator)"
echo "  3. bash start.sh                (wait for boot + connect ADB)"
echo "  4. bash scripts/install-apk.sh /path/to/game.apk"
echo "  5. cd .. && source .venv/bin/activate"
echo "  6. python main.py               (or edit settings.json: web_dashboard=true)"
echo ""
echo "View emulator: http://localhost:6080"
echo ""
