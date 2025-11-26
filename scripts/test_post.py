#!/usr/bin/env python3
import sys, os, time
sys.path.insert(0, os.getcwd())
from src import utils, promotion_helper, config

MESSAGE_TITLE = "Test Stream"
MESSAGE_LINK = "https://twitch.tv/test"

def main():
    print('Bringing Discord to front...')
    if not utils.find_discord():
        print('Could not find or focus Discord. Aborting.')
        sys.exit(1)

    print('Searching for promo channel...')
    found = utils.find_channel_position(config.CHANNEL_KEYWORDS)
    if not found:
        print('No promo channel detected.')
        sys.exit(1)

    if isinstance(found, tuple) and len(found) == 3 and isinstance(found[0], tuple):
        channel_position, channel_name, confidence = found
    elif isinstance(found, tuple) and len(found) == 2 and isinstance(found[0], tuple):
        channel_position, channel_name = found
        confidence = 0.6
    else:
        channel_position = found
        channel_name = str(channel_position)
        confidence = 0.0

    print(f"Detected channel: {channel_name} (confidence={confidence:.2f}) at {channel_position}")
    print('Posting test message in 3 seconds. Switch away to cancel.')
    for i in range(3,0,-1):
        print(i)
        time.sleep(1)

    try:
        # Only type the message and do NOT press Enter to avoid sending during testing
        promotion_helper.post_update(MESSAGE_TITLE, MESSAGE_LINK, channel_position, press_enter=False)
        print('Typed test message (not sent).')
    except Exception as e:
        print('Failed to type message:', e)
        sys.exit(1)


if __name__ == '__main__':
    main()
