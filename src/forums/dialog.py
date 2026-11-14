"""Optional Tkinter dialog for inspecting forum mirrors and staging reviewed drafts."""
from __future__ import annotations

import json
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Optional

from .config import DEFAULT_PROFILES_PATH, ForumProfile, load_forum_profiles
from .exchange import read_record
from .handoff import HandoffManager, SessionController
from .providers.desktop import DesktopDiscordProvider
from .providers.base import SeededDiscordProvider
from .reader import ForumReader
from .store import ForumStore


class ForumMirrorDialog(tk.Toplevel):
    """Forum mirror status, local search, and reviewed composer staging window."""

    def __init__(
        self,
        parent: tk.Misc,
        profiles_path: str | Path = DEFAULT_PROFILES_PATH,
    ) -> None:
        super().__init__(parent)
        self.title("Forum Mirror & Draft Staging")
        self.geometry("920x640")
        self.minsize(720, 480)

        self.profiles_path = Path(profiles_path)
        self.profiles: dict[str, ForumProfile] = load_forum_profiles(self.profiles_path)
        self.session_controller = SessionController()
        self.current_draft: Optional[dict[str, Any]] = None

        self._create_widgets()
        self._load_selected_profile()

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
        ttk.Button(top, text="Load Draft JSON...", command=self._load_draft_file).pack(side=tk.LEFT, padx=4)

        self.coverage_var = tk.StringVar(value="Coverage: not loaded")
        ttk.Label(top, textvariable=self.coverage_var, foreground="gray").pack(side=tk.RIGHT)

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

        detail_frame = ttk.LabelFrame(right, text="Topic Status / Draft Review", padding="8")
        detail_frame.pack(fill=tk.BOTH, expand=True)
        self.detail_text = tk.Text(detail_frame, wrap=tk.WORD, height=20)
        self.detail_text.pack(fill=tk.BOTH, expand=True)
        self.detail_text.configure(state="disabled")

        stage_bar = ttk.Frame(right, padding=(0, 8, 0, 0))
        stage_bar.pack(fill=tk.X)
        ttk.Button(
            stage_bar,
            text="Stage in Discord Composer (No Send)",
            command=self._stage_current_draft,
        ).pack(side=tk.LEFT)
        ttk.Button(
            stage_bar,
            text="Reconcile / Mark Submitted",
            command=self._reconcile_current_draft,
        ).pack(side=tk.LEFT, padx=6)

    def _current_profile(self) -> Optional[ForumProfile]:
        pid = self.profile_var.get()
        return self.profiles.get(pid)

    def _set_detail_text(self, text: str) -> None:
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert("1.0", text)
        self.detail_text.configure(state="disabled")

    def _load_selected_profile(self) -> None:
        prof = self._current_profile()
        if prof is None:
            self.coverage_var.set("No forum profile configured")
            return
        store = ForumStore(prof.db_path)
        try:
            store.ensure_scope(prof.guild_id, prof.forum_id)
            st = store.get_forum_status()
            cov = st["coverage"]
            self.coverage_var.set(
                f"Topics: {st['counts']['topics']} ({cov['topics']}) | "
                f"Histories: {cov['histories']} | Reqs: {cov['requirements']}"
            )
            self._populate_topics(store.search("", forum_id=prof.forum_id)["topics"])
        finally:
            store.close()

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
        prof = self._current_profile()
        if prof is None:
            return
        store = ForumStore(prof.db_path)
        try:
            res = store.search(self.search_var.get(), forum_id=prof.forum_id)
            self._populate_topics(res["topics"])
            lines = [
                f"Search query: {res['query']!r}",
                f"Topics: {len(res['topics'])}, Messages: {len(res['messages'])}, Requirements: {len(res['requirements'])}, Unresolved: {len(res['unresolved_topics'])}",
                "",
            ]
            for m in res["messages"][:10]:
                lines.append(f"[{m['topic_title']}] {m['author'].get('label')}: {m['body'][:160]}")
            self._set_detail_text("\n".join(lines))
        finally:
            store.close()

    def _on_topic_select(self) -> None:
        prof = self._current_profile()
        sel = self.topics_tree.selection()
        if prof is None or not sel:
            return
        tid = sel[0]
        store = ForumStore(prof.db_path)
        try:
            st = store.get_topic_status(tid)
            if not st:
                return
            lines = [
                f"Title: {st['title']}",
                f"URL: {st['source_url']}",
                f"Observed: {st['observed_at']} | Messages mirrored: {st['message_count']}",
                f"Starter summary:\n{st['starter_summary'] or '(not yet fetched)'}",
                "",
                "Latest messages:",
            ]
            for m in st["latest_messages"]:
                lines.append(f"- {m['author'].get('label')} ({m['observed_at']}): {m['body']}")
            self._set_detail_text("\n".join(lines))
        finally:
            store.close()

    def _refresh_mirror(self) -> None:
        prof = self._current_profile()
        if prof is None:
            return

        def worker() -> None:
            store = ForumStore(prof.db_path)
            try:
                provider = (
                    SeededDiscordProvider(
                        guild_id=prof.guild_id,
                        forum_id=prof.forum_id,
                        forum_name=prof.name,
                        seed_path=prof.seed_index_path,
                    )
                    if prof.seed_index_path and Path(prof.seed_index_path).exists()
                    else DesktopDiscordProvider()
                )
                reader = ForumReader(prof, store, provider, session_controller=self.session_controller)
                reader.incremental_refresh()
                self.after(0, self._load_selected_profile)
            except Exception as exc:
                self.after(0, lambda err=str(exc): messagebox.showerror("Refresh Error", err))
            finally:
                store.close()

        threading.Thread(target=worker, daemon=True).start()

    def _load_draft_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select draft.request.v1 JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            draft = read_record(path)
            if draft["schema"] != "draft.request.v1":
                raise ValueError(f"Expected draft.request.v1, got {draft['schema']}")
            self.current_draft = draft
            self._set_detail_text(
                f"Loaded draft {draft['draft_id']} (rev {draft['revision']})\n"
                f"Action: {draft['action']}\n"
                f"Destination: {draft['destination'].get('source_url')}\n"
                f"Attachments: {len(draft.get('evidence', []))}\n\n"
                f"Body:\n{draft['body']}"
            )
        except Exception as exc:
            messagebox.showerror("Invalid Draft", str(exc))

    def _stage_current_draft(self) -> None:
        prof = self._current_profile()
        if prof is None or self.current_draft is None:
            messagebox.showinfo("Draft Staging", "Select a profile and load a draft JSON first.")
            return
        store = ForumStore(prof.db_path)
        try:
            mgr = HandoffManager(store, DesktopDiscordProvider(), session_controller=self.session_controller)
            receipt = mgr.stage_draft_request(self.current_draft, prof)
            messagebox.showinfo(
                "Staging Result",
                f"Status: {receipt['status']}\nReason: {receipt.get('reason') or 'Ready in composer for your review and Send.'}",
            )
        except Exception as exc:
            messagebox.showerror("Staging Error", str(exc))
        finally:
            store.close()

    def _reconcile_current_draft(self) -> None:
        prof = self._current_profile()
        if prof is None or self.current_draft is None:
            return
        store = ForumStore(prof.db_path)
        try:
            mgr = HandoffManager(store, DesktopDiscordProvider(), session_controller=self.session_controller)
            receipt = mgr.reconcile_submission(
                self.current_draft["draft_id"],
                draft_revision=self.current_draft["revision"],
                profile=prof,
                user_confirmed=True,
            )
            messagebox.showinfo("Reconcile Result", f"Status: {receipt['status']}")
        except Exception as exc:
            messagebox.showerror("Reconcile Error", str(exc))
        finally:
            store.close()


def launch_forum_dialog(profiles_path: str | Path = DEFAULT_PROFILES_PATH) -> None:
    """Launch ForumMirrorDialog as a standalone Tkinter window."""
    root = tk.Tk()
    root.withdraw()
    dlg = ForumMirrorDialog(root, profiles_path=profiles_path)
    dlg.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
