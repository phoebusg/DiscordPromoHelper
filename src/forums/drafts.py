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
    """Compare draft baseline against current mirror requirements and target entities."""
    conflicts: list[str] = []
    baseline = draft_record.get("baseline") or {}
    scope = draft_record.get("scope") or {}
    dest = draft_record.get("destination") or {}

    try:
        status = store.get_forum_status(
            guild_id=scope.get("guild_id"),
            forum_id=scope.get("forum_id"),
        )
        snap = store.export_snapshot(
            guild_id=scope.get("guild_id"),
            forum_id=scope.get("forum_id"),
        )
    except ValueError:
        return ["local mirror scope is not initialized"]

    if status.get("guild_id") != scope.get("guild_id") or status.get("forum_id") != scope.get("forum_id"):
        conflicts.append(
            f"mirror scope mismatch: draft=({scope.get('guild_id')}, {scope.get('forum_id')}), "
            f"mirror=({status.get('guild_id')}, {status.get('forum_id')})"
        )
        return conflicts

    counts = status.get("counts") or {}
    cursor_pos = str((status.get("cursor") or {}).get("position") or "0")
    if (
        int(counts.get("topics") or 0) == 0
        and int(counts.get("messages") or 0) == 0
        and int(counts.get("requirements") or 0) == 0
        and cursor_pos == "0"
    ):
        conflicts.append("local mirror has no established baseline state")

    # Verify named snapshot identity and anchored snapshot revision.
    base_snap_id = baseline.get("snapshot_id")
    curr_snap_id = status.get("snapshot_id")
    if not base_snap_id:
        conflicts.append("draft baseline missing snapshot_id")
    elif base_snap_id != curr_snap_id:
        conflicts.append(
            f"snapshot_id mismatch: baseline={base_snap_id!r}, current={curr_snap_id!r}"
        )

    base_snap_rev = baseline.get("snapshot_revision")
    curr_snap_rev = int(status.get("snapshot_revision") or 0)
    if base_snap_rev is None:
        conflicts.append("draft baseline missing snapshot_revision")
    else:
        b_rev = int(base_snap_rev)
        if b_rev < 1 or b_rev > curr_snap_rev:
            conflicts.append(
                f"snapshot_revision invalid or ahead of mirror: baseline={b_rev}, current={curr_snap_rev}"
            )

    # Every referenced requirement revision must exist in the mirror and match the baseline.
    req_by_id = {r["requirement_id"]: r for r in snap.get("requirements", [])}
    for req_ref in baseline.get("requirements", []):
        rid = req_ref["requirement_id"]
        base_rev = int(req_ref["revision"])
        base_req_hash = req_ref.get("content_hash")
        curr = req_by_id.get(rid)
        if curr is None:
            conflicts.append(
                f"requirement {rid} missing from local mirror (expected baseline r{base_rev})"
            )
            continue
        curr_rev = int(curr["revision"])
        if curr_rev != base_rev:
            conflicts.append(
                f"requirement {rid} revision mismatch: baseline r{base_rev}, current r{curr_rev}"
            )
        elif base_req_hash is not None and curr.get("content_hash") != base_req_hash:
            conflicts.append(
                f"requirement {rid} content_hash mismatch at r{curr_rev}"
            )

    # Target topic availability check for reply_existing / edit_own_message.
    action = draft_record.get("action")
    target_tid = dest.get("thread_id")
    topic_by_id = {t["thread_id"]: t for t in snap.get("topics", [])}
    if action in {"reply_existing", "edit_own_message"}:
        if not target_tid:
            conflicts.append(f"{action} missing destination.thread_id")
        else:
            topic_obj = topic_by_id.get(target_tid)
            if topic_obj is None:
                conflicts.append(f"target topic {target_tid} missing from local mirror")
            else:
                if topic_obj.get("archived") is True:
                    conflicts.append(f"target topic {target_tid} is archived")
                if topic_obj.get("locked") is True:
                    conflicts.append(f"target topic {target_tid} is locked")

    # Exact target message verification for edit_own_message.
    if action == "edit_own_message":
        target_mid = dest.get("message_id")
        base_hash = baseline.get("message_content_hash")
        msg_by_id = {
            m["message_id"]: m for m in snap.get("messages", []) if m.get("message_id") is not None
        }
        if not target_mid:
            conflicts.append("edit_own_message missing destination.message_id")
        elif target_mid not in msg_by_id:
            conflicts.append(f"target message {target_mid} missing from local mirror")
        else:
            curr_msg = msg_by_id[target_mid]
            if curr_msg.get("thread_id") != target_tid:
                conflicts.append(
                    f"target message {target_mid} belongs to thread {curr_msg.get('thread_id')}, expected {target_tid}"
                )
            if not base_hash:
                conflicts.append(
                    f"edit_own_message for {target_mid} missing baseline.message_content_hash"
                )
            elif curr_msg.get("content_hash") != base_hash:
                conflicts.append(
                    f"target message {target_mid} content_hash changed since baseline"
                )

    draft_row = store.get_draft_row(draft_record["draft_id"], draft_record["revision"])
    if draft_row and draft_row.get("needs_recheck"):
        reason = draft_row.get("recheck_reason") or "flagged for recheck"
        if reason not in conflicts:
            conflicts.append(reason)

    return conflicts
