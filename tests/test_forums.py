"""Tests for forum mirror, v1 exchange contracts, staging handoff, OCR, and CLI target fixes."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from src import promotion_helper, server_config, utils
from src.forums.changes import CursorStreamMismatchError
from src.forums.config import ForumProfile, get_promotable_forum_profiles, load_forum_profiles, upsert_forum_profile
from src.forums.drafts import check_draft_conflicts, render_forum_promo_draft
from src.forums.exchange import (
    ContractValidationError,
    RecordConflictError,
    read_record,
    resolve_evidence_files,
    validate_exchange_record,
    write_record_atomic,
)
from src.forums.handoff import HandoffManager, SessionController
from src.forums.models import (
    build_discord_channel_url,
    build_server_scan_coverage,
    compute_bytes_sha256,
    compute_content_hash,
)
from src.forums.providers.base import (
    ComposerStageObservation,
    DiscordViewProvider,
    ForumObservation,
    MessageObservation,
    RequirementObservation,
    SeededDiscordProvider,
    SessionDescription,
    SubmissionObservation,
    TopicHistoryObservation,
    TopicListObservation,
    TopicObservation,
)
from src.forums.reader import ForumReader
from src.forums.store import ForumStore

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "forum_exchange_v1"

GUILD_ID = "100000000000000001"
FORUM_ID = "100000000000000002"
THREAD_1 = "100000000000000003"
THREAD_2 = "100000000000000004"
THREAD_A = "100000000000000011"
THREAD_B = "100000000000000012"
ACTOR_USER_ID = "100000000000000099"


class FakeForumProvider(DiscordViewProvider):
    """Programmable in-memory provider simulating Discord forum views and composer state."""

    def __init__(self, guild_id: str = GUILD_ID, forum_id: str = FORUM_ID) -> None:
        self.guild_id = guild_id
        self.forum_id = forum_id
        self.actor = {"user_id": ACTOR_USER_ID, "label": "Fixture Reviewer"}
        self.forum = ForumObservation(
            forum_id=forum_id,
            guild_id=guild_id,
            name="Example Supply Forum",
            source_url=build_discord_channel_url(guild_id, forum_id),
            tags=[{"tag_id": "100000000000000201", "name": "Verified"}],
            guidelines_text="Guidelines v1",
            can_create_topic=True,
            observed_at="2026-11-14T10:00:00Z",
        )
        self.requirements = [
            RequirementObservation(
                requirement_id="guidelines-fixture",
                forum_id=forum_id,
                body="Guidelines v1: Include sector name.",
                source_entity_ref="forum-guidelines",
                source_url=build_discord_channel_url(guild_id, forum_id),
                observed_at="2026-11-14T10:00:00Z",
            )
        ]
        # List of pages for list_topics: list[tuple[list[TopicObservation], Optional[str], bool]]
        self.topic_pages: list[TopicListObservation] = []
        # Map thread_id -> TopicHistoryObservation
        self.histories: dict[str, TopicHistoryObservation] = {}
        self.existing_composer_draft: str = ""
        self.send_pressed_count: int = 0
        self.stage_calls: list[dict] = []
        self.next_submission: Optional[SubmissionObservation] = None

    def describe_session(self) -> SessionDescription:
        return SessionDescription(
            actor=dict(self.actor),
            app_name="Discord",
            window_title="Example Supply Forum",
            window_bbox=(0, 0, 1280, 800),
            current_url=build_discord_channel_url(self.guild_id, self.forum_id),
            capabilities={"accessibility": True, "ocr": True, "composer_staging": True},
        )

    def describe_forum(self, guild_id: str, forum_id: str) -> ForumObservation:
        return self.forum

    def list_topics(
        self,
        guild_id: str,
        forum_id: str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> TopicListObservation:
        if not self.topic_pages:
            return TopicListObservation(topics=[], next_cursor=None, complete=True)
        if cursor is None:
            return self.topic_pages[0]
        idx = int(cursor)
        return self.topic_pages[idx]

    def read_topic(
        self,
        guild_id: str,
        forum_id: str,
        thread_id: str,
        cursor: str | None = None,
    ) -> TopicHistoryObservation:
        if thread_id not in self.histories:
            raise KeyError(f"Unknown thread {thread_id}")
        return self.histories[thread_id]

    def read_requirements(
        self,
        guild_id: str,
        forum_id: str,
        references: list[dict[str, str]] | None = None,
    ) -> list[RequirementObservation]:
        return list(self.requirements)

    def stage_draft(
        self,
        draft_request: dict,
        resolved_evidence: list[dict],
        expected_actor: dict | None = None,
    ) -> ComposerStageObservation:
        dest = draft_request["destination"]
        if expected_actor and expected_actor.get("user_id") and self.actor.get("user_id") != expected_actor.get("user_id"):
            return ComposerStageObservation(
                status="needs_review",
                observed_actor=dict(self.actor),
                destination=dest,
                reason=f"account_mismatch: expected {expected_actor}, observed {self.actor}",
            )
        if self.existing_composer_draft.strip():
            return ComposerStageObservation(
                status="needs_review",
                observed_actor=dict(self.actor),
                destination=dest,
                observed_composer_text=self.existing_composer_draft,
                reason="existing_human_draft_in_composer",
            )
        self.stage_calls.append(
            {
                "destination": dest,
                "body": draft_request["body"],
                "evidence": resolved_evidence,
            }
        )
        # Crucially never increment send_pressed_count
        return ComposerStageObservation(
            status="staged",
            observed_actor=dict(self.actor),
            destination=dest,
            observed_composer_text=draft_request["body"],
            staged_evidence_ids=[ev["evidence_id"] for ev in resolved_evidence],
            reason="Verified destination, multiline Unicode text, and attachment preview without Send.",
        )

    def observe_submission(self, draft_request: dict, receipt: dict) -> SubmissionObservation:
        if self.next_submission is not None:
            return self.next_submission
        return SubmissionObservation(
            status="in_discord_review",
            observed_actor=dict(self.actor),
            reason="Draft remains in composer awaiting user Send.",
        )


def _make_profile(tmp_dir: Path, purpose: str = "external_project", stage_enabled: bool = True) -> ForumProfile:
    return ForumProfile(
        profile_id="proj-fixture",
        name="Example Supply Forum",
        guild_id=GUILD_ID,
        forum_id=FORUM_ID,
        source_url=build_discord_channel_url(GUILD_ID, FORUM_ID),
        purpose=purpose,
        catalog_enabled=True,
        promotion_enabled=(purpose == "promotion"),
        external_drafts_enabled=(purpose == "external_project"),
        stage_enabled=stage_enabled,
        transfer_root=str(tmp_dir),
        db_path=str(tmp_dir / "mirror.sqlite3"),
        exchange_dir=str(tmp_dir / "exchange"),
    )


class TestExchangeContracts(unittest.TestCase):
    def test_validate_and_roundtrip_all_public_fixtures(self):
        names = [
            "partial_snapshot.v1.json",
            "resolved_snapshot.v1.json",
            "changes_batch.v1.json",
            "draft_with_attachment.v1.json",
            "staged_receipt.v1.json",
            "human_edited_submission_receipt.v1.json",
        ]
        with tempfile.TemporaryDirectory() as td:
            for name in names:
                rec = read_record(FIXTURE_DIR / name)
                validated = validate_exchange_record(rec)
                out_path = Path(td) / name
                write_record_atomic(out_path, validated)
                roundtripped = read_record(out_path)
                self.assertEqual(roundtripped["record_id"], rec["record_id"])
                if name == "partial_snapshot.v1.json":
                    self.assertEqual(roundtripped["x_consumer_hint"], {"keep_me": True})
                if name == "draft_with_attachment.v1.json":
                    resolved = resolve_evidence_files(rec["evidence"], FIXTURE_DIR)
                    self.assertEqual(len(resolved), 1)
                    self.assertTrue(Path(resolved[0]["resolved_path"]).exists())

    def test_rejects_non_string_ids_and_unresolved_topics_in_snapshot(self):
        snap = read_record(FIXTURE_DIR / "partial_snapshot.v1.json")
        bad_int = copy.deepcopy(snap)
        bad_int["scope"]["guild_id"] = 100000000000000001
        with self.assertRaises(ContractValidationError):
            validate_exchange_record(bad_int)

        bad_null_topic = copy.deepcopy(snap)
        bad_null_topic["topics"][0]["thread_id"] = None
        with self.assertRaises(ContractValidationError):
            validate_exchange_record(bad_null_topic)

        bad_pending = copy.deepcopy(snap)
        del bad_pending["coverage"]["pending"][0]["observation_ref"]
        with self.assertRaises(ContractValidationError):
            validate_exchange_record(bad_pending)

    def test_evidence_rejects_traversal_missing_and_hash_mismatch(self):
        draft = read_record(FIXTURE_DIR / "draft_with_attachment.v1.json")
        bad_trav = copy.deepcopy(draft["evidence"])
        bad_trav[0]["local_ref"] = "../outside.png"
        with self.assertRaises(ContractValidationError):
            resolve_evidence_files(bad_trav, FIXTURE_DIR)

        bad_missing = copy.deepcopy(draft["evidence"])
        bad_missing[0]["local_ref"] = "evidence/missing.png"
        with self.assertRaises(FileNotFoundError):
            resolve_evidence_files(bad_missing, FIXTURE_DIR)

        bad_hash = copy.deepcopy(draft["evidence"])
        bad_hash[0]["sha256"] = "0" * 64
        with self.assertRaises(ContractValidationError):
            resolve_evidence_files(bad_hash, FIXTURE_DIR)

    def test_duplicate_record_id_conflict_and_stream_mismatch(self):
        snap = read_record(FIXTURE_DIR / "partial_snapshot.v1.json")
        with tempfile.TemporaryDirectory() as td:
            store = ForumStore(Path(td) / "store.sqlite3")
            try:
                self.assertEqual(store.ingest_record(snap), "accepted")
                self.assertEqual(store.ingest_record(snap), "duplicate")
                mutated = copy.deepcopy(snap)
                mutated["forum"]["name"] = "Tampered Forum"
                with self.assertRaises(RecordConflictError):
                    store.ingest_record(mutated)

                store.ensure_scope(GUILD_ID, FORUM_ID, snapshot_id="mirror-test")
                with self.assertRaises(CursorStreamMismatchError):
                    store.export_changes(after_cursor={"stream_id": "other-stream", "position": "0"})
            finally:
                store.close()


class TestForumReaderAndChangeDetection(unittest.TestCase):
    def test_new_reply_recorded_once_and_quiet_refresh_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            profile = _make_profile(td_path)
            provider = FakeForumProvider()
            t1 = TopicObservation(
                forum_id=FORUM_ID,
                thread_id=THREAD_1,
                title="Depot One",
                source_url=build_discord_channel_url(GUILD_ID, THREAD_1),
                starter_message_id="100000000000000101",
                remote_last_activity_at="2026-11-14T10:00:00Z",
                observed_at="2026-11-14T10:00:00Z",
            )
            m1 = MessageObservation(
                thread_id=THREAD_1,
                message_id="100000000000000101",
                entity_ref="msg:100000000000000101",
                author={"user_id": "100000000000000050", "label": "Alice"},
                body="Starter body",
                remote_created_at="2026-11-14T10:00:00Z",
                source_url=build_discord_channel_url(GUILD_ID, THREAD_1, "100000000000000101"),
                observed_at="2026-11-14T10:00:00Z",
            )
            provider.topic_pages = [TopicListObservation(topics=[t1], complete=True)]
            provider.histories[THREAD_1] = TopicHistoryObservation(topic=t1, messages=[m1], complete=True)

            store = ForumStore(profile.db_path)
            try:
                reader = ForumReader(profile, store, provider)
                reader.initial_backfill()
                cursor_1 = store.current_cursor()

                # Quiet refresh -> 0 changes
                reader.incremental_refresh(deep_reread_count=1)
                quiet_batch = store.export_changes(after_cursor=cursor_1)
                self.assertEqual(quiet_batch["changes"], [])
                cursor_2 = store.current_cursor()

                # Add one reply -> recorded once; second quiet refresh adds 0 more
                m2 = MessageObservation(
                    thread_id=THREAD_1,
                    message_id="100000000000000102",
                    entity_ref="msg:100000000000000102",
                    author={"user_id": "100000000000000051", "label": "Bob"},
                    body="New field reply",
                    remote_created_at="2026-11-14T10:05:00Z",
                    source_url=build_discord_channel_url(GUILD_ID, THREAD_1, "100000000000000102"),
                    observed_at="2026-11-14T10:05:00Z",
                )
                provider.histories[THREAD_1] = TopicHistoryObservation(topic=t1, messages=[m1, m2], complete=True)
                reader.incremental_refresh(deep_reread_count=1)
                batch = store.export_changes(after_cursor=cursor_2)
                added_msgs = [c for c in batch["changes"] if c["entity_type"] == "message" and c["kind"] == "added"]
                self.assertEqual(len(added_msgs), 1)
                self.assertEqual(added_msgs[0]["entity_ref"], "msg:100000000000000102")

                cursor_3 = store.current_cursor()
                reader.incremental_refresh(deep_reread_count=1)
                self.assertEqual(store.export_changes(after_cursor=cursor_3)["changes"], [])
            finally:
                store.close()

    def test_silent_edit_detected_despite_unchanged_counts_and_activity(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            profile = _make_profile(td_path)
            provider = FakeForumProvider()
            t1 = TopicObservation(
                forum_id=FORUM_ID,
                thread_id=THREAD_1,
                title="Depot One",
                source_url=build_discord_channel_url(GUILD_ID, THREAD_1),
                starter_message_id="100000000000000101",
                remote_last_activity_at="2026-11-14T10:00:00Z",
                observed_at="2026-11-14T10:00:00Z",
            )
            m1 = MessageObservation(
                thread_id=THREAD_1,
                message_id="100000000000000101",
                entity_ref="msg:100000000000000101",
                author={"user_id": "100000000000000050", "label": "Alice"},
                body="Original crate count: 10",
                remote_created_at="2026-11-14T10:00:00Z",
                source_url=build_discord_channel_url(GUILD_ID, THREAD_1, "100000000000000101"),
                observed_at="2026-11-14T10:00:00Z",
            )
            provider.topic_pages = [TopicListObservation(topics=[t1], complete=True)]
            provider.histories[THREAD_1] = TopicHistoryObservation(topic=t1, messages=[m1], complete=True)

            store = ForumStore(profile.db_path)
            try:
                reader = ForumReader(profile, store, provider)
                reader.initial_backfill()
                c0 = store.current_cursor()

                # Silent edit: same message count (1), same remote_last_activity_at, body edited silently
                m1_edited = copy.deepcopy(m1)
                m1_edited.body = "Updated crate count: 14"
                m1_edited.remote_edited_at = "2026-11-14T10:06:00Z"
                m1_edited.observed_at = "2026-11-14T10:07:00Z"
                provider.histories[THREAD_1] = TopicHistoryObservation(topic=t1, messages=[m1_edited], complete=True)

                reader.incremental_refresh(deep_reread_count=1)
                batch = store.export_changes(after_cursor=c0)
                edits = [c for c in batch["changes"] if c["entity_type"] == "message" and c["kind"] == "edited"]
                self.assertEqual(len(edits), 1)
                self.assertEqual(edits[0]["before_revision"], 1)
                self.assertEqual(edits[0]["after_revision"], 2)
                self.assertEqual(edits[0]["after"]["body"], "Updated crate count: 14")
            finally:
                store.close()

    def test_ocr_correction_classified_distinctly_from_remote_edit(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            profile = _make_profile(td_path)
            provider = FakeForumProvider()
            t1 = TopicObservation(
                forum_id=FORUM_ID,
                thread_id=THREAD_1,
                title="Depot OCR",
                source_url=build_discord_channel_url(GUILD_ID, THREAD_1),
                observed_at="2026-11-14T10:00:00Z",
            )
            m1_ocr = MessageObservation(
                thread_id=THREAD_1,
                message_id="100000000000000101",
                entity_ref="msg:100000000000000101",
                author={"user_id": "100000000000000050", "label": "Alice"},
                body="Sect0r N0rth",
                source_method="ocr",
                confidence=0.62,
                recognition={"method": "ocr", "confidence": 0.62},
                remote_created_at="2026-11-14T10:00:00Z",
                remote_edited_at=None,
                source_url=build_discord_channel_url(GUILD_ID, THREAD_1, "100000000000000101"),
                observed_at="2026-11-14T10:00:00Z",
            )
            provider.topic_pages = [TopicListObservation(topics=[t1], complete=True)]
            provider.histories[THREAD_1] = TopicHistoryObservation(topic=t1, messages=[m1_ocr], complete=True)

            store = ForumStore(profile.db_path)
            try:
                reader = ForumReader(profile, store, provider)
                reader.initial_backfill()
                c0 = store.current_cursor()

                # Better OCR / accessibility read with no remote_edited_at change -> recognition_corrected
                m1_clean = copy.deepcopy(m1_ocr)
                m1_clean.body = "Sector North"
                m1_clean.source_method = "accessibility"
                m1_clean.confidence = 0.99
                m1_clean.recognition = {"method": "accessibility", "confidence": 0.99}
                m1_clean.observed_at = "2026-11-14T10:04:00Z"
                provider.histories[THREAD_1] = TopicHistoryObservation(topic=t1, messages=[m1_clean], complete=True)

                reader.incremental_refresh(deep_reread_count=1)
                batch = store.export_changes(after_cursor=c0)
                kinds = [c["kind"] for c in batch["changes"] if c["entity_type"] == "message"]
                self.assertEqual(kinds, ["recognition_corrected"])
            finally:
                store.close()

    def test_requirement_change_flags_dependent_draft_for_review(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            ext_profile = _make_profile(td_path, purpose="external_project")
            profile = _make_profile(td_path, purpose="promotion")
            provider = FakeForumProvider()
            t1 = TopicObservation(
                forum_id=FORUM_ID,
                thread_id=THREAD_1,
                title="Depot Req",
                source_url=build_discord_channel_url(GUILD_ID, THREAD_1),
                observed_at="2026-11-14T10:00:00Z",
            )
            provider.topic_pages = [TopicListObservation(topics=[t1], complete=True)]
            provider.histories[THREAD_1] = TopicHistoryObservation(topic=t1, messages=[], complete=True)

            store = ForumStore(profile.db_path)
            try:
                reader = ForumReader(profile, store, provider)
                reader.initial_backfill()
                snap1 = store.export_snapshot()

                with self.assertRaises(ValueError):
                    render_forum_promo_draft(
                        profile=ext_profile,
                        stream_info=SimpleNamespace(title="Test", stream_url="https://example.invalid/live"),
                        draft_id="draft-blocked-ext",
                        thread_id=THREAD_1,
                    )

                draft = render_forum_promo_draft(
                    profile=profile,
                    stream_info=SimpleNamespace(title="Test", stream_url="https://example.invalid/live"),
                    draft_id="draft-req-check",
                    thread_id=THREAD_1,
                    snapshot_id=snap1["snapshot_id"],
                    snapshot_revision=snap1["revision"],
                    requirements=snap1["requirements"],
                    template="Check-in: {title}",
                )
                store.save_draft(draft)
                c0 = store.current_cursor()

                # Change requirements
                provider.requirements[0] = RequirementObservation(
                    requirement_id="guidelines-fixture",
                    forum_id=FORUM_ID,
                    body="Guidelines v2: Now requires tag selection too.",
                    source_entity_ref="forum-guidelines",
                    source_url=build_discord_channel_url(GUILD_ID, FORUM_ID),
                    observed_at="2026-11-14T10:08:00Z",
                )
                reader.incremental_refresh(deep_reread_count=0)

                batch = store.export_changes(after_cursor=c0)
                req_changes = [c for c in batch["changes"] if c["kind"] == "requirements_changed"]
                self.assertEqual(len(req_changes), 1)
                self.assertEqual(req_changes[0]["after_revision"], 2)

                row = store.get_draft_row("draft-req-check", 1)
                self.assertIsNotNone(row)
                self.assertEqual(row["status"], "needs_review")
                self.assertEqual(row["needs_recheck"], 1)
                self.assertTrue(check_draft_conflicts(draft, store))

                handoff = HandoffManager(store, provider)
                receipt = handoff.stage_draft_request(draft, profile, source_kind="promotion")
                self.assertEqual(receipt["status"], "needs_review")
                self.assertEqual(provider.stage_calls, [])
            finally:
                store.close()

    def test_partial_traversal_resumes_without_false_deletion(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            profile = _make_profile(td_path)
            provider = FakeForumProvider()
            t1 = TopicObservation(
                forum_id=FORUM_ID,
                thread_id=THREAD_1,
                title="Page One Topic",
                source_url=build_discord_channel_url(GUILD_ID, THREAD_1),
                observed_at="2026-11-14T10:00:00Z",
            )
            t2 = TopicObservation(
                forum_id=FORUM_ID,
                thread_id=THREAD_2,
                title="Page Two Topic",
                source_url=build_discord_channel_url(GUILD_ID, THREAD_2),
                observed_at="2026-11-14T10:01:00Z",
            )
            provider.topic_pages = [
                TopicListObservation(topics=[t1], next_cursor="1", complete=False),
                TopicListObservation(topics=[t2], next_cursor=None, complete=True),
            ]
            provider.histories[THREAD_1] = TopicHistoryObservation(topic=t1, messages=[], complete=True)
            provider.histories[THREAD_2] = TopicHistoryObservation(topic=t2, messages=[], complete=True)

            store = ForumStore(profile.db_path)
            try:
                reader = ForumReader(profile, store, provider)
                res1 = reader.initial_backfill(max_topics_per_run=1)
                cov1 = res1["coverage"]
                self.assertEqual(cov1["topics"], "partial")
                self.assertTrue(any(p.get("reason") == "topic_directory_incomplete" for p in cov1["pending"]))

                # Page 2 run only sees Page 2 — Page 1 must NOT be marked removed!
                res2 = reader.initial_backfill(max_topics_per_run=5)
                cov2 = res2["coverage"]
                self.assertEqual(cov2["topics"], "complete")
                self.assertEqual(cov2["histories"], "complete")
                snap = store.export_snapshot()
                self.assertEqual({t["thread_id"] for t in snap["topics"]}, {THREAD_1, THREAD_2})
                all_changes = store.export_changes()["changes"]
                self.assertFalse(any(c["kind"] == "removal_observed" for c in all_changes))
            finally:
                store.close()

    def test_two_unresolved_same_title_topics_stay_distinct_and_reconcile(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            profile = _make_profile(td_path)
            provider = FakeForumProvider()

            unres_a = TopicObservation(
                forum_id=FORUM_ID,
                thread_id=None,
                observation_ref="obs-supply-a",
                title="Supply Cache",
                observed_at="2026-11-14T10:00:00Z",
            )
            unres_b = TopicObservation(
                forum_id=FORUM_ID,
                thread_id=None,
                observation_ref="obs-supply-b",
                title="Supply Cache",
                observed_at="2026-11-14T10:00:05Z",
            )
            # Also verify omitted observation_ref does not collide across two same-title observations
            unres_c1 = TopicObservation(forum_id=FORUM_ID, thread_id=None, title="Supply Cache")
            unres_c2 = TopicObservation(forum_id=FORUM_ID, thread_id=None, title="Supply Cache")
            provider.topic_pages = [
                TopicListObservation(topics=[unres_a, unres_b, unres_c1, unres_c2], complete=True)
            ]

            store = ForumStore(profile.db_path)
            try:
                reader = ForumReader(profile, store, provider)
                cov = reader.initial_backfill()["coverage"]
                # Unresolved topics stay out of exchanged snapshot topics and in pending with thread_id=None
                snap1 = store.export_snapshot()
                self.assertEqual(snap1["topics"], [])
                pending_null = [p for p in cov["pending"] if p["thread_id"] is None]
                self.assertEqual(len(pending_null), 4)
                obs_refs = {p["observation_ref"] for p in pending_null}
                self.assertIn("obs-supply-a", obs_refs)
                self.assertIn("obs-supply-b", obs_refs)

                # Attach distinct held messages locally while unresolved
                store.record_unresolved_topic(
                    observation_ref="obs-supply-a",
                    forum_id=FORUM_ID,
                    title="Supply Cache",
                    messages=[{"author": {"user_id": None, "label": "North"}, "body": "North held note"}],
                )
                store.record_unresolved_topic(
                    observation_ref="obs-supply-b",
                    forum_id=FORUM_ID,
                    title="Supply Cache",
                    messages=[{"author": {"user_id": None, "label": "South"}, "body": "South held note"}],
                )

                # Clean up c1/c2 and resolve obs-supply-a & obs-supply-b
                c0 = store.current_cursor()
                store.resolve_topic_observation(
                    observation_ref="obs-supply-a",
                    thread_id=THREAD_A,
                    source_url=build_discord_channel_url(GUILD_ID, THREAD_A),
                )
                store.resolve_topic_observation(
                    observation_ref="obs-supply-b",
                    thread_id=THREAD_B,
                    source_url=build_discord_channel_url(GUILD_ID, THREAD_B),
                )
                batch = store.export_changes(after_cursor=c0)
                added_topics = [c["entity_ref"] for c in batch["changes"] if c["entity_type"] == "topic" and c["kind"] == "added"]
                self.assertEqual(set(added_topics), {THREAD_A, THREAD_B})

                snap2 = store.export_snapshot()
                msgs_by_tid = {m["thread_id"]: m["body"] for m in snap2["messages"]}
                self.assertEqual(msgs_by_tid[THREAD_A], "North held note")
                self.assertEqual(msgs_by_tid[THREAD_B], "South held note")
            finally:
                store.close()


class TestComposerStagingAndEnablement(unittest.TestCase):
    def test_preserves_existing_human_draft_and_pauses_on_account_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            profile = _make_profile(td_path)
            provider = FakeForumProvider()
            store = ForumStore(profile.db_path)
            try:
                draft = read_record(FIXTURE_DIR / "draft_with_attachment.v1.json")
                profile.transfer_root = str(FIXTURE_DIR)
                handoff = HandoffManager(store, provider)

                # Account mismatch -> needs_review, no Send
                receipt_acct = handoff.stage_draft_request(
                    draft,
                    profile,
                    expected_actor={"user_id": "100000000000000888", "label": "Wrong"},
                )
                self.assertEqual(receipt_acct["status"], "needs_review")
                self.assertEqual(provider.stage_calls, [])

                # Existing human draft (new revision so prior account_mismatch recheck state is not reused) -> needs_review, never overwrite
                provider.existing_composer_draft = "Human half-written reply..."
                draft_r2 = copy.deepcopy(draft)
                draft_r2["record_id"] = "rec-draft-human-02"
                draft_r2["revision"] = 2
                receipt_draft = handoff.stage_draft_request(draft_r2, profile)
                self.assertEqual(receipt_draft["status"], "needs_review")
                self.assertIn("existing_human_draft", receipt_draft["reason"])
                self.assertEqual(provider.stage_calls, [])
                self.assertEqual(provider.send_pressed_count, 0)
            finally:
                store.close()

    def test_staging_multiline_unicode_attachment_pauses_reader_and_reconciles_human_edit(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            profile = _make_profile(td_path)
            profile.transfer_root = str(FIXTURE_DIR)
            provider = FakeForumProvider()
            t1 = TopicObservation(
                forum_id=FORUM_ID,
                thread_id=THREAD_1,
                title="North Ridge Depot",
                source_url=build_discord_channel_url(GUILD_ID, THREAD_1),
                observed_at="2026-11-14T10:00:00Z",
            )
            provider.topic_pages = [TopicListObservation(topics=[t1], complete=True)]
            provider.histories[THREAD_1] = TopicHistoryObservation(topic=t1, messages=[], complete=True)

            store = ForumStore(profile.db_path)
            try:
                session_ctrl = SessionController()
                reader = ForumReader(profile, store, provider, session_controller=session_ctrl)
                reader.initial_backfill()

                draft = read_record(FIXTURE_DIR / "draft_with_attachment.v1.json")
                handoff = HandoffManager(store, provider, session_controller=session_ctrl)
                receipt = handoff.stage_draft_request(draft, profile, source_kind="external")
                self.assertEqual(receipt["status"], "staged")
                self.assertEqual(provider.send_pressed_count, 0)
                self.assertEqual(len(provider.stage_calls), 1)
                self.assertIn("🧭", provider.stage_calls[0]["body"])
                self.assertIn("\n", provider.stage_calls[0]["body"])
                self.assertEqual(provider.stage_calls[0]["evidence"][0]["name"], "synthetic_probe.png")

                # Session lock is now in_discord_review: background reader must pause!
                self.assertTrue(session_ctrl.is_review_active())
                res_paused = reader.incremental_refresh()
                self.assertTrue(res_paused["paused"])
                self.assertEqual(res_paused["reason"], "session_in_review_or_staging")

                # New draft revision arriving during active review must NOT overwrite human's composer edits
                draft_v2 = copy.deepcopy(draft)
                draft_v2["record_id"] = "rec-draft-0002"
                draft_v2["revision"] = 2
                draft_v2["body"] = "Revised body v2"
                receipt_v2 = handoff.stage_draft_request(draft_v2, profile, source_kind="external")
                self.assertEqual(receipt_v2["status"], "needs_review")
                self.assertEqual(receipt_v2["reason"], "composer_review_in_progress")
                self.assertEqual(len(provider.stage_calls), 1)

                # User edits and presses Send in Discord; reconcile observes new message hash and releases review
                human_body = draft["body"] + "\nHuman verified seals."
                provider.next_submission = SubmissionObservation(
                    status="submission_observed",
                    observed_actor=dict(provider.actor),
                    message_id="100000000000000199",
                    message_url=build_discord_channel_url(GUILD_ID, THREAD_1, "100000000000000199"),
                    observed_body=human_body,
                    reason="Observed human-edited post.",
                )
                rec_sub = handoff.reconcile_submission("draft-north-ridge-01", draft_revision=1, profile=profile)
                self.assertEqual(rec_sub["status"], "submission_observed")
                self.assertEqual(rec_sub["observed_body_hash"], compute_content_hash(human_body))
                self.assertFalse(session_ctrl.is_review_active())

                # Re-staging same draft after submission must not duplicate-stage
                dup_stage = handoff.stage_draft_request(draft, profile, source_kind="external")
                self.assertEqual(dup_stage["status"], "submission_observed")
                self.assertEqual(len(provider.stage_calls), 1)
            finally:
                store.close()

    def test_enablement_separation_and_server_config_helpers(self):
        with self.assertRaises(ValueError):
            ForumProfile(
                profile_id="bad-proj",
                name="Bad",
                guild_id=GUILD_ID,
                forum_id=FORUM_ID,
                purpose="external_project",
                promotion_enabled=True,
            )

        read_only_proj = ForumProfile(
            profile_id="ro-proj",
            name="Project Mirror",
            guild_id=GUILD_ID,
            forum_id=FORUM_ID,
            purpose="external_project",
            catalog_enabled=True,
            promotion_enabled=False,
            external_drafts_enabled=True,
            stage_enabled=False,
        )
        self.assertFalse(read_only_proj.can_promote())
        self.assertFalse(read_only_proj.can_stage("promotion"))
        self.assertFalse(read_only_proj.can_stage("external"))

        with tempfile.TemporaryDirectory() as td:
            p_path = Path(td) / "profiles.json"
            upsert_forum_profile(read_only_proj, p_path)
            loaded = load_forum_profiles(p_path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(get_promotable_forum_profiles(p_path), [])

            sc_path = Path(td) / "server_config.json"
            legacy_servers = [{"name": "LegacySrv", "url": "https://discord.com/channels/1/2"}]
            cfg = {"servers": list(legacy_servers)}
            validated = server_config.set_forum_profile(read_only_proj.to_dict(), cfg, save=False)
            self.assertEqual(validated["profile_id"], "ro-proj")
            self.assertEqual(len(server_config.get_forum_profiles(cfg)), 1)
            # Legacy server list stays untouched
            self.assertEqual(cfg["servers"], legacy_servers)
            self.assertFalse(sc_path.exists())


class TestOCRAndLegacyTargets(unittest.TestCase):
    def test_structured_ocr_multiline_dark_light_and_crop_cache(self):
        utils.clear_ocr_cache()
        font = ImageFont.load_default()

        # Synthetic light-theme multiline forum message with Unicode/link-like text
        light = Image.new("RGB", (520, 120), color=(255, 255, 255))
        draw_l = ImageDraw.Draw(light)
        draw_l.text((16, 16), "North Depot Report", fill=(10, 10, 10), font=font)
        draw_l.text((16, 46), "Sector: Alpha-12", fill=(10, 10, 10), font=font)

        res1 = utils.ocr_image_structured(light, task="message_body", use_cache=True)
        self.assertFalse(res1.cached)
        self.assertGreaterEqual(len(res1.lines), 2)
        self.assertIn("North", res1.text)
        self.assertIn("Sector", res1.text)
        self.assertIn("\n", res1.text)

        # Identical image crop hits cache
        res2 = utils.ocr_image_structured(light, task="message_body", use_cache=True)
        self.assertTrue(res2.cached)
        self.assertEqual(res2.text, res1.text)

        # Dark theme one-word forum title
        dark = Image.new("RGB", (260, 70), color=(49, 51, 56))
        draw_d = ImageDraw.Draw(dark)
        draw_d.text((24, 24), "Explore", fill=(242, 243, 245), font=font)
        title_res = utils.ocr_image_structured(dark, task="forum_title", use_cache=False)
        self.assertIn("Explore", title_res.text)

    def test_safe_grab_pil_fallback_and_queue_updates_target_filtering(self):
        fake_img = Image.new("RGB", (10, 10), color=(1, 2, 3))
        with patch.object(utils.ImageGrab, "grab", side_effect=OSError("no macOS screen recording")):
            with patch.object(utils.pyautogui, "screenshot", return_value=fake_img) as mock_shot:
                got = utils._safe_grab((1, 2, 10, 10))
                self.assertEqual(got, fake_img)
                mock_shot.assert_called_once()

        cov = build_server_scan_coverage(
            [{"name": "Alpha", "channels": []}],
            stop_reason="budget_reached",
            pages_scanned=1,
        )
        self.assertFalse(cov["forum_complete"])
        self.assertFalse(cov["message_histories_complete"])
        self.assertEqual(cov["servers_collected"], 1)
        self.assertEqual(cov["stop_reason"], "budget_reached")

        filtered_servers = [{"name": "FilteredSrv", "promo_channels": ["#allowed-promo"]}]
        allowed, has_unconfigured = promotion_helper._collect_allowed_channels(filtered_servers)
        self.assertEqual(allowed, {"allowed-promo"})
        self.assertFalse(has_unconfigured)

        # Empty filtered list returns immediately without scanning channels
        with patch.object(promotion_helper, "find_channel_position") as mock_find:
            promotion_helper.queue_updates("Live Title", "https://example.invalid/live", servers=[])
            mock_find.assert_not_called()

        # Non-matching channel is skipped while matching channel calls post_update
        found_seq = iter([((100, 200), "other-promo", 0.9), ((120, 220), "allowed-promo", 0.9)])
        with patch.object(promotion_helper, "find_channel_position", side_effect=lambda *_: next(found_seq)):
            with patch.object(promotion_helper, "can_post", return_value=True):
                with patch.object(promotion_helper, "update_timestamp"):
                    with patch.object(promotion_helper, "post_update") as mock_post:
                        with patch.object(
                            promotion_helper.time,
                            "sleep",
                            side_effect=[None, StopIteration("stop_loop")],
                        ):
                            with self.assertRaises(StopIteration):
                                promotion_helper.queue_updates(
                                    "Live Title",
                                    "https://example.invalid/live",
                                    interval_minutes=1,
                                    run_duration_hours=1,
                                    servers=filtered_servers,
                                )
        mock_post.assert_called_once_with("Live Title", "https://example.invalid/live", (120, 220))


if __name__ == "__main__":
    unittest.main()
