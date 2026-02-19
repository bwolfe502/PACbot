"""
PACbot Key Generator
Run this script to generate license keys for your users.
Copy the key and paste it into your Google Sheet.

Usage:
    python keygen.py          → Generate 1 key
    python keygen.py 5        → Generate 5 keys
"""

import secrets
import sys


def generate_key():
    """Generate a random license key in XXXX-XXXX-XXXX-XXXX format."""
    parts = []
    for _ in range(4):
        # Generate 4 random hex characters (uppercase)
        part = secrets.token_hex(2).upper()
        parts.append(part)
    return "-".join(parts)


if __name__ == "__main__":
    count = 1
    if len(sys.argv) > 1:
        try:
            count = int(sys.argv[1])
        except ValueError:
            print(f"Usage: python keygen.py [number_of_keys]")
            sys.exit(1)

    print(f"\nGenerated {count} license key(s):\n")
    print("-" * 30)
    for i in range(count):
        key = generate_key()
        print(f"  {key}")
    print("-" * 30)
    print(f"\nCopy these into your Google Sheet (column A).")
    print(f"Set a user name in column B and TRUE in column C.\n")
