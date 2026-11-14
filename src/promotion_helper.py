# promotion_helper.py
import time
from datetime import datetime, timedelta
from typing import Optional
import pyautogui
from .config import CHANNEL_KEYWORDS, POST_COOLDOWN_MINUTES, PERSISTENCE_FILE
from .utils import find_channel_position, find_and_focus_discord, get_foreground_window_info
from .storage import can_post, update_timestamp

def post_update(title, link, channel_position, press_enter=True):
    """Post (or type) the update in the specified self-promo channel.

    If `press_enter` is False the function will only type the message without
    sending it (no Enter). The function attempts to ensure Discord is the
    active application and that the promo channel is selected before typing.
    """
    # Ensure Discord is active; try to bring it forward if not
    try:
        try:
            import pygetwindow as gw
            active = None
            try:
                aw = gw.getActiveWindow()
                active = getattr(aw, 'title', None)
            except Exception:
                active = None
            if not active or 'discord' not in (active or '').lower():
                find_and_focus_discord()
        except Exception:
            # Fallback: attempt to bring Discord to front using existing helper
            find_and_focus_discord()
    except Exception:
        pass

    # Click the channel position to ensure the channel is selected
    pyautogui.click(channel_position)
    time.sleep(0.8)  # short pause for UI to update

    message = f"Check out my latest stream: {title} {link}"
    pyautogui.typewrite(message)
    if press_enter:
        pyautogui.press('enter')

def _collect_allowed_channels(servers):
    """Collect normalized channel names from filtered server configs."""
    allowed = set()
    has_unconfigured = False
    for srv in servers:
        channels = srv.get("promo_channels") or []
        if channels:
            for ch in channels:
                norm = (ch or "").strip().lstrip("#").lower()
                if norm:
                    allowed.add(norm)
        else:
            has_unconfigured = True
    return allowed, has_unconfigured


def _parse_server_from_window_title(title: Optional[str]) -> Optional[str]:
    """Extract the active Discord server name from a window title like '#ch | Server - Discord'."""
    text = (title or "").strip()
    if not text or "discord" not in text.lower():
        return None
    if text.lower().endswith(" - discord"):
        text = text[: -len(" - discord")].strip()
    elif text.lower().endswith("| discord"):
        text = text[: -len("| discord")].strip()
    if "|" in text:
        # Format is typically '#channel | Server Name'
        parts = [p.strip() for p in text.split("|") if p.strip()]
        if len(parts) >= 2:
            return parts[-1]
    return text or None


def _get_active_server_name() -> Optional[str]:
    """Return the currently visible Discord server name, or None if unverified."""
    try:
        import pygetwindow as gw

        aw = gw.getActiveWindow()
        title = getattr(aw, "title", None)
        parsed = _parse_server_from_window_title(title)
        if parsed:
            return parsed
    except Exception:
        pass
    try:
        _, fg_title, _ = get_foreground_window_info()
        return _parse_server_from_window_title(fg_title)
    except Exception:
        return None


def _is_channel_allowed_for_active_server(
    channel_name: str,
    servers: list[dict],
    active_server_name: Optional[str],
) -> bool:
    """Verify the active Discord server matches a filtered server entry and allows channel_name."""
    active_norm = (active_server_name or "").strip().lower()
    if not active_norm:
        return False
    matched_servers = [
        srv for srv in servers if (srv.get("name") or "").strip().lower() == active_norm
    ]
    if not matched_servers:
        return False

    norm_found = (channel_name or "").strip().lstrip("#").lower()
    for srv in matched_servers:
        channels = [
            (ch or "").strip().lstrip("#").lower()
            for ch in (srv.get("promo_channels") or [])
            if (ch or "").strip().lstrip("#")
        ]
        if not channels:
            return True
        if norm_found in channels or any(c in norm_found for c in channels):
            return True
    return False


def queue_updates(title, link, interval_minutes=60, run_duration_hours=1, servers=None):
    """Queue updates to manage posting frequency."""
    if servers is not None and len(servers) == 0:
        return

    if servers is not None:
        allowed_channels, allow_keyword_fallback = _collect_allowed_channels(servers)
        if not allowed_channels and not allow_keyword_fallback:
            return

    end_time = datetime.now() + timedelta(hours=run_duration_hours)
    while datetime.now() < end_time:
        found = find_channel_position(CHANNEL_KEYWORDS)
        if found:
            # `find_channel_position` now returns ((x,y), channel_name, confidence) or None
            if isinstance(found, tuple) and len(found) == 3 and isinstance(found[0], tuple):
                channel_position, channel_name, confidence = found
            elif isinstance(found, tuple) and len(found) == 2 and isinstance(found[0], tuple):
                channel_position, channel_name = found
                confidence = 0.6
            else:
                channel_position = found
                channel_name = str(channel_position)
                confidence = 0.0

            if servers is not None:
                active_server = _get_active_server_name()
                if not _is_channel_allowed_for_active_server(channel_name, servers, active_server):
                    print(
                        f"Skipping '{channel_name}': active Discord server {active_server!r} "
                        "does not verify filtered target server/channel."
                    )
                    time.sleep(interval_minutes * 60)
                    continue

            # Respect whitelist/blacklist
            try:
                from .storage import is_channel_allowed
            except Exception:
                try:
                    from storage import is_channel_allowed
                except Exception:
                    is_channel_allowed = lambda n: True

            if not is_channel_allowed(channel_name):
                print(f"Channel '{channel_name}' is not allowed by whitelist/blacklist.")
            else:
                if can_post(channel_name, cooldown_minutes=interval_minutes, path=PERSISTENCE_FILE):
                    post_update(title, link, channel_position)
                    update_timestamp(channel_name, path=PERSISTENCE_FILE)
                else:
                    print(f"Skipping {channel_name}: cooldown in effect.")

        time.sleep(interval_minutes * 60)  # Interval between posts

