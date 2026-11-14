"""Installed-runtime Discord desktop adapter using OS accessibility + OCR fallback."""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import time
from typing import Any, Optional

from ..models import build_discord_channel_url, parse_discord_channel_url, utc_now_iso
from .base import (
    ComposerStageObservation,
    DiscordViewProvider,
    ForumObservation,
    MessageObservation,
    RequirementObservation,
    SessionDescription,
    SubmissionObservation,
    TopicHistoryObservation,
    TopicListObservation,
    TopicObservation,
)

logger = logging.getLogger(__name__)

try:
    import psutil
except Exception:
    psutil = None

try:
    import pyautogui
except Exception:
    pyautogui = None

try:
    import pyperclip
except Exception:
    pyperclip = None

try:
    from src import utils
except Exception:
    utils = None

_DISCORD_URL_IN_TEXT_RE = re.compile(
    r"(?:https://(?:(?:ptb|canary)\.)?discord\.com|discord://-)/channels/(\d+)/(\d+)(?:/(\d+))?"
)


def _find_discord_pid() -> Optional[int]:
    if psutil is None:
        return None
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = (proc.info.get("name") or "").strip().lower()
            if name == "discord" or name == "discord.exe":
                return int(proc.info["pid"])
        except Exception:
            continue
    return None


def _get_discord_window_quartz() -> tuple[Optional[str], Optional[tuple[int, int, int, int]]]:
    """Query on-screen Discord window geometry via macOS Quartz without requiring AX trust."""
    if not sys.platform.startswith("darwin"):
        return None, None
    try:
        import Quartz

        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
        )
        for w in wins:
            owner = (w.get("kCGWindowOwnerName") or "").lower()
            if owner == "discord":
                bounds = w.get("kCGWindowBounds") or {}
                width = int(bounds.get("Width", 0))
                height = int(bounds.get("Height", 0))
                if width >= 200 and height >= 200:
                    x = int(bounds.get("X", 0))
                    y = int(bounds.get("Y", 0))
                    title = w.get("kCGWindowName") or None
                    return title, (x, y, width, height)
    except Exception:
        pass
    return None, None


def _ax_attr(element: Any, attr: str) -> Any:
    try:
        import ApplicationServices as AS

        err, val = AS.AXUIElementCopyAttributeValue(element, attr, None)
        if err == 0:
            return val
    except Exception:
        pass
    return None


def _walk_ax_tree(
    root: Any,
    *,
    max_nodes: int = 4000,
    max_depth: int = 35,
) -> list[dict[str, Any]]:
    """Breadth-first bounded inspection of macOS AXUIElements."""
    nodes: list[dict[str, Any]] = []
    queue: list[tuple[Any, int]] = [(root, 0)]
    while queue and len(nodes) < max_nodes:
        elem, depth = queue.pop(0)
        if depth > max_depth:
            continue
        role = _ax_attr(elem, "AXRole")
        title = _ax_attr(elem, "AXTitle")
        value = _ax_attr(elem, "AXValue")
        desc = _ax_attr(elem, "AXDescription")
        url = _ax_attr(elem, "AXURL")
        enabled = _ax_attr(elem, "AXEnabled")
        dom_id = _ax_attr(elem, "AXDOMIdentifier")
        nodes.append(
            {
                "depth": depth,
                "role": str(role) if role is not None else "",
                "title": str(title) if title is not None else "",
                "value": str(value) if value is not None else "",
                "description": str(desc) if desc is not None else "",
                "url": str(url) if url is not None else "",
                "dom_id": str(dom_id) if dom_id is not None else "",
                "enabled": bool(enabled) if enabled is not None else None,
                "element": elem,
            }
        )
        children = _ax_attr(elem, "AXChildren")
        if children:
            for ch in children:
                queue.append((ch, depth + 1))
    return nodes


def _capture_settled_region(bbox: tuple[int, int, int, int], settle_ms: int = 150) -> Any:
    """Grab a window region twice and return the settled PIL image."""
    if utils is None or not hasattr(utils, "_safe_grab"):
        return None
    img1 = utils._safe_grab(bbox)
    if img1 is None:
        return None
    time.sleep(max(0.05, settle_ms / 1000.0))
    img2 = utils._safe_grab(bbox)
    return img2 or img1


class DesktopDiscordProvider(DiscordViewProvider):
    """Runtime adapter reading and staging via the installed Discord desktop client."""

    def __init__(self, expected_actor: Optional[dict[str, Any]] = None) -> None:
        self.expected_actor = expected_actor

    def describe_session(self) -> SessionDescription:
        pid = _find_discord_pid()
        errors: list[dict[str, Any]] = []
        win_title, win_bbox = _get_discord_window_quartz()

        if not win_bbox and utils is not None:
            try:
                _, fg_title, fg_bbox = utils.get_foreground_window_info()
                if fg_title and "discord" in fg_title.lower():
                    win_title = fg_title
                    win_bbox = fg_bbox
            except Exception:
                pass

        if pid is None:
            errors.append({"code": "discord_not_running", "message": "Discord process not found", "thread_id": None})
        elif win_bbox is None:
            errors.append({"code": "discord_window_not_found", "message": "Visible Discord window not found", "thread_id": None})

        can_read_semantic = False
        current_url: Optional[str] = None
        actor: dict[str, Any] = {"user_id": None, "label": "Unverified desktop session"}

        if sys.platform.startswith("darwin") and pid is not None:
            try:
                import ApplicationServices as AS

                trusted = bool(AS.AXIsProcessTrusted())
                app_ref = AS.AXUIElementCreateApplication(pid)
                err, windows = AS.AXUIElementCopyAttributeValue(app_ref, "AXWindows", None)
                if trusted and err == 0 and windows:
                    can_read_semantic = True
                    ax_win = windows[0]
                    ax_title = _ax_attr(ax_win, "AXTitle")
                    if ax_title:
                        win_title = str(ax_title)
                    nodes = _walk_ax_tree(ax_win, max_nodes=3500, max_depth=30)
                    for n in nodes:
                        if n["role"] == "AXWebArea" and n["url"] and parse_discord_channel_url(n["url"]):
                            current_url = n["url"]
                            break
                    if not current_url:
                        for n in nodes:
                            for field_val in (n["url"], n["value"], n["title"], n["description"]):
                                if field_val and parse_discord_channel_url(field_val):
                                    current_url = field_val
                                    break
                            if current_url:
                                break
                    # Check user status panel in AX tree
                    status_skip = {"Unmute", "Mute", "Deafen", "Online", "Idle", "Do Not Disturb", "Invisible"}
                    for n in nodes:
                        if n["role"] == "AXGroup" and n["description"] == "User status and settings":
                            sub = _walk_ax_tree(n["element"], max_nodes=40, max_depth=8)
                            texts = [
                                s["value"].strip()
                                for s in sub
                                if s["role"] == "AXStaticText" and s["value"].strip() and s["value"].strip() not in status_skip
                            ]
                            if texts:
                                actor = {"user_id": None, "label": texts[0]}
                            break
                    if actor.get("label") == "Unverified desktop session":
                        for n in nodes:
                            desc = n["description"]
                            if desc and "user settings" in desc.lower():
                                continue
                            if n["role"] == "AXGroup" and desc and (
                                "online" in desc.lower() or "idle" in desc.lower() or "do not disturb" in desc.lower()
                            ):
                                actor = {"user_id": None, "label": desc.split(",")[0].strip()}
                                break
                else:
                    errors.append(
                        {
                            "code": "ax_untrusted",
                            "message": f"macOS Accessibility API unavailable (AXIsProcessTrusted={trusted}, AXWindows err={err})",
                            "thread_id": None,
                        }
                    )
            except Exception as exc:
                errors.append({"code": "ax_probe_error", "message": str(exc), "thread_id": None})

        can_read_ocr = False
        if win_bbox and utils is not None and hasattr(utils, "_safe_grab"):
            probe_box = (win_bbox[0], win_bbox[1], win_bbox[0] + 32, win_bbox[1] + 32)
            if utils._safe_grab(probe_box) is not None:
                can_read_ocr = True
            else:
                errors.append(
                    {
                        "code": "screen_capture_unavailable",
                        "message": "Screen Recording permission or display capture unavailable for this process",
                        "thread_id": None,
                    }
                )

        can_navigate = sys.platform.startswith(("darwin", "win32", "linux")) and pid is not None
        can_stage = bool(pid is not None and win_bbox is not None and (can_read_semantic or can_read_ocr) and pyautogui is not None)

        return SessionDescription(
            actor=actor,
            app_name="Discord",
            window_title=win_title,
            window_bbox=win_bbox,
            current_url=current_url,
            capabilities={
                "can_read_semantic": can_read_semantic,
                "can_read_ocr": can_read_ocr,
                "can_navigate_deep_link": can_navigate,
                "can_stage_composer": can_stage,
            },
            errors=errors,
        )

    def _get_ax_window(self) -> Optional[Any]:
        if not sys.platform.startswith("darwin"):
            return None
        pid = _find_discord_pid()
        if pid is None:
            return None
        try:
            import ApplicationServices as AS

            if not AS.AXIsProcessTrusted():
                return None
            app_ref = AS.AXUIElementCreateApplication(pid)
            err, windows = AS.AXUIElementCopyAttributeValue(app_ref, "AXWindows", None)
            if err == 0 and windows:
                return windows[0]
        except Exception:
            return None
        return None

    def navigate_to_url(self, url: str) -> bool:
        """Open a canonical discord:// deep link in the existing logged-in Discord client."""
        parsed = parse_discord_channel_url(url)
        if not parsed:
            raise ValueError(f"Refusing to navigate to non-canonical URL: {url!r}")
        gid, cid, mid = parsed
        deep = f"discord://-/channels/{gid}/{cid}" + (f"/{mid}" if mid else "")
        try:
            if sys.platform.startswith("darwin"):
                subprocess.run(["open", deep], check=True)
            elif sys.platform.startswith("win32"):
                subprocess.Popen(["cmd", "/c", "start", "", deep])
            else:
                subprocess.Popen(["xdg-open", deep])
            time.sleep(0.8)
            return True
        except Exception as exc:
            logger.warning("Deep link navigation failed: %s", exc)
            return False

    def describe_forum(self, guild_id: str, forum_id: str) -> ForumObservation:
        url = build_discord_channel_url(guild_id, forum_id)
        session = self.describe_session()
        if session.capabilities.get("can_read_semantic"):
            parsed_cur = parse_discord_channel_url(session.current_url)
            if not parsed_cur or parsed_cur[0] != guild_id or parsed_cur[1] != forum_id:
                self.navigate_to_url(url)
                time.sleep(1.5)
                session = self.describe_session()
            ax_win = self._get_ax_window()
            if ax_win is not None:
                nodes = _walk_ax_tree(ax_win, max_nodes=4000, max_depth=35)
                tags: list[dict[str, Any]] = []
                seen_tags: set[str] = set()
                for n in nodes:
                    desc = n["description"]
                    if n["role"] == "AXCheckBox" and desc.startswith("Filter by tag, "):
                        tname = desc.replace("Filter by tag, ", "", 1).strip()
                        if tname and tname not in seen_tags:
                            seen_tags.add(tname)
                            tags.append({"tag_id": None, "name": tname})

                forum_name = ""
                for n in nodes:
                    if n["role"] == "AXWebArea" and n["title"]:
                        left = n["title"].split("|")[0].strip().lstrip("#").strip()
                        if left:
                            forum_name = left
                            break
                if not forum_name:
                    for n in nodes:
                        if n["role"] == "AXHeading" and n["title"]:
                            if ": " in n["title"]:
                                forum_name = n["title"].split(": ", 1)[1].strip()
                                break
                if not forum_name:
                    forum_name = session.window_title or f"Forum {forum_id}"

                new_post_btn = next(
                    (
                        n
                        for n in nodes
                        if n["role"] == "AXButton" and "new post" in (n["title"] + " " + n["description"]).lower()
                    ),
                    None,
                )
                return ForumObservation(
                    forum_id=forum_id,
                    guild_id=guild_id,
                    name=forum_name,
                    source_url=url,
                    tags=tags,
                    can_create_topic=new_post_btn.get("enabled") if new_post_btn else None,
                    source_method="accessibility",
                    confidence=0.98,
                    raw={"window_title": session.window_title, "tags_count": len(tags)},
                )

        if session.capabilities.get("can_read_ocr") and session.window_bbox and utils is not None:
            l, t, w, h = session.window_bbox
            header_box = (l + 300, t + 28, l + min(w, 850), t + 90)
            img = _capture_settled_region(header_box)
            if img is not None:
                res = utils.ocr_image_structured(img, task="forum_title")
                if res.text:
                    return ForumObservation(
                        forum_id=forum_id,
                        guild_id=guild_id,
                        name=res.text,
                        source_url=url,
                        source_method="ocr",
                        confidence=res.confidence,
                        raw={"ocr_raw": res.raw_text},
                    )

        return ForumObservation(
            forum_id=forum_id,
            guild_id=guild_id,
            name=f"Forum {forum_id}",
            source_url=url,
            source_method="unavailable",
            confidence=None,
            raw={"errors": session.errors},
        )

    def _extract_forum_directory_cards(
        self,
        nodes: list[dict[str, Any]],
        guild_id: str,
        forum_id: str,
    ) -> list[TopicObservation]:
        topics: list[TopicObservation] = []
        seen_tids: set[str] = set()
        # 1. Extract visible forum post cards
        for n in nodes:
            desc = n["description"] or ""
            if n["role"] == "AXButton" and desc.startswith("Post ") and desc != "Post Guidelines":
                parent = _ax_attr(n["element"], "AXParent")
                if parent is None:
                    continue
                p_sub = _walk_ax_tree(parent, max_nodes=60, max_depth=6)
                tid: Optional[str] = None
                title = ""
                for s in p_sub:
                    m = re.match(r"^message-content-(\d+)$", s["dom_id"])
                    if m and m.group(1) != forum_id:
                        tid = m.group(1)
                    if s["role"] == "AXHeading" and s["title"] and not title:
                        title = s["title"].strip()
                if tid and tid not in seen_tids:
                    seen_tids.add(tid)
                    topics.append(
                        TopicObservation(
                            forum_id=forum_id,
                            thread_id=tid,
                            title=title or desc.replace("Post ", "", 1).split(",")[0].strip(),
                            source_url=build_discord_channel_url(guild_id, tid),
                            starter_message_id=tid,
                            source_method="accessibility",
                            confidence=0.98,
                            raw={"card_description": desc},
                        )
                    )

        # 2. Extract canonical thread links inside forum/guidance view (excluding channel sidebar)
        for n in nodes:
            if n["role"] != "AXLink":
                continue
            desc_lower = (n["description"] or "").lower()
            if n["depth"] <= 21 and ("channel)" in desc_lower or "private channel" in desc_lower):
                continue
            for cand in (n["url"], n["value"], n["description"]):
                parsed = parse_discord_channel_url(cand)
                if parsed and parsed[0] == guild_id and parsed[1] != forum_id:
                    tid = parsed[1]
                    if tid in seen_tids:
                        continue
                    seen_tids.add(tid)
                    title = (n["description"] or n["title"] or n["value"] or f"Topic {tid}").strip()
                    topics.append(
                        TopicObservation(
                            forum_id=forum_id,
                            thread_id=tid,
                            title=title,
                            source_url=build_discord_channel_url(guild_id, tid),
                            source_method="accessibility",
                            confidence=0.95,
                            raw={"role": n["role"], "url": cand},
                        )
                    )
        return topics

    def list_topics(
        self,
        guild_id: str,
        forum_id: str,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> TopicListObservation:
        session = self.describe_session()
        topics: list[TopicObservation] = []
        if session.capabilities.get("can_read_semantic"):
            ax_win = self._get_ax_window()
            if ax_win is not None:
                nodes = _walk_ax_tree(ax_win, max_nodes=4500, max_depth=35)
                has_cards = any(
                    n["role"] == "AXButton" and (n["description"] or "").startswith("Post ")
                    for n in nodes
                )
                parsed_cur = parse_discord_channel_url(session.current_url)
                if not has_cards and (not parsed_cur or parsed_cur[1] != forum_id):
                    self.navigate_to_url(build_discord_channel_url(guild_id, forum_id))
                    time.sleep(1.5)
                    ax_win = self._get_ax_window()
                    if ax_win is not None:
                        nodes = _walk_ax_tree(ax_win, max_nodes=4500, max_depth=35)
                topics = self._extract_forum_directory_cards(nodes, guild_id, forum_id)
                return TopicListObservation(
                    topics=topics[:limit],
                    next_cursor=None,
                    complete=False,  # Directory view alone cannot prove archived/unscrolled completeness
                    source_method="accessibility",
                )

        if session.capabilities.get("can_read_ocr") and session.window_bbox and utils is not None:
            l, t, w, h = session.window_bbox
            list_box = (l + 300, t + 110, l + min(w - 20, 950), t + h - 40)
            img = _capture_settled_region(list_box)
            if img is not None:
                res = utils.ocr_image_structured(img, task="forum_title")
                for idx, line in enumerate(res.lines[:limit]):
                    topics.append(
                        TopicObservation(
                            forum_id=forum_id,
                            thread_id=None,
                            observation_ref=f"ocr-topic:{forum_id}:{idx}:{line[:24]}",
                            title=line,
                            source_url=None,
                            source_method="ocr",
                            confidence=res.confidence,
                            raw={"line": line},
                        )
                    )
                return TopicListObservation(
                    topics=topics,
                    next_cursor=None,
                    complete=False,
                    source_method="ocr",
                )

        return TopicListObservation(
            topics=[],
            next_cursor=None,
            complete=False,
            errors=session.errors,
            source_method="unavailable",
        )

    def read_topic(
        self,
        guild_id: str,
        forum_id: str,
        thread_id: str,
        cursor: Optional[str] = None,
    ) -> TopicHistoryObservation:
        session = self.describe_session()
        url = build_discord_channel_url(guild_id, thread_id)
        topic = TopicObservation(
            forum_id=forum_id,
            thread_id=thread_id,
            title=f"Topic {thread_id}",
            source_url=url,
            source_method="unavailable",
            confidence=None,
        )
        if session.capabilities.get("can_read_semantic"):
            ax_win = self._get_ax_window()
            if ax_win is not None:
                nodes = _walk_ax_tree(ax_win, max_nodes=3500, max_depth=28)
                target_prefix = f"chat-messages-{thread_id}-"
                if not any(n["dom_id"].startswith(target_prefix) for n in nodes):
                    self.navigate_to_url(url)
                    time.sleep(1.8)
                    ax_win = self._get_ax_window()
                    if ax_win is not None:
                        nodes = _walk_ax_tree(ax_win, max_nodes=3500, max_depth=28)

                msg_lists = [
                    n for n in nodes
                    if n["role"] == "AXList" and (n["description"] or "").startswith("Messages in ")
                ]
                chosen_list = None
                for ml in msg_lists:
                    children = _ax_attr(ml["element"], "AXChildren") or []
                    if any(str(_ax_attr(c, "AXDOMIdentifier") or "").startswith(target_prefix) for c in children):
                        chosen_list = ml
                        break
                if chosen_list is None and msg_lists:
                    chosen_list = msg_lists[0]

                locked = any(
                    "you do not have permission to send messages" in (n["description"] or "").lower()
                    for n in nodes
                )

                msgs: list[MessageObservation] = []
                topic_tags: list[dict[str, Any]] = []
                if chosen_list is not None:
                    list_title = (chosen_list["description"] or "").replace("Messages in ", "", 1).strip()
                    children = _ax_attr(chosen_list["element"], "AXChildren") or []
                    merged: dict[str, dict[str, Any]] = {}
                    order: list[str] = []
                    last_author: Optional[str] = None

                    for ch in children:
                        ch_dom = str(_ax_attr(ch, "AXDOMIdentifier") or "")
                        m = re.match(r"^chat-messages-(\d+)-(\d+)$", ch_dom)
                        if not m or m.group(1) != thread_id:
                            continue
                        mid = m.group(2)
                        sub = _walk_ax_tree(ch, max_nodes=250, max_depth=12)

                        has_content_box = any(s["dom_id"] == f"message-content-{mid}" for s in sub)
                        if not has_content_box:
                            # Starter tag pills share chat-messages-{tid}-{tid} before the starter message node
                            for s in sub:
                                if s["role"] == "AXStaticText" and s["value"].strip():
                                    tname = s["value"].strip()
                                    if not any(t["name"] == tname for t in topic_tags):
                                        topic_tags.append({"tag_id": None, "name": tname})
                            continue

                        entry = merged.setdefault(
                            mid,
                            {
                                "author": None,
                                "body_parts": [],
                                "timestamps": [],
                                "edited": False,
                                "links": [],
                            },
                        )
                        if mid not in order:
                            order.append(mid)

                        for s in sub:
                            if s["dom_id"] == f"message-username-{mid}":
                                u_sub = _walk_ax_tree(s["element"], max_nodes=25, max_depth=5)
                                for u in u_sub:
                                    if u["role"] == "AXButton" and u["title"].strip():
                                        entry["author"] = u["title"].strip()
                                        last_author = entry["author"]
                                        break
                            elif s["dom_id"] == f"message-content-{mid}":
                                c_sub = _walk_ax_tree(s["element"], max_nodes=180, max_depth=10)
                                for c in c_sub:
                                    if c["role"] == "AXStaticText" and c["value"] == "edited":
                                        entry["edited"] = True
                                        continue
                                    if c["role"] in ("AXStaticText", "AXHeading"):
                                        val = (c["value"] or c["title"]).strip()
                                        if val and val not in ("(", ")"):
                                            entry["body_parts"].append(val)
                                    elif c["role"] == "AXLink" and c["url"]:
                                        entry["links"].append(c["url"])
                            elif s["dom_id"] == f"message-timestamp-{mid}":
                                t_sub = _walk_ax_tree(s["element"], max_nodes=15, max_depth=4)
                                for t in t_sub:
                                    if t["role"] == "AXStaticText" and t["value"].strip():
                                        entry["timestamps"].append(t["value"].strip())

                        if entry["author"] is None and last_author:
                            entry["author"] = last_author

                    topic = TopicObservation(
                        forum_id=forum_id,
                        thread_id=thread_id,
                        title=list_title or f"Topic {thread_id}",
                        source_url=url,
                        tags=topic_tags,
                        starter_message_id=order[0] if order else None,
                        locked=locked,
                        source_method="accessibility",
                        confidence=0.98,
                        raw={"visible_message_count": len(order)},
                    )

                    for mid in order:
                        ent = merged[mid]
                        body = "\n".join(ent["body_parts"]).strip()
                        for link_url in ent["links"]:
                            if link_url and link_url not in body:
                                body = f"{body}\n{link_url}".strip()
                        msgs.append(
                            MessageObservation(
                                thread_id=thread_id,
                                entity_ref=f"msg:{mid}",
                                message_id=mid,
                                author={"user_id": None, "label": ent["author"] or "Unknown"},
                                body=body,
                                source_url=build_discord_channel_url(guild_id, thread_id, mid),
                                identity_verified=False,
                                recognition={"method": "accessibility", "confidence": 0.98},
                                explicit_edited_marker=bool(ent["edited"]),
                                source_method="accessibility",
                                confidence=0.98,
                                raw={"timestamps": ent["timestamps"], "links": ent["links"]},
                            )
                        )

                return TopicHistoryObservation(
                    topic=topic,
                    messages=msgs,
                    next_cursor=None,
                    complete=False,
                )

        return TopicHistoryObservation(
            topic=topic,
            messages=[],
            next_cursor=None,
            complete=False,
            errors=session.errors,
        )

    def read_requirements(
        self,
        guild_id: str,
        forum_id: str,
        references: Optional[list[dict[str, str]]] = None,
    ) -> list[RequirementObservation]:
        reqs: list[RequirementObservation] = []
        ax_win = self._get_ax_window()
        if ax_win is None:
            return reqs

        ref_tids: list[tuple[str, str]] = []
        for ref in references or []:
            tid = ref.get("thread_id")
            rid = ref.get("requirement_id") or (f"forum-guidance-{tid}" if tid else "")
            if tid:
                ref_tids.append((rid, tid))

        if not ref_tids:
            nodes = _walk_ax_tree(ax_win, max_nodes=2500, max_depth=28)
            # Detect pinned/open guidance thread dynamically without hardcoding community IDs
            for n in nodes:
                desc_lower = (n["description"] or "").lower()
                if n["role"] == "AXButton" and n["description"].startswith("Post ") and any(
                    kw in desc_lower for kw in ("read me", "guideline", "guidance", "rule")
                ):
                    parent = _ax_attr(n["element"], "AXParent")
                    if parent is not None:
                        p_sub = _walk_ax_tree(parent, max_nodes=60, max_depth=6)
                        for s in p_sub:
                            m = re.match(r"^message-content-(\d+)$", s["dom_id"])
                            if m and m.group(1) != forum_id:
                                tid = m.group(1)
                                ref_tids.append((f"forum-guidance-{tid}", tid))
                                break
                    if ref_tids:
                        break

        for rid, tid in ref_tids:
            hist = self.read_topic(guild_id, forum_id, tid)
            if hist.messages:
                combined_body = "\n\n".join(m.body for m in hist.messages if m.body).strip()
                if combined_body:
                    reqs.append(
                        RequirementObservation(
                            requirement_id=rid,
                            forum_id=forum_id,
                            body=combined_body,
                            source_entity_ref=f"thread:{tid}",
                            source_url=build_discord_channel_url(guild_id, tid),
                            source_method="accessibility",
                            confidence=0.98,
                            raw={"message_count": len(hist.messages), "title": hist.topic.title},
                        )
                    )
        return reqs

    def _inspect_composer(self, ax_win: Any) -> tuple[Optional[Any], str]:
        nodes = _walk_ax_tree(ax_win, max_nodes=3500, max_depth=28)
        for n in nodes:
            if n["role"] in ("AXTextArea", "AXTextField"):
                desc = (n["description"] + " " + n["title"]).lower()
                if "search" in desc:
                    continue
                if "message" in desc or "reply" in desc or "post" in desc or "thread" in desc:
                    cleaned = (n["value"] or "").replace("\ufeff", "")
                    return n["element"], cleaned
        return None, ""

    def stage_draft(
        self,
        draft_request: dict[str, Any],
        resolved_evidence: list[dict[str, Any]],
        expected_actor: Optional[dict[str, Any]] = None,
    ) -> ComposerStageObservation:
        """Stage multiline Unicode text and evidence into Discord without ever pressing Send."""
        session = self.describe_session()
        dest = dict(draft_request["destination"])

        if session.errors and not session.capabilities.get("can_stage_composer"):
            return ComposerStageObservation(
                status="failed",
                observed_actor=session.actor,
                destination=dest,
                reason="; ".join(e["code"] for e in session.errors),
            )

        if expected_actor:
            exp_uid = expected_actor.get("user_id")
            obs_uid = session.actor.get("user_id")
            exp_lbl = (expected_actor.get("label") or "").strip().lower()
            obs_lbl = (session.actor.get("label") or "").strip().lower()
            if exp_uid and obs_uid and exp_uid != obs_uid:
                return ComposerStageObservation(
                    status="needs_review",
                    observed_actor=session.actor,
                    destination=dest,
                    reason=f"account_mismatch: expected user_id={exp_uid}, observed={obs_uid}",
                )
            if exp_lbl and obs_lbl and obs_lbl != "unverified desktop session" and exp_lbl != obs_lbl:
                return ComposerStageObservation(
                    status="needs_review",
                    observed_actor=session.actor,
                    destination=dest,
                    reason=f"account_mismatch: expected label={expected_actor.get('label')!r}, observed={session.actor.get('label')!r}",
                )

        target_id = dest["thread_id"] or dest["forum_id"]
        target_url = dest.get("source_url") or build_discord_channel_url(dest["guild_id"], target_id)
        if not self.navigate_to_url(target_url):
            return ComposerStageObservation(
                status="failed",
                observed_actor=session.actor,
                destination=dest,
                reason="navigation_failed",
            )

        ax_win = self._get_ax_window()
        if ax_win is None:
            return ComposerStageObservation(
                status="needs_review",
                observed_actor=session.actor,
                destination=dest,
                reason="ax_untrusted_cannot_verify_composer",
            )

        composer_elem, existing_text = self._inspect_composer(ax_win)
        if composer_elem is None:
            return ComposerStageObservation(
                status="needs_review",
                observed_actor=session.actor,
                destination=dest,
                reason="composer_unavailable_or_locked",
            )

        if existing_text.strip():
            return ComposerStageObservation(
                status="needs_review",
                observed_actor=session.actor,
                destination=dest,
                observed_composer_text=existing_text,
                reason="existing_human_draft_present",
            )

        if pyperclip is None or pyautogui is None:
            return ComposerStageObservation(
                status="failed",
                observed_actor=session.actor,
                destination=dest,
                reason="clipboard_or_pyautogui_unavailable",
            )

        # Stage complete multiline text via clipboard paste (never Send / Return)
        pyperclip.copy(draft_request["body"])
        paste_mod = "command" if sys.platform.startswith("darwin") else "ctrl"
        pyautogui.hotkey(paste_mod, "v")
        time.sleep(0.35)

        _, verified_text = self._inspect_composer(ax_win)
        if draft_request["body"].strip() not in verified_text:
            return ComposerStageObservation(
                status="needs_review",
                observed_actor=session.actor,
                destination=dest,
                observed_composer_text=verified_text,
                reason="composer_text_verification_mismatch",
            )

        # NOTE: File attachment upload via OS picker is only invoked when explicitly verified in an interactive session;
        # never fabricate staged attachments if upload preview cannot be confirmed.
        staged_ids = []
        if resolved_evidence:
            return ComposerStageObservation(
                status="needs_review",
                observed_actor=session.actor,
                destination=dest,
                observed_composer_text=verified_text,
                staged_evidence_ids=staged_ids,
                reason="attachment_preview_verification_requires_interactive_upload",
            )

        return ComposerStageObservation(
            status="staged",
            observed_actor=session.actor,
            destination=dest,
            observed_composer_text=verified_text,
            staged_evidence_ids=staged_ids,
        )

    def observe_submission(
        self,
        draft_request: dict[str, Any],
        receipt: dict[str, Any],
    ) -> SubmissionObservation:
        session = self.describe_session()
        return SubmissionObservation(
            status="unresolved",
            observed_actor=session.actor,
            reason="live_submission_not_yet_observed",
        )
