"""Shared v1 exchange contract validation and atomic JSON file I/O."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Optional

from .models import (
    compute_bytes_sha256,
    compute_content_hash,
    is_valid_discord_id,
    is_valid_rfc3339_utc,
    optional_discord_id,
    require_discord_id,
)

VALID_SCHEMAS = {
    "forum.snapshot.v1",
    "forum.changes.v1",
    "draft.request.v1",
    "handoff.receipt.v1",
}
VALID_COVERAGE_STATES = {"not_started", "partial", "complete"}
VALID_ENTITY_TYPES = {"forum", "topic", "message", "requirement"}
VALID_CHANGE_KINDS = {
    "added",
    "edited",
    "requirements_changed",
    "tags_changed",
    "state_changed",
    "attachment_changed",
    "removal_observed",
    "recognition_corrected",
}
VALID_DRAFT_ACTIONS = {"reply_existing", "edit_own_message", "create_topic"}
VALID_RECEIPT_STATUSES = {
    "staged",
    "in_discord_review",
    "submission_observed",
    "user_confirmed_submitted",
    "needs_review",
    "failed",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_CANONICAL_CHANNEL_URL_RE = re.compile(
    r"^(?:https://(?:(?:ptb|canary)\.)?discord\.com|discord://-)/channels/(\d+)/(\d+)(?:/(\d+))?/?$"
)


def canonical_record_digest(record: Any) -> str:
    """Return canonical lowercase SHA-256 hex digest of an exchange record."""
    encoded = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _parse_canonical_discord_channel_url(url: str, context: str) -> tuple[str, str, Optional[str]]:
    if not isinstance(url, str) or not url.strip():
        raise ContractValidationError(f"{context} must be a canonical Discord channel URL, got {url!r}")
    match = _CANONICAL_CHANNEL_URL_RE.fullmatch(url.strip())
    if not match:
        raise ContractValidationError(
            f"{context} must be a canonical Discord channel URL (/channels/<guild_id>/<channel_id>), got {url!r}"
        )
    return match.group(1), match.group(2), match.group(3)


class ContractValidationError(ValueError):
    """Raised when an exchange record violates docs/FORUM_EXCHANGE_V1.md."""


class RecordConflictError(ValueError):
    """Raised when the same record_id arrives with different content."""


def _require_dict(obj: Any, name: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ContractValidationError(f"{name} must be an object, got {type(obj).__name__}")
    return obj


def _require_list(obj: Any, name: str) -> list[Any]:
    if not isinstance(obj, list):
        raise ContractValidationError(f"{name} must be an array, got {type(obj).__name__}")
    return obj


def _require_non_empty_str(obj: Any, name: str) -> str:
    if not isinstance(obj, str) or not obj.strip():
        raise ContractValidationError(f"{name} must be a non-empty string, got {obj!r}")
    return obj


def _require_str(obj: Any, name: str) -> str:
    if not isinstance(obj, str):
        raise ContractValidationError(f"{name} must be a string, got {type(obj).__name__}")
    return obj


def _optional_str(obj: Any, name: str) -> Optional[str]:
    if obj is None:
        return None
    return _require_str(obj, name)


def _optional_bool(obj: Any, name: str) -> Optional[bool]:
    if obj is None:
        return None
    if not isinstance(obj, bool):
        raise ContractValidationError(f"{name} must be a boolean or null, got {obj!r}")
    return obj


def _require_bool(obj: Any, name: str) -> bool:
    if not isinstance(obj, bool):
        raise ContractValidationError(f"{name} must be a boolean, got {obj!r}")
    return obj


def _require_positive_int(obj: Any, name: str) -> int:
    if isinstance(obj, bool) or not isinstance(obj, int) or obj < 1:
        raise ContractValidationError(f"{name} must be a positive integer, got {obj!r}")
    return obj


def _optional_positive_int(obj: Any, name: str) -> Optional[int]:
    if obj is None:
        return None
    return _require_positive_int(obj, name)


def _require_utc_timestamp(obj: Any, name: str) -> str:
    if not is_valid_rfc3339_utc(obj):
        raise ContractValidationError(f"{name} must be an RFC 3339 UTC string ending with Z, got {obj!r}")
    return obj


def _optional_utc_timestamp(obj: Any, name: str) -> Optional[str]:
    if obj is None:
        return None
    return _require_utc_timestamp(obj, name)


def _require_sha256(obj: Any, name: str) -> str:
    if not isinstance(obj, str) or not _SHA256_RE.match(obj):
        raise ContractValidationError(f"{name} must be a 64-char lowercase hex SHA-256, got {obj!r}")
    return obj


def _optional_sha256(obj: Any, name: str) -> Optional[str]:
    if obj is None:
        return None
    return _require_sha256(obj, name)


def _validate_keys_present(obj: dict[str, Any], required: set[str], context: str) -> None:
    missing = required - set(obj.keys())
    if missing:
        raise ContractValidationError(f"{context} missing required keys: {sorted(missing)}")


def _validate_actor(actor: Any, context: str) -> None:
    d = _require_dict(actor, context)
    _validate_keys_present(d, {"user_id", "label"}, context)
    try:
        optional_discord_id(d["user_id"], f"{context}.user_id")
    except ValueError as exc:
        raise ContractValidationError(str(exc)) from exc
    _require_str(d["label"], f"{context}.label")


def _validate_cursor(cursor: Any, context: str) -> None:
    d = _require_dict(cursor, context)
    _validate_keys_present(d, {"stream_id", "position"}, context)
    _require_non_empty_str(d["stream_id"], f"{context}.stream_id")
    _require_non_empty_str(d["position"], f"{context}.position")


def _validate_tag(tag: Any, context: str) -> None:
    d = _require_dict(tag, context)
    _validate_keys_present(d, {"tag_id", "name"}, context)
    try:
        optional_discord_id(d["tag_id"], f"{context}.tag_id")
    except ValueError as exc:
        raise ContractValidationError(str(exc)) from exc
    _require_non_empty_str(d["name"], f"{context}.name")


def _validate_attachment(att: Any, context: str) -> None:
    d = _require_dict(att, context)
    _validate_keys_present(d, {"attachment_id", "name", "source_url", "sha256", "local_ref"}, context)
    try:
        optional_discord_id(d["attachment_id"], f"{context}.attachment_id")
    except ValueError as exc:
        raise ContractValidationError(str(exc)) from exc
    _require_non_empty_str(d["name"], f"{context}.name")
    _optional_str(d["source_url"], f"{context}.source_url")
    _optional_sha256(d["sha256"], f"{context}.sha256")
    _optional_str(d["local_ref"], f"{context}.local_ref")


def _validate_destination(dest: Any, scope: dict[str, Any], context: str) -> dict[str, Any]:
    d = _require_dict(dest, context)
    _validate_keys_present(d, {"guild_id", "forum_id", "thread_id", "message_id", "source_url"}, context)
    try:
        gid = require_discord_id(d["guild_id"], f"{context}.guild_id")
        fid = require_discord_id(d["forum_id"], f"{context}.forum_id")
        tid = optional_discord_id(d["thread_id"], f"{context}.thread_id")
        mid = optional_discord_id(d["message_id"], f"{context}.message_id")
    except ValueError as exc:
        raise ContractValidationError(str(exc)) from exc
    if gid != scope["guild_id"] or fid != scope["forum_id"]:
        raise ContractValidationError(
            f"{context} guild_id/forum_id ({gid}/{fid}) must agree with scope ({scope['guild_id']}/{scope['forum_id']})"
        )
    surl = _optional_str(d["source_url"], f"{context}.source_url")
    if surl is not None:
        url_gid, url_cid, url_mid = _parse_canonical_discord_channel_url(surl, f"{context}.source_url")
        if url_gid != gid:
            raise ContractValidationError(
                f"{context}.source_url guild_id {url_gid!r} must match destination.guild_id {gid!r}"
            )
        expected_cid = tid if tid is not None else fid
        if url_cid != expected_cid:
            raise ContractValidationError(
                f"{context}.source_url target id {url_cid!r} must match destination target {expected_cid!r}"
            )
        if url_mid is not None and (mid is None or url_mid != mid):
            raise ContractValidationError(
                f"{context}.source_url message_id {url_mid!r} must match destination.message_id {mid!r}"
            )
    return d


def _validate_forum(forum: Any, scope_guild_id: str, scope_forum_id: str, context: str) -> str:
    d = _require_dict(forum, context)
    _validate_keys_present(d, {"forum_id", "name", "source_url", "tags", "revision", "observed_at"}, context)
    try:
        fid = require_discord_id(d["forum_id"], f"{context}.forum_id")
    except ValueError as exc:
        raise ContractValidationError(str(exc)) from exc
    if fid != scope_forum_id:
        raise ContractValidationError(f"{context}.forum_id must match scope.forum_id")
    _require_non_empty_str(d["name"], f"{context}.name")
    furl = _require_non_empty_str(d["source_url"], f"{context}.source_url")
    url_gid, url_cid, url_mid = _parse_canonical_discord_channel_url(furl, f"{context}.source_url")
    if url_gid != scope_guild_id or url_cid != fid or url_mid is not None:
        raise ContractValidationError(
            f"{context}.source_url must identify forum {scope_guild_id}/{fid}, got {furl!r}"
        )
    for idx, t in enumerate(_require_list(d["tags"], f"{context}.tags")):
        _validate_tag(t, f"{context}.tags[{idx}]")
    _require_positive_int(d["revision"], f"{context}.revision")
    _require_utc_timestamp(d["observed_at"], f"{context}.observed_at")
    return fid


def _validate_topic(
    topic: Any,
    scope_forum_id: str,
    context: str,
    scope_guild_id: Optional[str] = None,
) -> str:
    d = _require_dict(topic, context)
    _validate_keys_present(
        d,
        {
            "thread_id",
            "forum_id",
            "title",
            "source_url",
            "revision",
            "observed_at",
            "tags",
            "starter_message_id",
            "archived",
            "locked",
        },
        context,
    )
    try:
        tid = require_discord_id(d["thread_id"], f"{context}.thread_id")
        fid = require_discord_id(d["forum_id"], f"{context}.forum_id")
        optional_discord_id(d["starter_message_id"], f"{context}.starter_message_id")
    except ValueError as exc:
        raise ContractValidationError(str(exc)) from exc
    if fid != scope_forum_id:
        raise ContractValidationError(f"{context}.forum_id {fid!r} must match scope.forum_id {scope_forum_id!r}")
    _require_non_empty_str(d["title"], f"{context}.title")
    url = _require_non_empty_str(d["source_url"], f"{context}.source_url")
    url_gid, url_cid, url_mid = _parse_canonical_discord_channel_url(url, f"{context}.source_url")
    if (scope_guild_id is not None and url_gid != scope_guild_id) or url_cid != tid or url_mid is not None:
        raise ContractValidationError(f"{context}.source_url must identify thread_id {tid!r}")
    _require_positive_int(d["revision"], f"{context}.revision")
    _require_utc_timestamp(d["observed_at"], f"{context}.observed_at")
    for idx, t in enumerate(_require_list(d["tags"], f"{context}.tags")):
        _validate_tag(t, f"{context}.tags[{idx}]")
    _optional_bool(d["archived"], f"{context}.archived")
    _optional_bool(d["locked"], f"{context}.locked")
    if "remote_last_activity_at" in d:
        _optional_utc_timestamp(d["remote_last_activity_at"], f"{context}.remote_last_activity_at")
    return tid


def _validate_message(
    msg: Any,
    known_thread_ids: Optional[set[str]],
    context: str,
    scope_guild_id: Optional[str] = None,
) -> dict[str, Any]:
    d = _require_dict(msg, context)
    _validate_keys_present(
        d,
        {
            "entity_ref",
            "message_id",
            "thread_id",
            "author",
            "body",
            "content_hash",
            "revision",
            "observed_at",
            "remote_created_at",
            "remote_edited_at",
            "source_url",
            "identity_verified",
            "attachments",
        },
        context,
    )
    _require_non_empty_str(d["entity_ref"], f"{context}.entity_ref")
    try:
        mid = optional_discord_id(d["message_id"], f"{context}.message_id")
        tid = require_discord_id(d["thread_id"], f"{context}.thread_id")
    except ValueError as exc:
        raise ContractValidationError(str(exc)) from exc
    if known_thread_ids is not None and tid not in known_thread_ids:
        raise ContractValidationError(f"{context}.thread_id {tid!r} is not present in snapshot topics")
    _validate_actor(d["author"], f"{context}.author")
    body = _require_str(d["body"], f"{context}.body")
    chash = _require_sha256(d["content_hash"], f"{context}.content_hash")
    expected_hash = compute_content_hash(body)
    if chash != expected_hash:
        raise ContractValidationError(
            f"{context}.content_hash mismatch: expected {expected_hash}, got {chash}"
        )
    _require_positive_int(d["revision"], f"{context}.revision")
    _require_utc_timestamp(d["observed_at"], f"{context}.observed_at")
    _optional_utc_timestamp(d["remote_created_at"], f"{context}.remote_created_at")
    _optional_utc_timestamp(d["remote_edited_at"], f"{context}.remote_edited_at")
    surl = _optional_str(d["source_url"], f"{context}.source_url")
    if surl is not None:
        url_gid, url_cid, url_mid = _parse_canonical_discord_channel_url(surl, f"{context}.source_url")
        if scope_guild_id is not None and url_gid != scope_guild_id:
            raise ContractValidationError(f"{context}.source_url guild_id {url_gid!r} does not match scope")
        if url_cid != tid:
            raise ContractValidationError(f"{context}.source_url channel {url_cid!r} must match thread_id {tid!r}")
        if url_mid is not None and mid is not None and url_mid != mid:
            raise ContractValidationError(f"{context}.source_url message_id {url_mid!r} must match {mid!r}")
    _require_bool(d["identity_verified"], f"{context}.identity_verified")
    for idx, att in enumerate(_require_list(d["attachments"], f"{context}.attachments")):
        _validate_attachment(att, f"{context}.attachments[{idx}]")
    return d


def _validate_requirement(
    req: Any,
    context: str,
    scope_guild_id: Optional[str] = None,
) -> dict[str, Any]:
    d = _require_dict(req, context)
    _validate_keys_present(
        d,
        {
            "requirement_id",
            "source_entity_ref",
            "source_url",
            "body",
            "content_hash",
            "revision",
            "observed_at",
        },
        context,
    )
    _require_non_empty_str(d["requirement_id"], f"{context}.requirement_id")
    _optional_str(d["source_entity_ref"], f"{context}.source_entity_ref")
    surl = _optional_str(d["source_url"], f"{context}.source_url")
    if surl is not None and scope_guild_id is not None:
        url_gid, _, _ = _parse_canonical_discord_channel_url(surl, f"{context}.source_url")
        if url_gid != scope_guild_id:
            raise ContractValidationError(f"{context}.source_url guild_id {url_gid!r} does not match scope")
    body = _require_str(d["body"], f"{context}.body")
    chash = _require_sha256(d["content_hash"], f"{context}.content_hash")
    expected_hash = compute_content_hash(body)
    if chash != expected_hash:
        raise ContractValidationError(
            f"{context}.content_hash mismatch: expected {expected_hash}, got {chash}"
        )
    _require_positive_int(d["revision"], f"{context}.revision")
    _require_utc_timestamp(d["observed_at"], f"{context}.observed_at")
    return d


def _validate_snapshot(record: dict[str, Any]) -> None:
    _validate_keys_present(
        record,
        {"snapshot_id", "revision", "captured_at", "cursor", "forum", "topics", "messages", "requirements", "coverage"},
        "forum.snapshot.v1",
    )
    _require_non_empty_str(record["snapshot_id"], "snapshot_id")
    _require_positive_int(record["revision"], "revision")
    _require_utc_timestamp(record["captured_at"], "captured_at")
    _validate_cursor(record["cursor"], "cursor")

    scope = record["scope"]
    _validate_forum(record["forum"], scope["guild_id"], scope["forum_id"], "forum")

    topic_ids: set[str] = set()
    for idx, topic in enumerate(_require_list(record["topics"], "topics")):
        tid = _validate_topic(topic, scope["forum_id"], f"topics[{idx}]", scope_guild_id=scope["guild_id"])
        if tid in topic_ids:
            raise ContractValidationError(f"duplicate thread_id {tid!r} in snapshot.topics")
        topic_ids.add(tid)

    for idx, msg in enumerate(_require_list(record["messages"], "messages")):
        _validate_message(msg, topic_ids, f"messages[{idx}]", scope_guild_id=scope["guild_id"])

    for idx, req in enumerate(_require_list(record["requirements"], "requirements")):
        _validate_requirement(req, f"requirements[{idx}]", scope_guild_id=scope["guild_id"])

    cov = _require_dict(record["coverage"], "coverage")
    _validate_keys_present(cov, {"topics", "histories", "requirements", "pending", "errors", "last_success_at"}, "coverage")
    for k in ("topics", "histories", "requirements"):
        if cov[k] not in VALID_COVERAGE_STATES:
            raise ContractValidationError(f"coverage.{k} invalid: {cov[k]!r}")
    _optional_utc_timestamp(cov["last_success_at"], "coverage.last_success_at")

    seen_obs_refs: set[str] = set()
    has_unresolved = False
    for idx, pitem in enumerate(_require_list(cov["pending"], "coverage.pending")):
        pd = _require_dict(pitem, f"coverage.pending[{idx}]")
        _validate_keys_present(pd, {"thread_id", "reason", "resume_token"}, f"coverage.pending[{idx}]")
        try:
            tid = optional_discord_id(pd["thread_id"], f"coverage.pending[{idx}].thread_id")
        except ValueError as exc:
            raise ContractValidationError(str(exc)) from exc
        _require_non_empty_str(pd["reason"], f"coverage.pending[{idx}].reason")
        _optional_str(pd["resume_token"], f"coverage.pending[{idx}].resume_token")
        if tid is None:
            has_unresolved = True
            if "observation_ref" not in pd:
                raise ContractValidationError(f"coverage.pending[{idx}] with null thread_id requires observation_ref")
            obs_ref = _require_non_empty_str(pd["observation_ref"], f"coverage.pending[{idx}].observation_ref")
            if obs_ref in seen_obs_refs:
                raise ContractValidationError(f"duplicate observation_ref {obs_ref!r} in coverage.pending")
            seen_obs_refs.add(obs_ref)

    if has_unresolved and cov["topics"] != "partial":
        raise ContractValidationError("coverage.topics must be 'partial' when unresolved observations are pending")

    for idx, err in enumerate(_require_list(cov["errors"], "coverage.errors")):
        ed = _require_dict(err, f"coverage.errors[{idx}]")
        _validate_keys_present(ed, {"code", "message", "thread_id"}, f"coverage.errors[{idx}]")
        _require_non_empty_str(ed["code"], f"coverage.errors[{idx}].code")
        _require_str(ed["message"], f"coverage.errors[{idx}].message")
        try:
            optional_discord_id(ed["thread_id"], f"coverage.errors[{idx}].thread_id")
        except ValueError as exc:
            raise ContractValidationError(str(exc)) from exc


def _validate_changes(record: dict[str, Any]) -> None:
    _validate_keys_present(
        record,
        {"snapshot_id", "snapshot_revision", "after_cursor", "cursor", "changes"},
        "forum.changes.v1",
    )
    _require_non_empty_str(record["snapshot_id"], "snapshot_id")
    _require_positive_int(record["snapshot_revision"], "snapshot_revision")
    if record["after_cursor"] is not None:
        _validate_cursor(record["after_cursor"], "after_cursor")
    _validate_cursor(record["cursor"], "cursor")

    scope = record["scope"]
    seen_change_ids: set[str] = set()
    for idx, ch in enumerate(_require_list(record["changes"], "changes")):
        ctx = f"changes[{idx}]"
        cd = _require_dict(ch, ctx)
        _validate_keys_present(
            cd,
            {
                "change_id",
                "entity_type",
                "entity_ref",
                "kind",
                "before_revision",
                "after_revision",
                "observed_at",
                "source",
                "after",
            },
            ctx,
        )
        cid = _require_non_empty_str(cd["change_id"], f"{ctx}.change_id")
        if cid in seen_change_ids:
            raise ContractValidationError(f"{ctx}.change_id duplicate within batch: {cid!r}")
        seen_change_ids.add(cid)

        etype = cd["entity_type"]
        if etype not in VALID_ENTITY_TYPES:
            raise ContractValidationError(f"{ctx}.entity_type invalid: {etype!r}")
        eref = _require_non_empty_str(cd["entity_ref"], f"{ctx}.entity_ref")
        kind = cd["kind"]
        if kind not in VALID_CHANGE_KINDS:
            raise ContractValidationError(f"{ctx}.kind invalid: {kind!r}")
        brev = _optional_positive_int(cd["before_revision"], f"{ctx}.before_revision")
        arev = _optional_positive_int(cd["after_revision"], f"{ctx}.after_revision")
        if brev is not None and arev is not None and arev <= brev:
            raise ContractValidationError(
                f"{ctx}.after_revision ({arev}) must be > before_revision ({brev})"
            )

        _require_utc_timestamp(cd["observed_at"], f"{ctx}.observed_at")
        src = _require_dict(cd["source"], f"{ctx}.source")
        _validate_keys_present(src, {"method", "source_url", "confidence"}, f"{ctx}.source")
        _require_non_empty_str(src["method"], f"{ctx}.source.method")
        _optional_str(src["source_url"], f"{ctx}.source.source_url")
        conf = src["confidence"]
        if conf is not None:
            if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not (0.0 <= float(conf) <= 1.0):
                raise ContractValidationError(f"{ctx}.source.confidence must be 0..1 or null, got {conf!r}")

        after = cd["after"]
        if kind == "removal_observed":
            if after is not None or arev is not None:
                raise ContractValidationError(
                    f"{ctx} with kind='removal_observed' must have null after and after_revision"
                )
            continue

        if after is None:
            raise ContractValidationError(f"{ctx}.after must be a full entity object for kind={kind!r}")
        if arev is None:
            raise ContractValidationError(f"{ctx}.after_revision must be a positive integer for kind={kind!r}")

        after_dict = _require_dict(after, f"{ctx}.after")
        if etype == "forum":
            fid = _validate_forum(after_dict, scope["guild_id"], scope["forum_id"], f"{ctx}.after")
            if fid != eref:
                raise ContractValidationError(f"{ctx}.after.forum_id {fid!r} must match entity_ref {eref!r}")
            actual_rev = int(after_dict["revision"])
        elif etype == "topic":
            tid = _validate_topic(
                after_dict, scope["forum_id"], f"{ctx}.after", scope_guild_id=scope["guild_id"]
            )
            if tid != eref:
                raise ContractValidationError(f"{ctx}.after.thread_id {tid!r} must match entity_ref {eref!r}")
            actual_rev = int(after_dict["revision"])
        elif etype == "message":
            msg_d = _validate_message(
                after_dict, None, f"{ctx}.after", scope_guild_id=scope["guild_id"]
            )
            if msg_d["entity_ref"] != eref:
                raise ContractValidationError(
                    f"{ctx}.after.entity_ref {msg_d['entity_ref']!r} must match entity_ref {eref!r}"
                )
            actual_rev = int(msg_d["revision"])
        else:  # etype == "requirement"
            req_d = _validate_requirement(
                after_dict, f"{ctx}.after", scope_guild_id=scope["guild_id"]
            )
            if req_d["requirement_id"] != eref:
                raise ContractValidationError(
                    f"{ctx}.after.requirement_id {req_d['requirement_id']!r} must match entity_ref {eref!r}"
                )
            actual_rev = int(req_d["revision"])

        if actual_rev != arev:
            raise ContractValidationError(
                f"{ctx}.after.revision ({actual_rev}) must match after_revision ({arev})"
            )


def validate_relative_local_ref(local_ref: str, context: str = "local_ref") -> PurePosixPath:
    """Validate that local_ref is a relative path with no traversal or drive prefix."""
    ref = _require_non_empty_str(local_ref, context)
    if ref.startswith("/") or ref.startswith("\\") or _WIN_DRIVE_RE.match(ref):
        raise ContractValidationError(f"{context} must be a relative path under transfer root, got {ref!r}")
    norm = ref.replace("\\", "/")
    posix = PurePosixPath(norm)
    if posix.is_absolute() or ".." in posix.parts:
        raise ContractValidationError(f"{context} must not escape transfer root: {ref!r}")
    return posix


def _validate_draft(record: dict[str, Any]) -> None:
    _validate_keys_present(
        record,
        {"draft_id", "revision", "action", "destination", "body", "title", "tags", "evidence", "baseline"},
        "draft.request.v1",
    )
    _require_non_empty_str(record["draft_id"], "draft_id")
    _require_positive_int(record["revision"], "revision")
    action = record["action"]
    if action not in VALID_DRAFT_ACTIONS:
        raise ContractValidationError(f"action invalid: {action!r}")
    dest = _validate_destination(record["destination"], record["scope"], "destination")
    _require_str(record["body"], "body")
    title = _optional_str(record["title"], "title")
    _require_list(record["tags"], "tags")

    baseline = _require_dict(record["baseline"], "baseline")
    _validate_keys_present(
        baseline,
        {"snapshot_id", "snapshot_revision", "requirements", "message_content_hash"},
        "baseline",
    )
    _optional_str(baseline["snapshot_id"], "baseline.snapshot_id")
    _optional_positive_int(baseline["snapshot_revision"], "baseline.snapshot_revision")
    for idx, req_ref in enumerate(_require_list(baseline["requirements"], "baseline.requirements")):
        rd = _require_dict(req_ref, f"baseline.requirements[{idx}]")
        _validate_keys_present(rd, {"requirement_id", "revision"}, f"baseline.requirements[{idx}]")
        _require_non_empty_str(rd["requirement_id"], f"baseline.requirements[{idx}].requirement_id")
        _require_positive_int(rd["revision"], f"baseline.requirements[{idx}].revision")
    _optional_sha256(baseline["message_content_hash"], "baseline.message_content_hash")

    if action == "reply_existing":
        if dest["thread_id"] is None:
            raise ContractValidationError("reply_existing requires non-null destination.thread_id")
    elif action == "edit_own_message":
        if dest["thread_id"] is None or dest["message_id"] is None:
            raise ContractValidationError("edit_own_message requires non-null destination.thread_id and message_id")
        if baseline["message_content_hash"] is None:
            raise ContractValidationError("edit_own_message requires non-null baseline.message_content_hash")
    elif action == "create_topic":
        if not title or not title.strip():
            raise ContractValidationError("create_topic requires a non-empty title")
        if dest["thread_id"] is not None or dest["message_id"] is not None:
            raise ContractValidationError("create_topic must start with null thread_id and message_id")

    for idx, ev in enumerate(_require_list(record["evidence"], "evidence")):
        ctx = f"evidence[{idx}]"
        ed = _require_dict(ev, ctx)
        _validate_keys_present(ed, {"evidence_id", "name", "media_type", "sha256", "local_ref"}, ctx)
        _require_non_empty_str(ed["evidence_id"], f"{ctx}.evidence_id")
        _require_non_empty_str(ed["name"], f"{ctx}.name")
        _require_non_empty_str(ed["media_type"], f"{ctx}.media_type")
        _require_sha256(ed["sha256"], f"{ctx}.sha256")
        validate_relative_local_ref(ed["local_ref"], f"{ctx}.local_ref")


def _validate_receipt(record: dict[str, Any]) -> None:
    _validate_keys_present(
        record,
        {
            "receipt_id",
            "draft_id",
            "draft_revision",
            "destination",
            "status",
            "observed_at",
            "message_id",
            "message_url",
            "observed_body_hash",
            "evidence_ids",
            "reason",
        },
        "handoff.receipt.v1",
    )
    _require_non_empty_str(record["receipt_id"], "receipt_id")
    _require_non_empty_str(record["draft_id"], "draft_id")
    _require_positive_int(record["draft_revision"], "draft_revision")
    _validate_destination(record["destination"], record["scope"], "destination")
    if record["status"] not in VALID_RECEIPT_STATUSES:
        raise ContractValidationError(f"receipt status invalid: {record['status']!r}")
    _require_utc_timestamp(record["observed_at"], "observed_at")
    try:
        optional_discord_id(record["message_id"], "message_id")
    except ValueError as exc:
        raise ContractValidationError(str(exc)) from exc
    _optional_str(record["message_url"], "message_url")
    _optional_sha256(record["observed_body_hash"], "observed_body_hash")
    for idx, eid in enumerate(_require_list(record["evidence_ids"], "evidence_ids")):
        _require_non_empty_str(eid, f"evidence_ids[{idx}]")
    _optional_str(record["reason"], "reason")


def validate_exchange_record(record: Any) -> dict[str, Any]:
    """Validate any v1 forum exchange record against docs/FORUM_EXCHANGE_V1.md.

    Tolerates and preserves additive unknown fields. Raises ContractValidationError on violations.
    """
    d = _require_dict(record, "record")
    _validate_keys_present(d, {"schema", "record_id", "producer_id", "created_at", "scope"}, "record")
    schema = d["schema"]
    if schema not in VALID_SCHEMAS:
        raise ContractValidationError(f"unsupported schema: {schema!r}")
    _require_non_empty_str(d["record_id"], "record_id")
    _require_non_empty_str(d["producer_id"], "producer_id")
    _require_utc_timestamp(d["created_at"], "created_at")

    scope = _require_dict(d["scope"], "scope")
    _validate_keys_present(scope, {"guild_id", "forum_id", "actor"}, "scope")
    try:
        require_discord_id(scope["guild_id"], "scope.guild_id")
        require_discord_id(scope["forum_id"], "scope.forum_id")
    except ValueError as exc:
        raise ContractValidationError(str(exc)) from exc
    _validate_actor(scope["actor"], "scope.actor")

    if schema == "forum.snapshot.v1":
        _validate_snapshot(d)
    elif schema == "forum.changes.v1":
        _validate_changes(d)
    elif schema == "draft.request.v1":
        _validate_draft(d)
    elif schema == "handoff.receipt.v1":
        _validate_receipt(d)

    return d


def resolve_evidence_files(
    evidence_items: list[dict[str, Any]],
    transfer_root: str | Path,
) -> list[dict[str, Any]]:
    """Resolve and SHA-256 verify evidence files strictly inside transfer_root."""
    root = Path(transfer_root).resolve()
    resolved_list: list[dict[str, Any]] = []
    for idx, item in enumerate(evidence_items):
        posix = validate_relative_local_ref(item["local_ref"], f"evidence[{idx}].local_ref")
        candidate = (root / Path(*posix.parts)).resolve()
        if not candidate.is_relative_to(root):
            raise ContractValidationError(f"evidence[{idx}].local_ref escapes transfer_root: {item['local_ref']!r}")
        if not candidate.exists() or not candidate.is_file():
            raise FileNotFoundError(f"evidence[{idx}] file not found under transfer_root: {candidate}")
        actual_sha = compute_bytes_sha256(candidate.read_bytes())
        if actual_sha != item["sha256"]:
            raise ContractValidationError(
                f"evidence[{idx}] sha256 mismatch for {item['local_ref']}: expected {item['sha256']}, got {actual_sha}"
            )
        resolved_list.append({**item, "resolved_path": str(candidate)})
    return resolved_list


def write_record_atomic(path: str | Path, record: dict[str, Any]) -> Path:
    """Validate and atomically write a v1 JSON exchange record to disk."""
    validate_exchange_record(record)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{os.getpid()}.tmp"
    payload = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def read_record(path: str | Path) -> dict[str, Any]:
    """Read and validate a v1 JSON exchange record from disk."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    return validate_exchange_record(data)
