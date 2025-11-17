# promotion_helper.py
import pyautogui
import time
from datetime import datetime, timedelta
from .config import CHANNEL_KEYWORDS, POST_COOLDOWN_MINUTES, PERSISTENCE_FILE
from .utils import find_channel_position, find_and_focus_discord
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

def queue_updates(title, link, interval_minutes=60, run_duration_hours=1):
    """Queue updates to manage posting frequency."""
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

            # Respect whitelist/blacklist
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

