"""Generic forum draft rendering and baseline conflict checks."""
from __future__ import annotations

from typing import Any, Optional

from .changes import make_record_id
from .config import ForumProfile
from .exchange import validate_exchange_record
from .models import build_discord_channel_url, utc_now_iso
from .store import ForumStore


def render_forum_promo_draft(
    *,
    profile: ForumProfile,
    stream_info: Any,
    draft_id: str,
    revision: int = 1,
    thread_id: Optional[str] = None,
    title: Optional[str] = None,
    tags: Optional[list[dict[str, Any]]] = None,
    actor: Optional[dict[str, Any]] = None,
    snapshot_id: Optional[str] = None,
    snapshot_revision: Optional[int] = None,
    requirements: Optional[list[dict[str, Any]]] = None,
    template: Optional[str] = None,
) -> dict[str, Any]:
    """Build a validated draft.request.v1 for a generic self-promotion forum."""
    if not profile.can_promote():
        raise ValueError(
            f"Profile {profile.profile_id!r} is not enabled for promotion (purpose={profile.purpose!r}, promotion_enabled={profile.promotion_enabled})"
        )

    stream_title = getattr(stream_info, "title", "") or "Live Stream"
    stream_game = getattr(stream_info, "game", "") or ""
    stream_url = getattr(stream_info, "stream_url", "") or ""

    if template:
        body = template.format(
            title=stream_title,
            game=stream_game,
            url=stream_url,
            platform=getattr(stream_info, "platform", ""),
            username=getattr(stream_info, "username", ""),
        )
    else:
        lines = [f"Live now: {stream_title}"]
        if stream_game:
            lines.append(f"Game: {stream_game}")
        if stream_url:
            lines.append(stream_url)
        body = "\n".join(lines)

    eff_thread = thread_id or profile.default_thread_id
    if profile.routing_mode == "create_topic" and not eff_thread:
        action = "create_topic"
        eff_title = title or stream_title
        source_url = profile.source_url
    else:
        if not eff_thread:
            raise ValueError(
                f"Forum profile {profile.profile_id!r} requires an existing thread_id for routing_mode={profile.routing_mode!r}"
            )
        action = "reply_existing"
        eff_title = None
        source_url = build_discord_channel_url(profile.guild_id, eff_thread)

    record = {
        "schema": "draft.request.v1",
        "record_id": make_record_id("draft"),
        "producer_id": "discord-promo-helper",
        "created_at": utc_now_iso(),
        "scope": {
            "guild_id": profile.guild_id,
            "forum_id": profile.forum_id,
            "actor": actor or {"user_id": None, "label": "Selected account"},
        },
        "draft_id": draft_id,
        "revision": revision,
        "action": action,
        "destination": {
            "guild_id": profile.guild_id,
            "forum_id": profile.forum_id,
            "thread_id": None if action == "create_topic" else eff_thread,
            "message_id": None,
            "source_url": source_url,
        },
        "body": body,
        "title": eff_title,
        "tags": list(tags or []),
        "evidence": [],
        "baseline": {
            "snapshot_id": snapshot_id,
            "snapshot_revision": snapshot_revision,
            "requirements": list(requirements or []),
            "message_content_hash": None,
        },
    }
    return validate_exchange_record(record)


def check_draft_conflicts(draft_record: dict[str, Any], store: ForumStore) -> list[str]:
    """Compare draft baseline against current mirror requirements and target message."""
    conflicts: list[str] = []
    baseline = draft_record.get("baseline", {})
    status = store.get_forum_status()

    # Check requirement revisions
    snap = store.export_snapshot()
    req_by_id = {r["requirement_id"]: r for r in snap["requirements"]}
    for req_ref in baseline.get("requirements", []):
        rid = req_ref["requirement_id"]
        base_rev = int(req_ref["revision"])
        curr = req_by_id.get(rid)
        if curr and int(curr["revision"]) > base_rev:
            conflicts.append(
                f"requirement {rid} changed from baseline r{base_rev} to r{curr['revision']}"
            )

    # Check if edit_own_message target has changed since baseline
    if draft_record["action"] == "edit_own_message":
        target_mid = draft_record["destination"]["message_id"]
        base_hash = baseline.get("message_content_hash")
        for m in snap["messages"]:
            if m.get("message_id") == target_mid and base_hash and m["content_hash"] != base_hash:
                conflicts.append(
                    f"target message {target_mid} content_hash changed since baseline"
                )
                break

    draft_row = store.get_draft_row(draft_record["draft_id"], draft_record["revision"])
    if draft_row and draft_row.get("needs_recheck"):
        reason = draft_row.get("recheck_reason") or "flagged for recheck"
        if reason not in conflicts:
            conflicts.append(reason)

    _ = status
    return conflicts
