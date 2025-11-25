# config.py
"""Global configuration for Discord Promo Helper."""

# =============================================================================
# CHANNEL DETECTION
# =============================================================================
# Keywords to detect promo channels (case-insensitive, partial match)
CHANNEL_KEYWORDS = [
    "self-promo", "selfpromo", "self promo",
    "promotion", "promo", "promote",
    "advertise", "advertising", "ads",
    "plug", "self-plug", "plugs",
    "share", "share-your", "show-off",
    "spotlight", "showcase"
]

# =============================================================================
# RATE LIMITING
# =============================================================================
# Default posting cooldown (can be overridden in server_config.json)
# User requested: "one message per promo channel per 3 hours may be a sane rate limit"
RATE_LIMIT_HOURS = 3
POST_COOLDOWN_MINUTES = RATE_LIMIT_HOURS * 60  # Legacy compatibility

# =============================================================================
# PERSISTENCE
# =============================================================================
# File for last-post timestamps (per-channel tracking)
PERSISTENCE_FILE = "data/last_post_timestamps.json"

# Server configuration file (friendly names, channels, game tags)
SERVER_CONFIG_FILE = "server_config.json"

# Scanned servers from discord_nav
SERVERS_JSON_FILE = "servers.json"

# Cleanup persistence entries older than N days
PERSISTENCE_CLEANUP_DAYS = 30

# =============================================================================
# WHITELIST / BLACKLIST
# =============================================================================
# Optional explicit whitelist/blacklist for channels (names). 
# If whitelist is non-empty, only those channels are allowed.
CHANNEL_WHITELIST = []
CHANNEL_BLACKLIST = []

# =============================================================================
# GAME-BASED FILTERING
# =============================================================================
# Predefined game tags for common streaming games
# These can be assigned to servers in server_config.json
COMMON_GAME_TAGS = [
    "fortnite", "valorant", "apex", "warzone",
    "minecraft", "roblox", "gta", "gtav",
    "aoe2", "aoe4", "age of empires",
    "wow", "world of warcraft", "ffxiv",
    "league", "lol", "dota",
    "cs2", "csgo", "counter-strike",
    "overwatch", "ow2",
    "ultima", "uo", "ultima online",
    "variety", "just chatting", "irl"
]

# =============================================================================
# UI SETTINGS
# =============================================================================
# Default window size for Tkinter UI
UI_WINDOW_WIDTH = 800
UI_WINDOW_HEIGHT = 600

# =============================================================================
# FUTURE FEATURES (documented for reference)
# =============================================================================
# Mid-stream game change re-posting:
#   - Detect when streamer changes game category
#   - Re-post to servers tagged for the new game
#   - Respect rate limit (don't spam same channel)
#   - Configurable: enabled/disabled per session
#
# Stream status integration:
#   - Detect stream title/game from Twitch/YouTube API
#   - Auto-select servers based on current game
#   - Update message content when stream info changes

