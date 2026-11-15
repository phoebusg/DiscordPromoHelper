"""Installed-runtime Discord desktop adapter using OS accessibility + OCR fallback."""
from __future__ import annotations

from datetime import datetime
import difflib
import json
import logging
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Optional

from ..models import (
    build_discord_channel_url,
    normalize_text_for_hash,
    parse_discord_channel_url,
    utc_now_iso,
)
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

try:
    import Quartz  # type: ignore
except Exception:
    Quartz = None

_DISCORD_URL_IN_TEXT_RE = re.compile(
    r"(?:https://(?:(?:ptb|canary)\.)?discord\.com|discord://-)/channels/(\d+)/(\d+)(?:/(\d+))?"
)


def _find_discord_pid() -> Optional[int]:
    if psutil is None:
        return None
    try:
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                name = (proc.info.get("name") or "").strip().lower()
                if name == "discord" or name == "discord.exe":
                    return int(proc.info["pid"])
            except Exception:
                continue
    except Exception:
        return None
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


def _ax_bounds(element: Any) -> Optional[tuple[float, float, float, float]]:
    """Extract (x, y, width, height) screen coordinates from an AXUIElement."""
    pos_val = _ax_attr(element, "AXPosition")
    sz_val = _ax_attr(element, "AXSize")
    if pos_val is None or sz_val is None:
        return None
    pos_m = re.search(r"x:\s*([-\d.]+)\s+y:\s*([-\d.]+)", str(pos_val))
    sz_m = re.search(r"w:\s*([-\d.]+)\s+h:\s*([-\d.]+)", str(sz_val))
    if not (pos_m and sz_m):
        return None
    return (
        float(pos_m.group(1)),
        float(pos_m.group(2)),
        float(sz_m.group(1)),
        float(sz_m.group(2)),
    )


def _scroll_at_point(x: float, y: float, delta_lines: int) -> bool:
    """Post a wheel scroll event at screen coordinates (x, y)."""
    if sys.platform.startswith("darwin"):
        try:
            import Quartz

            ev = Quartz.CGEventCreateScrollWheelEvent(
                None, Quartz.kCGScrollEventUnitLine, 1, int(delta_lines)
            )
            if ev is not None:
                Quartz.CGEventSetLocation(ev, (float(x), float(y)))
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                return True
        except Exception:
            pass
    if pyautogui is not None:
        try:
            pyautogui.scroll(int(delta_lines), x=int(x), y=int(y))
            return True
        except Exception:
            pass
    return False


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

    _FORBIDDEN_SEND_KEYS = frozenset({"return", "enter", "numpadenter", "kp_enter", "\r", "\n"})

    def __init__(self, expected_actor: Optional[dict[str, Any]] = None) -> None:
        self.expected_actor = expected_actor
        self._session_cache: Optional[tuple[float, SessionDescription]] = None
        self._ocr_probe_cache: Optional[tuple[float, bool]] = None
        self._unverified_attachment_filenames: list[str] = []

    def _safe_key_press(self, key: str) -> None:
        if key.lower() in self._FORBIDDEN_SEND_KEYS:
            raise RuntimeError(f"Refusing to press forbidden send-triggering key {key!r}")
        if pyautogui is not None:
            pyautogui.press(key)

    def describe_session(self, force_refresh: bool = False) -> SessionDescription:
        now_mono = time.monotonic()
        if (
            not force_refresh
            and self._session_cache is not None
            and (now_mono - self._session_cache[0]) < 2.0
        ):
            return self._session_cache[1]

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
            preflight_blocked = False
            if (
                sys.platform.startswith("darwin")
                and Quartz is not None
                and hasattr(Quartz, "CGPreflightScreenCaptureAccess")
            ):
                try:
                    if not bool(Quartz.CGPreflightScreenCaptureAccess()):
                        preflight_blocked = True
                except Exception:
                    preflight_blocked = False

            if preflight_blocked:
                self._ocr_probe_cache = (time.monotonic(), False)
                errors.append(
                    {
                        "code": "screen_capture_unavailable",
                        "message": "Screen Recording permission unavailable for this process (CGPreflightScreenCaptureAccess=False)",
                        "thread_id": None,
                    }
                )
            else:
                cache_hit: Optional[bool] = None
                if not force_refresh and self._ocr_probe_cache is not None:
                    cached_at, cached_val = self._ocr_probe_cache
                    if (time.monotonic() - cached_at) < 30.0:
                        cache_hit = cached_val
                if cache_hit is not None:
                    can_read_ocr = cache_hit
                else:
                    probe_box = (win_bbox[0], win_bbox[1], win_bbox[0] + 32, win_bbox[1] + 32)
                    can_read_ocr = utils._safe_grab(probe_box) is not None
                    self._ocr_probe_cache = (time.monotonic(), can_read_ocr)
                if not can_read_ocr:
                    errors.append(
                        {
                            "code": "screen_capture_unavailable",
                            "message": "Screen Recording permission or display capture unavailable for this process",
                            "thread_id": None,
                        }
                    )

        can_navigate = sys.platform.startswith(("darwin", "win32", "linux")) and pid is not None
        can_stage = bool(pid is not None and win_bbox is not None and (can_read_semantic or can_read_ocr) and pyautogui is not None)

        desc = SessionDescription(
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
        self._session_cache = (time.monotonic(), desc)
        return desc

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

    def _activate_discord_window(self) -> None:
        if sys.platform.startswith("darwin"):
            try:
                subprocess.run(["open", "-a", "Discord"], check=False)
                time.sleep(0.25)
            except Exception:
                pass
            ax_win = self._get_ax_window()
            if ax_win is not None:
                try:
                    import ApplicationServices as AS

                    AS.AXUIElementPerformAction(ax_win, "AXRaise")
                except Exception:
                    pass

    def _extract_post_cards_and_links(
        self,
        nodes: list[dict[str, Any]],
        guild_id: str,
        forum_id: str,
    ) -> tuple[list[TopicObservation], list[TopicObservation], Optional[Any]]:
        """Extract visible forum post cards, secondary canonical links, and the scrollable AXList container."""
        card_topics: list[TopicObservation] = []
        link_topics: list[TopicObservation] = []
        seen_cards: set[str] = set()
        seen_links: set[str] = set()
        card_list_elem: Optional[Any] = None

        for n in nodes:
            desc = n["description"] or ""
            if n["role"] == "AXButton" and desc.startswith("Post ") and desc != "Post Guidelines":
                parent = _ax_attr(n["element"], "AXParent")
                if parent is None:
                    continue
                if card_list_elem is None:
                    cur = parent
                    for _ in range(8):
                        cur = _ax_attr(cur, "AXParent")
                        if cur is None:
                            break
                        if str(_ax_attr(cur, "AXRole") or "") == "AXList":
                            card_list_elem = cur
                            break
                p_sub = _walk_ax_tree(parent, max_nodes=60, max_depth=6)
                tid: Optional[str] = None
                title = ""
                for s in p_sub:
                    m = re.match(r"^message-content-(\d+)$", s["dom_id"])
                    if m and m.group(1) != forum_id:
                        tid = m.group(1)
                    if s["role"] == "AXHeading" and s["title"] and not title:
                        title = s["title"].strip()
                if tid and tid not in seen_cards:
                    seen_cards.add(tid)
                    card_topics.append(
                        TopicObservation(
                            forum_id=forum_id,
                            thread_id=tid,
                            title=title or desc.replace("Post ", "", 1).split(",")[0].strip(),
                            source_url=build_discord_channel_url(guild_id, tid),
                            starter_message_id=tid,
                            source_method="accessibility",
                            confidence=0.98,
                            raw={"card_description": desc, "from_directory_card": True},
                        )
                    )

        def _card_thread_id(card_elem: Any) -> Optional[str]:
            for s in _walk_ax_tree(card_elem, max_nodes=60, max_depth=6):
                m = re.match(r"^message-content-(\d+)$", s["dom_id"])
                if m and m.group(1) != forum_id:
                    return m.group(1)
            return None

        def _enclosing_forum_post_card_tid(elem: Any) -> tuple[bool, Optional[str]]:
            cur = elem
            for _ in range(6):
                cur = _ax_attr(cur, "AXParent")
                if cur is None:
                    break
                c_role = str(_ax_attr(cur, "AXRole") or "")
                c_desc = str(_ax_attr(cur, "AXDescription") or "")
                if c_role == "AXList" or c_desc.startswith("Messages in ") or c_desc == "Channels":
                    return False, None
                if c_role == "AXButton" and c_desc.startswith("Post ") and c_desc != "Post Guidelines":
                    card_parent = _ax_attr(cur, "AXParent")
                    return True, (_card_thread_id(card_parent) if card_parent is not None else None)
                for sib in (_ax_attr(cur, "AXChildren") or [])[:15]:
                    s_role = str(_ax_attr(sib, "AXRole") or "")
                    s_desc = str(_ax_attr(sib, "AXDescription") or "")
                    if s_role == "AXButton" and s_desc.startswith("Post ") and s_desc != "Post Guidelines":
                        return True, _card_thread_id(cur)
            return False, None

        for n in nodes:
            if n["role"] != "AXLink":
                continue
            desc_lower = (n["description"] or "").lower()
            if "channel)" in desc_lower or "private channel" in desc_lower:
                continue
            in_card, card_tid = _enclosing_forum_post_card_tid(n["element"])
            if not in_card:
                continue
            for cand in (n["url"], n["value"], n["description"]):
                parsed = parse_discord_channel_url(cand)
                if not parsed or parsed[2] is not None:
                    continue
                if parsed[0] == guild_id and parsed[1] != forum_id:
                    tid = parsed[1]
                    if card_tid is not None and tid != card_tid:
                        continue
                    if tid in seen_cards or tid in seen_links:
                        continue
                    seen_links.add(tid)
                    title = (n["description"] or n["title"] or n["value"] or f"Topic {tid}").strip()
                    link_topics.append(
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
        return card_topics, link_topics, card_list_elem

    def _extract_forum_directory_cards(
        self,
        nodes: list[dict[str, Any]],
        guild_id: str,
        forum_id: str,
    ) -> list[TopicObservation]:
        cards, links, _ = self._extract_post_cards_and_links(nodes, guild_id, forum_id)
        return cards + links

    def list_topics(
        self,
        guild_id: str,
        forum_id: str,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> TopicListObservation:
        session = self.describe_session()
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
                    if ax_win is None:
                        return TopicListObservation(
                            topics=[],
                            next_cursor=None,
                            complete=False,
                            errors=session.errors,
                            source_method="unavailable",
                        )
                    nodes = _walk_ax_tree(ax_win, max_nodes=4500, max_depth=35)

                anchor_tid: Optional[str] = None
                if cursor and cursor.startswith("dirscroll:"):
                    anchor_tid = cursor.split(":", 1)[1].strip() or None

                cards, links, list_elem = self._extract_post_cards_and_links(nodes, guild_id, forum_id)
                bounds = _ax_bounds(list_elem) if list_elem is not None else None
                if bounds is not None:
                    scroll_x = bounds[0] + (bounds[2] * 0.5)
                    scroll_y = bounds[1] + (bounds[3] * 0.5)
                elif session.window_bbox is not None:
                    wx, wy, ww, wh = session.window_bbox
                    scroll_x = wx + min(560.0, ww * 0.45)
                    scroll_y = wy + min(500.0, wh * 0.55)
                else:
                    scroll_x, scroll_y = 560.0, 500.0

                # Starting fresh from the top of the directory if user scrolled previously
                if anchor_tid is None and cards and list_elem is not None:
                    _scroll_at_point(scroll_x, scroll_y, 28)
                    time.sleep(0.25)
                    _scroll_at_point(scroll_x, scroll_y, 28)
                    time.sleep(0.35)
                    nodes = _walk_ax_tree(ax_win, max_nodes=4500, max_depth=35)
                    cards, links, _ = self._extract_post_cards_and_links(nodes, guild_id, forum_id)

                collected_cards: dict[str, TopicObservation] = {}
                collected_links: dict[str, TopicObservation] = {t.thread_id: t for t in links if t.thread_id}
                past_anchor = anchor_tid is None
                reached_bottom = False
                stagnant_steps = 0
                prev_last_tid: Optional[str] = None
                max_steps = max(4, (max(1, limit) // 3) + 12)

                for step in range(max_steps):
                    if step > 0:
                        nodes = _walk_ax_tree(ax_win, max_nodes=4500, max_depth=35)
                        cards, step_links, _ = self._extract_post_cards_and_links(nodes, guild_id, forum_id)
                        for lt in step_links:
                            if lt.thread_id and lt.thread_id not in collected_links:
                                collected_links[lt.thread_id] = lt

                    visible_tids = [c.thread_id for c in cards if c.thread_id]
                    before_count = len(collected_cards)
                    for c in cards:
                        if not c.thread_id:
                            continue
                        if not past_anchor:
                            if c.thread_id == anchor_tid:
                                past_anchor = True
                            continue
                        if c.thread_id not in collected_cards:
                            collected_cards[c.thread_id] = c

                    last_visible = visible_tids[-1] if visible_tids else None
                    if not past_anchor and anchor_tid and step >= 2 and not cards:
                        # Anchor card not in active cards (e.g. only link topics present)
                        past_anchor = True

                    if len(collected_cards) >= limit:
                        break

                    if step > 0 and len(collected_cards) == before_count and last_visible == prev_last_tid:
                        if not past_anchor and anchor_tid:
                            # If seeking failed after reaching bottom/stagnation, avoid infinite loop
                            stagnant_steps += 1
                            if stagnant_steps >= 2:
                                reached_bottom = True
                                break
                        else:
                            stagnant_steps += 1
                            if stagnant_steps >= 2:
                                reached_bottom = True
                                break
                    else:
                        stagnant_steps = 0
                    prev_last_tid = last_visible

                    if not _scroll_at_point(scroll_x, scroll_y, -18):
                        break
                    time.sleep(0.4)

                ordered_out: list[TopicObservation] = list(collected_cards.values())
                if reached_bottom and len(ordered_out) < limit:
                    for tid, lt in collected_links.items():
                        if tid not in collected_cards:
                            ordered_out.append(lt)

                if len(ordered_out) > limit:
                    page_topics = ordered_out[:limit]
                    last_tid = page_topics[-1].thread_id
                    return TopicListObservation(
                        topics=page_topics,
                        next_cursor=f"dirscroll:{last_tid}" if last_tid else None,
                        complete=False,
                        source_method="accessibility",
                    )

                if reached_bottom:
                    return TopicListObservation(
                        topics=ordered_out,
                        next_cursor=None,
                        complete=True,
                        source_method="accessibility",
                    )

                last_tid = ordered_out[-1].thread_id if ordered_out else anchor_tid
                return TopicListObservation(
                    topics=ordered_out,
                    next_cursor=f"dirscroll:{last_tid}" if (last_tid and len(ordered_out) >= limit) else None,
                    complete=(len(ordered_out) == 0),
                    source_method="accessibility",
                )

        if session.capabilities.get("can_read_ocr") and session.window_bbox and utils is not None:
            l, t, w, h = session.window_bbox
            list_box = (l + 300, t + 110, l + min(w - 20, 950), t + h - 40)
            img = _capture_settled_region(list_box)
            if img is not None:
                res = utils.ocr_image_structured(img, task="message_body")
                topics: list[TopicObservation] = []
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

    def _parse_messages_from_ax_list(
        self,
        chosen_list: Any,
        guild_id: str,
        thread_id: str,
    ) -> tuple[str, list[dict[str, Any]], dict[str, MessageObservation]]:
        """Extract visible messages, tags, and attachments from an AXList ('Messages in ...')."""
        list_title = str(_ax_attr(chosen_list, "AXDescription") or "").replace("Messages in ", "", 1).strip()
        children = _ax_attr(chosen_list, "AXChildren") or []
        merged: dict[str, dict[str, Any]] = {}
        topic_tags: list[dict[str, Any]] = []
        last_author: Optional[str] = None

        for ch in children:
            ch_dom = str(_ax_attr(ch, "AXDOMIdentifier") or "")
            m = re.match(r"^chat-messages-(\d+)-(\d+)$", ch_dom)
            if not m or m.group(1) != thread_id:
                continue
            mid = m.group(2)
            sub = _walk_ax_tree(ch, max_nodes=250, max_depth=12)

            has_content_box = any(s["dom_id"] == f"message-content-{mid}" for s in sub)
            has_accessories = any(s["dom_id"] == f"message-accessories-{mid}" for s in sub)
            if not has_content_box and not has_accessories:
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
                    "attachments": [],
                    "has_content_node": False,
                    "has_accessories_node": False,
                    "accessories_unparsed": False,
                },
            )

            seen_att_urls: set[str] = set()
            for s in sub:
                if s["dom_id"] == f"message-username-{mid}":
                    u_sub = _walk_ax_tree(s["element"], max_nodes=25, max_depth=5)
                    for u in u_sub:
                        if u["role"] == "AXButton" and u["title"].strip():
                            entry["author"] = u["title"].strip()
                            last_author = entry["author"]
                            break
                elif s["dom_id"] == f"message-content-{mid}":
                    entry["has_content_node"] = True
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
                elif s["dom_id"] == f"message-accessories-{mid}":
                    entry["has_accessories_node"] = True
                    acc_children = _ax_attr(s["element"], "AXChildren") or []
                    a_sub: list[dict[str, Any]] = []
                    for acc_ch in acc_children:
                        ch_dom_id = str(_ax_attr(acc_ch, "AXDOMIdentifier") or "")
                        if ch_dom_id.startswith("message-reactions-"):
                            continue
                        a_sub.extend(_walk_ax_tree(acc_ch, max_nodes=120, max_depth=8))
                    parsed_urls = 0
                    for a in a_sub:
                        cand_url = a["url"] or ""
                        if "cdn.discordapp.com/attachments/" in cand_url or "media.discordapp.net/attachments/" in cand_url:
                            parsed_urls += 1
                            if cand_url not in seen_att_urls:
                                seen_att_urls.add(cand_url)
                                fname = cand_url.split("?")[0].rstrip("/").split("/")[-1] or "attachment"
                                entry["attachments"].append(
                                    {
                                        "attachment_id": None,
                                        "name": fname,
                                        "source_url": cand_url,
                                        "sha256": None,
                                        "local_ref": None,
                                    }
                                )
                    if len(a_sub) > 0 and parsed_urls == 0:
                        entry["accessories_unparsed"] = True

            if entry["author"] is None and last_author:
                entry["author"] = last_author

        msgs_by_mid: dict[str, MessageObservation] = {}
        for mid, ent in merged.items():
            body = "\n".join(ent["body_parts"]).strip()
            for link_url in ent["links"]:
                if link_url and link_url not in body:
                    body = f"{body}\n{link_url}".strip()
            # Keep None when attachments could not be verified (e.g. non-empty
            # accessories without parsed CDN URLs or missing message-content box)
            # so store.upsert_message does not clear previously mirrored files.
            if ent["attachments"]:
                obs_attachments: Optional[list[dict[str, Any]]] = (
                    None if ent["accessories_unparsed"] else ent["attachments"]
                )
            elif ent["accessories_unparsed"] or not ent["has_content_node"]:
                obs_attachments = None
            else:
                obs_attachments = []
            msgs_by_mid[mid] = MessageObservation(
                thread_id=thread_id,
                entity_ref=f"msg:{mid}",
                message_id=mid,
                author={"user_id": None, "label": ent["author"] or "Unknown"},
                body=body,
                source_url=build_discord_channel_url(guild_id, thread_id, mid),
                identity_verified=False,
                attachments=obs_attachments,
                recognition={"method": "accessibility", "confidence": 0.98},
                explicit_edited_marker=bool(ent["edited"]),
                source_method="accessibility",
                confidence=0.98,
                raw={"timestamps": ent["timestamps"], "links": ent["links"]},
            )
        return list_title, topic_tags, msgs_by_mid

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
                    if ax_win is None:
                        return TopicHistoryObservation(
                            topic=topic,
                            messages=[],
                            next_cursor=None,
                            complete=False,
                            errors=session.errors,
                        )
                    nodes = _walk_ax_tree(ax_win, max_nodes=3500, max_depth=28)

                def _find_msg_list(cur_nodes: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
                    msg_lists = [
                        n for n in cur_nodes
                        if n["role"] == "AXList" and (n["description"] or "").startswith("Messages in ")
                    ]
                    for ml in msg_lists:
                        children = _ax_attr(ml["element"], "AXChildren") or []
                        if any(str(_ax_attr(c, "AXDOMIdentifier") or "").startswith(target_prefix) for c in children):
                            return ml
                    return msg_lists[0] if msg_lists else None

                chosen_list = _find_msg_list(nodes)
                locked = any(
                    "you do not have permission to send messages" in (n["description"] or "").lower()
                    for n in nodes
                )

                if chosen_list is None:
                    return TopicHistoryObservation(
                        topic=topic,
                        messages=[],
                        next_cursor=None,
                        complete=False,
                    )

                bounds = _ax_bounds(chosen_list["element"])
                if bounds is not None:
                    scroll_x = bounds[0] + (bounds[2] * 0.5)
                    scroll_y = bounds[1] + (bounds[3] * 0.5)
                elif session.window_bbox is not None:
                    wx, wy, ww, wh = session.window_bbox
                    scroll_x = wx + (ww * 0.65)
                    scroll_y = wy + (wh * 0.55)
                else:
                    scroll_x, scroll_y = 700.0, 500.0

                merged_msgs: dict[str, MessageObservation] = {}
                all_tags: list[dict[str, Any]] = []
                seen_tag_names: set[str] = set()
                list_title = ""

                def _ingest_current_list(ml_elem: Any) -> list[str]:
                    nonlocal list_title
                    t_title, t_tags, batch = self._parse_messages_from_ax_list(ml_elem, guild_id, thread_id)
                    if t_title:
                        list_title = t_title
                    for tg in t_tags:
                        if tg["name"] not in seen_tag_names:
                            seen_tag_names.add(tg["name"])
                            all_tags.append(tg)
                    for mid, obs in batch.items():
                        merged_msgs[mid] = obs
                    return sorted(batch.keys(), key=lambda x: int(x))

                cur_mids = _ingest_current_list(chosen_list["element"])
                top_reached = thread_id in merged_msgs
                stagnant_up = 0
                prev_oldest: Optional[int] = int(cur_mids[0]) if cur_mids else None
                scrolled_up = False

                max_up_steps = 6
                for _step in range(max_up_steps):
                    if top_reached:
                        break
                    if not _scroll_at_point(scroll_x, scroll_y, 20):
                        break
                    scrolled_up = True
                    time.sleep(0.4)
                    step_nodes = _walk_ax_tree(ax_win, max_nodes=3500, max_depth=28)
                    ml_now = _find_msg_list(step_nodes)
                    if ml_now is None:
                        break
                    step_mids = _ingest_current_list(ml_now["element"])
                    cur_oldest = int(step_mids[0]) if step_mids else None
                    if thread_id in merged_msgs:
                        top_reached = True
                        break
                    if cur_oldest is not None and prev_oldest is not None and cur_oldest >= prev_oldest:
                        stagnant_up += 1
                        if stagnant_up >= 2:
                            top_reached = True
                            break
                    else:
                        stagnant_up = 0
                    if cur_oldest is not None:
                        prev_oldest = cur_oldest if prev_oldest is None else min(prev_oldest, cur_oldest)

                # Return to bottom if we scrolled up so newest messages aren't missed
                if scrolled_up:
                    _scroll_at_point(scroll_x, scroll_y, -35)
                    time.sleep(0.25)
                    _scroll_at_point(scroll_x, scroll_y, -35)
                    time.sleep(0.3)
                    end_nodes = _walk_ax_tree(ax_win, max_nodes=3500, max_depth=28)
                    ml_end = _find_msg_list(end_nodes)
                    if ml_end is not None:
                        _ingest_current_list(ml_end["element"])

                ordered_mids = sorted(merged_msgs.keys(), key=lambda x: int(x))
                ordered_msgs = [merged_msgs[m] for m in ordered_mids]
                if cursor and cursor.startswith("msgscroll:up:"):
                    pass  # Full chronological merge covers all newly reached older slices

                topic = TopicObservation(
                    forum_id=forum_id,
                    thread_id=thread_id,
                    title=list_title or f"Topic {thread_id}",
                    source_url=url,
                    tags=all_tags if all_tags else None,
                    starter_message_id=(
                        thread_id
                        if thread_id in merged_msgs
                        else (ordered_mids[0] if (top_reached and ordered_mids) else None)
                    ),
                    locked=locked,
                    source_method="accessibility",
                    confidence=0.98,
                    raw={"visible_message_count": len(ordered_msgs), "top_reached": top_reached},
                )

                return TopicHistoryObservation(
                    topic=topic,
                    messages=ordered_msgs,
                    next_cursor=None if top_reached or not ordered_mids else f"msgscroll:up:{ordered_mids[0]}",
                    complete=bool(top_reached),
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
            combined_body = "\n\n".join(m.body for m in hist.messages if m.body).strip()
            req_complete = bool(hist.complete and not hist.errors)
            if combined_body or hist.errors:
                reqs.append(
                    RequirementObservation(
                        requirement_id=rid,
                        forum_id=forum_id,
                        body=combined_body,
                        source_entity_ref=f"thread:{tid}",
                        source_url=build_discord_channel_url(guild_id, tid),
                        complete=req_complete,
                        errors=list(hist.errors),
                        source_method="accessibility",
                        confidence=0.98 if req_complete else 0.7,
                        raw={"message_count": len(hist.messages), "title": hist.topic.title},
                    )
                )
        return reqs

    @staticmethod
    def _normalize_composer_text(raw: Optional[str]) -> str:
        cleaned = (raw or "").replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
        if cleaned == "\n":
            return ""
        return cleaned

    @staticmethod
    def _composer_text_matches(expected: str, observed: str) -> bool:
        exp_norm = (expected or "").replace("\r\n", "\n").replace("\r", "\n")
        obs_norm = DesktopDiscordProvider._normalize_composer_text(observed)
        if obs_norm == exp_norm:
            return True
        if not exp_norm.endswith("\n") and obs_norm == exp_norm + "\n":
            return True
        return False

    def _inspect_composer(self, ax_win: Any) -> tuple[Optional[Any], str]:
        nodes = _walk_ax_tree(ax_win, max_nodes=3500, max_depth=28)
        fallback: Optional[tuple[Any, str]] = None
        for n in nodes:
            if n["role"] in ("AXTextArea", "AXTextField"):
                desc = (n["description"] + " " + n["title"]).lower()
                if "search" in desc or "title" in desc:
                    continue
                if "send a message" in desc or "message " in desc or "message#" in desc or "reply" in desc:
                    return n["element"], self._normalize_composer_text(n["value"])
                if "message" in desc or "post" in desc or "thread" in desc:
                    if fallback is None:
                        fallback = (n["element"], self._normalize_composer_text(n["value"]))
        if fallback is not None:
            return fallback
        return None, ""

    def _inspect_title_input(self, ax_win: Any) -> tuple[Optional[Any], str]:
        nodes = _walk_ax_tree(ax_win, max_nodes=3500, max_depth=28)
        for n in nodes:
            if n["role"] in ("AXTextField", "AXTextArea"):
                desc = (n["description"] + " " + n["title"]).lower()
                if "search" in desc:
                    continue
                if "title" in desc or "post title" in desc:
                    return n["element"], self._normalize_composer_text(n["value"])
        return None, ""

    def _inspect_staged_attachments(
        self,
        ax_win: Any,
        composer_elem: Optional[Any] = None,
    ) -> list[dict[str, Any]]:
        """Inspect attachment preview items currently staged in Discord's composer."""
        if composer_elem is None and ax_win is not None:
            composer_elem, _ = self._inspect_composer(ax_win)
            if composer_elem is None and _ax_bounds(ax_win) is not None:
                return []
        comp_bounds = _ax_bounds(composer_elem) if composer_elem is not None else None
        # Narrow scan to composer ancestor container (excluding chat message history) when possible
        scan_root = ax_win
        if composer_elem is not None:
            cur = composer_elem
            for _ in range(6):
                parent = _ax_attr(cur, "AXParent")
                if parent is None:
                    break
                p_bounds = _ax_bounds(parent)
                if p_bounds is not None and comp_bounds is not None:
                    # Stay tightly scoped around composer tray (avoid scanning full scroller)
                    if p_bounds[3] > 520 or p_bounds[1] < max(0, comp_bounds[1] - 460):
                        break
                scan_root = parent
                cur = parent

        nodes = _walk_ax_tree(scan_root, max_nodes=1800, max_depth=24)
        items: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for n in nodes:
            if n["role"] in ("AXTextArea", "AXTextField"):
                continue
            parent_el = _ax_attr(n["element"], "AXParent")
            if parent_el is not None and str(_ax_attr(parent_el, "AXRole") or "") in ("AXTextArea", "AXTextField"):
                continue
            dom_id = n["dom_id"] or ""
            if dom_id.startswith("message-content-") or dom_id.startswith("chat-messages-"):
                continue
            bounds = _ax_bounds(n["element"])
            if comp_bounds is not None and bounds is not None:
                # Staged attachments sit immediately above/within composer band
                if bounds[1] + bounds[3] < comp_bounds[1] - 460 or bounds[1] > comp_bounds[1] + comp_bounds[3] + 40:
                    continue
                if bounds[0] + bounds[2] < comp_bounds[0] - 140:
                    continue
            text = " ".join(
                part.strip()
                for part in (n["title"], n["description"], n["value"])
                if part and part.strip()
            )
            if not text and "upload" not in dom_id.lower() and "attach" not in dom_id.lower():
                continue
            lower = text.lower()
            is_remove_btn = n["role"] == "AXButton" and (
                "remove attachment" in lower or "remove file" in lower
            )
            if bounds is not None and (bounds[2] <= 1.0 or bounds[3] <= 0.0) and not is_remove_btn:
                continue
            looks_like_file = bool(
                re.search(r"[A-Za-z0-9._-]+\.[A-Za-z0-9]{1,8}\b", text)
            )
            if is_remove_btn or looks_like_file or "upload" in dom_id.lower() or "attach" in dom_id.lower():
                b_key = f"{round(bounds[0])},{round(bounds[1])}" if bounds is not None else str(len(items))
                key = f"{n['role']}:{text}:{dom_id}:{b_key}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    items.append(
                        {
                            "role": n["role"],
                            "text": text,
                            "dom_id": dom_id,
                            "bounds": bounds,
                            "element": n["element"],
                            "is_remove_button": is_remove_btn,
                        }
                    )
        return items

    @staticmethod
    def _count_staged_attachment_slots(staged_items: list[dict[str, Any]]) -> int:
        remove_btns = [i for i in staged_items if i.get("is_remove_button")]
        if remove_btns:
            return len(remove_btns)
        return len(staged_items)

    @staticmethod
    def _attachment_preview_contains(staged_items: list[dict[str, Any]], filename: str) -> bool:
        target = filename.strip().lower()
        stem = Path(filename).stem.lower()
        for item in staged_items:
            txt = (item.get("text") or "").lower()
            if target and target in txt:
                return True
            if stem and len(stem) >= 4 and stem in txt and ("remove attachment" in txt or "upload" in (item.get("dom_id") or "").lower()):
                return True
        return False

    def _focus_element(self, elem: Any) -> bool:
        self._activate_discord_window()
        focused = False
        try:
            from ApplicationServices import AXUIElementSetAttributeValue, kAXFocusedAttribute  # type: ignore

            err = AXUIElementSetAttributeValue(elem, kAXFocusedAttribute, True)
            if err == 0:
                focused = True
        except Exception:
            pass
        bounds = _ax_bounds(elem)
        if bounds is not None and pyautogui is not None:
            cx = int(bounds[0] + max(12.0, min(bounds[2] * 0.5, 160.0)))
            cy = int(bounds[1] + bounds[3] * 0.5)
            try:
                pyautogui.click(cx, cy)
                time.sleep(0.15)
                focused = True
            except Exception:
                pass
        return focused

    def _stage_single_attachment(
        self,
        ax_win: Any,
        composer_elem: Any,
        file_path: Path,
        filename: str,
    ) -> bool:
        """Stage a single local file into the composer via POSIX clipboard paste without ever pressing Send."""
        resolved_str = str(file_path.resolve())
        paste_mod = "command" if sys.platform.startswith("darwin") else "ctrl"
        baseline_win = self._get_ax_window() or ax_win
        baseline_comp, _ = self._inspect_composer(baseline_win)
        baseline_slots = self._count_staged_attachment_slots(
            self._inspect_staged_attachments(baseline_win, baseline_comp or composer_elem)
        )

        # Strictly non-sending attachment staging: POSIX file reference on clipboard + paste into focused composer
        if sys.platform.startswith("darwin") and pyautogui is not None:
            try:
                escaped = resolved_str.replace("\\", "\\\\").replace('"', '\\"')
                script = f'set the clipboard to POSIX file "{escaped}"'
                subprocess.run(["osascript", "-e", script], check=True, timeout=5.0)
                time.sleep(0.1)
                self._focus_element(composer_elem)
                pyautogui.hotkey(paste_mod, "v")
                deadline = time.monotonic() + 3.5
                retried_paste = False
                step = 0
                while time.monotonic() < deadline:
                    time.sleep(0.35)
                    step += 1
                    cur_win = self._get_ax_window() or ax_win
                    cur_comp, _ = self._inspect_composer(cur_win)
                    items = self._inspect_staged_attachments(cur_win, cur_comp or composer_elem)
                    if self._attachment_preview_contains(items, filename):
                        return True
                    if self._count_staged_attachment_slots(items) > baseline_slots:
                        if filename not in self._unverified_attachment_filenames:
                            self._unverified_attachment_filenames.append(filename)
                        return True
                    if step == 3 and not retried_paste:
                        retried_paste = True
                        subprocess.run(["osascript", "-e", script], check=True, timeout=5.0)
                        time.sleep(0.1)
                        self._focus_element(cur_comp or composer_elem)
                        pyautogui.hotkey(paste_mod, "v")
            except Exception:
                pass
        return False

    def clear_staged_composer(self) -> bool:
        """Clear staged composer text and attachment previews locally without ever pressing Send."""
        ax_win = self._get_ax_window()
        if ax_win is None or pyautogui is None:
            return False
        mod = "command" if sys.platform.startswith("darwin") else "ctrl"
        # First clear composer text so multiline expansion shrinks and reveals full attachment cards
        cur_win = self._get_ax_window() or ax_win
        comp_elem, existing = self._inspect_composer(cur_win)
        if comp_elem is not None and existing:
            self._focus_element(comp_elem)
            pyautogui.hotkey(mod, "a")
            time.sleep(0.1)
            self._safe_key_press("backspace")
            time.sleep(0.25)

        # Then clear staged attachment pills via their Remove attachment buttons
        for _ in range(12):
            cur_win = self._get_ax_window() or ax_win
            comp_elem, _ = self._inspect_composer(cur_win)
            items = self._inspect_staged_attachments(cur_win, comp_elem)
            rem_item = next((i for i in items if i.get("is_remove_button")), None)
            if rem_item is None:
                break
            b = rem_item.get("bounds") or _ax_bounds(rem_item["element"])
            if b is not None:
                self._activate_discord_window()
                time.sleep(0.1)
                cx = int(b[0] + b[2] * 0.5)
                cy = int(b[1] + b[3] * 0.5)
                if hasattr(pyautogui, "moveTo"):
                    pyautogui.moveTo(max(0, cx - 60), cy + 40, duration=0.1)
                    time.sleep(0.12)
                    pyautogui.moveTo(cx, cy, duration=0.1)
                    time.sleep(0.12)
                pyautogui.click(cx, cy)
            else:
                try:
                    from ApplicationServices import AXUIElementPerformAction, kAXPressAction  # type: ignore

                    AXUIElementPerformAction(rem_item["element"], kAXPressAction)
                except Exception:
                    break
            time.sleep(0.3)

        final_win = self._get_ax_window() or ax_win
        final_comp, final_text = self._inspect_composer(final_win)
        final_atts = self._inspect_staged_attachments(final_win, final_comp)
        return not final_text.strip() and not final_atts

    @staticmethod
    def _actor_mismatch_reason(
        expected_actor: Optional[dict[str, Any]],
        observed_actor: dict[str, Any],
    ) -> Optional[str]:
        if not expected_actor:
            return None
        exp_uid = expected_actor.get("user_id")
        obs_uid = observed_actor.get("user_id")
        exp_lbl = (expected_actor.get("label") or "").strip().lower()
        obs_lbl = (observed_actor.get("label") or "").strip().lower()
        unverified_labels = {"", "unverified desktop session", "unknown session", "unknown"}
        placeholder_expected_labels = {"selected account", "current_user_selected_discord_account"}
        if exp_uid:
            if not obs_uid:
                return f"account_unverified: expected user_id={exp_uid}, observed={obs_uid!r}"
            if obs_uid != exp_uid:
                return f"account_mismatch: expected user_id={exp_uid}, observed={obs_uid}"
        if exp_lbl in placeholder_expected_labels:
            if obs_lbl in unverified_labels and not obs_uid:
                return (
                    f"account_unverified: expected label={expected_actor.get('label')!r}, "
                    f"observed={observed_actor.get('label')!r}"
                )
            return None
        if exp_lbl and exp_lbl not in unverified_labels:
            if obs_lbl in unverified_labels:
                if not (exp_uid and obs_uid and exp_uid == obs_uid):
                    return (
                        f"account_unverified: expected label={expected_actor.get('label')!r}, "
                        f"observed={observed_actor.get('label')!r}"
                    )
            elif exp_lbl != obs_lbl and not (exp_uid and obs_uid and exp_uid == obs_uid):
                return (
                    f"account_mismatch: expected label={expected_actor.get('label')!r}, "
                    f"observed={observed_actor.get('label')!r}"
                )
        return None

    def stage_draft(
        self,
        draft_request: dict[str, Any],
        resolved_evidence: list[dict[str, Any]],
        expected_actor: Optional[dict[str, Any]] = None,
    ) -> ComposerStageObservation:
        """Stage multiline Unicode text and evidence into Discord without ever pressing Send."""
        self._unverified_attachment_filenames = []
        session = self.describe_session()
        dest = dict(draft_request["destination"])
        action = draft_request.get("action")

        if session.errors and not session.capabilities.get("can_stage_composer"):
            return ComposerStageObservation(
                status="failed",
                observed_actor=session.actor,
                destination=dest,
                reason="; ".join(e["code"] for e in session.errors),
            )

        if action != "reply_existing":
            return ComposerStageObservation(
                status="needs_review",
                observed_actor=session.actor,
                destination=dest,
                reason=f"unsupported_stage_action:{action}",
            )

        eff_expected_actor = expected_actor or self.expected_actor
        actor_err = self._actor_mismatch_reason(eff_expected_actor, session.actor)
        if actor_err:
            return ComposerStageObservation(
                status="needs_review",
                observed_actor=session.actor,
                destination=dest,
                reason=actor_err,
            )

        expected_guild = dest.get("guild_id")
        target_id = dest.get("thread_id")
        if not expected_guild or not target_id:
            return ComposerStageObservation(
                status="needs_review",
                observed_actor=session.actor,
                destination=dest,
                reason="missing_destination_ids",
            )
        raw_dest_url = dest.get("source_url")
        if raw_dest_url:
            parsed_dest = parse_discord_channel_url(raw_dest_url)
            if (
                parsed_dest is None
                or parsed_dest[0] != str(expected_guild)
                or parsed_dest[1] != str(target_id)
                or parsed_dest[2] is not None
            ):
                return ComposerStageObservation(
                    status="needs_review",
                    observed_actor=session.actor,
                    destination=dest,
                    reason=f"invalid_destination_source_url:{raw_dest_url}",
                )
        target_url = raw_dest_url or build_discord_channel_url(str(expected_guild), str(target_id))
        if not self.navigate_to_url(target_url):
            return ComposerStageObservation(
                status="failed",
                observed_actor=session.actor,
                destination=dest,
                reason="navigation_failed",
            )

        # Allow client route transition to settle before verifying destination/account/composer
        ax_win = None
        composer_elem = None
        existing_text = ""
        for _ in range(4):
            time.sleep(0.3)
            ax_win = self._get_ax_window()
            if ax_win is not None:
                composer_elem, existing_text = self._inspect_composer(ax_win)
                if composer_elem is not None:
                    break

        post_session = self.describe_session(force_refresh=True)
        post_actor_err = self._actor_mismatch_reason(eff_expected_actor, post_session.actor)
        if post_actor_err:
            return ComposerStageObservation(
                status="needs_review",
                observed_actor=post_session.actor,
                destination=dest,
                reason=post_actor_err,
            )

        if ax_win is None:
            return ComposerStageObservation(
                status="needs_review",
                observed_actor=post_session.actor,
                destination=dest,
                reason="ax_untrusted_cannot_verify_composer",
            )

        win_nodes = _walk_ax_tree(ax_win, max_nodes=3500, max_depth=28)
        msg_thread_ids: set[str] = set()
        for n in win_nodes:
            m = re.match(r"^chat-messages-(\d+)-", n["dom_id"] or "")
            if m:
                msg_thread_ids.add(m.group(1))

        parsed_url = parse_discord_channel_url(post_session.current_url)
        if parsed_url is None:
            return ComposerStageObservation(
                status="needs_review",
                observed_actor=post_session.actor,
                destination=dest,
                reason="destination_url_unverified",
            )
        obs_guild, obs_chan, _ = parsed_url
        expected_forum = dest.get("forum_id")
        chan_matches = obs_chan == str(target_id) or (
            bool(expected_forum)
            and obs_chan == str(expected_forum)
            and str(target_id) in msg_thread_ids
        )
        if obs_guild != str(expected_guild) or not chan_matches:
            return ComposerStageObservation(
                status="needs_review",
                observed_actor=post_session.actor,
                destination=dest,
                reason=f"destination_url_mismatch: expected={target_url}, observed={post_session.current_url}",
            )

        if msg_thread_ids and str(target_id) not in msg_thread_ids:
            return ComposerStageObservation(
                status="needs_review",
                observed_actor=post_session.actor,
                destination=dest,
                reason="destination_thread_dom_mismatch",
            )

        if composer_elem is None:
            return ComposerStageObservation(
                status="needs_review",
                observed_actor=post_session.actor,
                destination=dest,
                reason="composer_unavailable_or_locked",
            )

        baseline_attachments = self._inspect_staged_attachments(ax_win, composer_elem)
        if existing_text.strip() or baseline_attachments:
            return ComposerStageObservation(
                status="needs_review",
                observed_actor=post_session.actor,
                destination=dest,
                observed_composer_text=existing_text,
                reason="existing_human_draft_present",
            )

        if pyperclip is None or pyautogui is None:
            return ComposerStageObservation(
                status="failed",
                observed_actor=post_session.actor,
                destination=dest,
                reason="clipboard_or_pyautogui_unavailable",
            )

        prior_clipboard: Optional[str] = None
        try:
            prior_clipboard = pyperclip.paste()
        except Exception:
            prior_clipboard = None

        paste_mod = "command" if sys.platform.startswith("darwin") else "ctrl"
        verified_text = ""
        staged_ids: list[str] = []
        try:
            if draft_request.get("action") == "create_topic" and draft_request.get("title"):
                title_elem, title_existing = self._inspect_title_input(ax_win)
                if title_elem is not None and not title_existing.strip():
                    self._focus_element(title_elem)
                    pyperclip.copy(draft_request["title"])
                    pyautogui.hotkey(paste_mod, "v")
                    time.sleep(0.2)

            # Focus body composer explicitly and paste exact multiline Unicode text (NEVER press Return/Send)
            self._focus_element(composer_elem)
            pyperclip.copy(draft_request["body"])
            pyautogui.hotkey(paste_mod, "v")
            time.sleep(0.35)

            latest_win = self._get_ax_window() or ax_win
            latest_comp, verified_text = self._inspect_composer(latest_win)
            if not self._composer_text_matches(draft_request["body"], verified_text):
                return ComposerStageObservation(
                    status="needs_review",
                    observed_actor=post_session.actor,
                    destination=dest,
                    observed_composer_text=verified_text,
                    reason="composer_text_verification_mismatch",
                )

            for ev in resolved_evidence:
                ev_id = ev["evidence_id"]
                ev_name = ev["name"]
                ev_path = Path(ev.get("resolved_path") or "")
                if not ev_path.is_file():
                    return ComposerStageObservation(
                        status="needs_review",
                        observed_actor=post_session.actor,
                        destination=dest,
                        observed_composer_text=verified_text,
                        staged_evidence_ids=staged_ids,
                        reason=f"missing_resolved_attachment:{ev_id}",
                    )
                cur_win = self._get_ax_window() or latest_win
                cur_comp, _ = self._inspect_composer(cur_win)
                ok = self._stage_single_attachment(cur_win, cur_comp or latest_comp or composer_elem, ev_path, ev_name)
                if not ok:
                    return ComposerStageObservation(
                        status="needs_review",
                        observed_actor=post_session.actor,
                        destination=dest,
                        observed_composer_text=verified_text,
                        staged_evidence_ids=staged_ids,
                        reason=f"attachment_preview_unverified:{ev_name}",
                    )
                staged_ids.append(ev_id)

            # Re-verify composer text once more after attachments finish mounting
            final_win = self._get_ax_window() or latest_win
            _, verified_text = self._inspect_composer(final_win)
            if not self._composer_text_matches(draft_request["body"], verified_text):
                return ComposerStageObservation(
                    status="needs_review",
                    observed_actor=post_session.actor,
                    destination=dest,
                    observed_composer_text=verified_text,
                    staged_evidence_ids=staged_ids,
                    reason="composer_text_changed_after_attachments",
                )

            return ComposerStageObservation(
                status="staged",
                observed_actor=post_session.actor,
                destination=dest,
                observed_composer_text=verified_text,
                staged_evidence_ids=staged_ids,
                reason=(
                    "attachment_slot_count_observed_filename_unverified"
                    if self._unverified_attachment_filenames
                    else None
                ),
            )
        finally:
            if prior_clipboard is not None:
                try:
                    pyperclip.copy(prior_clipboard)
                except Exception:
                    pass

    @staticmethod
    def _actors_compatible(actor_a: Optional[dict[str, Any]], actor_b: Optional[dict[str, Any]]) -> bool:
        if not actor_a or not actor_b:
            return True
        uid_a = actor_a.get("user_id")
        uid_b = actor_b.get("user_id")
        if uid_a and uid_b:
            return uid_a == uid_b
        lbl_a = (actor_a.get("label") or "").strip().lower()
        lbl_b = (actor_b.get("label") or "").strip().lower()
        if not lbl_a or not lbl_b or lbl_a == "unverified desktop session" or lbl_b == "unverified desktop session":
            return True
        return lbl_a == lbl_b or lbl_a in lbl_b or lbl_b in lbl_a

    @staticmethod
    def _score_submission_match(
        draft_body: str,
        observed_body: str,
        expected_filenames: list[str],
        observed_attachments: Optional[list[dict[str, Any]]],
    ) -> float:
        import difflib
        from ..models import normalize_text_for_hash

        norm_d = normalize_text_for_hash(draft_body or "")
        norm_o = normalize_text_for_hash(observed_body or "")
        if not norm_d and not expected_filenames:
            return 0.0
        if norm_d and norm_o == norm_d:
            base = 1.0
        else:
            seq = difflib.SequenceMatcher(None, norm_d, norm_o).ratio() if (norm_d and norm_o) else 0.0
            tokens_d = {w for w in re.findall(r"[A-Za-z0-9_]+", norm_d.lower()) if len(w) >= 2}
            tokens_o = {w for w in re.findall(r"[A-Za-z0-9_]+", norm_o.lower()) if len(w) >= 2}
            jac = (len(tokens_d & tokens_o) / len(tokens_d | tokens_o)) if (tokens_d and tokens_o) else 0.0
            base = max(seq, 0.55 * seq + 0.45 * jac)

        if expected_filenames:
            obs_att_text = " ".join(
                f"{a.get('name', '')} {a.get('url', '')}" for a in (observed_attachments or [])
            ).lower()
            matched_files = sum(
                1 for fn in expected_filenames if fn.lower() in obs_att_text or Path(fn).stem.lower() in obs_att_text
            )
            file_score = matched_files / len(expected_filenames)
            base = min(1.0, base + 0.25 * file_score)
        return base

    def observe_submission(
        self,
        draft_request: dict[str, Any],
        receipt: dict[str, Any],
    ) -> SubmissionObservation:
        """Read-only observation of whether a staged draft was submitted (possibly edited by user)."""
        session = self.describe_session()
        if session.errors and not (
            session.capabilities.get("can_read_semantic") or session.capabilities.get("can_read_ocr")
        ):
            return SubmissionObservation(
                status="failed",
                observed_actor=session.actor,
                reason="; ".join(e["code"] for e in session.errors),
            )

        receipt_actor = receipt.get("actor")
        if receipt_actor and not self._actors_compatible(receipt_actor, session.actor):
            return SubmissionObservation(
                status="unresolved",
                observed_actor=session.actor,
                reason="actor_mismatch",
            )

        dest = dict(draft_request.get("destination") or {})
        guild_id = dest.get("guild_id")
        forum_id = dest.get("forum_id")
        thread_id = dest.get("thread_id")
        action = draft_request.get("action")
        draft_body = draft_request.get("body") or ""
        expected_filenames = [
            ev["name"] for ev in (draft_request.get("attachments") or draft_request.get("evidence") or [])
            if isinstance(ev, dict) and ev.get("name")
        ]
        baseline_id = receipt.get("baseline_latest_message_id")

        if action == "create_topic" and not thread_id and guild_id and forum_id:
            tlist = self.list_topics(guild_id, forum_id)
            title = (draft_request.get("title") or "").strip().lower()
            candidates: list[tuple[float, str]] = []
            for top in tlist.topics:
                if not top.thread_id:
                    continue
                t_lower = (top.title or "").strip().lower()
                if title and (t_lower == title or title in t_lower or t_lower in title):
                    score = 1.0 if t_lower == title else 0.75
                    candidates.append((score, top.thread_id))
            candidates.sort(reverse=True)
            if not candidates:
                return SubmissionObservation(
                    status="unresolved",
                    observed_actor=session.actor,
                    reason="created_topic_not_observed",
                )
            thread_id = candidates[0][1]

        if not guild_id or not forum_id or not thread_id:
            return SubmissionObservation(
                status="unresolved",
                observed_actor=session.actor,
                reason="missing_destination_ids",
            )

        hist = self.read_topic(guild_id, forum_id, thread_id)
        if hist.errors and not hist.messages:
            return SubmissionObservation(
                status="failed",
                observed_actor=session.actor,
                reason="; ".join(e["code"] for e in hist.errors),
            )

        target_mid_raw = dest.get("message_id") or dest.get("target_message_id")
        if action == "edit_own_message" and target_mid_raw:
            target_mid = str(target_mid_raw)
            target_msg = next((m for m in hist.messages if m.message_id == target_mid), None)
            if target_msg is None:
                return SubmissionObservation(
                    status="unresolved",
                    observed_actor=session.actor,
                    reason="target_message_not_visible",
                )
            score = self._score_submission_match(
                draft_body, target_msg.body, expected_filenames, target_msg.attachments
            )
            from ..models import normalize_text_for_hash

            is_exact_or_marked = bool(
                target_msg.explicit_edited_marker
                or target_msg.remote_edited_at
                or normalize_text_for_hash(target_msg.body) == normalize_text_for_hash(draft_body)
            )
            if is_exact_or_marked and (score >= 0.42 or target_msg.explicit_edited_marker or target_msg.remote_edited_at):
                return SubmissionObservation(
                    status="submission_observed",
                    observed_actor=session.actor,
                    message_id=target_msg.message_id,
                    message_url=target_msg.source_url or build_discord_channel_url(guild_id, thread_id),
                    observed_body=target_msg.body,
                    reason="edited_target_message_observed",
                )
            return SubmissionObservation(
                status="unresolved",
                observed_actor=session.actor,
                reason="target_edit_not_observed",
            )

        def _parse_iso_ts(raw: Optional[str]) -> Optional[datetime]:
            if not raw or not isinstance(raw, str):
                return None
            try:
                return datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            except Exception:
                return None

        preexisting_ids = {
            str(x)
            for x in (receipt.get("_preexisting_message_ids") or receipt.get("preexisting_message_ids") or [])
            if x is not None
        }
        draft_created_dt = _parse_iso_ts(draft_request.get("created_at"))

        scored: list[tuple[float, MessageObservation]] = []
        for msg in hist.messages:
            if not msg.message_id:
                continue
            if str(msg.message_id) in preexisting_ids:
                continue
            if baseline_id and str(baseline_id).isdigit() and msg.message_id.isdigit():
                if int(msg.message_id) <= int(str(baseline_id)):
                    continue
            msg_dt = _parse_iso_ts(msg.remote_created_at)
            if msg_dt is not None and draft_created_dt is not None:
                if msg_dt.tzinfo is not None and draft_created_dt.tzinfo is not None and msg_dt < draft_created_dt:
                    continue
            if not self._actors_compatible(receipt_actor or session.actor, msg.author):
                continue
            score = self._score_submission_match(
                draft_body, msg.body, expected_filenames, msg.attachments
            )
            if score >= 0.42:
                scored.append((score, msg))

        if scored:
            scored.sort(key=lambda item: (item[0], int(item[1].message_id) if (item[1].message_id or "").isdigit() else 0), reverse=True)
            top_score, top_msg = scored[0]
            if len(scored) > 1 and abs(scored[1][0] - top_score) < 0.05 and top_score < 0.9:
                return SubmissionObservation(
                    status="unresolved",
                    observed_actor=session.actor,
                    reason="ambiguous_submission_candidates",
                )
            return SubmissionObservation(
                status="submission_observed",
                observed_actor=session.actor,
                message_id=top_msg.message_id,
                message_url=top_msg.source_url or build_discord_channel_url(guild_id, thread_id),
                observed_body=top_msg.body,
                reason="matched_posted_message",
            )

        # If not posted yet, inspect composer locally to report whether draft is still under human review
        ax_win = self._get_ax_window()
        if ax_win is not None:
            comp_elem, comp_text = self._inspect_composer(ax_win)
            comp_atts = self._inspect_staged_attachments(ax_win, comp_elem)
            if comp_text.strip() or comp_atts:
                return SubmissionObservation(
                    status="in_discord_review",
                    observed_actor=session.actor,
                    reason="composer_still_contains_unsubmitted_draft",
                )

        return SubmissionObservation(
            status="unresolved",
            observed_actor=session.actor,
            reason="matching_submission_not_observed_yet",
        )
