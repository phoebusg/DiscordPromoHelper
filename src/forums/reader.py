"""Forum backfill, resumable traversal, incremental refresh, and deep-history rotation."""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

from .config import ForumProfile
from .handoff import SessionController
from .models import (
    build_discord_channel_url,
    is_valid_discord_id,
    normalize_starter_requirement_body,
    parse_discord_channel_url,
    require_discord_id,
    topic_starter_requirement_id,
    utc_now_iso,
)
from .providers.base import DiscordViewProvider, TopicHistoryObservation, TopicObservation
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
        merged_refs: list[dict[str, Any]] = []
        seen_rids: set[str] = set()
        seen_tids: set[str] = set()
        for source_refs in (
            self.profile.requirement_refs,
            self.store.get_requirement_references(self.profile.forum_id),
        ):
            for ref in source_refs or []:
                tid = ref.get("thread_id")
                rid = ref.get("requirement_id")
                if rid and rid in seen_rids:
                    continue
                if tid and tid in seen_tids:
                    continue
                merged_refs.append(dict(ref))
                if rid:
                    seen_rids.add(rid)
                if tid:
                    seen_tids.add(tid)
        reqs = self.provider.read_requirements(
            self.profile.guild_id,
            self.profile.forum_id,
            references=merged_refs,
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

    def _begin_run_metrics(self) -> tuple[float, dict[str, int], int]:
        t0 = time.perf_counter()
        snap_fn = getattr(self.provider, "metrics_snapshot", None)
        start_prov = dict(snap_fn()) if callable(snap_fn) else {}
        start_seq = self.store.current_change_seq(
            guild_id=self.profile.guild_id,
            forum_id=self.profile.forum_id,
        )
        return t0, start_prov, start_seq

    def _finish_run_metrics(
        self,
        run_state: tuple[float, dict[str, int], int],
        *,
        reasons: Optional[list[str]] = None,
        fallback_pages: int = 0,
        extra_cache_hits: int = 0,
    ) -> dict[str, Any]:
        t0, start_prov, start_seq = run_state
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        snap_fn = getattr(self.provider, "metrics_snapshot", None)
        end_prov = dict(snap_fn()) if callable(snap_fn) else {}
        end_seq = self.store.current_change_seq(
            guild_id=self.profile.guild_id,
            forum_id=self.profile.forum_id,
        )
        pages_delta = int(end_prov.get("pages_read", 0)) - int(start_prov.get("pages_read", 0))
        return {
            "elapsed_ms": elapsed_ms,
            "desktop_reads": max(0, int(end_prov.get("desktop_reads", 0)) - int(start_prov.get("desktop_reads", 0))),
            "pages_read": max(fallback_pages, max(0, pages_delta)),
            "cache_hits": (
                max(0, int(end_prov.get("cache_hits", 0)) - int(start_prov.get("cache_hits", 0)))
                + max(0, int(extra_cache_hits))
            ),
            "changed_entities": max(0, int(end_seq) - int(start_seq)),
            "ui_actions": max(0, int(end_prov.get("ui_actions", 0)) - int(start_prov.get("ui_actions", 0))),
            "reasons": list(dict.fromkeys(r for r in (reasons or []) if r)),
        }

    def _sync_topic_starter_requirement(self, thread_id: str, *, history_complete: bool) -> bool:
        """Export the mirrored topic starter's form/body as a topic-bound requirement."""
        starter = self.store.get_topic_starter_message(thread_id)
        if starter is None:
            return False
        raw_body = starter.get("body") or ""
        clean_body = normalize_starter_requirement_body(raw_body)
        if not clean_body:
            return False
        req_id = topic_starter_requirement_id(thread_id)
        src_url = starter.get("source_url") or build_discord_channel_url(
            self.profile.guild_id,
            thread_id,
            starter.get("message_id") or thread_id,
        )
        rec_raw = starter.get("recognition_json")
        if isinstance(rec_raw, str) and rec_raw.strip():
            try:
                rec_obj = json.loads(rec_raw)
            except Exception:
                rec_obj = None
        elif isinstance(starter.get("recognition"), dict):
            rec_obj = starter.get("recognition")
        else:
            rec_obj = None
        src_method = rec_obj.get("method", "semantic") if isinstance(rec_obj, dict) else "semantic"
        self.store.upsert_requirement(
            requirement_id=req_id,
            forum_id=self.profile.forum_id,
            body=clean_body,
            source_entity_ref=starter["entity_ref"],
            source_url=src_url,
            observed_at=starter.get("observed_at"),
            source_method=src_method,
            complete=bool(history_complete),
            raw_observation={
                "raw_body": raw_body,
                "normalized_body": clean_body,
                "starter_message_id": starter.get("message_id"),
                "thread_id": thread_id,
            },
            extra={"kind": "topic_starter_form", "thread_id": thread_id},
        )
        return True

    def _ingest_history_pages(
        self,
        thread_id: str,
        *,
        initial_hist: Optional[TopicHistoryObservation] = None,
    ) -> tuple[bool, list[dict[str, Any]], int, Optional[TopicObservation]]:
        """Read and ingest all accessible history pages for one verified thread_id."""
        cursor: Optional[str] = None
        seen_cursors: set[str] = set()
        all_errors: list[dict[str, Any]] = []
        complete = True
        max_pages = 500
        pages = 0
        latest_topic: Optional[TopicObservation] = None

        while True:
            if pages > 0 and not self.session.can_navigate():
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
            if pages == 1 and initial_hist is not None:
                hist = initial_hist
            else:
                hist = self.provider.read_topic(
                    self.profile.guild_id,
                    self.profile.forum_id,
                    thread_id,
                    cursor=cursor,
                )
            if (
                hist.topic.thread_id != thread_id
                or hist.topic.forum_id != self.profile.forum_id
                or any(msg.thread_id not in (None, thread_id) for msg in hist.messages)
            ):
                all_errors.append(
                    {
                        "code": "history_target_mismatch",
                        "message": "Observed history does not match the requested topic and forum",
                        "thread_id": thread_id,
                    }
                )
                complete = False
                break
            latest_topic = hist.topic
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

        is_complete = complete and not all_errors
        if is_complete:
            self.store.mark_topic_deep_read(thread_id)
        self._sync_topic_starter_requirement(thread_id, history_complete=is_complete)
        return is_complete, all_errors, pages, latest_topic

    def _read_single_topic_history(self, thread_id: str) -> tuple[bool, list[dict[str, Any]]]:
        """Read accessible message history for one verified thread_id."""
        ok, errs, _pages, _top = self._ingest_history_pages(thread_id)
        return ok, errs

    def _update_single_topic_coverage(
        self,
        coverage: dict[str, Any],
        thread_id: str,
        *,
        complete: bool,
        has_reqs: bool,
        req_complete: bool,
        req_errors: list[dict[str, Any]],
        history_errors: list[dict[str, Any]],
        session_errors: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        errors = list(session_errors) + req_errors + history_errors
        pending = [
            dict(p)
            for p in coverage.get("pending", [])
            if p.get("thread_id") not in (None, thread_id)
        ]
        if not complete:
            pending.append(
                {
                    "thread_id": thread_id,
                    "reason": history_errors[0]["code"] if history_errors else "history_partial",
                    "resume_token": f"thread:{thread_id}",
                }
            )
        retained_errors = [
            e for e in coverage.get("errors", []) if e.get("thread_id") not in (None, thread_id)
        ]
        histories = coverage.get("histories", "not_started")
        if histories != "complete" or not complete:
            histories = "partial"
        cov = self.store.update_coverage(
            histories=histories,
            requirements="complete"
            if req_complete
            else ("partial" if has_reqs or errors else coverage.get("requirements", "not_started")),
            pending_known=pending,
            errors=retained_errors + errors,
        )
        return cov, errors

    def refresh_guidance_only(self) -> dict[str, Any]:
        """Refresh forum metadata and forum-wide guidance requirements without directory/topic reads."""
        self.store.ensure_scope(self.profile.guild_id, self.profile.forum_id)
        coverage = self.store.get_coverage()
        run_state = self._begin_run_metrics()
        if not self.profile.catalog_enabled:
            return {
                "paused": True,
                "reason": "catalog_disabled",
                "coverage": coverage,
                "metrics": self._finish_run_metrics(run_state, reasons=["catalog_disabled"]),
            }
        if not self.session.acquire_for_read():
            return {
                "paused": True,
                "reason": "session_in_review_or_staging",
                "coverage": coverage,
                "metrics": self._finish_run_metrics(run_state, reasons=["session_in_review_or_staging"]),
            }
        try:
            session = self.provider.describe_session()
            self.store.ensure_scope(self.profile.guild_id, self.profile.forum_id, actor=session.actor)
            if not self.session.can_navigate():
                return {
                    "paused": True,
                    "reason": "session_paused",
                    "coverage": coverage,
                    "metrics": self._finish_run_metrics(run_state, reasons=["session_paused"]),
                }
            has_reqs, req_errors, req_complete = self._sync_forum_and_requirements(session.errors)
            errors = list(session.errors) + req_errors
            retained_errors = [e for e in coverage.get("errors", []) if e.get("thread_id") is not None]
            cov = self.store.update_coverage(
                requirements="complete"
                if req_complete
                else ("partial" if has_reqs or errors else coverage.get("requirements", "not_started")),
                errors=retained_errors + errors,
            )
            reasons = [e.get("code") for e in errors if e.get("code")] or ["ok"]
            return {
                "paused": False,
                "requirements_complete": req_complete,
                "errors": errors,
                "coverage": cov,
                "metrics": self._finish_run_metrics(run_state, reasons=reasons),
            }
        finally:
            self.session.release_read()

    def discover_topic(
        self,
        target: str,
        *,
        refresh_requirements: bool = True,
    ) -> dict[str, Any]:
        """Discover and verify one explicitly supplied topic URL/ID against observed guild/parent forum."""
        self.store.ensure_scope(self.profile.guild_id, self.profile.forum_id)
        coverage = self.store.get_coverage()
        run_state = self._begin_run_metrics()
        raw_target = str(target or "").strip()
        if is_valid_discord_id(raw_target):
            thread_id = raw_target
            supplied_guild_id: Optional[str] = None
        else:
            parsed = parse_discord_channel_url(raw_target)
            if parsed is None:
                return {
                    "paused": False,
                    "discovered": False,
                    "reason": "invalid_target",
                    "target": raw_target,
                    "coverage": coverage,
                    "metrics": self._finish_run_metrics(run_state, reasons=["invalid_target"]),
                }
            supplied_guild_id, thread_id, _ = parsed
            if supplied_guild_id != self.profile.guild_id:
                return {
                    "paused": False,
                    "discovered": False,
                    "reason": "wrong_guild",
                    "thread_id": thread_id,
                    "observed_guild_id": supplied_guild_id,
                    "coverage": coverage,
                    "metrics": self._finish_run_metrics(run_state, reasons=["wrong_guild"]),
                }
            if thread_id == self.profile.forum_id:
                return {
                    "paused": False,
                    "discovered": False,
                    "reason": "target_is_forum_root",
                    "thread_id": thread_id,
                    "coverage": coverage,
                    "metrics": self._finish_run_metrics(run_state, reasons=["target_is_forum_root"]),
                }

        if not self.profile.catalog_enabled:
            return {
                "paused": True,
                "discovered": False,
                "reason": "catalog_disabled",
                "thread_id": thread_id,
                "coverage": coverage,
                "metrics": self._finish_run_metrics(run_state, reasons=["catalog_disabled"]),
            }
        if not self.session.acquire_for_read():
            return {
                "paused": True,
                "discovered": False,
                "reason": "session_in_review_or_staging",
                "thread_id": thread_id,
                "coverage": coverage,
                "metrics": self._finish_run_metrics(run_state, reasons=["session_in_review_or_staging"]),
            }

        try:
            self.store.invalidate_not_found_cache(
                target_key=thread_id,
                guild_id=self.profile.guild_id,
                forum_id=self.profile.forum_id,
            )
            session = self.provider.describe_session()
            self.store.ensure_scope(self.profile.guild_id, self.profile.forum_id, actor=session.actor)
            if not self.session.can_navigate():
                return {
                    "paused": True,
                    "discovered": False,
                    "reason": "session_paused",
                    "thread_id": thread_id,
                    "coverage": coverage,
                    "metrics": self._finish_run_metrics(run_state, reasons=["session_paused"]),
                }

            first_hist = self.provider.read_topic(
                self.profile.guild_id,
                self.profile.forum_id,
                thread_id,
            )
            parent_meta = (first_hist.topic.raw or {}).get("parent_verification") or {}
            obs_guild_id = first_hist.topic.observed_guild_id or parent_meta.get("observed_guild_id")
            obs_parent_fid = (
                first_hist.topic.observed_parent_forum_id
                or parent_meta.get("observed_parent_forum_id")
            )
            parent_ambiguous = bool(parent_meta.get("parent_ambiguous"))

            reject_reason: Optional[str] = None
            if (
                any(e.get("code") == "topic_unavailable" for e in (first_hist.errors or []))
                or (
                    not first_hist.messages
                    and first_hist.topic.starter_message_id is None
                    and not first_hist.complete
                )
                or (first_hist.topic.source_method == "unavailable" and not first_hist.messages)
            ):
                reject_reason = "topic_unavailable"
            elif first_hist.errors and not first_hist.messages:
                reject_reason = first_hist.errors[0].get("code") or "topic_read_failed"
            elif obs_guild_id is None:
                reject_reason = "unverified_guild"
            elif obs_guild_id != self.profile.guild_id:
                reject_reason = "wrong_guild"
            elif parent_ambiguous:
                reject_reason = "ambiguous_parent_forum"
            elif obs_parent_fid is not None and obs_parent_fid != self.profile.forum_id:
                reject_reason = "wrong_parent_forum"
            elif obs_parent_fid is None:
                reject_reason = "unverified_parent_forum"
            elif (
                first_hist.topic.thread_id != thread_id
                or any(m.thread_id not in (None, thread_id) for m in first_hist.messages)
            ):
                reject_reason = "history_target_mismatch"

            if reject_reason is not None:
                if reject_reason in ("topic_unavailable", "topic_read_failed"):
                    self.store.record_not_found_observation(
                        target_key=thread_id,
                        reason=reject_reason,
                        guild_id=self.profile.guild_id,
                        forum_id=self.profile.forum_id,
                        extra={
                            "observed_guild_id": obs_guild_id,
                            "observed_parent_forum_id": obs_parent_fid,
                            "parent_verification": parent_meta,
                        },
                    )
                return {
                    "paused": False,
                    "discovered": False,
                    "reason": reject_reason,
                    "thread_id": thread_id,
                    "observed_guild_id": obs_guild_id,
                    "observed_parent_forum_id": obs_parent_fid,
                    "identity": {
                        "thread_id": thread_id,
                        "title": first_hist.topic.title,
                        "guild_id": obs_guild_id,
                        "parent_forum_id": obs_parent_fid,
                        "locked": first_hist.topic.locked,
                        "archived": first_hist.topic.archived,
                    },
                    "completeness": {
                        "topic_complete": False,
                        "requirements_complete": False,
                        "coverage": coverage,
                    },
                    "source_observations": {
                        "message_count": len(first_hist.messages),
                        "parent_verification": parent_meta,
                        "locked": first_hist.topic.locked,
                        "archived": first_hist.topic.archived,
                    },
                    "errors": list(session.errors) + list(first_hist.errors or []),
                    "coverage": coverage,
                    "metrics": self._finish_run_metrics(
                        run_state,
                        reasons=[reject_reason],
                        fallback_pages=1,
                    ),
                }

            has_reqs = False
            req_errors: list[dict[str, Any]] = []
            req_complete = True
            if refresh_requirements:
                has_reqs, req_errors, req_complete = self._sync_forum_and_requirements(session.errors)
            else:
                req_complete = coverage.get("requirements") == "complete"

            complete, history_errors, pages_read, latest_topic = self._ingest_history_pages(
                thread_id,
                initial_hist=first_hist,
            )
            cov, errors = self._update_single_topic_coverage(
                coverage,
                thread_id,
                complete=complete,
                has_reqs=has_reqs,
                req_complete=req_complete,
                req_errors=req_errors,
                history_errors=history_errors,
                session_errors=session.errors,
            )
            eff_topic = latest_topic or first_hist.topic
            reasons = [e.get("code") for e in errors if e.get("code")] or ["discovered"]
            return {
                "paused": False,
                "discovered": True,
                "thread_id": thread_id,
                "title": eff_topic.title,
                "observed_guild_id": obs_guild_id,
                "observed_parent_forum_id": obs_parent_fid,
                "locked": eff_topic.locked,
                "archived": eff_topic.archived,
                "threads_read": 1,
                "topic_complete": complete,
                "requirements_complete": req_complete,
                "identity": {
                    "thread_id": thread_id,
                    "title": eff_topic.title,
                    "guild_id": obs_guild_id,
                    "parent_forum_id": obs_parent_fid,
                    "locked": eff_topic.locked,
                    "archived": eff_topic.archived,
                },
                "completeness": {
                    "topic_complete": complete,
                    "requirements_complete": req_complete,
                    "coverage": cov,
                },
                "source_observations": {
                    "starter_message_id": eff_topic.starter_message_id,
                    "parent_verification": parent_meta,
                    "topic_starter_requirement_id": topic_starter_requirement_id(thread_id),
                },
                "errors": errors,
                "coverage": cov,
                "metrics": self._finish_run_metrics(
                    run_state,
                    reasons=reasons,
                    fallback_pages=pages_read,
                ),
            }
        finally:
            self.session.release_read()

    def refresh_topic(
        self,
        thread_id: str,
        *,
        allow_discover: bool = False,
    ) -> dict[str, Any]:
        """Refresh one known destination and guidance without enumerating the forum."""
        thread_id = require_discord_id(thread_id, "thread_id")
        self.store.ensure_scope(self.profile.guild_id, self.profile.forum_id)
        coverage = self.store.get_coverage()
        run_state = self._begin_run_metrics()
        if not self.profile.catalog_enabled:
            return {
                "paused": True,
                "reason": "catalog_disabled",
                "coverage": coverage,
                "metrics": self._finish_run_metrics(run_state, reasons=["catalog_disabled"]),
            }
        topic = self.store.get_topic_status(thread_id)
        if topic is None or topic["forum_id"] != self.profile.forum_id:
            if allow_discover and topic is None:
                return self.discover_topic(thread_id, refresh_requirements=True)
            active_filter: Optional[str] = None
            for err in coverage.get("errors", []):
                if isinstance(err, dict) and err.get("filter_state"):
                    active_filter = str(err["filter_state"])
                    break
            nf_cached = self.store.get_not_found_observation(
                target_key=thread_id,
                filter_state=active_filter,
                guild_id=self.profile.guild_id,
                forum_id=self.profile.forum_id,
            )
            extra_hits = 1 if nf_cached is not None else 0
            nf_obs = nf_cached or self.store.record_not_found_observation(
                target_key=thread_id,
                reason="topic_not_in_configured_mirror",
                filter_state=active_filter,
                guild_id=self.profile.guild_id,
                forum_id=self.profile.forum_id,
            )
            return {
                "paused": True,
                "reason": "topic_not_in_configured_mirror",
                "coverage": coverage,
                "not_found_observation": nf_obs,
                "metrics": self._finish_run_metrics(
                    run_state,
                    reasons=["topic_not_in_configured_mirror"],
                    extra_cache_hits=extra_hits,
                ),
            }
        if not self.session.acquire_for_read():
            return {
                "paused": True,
                "reason": "session_in_review_or_staging",
                "coverage": coverage,
                "metrics": self._finish_run_metrics(run_state, reasons=["session_in_review_or_staging"]),
            }
        try:
            session = self.provider.describe_session()
            self.store.ensure_scope(self.profile.guild_id, self.profile.forum_id, actor=session.actor)
            if not self.session.can_navigate():
                return {
                    "paused": True,
                    "reason": "session_paused",
                    "coverage": coverage,
                    "metrics": self._finish_run_metrics(run_state, reasons=["session_paused"]),
                }
            has_reqs, req_errors, req_complete = self._sync_forum_and_requirements(session.errors)
            complete, history_errors, pages_read, _top = self._ingest_history_pages(thread_id)
            cov, errors = self._update_single_topic_coverage(
                coverage,
                thread_id,
                complete=complete,
                has_reqs=has_reqs,
                req_complete=req_complete,
                req_errors=req_errors,
                history_errors=history_errors,
                session_errors=session.errors,
            )
            reasons = [e.get("code") for e in errors if e.get("code")] or ["ok"]
            return {
                "paused": False,
                "thread_id": thread_id,
                "threads_read": 1,
                "topic_complete": complete,
                "requirements_complete": req_complete,
                "errors": errors,
                "coverage": cov,
                "metrics": self._finish_run_metrics(
                    run_state,
                    reasons=reasons,
                    fallback_pages=pages_read,
                ),
            }
        finally:
            self.session.release_read()

    def initial_backfill(
        self,
        *,
        max_topics_per_run: Optional[int] = None,
    ) -> dict[str, Any]:
        """Run or resume initial backfill of forum guidance, topics, and accessible histories."""
        self.store.ensure_scope(self.profile.guild_id, self.profile.forum_id)
        run_state = self._begin_run_metrics()
        if not self.profile.catalog_enabled:
            return {
                "paused": True,
                "reason": "catalog_disabled",
                "coverage": self.store.get_coverage(
                    guild_id=self.profile.guild_id, forum_id=self.profile.forum_id
                ),
                "metrics": self._finish_run_metrics(run_state, reasons=["catalog_disabled"]),
            }
        if not self.session.acquire_for_read():
            return {
                "paused": True,
                "reason": "session_in_review_or_staging",
                "coverage": self.store.get_coverage(
                    guild_id=self.profile.guild_id, forum_id=self.profile.forum_id
                ),
                "metrics": self._finish_run_metrics(run_state, reasons=["session_in_review_or_staging"]),
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
                        before_topic = (
                            self.store.get_topic_status(tobs.thread_id)
                            if tobs.thread_id
                            else None
                        )
                        tid = self._ingest_topic_obs(tobs)
                        if tid:
                            discovered_tids.append(tid)
                            activity_changed = bool(
                                tobs.remote_last_activity_at
                                and before_topic is not None
                                and tobs.remote_last_activity_at != before_topic.get("remote_last_activity_at")
                            )
                            needs_history = (
                                before_topic is None
                                or before_topic.get("last_deep_read_at") is None
                                or activity_changed
                            )
                            if tid not in pending_known_map and needs_history:
                                pending_known_map[tid] = {
                                    "thread_id": tid,
                                    "reason": "activity_or_new" if activity_changed else "history_unread",
                                    "resume_token": f"thread:{tid}",
                                }
                    if tlist.errors:
                        for err in tlist.errors:
                            e_copy = dict(err)
                            if (
                                getattr(tlist, "filter_state", None)
                                and e_copy.get("code") == "directory_filtered"
                                and not e_copy.get("filter_state")
                            ):
                                e_copy["filter_state"] = tlist.filter_state
                            dir_and_hist_errors.append(e_copy)
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
            reasons = [e.get("code") for e in all_errors if e.get("code")] or ["ok"]
            return {
                "paused": False,
                "threads_read": threads_read,
                "coverage": cov,
                "metrics": self._finish_run_metrics(run_state, reasons=reasons),
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
        run_state = self._begin_run_metrics()
        if not self.profile.catalog_enabled:
            return {
                "paused": True,
                "reason": "catalog_disabled",
                "coverage": self.store.get_coverage(
                    guild_id=self.profile.guild_id, forum_id=self.profile.forum_id
                ),
                "metrics": self._finish_run_metrics(run_state, reasons=["catalog_disabled"]),
            }
        if not self.session.acquire_for_read():
            return {
                "paused": True,
                "reason": "session_in_review_or_staging",
                "coverage": self.store.get_coverage(
                    guild_id=self.profile.guild_id, forum_id=self.profile.forum_id
                ),
                "metrics": self._finish_run_metrics(run_state, reasons=["session_in_review_or_staging"]),
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
                    for err in tlist.errors:
                        e_copy = dict(err)
                        if (
                            getattr(tlist, "filter_state", None)
                            and e_copy.get("code") == "directory_filtered"
                            and not e_copy.get("filter_state")
                        ):
                            e_copy["filter_state"] = tlist.filter_state
                        dir_and_hist_errors.append(e_copy)
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
            reasons = [e.get("code") for e in all_errors if e.get("code")] or ["ok"]
            return {
                "paused": False,
                "threads_read": threads_read,
                "coverage": cov,
                "metrics": self._finish_run_metrics(run_state, reasons=reasons),
            }
        finally:
            self.session.release_read()
