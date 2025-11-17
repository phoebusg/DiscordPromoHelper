"""Simple JSON-backed storage for per-channel last-post timestamps."""
from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional
from .config import PERSISTENCE_FILE, POST_COOLDOWN_MINUTES
from .config import CHANNEL_WHITELIST, CHANNEL_BLACKLIST, PERSISTENCE_CLEANUP_DAYS


def _ensure_data_dir(path: str) -> Path:
    p = Path(path)
    if not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_timestamps(path: Optional[str] = None) -> Dict[str, str]:
    path = path or PERSISTENCE_FILE
    p = _ensure_data_dir(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save_timestamps(data: Dict[str, str], path: Optional[str] = None) -> None:
    path = path or PERSISTENCE_FILE
    p = _ensure_data_dir(path)
    p.write_text(json.dumps(data, indent=2))


def cleanup_old_entries(path: Optional[str] = None, days: Optional[int] = None) -> None:
    """Remove entries older than `days` (default from config)."""
    path = path or PERSISTENCE_FILE
    days = days if days is not None else PERSISTENCE_CLEANUP_DAYS
    p = _ensure_data_dir(path)
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text())
    except Exception:
        return
    cutoff = datetime.now() - timedelta(days=days)
    changed = False
    keys = list(data.keys())
    for k in keys:
        try:
            dt = datetime.fromisoformat(data[k])
            if dt < cutoff:
                del data[k]
                changed = True
        except Exception:
            # If parsing fails, remove entry to keep data clean
            del data[k]
            changed = True
    if changed:
        save_timestamps(data, path)


def is_channel_allowed(channel_name: str) -> bool:
    """Check whitelist/blacklist rules for a given channel name."""
    name = (channel_name or "").strip()
    if CHANNEL_WHITELIST:
        return name in CHANNEL_WHITELIST
    if CHANNEL_BLACKLIST:
        return name not in CHANNEL_BLACKLIST
    return True


def can_post(channel_key: str, cooldown_minutes: Optional[int] = None, path: Optional[str] = None) -> bool:
    cooldown = cooldown_minutes if cooldown_minutes is not None else POST_COOLDOWN_MINUTES
    data = load_timestamps(path)
    last = data.get(channel_key)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except Exception:
        return True
    if datetime.now() - last_dt >= timedelta(minutes=cooldown):
        return True
    return False


def update_timestamp(channel_key: str, path: Optional[str] = None) -> None:
    data = load_timestamps(path)
    data[channel_key] = datetime.now().isoformat()
    save_timestamps(data, path)
