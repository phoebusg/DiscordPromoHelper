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
    canonical_record_digest,
    resolve_evidence_files,
    validate_exchange_record,
    write_record_atomic,
)
from .models import compute_content_hash, parse_rfc3339_utc, utc_now_iso
from .providers.base import DiscordViewProvider
from .store import ForumStore


class BundleValidationError(ContractValidationError):
    """Raised when an incoming selected-file draft bundle fails integrity or layout checks."""


def ingest_draft_bundle(
    bundle_dir: str | Path,
    *,
    require_hash_dirname: bool = True,
) -> tuple[dict[str, Any], Path]:
    """Validate an immutable <draft-record-hash>/ bundle and its transfer/ evidence files."""
    bdir = Path(bundle_dir)
    if not bdir.exists() or bdir.is_symlink() or not bdir.is_dir():
        raise BundleValidationError(f"Bundle directory missing or invalid: {bdir}")

    for child in bdir.iterdir():
        if child.is_symlink():
            raise BundleValidationError(f"Bundle must not contain symlinks: {child}")
        if child.name not in {"draft.request.json", "transfer"}:
            raise BundleValidationError(
                f"Unexpected entry in draft bundle {bdir.name!r}: {child.name!r}"
            )

    req_path = bdir / "draft.request.json"
    if not req_path.exists() or req_path.is_symlink() or not req_path.is_file():
        raise BundleValidationError(f"Bundle missing draft.request.json: {req_path}")

    draft = json.loads(req_path.read_text(encoding="utf-8"))
    validate_exchange_record(draft)
    if draft.get("schema") != "draft.request.v1":
        raise BundleValidationError(f"Expected draft.request.v1 inside bundle, got {draft.get('schema')!r}")

    expected_digest = canonical_record_digest(draft)
    if require_hash_dirname:
        if bdir.name != expected_digest:
            raise BundleValidationError(
                f"Bundle directory name {bdir.name!r} does not match canonical draft digest {expected_digest!r}"
            )

    transfer_dir = bdir / "transfer"
    evidence = draft.get("evidence") or []
    if evidence and (not transfer_dir.exists() or transfer_dir.is_symlink() or not transfer_dir.is_dir()):
        raise BundleValidationError(f"Bundle with evidence must include transfer/ directory: {transfer_dir}")

    if transfer_dir.exists():
        if transfer_dir.is_symlink() or not transfer_dir.is_dir():
            raise BundleValidationError(f"Bundle transfer path must be a real directory: {transfer_dir}")
        actual_files: set[Path] = set()
        for entry in transfer_dir.rglob("*"):
            if entry.is_symlink():
                raise BundleValidationError(f"Bundle transfer/ must not contain symlinks: {entry}")
            if entry.is_file():
                actual_files.add(entry.resolve())
        resolved = resolve_evidence_files(evidence, transfer_dir)
        expected_files = {Path(item["resolved_path"]).resolve() for item in resolved}
        if actual_files != expected_files:
            raise BundleValidationError(
                "Bundle transfer/ directory contains unexpected files not selected by draft.evidence"
            )
    elif evidence:
        raise BundleValidationError("Bundle requires transfer/ directory for selected evidence")

    return draft, transfer_dir


def _requires_discord_review_hold(stage_obs: Any) -> bool:
    if getattr(stage_obs, "status", None) == "staged":
        return True
    reason = str(getattr(stage_obs, "reason", None) or "")
    if reason == "existing_human_draft_present":
        return True
    obs_text = getattr(stage_obs, "observed_composer_text", None)
    if isinstance(obs_text, str) and obs_text.strip():
        return True
    if getattr(stage_obs, "staged_evidence_ids", None):
        return True
    return reason.startswith(
        (
            "composer_text_verification_mismatch",
            "attachment_preview_unverified",
            "missing_resolved_attachment",
            "composer_text_changed_after_attachments",
            "composer_unverified_after_attachments",
            "composer_focus_failed",
        )
    )


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

    def transition_staging_to_review(
        self,
        draft_id: str,
        draft_revision: int,
        reason: str = "staged_for_review",
        *,
        baseline_latest_message_id: Optional[str] = None,
        preexisting_message_ids: Optional[list[str]] = None,
    ) -> None:
        """Atomically transition staging ownership directly into durable in_discord_review."""
        with self._locked_state_file():
            if self._state_path is not None:
                st = self._read_disk_state_unlocked()
                self._mode = st["mode"]
                self._owner_pid = st["owner_pid"]
                self._review_draft = st["review_draft"]
            info: dict[str, Any] = {
                "draft_id": draft_id,
                "draft_revision": int(draft_revision),
                "entered_at": utc_now_iso(),
                "reason": reason,
            }
            if baseline_latest_message_id is not None:
                info["baseline_latest_message_id"] = baseline_latest_message_id
            if preexisting_message_ids:
                info["preexisting_message_ids"] = list(preexisting_message_ids)
            self._commit_state_unlocked("in_discord_review", os.getpid(), info)

    def enter_discord_review(
        self,
        draft_id: str,
        draft_revision: int,
        reason: str = "staged_for_review",
    ) -> None:
        self.transition_staging_to_review(draft_id, draft_revision, reason=reason)

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

    def stage_draft_bundle(
        self,
        bundle_dir: str | Path,
        profile: ForumProfile,
        *,
        source_kind: str = "external",
        expected_actor: Optional[dict[str, Any]] = None,
        require_hash_dirname: bool = True,
    ) -> dict[str, Any]:
        """Validate an immutable <draft-record-hash>/ selected-file bundle and stage it."""
        draft, transfer_dir = ingest_draft_bundle(
            bundle_dir,
            require_hash_dirname=require_hash_dirname,
        )
        return self.stage_draft_request(
            draft,
            profile,
            source_kind=source_kind,
            expected_actor=expected_actor,
            transfer_root=transfer_dir,
        )

    def _baseline_thread_messages(self, thread_id: Optional[str]) -> tuple[list[str], Optional[str]]:
        if not thread_id:
            return [], None
        try:
            snap = self.store.export_snapshot()
        except Exception:
            return [], None
        thread_mids = [
            str(m["message_id"])
            for m in snap.get("messages", [])
            if m.get("thread_id") == thread_id and m.get("message_id")
        ]
        numeric_mids = [int(mid) for mid in thread_mids if mid.isdecimal()]
        latest = str(max(numeric_mids)) if numeric_mids else None
        return thread_mids, latest

    def stage_draft_request(
        self,
        draft_record: dict[str, Any],
        profile: ForumProfile,
        *,
        source_kind: str = "external",
        expected_actor: Optional[dict[str, Any]] = None,
        transfer_root: Optional[str | Path] = None,
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
        self.store.ingest_record(draft_record)

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
                draft_id, draft_rev, "needs_review"
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

        # Resolve and SHA-256 verify evidence files inside effective transfer_root
        effective_transfer_root = transfer_root if transfer_root is not None else profile.transfer_root
        try:
            resolved_evidence = resolve_evidence_files(draft_record["evidence"], effective_transfer_root)
        except Exception as exc:
            self.store.update_draft_status(draft_id, draft_rev, "failed")
            return self._make_receipt(
                draft_record=draft_record,
                actor=obs_actor,
                status="failed",
                reason=f"evidence_resolution_failed: {exc}",
                exchange_dir=profile.exchange_dir,
            )

        preexisting_mids, baseline_latest_mid = self._baseline_thread_messages(
            draft_record["destination"].get("thread_id")
        )

        if not self.session.acquire_for_staging():
            cur_mode = self.session.status()["mode"]
            busy_reason = (
                "session_busy_in_review"
                if cur_mode == "in_discord_review"
                else f"session_busy_{cur_mode}"
            )
            self.store.update_draft_status(
                draft_id, draft_rev, "needs_review"
            )
            return self._make_receipt(
                draft_record=draft_record,
                actor=obs_actor,
                status="needs_review",
                reason=busy_reason,
                exchange_dir=profile.exchange_dir,
            )

        transitioned = False
        try:
            stage_obs = self.provider.stage_draft(
                draft_record,
                resolved_evidence,
                expected_actor=expected_actor or scope.get("actor"),
            )
            if _requires_discord_review_hold(stage_obs):
                self.session.transition_staging_to_review(
                    draft_id,
                    draft_rev,
                    reason=stage_obs.reason
                    or ("staged_for_review" if stage_obs.status == "staged" else "partial_composer_state"),
                    baseline_latest_message_id=baseline_latest_mid,
                    preexisting_message_ids=preexisting_mids,
                )
            else:
                self.session.release_staging()
            transitioned = True
        except Exception as exc:
            self.session.transition_staging_to_review(
                draft_id,
                draft_rev,
                reason=f"staging_exception:{type(exc).__name__}",
                baseline_latest_message_id=baseline_latest_mid,
                preexisting_message_ids=preexisting_mids,
            )
            transitioned = True
            raise
        finally:
            if not transitioned and self.session.status()["mode"] == "staging":
                self.session.release_staging()

        if stage_obs.status == "staged":
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

        self.store.update_draft_status(
            draft_id,
            draft_rev,
            stage_obs.status,
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
        prior_observed = next(
            (r for r in reversed(prior_receipts) if r.get("status") == "submission_observed"),
            None,
        )
        prior_confirmed = next(
            (r for r in reversed(prior_receipts) if r.get("status") == "user_confirmed_submitted"),
            None,
        )
        latest_receipt = dict(prior_receipts[-1]) if prior_receipts else {}
        review_info = self.session.active_review_info() or {}
        if review_info.get("draft_id") == draft_id and int(review_info.get("draft_revision") or -1) == rev:
            if review_info.get("baseline_latest_message_id") and not latest_receipt.get("baseline_latest_message_id"):
                latest_receipt["baseline_latest_message_id"] = review_info["baseline_latest_message_id"]
            if review_info.get("preexisting_message_ids") and not latest_receipt.get("_preexisting_message_ids"):
                latest_receipt["_preexisting_message_ids"] = list(review_info["preexisting_message_ids"])
        # Also exclude mirror messages recorded at or before draft creation/staging
        try:
            dest_tid = draft_record["destination"].get("thread_id")
            if dest_tid:
                snap = self.store.export_snapshot(
                    guild_id=draft_record["scope"]["guild_id"],
                    forum_id=draft_record["scope"]["forum_id"],
                )
                pre_ids = set(latest_receipt.get("_preexisting_message_ids") or [])
                cutoff_dt = parse_rfc3339_utc(row.get("staged_at")) or parse_rfc3339_utc(draft_record.get("created_at"))
                draft_created_dt = parse_rfc3339_utc(draft_record.get("created_at"))
                for m in snap.get("messages", []):
                    if m.get("thread_id") != dest_tid or not m.get("message_id"):
                        continue
                    r_created_dt = parse_rfc3339_utc(m.get("remote_created_at"))
                    obs_dt = parse_rfc3339_utc(m.get("observed_at"))
                    if (
                        (r_created_dt and draft_created_dt and r_created_dt < draft_created_dt)
                        or (obs_dt and cutoff_dt and obs_dt < cutoff_dt)
                    ):
                        pre_ids.add(str(m["message_id"]))
                if pre_ids:
                    latest_receipt["_preexisting_message_ids"] = sorted(pre_ids)
        except Exception:
            pass
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

        if not user_confirmed and prior_observed is not None:
            return prior_observed

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

        if prior_confirmed is not None:
            self.session.release_review()
            return prior_confirmed

        # Ambiguous/unconfirmed submission stays unresolved without duplicate staging
        return self._make_receipt(
            draft_record=draft_record,
            actor=sub_obs.observed_actor,
            status="in_discord_review" if self.session.is_review_active() else "needs_review",
            evidence_ids=latest_receipt.get("evidence_ids", []),
            reason=sub_obs.reason or "submission_not_confirmed",
            exchange_dir=ex_dir,
        )
