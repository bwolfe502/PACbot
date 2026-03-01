# 9Bot Tester Protocol

Guide for users helping test and improve 9Bot.

## Two Ways to Help

### Bug Reporters
You've found something broken and want to let us know. Minimum effort, maximum value.

**What to do:**
1. Run 9Bot normally
2. When something goes wrong, note what happened
3. Use the built-in bug report feature (generates a zip file with logs, stats, screenshots, settings)
4. Send the zip + a short description of what you saw

**What to include in your description:**
- What you expected to happen
- What actually happened
- Approximately when it happened (time of day)
- Which device/emulator it was on (if you noticed)

### Active Testers
You want to actively help test specific features, try pre-release builds, and provide detailed feedback.

**What we ask:**
- Run specific test scenarios we assign
- Report results (even if everything works — "it worked" is data too)
- Try new builds before general release
- Answer follow-up questions about behavior you observed

**What you get:**
- Early access to fixes and features
- Direct input into development priorities
- Your specific issues get fast-tracked

## Bug Report Contents

The bug report zip automatically collects:

| Data | Purpose |
|------|---------|
| `logs/9bot.log*` | Action history, errors, warnings, timing |
| `stats/session_*.json` | Per-action success rates, template match scores, ADB timing |
| `debug/failures/` | Screenshots captured at moment of failure |
| `settings.json` | Your configuration (thresholds, toggles, intervals) |
| `report_info.txt` | Version, OS, Python, devices, transition timing stats |

**Privacy:** Bug reports contain only 9Bot operational data. No personal info, passwords, or account details are included.

## Machine Profile

When you first start testing, please share these details so we can account for hardware-specific behavior:

- **Operating System**: Windows version (e.g. Windows 11 24H2)
- **Emulator(s)**: Type and version (e.g. BlueStacks 5.21, MuMu Player 12)
- **Number of emulator instances**: How many you run simultaneously
- **Emulator resolution**: Should be 1080x1920 portrait
- **CPU / RAM**: Approximate specs (affects ADB/screenshot timing)
- **Python version**: Shown in report_info.txt

## How We Use Your Data

### What We Analyze
- **Success/failure rates** per action (gather_gold, rally_titan, etc.)
- **Template match scores** — confidence values for image recognition
- **Timing data** — how long each ADB command and screen transition takes
- **Navigation patterns** — how often the bot gets stuck on unknown screens
- **Error patterns** — recurring failures, stuck states, recovery outcomes

### What We Fix
Your data directly drives fixes. Examples of data-driven fixes:
- Low template match scores → adjusted thresholds
- Timing budgets exceeded → increased wait times
- Recovery failures → added new recovery strategies
- Failure screenshots → identified missed UI elements

## Testing Scenarios

When assigned a test scenario, here's the format:

### Scenario Template
```
SCENARIO: [Name]
VERSION: [9Bot version to test]
DURATION: [How long to run]
SETTINGS: [Any specific settings to configure]
ACTIONS: [What to enable/run]
WATCH FOR: [Specific things to note]
REPORT: [What to send back]
```

### Example
```
SCENARIO: Gold Gathering Overnight
VERSION: v1.4.3
DURATION: 8+ hours overnight
SETTINGS: gather_enabled=true, gather_mine_level=4, gather_max_troops=3
ACTIONS: Enable Auto Quest with gather quests active
WATCH FOR: Does gather_gold succeed? Check logs for "Gather Gold troop deployed"
REPORT: Send bug report zip + count of successful/failed gathers from logs
```

## Communication

- Describe what you see, not what you think the cause is
- "The bot kept tapping the same spot for 5 minutes" is better than "I think the image matching is broken"
- Screenshots of the game screen during failures are extremely helpful
- Note the approximate time when issues occur — we can cross-reference with logs
- If the bot gets stuck, let it run for a few minutes (it may recover) before intervening

## Version History

When testing across versions, note which version you're running. The version is shown in:
- The 9Bot window title
- The log file header (`NEW SESSION — ... — vX.Y.Z`)
- `report_info.txt` in the bug report zip
