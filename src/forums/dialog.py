"""Optional Tkinter dialog for inspecting forum mirrors and staging reviewed drafts."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import Any, Callable, Optional

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    _HAS_TK = True
except Exception:  # pragma: no cover - headless Python builds without _tkinter
    tk = None  # type: ignore[assignment]
    filedialog = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    _HAS_TK = False

from .config import (
    DEFAULT_PROFILES_PATH,
    VALID_ROUTING_MODES,
    ForumProfile,
    load_forum_profiles,
    upsert_forum_profile,
)
from .drafts import check_draft_conflicts, render_forum_promo_draft
from .exchange import read_record
from .handoff import HandoffManager, SessionController, ingest_draft_bundle
from .models import optional_discord_id, utc_now_iso
from .providers.desktop import DesktopDiscordProvider
from .providers.base import SeededDiscordProvider
from .reader import ForumReader
from .store import ForumStore

_TOPLEVEL_BASE = tk.Toplevel if _HAS_TK and tk is not None else object


class ForumMirrorController:
    """Headless state and operations controller for forum mirror and draft staging UI."""

    def __init__(
        self,
        profiles_path: str | Path = DEFAULT_PROFILES_PATH,
        *,
        provider_factory: Optional[Callable[[ForumProfile], Any]] = None,
        handoff_provider_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.profiles_path = Path(profiles_path)
        self.profiles: dict[str, ForumProfile] = load_forum_profiles(self.profiles_path)
        self.active_profile_id: str = next(iter(self.profiles.keys()), "")
        self.session_controller = SessionController()
        self.current_draft: Optional[dict[str, Any]] = None
        self.current_bundle_dir: Optional[Path] = None
        self.current_transfer_root: Optional[Path] = None
        self.current_source_kind: str = "external"
        self._provider_factory = provider_factory
        self._handoff_provider_factory = handoff_provider_factory

    def current_profile(self, profile_id: Optional[str] = None) -> Optional[ForumProfile]:
        pid = self.active_profile_id if profile_id is None else profile_id
        return self.profiles.get(pid)

    def select_profile(self, profile_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        if profile_id is not None:
            self.active_profile_id = profile_id
        prof = self.current_profile()
        if prof is None:
            self.session_controller = SessionController()
            return None
        self.session_controller = SessionController(
            state_path=Path(prof.db_path).with_suffix(".session.json")
        )
        store = ForumStore(prof.db_path)
        try:
            store.ensure_scope(prof.guild_id, prof.forum_id)
            st = store.get_forum_status()
            topics = store.search("", forum_id=prof.forum_id)["topics"]
            return {
                "profile": prof,
                "status": st,
                "topics": topics,
                "review_active": self.session_controller.is_review_active(),
            }
        finally:
            store.close()

    def save_profile(
        self,
        *,
        profile_id: Optional[str] = None,
        routing_mode: Optional[str] = None,
        default_thread_id: Optional[str] = None,
        catalog_enabled: Optional[bool] = None,
        promotion_enabled: Optional[bool] = None,
        external_drafts_enabled: Optional[bool] = None,
        stage_enabled: Optional[bool] = None,
    ) -> ForumProfile:
        prof = self.current_profile(profile_id)
        if prof is None:
            raise ValueError("No forum profile selected")
        raw_thread = (default_thread_id or "").strip() or None
        clean_thread = optional_discord_id(raw_thread, "default_thread_id")
        updated = replace(
            prof,
            routing_mode=(routing_mode or prof.routing_mode).strip(),
            default_thread_id=clean_thread,
            catalog_enabled=prof.catalog_enabled if catalog_enabled is None else bool(catalog_enabled),
            promotion_enabled=prof.promotion_enabled if promotion_enabled is None else bool(promotion_enabled),
            external_drafts_enabled=(
                prof.external_drafts_enabled
                if external_drafts_enabled is None
                else bool(external_drafts_enabled)
            ),
            stage_enabled=prof.stage_enabled if stage_enabled is None else bool(stage_enabled),
        )
        upsert_forum_profile(updated, self.profiles_path)
        self.profiles[updated.profile_id] = updated
        self.active_profile_id = updated.profile_id
        return updated

    def make_provider(self, prof: ForumProfile) -> Any:
        if self._provider_factory is not None:
            return self._provider_factory(prof)
        desktop = DesktopDiscordProvider()
        has_seed = bool(prof.seed_index_path and Path(prof.seed_index_path).exists())
        if has_seed:
            try:
                caps = desktop.describe_session().capabilities
            except Exception:
                caps = {}
            if not caps.get("can_read_semantic") and not caps.get("can_read_ocr"):
                return SeededDiscordProvider(
                    guild_id=prof.guild_id,
                    forum_id=prof.forum_id,
                    forum_name=prof.name,
                    seed_path=prof.seed_index_path,
                )
        return desktop

    def make_handoff_provider(self) -> Any:
        if self._handoff_provider_factory is not None:
            return self._handoff_provider_factory()
        return DesktopDiscordProvider()

    def refresh(
        self,
        *,
        profile_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> dict[str, Any]:
        prof = self.current_profile(profile_id)
        if prof is None:
            raise ValueError("No forum profile selected")
        store = ForumStore(prof.db_path)
        try:
            provider = self.make_provider(prof)
            reader = ForumReader(prof, store, provider, session_controller=self.session_controller)
            result = reader.refresh_topic(thread_id) if thread_id else reader.incremental_refresh()
            if prof.exchange_dir:
                store.export_exchange_dir(
                    prof.exchange_dir,
                    guild_id=prof.guild_id,
                    forum_id=prof.forum_id,
                )
            return result
        finally:
            store.close()

    def discover_topic(
        self,
        target: str,
        *,
        profile_id: Optional[str] = None,
        refresh_requirements: bool = True,
    ) -> dict[str, Any]:
        prof = self.current_profile(profile_id)
        if prof is None:
            raise ValueError("No forum profile selected")
        store = ForumStore(prof.db_path)
        try:
            provider = self.make_provider(prof)
            reader = ForumReader(prof, store, provider, session_controller=self.session_controller)
            result = reader.discover_topic(target, refresh_requirements=refresh_requirements)
            if prof.exchange_dir and result.get("discovered") and not result.get("paused"):
                store.export_exchange_dir(
                    prof.exchange_dir,
                    guild_id=prof.guild_id,
                    forum_id=prof.forum_id,
                )
            return result
        finally:
            store.close()

    def search(self, query: str) -> dict[str, Any]:
        prof = self.current_profile()
        if prof is None:
            raise ValueError("No forum profile selected")
        store = ForumStore(prof.db_path)
        try:
            return store.search(query, forum_id=prof.forum_id)
        finally:
            store.close()

    def get_topic_status(self, thread_id: str) -> Optional[dict[str, Any]]:
        prof = self.current_profile()
        if prof is None:
            return None
        store = ForumStore(prof.db_path)
        try:
            return store.get_topic_status(thread_id)
        finally:
            store.close()

    @staticmethod
    def format_topic_status(st: dict[str, Any]) -> str:
        req_labels = [f"{r['requirement_id']}@r{r['revision']}" for r in st.get("requirements", [])]
        lines = [
            f"Title: {st['title']} (rev {st.get('revision', 1)})",
            f"URL: {st['source_url']}",
            (
                f"Observed: {st['observed_at']} | Deep read: {st.get('last_deep_read_at') or 'never'} | "
                f"History complete: {st.get('history_complete')} ({st.get('pending_reason') or 'ready'})"
            ),
            (
                f"Messages mirrored: {st['message_count']} | Replies: {st.get('reply_count', 0)} | "
                f"Has non-starter replies: {st.get('has_non_starter_replies')}"
            ),
            f"Requirements: {', '.join(req_labels) if req_labels else '(none)'}",
            "",
            f"Starter summary:\n{st['starter_summary'] or '(not yet fetched)'}",
            "",
            "Latest messages:",
        ]
        for m in st["latest_messages"]:
            lines.append(f"- {m['author'].get('label')} ({m['observed_at']}): {m['body']}")
        return "\n".join(lines)

    @staticmethod
    def format_draft_preview(
        draft: dict[str, Any],
        *,
        source_kind: str = "external",
        bundle_dir: Optional[Path] = None,
        conflicts: Optional[list[str]] = None,
    ) -> str:
        lines = [
            f"Loaded {source_kind} draft {draft['draft_id']} (rev {draft['revision']})",
            f"Action: {draft['action']}",
            f"Destination: {draft['destination'].get('source_url')}",
            f"Title: {draft.get('title') or '(none)'}",
            f"Attachments: {len(draft.get('evidence', []))}",
        ]
        if bundle_dir is not None:
            lines.append(f"Bundle Dir: {bundle_dir}")
        if conflicts:
            lines.append("Baseline Conflicts:")
            for c in conflicts:
                lines.append(f"  - {c}")
        lines.extend(["", f"Body:\n{draft['body']}"])
        return "\n".join(lines)

    def load_draft_file(self, path: str | Path) -> dict[str, Any]:
        draft = read_record(path)
        if draft["schema"] != "draft.request.v1":
            raise ValueError(f"Expected draft.request.v1, got {draft['schema']}")
        self.current_draft = draft
        self.current_bundle_dir = None
        self.current_transfer_root = Path(path).parent
        self.current_source_kind = "external"
        return draft

    def load_draft_bundle_dir(
        self,
        bundle_dir: str | Path,
        *,
        require_hash_dirname: bool = True,
    ) -> dict[str, Any]:
        draft, transfer_dir = ingest_draft_bundle(
            bundle_dir,
            require_hash_dirname=require_hash_dirname,
        )
        self.current_draft = draft
        self.current_bundle_dir = Path(bundle_dir)
        self.current_transfer_root = transfer_dir
        self.current_source_kind = "external"
        return draft

    def build_promo_draft(
        self,
        *,
        stream_info: Any,
        thread_id: Optional[str] = None,
        title: Optional[str] = None,
        draft_id: Optional[str] = None,
        message_id: Optional[str] = None,
        allow_create_if_missing: bool = True,
    ) -> tuple[dict[str, Any], list[str]]:
        prof = self.current_profile()
        if prof is None:
            raise ValueError("No forum profile selected")
        eff_draft_id = draft_id or f"promo-{prof.profile_id}-{utc_now_iso()}"
        store = ForumStore(prof.db_path)
        try:
            draft = render_forum_promo_draft(
                profile=prof,
                stream_info=stream_info,
                draft_id=eff_draft_id,
                thread_id=thread_id,
                title=title,
                store=store,
                message_id=message_id,
                allow_create_if_missing=allow_create_if_missing,
            )
            conflicts = check_draft_conflicts(draft, store)
        finally:
            store.close()
        self.current_draft = draft
        self.current_bundle_dir = None
        self.current_transfer_root = Path(prof.transfer_root)
        self.current_source_kind = "promotion"
        return draft, conflicts

    def stage_current_draft(self, *, refresh_before_stage: bool = True) -> dict[str, Any]:
        prof = self.current_profile()
        if prof is None or self.current_draft is None:
            raise ValueError("Select a profile and load or build a draft first.")
        store = ForumStore(prof.db_path)
        try:
            cooldown_path = str(Path(prof.db_path).with_suffix(".cooldown.json"))
            mgr = HandoffManager(
                store,
                self.make_handoff_provider(),
                session_controller=self.session_controller,
                cooldown_path=cooldown_path,
                exchange_dir=prof.exchange_dir,
            )
            if self.current_bundle_dir is not None:
                return mgr.stage_draft_bundle(
                    self.current_bundle_dir,
                    prof,
                    source_kind=self.current_source_kind,
                    refresh_before_stage=refresh_before_stage,
                )
            return mgr.stage_draft_request(
                self.current_draft,
                prof,
                source_kind=self.current_source_kind,
                transfer_root=self.current_transfer_root,
                refresh_before_stage=refresh_before_stage,
            )
        finally:
            store.close()

    def reconcile_current_draft(self, *, user_confirmed: bool = False) -> dict[str, Any]:
        prof = self.current_profile()
        if prof is None or self.current_draft is None:
            raise ValueError("Select a profile and load or build a draft first.")
        store = ForumStore(prof.db_path)
        try:
            cooldown_path = str(Path(prof.db_path).with_suffix(".cooldown.json"))
            mgr = HandoffManager(
                store,
                self.make_handoff_provider(),
                session_controller=self.session_controller,
                cooldown_path=cooldown_path,
                exchange_dir=prof.exchange_dir,
            )
            return mgr.reconcile_submission(
                self.current_draft["draft_id"],
                draft_revision=self.current_draft["revision"],
                profile=prof,
                user_confirmed=user_confirmed,
            )
        finally:
            store.close()

    def release_review_pause(self) -> None:
        self.session_controller.release_review()


class ForumMirrorDialog(_TOPLEVEL_BASE):
    """Forum mirror status, local search, and reviewed composer staging window."""

    def __init__(
        self,
        parent: Any,
        profiles_path: str | Path = DEFAULT_PROFILES_PATH,
        *,
        provider_factory: Optional[Callable[[ForumProfile], Any]] = None,
        handoff_provider_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        if not _HAS_TK or tk is None:
            raise RuntimeError("Tkinter (_tkinter) is unavailable in this Python environment.")
        super().__init__(parent)
        self.title("Forum Mirror & Draft Staging")
        self.geometry("1040x720")
        self.minsize(780, 520)

        self.controller = ForumMirrorController(
            profiles_path=profiles_path,
            provider_factory=provider_factory,
            handoff_provider_factory=handoff_provider_factory,
        )
        self._create_widgets()
        self._load_selected_profile()

    @property
    def profiles(self) -> dict[str, ForumProfile]:
        return self.controller.profiles

    @property
    def session_controller(self) -> SessionController:
        return self.controller.session_controller

    @property
    def current_draft(self) -> Optional[dict[str, Any]]:
        return self.controller.current_draft

    @property
    def current_bundle_dir(self) -> Optional[Path]:
        return self.controller.current_bundle_dir

    @property
    def current_source_kind(self) -> str:
        return self.controller.current_source_kind

    def _create_widgets(self) -> None:
        top = ttk.Frame(self, padding="10")
        top.pack(fill=tk.X)

        ttk.Label(top, text="Forum Profile:").pack(side=tk.LEFT)
        self.profile_var = tk.StringVar(
            value=next(iter(self.profiles.keys()), "")
        )
        self.profile_combo = ttk.Combobox(
            top,
            textvariable=self.profile_var,
            values=list(self.profiles.keys()),
            state="readonly",
            width=30,
        )
        self.profile_combo.pack(side=tk.LEFT, padx=6)
        self.profile_combo.bind("<<ComboboxSelected>>", lambda _e: self._load_selected_profile())

        ttk.Button(top, text="Refresh Mirror", command=self._refresh_mirror).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="Refresh Topic", command=self._refresh_selected_topic).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="Load Draft JSON...", command=self._load_draft_file).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="Load Bundle Dir...", command=self._load_draft_bundle_dir).pack(side=tk.LEFT, padx=4)

        self.coverage_var = tk.StringVar(value="Coverage: not loaded")
        ttk.Label(top, textvariable=self.coverage_var, foreground="gray").pack(side=tk.RIGHT)

        profile_bar = ttk.Frame(self, padding=(10, 0, 10, 6))
        profile_bar.pack(fill=tk.X)
        self.purpose_var = tk.StringVar(value="Purpose: -")
        ttk.Label(profile_bar, textvariable=self.purpose_var).pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(profile_bar, text="Routing:").pack(side=tk.LEFT)
        self.routing_mode_var = tk.StringVar(value="reply_existing")
        self.routing_combo = ttk.Combobox(
            profile_bar,
            textvariable=self.routing_mode_var,
            values=sorted(VALID_ROUTING_MODES),
            state="readonly",
            width=22,
        )
        self.routing_combo.pack(side=tk.LEFT, padx=4)

        ttk.Label(profile_bar, text="Default Thread:").pack(side=tk.LEFT, padx=(6, 0))
        self.default_thread_var = tk.StringVar()
        ttk.Entry(profile_bar, textvariable=self.default_thread_var, width=21).pack(side=tk.LEFT, padx=4)

        self.catalog_enabled_var = tk.BooleanVar(value=False)
        self.promotion_enabled_var = tk.BooleanVar(value=False)
        self.external_drafts_enabled_var = tk.BooleanVar(value=False)
        self.stage_enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(profile_bar, text="Catalog", variable=self.catalog_enabled_var).pack(side=tk.LEFT, padx=3)
        self.promotion_check = ttk.Checkbutton(
            profile_bar, text="Promotion", variable=self.promotion_enabled_var
        )
        self.promotion_check.pack(side=tk.LEFT, padx=3)
        ttk.Checkbutton(
            profile_bar, text="External Drafts", variable=self.external_drafts_enabled_var
        ).pack(side=tk.LEFT, padx=3)
        ttk.Checkbutton(profile_bar, text="Stage", variable=self.stage_enabled_var).pack(side=tk.LEFT, padx=3)
        ttk.Button(profile_bar, text="Save Profile", command=self._save_selected_profile).pack(side=tk.RIGHT)

        search_bar = ttk.Frame(self, padding=(10, 0, 10, 6))
        search_bar.pack(fill=tk.X)
        ttk.Label(search_bar, text="Search local mirror:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        entry = ttk.Entry(search_bar, textvariable=self.search_var, width=40)
        entry.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        entry.bind("<Return>", lambda _e: self._run_search())
        ttk.Button(search_bar, text="Search", command=self._run_search).pack(side=tk.LEFT)

        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        left = ttk.Frame(paned)
        paned.add(left, weight=1)
        columns = ("thread_id", "title", "rev", "observed_at")
        self.topics_tree = ttk.Treeview(left, columns=columns, show="headings")
        self.topics_tree.heading("thread_id", text="Thread ID")
        self.topics_tree.heading("title", text="Topic")
        self.topics_tree.heading("rev", text="Rev")
        self.topics_tree.heading("observed_at", text="Observed")
        self.topics_tree.column("thread_id", width=155, stretch=False)
        self.topics_tree.column("title", width=220)
        self.topics_tree.column("rev", width=40, stretch=False)
        self.topics_tree.column("observed_at", width=140, stretch=False)
        self.topics_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.topics_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_topic_select())

        scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.topics_tree.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.topics_tree.configure(yscrollcommand=scroll.set)

        right = ttk.Frame(paned)
        paned.add(right, weight=1)

        promo_frame = ttk.LabelFrame(right, text="Promotion Draft Generator", padding="6")
        promo_frame.pack(fill=tk.X, pady=(0, 6))
        self.promo_username_var = tk.StringVar()
        self.promo_title_var = tk.StringVar()
        self.promo_game_var = tk.StringVar()
        self.promo_url_var = tk.StringVar()

        ttk.Label(promo_frame, text="Streamer:").grid(row=0, column=0, sticky=tk.W, padx=(0, 4))
        ttk.Entry(promo_frame, textvariable=self.promo_username_var, width=18).grid(row=0, column=1, sticky=tk.EW, padx=(0, 8))
        ttk.Label(promo_frame, text="Game:").grid(row=0, column=2, sticky=tk.W, padx=(0, 4))
        ttk.Entry(promo_frame, textvariable=self.promo_game_var, width=18).grid(row=0, column=3, sticky=tk.EW)

        ttk.Label(promo_frame, text="Title:").grid(row=1, column=0, sticky=tk.W, padx=(0, 4), pady=(4, 0))
        ttk.Entry(promo_frame, textvariable=self.promo_title_var, width=18).grid(row=1, column=1, sticky=tk.EW, padx=(0, 8), pady=(4, 0))
        ttk.Label(promo_frame, text="URL:").grid(row=1, column=2, sticky=tk.W, padx=(0, 4), pady=(4, 0))
        ttk.Entry(promo_frame, textvariable=self.promo_url_var, width=18).grid(row=1, column=3, sticky=tk.EW, pady=(4, 0))

        self.build_promo_btn = ttk.Button(
            promo_frame,
            text="Build Promo Draft",
            command=self._build_promo_draft,
        )
        self.build_promo_btn.grid(row=0, column=4, rowspan=2, padx=(8, 0), sticky=tk.NS)
        promo_frame.columnconfigure(1, weight=1)
        promo_frame.columnconfigure(3, weight=1)

        detail_frame = ttk.LabelFrame(right, text="Topic Status / Draft Review", padding="8")
        detail_frame.pack(fill=tk.BOTH, expand=True)
        self.detail_text = tk.Text(detail_frame, wrap=tk.WORD, height=18)
        self.detail_text.pack(fill=tk.BOTH, expand=True)
        self.detail_text.configure(state="disabled")

        stage_bar = ttk.Frame(right, padding=(0, 8, 0, 0))
        stage_bar.pack(fill=tk.X)
        ttk.Button(
            stage_bar,
            text="Stage in Composer (No Send)",
            command=self._stage_current_draft,
        ).pack(side=tk.LEFT)
        ttk.Button(
            stage_bar,
            text="Observe Submission",
            command=self._reconcile_current_draft,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(
            stage_bar,
            text="Confirm Submitted",
            command=self._confirm_current_draft_submitted,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            stage_bar,
            text="Release Review Pause",
            command=self._release_review_pause,
        ).pack(side=tk.LEFT)

    def _current_profile(self) -> Optional[ForumProfile]:
        return self.controller.current_profile(self.profile_var.get())

    def _set_detail_text(self, text: str) -> None:
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert("1.0", text)
        self.detail_text.configure(state="disabled")

    def _load_selected_profile(self) -> None:
        state = self.controller.select_profile(self.profile_var.get())
        if state is None:
            self.coverage_var.set("No forum profile configured")
            self.purpose_var.set("Purpose: -")
            self.routing_mode_var.set("reply_existing")
            self.default_thread_var.set("")
            self.catalog_enabled_var.set(False)
            self.promotion_enabled_var.set(False)
            self.external_drafts_enabled_var.set(False)
            self.stage_enabled_var.set(False)
            return

        prof = state["profile"]
        self.purpose_var.set(f"Purpose: {prof.purpose}")
        self.routing_mode_var.set(prof.routing_mode)
        self.default_thread_var.set(prof.default_thread_id or "")
        self.catalog_enabled_var.set(bool(prof.catalog_enabled))
        self.promotion_enabled_var.set(bool(prof.promotion_enabled))
        self.external_drafts_enabled_var.set(bool(prof.external_drafts_enabled))
        self.stage_enabled_var.set(bool(prof.stage_enabled))
        if prof.purpose == "external_project":
            self.promotion_check.configure(state="disabled")
            self.build_promo_btn.configure(state="disabled")
        else:
            self.promotion_check.configure(state="normal")
            self.build_promo_btn.configure(state="normal" if prof.can_promote() else "disabled")

        st = state["status"]
        cov = st["coverage"]
        counts = st["counts"]
        lock_suffix = " | Review Pause Active" if state["review_active"] else ""
        self.coverage_var.set(
            f"Topics: {counts['topics']} ({cov['topics']}, deep={counts.get('topics_deep_read', 0)}, pending={counts.get('topics_pending_history', 0)}) | "
            f"Histories: {cov['histories']} | Reqs: {cov['requirements']}{lock_suffix}"
        )
        self._populate_topics(state["topics"])

    def _save_selected_profile(self, *, notify: bool = True) -> Optional[ForumProfile]:
        try:
            updated = self.controller.save_profile(
                profile_id=self.profile_var.get(),
                routing_mode=self.routing_mode_var.get(),
                default_thread_id=self.default_thread_var.get(),
                catalog_enabled=self.catalog_enabled_var.get(),
                promotion_enabled=self.promotion_enabled_var.get(),
                external_drafts_enabled=self.external_drafts_enabled_var.get(),
                stage_enabled=self.stage_enabled_var.get(),
            )
        except Exception as exc:
            if notify:
                messagebox.showerror("Profile Validation Error", str(exc))
            return None
        self._load_selected_profile()
        if notify:
            messagebox.showinfo(
                "Profile Saved",
                f"Saved profile {updated.profile_id!r} (routing={updated.routing_mode}, promotion={updated.promotion_enabled}, stage={updated.stage_enabled}).",
            )
        return updated

    def _populate_topics(self, topics: list[dict[str, Any]]) -> None:
        for item in self.topics_tree.get_children():
            self.topics_tree.delete(item)
        for t in topics:
            self.topics_tree.insert(
                "",
                tk.END,
                iid=t["thread_id"],
                values=(t["thread_id"], t["title"], t["revision"], t["observed_at"]),
            )

    def _run_search(self) -> None:
        if self._current_profile() is None:
            return
        res = self.controller.search(self.search_var.get())
        self._populate_topics(res["topics"])
        lines = [
            f"Search query: {res['query']!r}",
            f"Topics: {len(res['topics'])}, Messages: {len(res['messages'])}, Requirements: {len(res['requirements'])}, Unresolved: {len(res['unresolved_topics'])}",
            "",
        ]
        for m in res["messages"][:10]:
            lines.append(f"[{m['topic_title']}] {m['author'].get('label')}: {m['body'][:160]}")
        self._set_detail_text("\n".join(lines))

    def _on_topic_select(self) -> None:
        sel = self.topics_tree.selection()
        if not sel:
            return
        st = self.controller.get_topic_status(sel[0])
        if st:
            self._set_detail_text(self.controller.format_topic_status(st))

    def _refresh_mirror(self) -> None:
        prof = self._current_profile()
        if prof is None:
            return

        def worker() -> None:
            try:
                self.controller.refresh(profile_id=prof.profile_id)
                self.after(0, self._load_selected_profile)
            except Exception as exc:
                self.after(0, lambda err=str(exc): messagebox.showerror("Refresh Error", err))

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_selected_topic(self, thread_id: Optional[str] = None) -> None:
        prof = self._current_profile()
        sel = self.topics_tree.selection()
        tid = thread_id or (sel[0] if sel else None)
        if prof is None or not tid:
            messagebox.showinfo("Refresh Topic", "Select a topic row first.")
            return

        def worker() -> None:
            try:
                self.controller.refresh(profile_id=prof.profile_id, thread_id=tid)

                def _finish() -> None:
                    self._load_selected_profile()
                    if self.topics_tree.exists(tid):
                        self.topics_tree.selection_set(tid)
                        self._on_topic_select()

                self.after(0, _finish)
            except Exception as exc:
                self.after(0, lambda err=str(exc): messagebox.showerror("Topic Refresh Error", err))

        threading.Thread(target=worker, daemon=True).start()

    def _load_draft_file(self, path: Optional[str | Path] = None) -> Optional[dict[str, Any]]:
        selected = str(path) if path is not None else filedialog.askopenfilename(
            title="Select draft.request.v1 JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not selected:
            return None
        try:
            draft = self.controller.load_draft_file(selected)
            self._set_detail_text(self.controller.format_draft_preview(draft, source_kind="external"))
            return draft
        except Exception as exc:
            messagebox.showerror("Invalid Draft", str(exc))
            return None

    def _load_draft_bundle_dir(
        self,
        bundle_dir: Optional[str | Path] = None,
        *,
        require_hash_dirname: bool = True,
    ) -> Optional[dict[str, Any]]:
        selected = str(bundle_dir) if bundle_dir is not None else filedialog.askdirectory(
            title="Select <digest>/ Draft Bundle Directory"
        )
        if not selected:
            return None
        try:
            draft = self.controller.load_draft_bundle_dir(
                selected,
                require_hash_dirname=require_hash_dirname,
            )
            self._set_detail_text(
                self.controller.format_draft_preview(
                    draft,
                    source_kind="external",
                    bundle_dir=self.controller.current_bundle_dir,
                )
            )
            return draft
        except Exception as exc:
            messagebox.showerror("Invalid Draft Bundle", str(exc))
            return None

    def _build_promo_draft(
        self,
        *,
        stream_info: Optional[Any] = None,
        thread_id: Optional[str] = None,
        title: Optional[str] = None,
        draft_id: Optional[str] = None,
        message_id: Optional[str] = None,
        allow_create_if_missing: bool = True,
        notify: bool = True,
    ) -> Optional[dict[str, Any]]:
        prof = self._current_profile()
        if prof is None:
            return None
        info = stream_info or SimpleNamespace(
            username=(self.promo_username_var.get() or "").strip(),
            title=(self.promo_title_var.get() or "").strip() or "Live Stream",
            game=(self.promo_game_var.get() or "").strip(),
            stream_url=(self.promo_url_var.get() or "").strip(),
            platform="",
        )
        sel = self.topics_tree.selection()
        eff_tid = thread_id
        if eff_tid is None and prof.routing_mode == "reply_existing" and not prof.default_thread_id and sel:
            eff_tid = sel[0]
        eff_title = title or ((self.promo_title_var.get() or "").strip() or None)
        try:
            draft, conflicts = self.controller.build_promo_draft(
                stream_info=info,
                thread_id=eff_tid,
                title=eff_title,
                draft_id=draft_id,
                message_id=message_id,
                allow_create_if_missing=allow_create_if_missing,
            )
        except Exception as exc:
            if notify:
                messagebox.showerror("Promotion Draft Error", str(exc))
            return None
        self._set_detail_text(
            self.controller.format_draft_preview(
                draft,
                source_kind="promotion",
                conflicts=conflicts,
            )
        )
        return draft

    def _stage_current_draft(self, *, notify: bool = True) -> Optional[dict[str, Any]]:
        try:
            receipt = self.controller.stage_current_draft()
            if notify:
                messagebox.showinfo(
                    "Staging Result",
                    f"Status: {receipt['status']}\nReason: {receipt.get('reason') or 'Ready in composer for your review and Send.'}",
                )
            self._load_selected_profile()
            return receipt
        except Exception as exc:
            if notify:
                messagebox.showerror("Staging Error", str(exc))
            return None

    def _reconcile_current_draft(
        self,
        *,
        user_confirmed: bool = False,
        notify: bool = True,
    ) -> Optional[dict[str, Any]]:
        try:
            receipt = self.controller.reconcile_current_draft(user_confirmed=user_confirmed)
            if notify:
                messagebox.showinfo(
                    "Reconcile Result",
                    f"Status: {receipt['status']}\nMessage URL: {receipt.get('message_url') or '(none)'}\nReason: {receipt.get('reason') or '(none)'}",
                )
            self._load_selected_profile()
            return receipt
        except Exception as exc:
            if notify:
                messagebox.showerror("Reconcile Error", str(exc))
            return None

    def _confirm_current_draft_submitted(self, *, notify: bool = True) -> Optional[dict[str, Any]]:
        return self._reconcile_current_draft(user_confirmed=True, notify=notify)

    def _release_review_pause(self) -> None:
        self.controller.release_review_pause()
        self._load_selected_profile()
        messagebox.showinfo("Review Lock Released", "Forum background refresh is allowed again.")


def launch_forum_dialog(profiles_path: str | Path = DEFAULT_PROFILES_PATH) -> None:
    """Launch ForumMirrorDialog as a standalone Tkinter window."""
    if not _HAS_TK or tk is None:
        raise RuntimeError("Tkinter (_tkinter) is unavailable in this Python environment.")
    root = tk.Tk()
    root.withdraw()
    dlg = ForumMirrorDialog(root, profiles_path=profiles_path)
    dlg.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
