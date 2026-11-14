"""Forum backfill, resumable traversal, incremental refresh, and deep-history rotation."""
from __future__ import annotations

import uuid
from typing import Any, Optional

from .config import ForumProfile
from .handoff import SessionController
from .models import utc_now_iso
from .providers.base import DiscordViewProvider, TopicObservation
from .store import ForumStore


class ForumReader:
    """Orchestrates initial backfill and incremental refresh for a configured ForumProfile."""

    def __init__(
        self,
        profile: ForumProfile,
        store: ForumStore,
        provider: DiscordViewProvider,
        session_controller: Optional[SessionController] = None,
    ) -> None:
        self.profile = profile
        self.store = store
        self.provider = provider
        self.session = session_controller or SessionController()

    def _ingest_topic_obs(self, topic_obs: TopicObservation) -> Optional[str]:
        """Upsert resolved topic or keep unresolved topic distinct; returns thread_id if resolved."""
        if topic_obs.thread_id is None:
            obs_ref = topic_obs.observation_ref or f"unres:{topic_obs.forum_id}:{uuid.uuid4().hex[:12]}"
            self.store.record_unresolved_topic(
                observation_ref=obs_ref,
                forum_id=topic_obs.forum_id,
                title=topic_obs.title,
                reason="unresolved_thread_id",
                observed_at=topic_obs.observed_at,
                raw_observation=topic_obs.raw or {"title": topic_obs.title},
            )
            return None

        self.store.upsert_topic(
            thread_id=topic_obs.thread_id,
            forum_id=topic_obs.forum_id,
            title=topic_obs.title,
            source_url=topic_obs.source_url,
            tags=topic_obs.tags,
            starter_message_id=topic_obs.starter_message_id,
            archived=topic_obs.archived,
            locked=topic_obs.locked,
            remote_last_activity_at=topic_obs.remote_last_activity_at,
            observation_ref=topic_obs.observation_ref,
            observed_at=topic_obs.observed_at,
            source_method=topic_obs.source_method,
            confidence=topic_obs.confidence,
            raw_observation=topic_obs.raw,
        )
        return topic_obs.thread_id

    def _sync_forum_and_requirements(
        self,
        session_errors: list[dict[str, Any]],
    ) -> tuple[bool, list[dict[str, Any]], bool]:
        """Refresh forum metadata and requirement records; return (has_reqs, req_errors, all_complete)."""
        forum_obs = self.provider.describe_forum(self.profile.guild_id, self.profile.forum_id)
        self.store.upsert_forum(
            forum_id=forum_obs.forum_id,
            name=forum_obs.name,
            source_url=forum_obs.source_url,
            tags=forum_obs.tags,
            guidelines_text=forum_obs.guidelines_text,
            observed_at=forum_obs.observed_at,
            source_method=forum_obs.source_method,
            confidence=forum_obs.confidence,
            raw_observation=forum_obs.raw,
        )
        reqs = self.provider.read_requirements(
            self.profile.guild_id,
            self.profile.forum_id,
            references=self.profile.requirement_refs,
        )
        req_errors: list[dict[str, Any]] = []
        req_complete = bool(reqs) and not session_errors
        for req in reqs:
            if req.errors:
                req_errors.extend(req.errors)
                req_complete = False
            if not req.complete:
                req_complete = False
            if not req.body and req.errors:
                continue
            self.store.upsert_requirement(
                requirement_id=req.requirement_id,
                forum_id=req.forum_id,
                body=req.body,
                source_entity_ref=req.source_entity_ref,
                source_url=req.source_url,
                observed_at=req.observed_at,
                parsed_checks=req.parsed_checks,
                source_method=req.source_method,
                confidence=req.confidence,
                complete=req.complete,
                raw_observation=req.raw,
            )
        return bool(reqs), req_errors, req_complete

    def _read_single_topic_history(self, thread_id: str) -> tuple[bool, list[dict[str, Any]]]:
        """Read accessible message history for one verified thread_id."""
        cursor: Optional[str] = None
        seen_cursors: set[str] = set()
        all_errors: list[dict[str, Any]] = []
        complete = True
        max_pages = 500
        pages = 0

        while True:
            if not self.session.can_navigate():
                complete = False
                break
            if pages >= max_pages:
                complete = False
                all_errors.append(
                    {
                        "code": "history_pagination_limit",
                        "message": "Exceeded max pages while reading topic history",
                        "thread_id": thread_id,
                    }
                )
                break
            pages += 1
            hist = self.provider.read_topic(
                self.profile.guild_id,
                self.profile.forum_id,
                thread_id,
                cursor=cursor,
            )
            if hist.topic.thread_id:
                self._ingest_topic_obs(hist.topic)
            for msg in hist.messages:
                self.store.upsert_message(
                    entity_ref=msg.entity_ref,
                    message_id=msg.message_id,
                    thread_id=thread_id,
                    author=msg.author,
                    body=msg.body,
                    observed_at=msg.observed_at,
                    remote_created_at=msg.remote_created_at,
                    remote_edited_at=msg.remote_edited_at,
                    source_url=msg.source_url,
                    identity_verified=msg.identity_verified,
                    attachments=msg.attachments,
                    recognition=msg.recognition,
                    explicit_edited_marker=msg.explicit_edited_marker,
                    provisional_alias=msg.provisional_alias,
                    source_method=msg.source_method,
                    confidence=msg.confidence,
                    raw_observation=msg.raw,
                )
            if hist.errors:
                all_errors.extend(hist.errors)
                complete = False
                break
            if hist.next_cursor and hist.next_cursor != cursor and hist.next_cursor not in seen_cursors:
                seen_cursors.add(hist.next_cursor)
                cursor = hist.next_cursor
                continue
            complete = bool(hist.complete) and not bool(hist.next_cursor)
            break

        if complete and not all_errors:
            self.store.mark_topic_deep_read(thread_id)
        return complete and not all_errors, all_errors

    def initial_backfill(
        self,
        *,
        max_topics_per_run: Optional[int] = None,
    ) -> dict[str, Any]:
        """Run or resume initial backfill of forum guidance, topics, and accessible histories."""
        self.store.ensure_scope(self.profile.guild_id, self.profile.forum_id)
        if not self.profile.catalog_enabled:
            return {
                "paused": True,
                "reason": "catalog_disabled",
                "coverage": self.store.get_coverage(
                    guild_id=self.profile.guild_id, forum_id=self.profile.forum_id
                ),
            }
        if not self.session.acquire_for_read():
            return {
                "paused": True,
                "reason": "session_in_review_or_staging",
                "coverage": self.store.get_coverage(
                    guild_id=self.profile.guild_id, forum_id=self.profile.forum_id
                ),
            }

        try:
            session = self.provider.describe_session()
            self.store.ensure_scope(self.profile.guild_id, self.profile.forum_id, actor=session.actor)

            if not self.session.can_navigate():
                return {
                    "paused": True,
                    "reason": "session_paused",
                    "coverage": self.store.get_coverage(
                        guild_id=self.profile.guild_id, forum_id=self.profile.forum_id
                    ),
                }

            has_reqs, req_errors, req_complete = self._sync_forum_and_requirements(session.errors)

            existing_cov = self.store.get_coverage(
                guild_id=self.profile.guild_id, forum_id=self.profile.forum_id
            )
            pending_known_map: dict[str, dict[str, Any]] = {
                p["thread_id"]: dict(p)
                for p in existing_cov.get("pending", [])
                if p.get("thread_id") is not None
            }

            # Check if directory pagination had a saved resume cursor
            dir_cursor: Optional[str] = None
            for p in existing_cov.get("pending", []):
                if p.get("thread_id") is None and p.get("reason") == "topic_directory_incomplete":
                    dir_cursor = p.get("resume_token")
                    break

            dir_and_hist_errors: list[dict[str, Any]] = []
            seen_dir_cursors: set[str] = {dir_cursor} if dir_cursor else set()
            topics_complete = True
            discovered_tids: list[str] = []

            # Enumerate topic directory pages unless resuming solely unread topic histories
            if existing_cov.get("topics") != "complete" or dir_cursor is not None:
                while True:
                    if not self.session.can_navigate():
                        topics_complete = False
                        break
                    tlist = self.provider.list_topics(
                        self.profile.guild_id,
                        self.profile.forum_id,
                        cursor=dir_cursor,
                    )
                    for tobs in tlist.topics:
                        tid = self._ingest_topic_obs(tobs)
                        if tid:
                            discovered_tids.append(tid)
                            if tid not in pending_known_map:
                                pending_known_map[tid] = {
                                    "thread_id": tid,
                                    "reason": "history_unread",
                                    "resume_token": f"thread:{tid}",
                                }
                    if tlist.errors:
                        dir_and_hist_errors.extend(tlist.errors)
                        topics_complete = False
                        break
                    if (
                        tlist.next_cursor
                        and tlist.next_cursor != dir_cursor
                        and tlist.next_cursor not in seen_dir_cursors
                    ):
                        dir_cursor = tlist.next_cursor
                        seen_dir_cursors.add(dir_cursor)
                        if max_topics_per_run is not None and len(discovered_tids) >= max_topics_per_run:
                            topics_complete = False
                            with self.store._conn:
                                self.store._conn.execute(
                                    "DELETE FROM unresolved_topics WHERE forum_id = ? AND observation_ref LIKE 'page-cursor:%'",
                                    (self.profile.forum_id,),
                                )
                            self.store.record_unresolved_topic(
                                observation_ref=f"page-cursor:{dir_cursor}",
                                forum_id=self.profile.forum_id,
                                title="Pending forum directory page",
                                reason="topic_directory_incomplete",
                                resume_token=dir_cursor,
                            )
                            break
                    else:
                        topics_complete = bool(tlist.complete) and not bool(tlist.next_cursor)
                        if topics_complete:
                            with self.store._conn:
                                self.store._conn.execute(
                                    "DELETE FROM unresolved_topics WHERE forum_id = ? AND observation_ref LIKE 'page-cursor:%'",
                                    (self.profile.forum_id,),
                                )
                        dir_cursor = None
                        break

            # Read histories for pending known topics (up to max_topics_per_run)
            threads_read = 0
            for tid in list(pending_known_map.keys()):
                if max_topics_per_run is not None and threads_read >= max_topics_per_run:
                    break
                if not self.session.can_navigate():
                    break
                ok, errs = self._read_single_topic_history(tid)
                threads_read += 1
                if ok:
                    pending_known_map.pop(tid, None)
                else:
                    dir_and_hist_errors.extend(errs)
                    pending_known_map[tid]["reason"] = errs[0]["code"] if errs else "history_partial"

            all_errors = list(session.errors) + req_errors + dir_and_hist_errors
            remaining_pending = list(pending_known_map.values())
            req_status = (
                "complete"
                if req_complete
                else ("partial" if (has_reqs or session.errors or req_errors) else "not_started")
            )
            topics_status = (
                "complete"
                if (topics_complete and dir_cursor is None and not session.errors and not dir_and_hist_errors)
                else ("partial" if (discovered_tids or remaining_pending or existing_cov.get("topics") != "not_started") else "not_started")
            )
            histories_status = (
                "complete"
                if (topics_status == "complete" and not remaining_pending and not all_errors)
                else ("partial" if (threads_read > 0 or remaining_pending) else "not_started")
            )

            cov = self.store.update_coverage(
                topics=topics_status,
                histories=histories_status,
                requirements=req_status,
                pending_known=remaining_pending,
                errors=all_errors,
                last_success_at=utc_now_iso() if not all_errors else None,
            )
            return {
                "paused": False,
                "threads_read": threads_read,
                "coverage": cov,
            }
        finally:
            self.session.release_read()

    def incremental_refresh(
        self,
        *,
        deep_reread_count: int = 2,
        max_topics_per_run: Optional[int] = None,
    ) -> dict[str, Any]:
        """Refresh requirements, new/active topics, unread histories, and rotate deep history rereads."""
        self.store.ensure_scope(self.profile.guild_id, self.profile.forum_id)
        if not self.profile.catalog_enabled:
            return {
                "paused": True,
                "reason": "catalog_disabled",
                "coverage": self.store.get_coverage(
                    guild_id=self.profile.guild_id, forum_id=self.profile.forum_id
                ),
            }
        if not self.session.acquire_for_read():
            return {
                "paused": True,
                "reason": "session_in_review_or_staging",
                "coverage": self.store.get_coverage(
                    guild_id=self.profile.guild_id, forum_id=self.profile.forum_id
                ),
            }

        try:
            session = self.provider.describe_session()
            self.store.ensure_scope(self.profile.guild_id, self.profile.forum_id, actor=session.actor)

            # 1. High-priority refresh of forum metadata and requirements
            has_reqs, req_errors, req_complete = self._sync_forum_and_requirements(session.errors)

            existing_cov = self.store.get_coverage(
                guild_id=self.profile.guild_id, forum_id=self.profile.forum_id
            )
            pending_known_map: dict[str, dict[str, Any]] = {
                p["thread_id"]: dict(p)
                for p in existing_cov.get("pending", [])
                if p.get("thread_id") is not None
            }

            # 2. Discover new or updated topics (never delete topics missing from partial view!)
            dir_and_hist_errors: list[dict[str, Any]] = []
            cursor: Optional[str] = None
            seen_cursors: set[str] = set()
            topics_complete = True
            discovered_tids: list[str] = []
            while True:
                if not self.session.can_navigate():
                    topics_complete = False
                    break
                tlist = self.provider.list_topics(
                    self.profile.guild_id,
                    self.profile.forum_id,
                    cursor=cursor,
                )
                for tobs in tlist.topics:
                    before_topic = None
                    if tobs.thread_id:
                        before_topic = self.store.get_topic_status(tobs.thread_id)
                    tid = self._ingest_topic_obs(tobs)
                    if tid:
                        discovered_tids.append(tid)
                        # Mark for history read if newly discovered or remote activity changed
                        if before_topic is None or (
                            tobs.remote_last_activity_at
                            and tobs.remote_last_activity_at != before_topic.get("remote_last_activity_at")
                        ):
                            pending_known_map.setdefault(
                                tid,
                                {"thread_id": tid, "reason": "activity_or_new", "resume_token": f"thread:{tid}"},
                            )
                if tlist.errors:
                    dir_and_hist_errors.extend(tlist.errors)
                    topics_complete = False
                    break
                if tlist.next_cursor and tlist.next_cursor != cursor and tlist.next_cursor not in seen_cursors:
                    cursor = tlist.next_cursor
                    seen_cursors.add(cursor)
                else:
                    topics_complete = bool(tlist.complete) and not bool(tlist.next_cursor)
                    if topics_complete:
                        with self.store._conn:
                            self.store._conn.execute(
                                "DELETE FROM unresolved_topics WHERE forum_id = ? AND observation_ref LIKE 'page-cursor:%'",
                                (self.profile.forum_id,),
                            )
                    break

            # 3. Rotate deeper history rereads to catch silent edits despite unchanged counts/activity
            deep_candidates = self.store.pick_topics_for_deep_reread(
                self.profile.forum_id, limit=max(0, deep_reread_count)
            )
            read_queue: list[str] = list(pending_known_map.keys())
            for tid in deep_candidates:
                if tid not in read_queue:
                    read_queue.append(tid)

            threads_read = 0
            for tid in read_queue:
                if max_topics_per_run is not None and threads_read >= max_topics_per_run:
                    break
                if not self.session.can_navigate():
                    break
                ok, errs = self._read_single_topic_history(tid)
                threads_read += 1
                if ok:
                    pending_known_map.pop(tid, None)
                else:
                    dir_and_hist_errors.extend(errs)
                    if tid in pending_known_map:
                        pending_known_map[tid]["reason"] = errs[0]["code"] if errs else "history_partial"

            all_errors = list(session.errors) + req_errors + dir_and_hist_errors
            remaining_pending = list(pending_known_map.values())
            topics_status = (
                "complete"
                if (topics_complete and not session.errors and not dir_and_hist_errors)
                else "partial"
            )
            histories_status = (
                "complete"
                if (topics_status == "complete" and not remaining_pending and not all_errors)
                else ("partial" if (threads_read > 0 or remaining_pending) else existing_cov.get("histories", "not_started"))
            )
            req_status = (
                "complete"
                if req_complete
                else ("partial" if (has_reqs or session.errors or req_errors) else existing_cov.get("requirements", "partial"))
            )

            cov = self.store.update_coverage(
                topics=topics_status,
                histories=histories_status,
                requirements=req_status,
                pending_known=remaining_pending,
                errors=all_errors,
                last_success_at=utc_now_iso() if not all_errors else None,
            )
            return {
                "paused": False,
                "threads_read": threads_read,
                "coverage": cov,
            }
        finally:
            self.session.release_read()
