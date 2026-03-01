"""
PACbot License Validation
Validates license keys against a published Google Sheet (CSV).
Keys are bound to the machine hardware so they can't be shared.
"""

import os
import sys
import csv
import io
import base64
import hashlib
import hmac
import platform
import uuid

# The local file where the license key is saved after first entry
LICENSE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".license_key")

# Sheet URL (obfuscated — not plain text in source)
_SHEET_DATA = "aHR0cHM6Ly9kb2NzLmdvb2dsZS5jb20vc3ByZWFkc2hlZXRzL2QvZS8yUEFDWC0xdlRicnlKa3VPVXFlTEptcHBOTHVCcGVCY1RJS1I3eHRTbEQyRUpIMWNRMHlPN2F2NmVZTjlpdzdCZmE1NWJ3NUZkdVVRaEJZME1PN0psNy9wdWI/b3V0cHV0PWNzdg=="

def _get_sheet_url():
    return base64.b64decode(_SHEET_DATA).decode()


# ============================================================
# MACHINE FINGERPRINT
# ============================================================

def _get_machine_id():
    """
    Generate a stable machine fingerprint from hardware identifiers.
    This ties a license key to the specific machine it was activated on.
    """
    parts = []

    # MAC address (primary hardware identifier)
    mac = uuid.getnode()
    # uuid.getnode() returns a random value if it can't find a real MAC;
    # random MACs have bit 0 of the first octet set to 1
    if (mac >> 40) & 1:
        # Fallback: no real MAC found, use hostname + platform
        parts.append(platform.node())
        parts.append(platform.machine())
        parts.append(platform.processor())
    else:
        parts.append(format(mac, '012x'))

    # Add hostname for extra uniqueness
    parts.append(platform.node())

    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()


# ============================================================
# KEY STORAGE (machine-bound)
# ============================================================

def _load_saved_key():
    """
    Load the locally saved license key, if any.
    Verifies the key was saved on THIS machine by checking the HMAC.
    Returns the key string if valid, None otherwise.
    """
    if not os.path.isfile(LICENSE_FILE):
        return None

    try:
        with open(LICENSE_FILE, "r", encoding="utf-8") as f:
            data = f.read().strip()

        # Format: key:hmac_hex
        if ":" not in data:
            # Old format (plain key) — force re-activation
            return None

        key, stored_mac = data.split(":", 1)
        expected_mac = _compute_key_mac(key)

        if hmac.compare_digest(stored_mac, expected_mac):
            return key
        else:
            # HMAC mismatch — file was copied from another machine
            return None
    except Exception:
        return None


def _save_key(key):
    """Save the license key bound to this machine's hardware ID."""
    mac = _compute_key_mac(key)
    with open(LICENSE_FILE, "w", encoding="utf-8") as f:
        f.write(f"{key}:{mac}")


def _compute_key_mac(key):
    """Compute an HMAC of the key using the machine ID as the secret."""
    machine_id = _get_machine_id()
    return hmac.new(
        machine_id.encode(),
        key.encode(),
        hashlib.sha256
    ).hexdigest()


def get_license_key():
    """Return the saved license key, or None if not activated."""
    return _load_saved_key()


# ============================================================
# REMOTE VALIDATION
# ============================================================

def _fetch_valid_keys():
    """
    Fetch the list of active license keys from the published Google Sheet.
    Returns a dict of {key: user_name} for active keys.
    """
    import requests

    import time as _time

    url = _get_sheet_url()

    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            break
        except requests.ConnectionError:
            if attempt < 2:
                print(f"Connection failed, retrying ({attempt + 1}/3)...")
                _time.sleep(3)
                continue
            print("ERROR: Cannot connect to the internet to validate license.")
            print("Please check your internet connection and try again.")
            sys.exit(1)
        except requests.RequestException as e:
            if attempt < 2:
                print(f"Request failed ({e}), retrying ({attempt + 1}/3)...")
                _time.sleep(3)
                continue
            print(f"ERROR: Failed to validate license after 3 attempts: {e}")
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


# ============================================================
# MAIN VALIDATION FLOW
# ============================================================

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
    print("Don't have a key? Message Nine on Discord.")
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
