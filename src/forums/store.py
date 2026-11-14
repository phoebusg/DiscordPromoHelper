"""SQLite local mirror, immutable revisions, checkpoints, and change log."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Optional
import uuid

from .changes import (
    CursorStreamMismatchError,
    build_change_event,
    classify_body_change,
    make_record_id,
)
from .exchange import RecordConflictError, validate_exchange_record, write_record_atomic
from .models import (
    build_discord_channel_url,
    compute_bytes_sha256,
    compute_content_hash,
    optional_discord_id,
    require_discord_id,
    utc_now_iso,
)

DEFAULT_PRODUCER_ID = "discord-promo-helper"


class ForumStore:
    """Persistent SQLite mirror for configured Discord forums."""

    def __init__(self, db_path: str | Path = "data/forums/mirror.sqlite3") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS mirror_meta (
                    scope_key TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    guild_id TEXT NOT NULL,
                    forum_id TEXT NOT NULL,
                    producer_id TEXT NOT NULL,
                    snapshot_revision INTEGER NOT NULL,
                    stream_id TEXT NOT NULL,
                    cursor_seq INTEGER NOT NULL,
                    exported_snapshot_revision INTEGER NOT NULL DEFAULT 0,
                    exported_cursor_seq INTEGER NOT NULL DEFAULT 0,
                    actor_json TEXT NOT NULL,
                    coverage_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS forums (
                    forum_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    guidelines_text TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    extra_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS topics (
                    thread_id TEXT PRIMARY KEY,
                    forum_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    starter_message_id TEXT,
                    archived INTEGER,
                    locked INTEGER,
                    remote_last_activity_at TEXT,
                    revision INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    last_deep_read_at TEXT,
                    observation_ref TEXT,
                    extra_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS unresolved_topics (
                    observation_ref TEXT PRIMARY KEY,
                    forum_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    resume_token TEXT,
                    source_url TEXT,
                    raw_json TEXT NOT NULL,
                    resolved_thread_id TEXT
                );

                CREATE TABLE IF NOT EXISTS unresolved_messages (
                    observation_ref TEXT NOT NULL,
                    local_msg_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (observation_ref, local_msg_key)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    entity_ref TEXT PRIMARY KEY,
                    message_id TEXT,
                    thread_id TEXT NOT NULL,
                    author_json TEXT NOT NULL,
                    body TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    remote_created_at TEXT,
                    remote_edited_at TEXT,
                    source_url TEXT,
                    identity_verified INTEGER NOT NULL,
                    attachments_json TEXT NOT NULL,
                    recognition_json TEXT,
                    extra_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS message_aliases (
                    alias_ref TEXT PRIMARY KEY,
                    canonical_entity_ref TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS requirements (
                    requirement_id TEXT PRIMARY KEY,
                    forum_id TEXT NOT NULL,
                    source_entity_ref TEXT,
                    source_url TEXT,
                    body TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    parsed_checks_json TEXT,
                    extra_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_ref TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    normalized_json TEXT NOT NULL,
                    raw_observation_json TEXT NOT NULL,
                    recognition_json TEXT,
                    remote_edited_at TEXT,
                    observed_at TEXT NOT NULL,
                    captured_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS changes (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    change_id TEXT NOT NULL UNIQUE,
                    stream_id TEXT NOT NULL,
                    snapshot_revision INTEGER NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_ref TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    before_revision INTEGER,
                    after_revision INTEGER,
                    observed_at TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    after_json TEXT
                );

                CREATE TABLE IF NOT EXISTS drafts (
                    draft_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    record_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    needs_recheck INTEGER NOT NULL DEFAULT 0,
                    recheck_reason TEXT,
                    staged_at TEXT,
                    submitted_message_id TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (draft_id, revision)
                );

                CREATE TABLE IF NOT EXISTS receipts (
                    receipt_id TEXT PRIMARY KEY,
                    record_id TEXT NOT NULL UNIQUE,
                    draft_id TEXT NOT NULL,
                    draft_revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    record_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ingested_records (
                    record_id TEXT PRIMARY KEY,
                    schema TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                );
                """
            )
            meta_cols = {
                r["name"] for r in self._conn.execute("PRAGMA table_info(mirror_meta)").fetchall()
            }
            if "exported_snapshot_revision" not in meta_cols:
                self._conn.execute(
                    "ALTER TABLE mirror_meta ADD COLUMN exported_snapshot_revision INTEGER NOT NULL DEFAULT 0"
                )
            if "exported_cursor_seq" not in meta_cols:
                self._conn.execute(
                    "ALTER TABLE mirror_meta ADD COLUMN exported_cursor_seq INTEGER NOT NULL DEFAULT 0"
                )
            topic_cols = {
                r["name"] for r in self._conn.execute("PRAGMA table_info(topics)").fetchall()
            }
            if "observation_ref" not in topic_cols:
                self._conn.execute("ALTER TABLE topics ADD COLUMN observation_ref TEXT")

    def ensure_scope(
        self,
        guild_id: str,
        forum_id: str,
        actor: Optional[dict[str, Any]] = None,
        snapshot_id: Optional[str] = None,
        producer_id: str = DEFAULT_PRODUCER_ID,
    ) -> dict[str, Any]:
        """Initialize or return the mirror scope metadata for guild_id/forum_id."""
        gid = require_discord_id(guild_id, "guild_id")
        fid = require_discord_id(forum_id, "forum_id")
        scope_key = f"{gid}:{fid}"
        row = self._conn.execute(
            "SELECT * FROM mirror_meta WHERE scope_key = ?", (scope_key,)
        ).fetchone()
        if row is not None:
            meta = dict(row)
            if actor is not None:
                with self._conn:
                    self._conn.execute(
                        "UPDATE mirror_meta SET actor_json = ?, updated_at = ? WHERE scope_key = ?",
                        (json.dumps(actor), utc_now_iso(), scope_key),
                    )
                meta["actor_json"] = json.dumps(actor)
            return meta

        now = utc_now_iso()
        default_actor = actor or {"user_id": None, "label": "Unknown session"}
        default_coverage = {
            "topics": "not_started",
            "histories": "not_started",
            "requirements": "not_started",
            "pending": [],
            "errors": [],
            "last_success_at": None,
        }
        snap_id = snapshot_id or f"mirror-{gid}-{fid}"
        stream_id = f"stream-{gid}-{fid}-{uuid.uuid4().hex[:8]}"
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO mirror_meta (
                    scope_key, snapshot_id, guild_id, forum_id, producer_id,
                    snapshot_revision, stream_id, cursor_seq, actor_json, coverage_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, 0, ?, ?, ?)
                """,
                (
                    scope_key,
                    snap_id,
                    gid,
                    fid,
                    producer_id,
                    stream_id,
                    json.dumps(default_actor),
                    json.dumps(default_coverage),
                    now,
                ),
            )
        return dict(
            self._conn.execute(
                "SELECT * FROM mirror_meta WHERE scope_key = ?", (scope_key,)
            ).fetchone()
        )

    def _get_meta(self, scope_key: Optional[str] = None) -> sqlite3.Row:
        if scope_key:
            row = self._conn.execute(
                "SELECT * FROM mirror_meta WHERE scope_key = ?", (scope_key,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT * FROM mirror_meta LIMIT 1").fetchone()
        if row is None:
            raise ValueError("Mirror scope not initialized; call ensure_scope() first")
        return row

    def get_coverage(self) -> dict[str, Any]:
        meta = self._get_meta()
        cov = json.loads(meta["coverage_json"])
        unresolved = self._conn.execute(
            "SELECT * FROM unresolved_topics WHERE resolved_thread_id IS NULL ORDER BY observed_at, observation_ref"
        ).fetchall()
        pending = [p for p in cov.get("pending", []) if p.get("thread_id") is not None]
        for u in unresolved:
            pending.append(
                {
                    "thread_id": None,
                    "observation_ref": u["observation_ref"],
                    "observed_title": u["title"],
                    "title": u["title"],
                    "reason": u["reason"],
                    "resume_token": u["resume_token"],
                }
            )
        cov["pending"] = pending
        if unresolved and cov.get("topics") == "complete":
            cov["topics"] = "partial"
        return cov

    def update_coverage(
        self,
        *,
        topics: Optional[str] = None,
        histories: Optional[str] = None,
        requirements: Optional[str] = None,
        pending_known: Optional[list[dict[str, Any]]] = None,
        errors: Optional[list[dict[str, Any]]] = None,
        last_success_at: Optional[str] = None,
        keep_existing_errors: bool = False,
    ) -> dict[str, Any]:
        meta = self._get_meta()
        cov = json.loads(meta["coverage_json"])
        if topics is not None:
            cov["topics"] = topics
        if histories is not None:
            cov["histories"] = histories
        if requirements is not None:
            cov["requirements"] = requirements
        if pending_known is not None:
            cov["pending"] = [p for p in pending_known if p.get("thread_id") is not None]
        if errors is not None:
            if keep_existing_errors:
                cov["errors"] = list(cov.get("errors", [])) + list(errors)
            else:
                cov["errors"] = list(errors)
        if last_success_at is not None:
            cov["last_success_at"] = last_success_at
        with self._conn:
            self._conn.execute(
                "UPDATE mirror_meta SET coverage_json = ?, updated_at = ? WHERE scope_key = ?",
                (json.dumps(cov), utc_now_iso(), meta["scope_key"]),
            )
        return self.get_coverage()

    def _bump_snapshot_revision_and_append_change(
        self,
        meta: sqlite3.Row,
        change: dict[str, Any],
    ) -> int:
        """Append change to the active snapshot stream without advancing snapshot_revision per mutation.

        snapshot_revision stays anchored to the active exported snapshot and increments only
        at explicit snapshot cutover. cursor_seq advances monotonically for every change event.
        """
        fresh = self._conn.execute(
            "SELECT snapshot_revision FROM mirror_meta WHERE scope_key = ?",
            (meta["scope_key"],),
        ).fetchone()
        snap_rev = int(fresh["snapshot_revision"]) if fresh is not None else int(meta["snapshot_revision"])
        cur = self._conn.execute(
            """
            INSERT INTO changes (
                change_id, stream_id, snapshot_revision, entity_type, entity_ref,
                kind, before_revision, after_revision, observed_at, source_json, after_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                change["change_id"],
                meta["stream_id"],
                snap_rev,
                change["entity_type"],
                change["entity_ref"],
                change["kind"],
                change["before_revision"],
                change["after_revision"],
                change["observed_at"],
                json.dumps(change["source"]),
                json.dumps(change["after"]) if change["after"] is not None else None,
            ),
        )
        seq = int(cur.lastrowid)
        self._conn.execute(
            "UPDATE mirror_meta SET cursor_seq = ?, updated_at = ? WHERE scope_key = ?",
            (seq, utc_now_iso(), meta["scope_key"]),
        )
        return seq

    def _record_revision(
        self,
        entity_type: str,
        entity_ref: str,
        revision: int,
        content_hash: str,
        normalized: dict[str, Any],
        raw_observation: dict[str, Any],
        observed_at: str,
        recognition: Optional[dict[str, Any]] = None,
        remote_edited_at: Optional[str] = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO revisions (
                entity_type, entity_ref, revision, content_hash, normalized_json,
                raw_observation_json, recognition_json, remote_edited_at, observed_at, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_type,
                entity_ref,
                revision,
                content_hash,
                json.dumps(normalized),
                json.dumps(raw_observation),
                json.dumps(recognition) if recognition is not None else None,
                remote_edited_at,
                observed_at,
                utc_now_iso(),
            ),
        )

    def upsert_forum(
        self,
        *,
        forum_id: str,
        name: str,
        source_url: Optional[str] = None,
        tags: Optional[list[dict[str, Any]]] = None,
        guidelines_text: str = "",
        observed_at: Optional[str] = None,
        source_method: str = "semantic",
        confidence: Optional[float] = 1.0,
        raw_observation: Optional[dict[str, Any]] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        meta = self._get_meta()
        fid = require_discord_id(forum_id, "forum_id")
        now = observed_at or utc_now_iso()
        url = source_url or build_discord_channel_url(meta["guild_id"], fid)
        tags_norm = [{"tag_id": t.get("tag_id"), "name": t["name"]} for t in (tags or [])]
        row = self._conn.execute("SELECT * FROM forums WHERE forum_id = ?", (fid,)).fetchone()
        entity = {
            "forum_id": fid,
            "name": name,
            "source_url": url,
            "tags": tags_norm,
            "observed_at": now,
            **(extra or {}),
        }
        payload_hash = compute_content_hash(
            json.dumps({"name": name, "tags": tags_norm, "guidelines": guidelines_text}, sort_keys=True)
        )

        with self._conn:
            if row is None:
                rev = 1
                entity["revision"] = rev
                self._conn.execute(
                    """
                    INSERT INTO forums (forum_id, name, source_url, tags_json, guidelines_text, revision, observed_at, extra_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (fid, name, url, json.dumps(tags_norm), guidelines_text, rev, now, json.dumps(extra or {})),
                )
                self._record_revision(
                    "forum", fid, rev, payload_hash, entity, raw_observation or entity, now
                )
                chg = build_change_event(
                    "forum", fid, "added", None, rev, source_method, url, confidence, entity, observed_at=now
                )
                self._bump_snapshot_revision_and_append_change(meta, chg)
                return chg

            old_tags = json.loads(row["tags_json"])
            changed_tags = old_tags != tags_norm
            changed_meta = row["name"] != name or row["guidelines_text"] != guidelines_text
            if not changed_tags and not changed_meta:
                return None

            before_rev = int(row["revision"])
            rev = before_rev + 1
            entity["revision"] = rev
            self._conn.execute(
                """
                UPDATE forums
                SET name = ?, source_url = ?, tags_json = ?, guidelines_text = ?, revision = ?, observed_at = ?, extra_json = ?
                WHERE forum_id = ?
                """,
                (name, url, json.dumps(tags_norm), guidelines_text, rev, now, json.dumps(extra or {}), fid),
            )
            self._record_revision(
                "forum", fid, rev, payload_hash, entity, raw_observation or entity, now
            )
            kind = "tags_changed" if (changed_tags and not changed_meta) else "edited"
            chg = build_change_event(
                "forum", fid, kind, before_rev, rev, source_method, url, confidence, entity, observed_at=now
            )
            self._bump_snapshot_revision_and_append_change(meta, chg)
            return chg

    def upsert_topic(
        self,
        *,
        thread_id: str,
        forum_id: str,
        title: str,
        source_url: Optional[str] = None,
        tags: Optional[list[dict[str, Any]]] = None,
        starter_message_id: Optional[str] = None,
        archived: Optional[bool] = None,
        locked: Optional[bool] = None,
        remote_last_activity_at: Optional[str] = None,
        observation_ref: Optional[str] = None,
        observed_at: Optional[str] = None,
        source_method: str = "semantic",
        confidence: Optional[float] = 1.0,
        raw_observation: Optional[dict[str, Any]] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        meta = self._get_meta()
        tid = require_discord_id(thread_id, "thread_id")
        fid = require_discord_id(forum_id, "forum_id")
        optional_discord_id(starter_message_id, "starter_message_id")
        now = observed_at or utc_now_iso()
        url = source_url or build_discord_channel_url(meta["guild_id"], tid)
        tags_norm = [{"tag_id": t.get("tag_id"), "name": t["name"]} for t in (tags or [])]
        archived_db = None if archived is None else int(bool(archived))
        locked_db = None if locked is None else int(bool(locked))

        row = self._conn.execute("SELECT * FROM topics WHERE thread_id = ?", (tid,)).fetchone()
        eff_obs_ref = observation_ref or (row["observation_ref"] if row is not None else None)
        entity = {
            "thread_id": tid,
            "forum_id": fid,
            "title": title,
            "source_url": url,
            "tags": tags_norm,
            "starter_message_id": starter_message_id,
            "archived": archived,
            "locked": locked,
            "remote_last_activity_at": remote_last_activity_at,
            "observed_at": now,
            **(extra or {}),
        }
        if eff_obs_ref is not None:
            entity["observation_ref"] = eff_obs_ref
        payload_hash = compute_content_hash(
            json.dumps(
                {
                    "title": title,
                    "tags": tags_norm,
                    "archived": archived,
                    "locked": locked,
                    "starter_message_id": starter_message_id,
                },
                sort_keys=True,
            )
        )

        with self._conn:
            if row is None:
                rev = 1
                entity["revision"] = rev
                self._conn.execute(
                    """
                    INSERT INTO topics (
                        thread_id, forum_id, title, source_url, tags_json, starter_message_id,
                        archived, locked, remote_last_activity_at, revision, observed_at,
                        observation_ref, extra_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tid,
                        fid,
                        title,
                        url,
                        json.dumps(tags_norm),
                        starter_message_id,
                        archived_db,
                        locked_db,
                        remote_last_activity_at,
                        rev,
                        now,
                        eff_obs_ref,
                        json.dumps(extra or {}),
                    ),
                )
                self._record_revision(
                    "topic", tid, rev, payload_hash, entity, raw_observation or entity, now
                )
                chg = build_change_event(
                    "topic", tid, "added", None, rev, source_method, url, confidence, entity, observed_at=now
                )
                self._bump_snapshot_revision_and_append_change(meta, chg)
                return chg

            old_tags = json.loads(row["tags_json"])
            old_archived = None if row["archived"] is None else bool(row["archived"])
            old_locked = None if row["locked"] is None else bool(row["locked"])
            changed_tags = old_tags != tags_norm
            changed_state = (old_archived != archived) or (old_locked != locked)
            changed_title = row["title"] != title
            changed_starter = (
                starter_message_id is not None and row["starter_message_id"] != starter_message_id
            )
            changed_obs_ref = (
                observation_ref is not None and row["observation_ref"] != observation_ref
            )
            if not (changed_tags or changed_state or changed_title or changed_starter or changed_obs_ref):
                if remote_last_activity_at and remote_last_activity_at != row["remote_last_activity_at"]:
                    self._conn.execute(
                        "UPDATE topics SET remote_last_activity_at = ?, observed_at = ? WHERE thread_id = ?",
                        (remote_last_activity_at, now, tid),
                    )
                return None

            before_rev = int(row["revision"])
            rev = before_rev + 1
            entity["revision"] = rev
            eff_starter = starter_message_id or row["starter_message_id"]
            entity["starter_message_id"] = eff_starter
            self._conn.execute(
                """
                UPDATE topics
                SET title = ?, source_url = ?, tags_json = ?, starter_message_id = ?,
                    archived = ?, locked = ?, remote_last_activity_at = ?, revision = ?,
                    observed_at = ?, observation_ref = ?, extra_json = ?
                WHERE thread_id = ?
                """,
                (
                    title,
                    url,
                    json.dumps(tags_norm),
                    eff_starter,
                    archived_db,
                    locked_db,
                    remote_last_activity_at or row["remote_last_activity_at"],
                    rev,
                    now,
                    eff_obs_ref,
                    json.dumps(extra or {}),
                    tid,
                ),
            )
            self._record_revision(
                "topic", tid, rev, payload_hash, entity, raw_observation or entity, now
            )
            if changed_state and not changed_title and not changed_tags:
                kind = "state_changed"
            elif changed_tags and not changed_title and not changed_state:
                kind = "tags_changed"
            else:
                kind = "edited"
            chg = build_change_event(
                "topic", tid, kind, before_rev, rev, source_method, url, confidence, entity, observed_at=now
            )
            self._bump_snapshot_revision_and_append_change(meta, chg)
            return chg

    def record_unresolved_topic(
        self,
        *,
        observation_ref: str,
        forum_id: str,
        title: str,
        reason: str = "unresolved_thread_id",
        resume_token: Optional[str] = None,
        source_url: Optional[str] = None,
        observed_at: Optional[str] = None,
        raw_observation: Optional[dict[str, Any]] = None,
        messages: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Store an unresolved topic observation under its distinct observation_ref.

        Never merges same-title topics and never exports to snapshot.topics until resolved.
        """
        meta = self._get_meta()
        fid = require_discord_id(forum_id, "forum_id")
        now = observed_at or utc_now_iso()
        existing = self._conn.execute(
            "SELECT * FROM unresolved_topics WHERE observation_ref = ?",
            (observation_ref,),
        ).fetchone()

        # If this exact observation_ref was previously resolved, route directly to that thread_id
        if existing is not None and existing["resolved_thread_id"] is not None:
            resolved_tid = existing["resolved_thread_id"]
            self.upsert_topic(
                thread_id=resolved_tid,
                forum_id=fid,
                title=title,
                source_url=source_url,
                observed_at=now,
            )
            for idx, m in enumerate(messages or []):
                self.upsert_message(
                    entity_ref=m.get("entity_ref") or f"prov:{observation_ref}:{idx}",
                    message_id=m.get("message_id"),
                    thread_id=resolved_tid,
                    author=m.get("author", {"user_id": None, "label": "Unknown"}),
                    body=m["body"],
                    observed_at=m.get("observed_at", now),
                    remote_created_at=m.get("remote_created_at"),
                    remote_edited_at=m.get("remote_edited_at"),
                    source_url=m.get("source_url"),
                    identity_verified=bool(m.get("identity_verified", False)),
                    attachments=m.get("attachments", []),
                    recognition=m.get("recognition"),
                )
            return {"observation_ref": observation_ref, "resolved_thread_id": resolved_tid}

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO unresolved_topics (
                    observation_ref, forum_id, title, observed_at, reason, resume_token, source_url, raw_json, resolved_thread_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(observation_ref) DO UPDATE SET
                    title = excluded.title,
                    observed_at = excluded.observed_at,
                    reason = excluded.reason,
                    resume_token = excluded.resume_token,
                    source_url = excluded.source_url,
                    raw_json = excluded.raw_json
                """,
                (
                    observation_ref,
                    fid,
                    title,
                    now,
                    reason,
                    resume_token,
                    source_url,
                    json.dumps(raw_observation or {"title": title, "observation_ref": observation_ref}),
                ),
            )
            for idx, m in enumerate(messages or []):
                local_key = m.get("entity_ref") or f"msg:{idx}"
                self._conn.execute(
                    """
                    INSERT INTO unresolved_messages (observation_ref, local_msg_key, payload_json)
                    VALUES (?, ?, ?)
                    ON CONFLICT(observation_ref, local_msg_key) DO UPDATE SET
                        payload_json = excluded.payload_json
                    """,
                    (observation_ref, local_key, json.dumps(m)),
                )

            cov = json.loads(meta["coverage_json"])
            if cov.get("topics") == "complete":
                cov["topics"] = "partial"
                self._conn.execute(
                    "UPDATE mirror_meta SET coverage_json = ?, updated_at = ? WHERE scope_key = ?",
                    (json.dumps(cov), now, meta["scope_key"]),
                )

        return {"observation_ref": observation_ref, "resolved_thread_id": None}

    def resolve_topic_observation(
        self,
        *,
        observation_ref: str,
        thread_id: str,
        source_url: Optional[str] = None,
        title: Optional[str] = None,
        tags: Optional[list[dict[str, Any]]] = None,
        starter_message_id: Optional[str] = None,
        archived: Optional[bool] = None,
        locked: Optional[bool] = None,
        observed_at: Optional[str] = None,
        source_method: str = "semantic",
        confidence: Optional[float] = 1.0,
    ) -> dict[str, Any]:
        """Resolve an unresolved observation_ref to a verified thread_id and release held messages."""
        tid = require_discord_id(thread_id, "thread_id")
        now = observed_at or utc_now_iso()
        obs_row = self._conn.execute(
            "SELECT * FROM unresolved_topics WHERE observation_ref = ?",
            (observation_ref,),
        ).fetchone()
        if obs_row is None:
            raise KeyError(f"Unknown observation_ref: {observation_ref!r}")

        if obs_row["resolved_thread_id"] is not None and obs_row["resolved_thread_id"] != tid:
            raise ValueError(
                f"observation_ref {observation_ref!r} already resolved to {obs_row['resolved_thread_id']!r}, cannot reassign to {tid!r}"
            )

        topic_change = self.upsert_topic(
            thread_id=tid,
            forum_id=obs_row["forum_id"],
            title=title or obs_row["title"],
            source_url=source_url,
            tags=tags,
            starter_message_id=starter_message_id,
            archived=archived,
            locked=locked,
            observation_ref=observation_ref,
            observed_at=now,
            source_method=source_method,
            confidence=confidence,
        )

        held_rows = self._conn.execute(
            "SELECT * FROM unresolved_messages WHERE observation_ref = ? ORDER BY local_msg_key",
            (observation_ref,),
        ).fetchall()
        msg_changes = []
        for hrow in held_rows:
            m = json.loads(hrow["payload_json"])
            mc = self.upsert_message(
                entity_ref=m.get("entity_ref") or f"prov:{observation_ref}:{hrow['local_msg_key']}",
                message_id=m.get("message_id"),
                thread_id=tid,
                author=m.get("author", {"user_id": None, "label": "Unknown"}),
                body=m["body"],
                observed_at=m.get("observed_at", now),
                remote_created_at=m.get("remote_created_at"),
                remote_edited_at=m.get("remote_edited_at"),
                source_url=m.get("source_url"),
                identity_verified=bool(m.get("identity_verified", False)),
                attachments=m.get("attachments", []),
                recognition=m.get("recognition"),
                source_method=source_method,
                confidence=confidence,
            )
            if mc is not None:
                msg_changes.append(mc)

        with self._conn:
            self._conn.execute(
                "UPDATE unresolved_topics SET resolved_thread_id = ? WHERE observation_ref = ?",
                (tid, observation_ref),
            )
            self._conn.execute(
                "DELETE FROM unresolved_messages WHERE observation_ref = ?",
                (observation_ref,),
            )

        return {
            "observation_ref": observation_ref,
            "thread_id": tid,
            "topic_change": topic_change,
            "message_changes": msg_changes,
        }

    def _resolve_message_entity_ref(self, entity_ref: str, message_id: Optional[str]) -> str:
        alias = self._conn.execute(
            "SELECT canonical_entity_ref FROM message_aliases WHERE alias_ref = ?",
            (entity_ref,),
        ).fetchone()
        if alias is not None:
            return alias["canonical_entity_ref"]
        if message_id is not None:
            row = self._conn.execute(
                "SELECT entity_ref FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if row is not None:
                return row["entity_ref"]
        return entity_ref

    def upsert_message(
        self,
        *,
        entity_ref: Optional[str],
        message_id: Optional[str],
        thread_id: str,
        author: dict[str, Any],
        body: str,
        observed_at: Optional[str] = None,
        remote_created_at: Optional[str] = None,
        remote_edited_at: Optional[str] = None,
        source_url: Optional[str] = None,
        identity_verified: bool = True,
        attachments: Optional[list[dict[str, Any]]] = None,
        recognition: Optional[dict[str, Any]] = None,
        explicit_edited_marker: bool = False,
        provisional_alias: Optional[str] = None,
        source_method: str = "semantic",
        confidence: Optional[float] = 1.0,
        raw_observation: Optional[dict[str, Any]] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        meta = self._get_meta()
        tid = require_discord_id(thread_id, "thread_id")
        mid = optional_discord_id(message_id, "message_id")
        now = observed_at or utc_now_iso()
        chash = compute_content_hash(body)
        atts_norm = [
            {
                "attachment_id": a.get("attachment_id"),
                "name": a["name"],
                "source_url": a.get("source_url"),
                "sha256": a.get("sha256"),
                "local_ref": a.get("local_ref"),
            }
            for a in (attachments or [])
        ]

        # Resolve existing ref via provisional alias or message_id
        candidate_ref = entity_ref or (f"msg:{mid}" if mid else None)
        if not candidate_ref:
            raise ValueError("entity_ref is required when message_id is null")

        if provisional_alias:
            resolved_ref = self._resolve_message_entity_ref(provisional_alias, mid)
        else:
            resolved_ref = self._resolve_message_entity_ref(candidate_ref, mid)

        row = self._conn.execute(
            "SELECT * FROM messages WHERE entity_ref = ?", (resolved_ref,)
        ).fetchone()

        eff_url = source_url
        if eff_url is None and mid is not None:
            eff_url = build_discord_channel_url(meta["guild_id"], tid, mid)

        entity = {
            "entity_ref": resolved_ref,
            "message_id": mid,
            "thread_id": tid,
            "author": {"user_id": author.get("user_id"), "label": author.get("label", "Unknown")},
            "body": body,
            "content_hash": chash,
            "observed_at": now,
            "remote_created_at": remote_created_at,
            "remote_edited_at": remote_edited_at,
            "source_url": eff_url,
            "identity_verified": bool(identity_verified),
            "attachments": atts_norm,
            **(extra or {}),
        }
        if recognition is not None:
            entity["recognition"] = recognition

        with self._conn:
            if row is None:
                rev = 1
                entity["revision"] = rev
                self._conn.execute(
                    """
                    INSERT INTO messages (
                        entity_ref, message_id, thread_id, author_json, body, content_hash,
                        revision, observed_at, remote_created_at, remote_edited_at, source_url,
                        identity_verified, attachments_json, recognition_json, extra_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved_ref,
                        mid,
                        tid,
                        json.dumps(entity["author"]),
                        body,
                        chash,
                        rev,
                        now,
                        remote_created_at,
                        remote_edited_at,
                        eff_url,
                        int(bool(identity_verified)),
                        json.dumps(atts_norm),
                        json.dumps(recognition) if recognition is not None else None,
                        json.dumps(extra or {}),
                    ),
                )
                if provisional_alias and provisional_alias != resolved_ref:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO message_aliases (alias_ref, canonical_entity_ref) VALUES (?, ?)",
                        (provisional_alias, resolved_ref),
                    )
                self._record_revision(
                    "message",
                    resolved_ref,
                    rev,
                    chash,
                    entity,
                    raw_observation or entity,
                    now,
                    recognition=recognition,
                    remote_edited_at=remote_edited_at,
                )
                chg = build_change_event(
                    "message",
                    resolved_ref,
                    "added",
                    None,
                    rev,
                    source_method,
                    eff_url,
                    confidence,
                    entity,
                    observed_at=now,
                )
                self._bump_snapshot_revision_and_append_change(meta, chg)
                return chg

            # Reconcile message_id onto existing provisional message if newly verified
            eff_mid = mid or row["message_id"]
            if eff_mid and provisional_alias and provisional_alias != resolved_ref:
                self._conn.execute(
                    "INSERT OR IGNORE INTO message_aliases (alias_ref, canonical_entity_ref) VALUES (?, ?)",
                    (provisional_alias, resolved_ref),
                )

            old_hash = row["content_hash"]
            old_atts = json.loads(row["attachments_json"])
            body_changed = old_hash != chash
            atts_changed = old_atts != atts_norm

            if not body_changed and not atts_changed:
                if eff_mid and not row["message_id"]:
                    self._conn.execute(
                        "UPDATE messages SET message_id = ?, source_url = ?, identity_verified = ? WHERE entity_ref = ?",
                        (eff_mid, eff_url or row["source_url"], int(bool(identity_verified)), resolved_ref),
                    )
                return None

            before_rev = int(row["revision"])
            rev = before_rev + 1
            entity["revision"] = rev
            entity["message_id"] = eff_mid
            entity["source_url"] = eff_url or row["source_url"]
            prev_rec = json.loads(row["recognition_json"]) if row["recognition_json"] else None

            if body_changed:
                kind = classify_body_change(
                    row["body"],
                    body,
                    prev_rec,
                    recognition,
                    row["remote_edited_at"],
                    remote_edited_at,
                    explicit_edited_marker=explicit_edited_marker,
                )
            else:
                kind = "attachment_changed"

            self._conn.execute(
                """
                UPDATE messages
                SET message_id = ?, author_json = ?, body = ?, content_hash = ?, revision = ?,
                    observed_at = ?, remote_created_at = ?, remote_edited_at = ?, source_url = ?,
                    identity_verified = ?, attachments_json = ?, recognition_json = ?, extra_json = ?
                WHERE entity_ref = ?
                """,
                (
                    eff_mid,
                    json.dumps(entity["author"]),
                    body,
                    chash,
                    rev,
                    now,
                    remote_created_at or row["remote_created_at"],
                    remote_edited_at or row["remote_edited_at"],
                    entity["source_url"],
                    int(bool(identity_verified)),
                    json.dumps(atts_norm),
                    json.dumps(recognition) if recognition is not None else None,
                    json.dumps(extra or {}),
                    resolved_ref,
                ),
            )
            self._record_revision(
                "message",
                resolved_ref,
                rev,
                chash,
                entity,
                raw_observation or entity,
                now,
                recognition=recognition,
                remote_edited_at=remote_edited_at,
            )
            chg = build_change_event(
                "message",
                resolved_ref,
                kind,
                before_rev,
                rev,
                source_method,
                entity["source_url"],
                confidence,
                entity,
                observed_at=now,
            )
            self._bump_snapshot_revision_and_append_change(meta, chg)
            return chg

    def mark_topic_deep_read(self, thread_id: str, observed_at: Optional[str] = None) -> None:
        tid = require_discord_id(thread_id, "thread_id")
        now = observed_at or utc_now_iso()
        with self._conn:
            self._conn.execute(
                "UPDATE topics SET last_deep_read_at = ?, observed_at = ? WHERE thread_id = ?",
                (now, now, tid),
            )

    def pick_topics_for_deep_reread(self, forum_id: str, limit: int = 2) -> list[str]:
        fid = require_discord_id(forum_id, "forum_id")
        rows = self._conn.execute(
            """
            SELECT thread_id FROM topics
            WHERE forum_id = ?
            ORDER BY COALESCE(last_deep_read_at, '') ASC, observed_at ASC, thread_id ASC
            LIMIT ?
            """,
            (fid, int(limit)),
        ).fetchall()
        return [r["thread_id"] for r in rows]

    def upsert_requirement(
        self,
        *,
        requirement_id: str,
        forum_id: str,
        body: str,
        source_entity_ref: Optional[str] = None,
        source_url: Optional[str] = None,
        observed_at: Optional[str] = None,
        parsed_checks: Optional[list[dict[str, Any]]] = None,
        source_method: str = "semantic",
        confidence: Optional[float] = 1.0,
        raw_observation: Optional[dict[str, Any]] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        meta = self._get_meta()
        fid = require_discord_id(forum_id, "forum_id")
        now = observed_at or utc_now_iso()
        chash = compute_content_hash(body)
        row = self._conn.execute(
            "SELECT * FROM requirements WHERE requirement_id = ?", (requirement_id,)
        ).fetchone()
        entity = {
            "requirement_id": requirement_id,
            "source_entity_ref": source_entity_ref,
            "source_url": source_url,
            "body": body,
            "content_hash": chash,
            "observed_at": now,
            **(extra or {}),
        }
        if parsed_checks is not None:
            entity["parsed_checks"] = parsed_checks

        with self._conn:
            if row is None:
                rev = 1
                entity["revision"] = rev
                self._conn.execute(
                    """
                    INSERT INTO requirements (
                        requirement_id, forum_id, source_entity_ref, source_url, body,
                        content_hash, revision, observed_at, parsed_checks_json, extra_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        requirement_id,
                        fid,
                        source_entity_ref,
                        source_url,
                        body,
                        chash,
                        rev,
                        now,
                        json.dumps(parsed_checks) if parsed_checks is not None else None,
                        json.dumps(extra or {}),
                    ),
                )
                self._record_revision(
                    "requirement", requirement_id, rev, chash, entity, raw_observation or entity, now
                )
                chg = build_change_event(
                    "requirement",
                    requirement_id,
                    "added",
                    None,
                    rev,
                    source_method,
                    source_url,
                    confidence,
                    entity,
                    observed_at=now,
                )
                self._bump_snapshot_revision_and_append_change(meta, chg)
                return chg

            if row["content_hash"] == chash:
                return None

            before_rev = int(row["revision"])
            rev = before_rev + 1
            entity["revision"] = rev
            self._conn.execute(
                """
                UPDATE requirements
                SET source_entity_ref = ?, source_url = ?, body = ?, content_hash = ?,
                    revision = ?, observed_at = ?, parsed_checks_json = ?, extra_json = ?
                WHERE requirement_id = ?
                """,
                (
                    source_entity_ref or row["source_entity_ref"],
                    source_url or row["source_url"],
                    body,
                    chash,
                    rev,
                    now,
                    json.dumps(parsed_checks) if parsed_checks is not None else row["parsed_checks_json"],
                    json.dumps(extra or {}),
                    requirement_id,
                ),
            )
            self._record_revision(
                "requirement", requirement_id, rev, chash, entity, raw_observation or entity, now
            )
            chg = build_change_event(
                "requirement",
                requirement_id,
                "requirements_changed",
                before_rev,
                rev,
                source_method,
                source_url or row["source_url"],
                confidence,
                entity,
                observed_at=now,
            )
            self._bump_snapshot_revision_and_append_change(meta, chg)
            self._flag_dependent_drafts(requirement_id, rev, now)
            return chg

    def _flag_dependent_drafts(self, requirement_id: str, new_revision: int, now: str) -> None:
        rows = self._conn.execute(
            "SELECT * FROM drafts WHERE status IN ('pending', 'staged', 'needs_review')"
        ).fetchall()
        for r in rows:
            rec = json.loads(r["record_json"])
            reqs = rec.get("baseline", {}).get("requirements", [])
            for req_ref in reqs:
                if req_ref.get("requirement_id") == requirement_id and int(req_ref.get("revision", 0)) < new_revision:
                    reason = (
                        f"requirement {requirement_id} updated to revision {new_revision} "
                        f"(draft baseline revision {req_ref.get('revision')})"
                    )
                    self._conn.execute(
                        """
                        UPDATE drafts
                        SET needs_recheck = 1, status = 'needs_review', recheck_reason = ?, updated_at = ?
                        WHERE draft_id = ? AND revision = ?
                        """,
                        (reason, now, r["draft_id"], r["revision"]),
                    )
                    break

    def record_removal_observed(
        self,
        *,
        entity_type: str,
        entity_ref: str,
        evidence_reason: str,
        source_url: Optional[str] = None,
        source_method: str = "semantic",
        confidence: Optional[float] = 1.0,
        observed_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Explicitly record an observed removal with supporting evidence (never inferred from partial views)."""
        if not evidence_reason or not evidence_reason.strip():
            raise ValueError("evidence_reason is required to record removal_observed")
        meta = self._get_meta()
        now = observed_at or utc_now_iso()
        before_rev: Optional[int] = None
        if entity_type == "topic":
            row = self._conn.execute("SELECT revision FROM topics WHERE thread_id = ?", (entity_ref,)).fetchone()
            if row:
                before_rev = int(row["revision"])
        elif entity_type == "message":
            row = self._conn.execute("SELECT revision FROM messages WHERE entity_ref = ?", (entity_ref,)).fetchone()
            if row:
                before_rev = int(row["revision"])
        elif entity_type == "requirement":
            row = self._conn.execute("SELECT revision FROM requirements WHERE requirement_id = ?", (entity_ref,)).fetchone()
            if row:
                before_rev = int(row["revision"])

        chg = build_change_event(
            entity_type,
            entity_ref,
            "removal_observed",
            before_rev,
            None,
            source_method,
            source_url,
            confidence,
            None,
            observed_at=now,
        )
        with self._conn:
            if entity_type == "topic":
                self._conn.execute("DELETE FROM topics WHERE thread_id = ?", (entity_ref,))
            elif entity_type == "message":
                self._conn.execute("DELETE FROM messages WHERE entity_ref = ?", (entity_ref,))
            elif entity_type == "requirement":
                self._conn.execute("DELETE FROM requirements WHERE requirement_id = ?", (entity_ref,))
            self._bump_snapshot_revision_and_append_change(meta, chg)
        return chg

    def save_draft(self, draft_record: dict[str, Any]) -> dict[str, Any]:
        validate_exchange_record(draft_record)
        if draft_record["schema"] != "draft.request.v1":
            raise ValueError("Expected draft.request.v1 record")
        draft_id = draft_record["draft_id"]
        revision = int(draft_record["revision"])
        now = utc_now_iso()
        canonical_payload = json.dumps(draft_record, sort_keys=True, ensure_ascii=False)

        with self._conn:
            existing = self._conn.execute(
                "SELECT * FROM drafts WHERE draft_id = ? AND revision = ?",
                (draft_id, revision),
            ).fetchone()
            if existing is not None:
                existing_canonical = json.dumps(
                    json.loads(existing["record_json"]), sort_keys=True, ensure_ascii=False
                )
                if existing_canonical != canonical_payload:
                    raise RecordConflictError(
                        f"Draft {draft_id!r} revision {revision} already stored with different content"
                    )
                return dict(existing)

            self._conn.execute(
                """
                INSERT INTO drafts (draft_id, revision, record_json, status, needs_recheck, updated_at)
                VALUES (?, ?, ?, 'pending', 0, ?)
                """,
                (draft_id, revision, json.dumps(draft_record, ensure_ascii=False), now),
            )
            return dict(
                self._conn.execute(
                    "SELECT * FROM drafts WHERE draft_id = ? AND revision = ?",
                    (draft_id, revision),
                ).fetchone()
            )

    def get_draft_row(self, draft_id: str, revision: Optional[int] = None) -> Optional[dict[str, Any]]:
        if revision is not None:
            row = self._conn.execute(
                "SELECT * FROM drafts WHERE draft_id = ? AND revision = ?",
                (draft_id, int(revision)),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM drafts WHERE draft_id = ? ORDER BY revision DESC LIMIT 1",
                (draft_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_draft_status(
        self,
        draft_id: str,
        revision: int,
        status: str,
        *,
        needs_recheck: Optional[bool] = None,
        recheck_reason: Optional[str] = None,
        staged_at: Optional[str] = None,
        submitted_message_id: Optional[str] = None,
    ) -> None:
        row = self._conn.execute(
            "SELECT * FROM drafts WHERE draft_id = ? AND revision = ?", (draft_id, revision)
        ).fetchone()
        if row is None:
            return
        with self._conn:
            self._conn.execute(
                """
                UPDATE drafts
                SET status = ?,
                    needs_recheck = ?,
                    recheck_reason = ?,
                    staged_at = COALESCE(?, staged_at),
                    submitted_message_id = COALESCE(?, submitted_message_id),
                    updated_at = ?
                WHERE draft_id = ? AND revision = ?
                """,
                (
                    status,
                    int(needs_recheck) if needs_recheck is not None else row["needs_recheck"],
                    recheck_reason if recheck_reason is not None else row["recheck_reason"],
                    staged_at,
                    submitted_message_id,
                    utc_now_iso(),
                    draft_id,
                    revision,
                ),
            )

    def save_receipt(self, receipt_record: dict[str, Any]) -> dict[str, Any]:
        validate_exchange_record(receipt_record)
        if receipt_record["schema"] != "handoff.receipt.v1":
            raise ValueError("Expected handoff.receipt.v1 record")
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO receipts (receipt_id, record_id, draft_id, draft_revision, status, observed_at, record_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(receipt_id) DO NOTHING
                """,
                (
                    receipt_record["receipt_id"],
                    receipt_record["record_id"],
                    receipt_record["draft_id"],
                    int(receipt_record["draft_revision"]),
                    receipt_record["status"],
                    receipt_record["observed_at"],
                    json.dumps(receipt_record, ensure_ascii=False),
                ),
            )
        return receipt_record

    def list_receipts(self, draft_id: Optional[str] = None) -> list[dict[str, Any]]:
        if draft_id:
            rows = self._conn.execute(
                "SELECT record_json FROM receipts WHERE draft_id = ? ORDER BY observed_at, rowid",
                (draft_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT record_json FROM receipts ORDER BY observed_at, rowid"
            ).fetchall()
        return [json.loads(r["record_json"]) for r in rows]

    def ingest_record(self, record: dict[str, Any]) -> str:
        """Persist a validated record idempotently; raise RecordConflictError on content conflict."""
        validate_exchange_record(record)
        rec_id = record["record_id"]
        payload_hash = compute_bytes_sha256(
            json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
        )
        existing = self._conn.execute(
            "SELECT * FROM ingested_records WHERE record_id = ?", (rec_id,)
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise RecordConflictError(
                    f"record_id {rec_id!r} redelivered with conflicting payload"
                )
            return "duplicate"

        if record["schema"] == "draft.request.v1":
            self.save_draft(record)
        elif record["schema"] == "handoff.receipt.v1":
            self.save_receipt(record)

        with self._conn:
            self._conn.execute(
                "INSERT INTO ingested_records (record_id, schema, payload_hash, ingested_at) VALUES (?, ?, ?, ?)",
                (rec_id, record["schema"], payload_hash, utc_now_iso()),
            )
        return "accepted"

    def get_revisions(self, entity_type: str, entity_ref: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM revisions WHERE entity_type = ? AND entity_ref = ? ORDER BY revision ASC",
            (entity_type, entity_ref),
        ).fetchall()
        return [dict(r) for r in rows]

    def export_snapshot(
        self,
        *,
        producer_id: Optional[str] = None,
        record_id: Optional[str] = None,
        cutover: bool = False,
        advance_export_cursor: bool = False,
    ) -> dict[str, Any]:
        """Export a validated forum.snapshot.v1 record from current mirror state."""
        meta = self._get_meta()
        if cutover and int(meta["exported_snapshot_revision"]) > 0:
            next_rev = int(meta["snapshot_revision"]) + 1
            with self._conn:
                self._conn.execute(
                    "UPDATE mirror_meta SET snapshot_revision = ?, updated_at = ? WHERE scope_key = ?",
                    (next_rev, utc_now_iso(), meta["scope_key"]),
                )
            meta = self._get_meta(meta["scope_key"])

        now = utc_now_iso()
        forum_row = self._conn.execute(
            "SELECT * FROM forums WHERE forum_id = ?", (meta["forum_id"],)
        ).fetchone()
        if forum_row is None:
            forum_obj = {
                "forum_id": meta["forum_id"],
                "name": f"Forum {meta['forum_id']}",
                "source_url": build_discord_channel_url(meta["guild_id"], meta["forum_id"]),
                "tags": [],
                "revision": 1,
                "observed_at": now,
            }
        else:
            forum_obj = {
                "forum_id": forum_row["forum_id"],
                "name": forum_row["name"],
                "source_url": forum_row["source_url"],
                "tags": json.loads(forum_row["tags_json"]),
                "revision": int(forum_row["revision"]),
                "observed_at": forum_row["observed_at"],
                **json.loads(forum_row["extra_json"]),
            }

        topics_rows = self._conn.execute(
            "SELECT * FROM topics WHERE forum_id = ? ORDER BY thread_id",
            (meta["forum_id"],),
        ).fetchall()
        topics_list = []
        known_tids = set()
        for t in topics_rows:
            known_tids.add(t["thread_id"])
            t_obj = {
                "thread_id": t["thread_id"],
                "forum_id": t["forum_id"],
                "title": t["title"],
                "source_url": t["source_url"],
                "revision": int(t["revision"]),
                "observed_at": t["observed_at"],
                "tags": json.loads(t["tags_json"]),
                "starter_message_id": t["starter_message_id"],
                "archived": None if t["archived"] is None else bool(t["archived"]),
                "locked": None if t["locked"] is None else bool(t["locked"]),
                "remote_last_activity_at": t["remote_last_activity_at"],
                **json.loads(t["extra_json"]),
            }
            if t["observation_ref"]:
                t_obj["observation_ref"] = t["observation_ref"]
            topics_list.append(t_obj)

        messages_rows = self._conn.execute(
            "SELECT * FROM messages ORDER BY observed_at, entity_ref"
        ).fetchall()
        messages_list = []
        for m in messages_rows:
            if m["thread_id"] not in known_tids:
                continue
            msg_obj = {
                "entity_ref": m["entity_ref"],
                "message_id": m["message_id"],
                "thread_id": m["thread_id"],
                "author": json.loads(m["author_json"]),
                "body": m["body"],
                "content_hash": m["content_hash"],
                "revision": int(m["revision"]),
                "observed_at": m["observed_at"],
                "remote_created_at": m["remote_created_at"],
                "remote_edited_at": m["remote_edited_at"],
                "source_url": m["source_url"],
                "identity_verified": bool(m["identity_verified"]),
                "attachments": json.loads(m["attachments_json"]),
                **json.loads(m["extra_json"]),
            }
            if m["recognition_json"]:
                msg_obj["recognition"] = json.loads(m["recognition_json"])
            messages_list.append(msg_obj)

        req_rows = self._conn.execute(
            "SELECT * FROM requirements WHERE forum_id = ? ORDER BY requirement_id",
            (meta["forum_id"],),
        ).fetchall()
        req_list = []
        for r in req_rows:
            req_obj = {
                "requirement_id": r["requirement_id"],
                "source_entity_ref": r["source_entity_ref"],
                "source_url": r["source_url"],
                "body": r["body"],
                "content_hash": r["content_hash"],
                "revision": int(r["revision"]),
                "observed_at": r["observed_at"],
                **json.loads(r["extra_json"]),
            }
            if r["parsed_checks_json"]:
                req_obj["parsed_checks"] = json.loads(r["parsed_checks_json"])
            req_list.append(req_obj)

        snap_rev = int(meta["snapshot_revision"])
        cur_seq = int(meta["cursor_seq"])
        snap = {
            "schema": "forum.snapshot.v1",
            "record_id": record_id or make_record_id("snap"),
            "producer_id": producer_id or meta["producer_id"],
            "created_at": now,
            "scope": {
                "guild_id": meta["guild_id"],
                "forum_id": meta["forum_id"],
                "actor": json.loads(meta["actor_json"]),
            },
            "snapshot_id": meta["snapshot_id"],
            "revision": snap_rev,
            "captured_at": now,
            "cursor": {
                "stream_id": meta["stream_id"],
                "position": str(cur_seq),
            },
            "forum": forum_obj,
            "topics": topics_list,
            "messages": messages_list,
            "requirements": req_list,
            "coverage": self.get_coverage(),
        }
        validated = validate_exchange_record(snap)
        if advance_export_cursor:
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE mirror_meta
                    SET exported_snapshot_revision = ?, exported_cursor_seq = ?, updated_at = ?
                    WHERE scope_key = ?
                    """,
                    (snap_rev, cur_seq, utc_now_iso(), meta["scope_key"]),
                )
        return validated

    def current_cursor(self) -> dict[str, str]:
        meta = self._get_meta()
        return {"stream_id": meta["stream_id"], "position": str(meta["cursor_seq"])}

    def exported_checkpoint(self) -> dict[str, Any]:
        """Return the last exported snapshot revision and cursor position."""
        meta = self._get_meta()
        return {
            "exported_snapshot_revision": int(meta["exported_snapshot_revision"]),
            "snapshot_revision": int(meta["snapshot_revision"]),
            "cursor": {
                "stream_id": meta["stream_id"],
                "position": str(meta["exported_cursor_seq"]),
            },
        }

    def export_changes(
        self,
        after_cursor: Optional[dict[str, str]] = None,
        *,
        producer_id: Optional[str] = None,
        record_id: Optional[str] = None,
        advance_export_cursor: bool = False,
    ) -> dict[str, Any]:
        """Export validated forum.changes.v1 batch after the given or last-exported cursor."""
        meta = self._get_meta()
        stream_id = meta["stream_id"]
        if after_cursor is not None:
            if after_cursor.get("stream_id") != stream_id:
                raise CursorStreamMismatchError(
                    f"Cursor stream_id {after_cursor.get('stream_id')!r} does not match store stream_id {stream_id!r}"
                )
            try:
                after_seq = int(after_cursor["position"])
            except Exception as exc:
                raise ValueError(f"Invalid cursor position {after_cursor.get('position')!r}") from exc
        else:
            after_seq = int(meta["exported_cursor_seq"])
        eff_after_cursor = {"stream_id": stream_id, "position": str(after_seq)}

        rows = self._conn.execute(
            "SELECT * FROM changes WHERE stream_id = ? AND seq > ? ORDER BY seq ASC",
            (stream_id, after_seq),
        ).fetchall()

        changes_list = []
        last_seq = after_seq
        for r in rows:
            last_seq = int(r["seq"])
            changes_list.append(
                {
                    "change_id": r["change_id"],
                    "entity_type": r["entity_type"],
                    "entity_ref": r["entity_ref"],
                    "kind": r["kind"],
                    "before_revision": r["before_revision"],
                    "after_revision": r["after_revision"],
                    "observed_at": r["observed_at"],
                    "source": json.loads(r["source_json"]),
                    "after": json.loads(r["after_json"]) if r["after_json"] else None,
                }
            )

        anchor_rev = int(meta["exported_snapshot_revision"]) or int(meta["snapshot_revision"])
        now = utc_now_iso()
        batch = {
            "schema": "forum.changes.v1",
            "record_id": record_id or make_record_id("chgbatch"),
            "producer_id": producer_id or meta["producer_id"],
            "created_at": now,
            "scope": {
                "guild_id": meta["guild_id"],
                "forum_id": meta["forum_id"],
                "actor": json.loads(meta["actor_json"]),
            },
            "snapshot_id": meta["snapshot_id"],
            "snapshot_revision": anchor_rev,
            "after_cursor": eff_after_cursor,
            "cursor": {"stream_id": stream_id, "position": str(last_seq)},
            "changes": changes_list,
        }
        validated = validate_exchange_record(batch)
        if advance_export_cursor:
            with self._conn:
                self._conn.execute(
                    "UPDATE mirror_meta SET exported_cursor_seq = ?, updated_at = ? WHERE scope_key = ?",
                    (last_seq, utc_now_iso(), meta["scope_key"]),
                )
        return validated

    def export_exchange_dir(
        self,
        exchange_dir: str | Path,
        *,
        force_snapshot: bool = False,
        export_empty_changes: bool = False,
    ) -> dict[str, Any]:
        """Export ordered bootstrap snapshot, incremental delta batch, or snapshot cutover into exchange_dir.

        Directory layout matches consumer expectations:
        - snapshots/  (forum.snapshot.v1)
        - changes/    (forum.changes.v1)
        - acks/       (consumer acknowledgements)
        - drafts/     (incoming draft.request.v1)
        - receipts/   (outgoing handoff.receipt.v1)
        - archive/    (superseded pre-cutover delta files)
        """
        ex_dir = Path(exchange_dir)
        snapshots_dir = ex_dir / "snapshots"
        changes_dir = ex_dir / "changes"
        acks_dir = ex_dir / "acks"
        drafts_dir = ex_dir / "drafts"
        receipts_dir = ex_dir / "receipts"
        archive_dir = ex_dir / "archive" / "changes"
        for d in (snapshots_dir, changes_dir, acks_dir, drafts_dir, receipts_dir):
            d.mkdir(parents=True, exist_ok=True)

        meta = self._get_meta()
        is_bootstrap = int(meta["exported_snapshot_revision"]) == 0
        if is_bootstrap or force_snapshot:
            # Archive superseded delta files before writing a new snapshot cutover so a restarted
            # consumer processes snapshots first and does not replay stale pre-cutover deltas.
            if changes_dir.exists():
                pending_deltas = sorted(changes_dir.glob("*.json"))
                if pending_deltas:
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    for old_file in pending_deltas:
                        shutil.move(str(old_file), str(archive_dir / old_file.name))

            snap = self.export_snapshot(
                cutover=(force_snapshot and not is_bootstrap),
                advance_export_cursor=True,
            )
            snap_name = (
                f"{int(snap['revision']):06d}-"
                f"{int(snap['cursor']['position']):08d}-"
                f"{snap['record_id']}.snapshot.v1.json"
            )
            snap_path = write_record_atomic(snapshots_dir / snap_name, snap)
            return {
                "mode": "bootstrap_snapshot" if is_bootstrap else "cutover_snapshot",
                "snapshot": str(snap_path),
                "snapshot_revision": snap["revision"],
                "cursor": snap["cursor"],
                "changes": None,
                "change_count": 0,
            }

        batch = self.export_changes(after_cursor=None, advance_export_cursor=True)
        if not batch["changes"] and not export_empty_changes:
            return {
                "mode": "quiet_noop",
                "snapshot": None,
                "snapshot_revision": batch["snapshot_revision"],
                "cursor": batch["cursor"],
                "changes": None,
                "change_count": 0,
            }

        chg_name = (
            f"{int(batch['snapshot_revision']):06d}-"
            f"{int(batch['after_cursor']['position']):08d}-"
            f"{int(batch['cursor']['position']):08d}-"
            f"{batch['record_id']}.changes.v1.json"
        )
        chg_path = write_record_atomic(changes_dir / chg_name, batch)
        return {
            "mode": "incremental_changes",
            "snapshot": None,
            "snapshot_revision": batch["snapshot_revision"],
            "after_cursor": batch["after_cursor"],
            "cursor": batch["cursor"],
            "changes": str(chg_path),
            "change_count": len(batch["changes"]),
        }

    def search(self, query: str, forum_id: Optional[str] = None) -> dict[str, Any]:
        """Local search across topics, messages, unresolved observations, and requirements."""
        q = f"%{(query or '').strip().lower()}%"
        t_params: list[Any] = [q, q]
        t_sql = "SELECT * FROM topics WHERE (LOWER(title) LIKE ? OR LOWER(tags_json) LIKE ?)"
        if forum_id:
            t_sql += " AND forum_id = ?"
            t_params.append(forum_id)
        t_sql += " ORDER BY observed_at DESC"
        topics = [
            {
                "thread_id": r["thread_id"],
                "title": r["title"],
                "source_url": r["source_url"],
                "tags": json.loads(r["tags_json"]),
                "revision": int(r["revision"]),
                "observed_at": r["observed_at"],
            }
            for r in self._conn.execute(t_sql, t_params).fetchall()
        ]

        m_sql = """
            SELECT m.*, t.title AS topic_title
            FROM messages m
            LEFT JOIN topics t ON m.thread_id = t.thread_id
            WHERE (LOWER(m.body) LIKE ? OR LOWER(m.author_json) LIKE ?)
        """
        m_params: list[Any] = [q, q]
        if forum_id:
            m_sql += " AND t.forum_id = ?"
            m_params.append(forum_id)
        m_sql += " ORDER BY m.observed_at DESC"
        messages = [
            {
                "entity_ref": r["entity_ref"],
                "message_id": r["message_id"],
                "thread_id": r["thread_id"],
                "topic_title": r["topic_title"],
                "author": json.loads(r["author_json"]),
                "body": r["body"],
                "source_url": r["source_url"],
                "revision": int(r["revision"]),
                "observed_at": r["observed_at"],
            }
            for r in self._conn.execute(m_sql, m_params).fetchall()
        ]

        r_sql = "SELECT * FROM requirements WHERE LOWER(body) LIKE ?"
        r_params: list[Any] = [q]
        if forum_id:
            r_sql += " AND forum_id = ?"
            r_params.append(forum_id)
        requirements = [
            {
                "requirement_id": r["requirement_id"],
                "source_url": r["source_url"],
                "body": r["body"],
                "revision": int(r["revision"]),
                "observed_at": r["observed_at"],
            }
            for r in self._conn.execute(r_sql, r_params).fetchall()
        ]

        u_sql = "SELECT * FROM unresolved_topics WHERE resolved_thread_id IS NULL AND LOWER(title) LIKE ?"
        u_params: list[Any] = [q]
        if forum_id:
            u_sql += " AND forum_id = ?"
            u_params.append(forum_id)
        unresolved = [
            {
                "observation_ref": r["observation_ref"],
                "title": r["title"],
                "reason": r["reason"],
                "observed_at": r["observed_at"],
            }
            for r in self._conn.execute(u_sql, u_params).fetchall()
        ]

        return {
            "query": query,
            "topics": topics,
            "messages": messages,
            "requirements": requirements,
            "unresolved_topics": unresolved,
        }

    def get_topic_status(
        self,
        thread_id: str,
        since_position: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Return structured local status for a topic with source links and freshness."""
        tid = require_discord_id(thread_id, "thread_id")
        topic = self._conn.execute("SELECT * FROM topics WHERE thread_id = ?", (tid,)).fetchone()
        if topic is None:
            return None

        msgs = self._conn.execute(
            "SELECT * FROM messages WHERE thread_id = ? ORDER BY COALESCE(remote_created_at, observed_at), entity_ref",
            (tid,),
        ).fetchall()
        starter = None
        if topic["starter_message_id"]:
            for m in msgs:
                if m["message_id"] == topic["starter_message_id"]:
                    starter = dict(m)
                    break
        if starter is None and msgs:
            starter = dict(msgs[0])

        contributors = []
        seen_contrib = set()
        for m in msgs:
            auth = json.loads(m["author_json"])
            key = (auth.get("user_id"), auth.get("label"))
            if key not in seen_contrib:
                seen_contrib.add(key)
                contributors.append(auth)

        reqs = self._conn.execute(
            "SELECT requirement_id, revision, observed_at, source_url FROM requirements WHERE forum_id = ?",
            (topic["forum_id"],),
        ).fetchall()

        since_seq = int(since_position) if since_position and since_position.isdigit() else 0
        msg_refs = [m["entity_ref"] for m in msgs]
        placeholders = ",".join("?" for _ in msg_refs) if msg_refs else "'__none__'"
        chg_sql = f"""
            SELECT change_id, entity_type, entity_ref, kind, before_revision, after_revision, observed_at
            FROM changes
            WHERE seq > ? AND (
                (entity_type = 'topic' AND entity_ref = ?)
                OR (entity_type = 'message' AND entity_ref IN ({placeholders}))
                OR entity_type = 'requirement'
            )
            ORDER BY seq ASC
        """
        params: list[Any] = [since_seq, tid] + msg_refs
        recent_changes = [dict(c) for c in self._conn.execute(chg_sql, params).fetchall()]

        return {
            "thread_id": tid,
            "forum_id": topic["forum_id"],
            "title": topic["title"],
            "source_url": topic["source_url"],
            "tags": json.loads(topic["tags_json"]),
            "archived": None if topic["archived"] is None else bool(topic["archived"]),
            "locked": None if topic["locked"] is None else bool(topic["locked"]),
            "revision": int(topic["revision"]),
            "observed_at": topic["observed_at"],
            "last_deep_read_at": topic["last_deep_read_at"],
            "remote_last_activity_at": topic["remote_last_activity_at"],
            "message_count": len(msgs),
            "starter_summary": starter["body"] if starter else None,
            "starter_source_url": starter["source_url"] if starter else None,
            "recent_contributors": contributors,
            "latest_messages": [
                {
                    "entity_ref": m["entity_ref"],
                    "message_id": m["message_id"],
                    "author": json.loads(m["author_json"]),
                    "body": m["body"],
                    "revision": int(m["revision"]),
                    "observed_at": m["observed_at"],
                    "remote_created_at": m["remote_created_at"],
                    "remote_edited_at": m["remote_edited_at"],
                    "source_url": m["source_url"],
                    "identity_verified": bool(m["identity_verified"]),
                }
                for m in msgs[-5:]
            ],
            "requirements": [dict(r) for r in reqs],
            "changes_since_cursor": recent_changes,
            "coverage": self.get_coverage(),
        }

    def get_forum_status(self) -> dict[str, Any]:
        """Return high-level summary of the local mirror."""
        meta = self._get_meta()
        topics_cnt = self._conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        msgs_cnt = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        reqs_cnt = self._conn.execute("SELECT COUNT(*) FROM requirements").fetchone()[0]
        unres_cnt = self._conn.execute(
            "SELECT COUNT(*) FROM unresolved_topics WHERE resolved_thread_id IS NULL"
        ).fetchone()[0]
        drafts_cnt = self._conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
        pending_recheck = self._conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE needs_recheck = 1"
        ).fetchone()[0]
        return {
            "snapshot_id": meta["snapshot_id"],
            "snapshot_revision": int(meta["snapshot_revision"]),
            "guild_id": meta["guild_id"],
            "forum_id": meta["forum_id"],
            "actor": json.loads(meta["actor_json"]),
            "cursor": {"stream_id": meta["stream_id"], "position": str(meta["cursor_seq"])},
            "counts": {
                "topics": int(topics_cnt),
                "messages": int(msgs_cnt),
                "requirements": int(reqs_cnt),
                "unresolved_topics": int(unres_cnt),
                "drafts": int(drafts_cnt),
                "drafts_needing_review": int(pending_recheck),
            },
            "coverage": self.get_coverage(),
            "updated_at": meta["updated_at"],
        }
