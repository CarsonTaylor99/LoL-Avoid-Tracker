# LoL Avoid Tracker

A League of Legends companion tool that monitors the game activity of players you want to track — rivals, smurfs, or coordinated premade groups. Know when they're in game, queuing, or safe before you queue up yourself.

![App Screenshot](logo.png)

---

## Features

- **Real-time status tracking** — detects when players are live in a game, just finished, or safe
- **Premade detection** — clusters players into teams and alerts when 3–5 coordinated players are active together
- **Smart polling** — adaptive check intervals per status to stay within Riot's API rate limits
- **Match history logging** — append-only history per player with timestamps, outcomes, and teammates for future analysis
- **Drag-and-drop organization** — group players into named team folders, reorder freely
- **Live game panel** — see all 10 participants (champions, Riot IDs, teams) when a tracked player is in game
- **Offline-resilient** — champion name cache, transient error tolerance, and graceful degradation

---

## Status Legend

| Status | Meaning |
|---|---|
| 🔴 **IN GAME** | Currently in a live match |
| 🟠 **QUEUING LIKELY** | Finished <10–15 min ago, probably re-queuing |
| 🟡 **RECENTLY DONE** | Finished within the last 25 minutes |
| 🟢 **PLAYED THIS HOUR** | Played within the last hour |
| ⚪ **SAFE** | No games in the last hour |

---

## Requirements

- **Windows 10/11** (the pre-built `.exe` is Windows-only)
- **Riot Games API key** — free from [developer.riotgames.com](https://developer.riotgames.com)

> Development keys expire every 24 hours. You'll need to refresh yours daily or apply for a personal/production key.

---

## Installation

### Option A — Pre-built Executable (Recommended)

1. Download the latest release from the [Releases](../../releases) page
2. Extract the zip and run `LoLAvoidTracker.exe`
3. On first launch, click **Settings** and enter:
   - Your Riot API key
   - Your server region (NA1, EUW1, KR, etc.)
4. Click **Add Player**, enter a Riot ID in `Name#TAG` format (e.g. `Faker#KR1`), and start tracking

---

### Option B — Run from Source

#### Prerequisites

- [Python 3.10+](https://www.python.org/downloads/)
- pip (included with Python)

#### Steps

```bash
# 1. Clone the repository
git clone https://github.com/YOUR_USERNAME/LoLAvoidTracker.git
cd LoLAvoidTracker

# 2. (Optional but recommended) Create a virtual environment
python -m venv venv

# On Windows:
venv\Scripts\activate

# On macOS/Linux:
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the app
python app.py
```

On first launch, the app will create a `dist/` folder with config and data files.

---

### Option C — Build the Executable Yourself

```bash
# Install PyInstaller
pip install pyinstaller

# Build using the included spec file
pyinstaller LoLAvoidTracker.spec
```

The output `.exe` will be in `dist/LoLAvoidTracker.exe`.

---

## Configuration

All data is stored in the `dist/` directory alongside the executable:

| File | Purpose |
|---|---|
| `config.json` | API key and region setting |
| `players.json` | Tracked players and their current statuses |
| `teams.json` | Named team folders and member lists |
| `champions.json` | Champion ID → name cache (auto-fetched from Riot Data Dragon) |
| `history.json` | Append-only match log per player |

To change your API key or region at any time, click the **Settings** button in the top right of the app.

---

## Supported Regions

| Code | Region |
|---|---|
| NA1 | North America |
| EUW1 | Europe West |
| EUN1 | Europe Nordic & East |
| KR | Korea |
| BR1 | Brazil |
| LA1 | Latin America North |
| LA2 | Latin America South |
| OC1 | Oceania |
| TR1 | Turkey |
| RU | Russia |
| JP1 | Japan |
| SG2 | Southeast Asia |

---

## Usage

1. **Add players** — Click **+ Add Player** and enter `RiotName#TAG`
2. **Create teams** — Click **+ Team** to create a named group, then drag players into it
3. **Monitor activity** — The app automatically polls each player on a schedule and updates statuses in real time
4. **Live game details** — Click the expand arrow on an **IN GAME** player to see all participants
5. **Manage players** — Right-click any player for rename, enable/disable, or remove options

---

## Tech Stack

- **Python 3** + [pywebview](https://pywebview.flowrl.com/) — desktop window hosting a local HTML/JS UI
- **Riot Games API** — Spectator-V5 (live game detection) and Match-V5 (recent match history)
- **Vanilla HTML/CSS/JS** — no frontend framework dependencies
- **PyInstaller** — packages everything into a single standalone `.exe`

---

## API Rate Limits

The scheduler is designed to stay within Riot's free developer key limits:

- **Spectator-V5:** 100 requests / 120 seconds
- The cadence system leaves a headroom buffer and backs off during errors automatically

If you have a large list of players, consider applying for a [personal API key](https://developer.riotgames.com/app-type) to get higher limits.

---

## License

MIT License — see [LICENSE](LICENSE) for details.
