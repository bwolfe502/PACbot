# PACbot Web Dashboard — Control from Your Phone

Control PACbot from your iPhone (or any phone/tablet) using a web browser instead of TeamViewer.

## How It Works

PACbot runs a small web server on your PC. You open that address in Safari on your phone, and you get a mobile-friendly dashboard with the same controls as the desktop GUI.

## Setup (5 minutes)

### Step 1: Enable the Web Dashboard

In PACbot's desktop GUI, find the **Settings** section and check the **"Web"** checkbox.

- If Flask is not installed, PACbot will ask to install it (click **Yes** — it's a one-time ~10 MB download)
- **Restart PACbot** after enabling

### Step 2: Find Your PC's IP Address

You need your PC's local IP address (something like `192.168.1.42`).

**Windows 10/11:**
1. Open **Settings** (Windows key + I)
2. Go to **Network & Internet**
3. Click **Wi-Fi** (or **Ethernet** if using a cable)
4. Click your network name
5. Scroll down to find **IPv4 address** — that's your IP

**Or from Command Prompt:**
1. Press Windows key, type `cmd`, press Enter
2. Type `ipconfig` and press Enter
3. Look for **IPv4 Address** under your active adapter

### Step 3: Open on Your Phone

1. Make sure your phone is on the **same WiFi** as your PC
2. Open **Safari** (iPhone) or **Chrome** (Android)
3. Type in the address bar: `http://YOUR_IP:5000`
   - Example: `http://192.168.1.42:5000`
4. You should see the PACbot dashboard!

### Step 4: Add to Home Screen (Optional)

Make it feel like a real app:

**iPhone:**
1. While the dashboard is open in Safari, tap the **Share** button (square with arrow)
2. Scroll down and tap **"Add to Home Screen"**
3. Tap **Add**
4. Now you have a PACbot icon on your home screen!

## Accessing from Outside Your Home

By default, the dashboard only works when you're on the same WiFi as your PC. To access it from anywhere (cellular, work, etc.):

### Option A: Tailscale (Recommended — Free)

Tailscale creates a private connection between your phone and PC. It's free for personal use.

1. **On your PC:**
   - Go to https://tailscale.com/download
   - Download and install Tailscale for Windows
   - Sign in with Google, Microsoft, or Apple account

2. **On your iPhone:**
   - Download **Tailscale** from the App Store
   - Sign in with the **same account** you used on your PC

3. **Connect:**
   - Open Tailscale on both devices
   - Your PC will get a Tailscale IP (like `100.64.x.x`)
   - On your phone, go to `http://100.64.x.x:5000`
   - This works from anywhere — cellular, other WiFi networks, etc.

### Option B: Port Forwarding (Advanced)

If you want to access the dashboard via your home's public IP address:

1. Log into your router (usually `192.168.1.1` or `192.168.0.1` in a browser)
2. Find **Port Forwarding** settings (sometimes under "NAT" or "Advanced")
3. Add a rule:
   - External port: `5000`
   - Internal IP: your PC's IP address
   - Internal port: `5000`
   - Protocol: TCP
4. Find your public IP at https://whatismyip.com
5. Access from anywhere: `http://YOUR_PUBLIC_IP:5000`

**Note:** This exposes your dashboard to the internet. For security, consider using Tailscale instead.

## Troubleshooting

### "This site can't be reached"
- Make sure PACbot is running on your PC
- Make sure "Web" is checked in PACbot settings
- Make sure you're using the correct IP address
- Make sure your phone is on the same WiFi as your PC
- Try `http://YOUR_IP:5000` (not https)

### "Flask not installed"
- In PACbot, check the "Web" box — it will offer to install Flask automatically
- Or manually: open Command Prompt and run `pip install flask`

### Dashboard loads but buttons don't work
- Make sure your emulator is running (BlueStacks, MuMu, etc.)
- Click "Refresh Devices" on the dashboard
- Try restarting PACbot

### Windows Firewall blocking access
- Windows may block incoming connections on port 5000
- Search for "Windows Defender Firewall" in Start menu
- Click "Allow an app through firewall"
- Click "Change settings" then "Allow another app"
- Browse to your Python executable (usually in `.venv\Scripts\python.exe`)
- Make sure both "Private" and "Public" are checked

## What Each Page Does

- **Home** — Shows connected devices, their current status, and running tasks
- **Tasks** — Start and stop bot tasks (Auto Quest, Rally Titans, etc.)
- **Settings** — Change all bot settings (same options as the desktop GUI)
- **Logs** — View recent PACbot log output (auto-refreshes every 5 seconds)
