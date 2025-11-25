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
    """
    servers_path = Path(servers_json_path)
    if not servers_path.exists():
        return 0
    
    try:
        servers_data = json.loads(servers_path.read_text())
    except Exception:
        return 0
    
    config = load_config(config_path)
    servers_list = servers_data.get("servers", [])
    
    imported = 0
    for server in servers_list:
        ocr_name = server.get("name", "")
        if not ocr_name:
            continue
        
        key = _normalize_key(ocr_name)
        if key not in config.get("servers", {}):
            # New server - add with defaults
            set_server_config(
                ocr_name=ocr_name,
                config=config,
                save=False
            )
            imported += 1
    
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
