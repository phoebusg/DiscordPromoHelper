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
from .exchange import read_record, validate_exchange_record, write_record_atomic
from .handoff import HandoffManager, SessionController
from .providers.base import SeededDiscordProvider
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
    provider = SeededDiscordProvider(
        guild_id=prof.guild_id,
        forum_id=prof.forum_id,
        forum_name=prof.name,
        seed_path=seed_file,
    )
    store = ForumStore(prof.db_path)
    try:
        reader = ForumReader(prof, store, provider)
        res = reader.initial_backfill(max_topics_per_run=args.max_topics)
        _print_json({"profile": prof.profile_id, "result": res, "status": store.get_forum_status()})
    finally:
        store.close()
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    prof = _load_profile_or_exit(args.profile, args.profiles_path)
    desktop = DesktopDiscordProvider()
    session_desc = desktop.describe_session()
    store = ForumStore(prof.db_path)
    try:
        use_seed = args.provider == "seed" or (
            args.provider == "auto"
            and not session_desc.capabilities.get("can_read_semantic")
            and not session_desc.capabilities.get("can_read_ocr")
            and prof.seed_index_path
            and Path(prof.seed_index_path).exists()
        )

        if use_seed:
            provider: Any = SeededDiscordProvider(
                guild_id=prof.guild_id,
                forum_id=prof.forum_id,
                forum_name=prof.name,
                seed_path=prof.seed_index_path,
            )
        else:
            provider = desktop

        reader = ForumReader(prof, store, provider)
        if args.backfill:
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

        exported: dict[str, str] = {}
        if args.export:
            ex_dir = Path(prof.exchange_dir)
            ex_dir.mkdir(parents=True, exist_ok=True)
            snap = store.export_snapshot()
            snap_path = ex_dir / f"{prof.profile_id}.snapshot.v1.json"
            write_record_atomic(snap_path, snap)
            chg_batch = store.export_changes()
            chg_path = ex_dir / f"{prof.profile_id}.changes.v1.json"
            write_record_atomic(chg_path, chg_batch)
            exported = {"snapshot": str(snap_path), "changes": str(chg_path)}

        _print_json(
            {
                "profile": prof.profile_id,
                "provider_used": "seed+desktop_probe" if use_seed else "desktop",
                "desktop_session": asdict(session_desc),
                "result": res,
                "status": store.get_forum_status(),
                "exported": exported,
            }
        )
    finally:
        store.close()
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
    draft = read_record(args.draft_file)
    store = ForumStore(prof.db_path)
    try:
        mgr = HandoffManager(store, DesktopDiscordProvider(), session_controller=SessionController())
        receipt = mgr.stage_draft_request(draft, prof, source_kind=args.source_kind)
        if args.receipt_out:
            write_record_atomic(args.receipt_out, receipt)
        _print_json(receipt)
    finally:
        store.close()
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    prof = _load_profile_or_exit(args.profile, args.profiles_path)
    store = ForumStore(prof.db_path)
    try:
        mgr = HandoffManager(store, DesktopDiscordProvider(), session_controller=SessionController())
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
    p_ref.add_argument("--deep-reread", type=int, default=2)
    p_ref.add_argument("--max-topics", type=int)
    p_ref.add_argument("--export", action="store_true")

    p_stat = sub.add_parser("status", help="Show local forum or topic mirror status")
    p_stat.add_argument("--profile", required=True)
    p_stat.add_argument("--thread")
    p_stat.add_argument("--since-position")

    p_search = sub.add_parser("search", help="Search mirrored topics, messages, and requirements")
    p_search.add_argument("--profile", required=True)
    p_search.add_argument("query")

    p_chg = sub.add_parser("changes", help="Export forum.changes.v1 batch")
    p_chg.add_argument("--profile", required=True)
    p_chg.add_argument("--stream-id")
    p_chg.add_argument("--after-position")
    p_chg.add_argument("--out")

    p_stg = sub.add_parser("stage", help="Stage a draft.request.v1 into the composer without Send")
    p_stg.add_argument("--profile", required=True)
    p_stg.add_argument("--draft-file", required=True)
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
        "search": cmd_search,
        "changes": cmd_changes,
        "stage": cmd_stage,
        "reconcile": cmd_reconcile,
        "validate-contract": cmd_validate_contract,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
