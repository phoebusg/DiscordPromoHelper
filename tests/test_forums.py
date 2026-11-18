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


def _seed_mirror_for_draft(store: ForumStore, draft: dict[str, Any]) -> None:
    scope = draft["scope"]
    baseline = draft.get("baseline") or {}
    dest = draft["destination"]
    gid = scope["guild_id"]
    fid = scope["forum_id"]
    store.ensure_scope(gid, fid, actor=scope.get("actor"), snapshot_id=baseline.get("snapshot_id"))
    if dest.get("thread_id"):
        store.upsert_topic(
            thread_id=dest["thread_id"],
            forum_id=fid,
            title="North Ridge Depot",
            source_url=dest.get("source_url"),
        )
    for req_ref in baseline.get("requirements", []):
        rid = req_ref["requirement_id"]
        target_rev = max(1, int(req_ref["revision"]))
        for r_i in range(1, target_rev + 1):
            store.upsert_requirement(
                requirement_id=rid,
                forum_id=fid,
                body=f"Rule text rev {r_i}",
                source_entity_ref=f"forum:{fid}",
                source_url=build_discord_channel_url(gid, fid),
            )
    target_snap_rev = int(baseline.get("snapshot_revision") or 1)
    if target_snap_rev > 1:
        with store._conn:
            store._conn.execute(
                "UPDATE mirror_meta SET snapshot_revision = ? WHERE guild_id = ? AND forum_id = ?",
                (target_snap_rev, gid, fid),
            )


class TestComposerStagingAndEnablement(unittest.TestCase):
    def test_preserves_existing_human_draft_and_pauses_on_account_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            profile = _make_profile(td_path)
            provider = FakeForumProvider()
            store = ForumStore(profile.db_path)
            try:
                draft = read_record(FIXTURE_DIR / "draft_with_attachment.v1.json")
                _seed_mirror_for_draft(store, draft)
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
            provider.requirements = [
                RequirementObservation(
                    requirement_id="guidelines-fixture",
                    forum_id=FORUM_ID,
                    body="Include UTC timestamp and synthetic evidence.",
                    source_entity_ref=f"forum:{FORUM_ID}",
                    source_url=build_discord_channel_url(GUILD_ID, FORUM_ID),
                )
            ]

            store = ForumStore(profile.db_path)
            try:
                session_ctrl = SessionController()
                reader = ForumReader(profile, store, provider, session_controller=session_ctrl)
                reader.initial_backfill()
                snap = store.export_snapshot()

                draft = read_record(FIXTURE_DIR / "draft_with_attachment.v1.json")
                draft["baseline"] = {
                    "snapshot_id": snap["snapshot_id"],
                    "snapshot_revision": snap["revision"],
                    "requirements": [
                        {"requirement_id": r["requirement_id"], "revision": r["revision"]}
                        for r in snap["requirements"]
                    ],
                    "message_content_hash": None,
                }
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

        # Non-matching channel is skipped while matching channel + active server calls post_update
        found_seq = iter([((100, 200), "other-promo", 0.9), ((120, 220), "allowed-promo", 0.9)])
        with patch.object(promotion_helper, "_get_active_server_name", return_value="FilteredSrv"):
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

        # Excluded active server (even with matching channel name) or unconfigured sibling server does not allow send
        self.assertFalse(promotion_helper._is_channel_allowed_for_active_server("allowed-promo", filtered_servers, "OtherServer"))
        self.assertFalse(promotion_helper._is_channel_allowed_for_active_server("allowed-promo", filtered_servers, None))
        mixed_servers = [
            {"name": "SrvA", "promo_channels": ["#a-only"]},
            {"name": "SrvB", "promo_channels": []},
        ]
        self.assertFalse(promotion_helper._is_channel_allowed_for_active_server("general", mixed_servers, "SrvA"))
        self.assertTrue(promotion_helper._is_channel_allowed_for_active_server("a-only", mixed_servers, "SrvA"))
        self.assertTrue(promotion_helper._is_channel_allowed_for_active_server("general", mixed_servers, "SrvB"))


class TestCompletionHandoffPriorities(unittest.TestCase):
    def test_shared_scenario_fixture_and_cutover_export(self):
        from src.forums.config import upsert_forum_profile

        scenario = json.loads((FIXTURE_DIR / "shared_scenario.json").read_text(encoding="utf-8"))
        snap_unres = validate_exchange_record(scenario["snapshot_unresolved"])
        snap_res = validate_exchange_record(scenario["snapshot_resolved"])
        changes_recs = [validate_exchange_record(rec) for rec in scenario["changes"]]
        draft_rec = validate_exchange_record(scenario["draft_request"])
        receipt_recs = [validate_exchange_record(rec) for rec in scenario["receipts"]]

        self.assertEqual(snap_unres["schema"], "forum.snapshot.v1")
        self.assertEqual(snap_res["schema"], "forum.snapshot.v1")
        self.assertEqual(len(changes_recs), 2)
        self.assertEqual(len(receipt_recs), 2)

        # Evidence resolves strictly within FIXTURE_DIR.parent (tests/fixtures)
        resolved_ev = resolve_evidence_files(draft_rec["evidence"], FIXTURE_DIR.parent)
        self.assertEqual(len(resolved_ev), 1)
        self.assertTrue(Path(resolved_ev[0]["resolved_path"]).is_file())
        self.assertEqual(Path(resolved_ev[0]["resolved_path"]).name, "map-note.txt")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            profile = _make_profile(tmp_dir)
            upsert_forum_profile(profile, tmp_dir / "profiles.json")
            store = ForumStore(profile.db_path)
            try:
                store.ensure_scope(
                    GUILD_ID,
                    FORUM_ID,
                    actor={"user_id": ACTOR_USER_ID, "label": "Fixture account"},
                )
                store.upsert_forum(
                    forum_id=FORUM_ID,
                    name="Fictional POI testing",
                    source_url=build_discord_channel_url(GUILD_ID, FORUM_ID),
                )
                store.upsert_requirement(
                    forum_id=FORUM_ID,
                    requirement_id="poi-guidance",
                    body="Check geometry and prop behavior.",
                )
                # Two distinct same-title unresolved observations
                store.record_unresolved_topic(
                    forum_id=FORUM_ID,
                    observation_ref="poi-observation-a",
                    title="Amberfall Crossing",
                )
                store.record_unresolved_topic(
                    forum_id=FORUM_ID,
                    observation_ref="poi-observation-b",
                    title="Amberfall Crossing",
                )
                store.update_coverage(topics="partial", histories="partial", requirements="partial")

                # Bootstrap export writes snapshot without redundant 0..cursor delta file
                boot = store.export_exchange_dir(profile.exchange_dir)
                self.assertEqual(boot["mode"], "bootstrap_snapshot")
                self.assertIsNotNone(boot["snapshot"])
                self.assertIsNone(boot["changes"])
                boot_snap = read_record(boot["snapshot"])
                pending_titles = {p["observation_ref"]: p["observed_title"] for p in boot_snap["coverage"]["pending"]}
                self.assertEqual(pending_titles["poi-observation-a"], "Amberfall Crossing")
                self.assertEqual(pending_titles["poi-observation-b"], "Amberfall Crossing")

                # Quiet re-export emits neither snapshot nor empty delta batch
                noop = store.export_exchange_dir(profile.exchange_dir)
                self.assertEqual(noop["mode"], "quiet_noop")
                self.assertIsNone(noop["snapshot"])
                self.assertIsNone(noop["changes"])
                self.assertEqual(noop["change_count"], 0)

                # Resolve both observations cleanly while preserving observation_ref
                store.resolve_topic_observation(
                    observation_ref="poi-observation-a",
                    thread_id=THREAD_A,
                    title="Amberfall Crossing",
                    source_url=build_discord_channel_url(GUILD_ID, THREAD_A),
                )
                store.resolve_topic_observation(
                    observation_ref="poi-observation-b",
                    thread_id=THREAD_B,
                    title="Amberfall Crossing",
                    source_url=build_discord_channel_url(GUILD_ID, THREAD_B),
                )
                store.update_coverage(topics="complete")
                store.upsert_message(
                    thread_id=THREAD_A,
                    entity_ref="msg:100000000000000501",
                    message_id="100000000000000501",
                    author={"user_id": ACTOR_USER_ID, "label": "Fixture account"},
                    body="Verified bridge railing.",
                )

                inc = store.export_exchange_dir(profile.exchange_dir)
                self.assertEqual(inc["mode"], "incremental_changes")
                self.assertIsNone(inc["snapshot"])
                self.assertIsNotNone(inc["changes"])
                delta = read_record(inc["changes"])
                self.assertEqual(delta["after_cursor"]["position"], boot_snap["cursor"]["position"])
                self.assertEqual(delta["snapshot_revision"], boot_snap["revision"])

                # Snapshot cutover archives older delta batches before writing fresh snapshot
                cut = store.export_exchange_dir(profile.exchange_dir, force_snapshot=True)
                self.assertEqual(cut["mode"], "cutover_snapshot")
                self.assertIsNotNone(cut["snapshot"])
                self.assertEqual(list((Path(profile.exchange_dir) / "changes").glob("*.json")), [])
                self.assertEqual(len(list((Path(profile.exchange_dir) / "archive" / "changes").glob("*.json"))), 1)
                cut_snap = read_record(cut["snapshot"])
                obs_refs = {t["thread_id"]: t.get("observation_ref") for t in cut_snap["topics"]}
                self.assertEqual(obs_refs[THREAD_A], "poi-observation-a")
                self.assertEqual(obs_refs[THREAD_B], "poi-observation-b")
            finally:
                store.close()

    def test_desktop_provider_list_topics_and_read_topic_pagination(self):
        from src.forums.providers import desktop as desktop_mod
        from src.forums.providers.desktop import DesktopDiscordProvider

        provider = DesktopDiscordProvider()
        session = SessionDescription(
            actor={"user_id": ACTOR_USER_ID, "label": "Tester"},
            app_name="Discord",
            window_title="#forum - Discord",
            window_bbox=(0, 0, 1280, 800),
            current_url=build_discord_channel_url(GUILD_ID, FORUM_ID),
            capabilities={"can_read_semantic": True, "can_read_ocr": True, "can_stage_composer": True},
        )

        # Build 65 virtualized directory topic items sliced in sliding windows
        all_cards = [
            TopicObservation(
                forum_id=FORUM_ID,
                thread_id=str(200000000000000000 + i),
                title=f"Post Title {i}",
                source_url=build_discord_channel_url(GUILD_ID, str(200000000000000000 + i)),
                source_method="accessibility",
            )
            for i in range(65)
        ]
        scroll_pos = {"idx": 0}
        list_sentinel = object()

        def fake_extract(_nodes, _gid, _fid):
            start = scroll_pos["idx"]
            return all_cards[start : min(65, start + 12)], [], list_sentinel

        def fake_scroll(_x, _y, delta_lines):
            if delta_lines > 0:
                scroll_pos["idx"] = max(0, scroll_pos["idx"] - 10)
            else:
                scroll_pos["idx"] = min(55, scroll_pos["idx"] + 9)
            return True

        fake_win_nodes = [{"role": "AXButton", "description": "Post 0", "title": "", "value": "", "url": "", "dom_id": "", "depth": 1, "element": object()}]
        with patch.object(provider, "describe_session", return_value=session), \
             patch.object(provider, "_get_ax_window", return_value=object()), \
             patch.object(provider, "navigate_to_url", return_value=True), \
             patch.object(provider, "_extract_post_cards_and_links", side_effect=fake_extract), \
             patch.object(desktop_mod, "_walk_ax_tree", return_value=fake_win_nodes), \
             patch.object(desktop_mod, "_ax_bounds", return_value=(100.0, 150.0, 360.0, 600.0)), \
             patch.object(desktop_mod, "_scroll_at_point", side_effect=fake_scroll), \
             patch.object(desktop_mod.time, "sleep"):
            page1 = provider.list_topics(GUILD_ID, FORUM_ID, limit=35)
            self.assertEqual(len(page1.topics), 35)
            self.assertFalse(page1.complete)
            self.assertTrue((page1.next_cursor or "").startswith("dirscroll:"))

            # Reset to top and seek via dirscroll cursor like a resumed backfill run
            scroll_pos["idx"] = 0
            page2 = provider.list_topics(GUILD_ID, FORUM_ID, cursor=page1.next_cursor, limit=50)
            self.assertEqual(len(page2.topics), 30)
            self.assertTrue(page2.complete)
            self.assertIsNone(page2.next_cursor)
            combined_ids = [t.thread_id for t in page1.topics + page2.topics]
            self.assertEqual(combined_ids, [t.thread_id for t in all_cards])

        # Test read_topic upward pagination until starter message id == thread_id, then return to bottom
        tid = "300000000000000001"
        ml_elem = object()
        msg_win_nodes = [
            {
                "role": "AXList",
                "description": "Messages in Canyon Overlook",
                "title": "",
                "value": "",
                "url": "",
                "dom_id": f"chat-messages-{tid}-300000000000000005",
                "depth": 4,
                "element": ml_elem,
            }
        ]
        msg_view = {"step": 0}
        slices = [
            # Initial bottom slice
            (
                "Canyon Overlook",
                [{"name": "verified"}],
                {
                    "300000000000000004": MessageObservation(thread_id=tid, message_id="300000000000000004", author={"user_id": None, "label": "Tester"}, body="Fourth"),
                    "300000000000000005": MessageObservation(thread_id=tid, message_id="300000000000000005", author={"user_id": None, "label": "Tester"}, body="Latest", attachments=[{"name": "shot.png", "url": "https://cdn.example/shot.png"}]),
                },
            ),
            # 1st scroll up
            (
                "Canyon Overlook",
                [],
                {
                    "300000000000000002": MessageObservation(thread_id=tid, message_id="300000000000000002", author={"user_id": None, "label": "Tester"}, body="Second"),
                    "300000000000000003": MessageObservation(thread_id=tid, message_id="300000000000000003", author={"user_id": None, "label": "Tester"}, body="Third"),
                },
            ),
            # 2nd scroll up reaches starter tid
            (
                "Canyon Overlook",
                [],
                {
                    tid: MessageObservation(thread_id=tid, message_id=tid, author={"user_id": None, "label": "Tester"}, body="Starter post"),
                    "300000000000000002": MessageObservation(thread_id=tid, message_id="300000000000000002", author={"user_id": None, "label": "Tester"}, body="Second"),
                },
            ),
            # Final return-to-bottom check
            (
                "Canyon Overlook",
                [],
                {
                    "300000000000000005": MessageObservation(thread_id=tid, message_id="300000000000000005", author={"user_id": None, "label": "Tester"}, body="Latest", attachments=[{"name": "shot.png", "url": "https://cdn.example/shot.png"}]),
                },
            ),
        ]

        def fake_parse_msgs(_ml, _gid, _tid):
            idx = min(msg_view["step"], len(slices) - 1)
            msg_view["step"] += 1
            return slices[idx]

        with patch.object(provider, "describe_session", return_value=session), \
             patch.object(provider, "_get_ax_window", return_value=object()), \
             patch.object(desktop_mod, "_walk_ax_tree", return_value=msg_win_nodes), \
             patch.object(desktop_mod, "_ax_attr", return_value=[]), \
             patch.object(desktop_mod, "_ax_bounds", return_value=(400.0, 120.0, 700.0, 600.0)), \
             patch.object(desktop_mod, "_scroll_at_point", return_value=True), \
             patch.object(desktop_mod.time, "sleep"), \
             patch.object(provider, "_parse_messages_from_ax_list", side_effect=fake_parse_msgs):
            hist = provider.read_topic(GUILD_ID, FORUM_ID, tid)
            self.assertTrue(hist.complete)
            self.assertIsNone(hist.next_cursor)
            self.assertEqual([m.message_id for m in hist.messages], [tid, "300000000000000002", "300000000000000003", "300000000000000004", "300000000000000005"])
            self.assertEqual(hist.messages[-1].attachments[0]["url"], "https://cdn.example/shot.png")

    def test_cross_process_session_controller_and_cli_sync_workflow(self):
        import io
        from contextlib import redirect_stdout
        from src.forums.__main__ import main as cli_main
        from src.forums.config import upsert_forum_profile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_path = tmp_dir / "mirror.session.json"
            ctrl_reader = SessionController(state_path=state_path)
            ctrl_stager = SessionController(state_path=state_path)

            # Separate instances coordinate read vs stage exclusivity
            self.assertTrue(ctrl_reader.acquire_for_read())
            self.assertFalse(ctrl_stager.acquire_for_staging(wait_seconds=0.0))
            ctrl_reader.release_read()
            self.assertTrue(ctrl_stager.acquire_for_staging(wait_seconds=0.0))
            self.assertFalse(ctrl_reader.acquire_for_read())
            ctrl_stager.release_staging()

            # Simulate dead-PID stale transient lock on disk -> recovered automatically
            state_path.write_text(
                json.dumps({"mode": "reading", "owner_pid": 9999999, "updated_at": "2026-11-14T12:00:00Z", "review_draft": None}),
                encoding="utf-8",
            )
            self.assertTrue(ctrl_reader.acquire_for_read())
            ctrl_reader.release_read()

            # Durable human review lock survives across independent instances and pauses reader
            ctrl_stager.enter_discord_review("draft-x", 1, reason="staged_for_review")
            self.assertTrue(ctrl_reader.is_review_active())
            self.assertFalse(ctrl_reader.can_navigate())

            profile = _make_profile(tmp_dir)
            store = ForumStore(profile.db_path)
            try:
                fake_provider = FakeForumProvider()
                paused = ForumReader(profile, store, fake_provider, session_controller=ctrl_reader).incremental_refresh()
                self.assertTrue(paused["paused"])
                self.assertEqual(paused["reason"], "session_in_review_or_staging")
            finally:
                store.close()
            ctrl_reader.release_review()

            # Partial staging failure activates review lock and writes receipt to exchange/receipts
            fake_stage_provider = FakeForumProvider()
            partial_obs = ComposerStageObservation(
                status="needs_review",
                observed_actor={"user_id": ACTOR_USER_ID, "label": "Tester"},
                destination={"guild_id": GUILD_ID, "forum_id": FORUM_ID, "thread_id": THREAD_1, "message_id": None},
                observed_composer_text="Line 1\nLine 2",
                staged_evidence_ids=[],
                reason="attachment_preview_unverified:map-note.txt",
            )
            store = ForumStore(profile.db_path)
            try:
                mgr = HandoffManager(store, fake_stage_provider, session_controller=ctrl_stager, exchange_dir=profile.exchange_dir)
                req = {
                    "schema": "draft.request.v1",
                    "record_id": "req-partial-1",
                    "producer_id": "test",
                    "created_at": "2026-11-14T12:00:00Z",
                    "scope": {
                        "guild_id": GUILD_ID,
                        "forum_id": FORUM_ID,
                        "actor": {"user_id": ACTOR_USER_ID, "label": "Tester"},
                    },
                    "draft_id": "draft-partial-1",
                    "revision": 1,
                    "action": "reply_existing",
                    "destination": {
                        "guild_id": GUILD_ID,
                        "forum_id": FORUM_ID,
                        "thread_id": THREAD_1,
                        "message_id": None,
                        "source_url": build_discord_channel_url(GUILD_ID, THREAD_1),
                    },
                    "body": "Line 1\nLine 2",
                    "title": None,
                    "tags": [],
                    "evidence": [],
                    "baseline": {
                        "snapshot_id": None,
                        "snapshot_revision": None,
                        "requirements": [],
                        "message_content_hash": None,
                    },
                }
                _seed_mirror_for_draft(store, req)
                snap_req = store.export_snapshot(guild_id=GUILD_ID, forum_id=FORUM_ID)
                req["baseline"]["snapshot_id"] = snap_req["snapshot_id"]
                req["baseline"]["snapshot_revision"] = snap_req["revision"]
                with patch.object(fake_stage_provider, "stage_draft", return_value=partial_obs):
                    rec = mgr.stage_draft_request(req, profile)
                self.assertEqual(rec["status"], "needs_review")
                self.assertTrue(ctrl_reader.is_review_active())
                receipt_files = list((Path(profile.exchange_dir) / "receipts").glob("*.json"))
                self.assertEqual(len(receipt_files), 1)
                self.assertEqual(read_record(receipt_files[0])["receipt_id"], rec["receipt_id"])
            finally:
                store.close()
                ctrl_reader.release_review()

            # CLI workflow: sync -> session-state -> paused sync -> release-review -> reconcile
            profiles_path = tmp_dir / "profiles.json"
            seed_path = tmp_dir / "seed.json"
            seed_path.write_text(
                json.dumps({"groups": [{"label": "POI", "topics": [["Amberfall Crossing", THREAD_1]]}]}),
                encoding="utf-8",
            )
            cli_prof = ForumProfile(
                profile_id="cli-sync-test",
                name="CLI Sync Forum",
                guild_id=GUILD_ID,
                forum_id=FORUM_ID,
                source_url=build_discord_channel_url(GUILD_ID, FORUM_ID),
                purpose="external_project",
                catalog_enabled=True,
                promotion_enabled=False,
                external_drafts_enabled=True,
                stage_enabled=True,
                seed_index_path=str(seed_path),
                transfer_root=str(FIXTURE_DIR.parent),
                db_path=str(tmp_dir / "cli_mirror.sqlite3"),
                exchange_dir=str(tmp_dir / "cli_exchange"),
            )
            upsert_forum_profile(cli_prof, profiles_path)
            scenario = json.loads((FIXTURE_DIR / "shared_scenario.json").read_text(encoding="utf-8"))
            with redirect_stdout(io.StringIO()):
                cli_main(
                    [
                        "--profiles-path",
                        str(profiles_path),
                        "refresh",
                        "--profile",
                        "cli-sync-test",
                        "--provider",
                        "seed",
                        "--export",
                    ]
                )
            cli_store_init = ForumStore(cli_prof.db_path)
            try:
                cli_snap = cli_store_init.export_snapshot(guild_id=GUILD_ID, forum_id=FORUM_ID)
            finally:
                cli_store_init.close()
            cli_draft = copy.deepcopy(scenario["draft_request"])
            cli_draft["baseline"] = {
                "snapshot_id": cli_snap["snapshot_id"],
                "snapshot_revision": cli_snap["revision"],
                "requirements": [],
                "message_content_hash": None,
            }
            draft_path = Path(cli_prof.exchange_dir) / "drafts" / "draft_1.json"
            write_record_atomic(draft_path, cli_draft)

            mock_desk = FakeForumProvider()
            mock_desk.session_desc = SessionDescription(
                actor={"user_id": ACTOR_USER_ID, "label": "Fixture account"},
                app_name="Discord",
                window_title="Discord",
                window_bbox=(0, 0, 1280, 800),
                capabilities={"can_read_semantic": False, "can_read_ocr": False, "can_stage_composer": True},
            )
            with patch("src.forums.__main__.DesktopDiscordProvider", return_value=mock_desk):
                buf1 = io.StringIO()
                with redirect_stdout(buf1):
                    cli_main(["--profiles-path", str(profiles_path), "sync", "--profile", "cli-sync-test", "--provider", "seed", "--stage-pending"])
                out1 = json.loads(buf1.getvalue())
                self.assertEqual(len(out1["staged"]), 1)
                self.assertEqual(out1["staged"][0]["status"], "staged")
                self.assertEqual(out1["session_state"]["mode"], "in_discord_review")

                # Separate session-state query confirms persistent review lock
                buf_ss = io.StringIO()
                with redirect_stdout(buf_ss):
                    cli_main(["--profiles-path", str(profiles_path), "session-state", "--profile", "cli-sync-test"])
                self.assertEqual(json.loads(buf_ss.getvalue())["session_state"]["mode"], "in_discord_review")

                # Subsequent sync automatically pauses refresh while in_discord_review
                buf2 = io.StringIO()
                with redirect_stdout(buf2):
                    cli_main(["--profiles-path", str(profiles_path), "sync", "--profile", "cli-sync-test", "--provider", "seed"])
                out2 = json.loads(buf2.getvalue())
                self.assertTrue(out2["refresh"]["paused"])

                # Real separate OS child process also sees persistent in_discord_review and pauses cleanly
                import subprocess as _sp
                import sys as _sys

                sub_proc = _sp.run(
                    [
                        _sys.executable,
                        "-m",
                        "src.forums",
                        "--profiles-path",
                        str(profiles_path),
                        "refresh",
                        "--profile",
                        "cli-sync-test",
                        "--provider",
                        "seed",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                sub_out = json.loads(sub_proc.stdout)
                self.assertTrue(sub_out["result"]["paused"])
                self.assertEqual(sub_out["result"]["reason"], "session_in_review_or_staging")

                # Reconcile observes human-edited submission and releases review lock
                mock_desk.next_submission = SubmissionObservation(
                    status="submission_observed",
                    observed_actor=dict(mock_desk.actor),
                    message_id="100000000000000900",
                    message_url=build_discord_channel_url(GUILD_ID, THREAD_1),
                    observed_body=scenario["expected"]["human_edited_body"],
                    reason="matched_posted_message",
                )
                buf3 = io.StringIO()
                with redirect_stdout(buf3):
                    cli_main(["--profiles-path", str(profiles_path), "sync", "--profile", "cli-sync-test", "--provider", "seed", "--reconcile"])
                out3 = json.loads(buf3.getvalue())
                self.assertEqual(out3["reconciled"][0]["status"], "submission_observed")
                self.assertEqual(out3["session_state"]["mode"], "idle")

                # Explicit release-review subcommand succeeds cleanly
                buf4 = io.StringIO()
                with redirect_stdout(buf4):
                    cli_main(["--profiles-path", str(profiles_path), "release-review", "--profile", "cli-sync-test"])
                out4 = json.loads(buf4.getvalue())
                self.assertTrue(out4["released"])
                self.assertEqual(out4["session_state"]["mode"], "idle")

    def test_desktop_stage_draft_and_observe_submission(self):
        from src.forums.providers import desktop as desktop_mod
        from src.forums.providers.desktop import DesktopDiscordProvider

        provider = DesktopDiscordProvider()
        # Verify Slate empty sentinel and newline exact-match helper
        self.assertEqual(provider._normalize_composer_text("\ufeff\n"), "")
        self.assertTrue(provider._composer_text_matches("Line 1\nLine 2", "Line 1\nLine 2\n"))
        self.assertFalse(provider._composer_text_matches("Line 1\nLine 2", "Line 1 Line 2"))

        wrong_url_session = SessionDescription(
            actor={"user_id": ACTOR_USER_ID, "label": "Tester"},
            app_name="Discord",
            window_title="Discord",
            window_bbox=(0, 0, 1280, 800),
            current_url=build_discord_channel_url(GUILD_ID, THREAD_2),
            capabilities={"can_read_semantic": True, "can_read_ocr": True, "can_stage_composer": True},
        )
        draft_req = {
            "draft_id": "d-1",
            "revision": 1,
            "action": "reply_existing",
            "destination": {"guild_id": GUILD_ID, "forum_id": FORUM_ID, "thread_id": THREAD_1},
            "body": "Line 1\nLine 2 ✓",
        }
        with patch.object(provider, "describe_session", return_value=wrong_url_session), \
             patch.object(provider, "navigate_to_url", return_value=True), \
             patch.object(provider, "_get_ax_window", return_value=object()), \
             patch.object(provider, "_inspect_composer", return_value=(object(), "")), \
             patch.object(desktop_mod.time, "sleep"):
            mismatch = provider.stage_draft(draft_req, [])
            self.assertEqual(mismatch.status, "needs_review")
            self.assertIn("destination_url_mismatch", mismatch.reason or "")

        # Successful focus, exact text verification, attachment verification, and clipboard restore
        good_session = SessionDescription(
            actor={"user_id": ACTOR_USER_ID, "label": "Tester"},
            app_name="Discord",
            window_title="Discord",
            window_bbox=(0, 0, 1280, 800),
            current_url=build_discord_channel_url(GUILD_ID, THREAD_1),
            capabilities={"can_read_semantic": True, "can_read_ocr": True, "can_stage_composer": True},
        )
        clip_state = {"val": "user-prior-clipboard", "attached": False}

        class FakeClip:
            @staticmethod
            def paste():
                return clip_state["val"]

            @staticmethod
            def copy(txt):
                clip_state["val"] = txt

        class FakePyAutoGui:
            @staticmethod
            def click(*_a, **_k):
                pass

            @staticmethod
            def hotkey(*_a, **_k):
                pass

        comp_elem = object()
        att_seq = [
            [],  # baseline before staging: clean
            [{"role": "AXGroup", "text": "map-note.txt", "dom_id": "upload-1", "element": object(), "is_remove_button": False}],
        ]

        def fake_inspect_comp(_ax):
            return comp_elem, ("Line 1\nLine 2 ✓\n" if clip_state["attached"] else "")

        def fake_stage_single(_ax, _comp, _path, _name):
            return True

        with tempfile.TemporaryDirectory() as tmp:
            att_file = Path(tmp) / "map-note.txt"
            att_file.write_text("evidence\n", encoding="utf-8")
            resolved_ev = [{"evidence_id": "ev-1", "name": "map-note.txt", "resolved_path": str(att_file)}]

            def fake_hotkey(*args):
                if args[-1] == "v":
                    clip_state["attached"] = True

            FakePyAutoGui.hotkey = staticmethod(fake_hotkey)  # type: ignore[assignment]
            with patch.object(provider, "describe_session", return_value=good_session), \
                 patch.object(provider, "navigate_to_url", return_value=True), \
                 patch.object(provider, "_get_ax_window", return_value=object()), \
                 patch.object(provider, "_focus_element", return_value=True) as mock_focus, \
                 patch.object(provider, "_inspect_composer", side_effect=lambda _ax: (comp_elem, "Line 1\nLine 2 ✓\n" if clip_state["attached"] else "")), \
                 patch.object(provider, "_inspect_staged_attachments", side_effect=att_seq), \
                 patch.object(provider, "_stage_single_attachment", return_value=True) as mock_att, \
                 patch.object(desktop_mod, "pyperclip", FakeClip), \
                 patch.object(desktop_mod, "pyautogui", FakePyAutoGui), \
                 patch.object(desktop_mod.time, "sleep"):
                res = provider.stage_draft(draft_req, resolved_ev)
                self.assertEqual(res.status, "staged")
                self.assertEqual(res.staged_evidence_ids, ["ev-1"])
                self.assertEqual(clip_state["val"], "user-prior-clipboard")
                mock_focus.assert_called_once()
                mock_att.assert_called_once()

            # Split-pane forum URL verifies against chat-messages-<THREAD_1>-* DOM nodes; fails if absent
            split_pane_session = SessionDescription(
                actor={"user_id": ACTOR_USER_ID, "label": "Tester"},
                app_name="Discord",
                window_title="Discord",
                window_bbox=(0, 0, 1280, 800),
                current_url=build_discord_channel_url(GUILD_ID, FORUM_ID),
                capabilities={"can_read_semantic": True, "can_read_ocr": True, "can_stage_composer": True},
            )
            with patch.object(provider, "describe_session", return_value=split_pane_session), \
                 patch.object(provider, "navigate_to_url", return_value=True), \
                 patch.object(provider, "_get_ax_window", return_value=object()), \
                 patch.object(provider, "_inspect_composer", return_value=(comp_elem, "")), \
                 patch.object(desktop_mod, "_walk_ax_tree", return_value=[]), \
                 patch.object(desktop_mod.time, "sleep"):
                no_dom_mismatch = provider.stage_draft(draft_req, [])
                self.assertEqual(no_dom_mismatch.status, "needs_review")
                self.assertIn("destination_url_mismatch", no_dom_mismatch.reason or "")

            split_state = {"pasted": False}

            def fake_split_hotkey(*args):
                if args[-1] == "v":
                    split_state["pasted"] = True

            FakePyAutoGui.hotkey = staticmethod(fake_split_hotkey)  # type: ignore[assignment]
            split_dom_nodes = [{"dom_id": f"chat-messages-{THREAD_1}-{THREAD_1}", "role": "AXGroup"}]
            with patch.object(provider, "describe_session", return_value=split_pane_session), \
                 patch.object(provider, "navigate_to_url", return_value=True), \
                 patch.object(provider, "_get_ax_window", return_value=object()), \
                 patch.object(provider, "_focus_element", return_value=True), \
                 patch.object(desktop_mod, "_walk_ax_tree", return_value=split_dom_nodes), \
                 patch.object(provider, "_inspect_composer", side_effect=lambda _ax: (comp_elem, "Line 1\nLine 2 ✓\n" if split_state["pasted"] else "")), \
                 patch.object(provider, "_inspect_staged_attachments", return_value=[]), \
                 patch.object(desktop_mod, "pyperclip", FakeClip), \
                 patch.object(desktop_mod, "pyautogui", FakePyAutoGui), \
                 patch.object(desktop_mod.time, "sleep"):
                split_ok = provider.stage_draft(draft_req, [])
                self.assertEqual(split_ok.status, "staged")
                self.assertEqual(split_ok.staged_evidence_ids, [])

        # observe_submission handles reply correlation, edit correlation, create_topic, and in_discord_review
        reply_hist = TopicHistoryObservation(
            topic=TopicObservation(forum_id=FORUM_ID, thread_id=THREAD_1, title="Target Thread"),
            messages=[
                MessageObservation(thread_id=THREAD_1, message_id="100", author={"user_id": None, "label": "Tester"}, body="Older msg"),
                MessageObservation(
                    thread_id=THREAD_1,
                    message_id="105",
                    author={"user_id": None, "label": "Tester"},
                    body="Line 1\nLine 2 ✓ with human polish",
                    attachments=[{"name": "map-note.txt", "url": "https://cdn.example/map-note.txt"}],
                ),
            ],
            complete=True,
        )
        with patch.object(provider, "describe_session", return_value=good_session), \
             patch.object(provider, "read_topic", return_value=reply_hist):
            sub_reply = provider.observe_submission(
                {**draft_req, "evidence": [{"name": "map-note.txt"}]},
                {"actor": {"user_id": ACTOR_USER_ID, "label": "Tester"}, "baseline_latest_message_id": "100"},
            )
            self.assertEqual(sub_reply.status, "submission_observed")
            self.assertEqual(sub_reply.message_id, "105")

        # Unsubmitted draft still in composer returns in_discord_review
        empty_hist = TopicHistoryObservation(topic=TopicObservation(forum_id=FORUM_ID, thread_id=THREAD_1, title="Target Thread"), messages=[], complete=True)
        with patch.object(provider, "describe_session", return_value=good_session), \
             patch.object(provider, "read_topic", return_value=empty_hist), \
             patch.object(provider, "_get_ax_window", return_value=object()), \
             patch.object(provider, "_inspect_composer", return_value=(comp_elem, "Line 1\nLine 2 ✓")), \
             patch.object(provider, "_inspect_staged_attachments", return_value=[]):
            sub_rev = provider.observe_submission(draft_req, {})
            self.assertEqual(sub_rev.status, "in_discord_review")

        # create_topic without thread_id resolves by title
        tlist = TopicListObservation(
            topics=[TopicObservation(forum_id=FORUM_ID, thread_id=THREAD_2, title="New Field Report")],
            complete=True,
        )
        created_hist = TopicHistoryObservation(
            topic=TopicObservation(forum_id=FORUM_ID, thread_id=THREAD_2, title="New Field Report"),
            messages=[MessageObservation(thread_id=THREAD_2, message_id=THREAD_2, author={"user_id": None, "label": "Tester"}, body="New report body details")],
            complete=True,
        )
        with patch.object(provider, "describe_session", return_value=good_session), \
             patch.object(provider, "list_topics", return_value=tlist), \
             patch.object(provider, "read_topic", return_value=created_hist):
            sub_create = provider.observe_submission(
                {"action": "create_topic", "title": "New Field Report", "destination": {"guild_id": GUILD_ID, "forum_id": FORUM_ID, "thread_id": None}, "body": "New report body details"},
                {},
            )
            self.assertEqual(sub_create.status, "submission_observed")
            self.assertEqual(sub_create.message_id, THREAD_2)

        # Bounds-keyed Remove Attachment slots remain distinct even with empty DOM ids,
        # and semantic-only session without OCR still observes composer/submission cleanly.
        b1, b2 = object(), object()
        fake_cards = [
            {"role": "AXButton", "title": "", "description": "Remove Attachment", "value": "", "dom_id": "", "depth": 5, "element": b1},
            {"role": "AXButton", "title": "", "description": "Remove Attachment", "value": "", "dom_id": "", "depth": 5, "element": b2},
        ]
        with patch.object(desktop_mod, "_ax_attr", return_value=None), \
             patch.object(desktop_mod, "_walk_ax_tree", return_value=fake_cards), \
             patch.object(desktop_mod, "_ax_bounds", side_effect=lambda el: (598.0, 633.0, 32.0, 32.0) if el is b1 else ((840.0, 633.0, 32.0, 32.0) if el is b2 else (450.0, 860.0, 375.0, 56.0))):
            staged = provider._inspect_staged_attachments(object(), object())
            self.assertEqual(provider._count_staged_attachment_slots(staged), 2)

        no_ocr_session = SessionDescription(
            actor={"user_id": None, "label": "Tester"},
            app_name="Discord",
            window_title="Discord",
            window_bbox=(0, 0, 1280, 800),
            capabilities={"can_read_semantic": True, "can_read_ocr": False, "can_stage_composer": True},
            errors=[{"code": "screen_capture_unavailable", "message": "No Screen Recording", "thread_id": None}],
        )
        with patch.object(provider, "describe_session", return_value=no_ocr_session), \
             patch.object(provider, "read_topic", return_value=reply_hist):
            sub_no_ocr = provider.observe_submission(
                {**draft_req, "evidence": [{"name": "map-note.txt"}]},
                {"actor": {"user_id": ACTOR_USER_ID, "label": "Tester"}, "baseline_latest_message_id": "100"},
            )
            self.assertEqual(sub_no_ocr.status, "submission_observed")
            self.assertEqual(sub_no_ocr.message_id, "105")

    def test_never_sends_enter_or_return_and_preflights_screen_capture(self) -> None:
        import ast
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from src.forums.providers import desktop as desktop_mod
        from src.forums.providers.base import SessionDescription
        from src.forums.providers.desktop import DesktopDiscordProvider

        # 1. Static AST audit across all files in src/forums/
        forums_root = Path(__file__).resolve().parents[1] / "src" / "forums"
        forbidden_keys = {"return", "enter", "numpadenter", "kp_enter", "\r", "\n"}
        key_calls = {"press", "hotkey", "keyDown", "keyUp", "typewrite", "write", "_safe_key_press"}

        def _is_forbidden_key_literal(node: ast.AST) -> bool:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                raw = node.value
                low = raw.lower()
                return low in forbidden_keys or low.strip() in forbidden_keys
            return False

        py_files = sorted(forums_root.rglob("*.py"))
        self.assertGreater(len(py_files), 0)
        for py_file in py_files:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn("promotion_helper", alias.name, msg=str(py_file))
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    self.assertNotIn("promotion_helper", mod, msg=str(py_file))
                    for alias in node.names:
                        self.assertNotEqual(alias.name, "post_update", msg=str(py_file))
                elif isinstance(node, ast.Call):
                    fn_name = None
                    if isinstance(node.func, ast.Name):
                        fn_name = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        fn_name = node.func.attr
                    self.assertNotEqual(fn_name, "post_update", msg=str(py_file))
                    if fn_name in key_calls:
                        for arg in list(node.args) + [kw.value for kw in node.keywords]:
                            self.assertFalse(
                                _is_forbidden_key_literal(arg),
                                msg=f"Forbidden send key literal in {py_file}:{getattr(node, 'lineno', '?')}",
                            )

        # 2. Runtime _safe_key_press guard
        provider = DesktopDiscordProvider(expected_actor={"user_id": ACTOR_USER_ID, "label": "Tester"})
        for bad_key in ("return", "Return", "enter", "ENTER", "numpadenter", "kp_enter", "\n", "\r"):
            with self.assertRaises(RuntimeError):
                provider._safe_key_press(bad_key)

        # 3. Spy pyautogui trace during stage_draft (text + attachment) + clear_staged_composer
        with tempfile.TemporaryDirectory() as tmp:
            ev_file = Path(tmp) / "shot.png"
            ev_file.write_bytes(b"PNGDATA")

            composer_state: dict[str, Any] = {"text": "", "attachments": []}
            clipboard_state: dict[str, Any] = {"text": "", "file": None}
            recorded_events: list[tuple[str, list[str]]] = []
            comp_elem = object()
            ax_win = object()

            def _record(kind: str, args: tuple[Any, ...]) -> None:
                recorded_events.append((kind, [str(a) for a in args]))

            def spy_hotkey(*args: Any, **kwargs: Any) -> None:
                _record("hotkey", args)
                lows = [str(a).lower() for a in args]
                if "v" in lows:
                    if clipboard_state["file"]:
                        composer_state["attachments"].append(
                            {
                                "role": "AXButton",
                                "text": "Remove attachment shot.png",
                                "dom_id": "upload-1",
                                "bounds": (10, 20, 24, 24),
                                "element": object(),
                                "is_remove_button": True,
                            }
                        )
                    else:
                        composer_state["text"] = clipboard_state["text"]
                elif "a" in lows:
                    composer_state["selected_all"] = True

            def spy_press(key: Any, *args: Any, **kwargs: Any) -> None:
                _record("press", (key, *args))
                if str(key).lower() == "backspace":
                    composer_state["text"] = ""

            def spy_click(*args: Any, **kwargs: Any) -> None:
                _record("click", args)
                if len(args) >= 2 and int(args[0]) == 22 and int(args[1]) == 32 and composer_state["attachments"]:
                    composer_state["attachments"].pop(0)

            spy_gui = SimpleNamespace(
                hotkey=spy_hotkey,
                press=spy_press,
                keyDown=lambda k, *a, **kw: _record("keyDown", (k, *a)),
                keyUp=lambda k, *a, **kw: _record("keyUp", (k, *a)),
                typewrite=lambda *a, **kw: _record("typewrite", a),
                write=lambda *a, **kw: _record("write", a),
                click=spy_click,
            )

            def spy_subproc_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
                if cmd and cmd[0] == "osascript" and len(cmd) >= 3 and "POSIX file" in cmd[2]:
                    clipboard_state["file"] = "shot.png"
                return SimpleNamespace(returncode=0)

            spy_pyperclip = SimpleNamespace(
                paste=lambda: clipboard_state["text"],
                copy=lambda val: clipboard_state.update({"text": str(val), "file": None}),
            )

            ok_session = SessionDescription(
                actor={"user_id": ACTOR_USER_ID, "label": "Tester"},
                app_name="Discord",
                window_title="Discord",
                window_bbox=(0, 0, 1280, 800),
                current_url=f"https://discord.com/channels/{GUILD_ID}/{THREAD_1}",
                capabilities={"can_read_ax": True, "can_stage_composer": True, "can_read_ocr": False},
            )
            draft_req = {
                "draft_id": "draft-safe-1",
                "action": "reply_existing",
                "destination": {
                    "guild_id": GUILD_ID,
                    "forum_id": FORUM_ID,
                    "thread_id": THREAD_1,
                    "source_url": f"https://discord.com/channels/{GUILD_ID}/{THREAD_1}",
                },
                "body": "Hello!\nLine 2",
            }
            resolved_ev = [{"evidence_id": "ev1", "name": "shot.png", "resolved_path": str(ev_file)}]

            with patch.object(desktop_mod.sys, "platform", "darwin"), \
                 patch.object(desktop_mod, "pyautogui", spy_gui), \
                 patch.object(desktop_mod, "pyperclip", spy_pyperclip), \
                 patch.object(desktop_mod.subprocess, "run", side_effect=spy_subproc_run), \
                 patch.object(desktop_mod.time, "sleep", return_value=None), \
                 patch.object(provider, "describe_session", return_value=ok_session), \
                 patch.object(provider, "navigate_to_url", return_value=True), \
                 patch.object(provider, "_activate_discord_window", return_value=True), \
                 patch.object(provider, "_get_ax_window", return_value=ax_win), \
                 patch.object(provider, "_inspect_composer", side_effect=lambda _w: (comp_elem, composer_state["text"])), \
                 patch.object(
                     provider,
                     "_inspect_staged_attachments",
                     side_effect=lambda _w, _c=None: list(composer_state["attachments"]),
                 ):
                stage_res = provider.stage_draft(
                    draft_req,
                    resolved_ev,
                    expected_actor={"user_id": ACTOR_USER_ID, "label": "Tester"},
                )
                self.assertEqual(stage_res.status, "staged")
                self.assertEqual(stage_res.staged_evidence_ids, ["ev1"])
                self.assertTrue(provider.clear_staged_composer())

            self.assertTrue(any(kind == "hotkey" for kind, _ in recorded_events))
            self.assertTrue(any(kind == "press" and "backspace" in args for kind, args in recorded_events))
            self.assertTrue(any(kind == "click" for kind, _ in recorded_events))
            for kind, args in recorded_events:
                if kind in {"press", "hotkey", "keyDown", "keyUp", "typewrite", "write"}:
                    for arg in args:
                        self.assertNotIn(arg.lower(), forbidden_keys)
                        self.assertNotIn(arg.lower().strip(), forbidden_keys)

        # 4. describe_session() preflights Screen Recording without calling _safe_grab & caches 2.0s
        probe_provider = DesktopDiscordProvider()
        grab_mock = MagicMock()
        fake_quartz = SimpleNamespace(CGPreflightScreenCaptureAccess=MagicMock(return_value=False))
        fake_utils = SimpleNamespace(_safe_grab=grab_mock)

        with patch.object(desktop_mod.sys, "platform", "darwin"), \
             patch.object(desktop_mod, "Quartz", fake_quartz), \
             patch.object(desktop_mod, "utils", fake_utils), \
             patch.object(desktop_mod, "_find_discord_pid", return_value=12345) as pid_mock, \
             patch.object(
                 desktop_mod,
                 "_get_discord_window_quartz",
                 return_value=("Discord", (0, 0, 1280, 800)),
             ):
            s1 = probe_provider.describe_session()
            self.assertFalse(s1.capabilities.get("can_read_ocr"))
            self.assertIn("screen_capture_unavailable", [e.get("code") for e in s1.errors])
            grab_mock.assert_not_called()
            self.assertEqual(pid_mock.call_count, 1)

            s2 = probe_provider.describe_session()
            self.assertEqual(pid_mock.call_count, 1)
            self.assertEqual(s2.capabilities, s1.capabilities)

            s3 = probe_provider.describe_session(force_refresh=True)
            self.assertEqual(pid_mock.call_count, 2)
            self.assertFalse(s3.capabilities.get("can_read_ocr"))


class TestContractAlignmentSecurityAndCorrectness(unittest.TestCase):
    def test_baseline_conflict_checks_and_target_constraints(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            store = ForumStore(td_path / "baseline.sqlite3")
            try:
                base_draft = read_record(FIXTURE_DIR / "draft_with_attachment.v1.json")

                # Uninitialized store -> conflict
                self.assertIn("not initialized", "; ".join(check_draft_conflicts(base_draft, store)))

                # Initialized empty store -> baseline not established + missing entities
                store.ensure_scope(GUILD_ID, FORUM_ID, snapshot_id="mirror-fixture-main")
                c_empty = "; ".join(check_draft_conflicts(base_draft, store))
                self.assertIn("no established baseline state", c_empty)

                # Populate matching baseline
                store.upsert_forum(forum_id=FORUM_ID, name="Fixture", source_url=build_discord_channel_url(GUILD_ID, FORUM_ID))
                store.upsert_topic(thread_id=THREAD_1, forum_id=FORUM_ID, title="North Ridge Depot", archived=False, locked=False)
                store.upsert_requirement(requirement_id="guidelines-fixture", forum_id=FORUM_ID, body="v1")
                store.upsert_requirement(requirement_id="guidelines-fixture", forum_id=FORUM_ID, body="v2")
                with store._conn:
                    store._conn.execute("UPDATE mirror_meta SET snapshot_revision = 4")

                self.assertEqual(check_draft_conflicts(base_draft, store), [])

                # Null / mismatched snapshot_id
                d_null_snap = copy.deepcopy(base_draft)
                d_null_snap["baseline"]["snapshot_id"] = None
                self.assertTrue(any("missing snapshot_id" in c for c in check_draft_conflicts(d_null_snap, store)))

                d_bad_snap = copy.deepcopy(base_draft)
                d_bad_snap["baseline"]["snapshot_id"] = "mirror-other"
                self.assertTrue(any("snapshot_id mismatch" in c for c in check_draft_conflicts(d_bad_snap, store)))

                # Invalid / future snapshot_revision
                d_future_rev = copy.deepcopy(base_draft)
                d_future_rev["baseline"]["snapshot_revision"] = 99
                self.assertTrue(any("ahead of mirror" in c for c in check_draft_conflicts(d_future_rev, store)))

                # Missing / stale requirement revision
                d_bad_req = copy.deepcopy(base_draft)
                d_bad_req["baseline"]["requirements"] = [{"requirement_id": "missing-req", "revision": 1}]
                self.assertTrue(any("missing-req missing" in c for c in check_draft_conflicts(d_bad_req, store)))

                d_stale_req = copy.deepcopy(base_draft)
                d_stale_req["baseline"]["requirements"] = [{"requirement_id": "guidelines-fixture", "revision": 1}]
                self.assertTrue(any("revision mismatch" in c for c in check_draft_conflicts(d_stale_req, store)))

                d_omitted_req = copy.deepcopy(base_draft)
                d_omitted_req["baseline"]["requirements"] = []
                self.assertTrue(
                    any("missing required requirement guidelines-fixture" in c for c in check_draft_conflicts(d_omitted_req, store))
                )

                # Archived / locked target topic
                store.upsert_topic(thread_id=THREAD_1, forum_id=FORUM_ID, title="North Ridge Depot", archived=True, locked=True)
                c_state = "; ".join(check_draft_conflicts(base_draft, store))
                self.assertIn("archived", c_state)
                self.assertIn("locked", c_state)
                store.upsert_topic(thread_id=THREAD_1, forum_id=FORUM_ID, title="North Ridge Depot", archived=False, locked=False)

                # edit_own_message target message / hash verification
                mid = "100000000000000501"
                msg_body = "Original reported line"
                store.upsert_message(
                    entity_ref=f"msg:{mid}",
                    message_id=mid,
                    thread_id=THREAD_1,
                    author={"user_id": ACTOR_USER_ID, "label": "Tester"},
                    body=msg_body,
                    observed_at="2026-11-14T10:05:00Z",
                )
                d_edit = copy.deepcopy(base_draft)
                d_edit["action"] = "edit_own_message"
                d_edit["destination"]["message_id"] = mid
                d_edit["baseline"]["message_content_hash"] = compute_content_hash(msg_body)
                self.assertEqual(check_draft_conflicts(d_edit, store), [])

                d_edit_bad_hash = copy.deepcopy(d_edit)
                d_edit_bad_hash["baseline"]["message_content_hash"] = compute_content_hash("stale text")
                self.assertTrue(any("content_hash changed" in c for c in check_draft_conflicts(d_edit_bad_hash, store)))
            finally:
                store.close()

    def test_change_validation_and_canonical_discord_urls(self):
        chg = copy.deepcopy(read_record(FIXTURE_DIR / "changes_batch.v1.json"))
        # Canonical check accepts good batch
        self.assertEqual(validate_exchange_record(chg)["schema"], "forum.changes.v1")

        # Duplicate change_id rejected
        dup_chg = copy.deepcopy(chg)
        dup_chg["changes"].append(copy.deepcopy(dup_chg["changes"][0]))
        with self.assertRaises(ContractValidationError):
            validate_exchange_record(dup_chg)

        # Non-increasing revision rejected
        bad_rev = copy.deepcopy(chg)
        bad_rev["changes"][0]["before_revision"] = 2
        bad_rev["changes"][0]["after_revision"] = 2
        with self.assertRaises(ContractValidationError):
            validate_exchange_record(bad_rev)

        # removal_observed with non-null after / after_revision rejected
        bad_rem = copy.deepcopy(chg)
        bad_rem["changes"][0]["kind"] = "removal_observed"
        with self.assertRaises(ContractValidationError):
            validate_exchange_record(bad_rem)

        # Non-canonical URL rejected
        bad_url = copy.deepcopy(chg)
        bad_url["changes"][0]["after"]["source_url"] = "https://evil.example/channels/1/2"
        with self.assertRaises(ContractValidationError):
            validate_exchange_record(bad_url)

    def test_portable_bundle_intake_validation_and_atomic_review_hold(self):
        from src.forums.exchange import canonical_record_digest
        from src.forums.handoff import BundleValidationError, ingest_draft_bundle

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            profile = _make_profile(td_path)
            provider = FakeForumProvider()
            store = ForumStore(profile.db_path)
            try:
                base_draft = copy.deepcopy(read_record(FIXTURE_DIR / "draft_with_attachment.v1.json"))
                ev_bytes = b"synthetic-fictional-image"
                ev_sha = compute_bytes_sha256(ev_bytes)
                base_draft["evidence"] = [
                    {
                        "evidence_id": "ev-bundle-1",
                        "name": "probe.png",
                        "local_ref": "probe.png",
                        "sha256": ev_sha,
                        "media_type": "image/png",
                    }
                ]
                digest = canonical_record_digest(base_draft)
                bundle_dir = td_path / digest
                (bundle_dir / "transfer").mkdir(parents=True)
                (bundle_dir / "transfer" / "probe.png").write_bytes(ev_bytes)
                write_record_atomic(bundle_dir / "draft.request.json", base_draft)

                parsed_draft, xfer_root = ingest_draft_bundle(bundle_dir)
                self.assertEqual(parsed_draft["draft_id"], base_draft["draft_id"])
                self.assertEqual(xfer_root.resolve(), (bundle_dir / "transfer").resolve())

                # Stage full bundle cleanly after seeding matching mirror state
                _seed_mirror_for_draft(store, base_draft)
                handoff = HandoffManager(store, provider)
                receipt = handoff.stage_draft_bundle(bundle_dir, profile, source_kind="external")
                self.assertEqual(receipt["status"], "staged")
                self.assertTrue(handoff.session.is_review_active())

                # Reject unexpected extra top-level files (e.g. private leakage)
                leak_bundle = td_path / "bad_extra" / digest
                (leak_bundle / "transfer").mkdir(parents=True)
                (leak_bundle / "transfer" / "probe.png").write_bytes(ev_bytes)
                write_record_atomic(leak_bundle / "draft.request.json", base_draft)
                (leak_bundle / "observation.private.json").write_text("{}", encoding="utf-8")
                with self.assertRaises(BundleValidationError):
                    ingest_draft_bundle(leak_bundle)

                # Reject unselected file inside transfer/
                extra_xfer = td_path / "bad_xfer" / digest
                (extra_xfer / "transfer").mkdir(parents=True)
                (extra_xfer / "transfer" / "probe.png").write_bytes(ev_bytes)
                (extra_xfer / "transfer" / "unselected.png").write_bytes(b"other")
                write_record_atomic(extra_xfer / "draft.request.json", base_draft)
                with self.assertRaises(BundleValidationError):
                    ingest_draft_bundle(extra_xfer)

                # Reject wrong hash dirname
                bad_dir = td_path / "wronghash"
                (bad_dir / "transfer").mkdir(parents=True)
                (bad_dir / "transfer" / "probe.png").write_bytes(ev_bytes)
                write_record_atomic(bad_dir / "draft.request.json", base_draft)
                with self.assertRaises(BundleValidationError):
                    ingest_draft_bundle(bad_dir)

                # Reject symlink in transfer/
                sym_dir = td_path / "sym_wrap" / digest
                (sym_dir / "transfer").mkdir(parents=True)
                outside = td_path / "outside.png"
                outside.write_bytes(ev_bytes)
                (sym_dir / "transfer" / "probe.png").symlink_to(outside)
                write_record_atomic(sym_dir / "draft.request.json", base_draft)
                with self.assertRaises(BundleValidationError):
                    ingest_draft_bundle(sym_dir)
            finally:
                store.close()

    def test_catalog_enabled_profile_isolation_and_partial_preservation(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            p1 = ForumProfile(
                profile_id="prof_alpha",
                name="Alpha",
                guild_id=GUILD_ID,
                forum_id=FORUM_ID,
                source_url=build_discord_channel_url(GUILD_ID, FORUM_ID),
                purpose="external_project",
                catalog_enabled=False,
            )
            p2 = ForumProfile(
                profile_id="prof_beta",
                name="Beta",
                guild_id=GUILD_ID,
                forum_id="200000000000000002",
                source_url=build_discord_channel_url(GUILD_ID, "200000000000000002"),
                purpose="external_project",
                catalog_enabled=True,
            )
            # Per-profile DB isolation by default
            self.assertNotEqual(p1.db_path, p2.db_path)
            self.assertTrue(p1.db_path.endswith("prof_alpha.sqlite3"))

            store = ForumStore(td_path / "isolated.sqlite3")
            try:
                provider = FakeForumProvider()
                reader = ForumReader(p1, store, provider)
                # catalog_enabled=False pauses before touching provider
                with patch.object(provider, "describe_session", side_effect=AssertionError("should not probe")):
                    res_backfill = reader.initial_backfill()
                    res_refresh = reader.incremental_refresh()
                self.assertEqual(res_backfill["reason"], "catalog_disabled")
                self.assertEqual(res_refresh["reason"], "catalog_disabled")

                # Partial topic update preserves existing tags/archived/locked/starter_message_id
                store.upsert_topic(
                    thread_id=THREAD_1,
                    forum_id=FORUM_ID,
                    title="Initial Title",
                    tags=[{"tag_id": "1", "name": "POI"}],
                    starter_message_id=THREAD_1,
                    archived=False,
                    locked=False,
                )
                chg_partial = store.upsert_topic(
                    thread_id=THREAD_1,
                    forum_id=FORUM_ID,
                    title="Updated Title",
                    tags=None,
                    archived=None,
                    locked=None,
                    starter_message_id=None,
                )
                self.assertIsNotNone(chg_partial)
                snap = store.export_snapshot(guild_id=GUILD_ID, forum_id=FORUM_ID)
                t_after = next(t for t in snap["topics"] if t["thread_id"] == THREAD_1)
                self.assertEqual(t_after["tags"], [{"tag_id": "1", "name": "POI"}])
                self.assertEqual(t_after["starter_message_id"], THREAD_1)
                self.assertFalse(t_after["archived"])

                # Incomplete requirement read must not clobber complete requirement
                store.upsert_requirement(
                    requirement_id="guide-1",
                    forum_id=FORUM_ID,
                    body="Complete multi-section rules",
                    complete=True,
                )
                self.assertIsNone(
                    store.upsert_requirement(
                        requirement_id="guide-1",
                        forum_id=FORUM_ID,
                        body="Truncated prefix only",
                        complete=False,
                    )
                )
                snap2 = store.export_snapshot(guild_id=GUILD_ID, forum_id=FORUM_ID)
                self.assertEqual(snap2["requirements"][0]["body"], "Complete multi-section rules")
                self.assertEqual(snap2["requirements"][0]["revision"], 1)

                # Multi-page >50 topics completion latching
                p_multi = _make_profile(td_path)
                p_multi.db_path = str(td_path / "multi.sqlite3")
                multi_store = ForumStore(p_multi.db_path)
                try:
                    topics_55 = [
                        TopicObservation(
                            forum_id=FORUM_ID,
                            thread_id=str(400000000000000000 + i),
                            title=f"Topic {i}",
                            source_url=build_discord_channel_url(GUILD_ID, str(400000000000000000 + i)),
                        )
                        for i in range(55)
                    ]
                    multi_prov = FakeForumProvider()
                    multi_prov.topic_pages = [
                        TopicListObservation(topics=topics_55[:50], next_cursor="1", complete=False),
                        TopicListObservation(topics=topics_55[50:], next_cursor=None, complete=True),
                    ]
                    multi_prov.requirements = [
                        RequirementObservation(requirement_id="req-m", forum_id=FORUM_ID, body="Rules")
                    ]
                    for t in topics_55:
                        multi_prov.histories[t.thread_id] = TopicHistoryObservation(topic=t, messages=[], complete=True)
                    multi_reader = ForumReader(p_multi, multi_store, multi_prov)
                    res_multi = multi_reader.initial_backfill()
                    self.assertEqual(res_multi["coverage"]["topics"], "complete")
                    self.assertEqual(res_multi["coverage"]["histories"], "complete")
                    self.assertEqual(
                        multi_store.get_forum_status(guild_id=GUILD_ID, forum_id=FORUM_ID)["counts"]["topics"],
                        55,
                    )
                finally:
                    multi_store.close()
            finally:
                store.close()

    def test_desktop_guards_submission_exclusions_and_ocr_box_inverse(self):
        from src.forums.providers import desktop as desktop_mod
        from src.forums.providers.desktop import DesktopDiscordProvider

        # 1. _find_discord_pid handles psutil.process_iter outer errors gracefully
        with patch.object(desktop_mod, "psutil", SimpleNamespace(process_iter=lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("denied")))):
            self.assertIsNone(desktop_mod._find_discord_pid())

        # 2. _actor_mismatch_reason fails closed on unverified user_id or label
        self.assertIn(
            "account_unverified",
            DesktopDiscordProvider._actor_mismatch_reason(
                {"user_id": ACTOR_USER_ID, "label": "Tester"},
                {"user_id": None, "label": "Unverified desktop session"},
            )
            or "",
        )
        self.assertIn(
            "account_unverified",
            DesktopDiscordProvider._actor_mismatch_reason(
                {"user_id": None, "label": "Tester"},
                {"user_id": None, "label": "Unverified desktop session"},
            )
            or "",
        )
        self.assertIsNone(
            DesktopDiscordProvider._actor_mismatch_reason(
                {"user_id": None, "label": "Selected account"},
                {"user_id": None, "label": "Tester"},
            )
        )
        self.assertTrue(
            (
                DesktopDiscordProvider._actor_mismatch_reason(
                    {"user_id": None, "label": "Selected account"},
                    {"user_id": None, "label": "Unverified desktop session"},
                )
                or ""
            ).startswith("account_unverified:")
        )
        self.assertTrue(
            (
                DesktopDiscordProvider._actor_mismatch_reason(
                    {"user_id": None, "label": "OtherUser"},
                    {"user_id": None, "label": "Tester"},
                )
                or ""
            ).startswith("account_mismatch:")
        )

        # 3. stage_draft rejects non-reply_existing action and mismatched destination.source_url before navigation
        provider = DesktopDiscordProvider()
        good_session = SessionDescription(
            actor={"user_id": ACTOR_USER_ID, "label": "Tester"},
            app_name="Discord",
            window_title="Discord",
            window_bbox=(0, 0, 1280, 800),
            current_url=build_discord_channel_url(GUILD_ID, THREAD_1),
            capabilities={"can_read_semantic": True, "can_read_ocr": True, "can_stage_composer": True},
        )
        with patch.object(provider, "describe_session", return_value=good_session), \
             patch.object(provider, "navigate_to_url") as nav_mock:
            bad_action = provider.stage_draft(
                {
                    "draft_id": "d-edit",
                    "action": "edit_own_message",
                    "destination": {"guild_id": GUILD_ID, "forum_id": FORUM_ID, "thread_id": THREAD_1},
                    "body": "Edited",
                },
                [],
                expected_actor={"user_id": ACTOR_USER_ID, "label": "Tester"},
            )
            self.assertEqual(bad_action.status, "needs_review")
            self.assertIn("unsupported_stage_action", bad_action.reason or "")
            nav_mock.assert_not_called()

            bad_url = provider.stage_draft(
                {
                    "draft_id": "d-url",
                    "action": "reply_existing",
                    "destination": {
                        "guild_id": GUILD_ID,
                        "forum_id": FORUM_ID,
                        "thread_id": THREAD_1,
                        "source_url": build_discord_channel_url(GUILD_ID, THREAD_2),
                    },
                    "body": "Hello",
                },
                [],
                expected_actor={"user_id": ACTOR_USER_ID, "label": "Tester"},
            )
            self.assertEqual(bad_url.status, "needs_review")
            self.assertIn("invalid_destination_source_url", bad_url.reason or "")
            nav_mock.assert_not_called()

        # Unverified post-nav current_url=None fails closed
        unverified_post_session = copy.deepcopy(good_session)
        unverified_post_session.current_url = None
        with patch.object(provider, "describe_session", return_value=unverified_post_session), \
             patch.object(provider, "navigate_to_url", return_value=True), \
             patch.object(provider, "_get_ax_window", return_value=object()), \
             patch.object(provider, "_inspect_composer", return_value=(object(), "")), \
             patch.object(desktop_mod.time, "sleep"):
            res_unver = provider.stage_draft(
                {
                    "draft_id": "d-unver",
                    "action": "reply_existing",
                    "destination": {"guild_id": GUILD_ID, "forum_id": FORUM_ID, "thread_id": THREAD_1},
                    "body": "Hello",
                },
                [],
            )
            self.assertEqual(res_unver.status, "needs_review")
            self.assertEqual(res_unver.reason, "destination_url_unverified")

        # 4. Slot-count-only attachment staging records honest verification limitation in reason
        with tempfile.TemporaryDirectory() as td:
            att_f = Path(td) / "probe.txt"
            att_f.write_text("ok", encoding="utf-8")
            clip = {"val": "", "pasted": False}
            fake_clip = SimpleNamespace(paste=lambda: clip["val"], copy=lambda v: clip.__setitem__("val", v))

            def slot_hotkey(*args, **_k):
                if args and args[-1] == "v" and clip["val"] == "Line 1":
                    clip["pasted"] = True

            fake_gui = SimpleNamespace(hotkey=slot_hotkey, click=lambda *_a, **_k: None)
            with patch.object(desktop_mod.sys, "platform", "darwin"), \
                 patch.object(desktop_mod, "pyperclip", fake_clip), \
                 patch.object(desktop_mod, "pyautogui", fake_gui), \
                 patch.object(desktop_mod.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
                 patch.object(desktop_mod.time, "sleep"), \
                 patch.object(provider, "describe_session", return_value=good_session), \
                 patch.object(provider, "navigate_to_url", return_value=True), \
                 patch.object(provider, "_get_ax_window", return_value=object()), \
                 patch.object(provider, "_focus_element", return_value=True), \
                 patch.object(provider, "_inspect_composer", side_effect=lambda _ax: (object(), "Line 1\n" if clip["pasted"] else "")), \
                 patch.object(
                     provider,
                     "_inspect_staged_attachments",
                     side_effect=[
                         [],  # initial baseline_attachments in stage_draft
                         [],  # baseline_slots in _stage_single_attachment
                         [{"role": "AXButton", "text": "Remove Attachment", "dom_id": "upload-1", "element": object(), "is_remove_button": True}],
                     ],
                 ):
                slot_res = provider.stage_draft(
                    {
                        "draft_id": "d-slot",
                        "action": "reply_existing",
                        "destination": {"guild_id": GUILD_ID, "forum_id": FORUM_ID, "thread_id": THREAD_1},
                        "body": "Line 1",
                    },
                    [{"evidence_id": "ev-slot", "name": "probe.txt", "resolved_path": str(att_f)}],
                )
                self.assertEqual(slot_res.status, "staged")
                self.assertEqual(slot_res.reason, "attachment_slot_count_observed_filename_unverified")

        # 5. observe_submission excludes _preexisting_message_ids and remote_created_at < created_at
        old_pre = MessageObservation(
            thread_id=THREAD_1,
            entity_ref="msg:200",
            message_id="200",
            author={"user_id": ACTOR_USER_ID, "label": "Tester"},
            body="Line 1\nLine 2 ✓",
            remote_created_at="2026-11-14T11:59:00Z",
            source_url=build_discord_channel_url(GUILD_ID, THREAD_1, "200"),
        )
        old_time = MessageObservation(
            thread_id=THREAD_1,
            entity_ref="msg:999",
            message_id="999",
            author={"user_id": ACTOR_USER_ID, "label": "Tester"},
            body="Line 1\nLine 2 ✓",
            remote_created_at="2026-11-14T11:00:00Z",
            source_url=build_discord_channel_url(GUILD_ID, THREAD_1, "999"),
        )
        hist_old = TopicHistoryObservation(
            topic=TopicObservation(forum_id=FORUM_ID, thread_id=THREAD_1, title="North Ridge"),
            messages=[old_pre, old_time],
            complete=True,
        )
        with patch.object(provider, "describe_session", return_value=good_session), \
             patch.object(provider, "read_topic", return_value=hist_old), \
             patch.object(provider, "_get_ax_window", return_value=None):
            sub_res = provider.observe_submission(
                {
                    "draft_id": "d-sub-excl",
                    "action": "reply_existing",
                    "created_at": "2026-11-14T12:00:00Z",
                    "destination": {"guild_id": GUILD_ID, "forum_id": FORUM_ID, "thread_id": THREAD_1},
                    "body": "Line 1\nLine 2 ✓",
                },
                {"_preexisting_message_ids": ["200"], "baseline_latest_message_id": None},
            )
            self.assertEqual(sub_res.status, "unresolved")

        # 6. OCR box inverse transform accounts for preprocessing pad and tooltip crop offset
        fake_data = {
            "text": ["North"],
            "conf": ["95"],
            "left": [46],   # e.g., at scale=3, pad=16 -> (46 - 16) / 3 = 10
            "top": [46],
            "width": [30],
            "height": [18],
            "block_num": [1],
            "par_num": [1],
            "line_num": [1],
        }
        _raw, _lines, boxes, _conf = utils._extract_lines_and_boxes(
            fake_data, scale=3, pad=16, crop_offset=(25, 12)
        )
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0].left, 35)
        self.assertEqual(boxes[0].top, 22)
        self.assertEqual(boxes[0].width, 10)
        self.assertEqual(boxes[0].height, 6)

        # 7. _extract_post_cards_and_links excludes AXLink outside forum post cards
        fake_links_nodes = [
            {
                "role": "AXLink",
                "description": "Arbitrary same-guild channel link",
                "title": "general",
                "value": "",
                "url": build_discord_channel_url(GUILD_ID, "999999999999999999"),
                "dom_id": "",
                "depth": 25,
                "element": object(),
            }
        ]
        with patch.object(desktop_mod, "_ax_attr", return_value=None):
            cards, links, _ = provider._extract_post_cards_and_links(fake_links_nodes, GUILD_ID, FORUM_ID)
        self.assertEqual(cards, [])
        self.assertEqual(links, [])

        # 8. read_requirements propagates partial completeness and errors
        req_topic = TopicObservation(forum_id=FORUM_ID, thread_id=THREAD_2, title="Read Me First")
        req_hist = TopicHistoryObservation(
            topic=req_topic,
            messages=[MessageObservation(thread_id=THREAD_2, author={"user_id": None, "label": "Mod"}, body="Rule slice")],
            complete=False,
            errors=[{"code": "scroll_stagnant", "message": "Truncated", "thread_id": THREAD_2}],
        )
        with patch.object(provider, "_get_ax_window", return_value=object()), \
             patch.object(provider, "read_topic", return_value=req_hist):
            reqs_out = provider.read_requirements(
                GUILD_ID,
                FORUM_ID,
                references=[{"requirement_id": "req-pin", "thread_id": THREAD_2}],
            )
        self.assertEqual(len(reqs_out), 1)
        self.assertFalse(reqs_out[0].complete)
        self.assertIsNone(reqs_out[0].source_entity_ref)
        self.assertEqual(reqs_out[0].errors[0]["code"], "scroll_stagnant")

        # 9. read_topic sets starter_message_id=None when top not reached and starter not visible
        reply_only_msg = MessageObservation(
            thread_id=THREAD_1,
            entity_ref="msg:500000000000000099",
            message_id="500000000000000099",
            author={"user_id": None, "label": "Tester"},
            body="Later reply only",
            source_url=build_discord_channel_url(GUILD_ID, THREAD_1, "500000000000000099"),
        )
        msg_list_elem = object()
        fake_msg_win_nodes = [
            {
                "role": "AXList",
                "description": "Messages in North Ridge Depot",
                "title": "",
                "value": "",
                "url": "",
                "dom_id": f"chat-messages-{THREAD_1}-500000000000000099",
                "depth": 4,
                "element": msg_list_elem,
            }
        ]
        with patch.object(provider, "describe_session", return_value=good_session), \
             patch.object(provider, "_get_ax_window", return_value=object()), \
             patch.object(desktop_mod, "_walk_ax_tree", return_value=fake_msg_win_nodes), \
             patch.object(desktop_mod, "_ax_bounds", return_value=(0.0, 0.0, 500.0, 400.0)), \
             patch.object(desktop_mod, "_scroll_at_point", return_value=False), \
             patch.object(
                 provider,
                 "_parse_messages_from_ax_list",
                 return_value=("North Ridge Depot", [], {"500000000000000099": reply_only_msg}),
             ):
            trunc_hist = provider.read_topic(GUILD_ID, FORUM_ID, THREAD_1)
        self.assertFalse(trunc_hist.complete)
        self.assertIsNone(trunc_hist.topic.starter_message_id)
        self.assertIsNone(trunc_hist.topic.tags)

    def test_retry_transient_needs_review_and_cli_bundle_stage_with_microsecond_ts(self) -> None:
        from contextlib import redirect_stderr, redirect_stdout
        import io
        from src.forums import __main__ as forums_cli
        from src.forums.exchange import canonical_record_digest
        from src.forums.models import compute_bytes_sha256, parse_rfc3339_utc, utc_now_iso

        ts = utc_now_iso()
        self.assertIn(".", ts)
        self.assertTrue(ts.endswith("Z"))
        self.assertLess(
            parse_rfc3339_utc("2026-11-14T22:23:07Z"),
            parse_rfc3339_utc("2026-11-14T22:23:07.500000Z"),
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "mirror.sqlite3"
            ex_dir = root / "exchange"
            profiles_path = root / "profiles.json"

            fixture_path = Path("tests/fixtures/forum_exchange_v1/draft_with_attachment.v1.json")
            draft = copy.deepcopy(read_record(fixture_path))
            scope = draft["scope"]
            gid, fid, tid = scope["guild_id"], scope["forum_id"], draft["destination"]["thread_id"]
            prof = ForumProfile(
                profile_id="cli-bundle-prof",
                name="CLI Bundle Forum",
                guild_id=gid,
                forum_id=fid,
                purpose="external_project",
                catalog_enabled=True,
                external_drafts_enabled=True,
                stage_enabled=True,
                db_path=str(db_path),
                transfer_root=str(root / "unused_transfer"),
                exchange_dir=str(ex_dir),
            )
            upsert_forum_profile(prof, profiles_path)

            store = ForumStore(db_path)
            try:
                _seed_mirror_for_draft(store, draft)
            finally:
                store.close()

            ev_bytes = b"SYNTHETIC-PNG-PROBE-BYTES"
            draft["evidence"][0]["sha256"] = compute_bytes_sha256(ev_bytes)
            bundle_digest = canonical_record_digest(draft)
            bundle_dir = root / "bundles" / bundle_digest
            ev_path = bundle_dir / "transfer" / draft["evidence"][0]["local_ref"]
            ev_path.parent.mkdir(parents=True, exist_ok=True)
            ev_path.write_bytes(ev_bytes)
            write_record_atomic(bundle_dir / "draft.request.json", draft)

            receipt_out = root / "cli_receipt.json"
            fail_obs = ComposerStageObservation(
                status="needs_review",
                observed_actor={"user_id": scope["actor"]["user_id"], "label": "DemoUser"},
                destination=dict(draft["destination"]),
                observed_composer_text=draft["body"],
                staged_evidence_ids=[],
                reason="attachment_upload_interrupted",
            )
            ok_obs = ComposerStageObservation(
                status="staged",
                observed_actor={"user_id": scope["actor"]["user_id"], "label": "DemoUser"},
                destination=dict(draft["destination"]),
                observed_composer_text=draft["body"],
                staged_evidence_ids=[ev["evidence_id"] for ev in draft["evidence"]],
                reason="staged_verified",
            )

            class FakeDesktopProvider:
                calls = 0

                def describe_session(self) -> SessionDescription:
                    return SessionDescription(
                        actor={"user_id": scope["actor"]["user_id"], "label": "DemoUser"},
                        app_name="Discord",
                        window_title="Discord",
                        window_bbox=(0, 0, 1000, 700),
                        current_url=draft["destination"]["source_url"],
                        capabilities={"can_read_semantic": True, "can_stage_composer": True},
                    )

                def stage_draft(self, *_args: Any, **_kwargs: Any) -> ComposerStageObservation:
                    FakeDesktopProvider.calls += 1
                    return fail_obs if FakeDesktopProvider.calls == 1 else ok_obs

            with patch.object(forums_cli, "DesktopDiscordProvider", FakeDesktopProvider):
                buf1 = io.StringIO()
                with redirect_stdout(buf1):
                    rc1 = forums_cli.main([
                        "--profiles-path", str(profiles_path),
                        "stage", "--profile", prof.profile_id,
                        "--bundle-dir", str(bundle_dir),
                        "--receipt-out", str(receipt_out),
                    ])
                self.assertEqual(rc1, 0)
                r1 = json.loads(buf1.getvalue())
                self.assertEqual(r1["status"], "needs_review")
                self.assertEqual(r1["reason"], "attachment_upload_interrupted")

                # Review lock holds across processes, and transient failure did not poison needs_recheck
                ctrl = SessionController(Path(db_path).with_suffix(".session.json"))
                self.assertTrue(ctrl.is_review_active())
                store_chk = ForumStore(db_path)
                try:
                    drow = store_chk.get_draft_row(draft["draft_id"], int(draft["revision"]))
                    self.assertIsNotNone(drow)
                    self.assertEqual(drow["needs_recheck"], 0)
                finally:
                    store_chk.close()

                # Releasing review allows clean retry of the same bundle via CLI
                ctrl.release_review()
                buf2 = io.StringIO()
                with redirect_stdout(buf2):
                    rc2 = forums_cli.main([
                        "--profiles-path", str(profiles_path),
                        "stage", "--profile", prof.profile_id,
                        "--bundle-dir", str(bundle_dir),
                        "--receipt-out", str(receipt_out),
                    ])
                self.assertEqual(rc2, 0)
                r2 = json.loads(buf2.getvalue())
                self.assertEqual(r2["status"], "staged")
                self.assertEqual(r2["observed_body_hash"], compute_content_hash(draft["body"]))
                self.assertTrue(receipt_out.exists())

            # CLI rejects a corrupted bundle without staging
            (ev_path).write_text("tampered", encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                rc_bad = forums_cli.main([
                    "--profiles-path", str(profiles_path),
                    "stage", "--profile", prof.profile_id,
                    "--bundle-dir", str(bundle_dir),
                ])
            self.assertEqual(rc_bad, 2)

    def test_unverified_attachments_preserve_prior_mirror_while_verified_empty_clears(self) -> None:
        from src.forums.providers import desktop as desktop_mod

        class _FakeAX:
            def __init__(
                self,
                role: str = "",
                *,
                title: str = "",
                description: str = "",
                value: str = "",
                url: str = "",
                dom_id: str = "",
                children: Optional[list[Any]] = None,
            ) -> None:
                self.attrs: dict[str, Any] = {
                    "AXRole": role,
                    "AXTitle": title,
                    "AXDescription": description,
                    "AXValue": value,
                    "AXURL": url,
                    "AXDOMIdentifier": dom_id,
                    "AXChildren": list(children or []),
                    "AXParent": None,
                }
                for ch in self.attrs["AXChildren"]:
                    ch.attrs["AXParent"] = self

        # Provider level: unparsed non-empty accessories or missing content node -> attachments=None
        # Verified empty/absent accessories with content node -> attachments=[]
        m1 = _FakeAX(
            "AXGroup",
            dom_id=f"chat-messages-{THREAD_1}-600000000000000001",
            children=[
                _FakeAX("AXGroup", dom_id="message-content-600000000000000001", children=[
                    _FakeAX("AXStaticText", value="With image"),
                ]),
                _FakeAX("AXGroup", dom_id="message-accessories-600000000000000001", children=[
                    _FakeAX("AXLink", url="https://cdn.discordapp.com/attachments/1/2/shot.png"),
                ]),
            ],
        )
        m2 = _FakeAX(
            "AXGroup",
            dom_id=f"chat-messages-{THREAD_1}-600000000000000002",
            children=[
                _FakeAX("AXGroup", dom_id="message-content-600000000000000002", children=[
                    _FakeAX("AXStaticText", value="Unparsed acc"),
                ]),
                _FakeAX("AXGroup", dom_id="message-accessories-600000000000000002", children=[
                    _FakeAX("AXImage", description="Image without AXURL"),
                ]),
            ],
        )
        m3 = _FakeAX(
            "AXGroup",
            dom_id=f"chat-messages-{THREAD_1}-600000000000000003",
            children=[
                _FakeAX("AXGroup", dom_id="message-accessories-600000000000000003"),
            ],
        )
        m4 = _FakeAX(
            "AXGroup",
            dom_id=f"chat-messages-{THREAD_1}-600000000000000004",
            children=[
                _FakeAX("AXGroup", dom_id="message-content-600000000000000004", children=[
                    _FakeAX("AXStaticText", value="Verified empty acc"),
                ]),
                _FakeAX("AXGroup", dom_id="message-accessories-600000000000000004"),
            ],
        )
        m5 = _FakeAX(
            "AXGroup",
            dom_id=f"chat-messages-{THREAD_1}-600000000000000005",
            children=[
                _FakeAX("AXGroup", dom_id="message-content-600000000000000005", children=[
                    _FakeAX("AXStaticText", value="No acc node"),
                ]),
            ],
        )
        m6 = _FakeAX(
            "AXGroup",
            dom_id=f"chat-messages-{THREAD_1}-600000000000000006",
            children=[
                _FakeAX("AXGroup", dom_id="message-content-600000000000000006", children=[
                    _FakeAX("AXStaticText", value="Reactions only inside accessories"),
                ]),
                _FakeAX("AXGroup", dom_id="message-accessories-600000000000000006", children=[
                    _FakeAX("AXGroup", dom_id="message-reactions-600000000000000006", children=[
                        _FakeAX("AXButton", description="Add Reaction"),
                    ]),
                ]),
            ],
        )
        m7 = _FakeAX(
            "AXGroup",
            dom_id=f"chat-messages-{THREAD_1}-600000000000000007",
            children=[
                _FakeAX("AXGroup", dom_id="message-content-600000000000000007", children=[
                    _FakeAX("AXStaticText", value="Reactions plus attachment"),
                ]),
                _FakeAX("AXGroup", dom_id="message-accessories-600000000000000007", children=[
                    _FakeAX("AXLink", url="https://cdn.discordapp.com/attachments/1/2/crop.png"),
                    _FakeAX("AXGroup", dom_id="message-reactions-600000000000000007", children=[
                        _FakeAX("AXButton", description="Add Reaction"),
                    ]),
                ]),
            ],
        )
        msg_list = _FakeAX(
            "AXList",
            description="Messages in North Ridge Depot",
            children=[m1, m2, m3, m4, m5, m6, m7],
        )
        provider = desktop_mod.DesktopDiscordProvider()
        with patch.object(
            desktop_mod,
            "_ax_attr",
            side_effect=lambda el, attr: el.attrs.get(attr) if hasattr(el, "attrs") else None,
        ):
            _, _, msgs = provider._parse_messages_from_ax_list(msg_list, GUILD_ID, THREAD_1)

        self.assertEqual(len(msgs["600000000000000001"].attachments or []), 1)
        self.assertEqual(msgs["600000000000000001"].attachments[0]["name"], "shot.png")
        self.assertIsNone(msgs["600000000000000002"].attachments)
        self.assertIsNone(msgs["600000000000000003"].attachments)
        self.assertEqual(msgs["600000000000000004"].attachments, [])
        self.assertEqual(msgs["600000000000000005"].attachments, [])
        self.assertEqual(msgs["600000000000000006"].attachments, [])
        self.assertEqual(len(msgs["600000000000000007"].attachments or []), 1)
        self.assertEqual(msgs["600000000000000007"].attachments[0]["name"], "crop.png")

        # Store level: None preserves previously stored attachments even across body edit; [] removes them
        with tempfile.TemporaryDirectory() as tmp:
            store = ForumStore(Path(tmp) / "mirror.sqlite3")
            try:
                store.ensure_scope(GUILD_ID, FORUM_ID, snapshot_id="snap-att")
                store.upsert_forum(forum_id=FORUM_ID, name="Forum", source_url=build_discord_channel_url(GUILD_ID, FORUM_ID))
                store.upsert_topic(thread_id=THREAD_1, forum_id=FORUM_ID, title="North Ridge Depot")
                store.upsert_message(
                    entity_ref="msg:600000000000000001",
                    message_id="600000000000000001",
                    thread_id=THREAD_1,
                    author={"user_id": None, "label": "FixtureAuthor"},
                    body="Initial caption",
                    attachments=[{"name": "shot.png", "source_url": "https://cdn.discordapp.com/attachments/1/2/shot.png"}],
                )
                quiet_chg = store.upsert_message(
                    entity_ref="msg:600000000000000001",
                    message_id="600000000000000001",
                    thread_id=THREAD_1,
                    author={"user_id": None, "label": "FixtureAuthor"},
                    body="Initial caption",
                    attachments=None,
                )
                self.assertIsNone(quiet_chg)
                edit_chg = store.upsert_message(
                    entity_ref="msg:600000000000000001",
                    message_id="600000000000000001",
                    thread_id=THREAD_1,
                    author={"user_id": None, "label": "FixtureAuthor"},
                    body="Edited caption",
                    attachments=None,
                )
                self.assertIsNotNone(edit_chg)
                self.assertEqual(len(edit_chg["after"]["attachments"]), 1)
                self.assertEqual(len(store.export_snapshot()["messages"][0]["attachments"]), 1)

                del_chg = store.upsert_message(
                    entity_ref="msg:600000000000000001",
                    message_id="600000000000000001",
                    thread_id=THREAD_1,
                    author={"user_id": None, "label": "FixtureAuthor"},
                    body="Edited caption",
                    attachments=[],
                )
                self.assertIsNotNone(del_chg)
                self.assertEqual(del_chg["kind"], "attachment_changed")
                self.assertEqual(store.export_snapshot()["messages"][0]["attachments"], [])
            finally:
                store.close()

    def test_extract_post_cards_and_links_rejects_non_card_and_cross_channel_links(self) -> None:
        from src.forums.providers import desktop as desktop_mod

        class _FakeAX:
            def __init__(
                self,
                role: str = "",
                *,
                title: str = "",
                description: str = "",
                value: str = "",
                url: str = "",
                dom_id: str = "",
                children: Optional[list[Any]] = None,
            ) -> None:
                self.attrs: dict[str, Any] = {
                    "AXRole": role,
                    "AXTitle": title,
                    "AXDescription": description,
                    "AXValue": value,
                    "AXURL": url,
                    "AXDOMIdentifier": dom_id,
                    "AXChildren": list(children or []),
                    "AXParent": None,
                }
                for ch in self.attrs["AXChildren"]:
                    ch.attrs["AXParent"] = self

        win = _FakeAX(
            "AXWindow",
            children=[
                _FakeAX("AXList", description="Messages in #general", children=[
                    _FakeAX("AXLink", description="Chat link", url=build_discord_channel_url(GUILD_ID, "888000000000000001")),
                ]),
                _FakeAX("AXList", description="Channels", children=[
                    _FakeAX("AXLink", description="Sidebar link", url=build_discord_channel_url(GUILD_ID, "888000000000000002")),
                ]),
                _FakeAX("AXList", description="Forum posts", children=[
                    _FakeAX("AXGroup", children=[
                        _FakeAX("AXButton", description="Post Card 101"),
                        _FakeAX("AXGroup", dom_id="message-content-888000000000000101", children=[
                            _FakeAX("AXHeading", title="Card 101"),
                            _FakeAX("AXLink", description="Mentioned channel", url=build_discord_channel_url(GUILD_ID, "888000000000000003")),
                        ]),
                    ]),
                    _FakeAX("AXGroup", children=[
                        _FakeAX("AXButton", description="Post Permalink Card"),
                        _FakeAX(
                            "AXLink",
                            description="Permalink",
                            url=build_discord_channel_url(GUILD_ID, "888000000000000004", "999000000000000001"),
                        ),
                    ]),
                    _FakeAX("AXGroup", children=[
                        _FakeAX("AXButton", description="Post Link-Only Topic"),
                        _FakeAX(
                            "AXLink",
                            description="Link-Only Topic",
                            url=build_discord_channel_url(GUILD_ID, "888000000000000005"),
                        ),
                    ]),
                ]),
            ],
        )
        provider = desktop_mod.DesktopDiscordProvider()
        with patch.object(
            desktop_mod,
            "_ax_attr",
            side_effect=lambda el, attr: el.attrs.get(attr) if hasattr(el, "attrs") else None,
        ):
            nodes = desktop_mod._walk_ax_tree(win)
            cards, links, _ = provider._extract_post_cards_and_links(nodes, GUILD_ID, FORUM_ID)

        self.assertEqual([c.thread_id for c in cards], ["888000000000000101"])
        self.assertEqual([l.thread_id for l in links], ["888000000000000005"])

    def test_mixed_precision_rfc3339_timestamps_sort_chronologically_in_sql(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ForumStore(Path(tmp) / "mirror.sqlite3")
            try:
                store.ensure_scope(GUILD_ID, FORUM_ID, snapshot_id="snap-ts")
                store.upsert_forum(forum_id=FORUM_ID, name="Forum", source_url=build_discord_channel_url(GUILD_ID, FORUM_ID))
                store.upsert_topic(thread_id=THREAD_1, forum_id=FORUM_ID, title="Mixed Timestamps")

                # Insert .500000Z BEFORE whole-second Z so both ASCII and rowid order would invert chronology
                store.upsert_message(
                    entity_ref="msg:700000000000000002",
                    message_id="700000000000000002",
                    thread_id=THREAD_1,
                    author={"user_id": None, "label": "Tester"},
                    body="Second chronologically (token-alpha)",
                    observed_at="2026-11-14T22:23:07.500000Z",
                )
                store.upsert_message(
                    entity_ref="msg:700000000000000001",
                    message_id="700000000000000001",
                    thread_id=THREAD_1,
                    author={"user_id": None, "label": "Tester"},
                    body="First chronologically (token-alpha)",
                    observed_at="2026-11-14T22:23:07Z",
                )

                status = store.get_topic_status(THREAD_1)
                self.assertEqual(
                    [m["message_id"] for m in status["latest_messages"]],
                    ["700000000000000001", "700000000000000002"],
                )
                snap_msgs = store.export_snapshot()["messages"]
                self.assertEqual(
                    [m["message_id"] for m in snap_msgs],
                    ["700000000000000001", "700000000000000002"],
                )
                search_msgs = store.search("token-alpha", forum_id=FORUM_ID)["messages"]
                self.assertEqual(
                    [m["message_id"] for m in search_msgs],
                    ["700000000000000002", "700000000000000001"],
                )

                base_receipt = {
                    "schema": "handoff.receipt.v1",
                    "producer_id": "discord-promo-helper",
                    "scope": {"guild_id": GUILD_ID, "forum_id": FORUM_ID, "actor": {"user_id": None, "label": "Tester"}},
                    "draft_id": "draft-mixed-ts",
                    "draft_revision": 1,
                    "destination": {"guild_id": GUILD_ID, "forum_id": FORUM_ID, "thread_id": THREAD_1, "message_id": None, "action": "reply_existing", "source_url": build_discord_channel_url(GUILD_ID, THREAD_1)},
                    "actor": {"user_id": None, "label": "Tester"},
                    "status": "needs_review",
                    "reason": "temp",
                    "message_id": None,
                    "message_url": None,
                    "observed_body_hash": None,
                    "evidence_ids": [],
                }
                r_frac = {**base_receipt, "receipt_id": "rcpt-frac", "record_id": "rec-frac", "created_at": "2026-11-14T22:23:07.500000Z", "observed_at": "2026-11-14T22:23:07.500000Z", "status": "staged"}
                r_whole = {**base_receipt, "receipt_id": "rcpt-whole", "record_id": "rec-whole", "created_at": "2026-11-14T22:23:07Z", "observed_at": "2026-11-14T22:23:07Z", "status": "needs_review"}
                store.save_receipt(r_frac)
                store.save_receipt(r_whole)
                receipts = store.list_receipts("draft-mixed-ts")
                self.assertEqual([r["record_id"] for r in receipts], ["rec-whole", "rec-frac"])
            finally:
                store.close()

    def test_sync_stage_pending_retries_transient_needs_review_and_skips_recheck(self) -> None:
        from contextlib import redirect_stdout
        import io
        from src.forums import __main__ as forums_cli
        from src.forums.exchange import canonical_record_digest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "mirror.sqlite3"
            ex_dir = root / "exchange"
            profiles_path = root / "profiles.json"

            fixture_path = Path("tests/fixtures/forum_exchange_v1/draft_with_attachment.v1.json")
            draft_a = copy.deepcopy(read_record(fixture_path))
            scope = draft_a["scope"]
            gid, fid, tid = scope["guild_id"], scope["forum_id"], draft_a["destination"]["thread_id"]
            prof = ForumProfile(
                profile_id="sync-retry-prof",
                name="Sync Retry Forum",
                guild_id=gid,
                forum_id=fid,
                purpose="external_project",
                catalog_enabled=True,
                external_drafts_enabled=True,
                stage_enabled=True,
                db_path=str(db_path),
                transfer_root=str(root / "unused_transfer"),
                exchange_dir=str(ex_dir),
            )
            upsert_forum_profile(prof, profiles_path)

            store = ForumStore(db_path)
            try:
                _seed_mirror_for_draft(store, draft_a)
            finally:
                store.close()

            ev_bytes = b"SYNC-RETRY-PNG"
            draft_a["evidence"][0]["sha256"] = compute_bytes_sha256(ev_bytes)
            bundle_a = ex_dir / "bundles" / canonical_record_digest(draft_a)
            ev_path_a = bundle_a / "transfer" / draft_a["evidence"][0]["local_ref"]
            ev_path_a.parent.mkdir(parents=True, exist_ok=True)
            ev_path_a.write_bytes(ev_bytes)
            write_record_atomic(bundle_a / "draft.request.json", draft_a)

            fail_obs = ComposerStageObservation(
                status="needs_review",
                observed_actor={"user_id": scope["actor"]["user_id"], "label": "DemoUser"},
                destination=dict(draft_a["destination"]),
                observed_composer_text=draft_a["body"],
                staged_evidence_ids=[],
                reason="attachment_upload_interrupted",
            )
            ok_obs = ComposerStageObservation(
                status="staged",
                observed_actor={"user_id": scope["actor"]["user_id"], "label": "DemoUser"},
                destination=dict(draft_a["destination"]),
                observed_composer_text=draft_a["body"],
                staged_evidence_ids=[ev["evidence_id"] for ev in draft_a["evidence"]],
                reason="staged_verified",
            )

            class FakeSyncDesktopProvider:
                stage_calls = 0

                def describe_session(self) -> SessionDescription:
                    return SessionDescription(
                        actor={"user_id": scope["actor"]["user_id"], "label": "DemoUser"},
                        app_name="Discord",
                        window_title="Discord",
                        window_bbox=(0, 0, 1000, 700),
                        current_url=draft_a["destination"]["source_url"],
                        capabilities={"can_read_semantic": True, "can_stage_composer": True},
                    )

                def describe_forum(self, guild_id: str, forum_id: str) -> ForumObservation:
                    return ForumObservation(
                        forum_id=forum_id,
                        guild_id=guild_id,
                        name="CLI Bundle Forum",
                        source_url=build_discord_channel_url(guild_id, forum_id),
                        guidelines_text="",
                        tags=[],
                    )

                def list_topics(self, guild_id: str, forum_id: str, cursor: Optional[str] = None) -> TopicListObservation:
                    return TopicListObservation(
                        topics=[TopicObservation(forum_id=forum_id, thread_id=tid, title="North Ridge Depot", source_url=draft_a["destination"]["source_url"])],
                        next_cursor=None,
                        complete=True,
                    )

                def read_requirements(self, guild_id: str, forum_id: str, references: Optional[list[dict[str, Any]]] = None) -> list[RequirementObservation]:
                    return []

                def read_topic(self, guild_id: str, forum_id: str, thread_id: str, cursor: Optional[str] = None) -> TopicHistoryObservation:
                    return TopicHistoryObservation(
                        topic=TopicObservation(forum_id=forum_id, thread_id=thread_id, title="North Ridge Depot", source_url=draft_a["destination"]["source_url"]),
                        messages=[],
                        complete=True,
                    )

                def stage_draft(self, *_args: Any, **_kwargs: Any) -> ComposerStageObservation:
                    FakeSyncDesktopProvider.stage_calls += 1
                    return fail_obs if FakeSyncDesktopProvider.stage_calls == 1 else ok_obs

            with patch.object(forums_cli, "DesktopDiscordProvider", FakeSyncDesktopProvider):
                # 1. First sync stages into transient needs_review + active review lock
                out1 = io.StringIO()
                with redirect_stdout(out1):
                    rc1 = forums_cli.main(["--profiles-path", str(profiles_path), "sync", "--profile", prof.profile_id, "--provider", "desktop", "--stage-pending"])
                self.assertEqual(rc1, 0)
                res1 = json.loads(out1.getvalue())
                self.assertEqual(res1["staged"][0]["status"], "needs_review")
                self.assertEqual(FakeSyncDesktopProvider.stage_calls, 1)

                # 2. While review lock is still held, sync skips retrying
                out2 = io.StringIO()
                with redirect_stdout(out2):
                    rc2 = forums_cli.main(["--profiles-path", str(profiles_path), "sync", "--profile", prof.profile_id, "--provider", "desktop", "--stage-pending"])
                self.assertEqual(rc2, 0)
                self.assertEqual(json.loads(out2.getvalue())["staged"], [])
                self.assertEqual(FakeSyncDesktopProvider.stage_calls, 1)

                # 3. After release_review(), sync retries the transient needs_review draft cleanly
                ctrl = SessionController(Path(db_path).with_suffix(".session.json"))
                ctrl.release_review()
                out3 = io.StringIO()
                with redirect_stdout(out3):
                    rc3 = forums_cli.main(["--profiles-path", str(profiles_path), "sync", "--profile", prof.profile_id, "--provider", "desktop", "--stage-pending"])
                self.assertEqual(rc3, 0)
                res3 = json.loads(out3.getvalue())
                self.assertEqual(res3["staged"][0]["status"], "staged")
                self.assertEqual(FakeSyncDesktopProvider.stage_calls, 2)

                # 4. A needs_review draft flagged with needs_recheck=1 stays skipped even after release_review()
                draft_b = copy.deepcopy(draft_a)
                draft_b["draft_id"] = "draft-recheck-blocked"
                draft_b["record_id"] = "rec-recheck-blocked"
                bundle_b = ex_dir / "bundles" / canonical_record_digest(draft_b)
                ev_path_b = bundle_b / "transfer" / draft_b["evidence"][0]["local_ref"]
                ev_path_b.parent.mkdir(parents=True, exist_ok=True)
                ev_path_b.write_bytes(ev_bytes)
                write_record_atomic(bundle_b / "draft.request.json", draft_b)

                store2 = ForumStore(db_path)
                try:
                    store2.save_draft(draft_b)
                    store2.update_draft_status(
                        draft_b["draft_id"],
                        int(draft_b["revision"]),
                        "needs_review",
                        needs_recheck=True,
                        recheck_reason="requirement_changed",
                    )
                finally:
                    store2.close()

                out4 = io.StringIO()
                with redirect_stdout(out4):
                    rc4 = forums_cli.main(["--profiles-path", str(profiles_path), "sync", "--profile", prof.profile_id, "--provider", "desktop", "--stage-pending"])
                self.assertEqual(rc4, 0)
                self.assertEqual(json.loads(out4.getvalue())["staged"], [])
                self.assertEqual(FakeSyncDesktopProvider.stage_calls, 2)

    def test_export_publication_failures_preserve_checkpoint_and_cutover_files(self) -> None:
        from src.forums import store as store_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "mirror.sqlite3"
            ex_dir = root / "exchange"
            store = ForumStore(db_path)
            try:
                store.ensure_scope(GUILD_ID, FORUM_ID, snapshot_id="snap-rec-1")
                store.upsert_forum(forum_id=FORUM_ID, name="Recovery Forum v1")

                # 1. Bootstrap failure leaves checkpoint at 0/0 with pending outbox item and no file
                with patch.object(store_mod, "write_record_atomic", side_effect=OSError("simulated disk failure")):
                    with self.assertRaises(OSError):
                        store.export_exchange_dir(ex_dir)
                chk0 = store.exported_checkpoint()
                self.assertEqual(chk0["exported_snapshot_revision"], 0)
                self.assertEqual(chk0["cursor"]["position"], "0")
                self.assertEqual(chk0["pending_outbox_count"], 1)
                self.assertEqual(list((ex_dir / "snapshots").glob("*.json")), [])
                boot_outbox_row = store._conn.execute("SELECT record_id FROM export_outbox").fetchone()
                self.assertIsNotNone(boot_outbox_row)

                # Quiet retry recovers bootstrap with identical record_id
                boot = store.export_exchange_dir(ex_dir)
                self.assertEqual(boot["mode"], "bootstrap_snapshot")
                boot_rec = read_record(boot["snapshot"])
                self.assertEqual(boot_rec["record_id"], boot_outbox_row["record_id"])
                chk1 = store.exported_checkpoint()
                self.assertEqual(chk1["exported_snapshot_revision"], 1)
                self.assertEqual(chk1["cursor"]["position"], "1")
                self.assertEqual(chk1["pending_outbox_count"], 0)

                # 2. Delta failure leaves exported_cursor_seq un-advanced; retry does NOT become quiet_noop
                store.upsert_forum(forum_id=FORUM_ID, name="Recovery Forum v2")
                with patch.object(store_mod, "write_record_atomic", side_effect=OSError("simulated delta write failure")):
                    with self.assertRaises(OSError):
                        store.export_exchange_dir(ex_dir)
                chk2 = store.exported_checkpoint()
                self.assertEqual(chk2["cursor"]["position"], "1")
                self.assertEqual(chk2["pending_outbox_count"], 1)
                self.assertEqual(list((ex_dir / "changes").glob("*.json")), [])
                delta_outbox_row = store._conn.execute("SELECT record_id FROM export_outbox").fetchone()
                self.assertIsNotNone(delta_outbox_row)

                inc = store.export_exchange_dir(ex_dir)
                self.assertEqual(inc["mode"], "incremental_changes")
                self.assertEqual(inc["change_count"], 1)
                inc_rec = read_record(inc["changes"])
                self.assertEqual(inc_rec["record_id"], delta_outbox_row["record_id"])
                self.assertEqual(store.exported_checkpoint()["cursor"]["position"], "2")

                # 3. Cutover failure keeps last usable snapshot & pending deltas intact (nothing archived early)
                with patch.object(store_mod, "write_record_atomic", side_effect=OSError("simulated cutover failure")):
                    with self.assertRaises(OSError):
                        store.export_exchange_dir(ex_dir, force_snapshot=True)
                chk_cut_fail = store.exported_checkpoint()
                self.assertEqual(chk_cut_fail["exported_snapshot_revision"], 1)
                self.assertEqual(chk_cut_fail["snapshot_revision"], 1)
                self.assertEqual(chk_cut_fail["pending_outbox_count"], 1)
                self.assertEqual(len(list((ex_dir / "snapshots").glob("*.json"))), 1)
                self.assertEqual(len(list((ex_dir / "changes").glob("*.json"))), 1)
                self.assertEqual(list((ex_dir / "archive" / "changes").glob("*.json")), [])
                cut_outbox_row = store._conn.execute("SELECT record_id FROM export_outbox").fetchone()

                # Retry completes cutover with identical record_id and archives superseded delta afterwards
                cut = store.export_exchange_dir(ex_dir, force_snapshot=True)
                self.assertEqual(cut["mode"], "cutover_snapshot")
                cut_rec = read_record(cut["snapshot"])
                self.assertEqual(cut_rec["record_id"], cut_outbox_row["record_id"])
                self.assertEqual(cut_rec["revision"], 2)
                self.assertEqual(store.exported_checkpoint()["exported_snapshot_revision"], 2)
                self.assertEqual(store.exported_checkpoint()["pending_outbox_count"], 0)
                self.assertEqual(list((ex_dir / "changes").glob("*.json")), [])
                self.assertEqual(len(list((ex_dir / "archive" / "changes").glob("*.json"))), 1)
            finally:
                store.close()

    def test_export_recovery_across_restarts_for_write_rename_and_post_publish_failures(self) -> None:
        import builtins
        from src.forums import exchange as exchange_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "mirror.sqlite3"
            ex_dir = root / "exchange"

            store = ForumStore(db_path)
            try:
                store.ensure_scope(GUILD_ID, FORUM_ID, snapshot_id="snap-restart")
                store.upsert_forum(forum_id=FORUM_ID, name="Restart Recovery Forum")
                store.export_exchange_dir(ex_dir)

                # A. Failure before/during temp file write across restart
                store.upsert_topic(thread_id=THREAD_1, forum_id=FORUM_ID, title="Topic PreWrite")
                real_open = builtins.open

                def failing_tmp_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
                    if ".changes.v1.json" in str(file) and str(file).endswith(".tmp") and "w" in mode:
                        raise OSError("simulated pre-write failure")
                    return real_open(file, mode, *args, **kwargs)

                with patch.object(builtins, "open", side_effect=failing_tmp_open):
                    with self.assertRaises(OSError):
                        store.export_exchange_dir(ex_dir)
                out_a = store._conn.execute("SELECT record_id, record_json FROM export_outbox").fetchone()
                self.assertIsNotNone(out_a)
                self.assertEqual(list((ex_dir / "changes").iterdir()), [])
            finally:
                store.close()

            store = ForumStore(db_path)
            try:
                res_a = store.export_exchange_dir(ex_dir)
                self.assertEqual(res_a["mode"], "incremental_changes")
                rec_a = read_record(res_a["changes"])
                self.assertEqual(rec_a["record_id"], out_a["record_id"])
                self.assertEqual(rec_a, json.loads(out_a["record_json"]))
                self.assertEqual(store.exported_checkpoint()["pending_outbox_count"], 0)

                # B. Failure during atomic rename (os.replace) cleans .tmp and recovers across restart
                store.upsert_message(
                    entity_ref="msg:710000000000000001",
                    message_id="710000000000000001",
                    thread_id=THREAD_1,
                    author={"user_id": None, "label": "Tester"},
                    body="Rename fault test message",
                )
                with patch.object(exchange_mod.os, "replace", side_effect=OSError("simulated atomic rename failure")):
                    with self.assertRaises(OSError):
                        store.export_exchange_dir(ex_dir)
                self.assertEqual(
                    [p.name for p in (ex_dir / "changes").iterdir() if p.name.startswith(".")],
                    [],
                )
                out_b = store._conn.execute("SELECT record_id, rel_path, record_json FROM export_outbox").fetchone()
                self.assertIsNotNone(out_b)
                self.assertFalse((ex_dir / out_b["rel_path"]).exists())
            finally:
                store.close()

            store = ForumStore(db_path)
            try:
                res_b = store.export_exchange_dir(ex_dir)
                self.assertEqual(res_b["mode"], "incremental_changes")
                rec_b = read_record(res_b["changes"])
                self.assertEqual(rec_b["record_id"], out_b["record_id"])
                self.assertEqual(rec_b, json.loads(out_b["record_json"]))

                # C. Failure AFTER file is written on disk / BEFORE SQLite checkpoint commit
                store.upsert_requirement(
                    requirement_id="guidance",
                    forum_id=FORUM_ID,
                    body="Requirement v1 before post-write crash",
                )
                with patch.object(store, "_commit_outbox_checkpoint", side_effect=RuntimeError("simulated crash before commit")):
                    with self.assertRaises(RuntimeError):
                        store.export_exchange_dir(ex_dir)
                out_c = store._conn.execute("SELECT record_id, rel_path FROM export_outbox").fetchone()
                self.assertIsNotNone(out_c)
                published_path = ex_dir / out_c["rel_path"]
                self.assertTrue(published_path.exists())
                disk_bytes_before = published_path.read_bytes()
                self.assertEqual(store.exported_checkpoint()["pending_outbox_count"], 1)
            finally:
                store.close()

            # Restarted process recovers checkpoint cleanly with identical bytes and no duplicate file
            store = ForumStore(db_path)
            try:
                files_before = sorted(p.name for p in (ex_dir / "changes").glob("*.json"))
                res_c = store.export_exchange_dir(ex_dir)
                files_after = sorted(p.name for p in (ex_dir / "changes").glob("*.json"))
                self.assertEqual(files_before, files_after)
                self.assertEqual(res_c["mode"], "incremental_changes")
                self.assertEqual(Path(res_c["changes"]).read_bytes(), disk_bytes_before)
                self.assertEqual(read_record(res_c["changes"])["record_id"], out_c["record_id"])
                self.assertEqual(store.exported_checkpoint()["pending_outbox_count"], 0)
            finally:
                store.close()

    def test_export_fault_recovery_with_strict_consumer(self) -> None:
        import hashlib
        from src.forums import exchange as exchange_mod
        from src.forums import store as store_mod

        class _StrictConsumer:
            """Strict v1 exchange consumer enforcing immutable records and contiguous cursors."""

            def __init__(self, exchange_dir: Path, expected_scope: dict[str, str]) -> None:
                self.snapshots_dir = exchange_dir / "snapshots"
                self.changes_dir = exchange_dir / "changes"
                self.expected_scope = dict(expected_scope)
                self.processed_inodes: dict[str, int] = {}
                self.record_hashes: dict[str, str] = {}
                self.applied_change_ids: set[str] = set()
                self.snapshot_id: str | None = None
                self.snapshot_revision = 0
                self.cursor: dict[str, str] | None = None
                self.requirements: dict[str, dict[str, Any]] = {}
                self.messages: dict[str, dict[str, Any]] = {}

            @staticmethod
            def _canonical_hash(record: dict[str, Any]) -> str:
                raw = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                return hashlib.sha256(raw).hexdigest()

            def _apply_change_item(self, item: dict[str, Any]) -> None:
                etype = item["entity_type"]
                eref = item["entity_ref"]
                if item["kind"] == "removal_observed":
                    if etype == "requirement":
                        self.requirements.pop(eref, None)
                    elif etype == "message":
                        self.messages.pop(eref, None)
                    return
                after = copy.deepcopy(item["after"])
                if etype == "requirement":
                    self.requirements[after["requirement_id"]] = after
                elif etype == "message":
                    self.messages[after["entity_ref"]] = after

            def _ingest_record(self, path: Path) -> bool:
                record = read_record(path)
                validate_exchange_record(record)
                scope = record.get("scope") or {}
                for key, expected_val in self.expected_scope.items():
                    if scope.get(key) != expected_val:
                        raise ContractValidationError(f"Scope mismatch for {key}: {scope.get(key)} != {expected_val}")

                record_id = record["record_id"]
                payload_hash = self._canonical_hash(record)
                previous_hash = self.record_hashes.get(record_id)
                if previous_hash is not None:
                    if previous_hash != payload_hash:
                        raise RecordConflictError(f"Conflicting payload for record_id {record_id}")
                    return False

                schema = record["schema"]
                if schema == "forum.snapshot.v1":
                    rev = int(record["revision"])
                    if rev < self.snapshot_revision:
                        raise ContractValidationError(f"Regressing snapshot revision {rev} < {self.snapshot_revision}")
                    self.snapshot_id = record["snapshot_id"]
                    self.snapshot_revision = rev
                    self.cursor = dict(record["cursor"])
                    self.requirements = {req["requirement_id"]: copy.deepcopy(req) for req in record["requirements"]}
                    self.messages = {msg["entity_ref"]: copy.deepcopy(msg) for msg in record["messages"]}
                elif schema == "forum.changes.v1":
                    if self.snapshot_id is None:
                        raise ContractValidationError("Cannot apply changes before initial snapshot")
                    if record["snapshot_id"] != self.snapshot_id:
                        raise ContractValidationError(
                            f"Snapshot id mismatch: {record['snapshot_id']} != {self.snapshot_id}"
                        )
                    if int(record["snapshot_revision"]) != self.snapshot_revision:
                        raise ContractValidationError(
                            f"Snapshot revision mismatch: {record['snapshot_revision']} != {self.snapshot_revision}"
                        )
                    if record["after_cursor"] != self.cursor:
                        raise ContractValidationError(
                            f"Non-contiguous cursor: after_cursor={record['after_cursor']} != {self.cursor}"
                        )
                    for item in record["changes"]:
                        cid = item["change_id"]
                        if cid in self.applied_change_ids:
                            raise ContractValidationError(f"Duplicate change_id {cid}")
                        self.applied_change_ids.add(cid)
                        self._apply_change_item(item)
                    self.cursor = dict(record["cursor"])
                else:
                    raise ContractValidationError(f"Unexpected schema {schema}")

                self.record_hashes[record_id] = payload_hash
                return True

            def process_pending(self) -> list[dict[str, Any]]:
                candidates: list[Path] = []
                for directory in (self.snapshots_dir, self.changes_dir):
                    if directory.is_dir():
                        candidates.extend(sorted(p for p in directory.glob("*.json") if p.is_file()))
                results: list[dict[str, Any]] = []
                for path in candidates:
                    ino = path.stat().st_ino
                    key = str(path)
                    if self.processed_inodes.get(key) == ino:
                        continue
                    applied = self._ingest_record(path)
                    self.processed_inodes[key] = ino
                    results.append({"path": key, "applied": applied})
                return results

        actor = {"user_id": "100000000000000099", "label": "Fictional reviewer"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "mirror.sqlite3"
            ex_dir = root / "exchange"
            consumer = _StrictConsumer(ex_dir, expected_scope={"guild_id": GUILD_ID, "forum_id": FORUM_ID})

            # 1. Bootstrap export & strict intake
            pub = ForumStore(db_path)
            try:
                pub.ensure_scope(GUILD_ID, FORUM_ID, actor=actor, snapshot_id="snap-strict-rec")
                pub.upsert_forum(forum_id=FORUM_ID, name="Fictional Forum v1")
                pub.upsert_topic(thread_id=THREAD_1, forum_id=FORUM_ID, title="Fictional Outpost")
                pub.upsert_requirement(
                    requirement_id="guidance",
                    forum_id=FORUM_ID,
                    body="Req rev 1: report observed status.",
                )
                boot = pub.export_exchange_dir(ex_dir)
                self.assertEqual(boot["mode"], "bootstrap_snapshot")
                self.assertEqual([r["applied"] for r in consumer.process_pending()], [True])

                # 2. Exact reproduction: forum update -> write failure -> normal retry (never quiet_noop)
                pub.upsert_forum(forum_id=FORUM_ID, name="Fictional Forum v2")
                with patch.object(store_mod, "write_record_atomic", side_effect=OSError("simulated disk failure")):
                    with self.assertRaises(OSError):
                        pub.export_exchange_dir(ex_dir)
                self.assertEqual(consumer.process_pending(), [])

                retry_forum = pub.export_exchange_dir(ex_dir)
                self.assertEqual(retry_forum["mode"], "incremental_changes")
                self.assertEqual(retry_forum["change_count"], 1)
                self.assertEqual([r["applied"] for r in consumer.process_pending()], [True])
                self.assertEqual(consumer.snapshot_revision, 1)

                # 3. Requirement edit fails during atomic rename across restart, then recovers without cutover
                pub.upsert_requirement(
                    requirement_id="guidance",
                    forum_id=FORUM_ID,
                    body="Req rev 2: include coordinates and screenshot.",
                )
                with patch.object(exchange_mod.os, "replace", side_effect=OSError("simulated rename failure")):
                    with self.assertRaises(OSError):
                        pub.export_exchange_dir(ex_dir)
                self.assertEqual(consumer.process_pending(), [])
            finally:
                pub.close()

            pub = ForumStore(db_path)
            try:
                retry_req = pub.export_exchange_dir(ex_dir)
                self.assertEqual(retry_req["mode"], "incremental_changes")
                self.assertEqual([r["applied"] for r in consumer.process_pending()], [True])
                req_item = consumer.requirements["guidance"]
                self.assertEqual(req_item["revision"], 2)
                self.assertIn("coordinates", req_item["body"])

                # 4. Post-write / pre-commit crash where strict consumer ingests BEFORE producer restarts
                pub.upsert_message(
                    entity_ref="msg:900000000000000001",
                    message_id="900000000000000001",
                    thread_id=THREAD_1,
                    author=actor,
                    body="Ordinary update during post-write crash window.",
                )
                with patch.object(pub, "_commit_outbox_checkpoint", side_effect=RuntimeError("crash before SQLite commit")):
                    with self.assertRaises(RuntimeError):
                        pub.export_exchange_dir(ex_dir)
                self.assertEqual([r["applied"] for r in consumer.process_pending()], [True])
            finally:
                pub.close()

            # Producer restarts, recovers checkpoint with identical record, then delivers requirement rev 3
            pub = ForumStore(db_path)
            try:
                recovered = pub.export_exchange_dir(ex_dir)
                self.assertEqual(recovered["mode"], "incremental_changes")
                self.assertEqual([r["applied"] for r in consumer.process_pending()], [False])

                pub.upsert_requirement(
                    requirement_id="guidance",
                    forum_id=FORUM_ID,
                    body="Req rev 3: attach annotated crop.",
                )
                req3 = pub.export_exchange_dir(ex_dir)
                self.assertEqual(req3["mode"], "incremental_changes")
                self.assertEqual([r["applied"] for r in consumer.process_pending()], [True])
                req3_item = consumer.requirements["guidance"]
                self.assertEqual(req3_item["revision"], 3)
                self.assertIn("annotated crop", req3_item["body"])
                self.assertEqual(len(consumer.messages), 1)
                self.assertEqual(consumer.snapshot_revision, 1)

                # 5. Cutover failure preserves existing files; retry completes cutover
                with patch.object(store_mod, "write_record_atomic", side_effect=OSError("simulated cutover failure")):
                    with self.assertRaises(OSError):
                        pub.export_exchange_dir(ex_dir, force_snapshot=True)
                self.assertEqual(consumer.process_pending(), [])
                cut_ok = pub.export_exchange_dir(ex_dir, force_snapshot=True)
                self.assertEqual(cut_ok["mode"], "cutover_snapshot")
                self.assertEqual([r["applied"] for r in consumer.process_pending()], [True])
                self.assertEqual(consumer.snapshot_revision, 2)
            finally:
                pub.close()


if __name__ == "__main__":
    unittest.main()
