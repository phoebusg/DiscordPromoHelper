# Specs.MD

Repository: DiscordPromoHelper

Purpose:
- Automate respectful self-promotion in Discord servers by detecting promotion channels and posting a preset message with a stream link.
- Respect anti-spam by queueing posts and enforcing per-channel/posting delays.

Core modules and responsibilities:

- `src/config.py`
  - `CHANNEL_KEYWORDS`: list of keywords used to detect promo channels.

- `src/utils.py`
  - `is_tesseract_installed()` / install helpers: detect and install Tesseract OCR on Windows/macOS/Linux.
  - `check_tesseract_path()` / `update_tesseract_cmd()`: ensure `pytesseract` points to a valid binary.
  - `is_discord_running()` / `run_discord()`: detect and launch Discord.
  - `find_and_focus_discord()`: find Discord window and bring it to foreground. (Updated to have cross-platform fallback using `pygetwindow`.)
  - `read_screen_with_tesseract()`: capture screen and OCR to find UI elements.

- `src/promotion_helper.py`
  - `find_channel_position()`: (implemented in `utils`), locate the promo channel in Discord's UI.
  - `post_update()`: type/post message to a given channel position.
  - `queue_updates()`: manages repeated posting with delays to avoid spam.

Immediate issues and tasks (short-term):
1. Window-finding reliability (macOS):
   - Ensure `find_and_focus_discord()` correctly enumerates windows on macOS (uses `pygetwindow` fallback now).
   - Verify macOS accessibility/screen-recording permissions for `Pillow`/`ImageGrab` and `PyAutoGUI`.

2. OCR reliability and paths:
   - Confirm `pytesseract.pytesseract.tesseract_cmd` resolves to actual binary on macOS (Homebrew path: `/opt/homebrew/bin/tesseract` or `/usr/local/bin/tesseract`).
   - Add clear notes for users to install Tesseract and grant screen permissions.

3. Missing behavior to implement to match Windows behavior:
   - Robust window activation when Discord is minimized or hidden (macOS may require `osascript` to bring app to front).
   - Channel detection heuristics: detect the left-sidebar area and locate promo channels via OCR + anchor image matching.

4. Respectful posting policy (requirements):
   - Per-server rate limiting (default: max 1 message per channel per hour).
   - Queue persistence across restarts (optional): store timestamps in a local file/db.
   - Blacklist/whitelist of servers/channels in `config.py`.

Planned next steps:
- [ ] Add macOS-specific activation via `osascript` when `pygetwindow`/`pyautogui` cannot activate the window.
- [ ] Add user-facing `SETUP.md` with required permissions and Tesseract install steps for macOS.
- [ ] Add a lightweight storage (JSON) to track last-post times per channel to avoid over-posting.
- [ ] Manual test on macOS and iterate on timing/screenshot regions for reliable channel detection.

Notes for maintainers:
- Do not attempt to circumvent server anti-spam rules; the tool is intended to be respectful and compliant.
- When testing on macOS, ensure `System Settings -> Privacy & Security -> Screen Recording` grants access to the terminal/Python process.

