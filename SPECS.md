# Technical Specification

> Developer reference for Discord Promo Helper internals.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Discord Promo Helper                        │
├─────────────────────────────────────────────────────────────────────┤
│  UI Layer                                                           │
│  └── src/ui.py           Tkinter GUI                               │
├─────────────────────────────────────────────────────────────────────┤
│  Business Logic                                                      │
│  ├── src/main.py          Entry point                              │
│  ├── src/server_config.py Server settings                          │
│  ├── src/promotion_helper.py  Posting queue                        │
│  └── src/storage.py       Timestamp persistence                    │
├─────────────────────────────────────────────────────────────────────┤
│  Discord Automation                                                  │
│  ├── src/discord_nav.py   Server scanning                          │
│  └── src/utils.py         OCR & window helpers                     │
├─────────────────────────────────────────────────────────────────────┤
│  Data Files                                                          │
│  ├── servers.json         Scanned servers                          │
│  ├── server_config.json   User settings                            │
│  └── data/*.json          Runtime data                             │
└─────────────────────────────────────────────────────────────────────┘
```

## Core Modules

| Module | Purpose |
|--------|---------|
| `main.py` | CLI entry point (`--ui`, `--scan`, `--post`) |
| `ui.py` | Tkinter GUI for server configuration |
| `discord_nav.py` | Server scanning via OCR + mouse automation |
| `server_config.py` | Load/save user settings (friendly names, tags) |
| `promotion_helper.py` | Posting logic with rate limiting |
| `storage.py` | Per-channel timestamp persistence |
| `utils.py` | Tesseract setup, Discord window management |
| `config.py` | Global constants (keywords, rate limits) |

## Data Files

| File | Purpose |
|------|---------|
| `servers.json` | Scanned server list (OCR names, positions) |
| `server_config.json` | User config (friendly names, channels, tags) |
| `data/last_post_timestamps.json` | Rate limit tracking |

### server_config.json Format

```json
{
  "servers": {
    "normalized key": {
      "ocr_name": "T900fficial Discord",
      "friendly_name": "T90 Official",
      "promo_channels": ["self-promo"],
      "game_tags": ["aoe2"],
      "enabled": true
    }
  },
  "settings": {
    "rate_limit_hours": 3
  }
}
```

## Rate Limiting

- **Default**: 3 hours per channel
- **Storage**: `data/last_post_timestamps.json`
- **Key format**: `{server}:{channel}`
- **Logic**: Check timestamp before posting, update after success

## Server Scanning

### How It Works

1. Focus Discord window
2. Hover each server icon in left sidebar
3. OCR the tooltip that appears
4. Scroll down, repeat until end detected

### End Detection

1. **End markers**: "Add a Server" or "Discover" tooltip
2. **Stale fallback**: 3 consecutive pages with no new servers
3. **Safety limit**: Max 50 pages

### OCR Challenges

- Tooltips appear briefly → multiple retry attempts
- Dark icons may not trigger tooltip → synthetic hover
- OCR produces garbled text → fuzzy deduplication
- Dedup: substring match, 60% word overlap, or 85% char similarity

## Game Filtering

Tag servers by game to filter promotions:

```bash
# Only post to Fortnite servers
python -m src.main --post "Title" "link" --game fortnite
```

**Predefined tags** (in `config.py`):
- FPS: fortnite, valorant, apex, warzone, cs2
- Strategy: aoe2, aoe4
- MMO: wow, ffxiv
- General: variety, just chatting

## Channel Detection

**Keywords** (partial match):
- promo, self-promo, promotion
- advertise, ads
- plug, share, showcase

## Future Features

### Mid-Stream Game Change
- Detect game switch during stream
- Re-post to servers tagged for new game
- Respect rate limits (no double-posting)

### Stream Integration
- Twitch/YouTube API for live status
- Auto-detect game category
- Message templates: `{title}`, `{game}`, `{viewers}`

### Scheduled Posting
- Queue promotions for specific times
- Periodic re-promotion during long streams

## Development

### Requirements

- Python 3.11+
- Tesseract OCR: `brew install tesseract`
- Tkinter: `brew install python-tk@3.11`

### Permissions (macOS)

- Screen Recording (for screenshots)
- Accessibility (for mouse/keyboard)

### Running Tests

```bash
source .venv/bin/activate
python scripts/test_full_scan.py
python -m pytest tests/
```

### Code Style

- Type hints where practical
- Docstrings for public functions
- JSON for persistence (no database)


