# Specs.MD

Repository: DiscordPromoHelper

Purpose:
- Automate respectful self-promotion in Discord servers by detecting promotion channels and posting a preset message with a stream link.
- Respect anti-spam by queueing posts and enforcing per-channel/posting delays.

Architecture Overview
=====================

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Discord Promo Helper                        │
├─────────────────────────────────────────────────────────────────────┤
│  UI Layer (src/ui.py)                                               │
│  ├── Server list with friendly names                                │
│  ├── Per-server channel configuration                               │
│  ├── Game tag filtering                                             │
│  └── Settings (rate limit, etc.)                                    │
├─────────────────────────────────────────────────────────────────────┤
│  Business Logic                                                      │
│  ├── src/main.py          Entry point (--ui, --cli, --scan)        │
│  ├── src/promotion_helper.py  Posting queue & rate limiting        │
│  ├── src/server_config.py     Server settings (friendly names)     │
│  └── src/storage.py           Timestamp persistence                │
├─────────────────────────────────────────────────────────────────────┤
│  Discord Automation                                                  │
│  ├── src/discord_nav.py   Server scanning & navigation             │
│  └── src/utils.py         OCR, window management, helpers          │
├─────────────────────────────────────────────────────────────────────┤
│  Data Files                                                          │
│  ├── servers.json              Scanned servers (OCR names)         │
│  ├── server_config.json        User config (friendly names, tags)  │
│  └── data/last_post_timestamps.json  Rate limit tracking           │
└─────────────────────────────────────────────────────────────────────┘
```

Core Modules
============

- `src/config.py`
  - `CHANNEL_KEYWORDS`: list of keywords used to detect promo channels
  - `RATE_LIMIT_HOURS`: default 3 hours between posts to same channel
  - `COMMON_GAME_TAGS`: predefined game tags for filtering

- `src/main.py` (Entry Point)
  - `--ui`: Launch Tkinter GUI (default)
  - `--cli`: Run in legacy CLI mode
  - `--scan`: Run server scan only
  - `--post TITLE LINK`: Post promotion with given message
  - `--game TAG`: Filter servers by game tag
  - `--dry-run`: Show what would happen without posting

- `src/ui.py` (Tkinter GUI)
  - Server list with filtering
  - Per-server settings dialog (friendly name, channels, game tags)
  - Game filter management
  - Rate limit configuration

- `src/server_config.py`
  - Load/save server configuration from `server_config.json`
  - Friendly name management (OCR names are often garbled)
  - Game tag assignment for filtering
  - Promo channel list per server
  - Enable/disable servers

- `src/discord_nav.py`
  - Server column scanning with OCR
  - Tooltip hover and reading
  - Scroll management for full server enumeration
  - Outputs to `servers.json`

- `src/utils.py`
  - Tesseract OCR setup and helpers
  - Discord window detection and focus
  - Channel position finding via OCR

- `src/promotion_helper.py`
  - `post_update()`: type/post message to a channel
  - `queue_updates()`: manages repeated posting with delays

- `src/storage.py`
  - Per-channel timestamp tracking
  - Cooldown enforcement
  - Whitelist/blacklist checking

Rate Limiting
=============

**Default: 3 hours per promo channel**

Rationale: Many Discord servers have anti-spam rules limiting self-promotion 
to once every few hours. A 3-hour default is respectful and unlikely to 
trigger moderator action.

Implementation:
- Timestamps stored in `data/last_post_timestamps.json`
- Key format: `"{server_name}:{channel_name}"`
- Before posting, check if enough time has passed
- Update timestamp after successful post
- Configurable via UI or `server_config.json`

Game-Based Filtering
====================

**Purpose**: When streaming a specific game, only post to relevant servers.

Example use case:
- User is streaming Fortnite
- Only servers tagged with "fortnite" receive promotions
- AoE2 servers are skipped for this session

Configuration:
- Game tags assigned per server in `server_config.json`
- Filter applied via `--game TAG` CLI flag or UI selection
- Multiple tags per server supported (e.g., "fortnite", "battle royale")

Predefined tags (in config.py):
- FPS: fortnite, valorant, apex, warzone, cs2, overwatch
- MOBA: league, dota
- MMO: wow, ffxiv
- Strategy: aoe2, aoe4
- Sandbox: minecraft, roblox, gta
- Classic: ultima, uo
- General: variety, just chatting, irl
- [ ] Add `max_icon_retries` argument to `capture_discord_servers` and expose it via `scripts/test_full_scan.py`.
- [ ] Add a preflight check for macOS Screen Recording permission and `tesseract` availability.
- [ ] Run an instrumented scan in a visible Discord session, capture `viewport_history.json`, and verify coverage.
- [ ] If repeated stable fingerprints prevent reaching top/bottom, implement a scrollbar-drag fallback to move the scroll thumb to the extremes.

Notes for maintainers:
- Do not attempt to circumvent server anti-spam rules; the tool is intended to be respectful and compliant.
- When testing on macOS, ensure `System Settings -> Privacy & Security -> Screen Recording` grants access to the terminal/Python process.

Server Navigation: Name-Based Identification (Design)
------------------------------------------------------
**Problem**: Discord adds new servers at TOP (below DM icon), causing all indices to shift.
Index-based navigation is unreliable - "server at index 5" changes when a new server is added.

**Solution**: Hybrid approach - use POSITION for iteration, NAME as backup identifier.

Key insight: OCR is unreliable, so we cannot solely depend on name matching.

Storage format (`servers.json`):
```json
{
  "total_servers": 96,
  "servers": [
    {"index": 0, "name": "Innovation Inc", "page": 0, "y_position": 300},
    {"index": 1, "name": "GrogCloud", "page": 7, "y_position": 300}
  ]
}
```

Scan termination detection:
1. **End marker**: Look for "Add a Server" or "Explore/Discover" tooltips
2. **Stale page fallback**: After 3 consecutive pages with no new servers, assume end reached
3. **Max pages safety**: Hard limit (50 pages) prevents infinite loops

Navigation strategy (not yet implemented):
1. **Full rescan on each session**: Position is ephemeral, rescan to get current layout
2. **Count-based verification**: If we had N servers before, we should find ~N servers now
3. **New server detection**: Compare counts - if current > previous, new servers added at top
4. **Name as secondary ID**: Use fuzzy name matching only when position-based nav fails

Why not pure name-based:
- OCR produces garbled text ("Add a Server" → "eae a. box")
- Fuzzy matching has false positives
- Server renames break name-based tracking

Why position works:
- Servers have stable relative order (new ones at top, rest shifts down)
- We can iterate reliably with scroll + icon detection
- Count-based verification catches missed/extra servers

Caveats:
- Must rescan before each operation (positions change)
- OCR names are hints only, not authoritative
- Need to handle case where user left a server between scans

Channel Reading & Promo Detection (Design)
------------------------------------------
**Goal**: For each server, scan channels to find promotion/self-promo channels.

**Phase 1: Keyword-based detection**
Primary keywords (exact/fuzzy match in channel name):
- `promo`, `self-promo`, `self promo`, `selfpromo`
- `promote`, `promotion`, `self-promotion`
- `advertise`, `advertising`, `ads`
- `plug`, `self-plug`, `plugs`
- `share`, `share-your`, `show-off`
- `spotlight`, `showcase`

**Phase 2: Content verification (for ambiguous channels)**
Some servers customize promo channel names. If channel name is ambiguous:
1. Enter channel, read recent messages
2. Look for promotional content patterns:
   - Links (twitch.tv, youtube.com, twitter.com, etc.)
   - "Check out my...", "Follow me...", "Subscribe..."
   - Multiple users posting similar self-promotional content
3. Check channel topic/description if visible

**Storage format** (`server_channels.json`):
```json
{
  "servers": {
    "Innovation Inc": {
      "server_index": 0,
      "channels": {
        "promo": {"type": "promo", "confidence": "high", "method": "keyword"},
        "share-your-stuff": {"type": "promo", "confidence": "medium", "method": "keyword"},
        "general-chat": {"type": "general", "confidence": "high"}
      },
      "promo_channels": ["promo", "share-your-stuff"],
      "last_scanned": "2025-11-25T12:00:00Z"
    }
  }
}
```

**Channel scanning workflow**:
1. Click server icon to enter server
2. Wait for channel list to load (left sidebar)
3. Scroll to top of channel list
4. Iterate visible channels:
   - OCR channel name
   - Check against keyword list
   - If match → mark as promo (high confidence)
   - If partial match → mark for content verification
5. Scroll down, repeat until end of channel list
6. For ambiguous channels: enter and scan content
7. Save results to `server_channels.json`

**Channel detection challenges**:
- Channel categories (folders) vs actual channels
- Voice channels vs text channels (only text channels matter)
- Private/locked channels (skip, can't post anyway)
- Channel icons/emojis in names

**Implementation priority**:
1. [ ] `enter_server(index)` - click server icon by position
2. [ ] `scan_channel_list()` - iterate and OCR channel names  
3. [ ] `find_promo_channels(keywords)` - filter for promo keywords
4. [ ] `verify_channel_content(channel)` - enter and read messages
5. [ ] `save_server_channels()` - persist to JSON

Future Features (Planned)
=========================

## Mid-Stream Game Change Re-Posting

**Scenario**: Streamer starts with Fortnite, switches to Minecraft mid-stream.

**Behavior**:
1. Detect game category change (via Twitch/YouTube API or manual trigger)
2. Identify servers tagged with the NEW game (Minecraft servers)
3. Check rate limit for those servers' promo channels
4. If allowed (3hr since last post), post update to new game's servers
5. Do NOT re-post to servers already posted this session

**Technical requirements**:
- Track "posted this session" separately from cooldown timestamps
- Streaming platform API integration (Twitch, YouTube)
- Game change detection callback/webhook
- Session state management

**Configuration**:
- `enable_game_change_repost`: true/false (default: false)
- `game_change_cooldown_minutes`: minimum time after game change before repost (default: 15)
- `max_reposts_per_session`: prevent spam on frequent game changes (default: 2)

**NOT in scope**:
- Automatic game detection from Discord activity status
- Multi-platform simultaneous streaming

## Stream Status Integration

**Purpose**: Auto-detect stream info and update promotions accordingly.

**Twitch Integration**:
- OAuth connection to Twitch API
- Detect stream title, game category, viewer count
- Auto-select servers based on detected game
- Include viewer count in promo message (optional)

**YouTube Integration**:
- Similar OAuth flow
- Detect live stream status and title
- Game category detection (if available)

**Configuration**:
- Platform API keys (stored securely)
- Message templates with placeholders: `{title}`, `{game}`, `{viewers}`, `{url}`
- Auto-post on stream start (optional, with rate limit)

## Scheduled Promotions

**Purpose**: Queue promotions for specific times.

**Use case**:
- Schedule promotion 15 minutes after stream start
- Periodic re-promotion every 2 hours during long streams
- Pre-scheduled promotion for announced stream times

## Multi-Message Templates

**Purpose**: Avoid repetitive identical messages.

**Implementation**:
- Define multiple message templates per server/channel
- Rotate through templates on each post
- Track last-used template to ensure variety

## Analytics & Reporting

**Purpose**: Track promotion effectiveness.

**Metrics to track**:
- Posts sent per server/channel
- Time since last post
- Failed posts (errors, rate limits hit)
- Server/channel coverage percentage

**Reports**:
- Weekly summary of promotion activity
- Servers not promoted to in X days
- Channels with frequent rate limit hits

Data Files Reference
====================

| File | Purpose | Created By |
|------|---------|------------|
| `servers.json` | Scanned server list (OCR names, positions) | `discord_nav.py` |
| `server_config.json` | User settings (friendly names, tags, channels) | `ui.py` / `server_config.py` |
| `data/last_post_timestamps.json` | Rate limit tracking | `storage.py` |
| `server_channels.json` | (Future) Per-server channel scan results | (not yet implemented) |

Server Config JSON Format
-------------------------

```json
{
  "servers": {
    "normalized ocr name key": {
      "ocr_name": "T900fficial Discord",
      "friendly_name": "T90 Official Discord",
      "promo_channels": ["self-promo", "share-your-work"],
      "game_tags": ["aoe2", "age of empires", "strategy"],
      "enabled": true,
      "notes": "Age of Empires 2 community"
    }
  },
  "game_filters": {
    "fortnite": ["Fortnite Official", "FN Streams"],
    "aoe2": ["T90 Official Discord", "AoE2 Community"]
  },
  "settings": {
    "rate_limit_hours": 3,
    "default_enabled": true
  }
}
```

Development Notes
=================

## macOS Requirements

1. **Screen Recording Permission**: Required for screenshots
   - System Settings → Privacy & Security → Screen Recording
   - Grant access to Terminal / VS Code / Python

2. **Accessibility Permission**: Required for mouse/keyboard control
   - System Settings → Privacy & Security → Accessibility

3. **Tesseract OCR**: Required for text recognition
   ```bash
   brew install tesseract
   ```

## Running the App

```bash
# Activate virtual environment
source .venv/bin/activate

# Launch GUI
python -m src.main

# Or run specific modes
python -m src.main --scan      # Scan servers only
python -m src.main --cli       # CLI mode
python -m src.main --help      # Show help
```

## Testing

```bash
# Run server scan test
python scripts/test_full_scan.py

# Test imports
python -c "from src.server_config import load_config; print(load_config())"
```


