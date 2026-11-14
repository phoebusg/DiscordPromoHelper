"""Session serialization lock, reviewed composer staging, and submission reconciliation."""
from __future__ import annotations

import threading
from typing import Any, Optional

from src import storage

from .changes import make_record_id
from .config import ForumProfile
from .drafts import check_draft_conflicts
from .exchange import (
    ContractValidationError,
    resolve_evidence_files,
    validate_exchange_record,
)
from .models import compute_content_hash, utc_now_iso
from .providers.base import DiscordViewProvider
from .store import ForumStore


class SessionController:
    """Coordinates exclusive Discord UI navigation between reading and human-reviewed staging."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._mode: str = "idle"  # idle | reading | staging | in_discord_review
        self._review_draft: Optional[dict[str, Any]] = None

    def can_navigate(self) -> bool:
        """Return True iff background/CLI forum reads may navigate Discord."""
        with self._lock:
            return self._mode in ("idle", "reading")

    def is_review_active(self) -> bool:
        with self._lock:
            return self._mode == "in_discord_review"

    def active_review_info(self) -> Optional[dict[str, Any]]:
        with self._lock:
            return dict(self._review_draft) if self._review_draft else None

    def acquire_for_read(self) -> bool:
        with self._lock:
            if self._mode in ("staging", "in_discord_review"):
                return False
            self._mode = "reading"
            return True

    def release_read(self) -> None:
        with self._lock:
            if self._mode == "reading":
                self._mode = "idle"

    def acquire_for_staging(self) -> bool:
        with self._lock:
            if self._mode == "in_discord_review":
                return False
            self._mode = "staging"
            return True

    def release_staging(self) -> None:
        with self._lock:
            if self._mode == "staging":
                self._mode = "idle"

    def enter_discord_review(self, draft_id: str, draft_revision: int) -> None:
        with self._lock:
            self._mode = "in_discord_review"
            self._review_draft = {
                "draft_id": draft_id,
                "draft_revision": int(draft_revision),
                "entered_at": utc_now_iso(),
            }

    def release_review(self) -> None:
        with self._lock:
            self._review_draft = None
            if self._mode == "in_discord_review":
                self._mode = "idle"


class HandoffManager:
    """Validates external/promotion draft requests, stages safely without Send, and reconciles submissions."""

    def __init__(
        self,
        store: ForumStore,
        provider: DiscordViewProvider,
        session_controller: Optional[SessionController] = None,
        cooldown_path: Optional[str] = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.session = session_controller or SessionController()
        self.cooldown_path = cooldown_path

    def _make_receipt(
        self,
        *,
        draft_record: dict[str, Any],
        actor: dict[str, Any],
        status: str,
        message_id: Optional[str] = None,
        message_url: Optional[str] = None,
        observed_body_hash: Optional[str] = None,
        evidence_ids: Optional[list[str]] = None,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        receipt = {
            "schema": "handoff.receipt.v1",
            "record_id": make_record_id("rcptrec"),
            "producer_id": "discord-promo-helper",
            "created_at": utc_now_iso(),
            "scope": {
                "guild_id": draft_record["scope"]["guild_id"],
                "forum_id": draft_record["scope"]["forum_id"],
                "actor": actor,
            },
            "receipt_id": make_record_id("rcpt"),
            "draft_id": draft_record["draft_id"],
            "draft_revision": int(draft_record["revision"]),
            "destination": dict(draft_record["destination"]),
            "status": status,
            "observed_at": utc_now_iso(),
            "message_id": message_id,
            "message_url": message_url,
            "observed_body_hash": observed_body_hash,
            "evidence_ids": list(evidence_ids or []),
            "reason": reason,
        }
        validate_exchange_record(receipt)
        self.store.save_receipt(receipt)
        return receipt

    def stage_draft_request(
        self,
        draft_record: dict[str, Any],
        profile: ForumProfile,
        *,
        source_kind: str = "external",
        expected_actor: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Stage a validated draft.request.v1 into Discord without pressing Send."""
        validate_exchange_record(draft_record)
        if draft_record["schema"] != "draft.request.v1":
            raise ContractValidationError("Expected draft.request.v1 record")

        scope = draft_record["scope"]
        if scope["guild_id"] != profile.guild_id or scope["forum_id"] != profile.forum_id:
            raise ContractValidationError(
                f"Draft scope ({scope['guild_id']}/{scope['forum_id']}) does not match profile ({profile.guild_id}/{profile.forum_id})"
            )

        self.store.ensure_scope(profile.guild_id, profile.forum_id)
        self.store.save_draft(draft_record)

        session_desc = self.provider.describe_session()
        obs_actor = session_desc.actor
        draft_id = draft_record["draft_id"]
        draft_rev = int(draft_record["revision"])

        # Check enablement flags independently
        if not profile.can_stage(source_kind):
            self.store.update_draft_status(draft_id, draft_rev, "failed")
            return self._make_receipt(
                draft_record=draft_record,
                actor=obs_actor,
                status="failed",
                reason=f"staging_disabled_for_profile_or_source (purpose={profile.purpose}, source={source_kind}, stage_enabled={profile.stage_enabled})",
            )

        # Idempotency check: never duplicate-stage a submitted or already-staged revision
        prior_receipts = self.store.list_receipts(draft_id=draft_id)
        for pr in reversed(prior_receipts):
            if pr["status"] in ("submission_observed", "user_confirmed_submitted"):
                return self._make_receipt(
                    draft_record=draft_record,
                    actor=obs_actor,
                    status=pr["status"],
                    message_id=pr.get("message_id"),
                    message_url=pr.get("message_url"),
                    observed_body_hash=pr.get("observed_body_hash"),
                    evidence_ids=pr.get("evidence_ids", []),
                    reason="already_submitted_no_duplicate_staging",
                )
            if int(pr["draft_revision"]) == draft_rev and pr["status"] in ("staged", "in_discord_review"):
                return self._make_receipt(
                    draft_record=draft_record,
                    actor=obs_actor,
                    status="in_discord_review",
                    observed_body_hash=pr.get("observed_body_hash"),
                    evidence_ids=pr.get("evidence_ids", []),
                    reason="already_staged_in_review",
                )

        # Preserve human edits if composer review is already active (e.g. new draft revision arrives)
        if self.session.is_review_active():
            self.store.update_draft_status(
                draft_id, draft_rev, "needs_review", needs_recheck=True, recheck_reason="composer_review_in_progress"
            )
            return self._make_receipt(
                draft_record=draft_record,
                actor=obs_actor,
                status="needs_review",
                reason="composer_review_in_progress",
            )

        # Promotion cadence cooldown check (promotion profiles only)
        if source_kind == "promotion" and profile.can_promote():
            dest_tid = draft_record["destination"]["thread_id"] or profile.forum_id
            cooldown_key = f"forum:{profile.guild_id}:{dest_tid}"
            cooldown_mins = int(profile.cooldown_hours * 60)
            if not storage.can_post(cooldown_key, cooldown_minutes=cooldown_mins, path=self.cooldown_path):
                return self._make_receipt(
                    draft_record=draft_record,
                    actor=obs_actor,
                    status="needs_review",
                    reason=f"cooldown_active_for_{cooldown_key}",
                )

        # Baseline / requirements conflict check
        conflicts = check_draft_conflicts(draft_record, self.store)
        if conflicts:
            reason = "; ".join(conflicts)
            self.store.update_draft_status(
                draft_id, draft_rev, "needs_review", needs_recheck=True, recheck_reason=reason
            )
            return self._make_receipt(
                draft_record=draft_record,
                actor=obs_actor,
                status="needs_review",
                reason=reason,
            )

        # Resolve and SHA-256 verify evidence files inside transfer_root
        try:
            resolved_evidence = resolve_evidence_files(draft_record["evidence"], profile.transfer_root)
        except Exception as exc:
            self.store.update_draft_status(draft_id, draft_rev, "failed")
            return self._make_receipt(
                draft_record=draft_record,
                actor=obs_actor,
                status="failed",
                reason=f"evidence_resolution_failed: {exc}",
            )

        if not self.session.acquire_for_staging():
            return self._make_receipt(
                draft_record=draft_record,
                actor=obs_actor,
                status="needs_review",
                reason="session_busy_in_review",
            )

        try:
            stage_obs = self.provider.stage_draft(
                draft_record,
                resolved_evidence,
                expected_actor=expected_actor or scope.get("actor"),
            )
        finally:
            if self.session._mode == "staging":
                self.session.release_staging()

        if stage_obs.status == "staged":
            self.session.enter_discord_review(draft_id, draft_rev)
            self.store.update_draft_status(draft_id, draft_rev, "staged", staged_at=utc_now_iso())
            obs_hash = (
                compute_content_hash(stage_obs.observed_composer_text)
                if stage_obs.observed_composer_text
                else None
            )
            return self._make_receipt(
                draft_record=draft_record,
                actor=stage_obs.observed_actor,
                status="staged",
                observed_body_hash=obs_hash,
                evidence_ids=stage_obs.staged_evidence_ids,
                reason=stage_obs.reason,
            )

        self.store.update_draft_status(
            draft_id,
            draft_rev,
            stage_obs.status,
            needs_recheck=(stage_obs.status == "needs_review"),
            recheck_reason=stage_obs.reason,
        )
        return self._make_receipt(
            draft_record=draft_record,
            actor=stage_obs.observed_actor,
            status=stage_obs.status,
            evidence_ids=stage_obs.staged_evidence_ids,
            reason=stage_obs.reason,
        )

    def reconcile_submission(
        self,
        draft_id: str,
        *,
        draft_revision: Optional[int] = None,
        profile: Optional[ForumProfile] = None,
        user_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Observe later submission (including human-edited text) and release review lock."""
        import json

        row = self.store.get_draft_row(draft_id, draft_revision)
        if row is None:
            raise KeyError(f"Unknown draft_id={draft_id!r}, revision={draft_revision!r}")
        draft_record = json.loads(row["record_json"])
        rev = int(row["revision"])
        prior_receipts = [
            r for r in self.store.list_receipts(draft_id=draft_id) if int(r["draft_revision"]) == rev
        ]
        latest_receipt = prior_receipts[-1] if prior_receipts else {}

        session_desc = self.provider.describe_session()
        if user_confirmed:
            self.session.release_review()
            self.store.update_draft_status(draft_id, rev, "user_confirmed_submitted")
            if profile and profile.can_promote():
                dest_tid = draft_record["destination"]["thread_id"] or profile.forum_id
                storage.update_timestamp(
                    f"forum:{profile.guild_id}:{dest_tid}", path=self.cooldown_path
                )
            return self._make_receipt(
                draft_record=draft_record,
                actor=session_desc.actor,
                status="user_confirmed_submitted",
                evidence_ids=[ev["evidence_id"] for ev in draft_record.get("evidence", [])],
                reason="user_confirmed",
            )

        sub_obs = self.provider.observe_submission(draft_record, latest_receipt)
        if sub_obs.status == "submission_observed" and sub_obs.message_id:
            obs_hash = (
                compute_content_hash(sub_obs.observed_body)
                if sub_obs.observed_body is not None
                else None
            )
            self.session.release_review()
            self.store.update_draft_status(
                draft_id, rev, "submission_observed", submitted_message_id=sub_obs.message_id
            )
            if profile and profile.can_promote():
                dest_tid = draft_record["destination"]["thread_id"] or profile.forum_id
                storage.update_timestamp(
                    f"forum:{profile.guild_id}:{dest_tid}", path=self.cooldown_path
                )
            return self._make_receipt(
                draft_record=draft_record,
                actor=sub_obs.observed_actor,
                status="submission_observed",
                message_id=sub_obs.message_id,
                message_url=sub_obs.message_url,
                observed_body_hash=obs_hash,
                evidence_ids=[ev["evidence_id"] for ev in draft_record.get("evidence", [])],
                reason=sub_obs.reason,
            )

        # Ambiguous/unconfirmed submission stays unresolved without duplicate staging
        return self._make_receipt(
            draft_record=draft_record,
            actor=sub_obs.observed_actor,
            status="in_discord_review" if self.session.is_review_active() else "needs_review",
            evidence_ids=latest_receipt.get("evidence_ids", []),
            reason=sub_obs.reason or "submission_not_confirmed",
        )
