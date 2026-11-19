"""Active-destination refresh must not scan unrelated topics or corrupt their coverage."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.forums.providers.base import MessageObservation, TopicHistoryObservation, TopicObservation
from src.forums.reader import ForumReader
from src.forums.store import ForumStore
from tests.test_forums import FakeForumProvider, FORUM_ID, THREAD_1, THREAD_2, _make_profile


class TestTargetRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = _make_profile(Path(self.tmp.name))
        self.store = ForumStore(self.profile.db_path)
        self.addCleanup(self.store.close)
        self.store.ensure_scope(self.profile.guild_id, FORUM_ID)
        self.topic = TopicObservation(thread_id=THREAD_1, forum_id=FORUM_ID, title="Example Depot")
        self.store.upsert_topic(thread_id=THREAD_1, forum_id=FORUM_ID, title=self.topic.title)
        self.store.upsert_topic(thread_id=THREAD_2, forum_id=FORUM_ID, title="Other Depot")
        self.provider = FakeForumProvider()
        self.provider.histories[THREAD_1] = TopicHistoryObservation(
            topic=self.topic, messages=[MessageObservation(
                thread_id=THREAD_1, author={"label": "Reviewer"}, body="New evidence",
                message_id="100000000000000105", entity_ref="msg:100000000000000105",
            )], complete=True,
        )
        self.store.update_coverage(
            topics="partial", histories="partial", requirements="partial",
            pending_known=[{"thread_id": THREAD_1, "reason": "history_unread", "resume_token": f"thread:{THREAD_1}"},
                           {"thread_id": THREAD_2, "reason": "history_unread", "resume_token": f"thread:{THREAD_2}"}],
            errors=[{"thread_id": THREAD_1, "code": "history_unread", "message": "History unread"},
                    {"thread_id": THREAD_2, "code": "history_unread", "message": "History unread"}],
            last_success_at="2026-11-14T10:00:00Z",
        )
        self.reader = ForumReader(self.profile, self.store, self.provider)

    def test_reads_target_and_guidance_without_directory_scan_or_global_freshness_claim(self):
        with patch.object(self.provider, "list_topics", side_effect=AssertionError("directory scan")):
            result = self.reader.refresh_topic(THREAD_1)
        self.assertTrue(result["topic_complete"])
        self.assertTrue(result["requirements_complete"])
        self.assertEqual(self.store.get_topic_status(THREAD_1)["message_count"], 1)
        self.assertIsNotNone(self.store.get_topic_status(THREAD_1)["last_deep_read_at"])
        self.assertIsNone(self.store.get_topic_status(THREAD_2)["last_deep_read_at"])
        cov = result["coverage"]
        self.assertEqual(cov["topics"], "partial")
        self.assertEqual(cov["histories"], "partial")
        self.assertEqual(cov["last_success_at"], "2026-11-14T10:00:00Z")
        self.assertEqual([p["thread_id"] for p in cov["pending"]], [THREAD_2])
        self.assertEqual([e["thread_id"] for e in cov["errors"]], [THREAD_2])

    def test_incomplete_target_remains_pending_and_unrelated_work_survives(self):
        self.provider.histories[THREAD_1].complete = False
        result = self.reader.refresh_topic(THREAD_1)
        self.assertFalse(result["topic_complete"])
        self.assertIsNone(self.store.get_topic_status(THREAD_1)["last_deep_read_at"])
        self.assertEqual({p["thread_id"] for p in result["coverage"]["pending"]}, {THREAD_1, THREAD_2})

    def test_wrong_destination_or_foreign_message_is_not_ingested(self):
        original = copy.deepcopy(self.provider.histories[THREAD_1])
        for variant in ("thread", "forum", "message"):
            with self.subTest(variant=variant):
                history = copy.deepcopy(original)
                if variant == "thread":
                    history.topic.thread_id = THREAD_2
                elif variant == "forum":
                    history.topic.forum_id = "100000000000000008"
                else:
                    history.messages[0].thread_id = THREAD_2
                self.provider.histories[THREAD_1] = history
                result = self.reader.refresh_topic(THREAD_1)
                self.assertFalse(result["topic_complete"])
                self.assertEqual(result["errors"][0]["code"], "history_target_mismatch")
                self.assertEqual(self.store.get_topic_status(THREAD_1)["message_count"], 0)
                self.assertIsNone(self.store.get_topic_status(THREAD_1)["last_deep_read_at"])

    def test_review_disabled_and_unknown_target_do_not_touch_provider(self):
        with patch.object(self.provider, "describe_session", side_effect=AssertionError("UI access")):
            self.reader.session.enter_discord_review("draft-in-review", 1)
            self.assertEqual(self.reader.refresh_topic(THREAD_1)["reason"], "session_in_review_or_staging")
            self.reader.session.release_review()
            self.profile.catalog_enabled = False
            self.assertEqual(self.reader.refresh_topic(THREAD_1)["reason"], "catalog_disabled")
            self.profile.catalog_enabled = True
            self.assertEqual(self.reader.refresh_topic("100000000000000009")["reason"],
                             "topic_not_in_configured_mirror")

    def test_refresh_cli_selects_target_and_rejects_backfill_combination(self):
        from src.forums.__main__ import build_parser, cmd_refresh
        args = build_parser().parse_args(["refresh", "--profile", "fixture", "--thread", THREAD_1,
                                          "--backfill"])
        with patch("src.forums.__main__._load_profile_or_exit", side_effect=AssertionError("load")):
            self.assertEqual(cmd_refresh(args), 2)

    def test_cli_exports_target_and_reports_incomplete_input_as_nonzero(self):
        from src.forums.__main__ import build_parser, cmd_refresh
        args = build_parser().parse_args([
            "refresh", "--profile", "fixture", "--thread", THREAD_1, "--provider", "seed", "--export",
        ])
        for complete in (True, False):
            with self.subTest(complete=complete):
                self.provider.histories[THREAD_1].complete = complete
                output = io.StringIO()
                with patch("src.forums.__main__._load_profile_or_exit", return_value=self.profile), \
                     patch("src.forums.__main__._select_provider", return_value=(self.provider, False)), \
                     redirect_stdout(output):
                    self.assertEqual(cmd_refresh(args), 0 if complete else 2)
                result = json.loads(output.getvalue())
                self.assertEqual(result["result"]["thread_id"], THREAD_1)
                self.assertEqual(result["result"]["topic_complete"], complete)
                if complete:
                    self.assertTrue(Path(result["exported"]["snapshot"]).is_file())

    def test_paged_target_refresh_detects_silent_edit_without_directory_scan(self):
        history = self.provider.histories[THREAD_1]
        self.reader.refresh_topic(THREAD_1)
        cursor = self.store.current_cursor()
        first = TopicHistoryObservation(topic=self.topic, next_cursor="page-two", complete=False)
        final = copy.deepcopy(history)
        final.messages[0].body = "Edited evidence without new activity"
        with patch.object(self.provider, "list_topics", side_effect=AssertionError("directory scan")), \
             patch.object(self.provider, "read_topic", side_effect=[first, final]) as read:
            result = self.reader.refresh_topic(THREAD_1)
        self.assertTrue(result["topic_complete"])
        self.assertEqual(read.call_count, 2)
        changes = self.store.export_changes(after_cursor=cursor)["changes"]
        self.assertTrue(any(c["entity_type"] == "message" and c["after"]["body"] == final.messages[0].body
                            for c in changes))

    def test_stored_requirement_refs_legacy_migration_and_status_fields(self):
        from src.forums.models import build_discord_channel_url
        from src.forums.providers.base import RequirementObservation

        guide_tid = "100000000000000099"
        guide_url = build_discord_channel_url(self.profile.guild_id, guide_tid)
        self.store.upsert_requirement(
            requirement_id="poi-guidance-custom",
            forum_id=FORUM_ID,
            body="Guidelines body",
            source_entity_ref=f"thread:{guide_tid}",
            source_url=guide_url,
        )
        # Stale session-level (thread_id=None) error alongside unrelated per-thread error
        self.store.update_coverage(
            topics="partial",
            histories="partial",
            requirements="partial",
            pending_known=[
                {"thread_id": THREAD_1, "reason": "history_unread", "resume_token": f"thread:{THREAD_1}"},
                {"thread_id": THREAD_2, "reason": "history_unread", "resume_token": f"thread:{THREAD_2}"},
            ],
            errors=[
                {"thread_id": None, "code": "session_stale", "message": "Transient error"},
                {"thread_id": THREAD_2, "code": "history_unread", "message": "History unread"},
            ],
        )
        cursor_before = self.store.current_cursor()
        self.assertEqual(
            self.store.get_requirement_references(FORUM_ID),
            [{"requirement_id": "poi-guidance-custom", "thread_id": guide_tid, "source_entity_ref": None}],
        )

        # Auto-detected requirement ID reusing same source_url deduplicates against existing ID
        self.provider.histories[THREAD_1].topic.starter_message_id = "100000000000000105"
        self.provider.histories[THREAD_1].messages.append(
            MessageObservation(
                thread_id=THREAD_1,
                author={"label": "Responder"},
                body="Follow-up reply",
                message_id="100000000000000106",
                entity_ref="msg:100000000000000106",
                attachments=[{"attachment_id": None, "name": "crop.png", "source_url": None, "sha256": None, "local_ref": None}],
            )
        )
        seen_refs = []

        def _fake_read_reqs(_gid, _fid, references=None):
            seen_refs.append(references)
            return [
                RequirementObservation(
                    requirement_id=f"forum-guidance-{guide_tid}",
                    forum_id=FORUM_ID,
                    body="Guidelines body",
                    source_entity_ref=None,
                    source_url=guide_url,
                    complete=True,
                )
            ]

        with patch.object(self.provider, "list_topics", side_effect=AssertionError("directory scan")), \
             patch.object(self.provider, "read_requirements", side_effect=_fake_read_reqs):
            res = self.reader.refresh_topic(THREAD_1)

        self.assertTrue(res["topic_complete"])
        self.assertTrue(res["requirements_complete"])
        self.assertEqual(seen_refs[0][0]["requirement_id"], "poi-guidance-custom")
        self.assertEqual(res["coverage"]["errors"], [{"thread_id": THREAD_2, "code": "history_unread", "message": "History unread"}])

        changes = self.store.export_changes(after_cursor=cursor_before)["changes"]
        req_chg = [c for c in changes if c["entity_type"] == "requirement" and c["entity_ref"] == "poi-guidance-custom"]
        self.assertEqual(len(req_chg), 1)
        self.assertEqual((req_chg[0]["before_revision"], req_chg[0]["after_revision"]), (1, 2))
        self.assertIsNone(req_chg[0]["after"]["source_entity_ref"])

        t1_status = self.store.get_topic_status(THREAD_1)
        self.assertEqual(t1_status["starter_message_id"], "100000000000000105")
        self.assertEqual(t1_status["message_count"], 2)
        self.assertEqual(t1_status["reply_count"], 1)
        self.assertTrue(t1_status["has_non_starter_replies"])
        self.assertTrue(t1_status["history_complete"])
        self.assertIsNone(t1_status["pending_reason"])
        self.assertIn("content_hash", t1_status["latest_messages"][1])
        self.assertEqual(t1_status["latest_messages"][1]["attachments"][0]["name"], "crop.png")

        t2_status = self.store.get_topic_status(THREAD_2)
        self.assertFalse(t2_status["history_complete"])
        self.assertEqual(t2_status["pending_reason"], "history_unread")

        self.assertEqual(seen_refs[0], [{"requirement_id": "poi-guidance-custom", "thread_id": guide_tid, "source_entity_ref": None}])

        f_status = self.store.get_forum_status()
        self.assertEqual(f_status["counts"]["requirements"], 2)
        reqs_by_id = {r["requirement_id"]: r for r in self.store.export_snapshot()["requirements"]}
        self.assertIn(f"topic-starter:{THREAD_1}", reqs_by_id)
        self.assertEqual(reqs_by_id[f"topic-starter:{THREAD_1}"]["source_entity_ref"], "msg:100000000000000105")
        self.assertEqual(f_status["counts"]["topics_deep_read"], 1)
        self.assertEqual(f_status["counts"]["topics_pending_history"], 1)
        self.assertIn("exported_checkpoint", f_status)
