# config.py
CHANNEL_KEYWORDS = ["self-promo", "promotion", "advertise"]

# Posting cooldown in minutes (default: 60 minutes per channel)
POST_COOLDOWN_MINUTES = 60

# Persistence file for last-post timestamps
PERSISTENCE_FILE = "data/last_post_timestamps.json"

# Optional explicit whitelist/blacklist for channels (names). If whitelist non-empty, only those channels are allowed.
CHANNEL_WHITELIST = []
CHANNEL_BLACKLIST = []

# Cleanup persistence entries older than N days
PERSISTENCE_CLEANUP_DAYS = 30

