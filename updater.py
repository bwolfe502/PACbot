"""
PACbot Auto-Updater
Checks GitHub releases for new versions and updates automatically.
"""

import os
import sys
import zipfile
import shutil

# ============================================================
# CONFIGURATION — set these after creating your GitHub repo
# ============================================================
GITHUB_USER = "bwolfe502"
GITHUB_REPO = "PACbot"  # Change this if your repo name is different

VERSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "version.txt")

# Files/folders that should NOT be overwritten during updates
# (user-specific files that should be preserved)
PRESERVE_FILES = {
    ".license_key",
    ".venv",
    "__pycache__",
    "platform-tools",
    "_update.zip",
    "_update_temp",
}


def get_current_version():
    """Read the current version from version.txt."""
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "0.0.0"


def get_latest_release():
    """
    Check GitHub for the latest release.
    Returns (tag_name, zip_download_url) or (None, None) if no update.
    """
    import requests

    api_url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/releases/latest"

    try:
        resp = requests.get(api_url, timeout=10)

        if resp.status_code == 404:
            # No releases yet
            return None, None

        resp.raise_for_status()
        data = resp.json()

        tag = data.get("tag_name", "")

        # Look for the uploaded .zip asset (the full package with platform-tools)
        zip_url = None
        for asset in data.get("assets", []):
            if asset["name"].endswith(".zip"):
                zip_url = asset["browser_download_url"]
                break

        # Fall back to source zipball if no asset found
        if not zip_url:
            zip_url = data.get("zipball_url")

        return tag, zip_url

    except Exception:
        # Network error — skip update silently
        return None, None


def version_tuple(v):
    """Convert version string like 'v1.2.3' or '1.2.3' to tuple (1, 2, 3)."""
    v = v.lstrip("v")
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def check_and_update():
    """
    Main update flow. Checks GitHub and updates if a newer version exists.
    """
    import requests

    current = get_current_version()
    print(f"Current version: {current}")
    print("Checking for updates...", end=" ", flush=True)

    latest_tag, zip_url = get_latest_release()

    if latest_tag is None:
        print("No releases found. Skipping update.")
        return False

    if version_tuple(latest_tag) <= version_tuple(current):
        print(f"Up to date.")
        return False

    print(f"Update available! {current} → {latest_tag}")
    print("Downloading update...", end=" ", flush=True)

    try:
        resp = requests.get(zip_url, timeout=60, stream=True)
        resp.raise_for_status()

        # Save the zip to a temp file
        app_dir = os.path.dirname(os.path.abspath(__file__))
        zip_path = os.path.join(app_dir, "_update.zip")
        extract_dir = os.path.join(app_dir, "_update_temp")

        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        print("Done.")
        print("Installing update...", end=" ", flush=True)

        # Extract the zip
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        # GitHub zipball contains a single top-level folder like "user-repo-hash/"
        # We need to find it and copy its contents
        extracted_items = os.listdir(extract_dir)
        if len(extracted_items) == 1:
            source_dir = os.path.join(extract_dir, extracted_items[0])
        else:
            source_dir = extract_dir

        # Copy new files over existing ones, preserving protected files
        for item in os.listdir(source_dir):
            if item in PRESERVE_FILES:
                continue

            src = os.path.join(source_dir, item)
            dst = os.path.join(app_dir, item)

            if os.path.isdir(src):
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

        print("Done.")
        print(f"Updated to {latest_tag} successfully!")

        # Cleanup temp files
        try:
            os.remove(zip_path)
            shutil.rmtree(extract_dir)
        except OSError:
            pass

        return True

    except Exception as e:
        print(f"\nUpdate failed: {e}")
        print("Continuing with current version...")

        # Cleanup on failure
        for path in ["_update.zip", "_update_temp"]:
            full = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
            try:
                if os.path.isfile(full):
                    os.remove(full)
                elif os.path.isdir(full):
                    shutil.rmtree(full)
            except OSError:
                pass

        return False


if __name__ == "__main__":
    check_and_update()
