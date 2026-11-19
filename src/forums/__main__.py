"""CLI entry point for configured forum retrieval, local queries, change feed, and staging."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Any, Optional

from .config import (
    DEFAULT_PROFILES_PATH,
    ForumProfile,
    get_forum_profile,
    upsert_forum_profile,
)
from .drafts import check_draft_conflicts
from .exchange import (
    ContractValidationError,
    RecordConflictError,
    read_record,
    validate_exchange_record,
    write_record_atomic,
)
from .handoff import BundleValidationError, HandoffManager, SessionController, ingest_draft_bundle
from .providers.base import SeededDiscordProvider, SessionDescription
from .providers.desktop import DesktopDiscordProvider
from .reader import ForumReader
from .store import ForumStore


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _load_profile_or_exit(profile_id: str, profiles_path: str) -> ForumProfile:
    prof = get_forum_profile(profile_id, profiles_path)
    if prof is None:
        print(f"Profile {profile_id!r} not found in {profiles_path}", file=sys.stderr)
        sys.exit(2)
    return prof


def _session_controller_for(prof: ForumProfile) -> SessionController:
    return SessionController(state_path=Path(prof.db_path).with_suffix(".session.json"))


def _seed_session_description(prof: ForumProfile) -> SessionDescription:
    return SessionDescription(
        actor={"user_id": None, "label": "Seed Provider"},
        app_name="SeedProvider",
        window_title=prof.name,
        window_bbox=None,
        current_url=prof.source_url,
        capabilities={
            "accessibility_api": False,
            "ocr_fallback": False,
            "can_read_semantic": False,
            "can_read_ocr": False,
            "can_stage_composer": False,
        },
        errors=[],
    )


def _select_provider(
    provider_mode: str,
    prof: ForumProfile,
    desktop: Optional[DesktopDiscordProvider],
    session_desc: Any,
) -> tuple[Any, bool]:
    use_seed = provider_mode == "seed" or (
        provider_mode == "auto"
        and not session_desc.capabilities.get("can_read_semantic")
        and not session_desc.capabilities.get("can_read_ocr")
        and prof.seed_index_path
        and Path(prof.seed_index_path).exists()
    )
    if use_seed:
        return (
            SeededDiscordProvider(
                guild_id=prof.guild_id,
                forum_id=prof.forum_id,
                forum_name=prof.name,
                seed_path=prof.seed_index_path,
            ),
            True,
        )
    if desktop is None:
        desktop = DesktopDiscordProvider()
    return desktop, False


def cmd_session(_args: argparse.Namespace) -> int:
    provider = DesktopDiscordProvider()
    desc = provider.describe_session()
    _print_json(asdict(desc))
    return 0


def cmd_init_profile(args: argparse.Namespace) -> int:
    prof = ForumProfile(
        profile_id=args.profile,
        name=args.name,
        guild_id=args.guild_id,
        forum_id=args.forum_id,
        purpose=args.purpose,
        catalog_enabled=args.catalog_enabled,
        promotion_enabled=args.promotion_enabled,
        external_drafts_enabled=args.external_drafts_enabled,
        stage_enabled=args.stage_enabled,
        routing_mode=args.routing_mode,
        default_thread_id=args.default_thread_id,
        seed_index_path=args.seed_index,
        transfer_root=args.transfer_root,
        db_path=args.db_path or f"data/forums/{args.profile}.sqlite3",
        exchange_dir=args.exchange_dir,
    )
    upsert_forum_profile(prof, args.profiles_path)
    _print_json(prof.to_dict())
    return 0


def cmd_seed_index(args: argparse.Namespace) -> int:
    prof = _load_profile_or_exit(args.profile, args.profiles_path)
    seed_file = args.seed_file or prof.seed_index_path
    if not seed_file:
        print("No --seed-file or profile.seed_index_path configured", file=sys.stderr)
        return 2
    store = ForumStore(prof.db_path)
    try:
        store.ensure_scope(prof.guild_id, prof.forum_id)
        if not prof.catalog_enabled:
            _print_json(
                {
                    "profile": prof.profile_id,
                    "result": {"paused": True, "reason": "catalog_disabled", "coverage": store.get_coverage()},
                    "status": store.get_forum_status(),
                }
            )
            return 2
        provider = SeededDiscordProvider(
            guild_id=prof.guild_id,
            forum_id=prof.forum_id,
            forum_name=prof.name,
            seed_path=seed_file,
        )
        reader = ForumReader(prof, store, provider)
        res = reader.initial_backfill(max_topics_per_run=args.max_topics)
        _print_json({"profile": prof.profile_id, "result": res, "status": store.get_forum_status()})
    finally:
        store.close()
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    if getattr(args, "thread", None) and args.backfill:
        print("--thread and --backfill cannot be combined", file=sys.stderr)
        return 2
    prof = _load_profile_or_exit(args.profile, args.profiles_path)
    desktop: Optional[DesktopDiscordProvider] = None
    if args.provider == "seed":
        session_desc = _seed_session_description(prof)
    else:
        desktop = DesktopDiscordProvider()
        session_desc = desktop.describe_session()
    store = ForumStore(prof.db_path)
    try:
        provider, use_seed = _select_provider(args.provider, prof, desktop, session_desc)
        ctrl = _session_controller_for(prof)
        reader = ForumReader(prof, store, provider, session_controller=ctrl)
        if getattr(args, "thread", None):
            res = reader.refresh_topic(args.thread)
        elif args.backfill:
            res = reader.initial_backfill(max_topics_per_run=args.max_topics)
        else:
            res = reader.incremental_refresh(
                deep_reread_count=args.deep_reread,
                max_topics_per_run=args.max_topics,
            )

        if use_seed and session_desc.errors:
            res["coverage"] = store.update_coverage(
                errors=session_desc.errors,
                keep_existing_errors=True,
            )

        exported: dict[str, Any] = {}
        if args.export and not res.get("paused"):
            exported = store.export_exchange_dir(
                prof.exchange_dir,
                force_snapshot=getattr(args, "snapshot", False),
            )

        _print_json(
            {
                "profile": prof.profile_id,
                "provider_used": (
                    "seed"
                    if args.provider == "seed"
                    else ("seed+desktop_probe" if use_seed else "desktop")
                ),
                "desktop_session": asdict(session_desc),
                "result": res,
                "status": store.get_forum_status(),
                "exported": exported,
            }
        )
    finally:
        store.close()
    if getattr(args, "thread", None):
        complete = (
            not res.get("paused")
            and res.get("topic_complete")
            and res.get("requirements_complete")
            and not res.get("errors")
        )
        return 0 if complete else 2
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    prof = _load_profile_or_exit(args.profile, args.profiles_path)
    store = ForumStore(prof.db_path)
    try:
        store.ensure_scope(prof.guild_id, prof.forum_id)
        if args.thread:
            st = store.get_topic_status(args.thread, since_position=args.since_position)
            if st is None:
                print(f"Thread {args.thread!r} not found in mirror", file=sys.stderr)
                return 1
            _print_json(st)
        else:
            _print_json(store.get_forum_status())
    finally:
        store.close()
    return 0


def cmd_topics(args: argparse.Namespace) -> int:
    prof = _load_profile_or_exit(args.profile, args.profiles_path)
    has_replies: Optional[bool] = None
    if getattr(args, "has_replies", False):
        has_replies = True
    elif getattr(args, "no_replies", False):
        has_replies = False
    store = ForumStore(prof.db_path)
    try:
        store.ensure_scope(prof.guild_id, prof.forum_id)
        res = store.list_topics(
            guild_id=prof.guild_id,
            forum_id=prof.forum_id,
            pending_only=bool(getattr(args, "pending_only", False)),
            deep_read_only=bool(getattr(args, "deep_read_only", False)),
            has_replies=has_replies,
            tag=getattr(args, "tag", None),
            limit=args.limit,
            offset=args.offset,
        )
        _print_json(res)
    finally:
        store.close()
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    prof = _load_profile_or_exit(args.profile, args.profiles_path)
    store = ForumStore(prof.db_path)
    try:
        store.ensure_scope(prof.guild_id, prof.forum_id)
        _print_json(store.search(args.query, forum_id=prof.forum_id))
    finally:
        store.close()
    return 0


def cmd_changes(args: argparse.Namespace) -> int:
    prof = _load_profile_or_exit(args.profile, args.profiles_path)
    store = ForumStore(prof.db_path)
    try:
        store.ensure_scope(prof.guild_id, prof.forum_id)
        after_cursor: Optional[dict[str, str]] = None
        if args.after_position is not None:
            stream_id = args.stream_id or store.current_cursor()["stream_id"]
            after_cursor = {"stream_id": stream_id, "position": args.after_position}
        batch = store.export_changes(after_cursor)
        if args.out:
            write_record_atomic(args.out, batch)
        _print_json(batch)
    finally:
        store.close()
    return 0


def cmd_stage(args: argparse.Namespace) -> int:
    prof = _load_profile_or_exit(args.profile, args.profiles_path)
    bundle_dir = getattr(args, "bundle_dir", None)
    draft_file = getattr(args, "draft_file", None)
    if not bundle_dir and not draft_file:
        print("Either --bundle-dir or --draft-file is required", file=sys.stderr)
        return 2
    store = ForumStore(prof.db_path)
    try:
        mgr = HandoffManager(
            store,
            DesktopDiscordProvider(),
            session_controller=_session_controller_for(prof),
            exchange_dir=prof.exchange_dir,
        )
        if bundle_dir:
            receipt = mgr.stage_draft_bundle(bundle_dir, prof, source_kind=args.source_kind)
        else:
            df = Path(draft_file)
            if df.is_dir():
                receipt = mgr.stage_draft_bundle(df, prof, source_kind=args.source_kind)
            elif df.name == "draft.request.json" and (
                (df.parent / "transfer").exists() or len(df.parent.name) == 64
            ):
                receipt = mgr.stage_draft_bundle(df.parent, prof, source_kind=args.source_kind)
            else:
                draft = read_record(df)
                receipt = mgr.stage_draft_request(draft, prof, source_kind=args.source_kind)
        if args.receipt_out:
            write_record_atomic(args.receipt_out, receipt)
        _print_json(receipt)
    except (BundleValidationError, ContractValidationError, RecordConflictError) as exc:
        print(f"Invalid draft bundle: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    prof = _load_profile_or_exit(args.profile, args.profiles_path)
    store = ForumStore(prof.db_path)
    try:
        mgr = HandoffManager(
            store,
            DesktopDiscordProvider(),
            session_controller=_session_controller_for(prof),
            exchange_dir=prof.exchange_dir,
        )
        receipt = mgr.reconcile_submission(
            args.draft_id,
            draft_revision=args.revision,
            profile=prof,
            user_confirmed=args.user_confirmed,
        )
        if args.receipt_out:
            write_record_atomic(args.receipt_out, receipt)
        _print_json(receipt)
    finally:
        store.close()
    return 0


def cmd_session_state(args: argparse.Namespace) -> int:
    prof = _load_profile_or_exit(args.profile, args.profiles_path)
    ctrl = _session_controller_for(prof)
    desktop = DesktopDiscordProvider()
    _print_json(
        {
            "profile": prof.profile_id,
            "state_path": str(Path(prof.db_path).with_suffix(".session.json")),
            "session_state": ctrl.status(),
            "desktop_session": asdict(desktop.describe_session()),
        }
    )
    return 0


def cmd_release_review(args: argparse.Namespace) -> int:
    prof = _load_profile_or_exit(args.profile, args.profiles_path)
    ctrl = _session_controller_for(prof)
    ctrl.release_review()
    _print_json(
        {
            "profile": prof.profile_id,
            "released": True,
            "session_state": ctrl.status(),
        }
    )
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    prof = _load_profile_or_exit(args.profile, args.profiles_path)
    desktop: Optional[DesktopDiscordProvider] = None
    if args.provider == "seed":
        session_desc = _seed_session_description(prof)
    else:
        desktop = DesktopDiscordProvider()
        session_desc = desktop.describe_session()
    store = ForumStore(prof.db_path)
    try:
        ctrl = _session_controller_for(prof)
        provider, use_seed = _select_provider(args.provider, prof, desktop, session_desc)
        reconciled: list[dict[str, Any]] = []
        if args.reconcile:
            if desktop is None:
                desktop = DesktopDiscordProvider()
            mgr = HandoffManager(
                store,
                desktop,
                session_controller=ctrl,
                exchange_dir=prof.exchange_dir,
            )
            latest_by_draft: dict[tuple[str, int], dict[str, Any]] = {}
            for rec in store.list_receipts():
                dest = rec.get("destination") or {}
                if dest.get("guild_id") == prof.guild_id and dest.get("forum_id") == prof.forum_id:
                    latest_by_draft[(rec["draft_id"], int(rec["draft_revision"]))] = rec
            for (did, rev), rec in latest_by_draft.items():
                if rec.get("status") in ("staged", "in_discord_review", "needs_review"):
                    reconciled.append(
                        mgr.reconcile_submission(did, draft_revision=rev, profile=prof)
                    )

        reader = ForumReader(prof, store, provider, session_controller=ctrl)
        refresh_res = reader.incremental_refresh(
            deep_reread_count=args.deep_reread,
            max_topics_per_run=args.max_topics,
        )

        exported: dict[str, Any] = {}
        if not refresh_res.get("paused"):
            exported = store.export_exchange_dir(
                prof.exchange_dir,
                force_snapshot=args.snapshot,
            )

        staged: list[dict[str, Any]] = []
        if args.stage_pending:
            if desktop is None:
                desktop = DesktopDiscordProvider()
            mgr = HandoffManager(
                store,
                desktop,
                session_controller=ctrl,
                exchange_dir=prof.exchange_dir,
            )
            ex_dir = Path(prof.exchange_dir)
            bundles_dir = ex_dir / "bundles"
            drafts_dir = ex_dir / "drafts"
            candidates: list[tuple[Path, bool]] = []
            if bundles_dir.is_dir():
                for b_entry in sorted(bundles_dir.iterdir()):
                    if b_entry.is_dir():
                        candidates.append((b_entry, True))
            if drafts_dir.is_dir():
                for d_entry in sorted(drafts_dir.iterdir()):
                    if d_entry.is_dir():
                        candidates.append((d_entry, True))
                    elif d_entry.is_file() and d_entry.suffix == ".json":
                        candidates.append((d_entry, False))

            for entry_path, is_bundle in candidates:
                transfer_root: Optional[Path] = None
                try:
                    if is_bundle:
                        draft, transfer_root = ingest_draft_bundle(entry_path)
                    else:
                        draft = read_record(entry_path)
                except (BundleValidationError, ContractValidationError, RecordConflictError) as exc:
                    staged.append(
                        {
                            "bundle": str(entry_path),
                            "status": "failed",
                            "reason": f"bundle_validation_failed: {exc}",
                        }
                    )
                    continue
                except Exception:
                    continue

                if draft.get("schema") != "draft.request.v1":
                    continue
                dest = draft.get("destination") or {}
                if dest.get("guild_id") != prof.guild_id or dest.get("forum_id") != prof.forum_id:
                    continue
                row = store.get_draft_row(draft["draft_id"], int(draft["revision"]))
                if row is not None and row["status"] in (
                    "staged",
                    "in_discord_review",
                    "submission_observed",
                    "user_confirmed_submitted",
                    "rejected",
                ):
                    continue
                if row is not None and row["status"] == "needs_review":
                    if (
                        int(row.get("needs_recheck") or 0) == 1
                        or ctrl.is_review_active()
                        or bool(check_draft_conflicts(draft, store))
                    ):
                        continue
                receipt = mgr.stage_draft_request(
                    draft,
                    prof,
                    source_kind=args.source_kind,
                    transfer_root=transfer_root,
                )
                staged.append(receipt)
                if receipt["status"] in ("staged", "in_discord_review", "needs_review"):
                    break

        _print_json(
            {
                "profile": prof.profile_id,
                "provider_used": (
                    "seed"
                    if args.provider == "seed"
                    else ("seed+desktop_probe" if use_seed else "desktop")
                ),
                "reconciled": reconciled,
                "refresh": refresh_res,
                "exported": exported,
                "staged": staged,
                "session_state": ctrl.status(),
            }
        )
    finally:
        store.close()
    return 0


def cmd_validate_contract(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    validated = validate_exchange_record(data)
    _print_json({"valid": True, "schema": validated["schema"], "record_id": validated["record_id"]})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.forums",
        description="Forum retrieval, local mirror, change feed, and reviewed composer staging",
    )
    parser.add_argument("--profiles-path", default=DEFAULT_PROFILES_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("session", help="Probe installed Discord desktop session & permissions")

    p_init = sub.add_parser("init-profile", help="Configure a forum profile")
    p_init.add_argument("--profile", required=True)
    p_init.add_argument("--name", required=True)
    p_init.add_argument("--guild-id", required=True)
    p_init.add_argument("--forum-id", required=True)
    p_init.add_argument("--purpose", choices=["promotion", "external_project"], default="external_project")
    p_init.add_argument("--catalog-enabled", action="store_true")
    p_init.add_argument("--promotion-enabled", action="store_true")
    p_init.add_argument("--external-drafts-enabled", action="store_true")
    p_init.add_argument("--stage-enabled", action="store_true")
    p_init.add_argument("--routing-mode", choices=["reply_existing", "one_topic_per_streamer", "create_topic"], default="reply_existing")
    p_init.add_argument("--default-thread-id")
    p_init.add_argument("--seed-index")
    p_init.add_argument("--transfer-root", default="data/forums/transfer")
    p_init.add_argument("--db-path")
    p_init.add_argument("--exchange-dir", default="data/forums/exchange")

    p_seed = sub.add_parser("seed-index", help="Import an index observation into the local mirror")
    p_seed.add_argument("--profile", required=True)
    p_seed.add_argument("--seed-file")
    p_seed.add_argument("--max-topics", type=int)

    p_ref = sub.add_parser("refresh", help="Run backfill or incremental refresh")
    p_ref.add_argument("--profile", required=True)
    p_ref.add_argument("--provider", choices=["auto", "desktop", "seed"], default="auto")
    p_ref.add_argument("--backfill", action="store_true")
    p_ref.add_argument("--thread", help="Refresh one already mirrored topic and guidance without a directory scan")
    p_ref.add_argument("--deep-reread", type=int, default=2)
    p_ref.add_argument("--max-topics", type=int)
    p_ref.add_argument("--export", action="store_true")
    p_ref.add_argument("--snapshot", action="store_true", help="Force a snapshot cutover on export")

    p_stat = sub.add_parser("status", help="Show local forum or topic mirror status")
    p_stat.add_argument("--profile", required=True)
    p_stat.add_argument("--thread")
    p_stat.add_argument("--since-position")

    p_top = sub.add_parser("topics", help="List mirrored topics filtered by completion, replies, or tag")
    p_top.add_argument("--profile", required=True)
    p_top.add_argument("--pending-only", action="store_true")
    p_top.add_argument("--deep-read-only", action="store_true")
    grp_rep = p_top.add_mutually_exclusive_group()
    grp_rep.add_argument("--has-replies", action="store_true")
    grp_rep.add_argument("--no-replies", action="store_true")
    p_top.add_argument("--tag")
    p_top.add_argument("--limit", type=int, default=50)
    p_top.add_argument("--offset", type=int, default=0)

    p_search = sub.add_parser("search", help="Search mirrored topics, messages, and requirements")
    p_search.add_argument("--profile", required=True)
    p_search.add_argument("query")

    p_chg = sub.add_parser("changes", help="Export forum.changes.v1 batch")
    p_chg.add_argument("--profile", required=True)
    p_chg.add_argument("--stream-id")
    p_chg.add_argument("--after-position")
    p_chg.add_argument("--out")

    p_stg = sub.add_parser("stage", help="Stage a draft.request.v1 or bundle into the composer without Send")
    p_stg.add_argument("--profile", required=True)
    p_stg.add_argument("--draft-file", help="Path to draft.request.v1 JSON or bundle dir/draft.request.json")
    p_stg.add_argument("--bundle-dir", "--bundle", dest="bundle_dir", help="Path to <draft-record-hash>/ selected-file bundle directory")
    p_stg.add_argument("--source-kind", choices=["external", "promotion"], default="external")
    p_stg.add_argument("--receipt-out")

    p_rec = sub.add_parser("reconcile", help="Reconcile submission or mark user-confirmed")
    p_rec.add_argument("--profile", required=True)
    p_rec.add_argument("--draft-id", required=True)
    p_rec.add_argument("--revision", type=int)
    p_rec.add_argument("--user-confirmed", action="store_true")
    p_rec.add_argument("--receipt-out")

    p_val = sub.add_parser("validate-contract", help="Validate a v1 JSON exchange file")
    p_val.add_argument("file")

    p_ss = sub.add_parser("session-state", help="Show persistent cross-process UI session/review lock state")
    p_ss.add_argument("--profile", required=True)

    p_rr = sub.add_parser("release-review", help="Clear persistent in_discord_review lock after manual review")
    p_rr.add_argument("--profile", required=True)

    p_sync = sub.add_parser("sync", help="Run reconcile -> incremental refresh -> exchange export -> stage pending")
    p_sync.add_argument("--profile", required=True)
    p_sync.add_argument("--provider", choices=["auto", "desktop", "seed"], default="auto")
    p_sync.add_argument("--deep-reread", type=int, default=2)
    p_sync.add_argument("--max-topics", type=int)
    p_sync.add_argument("--snapshot", action="store_true", help="Force a snapshot cutover on export")
    p_sync.add_argument("--stage-pending", action="store_true", help="Stage next un-staged draft in exchange/drafts")
    p_sync.add_argument("--reconcile", action="store_true", help="Reconcile previously staged drafts first")
    p_sync.add_argument("--source-kind", choices=["external", "promotion"], default="external")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dispatch = {
        "session": cmd_session,
        "init-profile": cmd_init_profile,
        "seed-index": cmd_seed_index,
        "refresh": cmd_refresh,
        "status": cmd_status,
        "topics": cmd_topics,
        "search": cmd_search,
        "changes": cmd_changes,
        "stage": cmd_stage,
        "reconcile": cmd_reconcile,
        "validate-contract": cmd_validate_contract,
        "session-state": cmd_session_state,
        "release-review": cmd_release_review,
        "sync": cmd_sync,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
