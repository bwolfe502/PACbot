#!/bin/bash
# Always run from this script's directory
cd "$(dirname "$0")"

# UTF-8 encoding for Python (prevents Unicode crashes)
export PYTHONIOENCODING=utf-8

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

# Install SSL certificates if missing (macOS python.org installer doesn't include them)
python3 -c "import ssl; ssl.create_default_context()" 2>/dev/null
if [ $? -ne 0 ]; then
    echo ""
    echo "Installing SSL certificates for Python..."
    PYTHON_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    CERT_SCRIPT="/Applications/Python ${PYTHON_VER}/Install Certificates.command"
    if [ -f "$CERT_SCRIPT" ]; then
        "$CERT_SCRIPT"
    else
        # Fallback: install certifi manually and link it
        python3 -m pip install --upgrade certifi -q
        CERT_PATH=$(python3 -c "import certifi; print(certifi.where())")
        SSL_DIR=$(python3 -c "import ssl; print(ssl.get_default_verify_paths().openssl_capath)")
        if [ -n "$SSL_DIR" ] && [ -n "$CERT_PATH" ]; then
            sudo mkdir -p "$SSL_DIR"
            sudo ln -sf "$CERT_PATH" "$SSL_DIR/cert.pem"
        fi
    fi
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

# Check if first-time setup (easyocr not installed yet)
FIRST_RUN=0
python -c "import easyocr" > /dev/null 2>&1
if [ $? -ne 0 ]; then
    FIRST_RUN=1
fi

if [ "$FIRST_RUN" -eq 1 ]; then
    echo ""
    echo "First-time setup: downloading OCR engine."
    echo "This only happens once and may take a few minutes."
    echo ""
    python -m pip install --upgrade pip -q
    python -m pip install -r requirements.txt
    if [ $? -ne 0 ]; then
        echo ""
        echo "ERROR: Failed to install requirements."
        exit 1
    fi
else
    # Only install if requirements.txt changed since last install
    NEEDS_INSTALL=0
    if [ ! -f ".venv/.req_hash" ]; then
        NEEDS_INSTALL=1
    else
        NEW_HASH=$(md5 -q requirements.txt 2>/dev/null || md5sum requirements.txt | awk '{print $1}')
        OLD_HASH=$(cat .venv/.req_hash 2>/dev/null)
        if [ "$NEW_HASH" != "$OLD_HASH" ]; then
            NEEDS_INSTALL=1
        fi
    fi

    if [ "$NEEDS_INSTALL" -eq 1 ]; then
        echo "Installing requirements..."
        python -m pip install --upgrade pip -q
        python -m pip install -r requirements.txt -q
        if [ $? -ne 0 ]; then
            echo ""
            echo "ERROR: Failed to install requirements."
            exit 1
        fi
        (md5 -q requirements.txt 2>/dev/null || md5sum requirements.txt | awk '{print $1}') > .venv/.req_hash
    fi
fi
echo "Done!"

echo ""
python updater.py

echo ""
python main.py
if [ $? -ne 0 ]; then
    echo ""
    echo "=========================================="
    echo "PACbot crashed! See error message above."
    echo "=========================================="
    read -p "Press Enter to close..."
    exit 1
fi

# Close the Terminal window on clean exit
osascript -e 'tell application "Terminal" to close front window' 2>/dev/null &
exit 0
