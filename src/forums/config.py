"""Explicit forum profiles and independent enablement flags."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Optional

from .models import optional_discord_id, require_discord_id

DEFAULT_PROFILES_PATH = "data/forums/profiles.json"
LEGACY_SHARED_DB_PATH = "data/forums/mirror.sqlite3"
VALID_PURPOSES = {"promotion", "external_project"}
VALID_ROUTING_MODES = {"reply_existing", "one_topic_per_streamer", "create_topic"}


def _maybe_migrate_legacy_mirror_db(
    legacy_path: Path,
    target_path: Path,
    guild_id: str,
    forum_id: str,
) -> None:
    """Safely copy legacy shared mirror DB to an isolated profile DB if its scope matches."""
    if not legacy_path.exists() or target_path.exists():
        return
    try:
        src = sqlite3.connect(str(legacy_path))
        src.row_factory = sqlite3.Row
        try:
            row = src.execute(
                "SELECT scope_key FROM mirror_meta WHERE guild_id = ? AND forum_id = ? LIMIT 1",
                (guild_id, forum_id),
            ).fetchone()
        finally:
            src.close()
        if row is None:
            return
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.tmp")
        src_conn = sqlite3.connect(str(legacy_path))
        dst_conn = sqlite3.connect(str(tmp_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()
        os.replace(tmp_path, target_path)
    except Exception:
        pass


@dataclass
class ForumProfile:
    """Configuration profile for a single Discord forum destination."""

    profile_id: str
    name: str
    guild_id: str
    forum_id: str
    source_url: str = ""
    purpose: str = "promotion"
    catalog_enabled: bool = False
    promotion_enabled: bool = False
    external_drafts_enabled: bool = False
    stage_enabled: bool = False
    routing_mode: str = "reply_existing"
    default_thread_id: Optional[str] = None
    cooldown_hours: float = 3.0
    requirement_refs: list[dict[str, str]] = field(default_factory=list)
    seed_index_path: Optional[str] = None
    transfer_root: str = "data/forums/transfer"
    db_path: str = ""
    exchange_dir: str = "data/forums/exchange"

    def __post_init__(self) -> None:
        if not self.profile_id or not isinstance(self.profile_id, str):
            raise ValueError("profile_id must be a non-empty string")
        if not self.name or not isinstance(self.name, str):
            raise ValueError("name must be a non-empty string")
        if self.purpose not in VALID_PURPOSES:
            raise ValueError(f"purpose must be one of {sorted(VALID_PURPOSES)}, got {self.purpose!r}")
        if self.routing_mode not in VALID_ROUTING_MODES:
            raise ValueError(
                f"routing_mode must be one of {sorted(VALID_ROUTING_MODES)}, got {self.routing_mode!r}"
            )
        require_discord_id(self.guild_id, "guild_id")
        require_discord_id(self.forum_id, "forum_id")
        optional_discord_id(self.default_thread_id, "default_thread_id")
        if not self.source_url:
            self.source_url = f"https://discord.com/channels/{self.guild_id}/{self.forum_id}"
        if not self.db_path:
            self.db_path = f"data/forums/{self.profile_id}.sqlite3"
        if self.purpose == "external_project" and self.promotion_enabled:
            raise ValueError("external_project profiles must never have promotion_enabled=True")
        if float(self.cooldown_hours) < 0:
            raise ValueError("cooldown_hours must be non-negative")

    def can_promote(self) -> bool:
        """Return True only if profile is a promotion profile with promotion_enabled=True."""
        return self.purpose == "promotion" and bool(self.promotion_enabled)

    def can_stage(self, source_kind: str = "external") -> bool:
        """Check whether staging is permitted for external or promotion drafts."""
        if not self.stage_enabled:
            return False
        if source_kind == "external":
            return bool(self.external_drafts_enabled)
        if source_kind == "promotion":
            return self.can_promote()
        return False

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-serializable dict representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ForumProfile":
        """Construct and validate a ForumProfile from a dict."""
        pid = raw["profile_id"]
        gid = raw["guild_id"]
        fid = raw["forum_id"]
        raw_db = raw.get("db_path")
        isolated_db = f"data/forums/{pid}.sqlite3"
        if not raw_db or raw_db == LEGACY_SHARED_DB_PATH:
            _maybe_migrate_legacy_mirror_db(
                Path(LEGACY_SHARED_DB_PATH),
                Path(isolated_db),
                str(gid),
                str(fid),
            )
            eff_db = isolated_db
        else:
            eff_db = str(raw_db)
        return cls(
            profile_id=pid,
            name=raw["name"],
            guild_id=gid,
            forum_id=fid,
            source_url=raw.get("source_url", ""),
            purpose=raw.get("purpose", "promotion"),
            catalog_enabled=bool(raw.get("catalog_enabled", False)),
            promotion_enabled=bool(raw.get("promotion_enabled", False)),
            external_drafts_enabled=bool(raw.get("external_drafts_enabled", False)),
            stage_enabled=bool(raw.get("stage_enabled", False)),
            routing_mode=raw.get("routing_mode", "reply_existing"),
            default_thread_id=raw.get("default_thread_id"),
            cooldown_hours=float(raw.get("cooldown_hours", 3.0)),
            requirement_refs=list(raw.get("requirement_refs", [])),
            seed_index_path=raw.get("seed_index_path"),
            transfer_root=raw.get("transfer_root", "data/forums/transfer"),
            db_path=eff_db,
            exchange_dir=raw.get("exchange_dir", "data/forums/exchange"),
        )


def load_forum_profiles(path: str | Path = DEFAULT_PROFILES_PATH) -> dict[str, ForumProfile]:
    """Load configured forum profiles from JSON file."""
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    profiles_raw = raw.get("profiles", {}) if isinstance(raw, dict) else {}
    return {pid: ForumProfile.from_dict(data) for pid, data in profiles_raw.items()}


def save_forum_profiles(
    profiles: dict[str, ForumProfile],
    path: str | Path = DEFAULT_PROFILES_PATH,
) -> Path:
    """Atomically write forum profiles to JSON file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"profiles": {pid: prof.to_dict() for pid, prof in sorted(profiles.items())}}
    tmp = p.parent / f".{p.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def get_forum_profile(
    profile_id: str,
    path: str | Path = DEFAULT_PROFILES_PATH,
) -> Optional[ForumProfile]:
    """Return a single configured ForumProfile by ID, or None if missing."""
    return load_forum_profiles(path).get(profile_id)


def upsert_forum_profile(
    profile: ForumProfile,
    path: str | Path = DEFAULT_PROFILES_PATH,
) -> ForumProfile:
    """Create or update a ForumProfile in profiles.json."""
    profiles = load_forum_profiles(path)
    profiles[profile.profile_id] = profile
    save_forum_profiles(profiles, path)
    return profile


def get_promotable_forum_profiles(
    path: str | Path = DEFAULT_PROFILES_PATH,
) -> list[ForumProfile]:
    """Return only promotion profiles with promotion_enabled=True."""
    return [p for p in load_forum_profiles(path).values() if p.can_promote()]
