# promotion_helper.py
import pyautogui
import time
from datetime import datetime, timedelta
from config import CHANNEL_KEYWORDS
from utils import find_channel_position

def post_update(title, link, channel_position):
    """Post the update in the specified self-promo channel."""
    pyautogui.click(channel_position)
    time.sleep(2)  # Adjust based on your system's responsiveness

    message = f"Check out my latest stream: {title} {link}"
    pyautogui.typewrite(message)
    pyautogui.press('enter')

def queue_updates(title, link, interval_minutes=60, run_duration_hours=1):
    """Queue updates to manage posting frequency."""
    end_time = datetime.now() + timedelta(hours=run_duration_hours)
    while datetime.now() < end_time:
        channel_position = find_channel_position(CHANNEL_KEYWORDS)
        if channel_position:
            post_update(title, link, channel_position)
        time.sleep(interval_minutes * 60)  # Interval between posts

