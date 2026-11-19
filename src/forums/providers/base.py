"""Provider interface and seed-backed provider for Discord forum views."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Optional

from ..models import build_discord_channel_url, utc_now_iso


@dataclass
class SessionDescription:
    """Observed Discord desktop/web session state and runtime capabilities."""

    actor: dict[str, Any]
    app_name: str
    window_title: Optional[str]
    window_bbox: Optional[tuple[int, int, int, int]]
    current_url: Optional[str] = None
    capabilities: dict[str, bool] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    observed_at: str = field(default_factory=utc_now_iso)


@dataclass
class ForumObservation:
    """Observed forum header, guidelines, tags, and permissions."""

    forum_id: str
    guild_id: str
    name: str
    source_url: str
    tags: Optional[list[dict[str, Any]]] = None
    guidelines_text: str = ""
    can_create_topic: Optional[bool] = None
    source_method: str = "semantic"
    confidence: Optional[float] = 1.0
    observed_at: str = field(default_factory=utc_now_iso)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TopicObservation:
    """Observed forum topic entry (resolved or provisional/unresolved)."""

    forum_id: str
    title: str
    thread_id: Optional[str] = None
    observation_ref: Optional[str] = None
    source_url: Optional[str] = None
    tags: Optional[list[dict[str, Any]]] = None
    starter_message_id: Optional[str] = None
    archived: Optional[bool] = None
    locked: Optional[bool] = None
    remote_last_activity_at: Optional[str] = None
    observed_guild_id: Optional[str] = None
    observed_parent_forum_id: Optional[str] = None
    source_method: str = "semantic"
    confidence: Optional[float] = 1.0
    observed_at: str = field(default_factory=utc_now_iso)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TopicListObservation:
    """One page/batch of observed topics with cursor and completeness metadata."""

    topics: list[TopicObservation] = field(default_factory=list)
    next_cursor: Optional[str] = None
    complete: bool = False
    errors: list[dict[str, Any]] = field(default_factory=list)
    filter_state: Optional[str] = None
    source_method: str = "semantic"
    observed_at: str = field(default_factory=utc_now_iso)


@dataclass
class MessageObservation:
    """Observed message inside a topic thread."""

    thread_id: Optional[str]
    author: dict[str, Any]
    body: str
    entity_ref: Optional[str] = None
    message_id: Optional[str] = None
    remote_created_at: Optional[str] = None
    remote_edited_at: Optional[str] = None
    source_url: Optional[str] = None
    identity_verified: bool = True
    attachments: Optional[list[dict[str, Any]]] = None
    recognition: Optional[dict[str, Any]] = None
    explicit_edited_marker: bool = False
    provisional_alias: Optional[str] = None
    source_method: str = "semantic"
    confidence: Optional[float] = 1.0
    observed_at: str = field(default_factory=utc_now_iso)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TopicHistoryObservation:
    """Observed topic header + message slice with pagination cursor."""

    topic: TopicObservation
    messages: list[MessageObservation] = field(default_factory=list)
    next_cursor: Optional[str] = None
    complete: bool = False
    errors: list[dict[str, Any]] = field(default_factory=list)
    observed_at: str = field(default_factory=utc_now_iso)


@dataclass
class RequirementObservation:
    """Observed forum guidelines, pinned instructions, or template requirement."""

    requirement_id: str
    forum_id: str
    body: str
    source_entity_ref: Optional[str] = None
    source_url: Optional[str] = None
    parsed_checks: Optional[list[dict[str, Any]]] = None
    complete: bool = True
    errors: list[dict[str, Any]] = field(default_factory=list)
    source_method: str = "semantic"
    confidence: Optional[float] = 1.0
    observed_at: str = field(default_factory=utc_now_iso)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ComposerStageObservation:
    """Observed result of attempting to stage a draft into Discord's composer."""

    status: str  # staged, needs_review, failed
    observed_actor: dict[str, Any]
    destination: dict[str, Any]
    observed_composer_text: str = ""
    staged_evidence_ids: list[str] = field(default_factory=list)
    reason: Optional[str] = None
    observed_at: str = field(default_factory=utc_now_iso)


@dataclass
class SubmissionObservation:
    """Observed post-staging submission state in the destination thread."""

    status: str  # submission_observed, in_discord_review, unresolved, failed
    observed_actor: dict[str, Any]
    message_id: Optional[str] = None
    message_url: Optional[str] = None
    observed_body: Optional[str] = None
    reason: Optional[str] = None
    observed_at: str = field(default_factory=utc_now_iso)


class DiscordViewProvider(ABC):
    """Abstract runtime provider around the Discord desktop/web view."""

    @abstractmethod
    def describe_session(self) -> SessionDescription:
        """Return observed account, window state, and runtime capabilities."""

    @abstractmethod
    def describe_forum(self, guild_id: str, forum_id: str) -> ForumObservation:
        """Read forum header metadata, guidelines, and available tags."""

    @abstractmethod
    def list_topics(
        self,
        guild_id: str,
        forum_id: str,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> TopicListObservation:
        """Enumerate topics from the forum directory/list."""

    @abstractmethod
    def read_topic(
        self,
        guild_id: str,
        forum_id: str,
        thread_id: str,
        cursor: Optional[str] = None,
    ) -> TopicHistoryObservation:
        """Read topic starter, state, and accessible message history."""

    @abstractmethod
    def read_requirements(
        self,
        guild_id: str,
        forum_id: str,
        references: Optional[list[dict[str, str]]] = None,
    ) -> list[RequirementObservation]:
        """Read designated forum guidelines, pinned posts, or template requirements."""

    @abstractmethod
    def stage_draft(
        self,
        draft_request: dict[str, Any],
        resolved_evidence: list[dict[str, Any]],
        expected_actor: Optional[dict[str, Any]] = None,
    ) -> ComposerStageObservation:
        """Navigate to the canonical composer, stage text and evidence, and stop before Send."""

    @abstractmethod
    def observe_submission(
        self,
        draft_request: dict[str, Any],
        receipt: dict[str, Any],
    ) -> SubmissionObservation:
        """Observe whether the user submitted or edited the staged draft."""


class SeededDiscordProvider(DiscordViewProvider):
    """Deterministic provider backed by an in-memory/JSON seed catalogue (for offline reads & tests).

    Loads structured observations such as `private.poi_index_observation.v1` or synthetic test fixtures
    and keeps explicit unresolved/partial coverage boundaries.
    """

    def __init__(
        self,
        *,
        guild_id: str,
        forum_id: str,
        forum_name: str = "Configured Forum",
        actor: Optional[dict[str, Any]] = None,
        seed_path: Optional[str | Path] = None,
    ) -> None:
        self.guild_id = guild_id
        self.forum_id = forum_id
        self.forum_name = forum_name
        self.actor = actor or {"user_id": None, "label": "Local seed session"}
        self.topics: list[TopicObservation] = []
        self.messages_by_thread: dict[str, list[MessageObservation]] = {}
        self.requirements: list[RequirementObservation] = []
        self.composer_state: dict[str, dict[str, Any]] = {}
        self.can_create_topic: Optional[bool] = None
        if seed_path:
            self.load_seed_index(seed_path)

    def load_seed_index(self, path: str | Path) -> None:
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        groups = data.get("groups", [])
        idx = 0
        for g in groups:
            glabel = g.get("label", "")
            for title, tid in g.get("topics", []):
                self.topics.append(
                    TopicObservation(
                        forum_id=self.forum_id,
                        thread_id=str(tid),
                        title=title,
                        source_url=build_discord_channel_url(self.guild_id, str(tid)),
                        tags=[{"tag_id": None, "name": glabel}] if glabel else [],
                        source_method="seed_index",
                        confidence=1.0,
                        raw={"group": glabel, "title": title, "thread_id": str(tid)},
                    )
                )
                idx += 1
            for u in g.get("unlinked", []):
                obs_ref = f"seed-unlinked:{glabel}:{u.get('observed_name', idx)}"
                self.topics.append(
                    TopicObservation(
                        forum_id=self.forum_id,
                        thread_id=None,
                        observation_ref=obs_ref,
                        title=u["observed_name"],
                        source_url=None,
                        tags=[{"tag_id": None, "name": glabel}] if glabel else [],
                        source_method="seed_index",
                        confidence=1.0,
                        raw=u,
                    )
                )
                idx += 1

    def describe_session(self) -> SessionDescription:
        return SessionDescription(
            actor=dict(self.actor),
            app_name="SeededProvider",
            window_title=self.forum_name,
            window_bbox=None,
            current_url=build_discord_channel_url(self.guild_id, self.forum_id),
            capabilities={
                "can_read_semantic": True,
                "can_read_ocr": False,
                "can_navigate_deep_link": False,
                "can_stage_composer": False,
            },
        )

    def describe_forum(self, guild_id: str, forum_id: str) -> ForumObservation:
        return ForumObservation(
            forum_id=forum_id,
            guild_id=guild_id,
            name=self.forum_name,
            source_url=build_discord_channel_url(guild_id, forum_id),
            can_create_topic=self.can_create_topic,
            source_method="seed_index",
        )

    def list_topics(
        self,
        guild_id: str,
        forum_id: str,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> TopicListObservation:
        offset = int(cursor) if cursor and cursor.isdigit() else 0
        slice_items = self.topics[offset : offset + limit]
        next_offset = offset + len(slice_items)
        complete = next_offset >= len(self.topics)
        return TopicListObservation(
            topics=slice_items,
            next_cursor=None if complete else str(next_offset),
            complete=complete,
            source_method="seed_index",
        )

    def read_topic(
        self,
        guild_id: str,
        forum_id: str,
        thread_id: str,
        cursor: Optional[str] = None,
    ) -> TopicHistoryObservation:
        matched = next((t for t in self.topics if t.thread_id == thread_id), None)
        if matched is None:
            matched = TopicObservation(
                forum_id=forum_id,
                thread_id=thread_id,
                title=f"Topic {thread_id}",
                source_url=build_discord_channel_url(guild_id, thread_id),
            )
        msgs = self.messages_by_thread.get(thread_id, [])
        if not msgs:
            return TopicHistoryObservation(
                topic=matched,
                messages=[],
                next_cursor=None,
                complete=False,
                errors=[
                    {
                        "code": "history_not_fetched",
                        "message": "Seed index provides topic links only; live Discord view required for message history",
                        "thread_id": thread_id,
                    }
                ],
            )
        return TopicHistoryObservation(topic=matched, messages=list(msgs), next_cursor=None, complete=True)

    def read_requirements(
        self,
        guild_id: str,
        forum_id: str,
        references: Optional[list[dict[str, str]]] = None,
    ) -> list[RequirementObservation]:
        return list(self.requirements)

    def stage_draft(
        self,
        draft_request: dict[str, Any],
        resolved_evidence: list[dict[str, Any]],
        expected_actor: Optional[dict[str, Any]] = None,
    ) -> ComposerStageObservation:
        return ComposerStageObservation(
            status="failed",
            observed_actor=dict(self.actor),
            destination=dict(draft_request["destination"]),
            reason="seed_provider_read_only",
        )

    def observe_submission(
        self,
        draft_request: dict[str, Any],
        receipt: dict[str, Any],
    ) -> SubmissionObservation:
        return SubmissionObservation(
            status="unresolved",
            observed_actor=dict(self.actor),
            reason="seed_provider_read_only",
        )
