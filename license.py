"""
PACbot License Validation
Validates license keys against a published Google Sheet (CSV).
Stores the key locally so the user only enters it once.
"""

import os
import sys
import csv
import io

# The local file where the license key is saved after first entry
LICENSE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".license_key")

# ============================================================
# IMPORTANT: Set this to your published Google Sheet CSV URL
#
# To get this URL:
#   1. Open your Google Sheet
#   2. File → Share → Publish to web
#   3. Select "Sheet1" and "Comma-separated values (.csv)"
#   4. Click Publish and copy the URL
# ============================================================
SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTbryJkuOUqeLJmppNLuBpeBcTIKR7xtSlD2EJH1cQ0yO7av6eYN9iw7Bfa55bw5FduUQhBY0MO7Jl7/pub?output=csv"


def _load_saved_key():
    """Load the locally saved license key, if any."""
    if os.path.isfile(LICENSE_FILE):
        with open(LICENSE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return None


def _save_key(key):
    """Save the license key locally so user doesn't have to re-enter it."""
    with open(LICENSE_FILE, "w", encoding="utf-8") as f:
        f.write(key.strip())


def _fetch_valid_keys():
    """
    Fetch the list of active license keys from the published Google Sheet.
    Returns a dict of {key: user_name} for active keys.
    """
    import requests

    if not SHEET_CSV_URL:
        print("ERROR: License sheet URL not configured.")
        print("Please set SHEET_CSV_URL in license.py")
        sys.exit(1)

    try:
        resp = requests.get(SHEET_CSV_URL, timeout=10)
        resp.raise_for_status()
    except requests.ConnectionError:
        print("ERROR: Cannot connect to the internet to validate license.")
        print("Please check your internet connection and try again.")
        sys.exit(1)
    except requests.RequestException as e:
        print(f"ERROR: Failed to validate license: {e}")
        sys.exit(1)

    valid_keys = {}
    reader = csv.reader(io.StringIO(resp.text))

    # Skip header row
    try:
        next(reader)
    except StopIteration:
        return valid_keys

    for row in reader:
        if len(row) >= 3:
            key = row[0].strip()
            user = row[1].strip()
            active = row[2].strip().upper()
            if active in ("TRUE", "YES", "1"):
                valid_keys[key] = user

    return valid_keys


def validate_license():
    """
    Main license validation flow.
    Returns the username associated with the key if valid.
    Exits the program if invalid.
    """
    saved_key = _load_saved_key()

    if saved_key:
        # Silently validate the saved key
        print("Validating license...", end=" ", flush=True)
        valid_keys = _fetch_valid_keys()

        if saved_key in valid_keys:
            user = valid_keys[saved_key]
            print(f"OK. Welcome back, {user}!")
            return user
        else:
            print("FAILED.")
            print("Your license key has been revoked or is no longer valid.")
            # Remove the invalid saved key
            try:
                os.remove(LICENSE_FILE)
            except OSError:
                pass
            # Fall through to prompt for a new key

    # No saved key (or it was invalid) — prompt the user
    print()
    print("=" * 40)
    print("  PACbot License Required")
    print("=" * 40)
    print()

    attempts = 3
    for attempt in range(attempts):
        key = input("Enter your license key: ").strip()

        if not key:
            print("No key entered.\n")
            continue

        print("Validating...", end=" ", flush=True)
        valid_keys = _fetch_valid_keys()

        if key in valid_keys:
            user = valid_keys[key]
            _save_key(key)
            print(f"OK. Welcome, {user}!")
            return user
        else:
            remaining = attempts - attempt - 1
            if remaining > 0:
                print(f"INVALID. {remaining} attempt(s) remaining.\n")
            else:
                print("INVALID.")

    print("\nToo many failed attempts. Exiting.")
    sys.exit(1)
