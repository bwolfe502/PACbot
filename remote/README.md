# PACbot Remote Server — Run PACbot in the Cloud

Run PACbot + an Android emulator on a cloud server. Control everything from your phone using the web dashboard. No home PC required.

## What You'll Have

- A cloud server running 24/7 with an Android emulator
- Kingdom Guard (or any game) running in the emulator
- PACbot controlling the game automatically
- A web dashboard on your phone to start/stop tasks and change settings
- A browser-based game viewer to see what's happening on screen

## Cost

| Provider | RAM | Price | Notes |
|----------|-----|-------|-------|
| DigitalOcean | 4 GB | ~$24/mo | Minimum for 1 emulator |
| DigitalOcean | 8 GB | ~$48/mo | Better performance, 2 emulators |
| Hetzner | 8 GB | ~$7/mo | Cheapest option (EU servers) |

## Setup Guide

### Step 1: Create a Cloud Server

**DigitalOcean (recommended for beginners):**

1. Go to https://www.digitalocean.com and create an account
2. Click **"Create"** → **"Droplets"**
3. Choose settings:
   - **Region:** Pick one close to you
   - **Image:** Ubuntu 22.04 LTS
   - **Size:** Regular → $48/mo (8 GB, 4 vCPUs) recommended
   - **Authentication:** Choose **Password** (simpler) or **SSH Key** (more secure)
4. Click **"Create Droplet"**
5. Wait ~60 seconds for it to spin up
6. Copy the **IP address** shown on the dashboard

### Step 2: Connect to Your Server

**From Windows:**
1. Open **Windows Terminal** (or PowerShell)
2. Type: `ssh root@YOUR_SERVER_IP`
3. Enter your password when prompted
4. You're now connected to your server!

**From iPhone:**
1. Download **Termius** from the App Store (free)
2. Add a new host with your server's IP, username `root`, and password
3. Connect

### Step 3: Install PACbot

Run these commands on your server (copy and paste each line):

```bash
# Download PACbot
git clone https://github.com/YOUR_USERNAME/PACbot.git
cd PACbot

# Run the setup script (installs Docker, Python, ADB, everything)
sudo bash remote/setup.sh
```

This takes 3-5 minutes. It installs all required software automatically.

### Step 4: Start the Android Emulator

```bash
cd remote
bash start.sh
```

This starts the Android emulator in Docker. It takes 1-3 minutes to boot.

When you see "Emulator is ready!", open your browser and go to:
```
http://YOUR_SERVER_IP:6080
```
You should see the Android home screen!

### Step 5: Install Kingdom Guard

You need the game's APK file. You can download it from APKPure, APKCombo, or similar sites.

```bash
# Transfer the APK to your server (from your PC):
# Open a new terminal/PowerShell window and run:
scp /path/to/kingdom-guard.apk root@YOUR_SERVER_IP:/root/PACbot/

# Then on the server, install it:
bash scripts/install-apk.sh /root/PACbot/kingdom-guard.apk
```

Open `http://YOUR_SERVER_IP:6080` to see the game running!

### Step 6: Start PACbot with Web Dashboard

```bash
cd /root/PACbot
source .venv/bin/activate

# Enable web dashboard
python3 -c "
import json
s = {}
try:
    with open('settings.json') as f: s = json.load(f)
except: pass
s['web_dashboard'] = True
with open('settings.json', 'w') as f: json.dump(s, f, indent=2)
print('Web dashboard enabled')
"

# Start PACbot (headless — no GUI needed on server)
python3 main.py
```

Open your phone browser and go to:
```
http://YOUR_SERVER_IP:5000
```

You should see the PACbot web dashboard!

### Step 7: Keep PACbot Running (Optional)

To keep PACbot running after you disconnect from SSH, use `screen`:

```bash
# Install screen (one time)
sudo apt install -y screen

# Start a screen session
screen -S pacbot

# Start PACbot
cd /root/PACbot && source .venv/bin/activate && python3 main.py

# Detach from screen: press Ctrl+A, then D
# PACbot keeps running in the background!

# To reconnect later:
screen -r pacbot
```

## Security

Your server is on the internet, so you should secure it:

### Firewall (UFW)

```bash
# Allow only the ports we need
sudo ufw allow 22      # SSH
sudo ufw allow 5000    # Web dashboard
sudo ufw allow 6080    # Game viewer (noVNC)
sudo ufw enable
```

### Better Option: Use Tailscale

For better security, use Tailscale so only you can access the dashboard:

1. Install Tailscale on the server: `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`
2. Install Tailscale on your phone
3. Sign in with the same account
4. Access via Tailscale IP instead of public IP
5. Then remove public access: `sudo ufw delete allow 5000` and `sudo ufw delete allow 6080`

## Saving Money

Cloud servers charge by the hour. To save money when you're not playing:

**DigitalOcean:**
- Go to your Droplet → click **"Turn off"** → Confirm
- You still pay for storage (~$0.01/hr) but not compute
- Turn it back on when you want to play

**Stop the emulator (saves resources):**
```bash
cd /root/PACbot/remote
bash stop.sh
```

**Start it back up:**
```bash
bash start.sh
```

## Troubleshooting

### Emulator won't start
- Check Docker logs: `cd remote && docker compose logs`
- Make sure Docker is running: `sudo systemctl start docker`
- Try restarting: `bash stop.sh && bash start.sh`

### Game crashes or won't install
- Some games don't work on generic Android emulators
- Try a different Android version: edit `docker-compose.yml` and change `emulator_14.0` to `emulator_13.0`
- Make sure the APK is for the right architecture (x86 or universal)

### Web dashboard not loading
- Make sure PACbot is running
- Check firewall: `sudo ufw status`
- Try: `curl http://localhost:5000` from the server itself

### "KVM not available" warning
- Software rendering is slower but works
- For KVM: use a bare-metal server or a provider that supports nested virtualization
- DigitalOcean regular Droplets do NOT have KVM (software rendering only)
- Hetzner dedicated servers and GCP N1 instances support KVM

### Can't connect via SSH
- Double-check the IP address
- Make sure port 22 is not blocked by your network
- Try from a different network (some corporate/school networks block SSH)
