# Discord Promo Helper

Respectful self-promotion automation for Discord streamers.

## What It Does

- **Scans** all your Discord servers (106+ supported)
- **Detects** promo/self-promo channels automatically
- **Fetches** stream info from Twitch, Kick, YouTube (title, game, viewers)
- **Posts** your stream link with rate limiting (3h default)
- **Filters** by game tag (Fortnite servers, AoE2 servers, etc.)

## Quick Start

```bash
# 1. Clone and setup
git clone https://github.com/phoebusg/DiscordPromoHelper.git
cd DiscordPromoHelper
./scripts/setup.sh
source .venv/bin/activate

# 2. Launch the GUI
python -m src.main
```

## Usage

### GUI Mode (Recommended)
```bash
python -m src.main
```
- **Fetch Stream Info**: Auto-detect title/game from your stream URL
- Configure friendly names for servers (OCR names are often garbled)
- Set promo channels per server
- Tag servers by game for filtering
- Enable/disable servers

### CLI Mode
```bash
# Scan Discord for servers
python -m src.main --scan

# Post to all enabled servers
python -m src.main --post "Check out my stream!" "https://twitch.tv/you"

# Post only to Fortnite servers
python -m src.main --post "Playing Fortnite!" "https://twitch.tv/you" --game fortnite

# Preview without posting
python -m src.main --post "Test" "https://twitch.tv/you" --dry-run

# Test stream info fetching
python -m src.stream_info twitch yourname
python -m src.stream_info https://kick.com/xqc
```

## Features

| Feature | Description |
|---------|-------------|
| **Stream Info** | Auto-fetch title/game from Twitch, Kick, YouTube |
| Server Scanning | OCR-based detection of all your Discord servers |
| Friendly Names | Replace garbled OCR names with readable labels |
| Game Tagging | Filter servers by game (fortnite, aoe2, valorant, etc.) |
| Rate Limiting | 3-hour default cooldown per channel |
| Promo Detection | Auto-detect channels named "promo", "self-promo", etc. |

## Stream Info Setup (Optional)

To use the "Fetch Stream Info" feature, set up API keys:

```bash
# Copy the example and fill in your values
cp .env.example .env
```

| Platform | Setup URL | Required |
|----------|-----------|----------|
| **Twitch** | [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps) | Client ID + Secret |
| **YouTube** | [console.cloud.google.com](https://console.cloud.google.com/apis/credentials) | API Key |
| **Kick** | N/A | No setup needed |

## Requirements

- **macOS** (primary) or Windows
- **Python 3.11+**
- **Tesseract OCR**: `brew install tesseract`
- **Permissions**: Screen Recording + Accessibility (macOS)

See [SETUP.md](SETUP.md) for detailed setup instructions.

## Project Structure

```
├── src/
│   ├── main.py           # Entry point (--ui, --scan, --post)
│   ├── ui.py             # Tkinter GUI
│   ├── stream_info.py    # Twitch/Kick/YouTube API integration
│   ├── discord_nav.py    # Server scanning
│   ├── server_config.py  # Settings management
│   └── utils.py          # OCR, window helpers
├── servers.json          # Scanned server list
├── server_config.json    # Your settings
├── .env.example          # API key template
└── scripts/setup.sh      # Setup script
```

## ⚠️ Disclaimer

This tool is for **respectful** self-promotion only. Built-in safeguards:

- Rate limiting prevents spam
- Per-server enable/disable
- Channel whitelist/blacklist

**Circumventing these safeguards may get your account banned.** Always follow server rules.
