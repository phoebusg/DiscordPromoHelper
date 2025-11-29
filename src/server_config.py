"""Server configuration storage for friendly names, channels, and game tags.

server_config.json format:
{
    "servers": {
        "ocr_name_key": {
            "ocr_name": "T900fficial Discord",
            "friendly_name": "T90 Official Discord",
            "promo_channels": ["self-promo", "share-your-work"],
            "game_tags": ["aoe2", "age of empires"],
            "enabled": true,
            "notes": "User notes about this server"
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
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Dict, List, Optional, Any
from difflib import SequenceMatcher

CONFIG_FILE = "server_config.json"


def _normalize_key(name: str) -> str:
    """Create a stable key from OCR name (lowercase, strip, collapse spaces)."""
    return " ".join(name.lower().split())


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load server configuration from JSON file."""
    p = Path(path or CONFIG_FILE)
    if not p.exists():
        return {
            "servers": {},
            "game_filters": {},
            "settings": {
                "rate_limit_hours": 3,
                "default_enabled": True
            }
        }
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"servers": {}, "game_filters": {}, "settings": {"rate_limit_hours": 3, "default_enabled": True}}


def save_config(config: Dict[str, Any], path: Optional[str] = None) -> None:
    """Save server configuration to JSON file."""
    p = Path(path or CONFIG_FILE)
    p.write_text(json.dumps(config, indent=2))


def get_server_config(ocr_name: str, config: Optional[Dict] = None) -> Dict[str, Any]:
    """Get configuration for a server by its OCR name."""
    if config is None:
        config = load_config()
    
    key = _normalize_key(ocr_name)
    servers = config.get("servers", {})
    
    if key in servers:
        return servers[key]
    
    # Return default config if not found
    return {
        "ocr_name": ocr_name,
        "friendly_name": "",  # Empty means use OCR name
        "promo_channels": [],
        "game_tags": [],
        "enabled": config.get("settings", {}).get("default_enabled", True),
        "notes": ""
    }


def set_server_config(
    ocr_name: str,
    friendly_name: Optional[str] = None,
    promo_channels: Optional[List[str]] = None,
    game_tags: Optional[List[str]] = None,
    enabled: Optional[bool] = None,
    notes: Optional[str] = None,
    config: Optional[Dict] = None,
    save: bool = True
) -> Dict[str, Any]:
    """Update configuration for a server."""
    if config is None:
        config = load_config()
    
    key = _normalize_key(ocr_name)
    servers = config.setdefault("servers", {})
    
    # Get existing or create new
    server = servers.get(key, {
        "ocr_name": ocr_name,
        "friendly_name": "",
        "promo_channels": [],
        "game_tags": [],
        "enabled": True,
        "notes": ""
    })
    
    # Update fields if provided
    if friendly_name is not None:
        server["friendly_name"] = friendly_name
    if promo_channels is not None:
        server["promo_channels"] = promo_channels
    if game_tags is not None:
        server["game_tags"] = game_tags
    if enabled is not None:
        server["enabled"] = enabled
    if notes is not None:
        server["notes"] = notes
    
    servers[key] = server
    
    if save:
        save_config(config)
    
    return server


def get_display_name(ocr_name: str, config: Optional[Dict] = None) -> str:
    """Get the display name for a server (friendly name or OCR name fallback)."""
    server_cfg = get_server_config(ocr_name, config)
    friendly = server_cfg.get("friendly_name", "")
    return friendly if friendly else ocr_name


def get_servers_by_game(game_tag: str, config: Optional[Dict] = None) -> List[Dict[str, Any]]:
    """Get all servers that match a specific game tag."""
    if config is None:
        config = load_config()
    
    game_tag_lower = game_tag.lower()
    matching = []
    
    for key, server in config.get("servers", {}).items():
        tags = [t.lower() for t in server.get("game_tags", [])]
        if game_tag_lower in tags:
            matching.append(server)
    
    return matching


def get_enabled_servers(config: Optional[Dict] = None) -> List[Dict[str, Any]]:
    """Get all enabled servers."""
    if config is None:
        config = load_config()
    
    return [
        server for server in config.get("servers", {}).values()
        if server.get("enabled", True)
    ]


def get_rate_limit_hours(config: Optional[Dict] = None) -> int:
    """Get the rate limit in hours from settings."""
    if config is None:
        config = load_config()
    return config.get("settings", {}).get("rate_limit_hours", 3)


def set_rate_limit_hours(hours: int, config: Optional[Dict] = None) -> None:
    """Set the rate limit in hours."""
    if config is None:
        config = load_config()
    config.setdefault("settings", {})["rate_limit_hours"] = hours
    save_config(config)


def import_from_servers_json(servers_json_path: str = "servers.json", config_path: Optional[str] = None) -> int:
    """Import servers from servers.json (scan output) into server_config.json.
    
    Returns the number of new servers imported.
    Handles both list format (from scan) and dict format with "servers" key.
    Now includes icon_hash for reliable server identification.
    """
    servers_path = Path(servers_json_path)
    if not servers_path.exists():
        return 0
    
    try:
        servers_data = json.loads(servers_path.read_text())
    except Exception:
        return 0
    
    config = load_config(config_path)
    
    # Handle both list format and dict format
    if isinstance(servers_data, list):
        servers_list = servers_data
    else:
        servers_list = servers_data.get("servers", [])
    
    imported = 0
    updated = 0
    
    for server in servers_list:
        ocr_name = server.get("name", "")
        icon_hash = server.get("icon_hash", "")
        
        if not ocr_name and not icon_hash:
            continue
        
        # Use icon_hash as primary key if available, else fall back to name
        if icon_hash:
            key = f"icon_{icon_hash[:16]}"
        else:
            key = _normalize_key(ocr_name)
        
        existing = config.get("servers", {}).get(key)
        
        if existing is None:
            # New server - add with defaults
            config.setdefault("servers", {})[key] = {
                "ocr_name": ocr_name or f"Unknown ({icon_hash[:8]})",
                "friendly_name": "",
                "promo_channels": [],
                "game_tags": [],
                "enabled": True,
                "notes": "",
                "icon_hash": icon_hash,
                "scan_index": server.get("index", -1)
            }
            imported += 1
        else:
            # Existing server - update icon_hash if not set
            if icon_hash and not existing.get("icon_hash"):
                existing["icon_hash"] = icon_hash
                updated += 1
            # Update OCR name if we got a better one
            if ocr_name and (not existing.get("ocr_name") or existing.get("ocr_name", "").startswith("Unknown")):
                existing["ocr_name"] = ocr_name
                updated += 1
    
    save_config(config, config_path)
    return imported


def find_similar_servers(name: str, config: Optional[Dict] = None, threshold: float = 0.6) -> List[Dict]:
    """Find servers with names similar to the given name.
    
    Useful for detecting if a newly OCR'd name might be a duplicate.
    """
    if config is None:
        config = load_config()
    
    similar = []
    name_lower = name.lower()
    
    for key, server in config.get("servers", {}).items():
        ocr = server.get("ocr_name", "").lower()
        friendly = server.get("friendly_name", "").lower()
        
        # Check similarity against both names
        ratio_ocr = SequenceMatcher(None, name_lower, ocr).ratio()
        ratio_friendly = SequenceMatcher(None, name_lower, friendly).ratio() if friendly else 0
        
        best_ratio = max(ratio_ocr, ratio_friendly)
        if best_ratio >= threshold:
            similar.append({
                "server": server,
                "similarity": best_ratio
            })
    
    # Sort by similarity descending
    similar.sort(key=lambda x: x["similarity"], reverse=True)
    return similar


def clean_and_dedupe_servers(config_path: Optional[str] = None) -> Dict[str, int]:
    """Clean up server config by removing duplicates and invalid entries.
    
    Removes:
    - Entries with no icon_hash (old cruft before hash-based tracking)
    - Entries with no valid name AND no icon_hash
    - Near-duplicate entries (very similar icon_hash, distance <= 2)
    - Entries with "Unknown" names that have duplicates with real names
    
    Returns dict with counts: {'removed_invalid', 'removed_duplicates', 'removed_no_hash', 'total_remaining'}
    """
    config = load_config(config_path)
    servers = config.get("servers", {})
    
    # Try to import hash distance function
    try:
        from src.utils import icon_hash_distance
    except ImportError:
        try:
            from utils import icon_hash_distance
        except ImportError:
            icon_hash_distance = None
    
    stats = {
        'removed_invalid': 0,
        'removed_duplicates': 0,
        'removed_no_hash': 0,
        'cleaned_names': 0,
        'total_before': len(servers),
        'total_remaining': 0
    }
    
    keys_to_remove = set()
    
    # First pass: remove entries without icon_hash (old cruft)
    for key, server in servers.items():
        icon_hash = server.get("icon_hash", "") or ""
        
        if not icon_hash:
            keys_to_remove.add(key)
            stats['removed_no_hash'] += 1
            continue
        
        # Check for obviously invalid entries
        ocr_name = server.get("ocr_name", "") or ""
        friendly_name = server.get("friendly_name", "") or ""
        
        # Garbage patterns
        garbage_patterns = ['|||', '___', '...', '???', 'null', 'none', 'error']
        if any(p in ocr_name.lower() for p in garbage_patterns) and not friendly_name:
            keys_to_remove.add(key)
            stats['removed_invalid'] += 1
    
    # Second pass: find near-duplicate hashes (distance <= 2)
    remaining = {k: v for k, v in servers.items() if k not in keys_to_remove}
    
    # Build list of (key, hash) pairs
    hash_entries = [(k, v.get('icon_hash', '')) for k, v in remaining.items() if v.get('icon_hash')]
    
    # Track which hashes we've decided to keep
    kept_hashes = {}  # hash -> key
    
    for key, icon_hash in hash_entries:
        if key in keys_to_remove:
            continue
            
        # Check if this hash is near-duplicate of a kept hash
        is_near_dup = False
        dup_of_key = None
        
        if icon_hash_distance:
            for kept_hash, kept_key in kept_hashes.items():
                dist = icon_hash_distance(icon_hash, kept_hash)
                if dist <= 2:  # Very strict - only near-identical icons
                    is_near_dup = True
                    dup_of_key = kept_key
                    break
        else:
            # Fallback: exact match only
            if icon_hash in kept_hashes:
                is_near_dup = True
                dup_of_key = kept_hashes[icon_hash]
        
        if is_near_dup:
            # Decide which to keep: prefer one with friendly_name or better ocr_name
            current = remaining[key]
            existing = remaining.get(dup_of_key, {})
            
            current_has_friendly = bool(current.get("friendly_name"))
            existing_has_friendly = bool(existing.get("friendly_name"))
            current_is_unknown = (current.get("ocr_name") or "").lower().startswith("unknown")
            existing_is_unknown = (existing.get("ocr_name") or "").lower().startswith("unknown")
            
            # Keep current if it's better
            if (current_has_friendly and not existing_has_friendly) or \
               (not current_is_unknown and existing_is_unknown):
                # Remove the existing one, keep current
                keys_to_remove.add(dup_of_key)
                kept_hashes[icon_hash] = key
                # Remove old hash entry
                for h, k in list(kept_hashes.items()):
                    if k == dup_of_key:
                        del kept_hashes[h]
                        break
            else:
                # Keep existing, remove current
                keys_to_remove.add(key)
            
            stats['removed_duplicates'] += 1
        else:
            # No duplicate, keep this one
            kept_hashes[icon_hash] = key
    
    # Build cleaned config
    cleaned_servers = {}
    for key, server in servers.items():
        if key in keys_to_remove:
            continue
        
        # Clean up the entry
        cleaned = server.copy()
        cleaned.setdefault("ocr_name", "")
        cleaned.setdefault("friendly_name", "")
        cleaned.setdefault("promo_channels", [])
        cleaned.setdefault("game_tags", [])
        cleaned.setdefault("enabled", True)
        cleaned.setdefault("notes", "")
        cleaned.setdefault("icon_hash", "")
        
        # Strip whitespace from names
        if cleaned["ocr_name"]:
            new_name = cleaned["ocr_name"].strip()
            if new_name != cleaned["ocr_name"]:
                stats['cleaned_names'] += 1
            cleaned["ocr_name"] = new_name
        
        if cleaned["friendly_name"]:
            cleaned["friendly_name"] = cleaned["friendly_name"].strip()
        
        cleaned_servers[key] = cleaned
    
    # Save cleaned config
    config["servers"] = cleaned_servers
    save_config(config, config_path)
    
    stats['total_remaining'] = len(cleaned_servers)
    
    return stats
