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

Server list scanning: deterministic nested scan (Plan)
--------------------------------------------------
Goal: reliable, deterministic scan of Discord's left server column that:
- Starts explicitly at the first server icon after the DM/home icon.
- Visits every visible server icon top→bottom.
- Hovers each icon and OCRs its tooltip. Do not proceed to the next icon until a non-empty normalized name is read (or an explicit stop).
- After scanning all visible icons, scroll so that the previous last visible icon becomes the new top.
- Repeat until the viewport stabilizes at the bottom.

Design decisions and rationale:
- Determinism: Avoid heuristics that rely on start position or fragile anchors. Instead, use a precise top→bottom order centered on computed icon centers.
- Blocking OCR per-icon: Enforce strict correctness; the scanner must not skip unread icons. A bounded retry cap can be used to avoid indefinite stalls.
- UI label blacklist: Recognize and ignore non-server UI labels (DMs, Nitro, Explore Public Servers, etc.). These entries should be recognized and not treated as servers.
- Thumbnail deduplication: Save icon thumbnails and avoid re-adding duplicate icons across runs.

Implementation steps:
1. Replace the current scanning loop with a deterministic algorithm in `capture_discord_servers`:
   - Seek to the top of the server column via repeated small upward scrolls until viewport fingerprint stabilizes.
   - Build the list of center Y positions for the current viewport (use vertical projection as currently implemented).
   - Detect which center corresponds to the DM/Home icon by hovering the centers and OCRing. Set the first server index to be the center immediately following the DM/Home center.
   - For each center from first server → last visible:
     - Ensure an icon is present via `_is_icon_at` check before hovering.
     - Hover, wait, and OCR until a valid name is returned or a bounded retry count is reached.
     - If the OCR indicates a UI label or matches blacklist keywords, do NOT add a server record; mark the center as processed.
     - Otherwise, save icon PNG, update thumbnail dedupe data, save metadata immediately to `servers.json`, and mark center processed.
   - After all visible centers are processed, perform a scroll that moves the last processed icon to the viewport top (make minimal scroll to avoid skipping icons).
   - Continue until fingerprint repeats for 3-4 iterations with no new servers found.

2. Safety and robustness:
   - Add a bounded retry for the per-icon OCR: by default 120 attempts (approx 10–20s), but this can be set via `max_icon_retries` in call signature.
   - Add `should_stop_scan()` sentinel checks frequently to allow graceful aborts.
   - Make the scroll calculation relative to the viewport rather than absolute amounts — avoid hard-coded `scroll_amount` values when possible.
   - Add small jitter while hovering to provoke tooltips (left→right jitter and micro-scroll if necessary).

3. Tests & validation:
   - Create a test harness (`scripts/test_full_scan.py`) to run the scan and store output in `data/test_scan/`.
   - Validate scanning behavior using a visible Discord window: confirm `servers.json` contains non-empty names for all visible servers.
   - Generate `viewport_history.json` showing per-viewport OCR sets; verify monotonic coverage from top→bottom and no missing icons.
   - Integration acceptance: run multiple times with different initial scrollbar positions and confirm the output is identical.

4. Cleanup & risk control:
   - Remove leftover debug screenshot flows and non-essential heuristics that conflict with the deterministic nested-for approach.
   - Retain low-level helpers (`_get_viewport_centers`, `find_and_focus_discord`, `_is_icon_at`, `_hover_and_read`) to keep code modular.
   - Provide a bounded retry cap and fail-safe to avoid indefinite hangs if a tooltip never appears.

Acceptance criteria:
- Determinism: Starting anywhere in the server list, the scan produces the same `servers.json` result for the same Discord server order.
- Completeness: All visible server icons get a non-empty `name` or a recorded icon with blank name marked for later repair.
- Stability: The scan terminates when viewport fingerprint repeats 3–4 times without new servers.
- Usability: `scripts/test_full_scan.py` runs end-to-end on macOS with appropriate permissions.

Next short-term tasks:
- [ ] Add `max_icon_retries` argument to `capture_discord_servers` and expose it via `scripts/test_full_scan.py`.
- [ ] Add a preflight check for macOS Screen Recording permission and `tesseract` availability.
- [ ] Run an instrumented scan in a visible Discord session, capture `viewport_history.json`, and verify coverage.
- [ ] If repeated stable fingerprints prevent reaching top/bottom, implement a scrollbar-drag fallback to move the scroll thumb to the extremes.

Notes for maintainers:
- Do not attempt to circumvent server anti-spam rules; the tool is intended to be respectful and compliant.
- When testing on macOS, ensure `System Settings -> Privacy & Security -> Screen Recording` grants access to the terminal/Python process.

Server Navigation: Name-Based Identification (Design)
------------------------------------------------------
**Problem**: Discord adds new servers at TOP (below DM icon), causing all indices to shift.
Index-based navigation is unreliable - "server at index 5" changes when a new server is added.

**Solution**: Hybrid approach - use POSITION for iteration, NAME as backup identifier.

Key insight: OCR is unreliable, so we cannot solely depend on name matching.

Storage format (`servers.json`):
```json
{
  "total_servers": 96,
  "servers": [
    {"index": 0, "name": "Innovation Inc", "page": 0, "y_position": 300},
    {"index": 1, "name": "GrogCloud", "page": 7, "y_position": 300}
  ]
}
```

Scan termination detection:
1. **End marker**: Look for "Add a Server" or "Explore/Discover" tooltips
2. **Stale page fallback**: After 3 consecutive pages with no new servers, assume end reached
3. **Max pages safety**: Hard limit (50 pages) prevents infinite loops

Navigation strategy (not yet implemented):
1. **Full rescan on each session**: Position is ephemeral, rescan to get current layout
2. **Count-based verification**: If we had N servers before, we should find ~N servers now
3. **New server detection**: Compare counts - if current > previous, new servers added at top
4. **Name as secondary ID**: Use fuzzy name matching only when position-based nav fails

Why not pure name-based:
- OCR produces garbled text ("Add a Server" → "eae a. box")
- Fuzzy matching has false positives
- Server renames break name-based tracking

Why position works:
- Servers have stable relative order (new ones at top, rest shifts down)
- We can iterate reliably with scroll + icon detection
- Count-based verification catches missed/extra servers

Caveats:
- Must rescan before each operation (positions change)
- OCR names are hints only, not authoritative
- Need to handle case where user left a server between scans

Channel Reading & Promo Detection (Design)
------------------------------------------
**Goal**: For each server, scan channels to find promotion/self-promo channels.

**Phase 1: Keyword-based detection**
Primary keywords (exact/fuzzy match in channel name):
- `promo`, `self-promo`, `self promo`, `selfpromo`
- `promote`, `promotion`, `self-promotion`
- `advertise`, `advertising`, `ads`
- `plug`, `self-plug`, `plugs`
- `share`, `share-your`, `show-off`
- `spotlight`, `showcase`

**Phase 2: Content verification (for ambiguous channels)**
Some servers customize promo channel names. If channel name is ambiguous:
1. Enter channel, read recent messages
2. Look for promotional content patterns:
   - Links (twitch.tv, youtube.com, twitter.com, etc.)
   - "Check out my...", "Follow me...", "Subscribe..."
   - Multiple users posting similar self-promotional content
3. Check channel topic/description if visible

**Storage format** (`server_channels.json`):
```json
{
  "servers": {
    "Innovation Inc": {
      "server_index": 0,
      "channels": {
        "promo": {"type": "promo", "confidence": "high", "method": "keyword"},
        "share-your-stuff": {"type": "promo", "confidence": "medium", "method": "keyword"},
        "general-chat": {"type": "general", "confidence": "high"}
      },
      "promo_channels": ["promo", "share-your-stuff"],
      "last_scanned": "2025-11-25T12:00:00Z"
    }
  }
}
```

**Channel scanning workflow**:
1. Click server icon to enter server
2. Wait for channel list to load (left sidebar)
3. Scroll to top of channel list
4. Iterate visible channels:
   - OCR channel name
   - Check against keyword list
   - If match → mark as promo (high confidence)
   - If partial match → mark for content verification
5. Scroll down, repeat until end of channel list
6. For ambiguous channels: enter and scan content
7. Save results to `server_channels.json`

**Channel detection challenges**:
- Channel categories (folders) vs actual channels
- Voice channels vs text channels (only text channels matter)
- Private/locked channels (skip, can't post anyway)
- Channel icons/emojis in names

**Implementation priority**:
1. [ ] `enter_server(index)` - click server icon by position
2. [ ] `scan_channel_list()` - iterate and OCR channel names  
3. [ ] `find_promo_channels(keywords)` - filter for promo keywords
4. [ ] `verify_channel_content(channel)` - enter and read messages
5. [ ] `save_server_channels()` - persist to JSON

