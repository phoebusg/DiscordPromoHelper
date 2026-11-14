"""Core identifiers, hashing, timestamp helpers, and coverage structures."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from typing import Any, Optional

_DISCORD_CHANNEL_URL_RE = re.compile(
    r"(?:https://(?:(?:ptb|canary)\.)?discord\.com|discord://-)/channels/(\d+)/(\d+)(?:/(\d+))?"
)


def is_valid_discord_id(val: Any) -> bool:
    """Return True iff val is a non-empty decimal string (never int/float)."""
    return isinstance(val, str) and len(val) > 0 and val.isascii() and val.isdigit()


def require_discord_id(val: Any, field_name: str = "id") -> str:
    """Validate and return a canonical decimal string Discord ID."""
    if not is_valid_discord_id(val):
        raise ValueError(f"{field_name} must be a non-empty decimal string, got {val!r}")
    return val


def optional_discord_id(val: Any, field_name: str = "id") -> Optional[str]:
    """Return None if val is None, otherwise validate as canonical decimal string."""
    if val is None:
        return None
    return require_discord_id(val, field_name)


def normalize_text_for_hash(text: str) -> str:
    """Normalize CRLF/CR to LF while preserving other whitespace, punctuation, and Unicode."""
    if not isinstance(text, str):
        raise ValueError(f"text must be a string, got {type(text).__name__}")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def compute_content_hash(text: str) -> str:
    """Compute lowercase SHA-256 hex digest of UTF-8 text with CRLF normalized to LF."""
    normalized = normalize_text_for_hash(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def compute_bytes_sha256(data: bytes) -> str:
    """Compute lowercase SHA-256 hex digest of raw bytes without text normalization."""
    return hashlib.sha256(data).hexdigest()


def utc_now_iso() -> str:
    """Return current UTC timestamp in RFC 3339 format with microsecond precision and Z suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_rfc3339_utc(val: Any) -> Optional[datetime]:
    """Parse an RFC 3339 UTC timestamp string (with or without microseconds) into an aware datetime."""
    if not isinstance(val, str) or not val.strip():
        return None
    raw = val.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def is_valid_rfc3339_utc(val: Any) -> bool:
    """Validate an RFC 3339 UTC timestamp string ending in 'Z'."""
    if not isinstance(val, str) or not val.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(val[:-1] + "+00:00")
        return True
    except ValueError:
        return False


def parse_discord_channel_url(url: Optional[str]) -> Optional[tuple[str, str, Optional[str]]]:
    """Parse (guild_id, channel_or_thread_id, message_id) from a Discord web or desktop URL."""
    if not url or not isinstance(url, str):
        return None
    match = _DISCORD_CHANNEL_URL_RE.search(url)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def build_discord_channel_url(
    guild_id: str,
    channel_or_thread_id: str,
    message_id: Optional[str] = None,
) -> str:
    """Build a canonical https://discord.com/channels/... link."""
    gid = require_discord_id(guild_id, "guild_id")
    cid = require_discord_id(channel_or_thread_id, "channel_or_thread_id")
    if message_id is not None:
        mid = require_discord_id(message_id, "message_id")
        return f"https://discord.com/channels/{gid}/{cid}/{mid}"
    return f"https://discord.com/channels/{gid}/{cid}"


def build_server_scan_coverage(
    servers: list[dict[str, Any]],
    stop_reason: str = "unknown",
    pages_scanned: int = 0,
) -> dict[str, Any]:
    """Structured coverage manifest for legacy sidebar server scans.

    A sidebar icon scan never proves forum or message history completeness.
    """
    return {
        "scope": "sidebar_server_icons",
        "servers_collected": len(servers),
        "pages_scanned": int(pages_scanned),
        "stop_reason": stop_reason,
        "forum_complete": False,
        "message_histories_complete": False,
        "captured_at": utc_now_iso(),
    }
