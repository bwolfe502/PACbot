# PACbot

Automated game bot for Android emulators on Windows.

## Requirements

- **Windows 10/11**
- **Python 3.11+** — [Download here](https://www.python.org/downloads/windows/)
- **BlueStacks** or **MuMu Player** emulator
- A valid license key (message Nine on Discord)

## Emulator Setup

Before PACbot will work, change these two settings in your emulator:

1. Open emulator **Settings** (gear icon)
2. Go to **Advanced** or **Device**
3. Set **Resolution** to **1080 x 1920**
4. Go to **Advanced**
5. Turn **ON** "Android Debug Bridge (ADB)"
6. Click **Save** and restart the emulator if prompted

> PACbot will not work without both of these settings.

## Installation

1. Install Python 3.11+ (check **"Add python.exe to PATH"** and **"Install launcher (py.exe)"** during install)
2. Unzip PACbot anywhere (e.g. Desktop)
3. Double-click **`run.bat`**

First run automatically creates a virtual environment, installs dependencies, checks for updates, and prompts for your license key.

## Quick Start

1. Open your emulator and load the game
2. Make sure the game is on the **map screen**
3. Double-click `run.bat` to start PACbot
4. If no devices appear, click **Auto-Connect** then **Refresh**

## Features

### Home Server Mode

For rest weeks when Broken Lands is not active.

| Toggle | What it does |
|--------|-------------|
| **Auto Rally Titans** | Rallies Titans on a loop. Heals first if Auto Heal is on. |
| **Auto Join Groot Rallies** | Joins Groot rallies from alliance members. Mutually exclusive with Rally Titans. |
| **Auto Reinforce Throne** | Reinforces your alliance throne on a loop. Can run alongside rallies. |

### Broken Lands Mode

For BL / Burning Expedition weeks.

| Toggle | What it does |
|--------|-------------|
| **Auto Quest** | Claims rewards, detects quest types, starts rallies automatically. |
| **Auto Pass Battle** | Reinforces your pass/tower or joins war rallies. Requires a Personal "Enemy" marker. |
| **Auto Occupy** | Scans territory grid, attacks enemy squares, captures territory. |

### Settings

| Setting | Description |
|---------|-------------|
| **Auto Heal** | Automatically heal troops before actions. On by default. |
| **Auto Restore AP** | Restore Action Points before PvE actions. Configure sources: Free, Potions, Large Potions, Gems (with limit). |
| **Min Troops** | Minimum available troops before PACbot will deploy. |
| **Randomize +/-** | Adds random variation to intervals (e.g. 30s ± 5 = 25-35s). |
| **Territory Teams** | Set your team color and the enemy color to attack. |

### More Actions

Expand the **More Actions** panel for manual controls across three tabs:

**Farm** — Rally Evil Guard, Join Titan/EG/Groot Rally, Heal All

**War** — Target (navigate to marker), Attack, Reinforce Throne, UP UP UP (join war rallies), Teleport, Attack Territory

**Debug** — Save Screenshot, Check Quests/Troops/Screen, Sample Squares, Tap X/Y

Each task row has a **repeat checkbox** with an interval, or a **button** to run once.

## Controls

| Button | Action |
|--------|--------|
| **STOP ALL** | Stops every running task on every device immediately |
| **Restart** | Saves settings, stops tasks, checks for updates, relaunches |
| **Quit** | Saves settings, stops tasks, closes PACbot |

## Tips

- Start your emulator and load the game **before** starting PACbot
- Make sure the game is on the **map screen** before starting auto tasks
- Set **Min Troops** if you want to keep a reserve
- For pass battles, place your **Personal "Enemy" marker** first
- Use **Randomize +/-** to make intervals less predictable
- Check the console window for detailed logs

## Troubleshooting

**run.bat closes instantly**
Python is not installed correctly. Reinstall and make sure both "Add to PATH" and "Install launcher" are checked.

**No devices found**
Make sure your emulator is fully loaded before starting PACbot. Click Auto-Connect, then Refresh. Verify ADB is enabled in emulator settings.

## License

Contact Nine on Discord for a license key.
