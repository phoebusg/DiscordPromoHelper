"""Session serialization lock, reviewed composer staging, and submission reconciliation."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

from src import storage

from .changes import make_record_id
from .config import ForumProfile
from .drafts import check_draft_conflicts
from .exchange import (
    ContractValidationError,
    resolve_evidence_files,
    validate_exchange_record,
    write_record_atomic,
)
from .models import compute_content_hash, utc_now_iso
from .providers.base import DiscordViewProvider
from .store import ForumStore


def _is_pid_alive(pid: Optional[int]) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if psutil is not None:
        try:
            return bool(psutil.pid_exists(pid))
        except Exception:
            pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class SessionController:
    """Coordinates exclusive Discord UI navigation across threads and separate CLI processes."""

    def __init__(self, state_path: Optional[str | Path] = None) -> None:
        self._lock = threading.RLock()
        self._state_path = Path(state_path) if state_path else None
        self._lock_path = Path(f"{self._state_path}.lock") if self._state_path else None
        self._mode: str = "idle"  # idle | reading | staging | in_discord_review
        self._owner_pid: Optional[int] = None
        self._review_draft: Optional[dict[str, Any]] = None
        if self._state_path is not None:
            self._sync_from_disk()

    @contextmanager
    def _locked_state_file(self) -> Iterator[None]:
        with self._lock:
            if self._lock_path is None:
                yield
                return
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._lock_path, "a+", encoding="utf-8") as lock_fh:
                if fcntl is not None:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    def _read_disk_state_unlocked(self) -> dict[str, Any]:
        default_state: dict[str, Any] = {
            "mode": "idle",
            "owner_pid": None,
            "updated_at": utc_now_iso(),
            "review_draft": None,
        }
        if self._state_path is None or not self._state_path.exists():
            return default_state
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return default_state
            mode = raw.get("mode", "idle")
            owner_pid = raw.get("owner_pid")
            review_draft = raw.get("review_draft")
            # Transient reading/staging locks auto-recover if owning CLI process died.
            # Durable in_discord_review NEVER auto-expires across process exit/restart.
            if (
                mode in ("reading", "staging")
                and owner_pid is not None
                and owner_pid != os.getpid()
                and not _is_pid_alive(owner_pid)
            ):
                repaired = {
                    "mode": "idle",
                    "owner_pid": None,
                    "updated_at": utc_now_iso(),
                    "review_draft": None,
                }
                self._write_disk_state_unlocked(repaired)
                return repaired
            return {
                "mode": mode,
                "owner_pid": owner_pid,
                "updated_at": raw.get("updated_at"),
                "review_draft": review_draft if isinstance(review_draft, dict) else None,
            }
        except Exception:
            return default_state

    def _write_disk_state_unlocked(self, state: dict[str, Any]) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.parent / f".{self._state_path.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self._state_path)

    def _sync_from_disk(self) -> dict[str, Any]:
        with self._locked_state_file():
            if self._state_path is None:
                return {
                    "mode": self._mode,
                    "owner_pid": self._owner_pid,
                    "review_draft": dict(self._review_draft) if self._review_draft else None,
                }
            st = self._read_disk_state_unlocked()
            self._mode = st["mode"]
            self._owner_pid = st["owner_pid"]
            self._review_draft = st["review_draft"]
            return st

    def _commit_state_unlocked(self, mode: str, owner_pid: Optional[int], review_draft: Optional[dict[str, Any]]) -> None:
        self._mode = mode
        self._owner_pid = owner_pid
        self._review_draft = review_draft
        if self._state_path is not None:
            self._write_disk_state_unlocked(
                {
                    "mode": mode,
                    "owner_pid": owner_pid,
                    "updated_at": utc_now_iso(),
                    "review_draft": review_draft,
                }
            )

    def status(self) -> dict[str, Any]:
        """Return current synchronized UI ownership state."""
        return dict(self._sync_from_disk())

    def can_navigate(self) -> bool:
        """Return True iff background/CLI forum reads may navigate Discord."""
        with self._locked_state_file():
            if self._state_path is not None:
                st = self._read_disk_state_unlocked()
                self._mode = st["mode"]
                self._owner_pid = st["owner_pid"]
                self._review_draft = st["review_draft"]
            return self._mode in ("idle", "reading")

    def is_review_active(self) -> bool:
        with self._locked_state_file():
            if self._state_path is not None:
                st = self._read_disk_state_unlocked()
                self._mode = st["mode"]
                self._owner_pid = st["owner_pid"]
                self._review_draft = st["review_draft"]
            return self._mode == "in_discord_review"

    def active_review_info(self) -> Optional[dict[str, Any]]:
        with self._locked_state_file():
            if self._state_path is not None:
                st = self._read_disk_state_unlocked()
                self._mode = st["mode"]
                self._owner_pid = st["owner_pid"]
                self._review_draft = st["review_draft"]
            return dict(self._review_draft) if self._review_draft else None

    def acquire_for_read(self) -> bool:
        with self._locked_state_file():
            if self._state_path is not None:
                st = self._read_disk_state_unlocked()
                self._mode = st["mode"]
                self._owner_pid = st["owner_pid"]
                self._review_draft = st["review_draft"]
            if self._mode in ("staging", "in_discord_review"):
                return False
            if self._mode == "reading" and self._owner_pid not in (None, os.getpid()):
                return False
            self._commit_state_unlocked("reading", os.getpid(), None)
            return True

    def release_read(self) -> None:
        with self._locked_state_file():
            if self._state_path is not None:
                st = self._read_disk_state_unlocked()
                self._mode = st["mode"]
                self._owner_pid = st["owner_pid"]
                self._review_draft = st["review_draft"]
            if self._mode == "reading" and self._owner_pid in (None, os.getpid()):
                self._commit_state_unlocked("idle", None, None)

    def acquire_for_staging(self, wait_seconds: float = 0.0) -> bool:
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            with self._locked_state_file():
                if self._state_path is not None:
                    st = self._read_disk_state_unlocked()
                    self._mode = st["mode"]
                    self._owner_pid = st["owner_pid"]
                    self._review_draft = st["review_draft"]
                if self._mode == "idle":
                    self._commit_state_unlocked("staging", os.getpid(), None)
                    return True
                if self._mode in ("in_discord_review", "staging"):
                    return False
                # _mode == "reading": wait up to wait_seconds, or fail immediately if wait_seconds == 0
                if time.monotonic() >= deadline:
                    return False
            time.sleep(0.1)

    def release_staging(self) -> None:
        with self._locked_state_file():
            if self._state_path is not None:
                st = self._read_disk_state_unlocked()
                self._mode = st["mode"]
                self._owner_pid = st["owner_pid"]
                self._review_draft = st["review_draft"]
            if self._mode == "staging" and self._owner_pid in (None, os.getpid()):
                self._commit_state_unlocked("idle", None, None)

    def enter_discord_review(
        self,
        draft_id: str,
        draft_revision: int,
        reason: str = "staged_for_review",
    ) -> None:
        with self._locked_state_file():
            info = {
                "draft_id": draft_id,
                "draft_revision": int(draft_revision),
                "entered_at": utc_now_iso(),
                "reason": reason,
            }
            self._commit_state_unlocked("in_discord_review", os.getpid(), info)

    def release_review(self) -> None:
        with self._locked_state_file():
            if self._state_path is not None:
                st = self._read_disk_state_unlocked()
                self._mode = st["mode"]
                self._owner_pid = st["owner_pid"]
                self._review_draft = st["review_draft"]
            if self._mode == "in_discord_review":
                self._commit_state_unlocked("idle", None, None)


class HandoffManager:
    """Validates external/promotion draft requests, stages safely without Send, and reconciles submissions."""

    def __init__(
        self,
        store: ForumStore,
        provider: DiscordViewProvider,
        session_controller: Optional[SessionController] = None,
        cooldown_path: Optional[str] = None,
        exchange_dir: Optional[str | Path] = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.session = session_controller or SessionController()
        self.cooldown_path = cooldown_path
        self.exchange_dir = Path(exchange_dir) if exchange_dir else None

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
        exchange_dir: Optional[str | Path] = None,
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
        out_dir = Path(exchange_dir) if exchange_dir else self.exchange_dir
        if out_dir is not None:
            receipts_dir = out_dir / "receipts"
            receipts_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{receipt['draft_id']}-rev{receipt['draft_revision']}-{receipt['status']}-{receipt['record_id']}.receipt.v1.json"
            write_record_atomic(receipts_dir / fname, receipt)
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
                exchange_dir=profile.exchange_dir,
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
                    exchange_dir=profile.exchange_dir,
                )
            if int(pr["draft_revision"]) == draft_rev and pr["status"] in ("staged", "in_discord_review"):
                return self._make_receipt(
                    draft_record=draft_record,
                    actor=obs_actor,
                    status="in_discord_review",
                    observed_body_hash=pr.get("observed_body_hash"),
                    evidence_ids=pr.get("evidence_ids", []),
                    reason="already_staged_in_review",
                    exchange_dir=profile.exchange_dir,
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
                exchange_dir=profile.exchange_dir,
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
                    exchange_dir=profile.exchange_dir,
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
                exchange_dir=profile.exchange_dir,
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
                exchange_dir=profile.exchange_dir,
            )

        if not self.session.acquire_for_staging():
            cur_mode = self.session.status()["mode"]
            busy_reason = (
                "session_busy_in_review"
                if cur_mode == "in_discord_review"
                else f"session_busy_{cur_mode}"
            )
            self.store.update_draft_status(
                draft_id, draft_rev, "needs_review", needs_recheck=True, recheck_reason=busy_reason
            )
            return self._make_receipt(
                draft_record=draft_record,
                actor=obs_actor,
                status="needs_review",
                reason=busy_reason,
                exchange_dir=profile.exchange_dir,
            )

        try:
            stage_obs = self.provider.stage_draft(
                draft_record,
                resolved_evidence,
                expected_actor=expected_actor or scope.get("actor"),
            )
        finally:
            if self.session.status()["mode"] == "staging":
                self.session.release_staging()

        if stage_obs.status == "staged":
            self.session.enter_discord_review(draft_id, draft_rev, reason="staged_for_review")
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
                exchange_dir=profile.exchange_dir,
            )

        # Keep review pause engaged if human text or partially staged text is in the composer!
        if stage_obs.reason == "existing_human_draft_present" or (
            stage_obs.observed_composer_text and stage_obs.observed_composer_text.strip()
        ):
            self.session.enter_discord_review(
                draft_id,
                draft_rev,
                reason=stage_obs.reason or "partial_composer_state",
            )

        self.store.update_draft_status(
            draft_id,
            draft_rev,
            stage_obs.status,
            needs_recheck=(stage_obs.status == "needs_review"),
            recheck_reason=stage_obs.reason,
        )
        obs_hash = (
            compute_content_hash(stage_obs.observed_composer_text)
            if stage_obs.observed_composer_text
            else None
        )
        return self._make_receipt(
            draft_record=draft_record,
            actor=stage_obs.observed_actor,
            status=stage_obs.status,
            observed_body_hash=obs_hash,
            evidence_ids=stage_obs.staged_evidence_ids,
            reason=stage_obs.reason,
            exchange_dir=profile.exchange_dir,
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
        row = self.store.get_draft_row(draft_id, draft_revision)
        if row is None:
            raise KeyError(f"Unknown draft_id={draft_id!r}, revision={draft_revision!r}")
        draft_record = json.loads(row["record_json"])
        rev = int(row["revision"])
        prior_receipts = [
            r for r in self.store.list_receipts(draft_id=draft_id) if int(r["draft_revision"]) == rev
        ]
        latest_receipt = prior_receipts[-1] if prior_receipts else {}
        ex_dir = profile.exchange_dir if profile else None

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
                exchange_dir=ex_dir,
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
                exchange_dir=ex_dir,
            )

        # Ambiguous/unconfirmed submission stays unresolved without duplicate staging
        return self._make_receipt(
            draft_record=draft_record,
            actor=sub_obs.observed_actor,
            status="in_discord_review" if self.session.is_review_active() else "needs_review",
            evidence_ids=latest_receipt.get("evidence_ids", []),
            reason=sub_obs.reason or "submission_not_confirmed",
            exchange_dir=ex_dir,
        )
