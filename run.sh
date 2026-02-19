#!/bin/bash
# Always run from this script's directory
cd "$(dirname "$0")"

echo "============================"
echo "PACbot - Setup + Run"
echo "============================"

# Check Python
if ! command -v python3 &> /dev/null; then
    echo ""
    echo "ERROR: python3 not found."
    echo "Install Python 3: https://python.org (or 'brew install python3' on macOS)"
    exit 1
fi

# Create venv if missing
if [ ! -f ".venv/bin/python" ]; then
    echo ""
    echo "Creating virtual environment..."
    python3 -m venv .venv
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to create venv."
        exit 1
    fi
fi

echo ""
echo "Activating venv..."
source .venv/bin/activate

echo ""
echo "Installing requirements..."
python -m pip install --upgrade pip -q
python -m pip install -r requirements.txt -q
if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Failed to install requirements."
    exit 1
fi

echo ""
echo "Running PACbot..."
python main.py

echo ""
echo "PACbot exited."
