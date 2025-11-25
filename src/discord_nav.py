"""Focused Discord navigation utilities.
This file intentionally reuses a few safe helpers from `src.utils` when possible to keep logic consistent.
"""
from pathlib import Path
import time
import sys
import subprocess
import os

try:
    from src import utils
except Exception:
    utils = None
    # Attempt alternative import from file if running as a script inside `src`
    try:
        import importlib.util
        fn = os.path.join(os.path.dirname(__file__), 'utils.py')
        if os.path.isfile(fn):
            spec = importlib.util.spec_from_file_location('utils', fn)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            utils = module
    except Exception:
        pass

                    
try:
    from PIL import Image, ImageOps
    from PIL import ImageGrab
except Exception:
    Image = None
    ImageGrab = None

try:
    import pytesseract
except Exception:
    pytesseract = None

try:
    import pyautogui
except Exception:
    pyautogui = None


def _safe_grab(bbox=None, timeout_sec: float = 1.0):
    # Prefer the helper from utils if available
    if utils and hasattr(utils, '_safe_grab'):
        try:
            return utils._safe_grab(bbox=bbox, timeout_sec=timeout_sec)
        except Exception:
            pass
    # fallback: PIL ImageGrab
    try:
        if bbox:
            return ImageGrab.grab(bbox=bbox)
        return ImageGrab.grab()
    except Exception:
        return None


def _vertical_projection_centers(img, w, h, merge_gap=8):
    """Return list of center Y positions (relative to the image top).

    Uses row-wise variance to detect icons against uniform background.
    Icons have high variance (colorful pixels), gaps have low variance (solid color).
    
    Input `img` should be a grayscale PIL image of the left server column.
    """
    arr = list(img.getdata())
    
    # Compute row variance (icons have texture, gaps are uniform)
    variances = []
    for y in range(h):
        row = [arr[y * w + x] for x in range(w)]
        if not row:
            variances.append(0)
            continue
        mean = sum(row) / len(row)
        variance = sum((v - mean) ** 2 for v in row) / len(row)
        variances.append(variance)
    
    # Use adaptive threshold based on variance distribution
    # Gaps are typically in the bottom 20-30% of variance values
    sorted_vars = sorted(variances)
    p25_idx = len(sorted_vars) // 4
    p75_idx = (3 * len(sorted_vars)) // 4
    p25 = sorted_vars[p25_idx] if sorted_vars else 0
    p75 = sorted_vars[p75_idx] if sorted_vars else 1000
    
    # Gap threshold: values below the 25th percentile plus a margin
    # This adapts to different themes and display scales
    GAP_THRESH = max(100, p25 + (p75 - p25) * 0.15)
    MIN_GAP_SIZE = 4  # Minimum consecutive gap rows
    MIN_ICON_SIZE = 15  # Minimum icon height in pixels
    
    # Find gap regions (runs of low variance)
    gaps = []
    in_gap = False
    gap_start = 0
    
    for y in range(h):
        if variances[y] < GAP_THRESH and not in_gap:
            in_gap = True
            gap_start = y
        elif variances[y] >= GAP_THRESH and in_gap:
            in_gap = False
            gap_size = y - gap_start
            if gap_size >= MIN_GAP_SIZE:
                gaps.append({'start': gap_start, 'end': y, 'size': gap_size})
    
    # Handle gap at end of image
    if in_gap and (h - gap_start) >= MIN_GAP_SIZE:
        gaps.append({'start': gap_start, 'end': h, 'size': h - gap_start})
    
    # If too few gaps found, use tighter threshold
    if len(gaps) < 3:
        tighter_thresh = max(50, p25 + (p75 - p25) * 0.05)
        gaps = []
        in_gap = False
        gap_start = 0
        for y in range(h):
            if variances[y] < tighter_thresh and not in_gap:
                in_gap = True
                gap_start = y
            elif variances[y] >= tighter_thresh and in_gap:
                in_gap = False
                if y - gap_start >= MIN_GAP_SIZE:
                    gaps.append({'start': gap_start, 'end': y, 'size': y - gap_start})
        if in_gap and (h - gap_start) >= MIN_GAP_SIZE:
            gaps.append({'start': gap_start, 'end': h, 'size': h - gap_start})
    
    # If still no gaps found, fall back to fixed-step detection
    if len(gaps) < 2:
        step = 48
        centers = list(range(24, h - 24, step))
        return centers if centers else [h // 2]
    
    # Icons are the regions between consecutive gaps
    icons = []
    for i in range(len(gaps) - 1):
        icon_start = gaps[i]['end']
        icon_end = gaps[i + 1]['start']
        icon_size = icon_end - icon_start
        if icon_size >= MIN_ICON_SIZE:
            center = (icon_start + icon_end) // 2
            icons.append({'start': icon_start, 'end': icon_end, 'center': center, 'size': icon_size})
    
    # Split overly large icons (likely merged due to small separators)
    # Typical icon is ~40-60px tall; anything > 70px likely contains multiple icons
    MAX_SINGLE_ICON = 70
    split_icons = []
    for icon in icons:
        if icon['size'] <= MAX_SINGLE_ICON:
            split_icons.append(icon)
        else:
            # Try to split by finding local variance minima within this region
            region_start = icon['start']
            region_end = icon['end']
            region_vars = variances[region_start:region_end]
            
            # Find local minima that could be separators
            minima = []
            for j in range(5, len(region_vars) - 5):
                local_val = region_vars[j]
                # Check if this is a local minimum
                window_before = region_vars[max(0, j-3):j]
                window_after = region_vars[j+1:min(len(region_vars), j+4)]
                if window_before and window_after:
                    if local_val < min(window_before) * 0.6 and local_val < min(window_after) * 0.6:
                        minima.append(region_start + j)
            
            if minima:
                # Create sub-icons based on minima
                sub_starts = [region_start] + minima
                sub_ends = minima + [region_end]
                for s, e in zip(sub_starts, sub_ends):
                    size = e - s
                    if size >= MIN_ICON_SIZE:
                        split_icons.append({
                            'start': s, 'end': e, 
                            'center': (s + e) // 2, 'size': size
                        })
            else:
                # Can't split, keep as is
                split_icons.append(icon)
    
    centers = [icon['center'] for icon in split_icons]
    return centers


def _is_icon_by_variance(img, cx, cy, size=28, var_threshold=8.0):
    try:
        half = size // 2
        bbox = (cx - half, cy - half, cx + half, cy + half)
        crop = img.crop(bbox).convert('L')
        px = list(crop.getdata())
        if not px:
            return False
        mean = sum(px) / len(px)
        var = sum((p - mean) ** 2 for p in px) / len(px)
        stddev = var ** 0.5
        return stddev >= var_threshold
    except Exception:
        return True


def _is_dm_icon_by_color(img, cx, cy, size=36):
    """Check if the icon at (cx, cy) has the characteristic Discord DM blue color.
    
    Discord DM icon typically has a blue/indigo background (#5865F2 or similar).
    Returns True if the icon area has significant blue content.
    """
    try:
        half = size // 2
        bbox = (max(0, cx - half), max(0, cy - half), cx + half, cy + half)
        crop = img.crop(bbox).convert('RGB')
        px = list(crop.getdata())
        if not px:
            return False
        # Count pixels with significant blue component (Discord's blurple is ~88,101,242)
        blue_count = 0
        for r, g, b in px:
            # Check for Discord blurple-ish colors (blue dominant, moderate red/green)
            if b > 150 and b > r and b > g * 0.9:
                blue_count += 1
            # Also check for lighter blue variants
            elif b > 180 and b > r * 1.1:
                blue_count += 1
            # Check for Discord's exact blurple (#5865F2 = 88,101,242)
            elif 70 < r < 110 and 85 < g < 120 and b > 200:
                blue_count += 1
        ratio = blue_count / len(px)
        return ratio > 0.08  # At least 8% blue pixels suggests DM icon
    except Exception:
        return False


def find_and_hover_first_server(start_from_top: bool = True, hover_delay: float = 0.6, test_target: str | None = None,
                                force_run: bool = False, debug_save: bool = False, max_centers: int | None = None,
                                max_icon_retries: int = 3, start_index_offset: int = 0):
    """Find the first server icon (after DM/home) and hover center.

    Returns (x, y) if hovered, None otherwise.
    """
    # Bring Discord to front
    # Try to focus Discord and obtain its bbox
    print('utils imported?', utils is not None)
    if utils is not None:
        print('utils attributes: run_discord?', hasattr(utils, 'run_discord'), 'find_and_focus_discord?', hasattr(utils, 'find_and_focus_discord'))
    bbox = None
    if utils:
        try:
            bbox = utils.find_and_focus_discord()
        except Exception:
            bbox = None
        # If we couldn't focus, try to run/bring Discord to front, then refocus
        if not bbox and hasattr(utils, 'run_discord'):
            try:
                utils.run_discord()
                time.sleep(0.8)
                bbox = utils.find_and_focus_discord()
            except Exception:
                bbox = bbox or None
    # If still not found and `find_discord` is available, call it (it attempts to focus/launch)
    if not bbox and utils and hasattr(utils, 'find_discord'):
        try:
            found = utils.find_discord()
            # ask for a subsequent focus attempt
            if found:
                try:
                    bbox = utils.find_and_focus_discord()
                except Exception:
                    bbox = None
        except Exception:
            bbox = None
    # If still no bbox or force_run requested, attempt to run Discord and re-check (OS-specific activation)
    if (not bbox or force_run):
        try:
            print('Attempting to run/activate Discord and re-focus...')
            if utils and hasattr(utils, 'run_discord'):
                try:
                    print('Calling utils.run_discord() to start Discord...')
                    utils.run_discord()
                except Exception as e:
                    print('utils.run_discord() error:', e)
                    pass
            else:
                # fallback to OS-level start
                try:
                    if sys.platform.startswith('darwin'):
                        subprocess.Popen(['open', '-a', 'Discord'])
                    elif sys.platform.startswith('linux'):
                        subprocess.Popen(['discord'])
                    elif sys.platform.startswith('win32'):
                        subprocess.Popen(['start', 'Discord'], shell=True)
                except Exception as e:
                    print('OS-level run_discord fallback failed:', e)
            # OS-specific additional activation (macOS AppleScript, Linux/Windows run)
            try:
                if sys.platform.startswith('darwin'):
                    subprocess.run(["osascript", "-e", 'tell application "Discord" to activate'], check=False)
                elif sys.platform.startswith('linux'):
                    subprocess.Popen(['discord'])
                elif sys.platform.startswith('win32'):
                    subprocess.Popen(['start', 'Discord'], shell=True)
            except Exception:
                pass
            # Poll for focus/bbox  -> give a longer window to start
            for attempt in range(16):
                time.sleep(0.6)
                try:
                    bbox = utils.find_and_focus_discord()
                except Exception:
                    bbox = None
                print('  re-check attempt', attempt, 'bbox=', bbox)
                if bbox:
                    break
        except Exception:
            pass
    # Log bbox info
    print('Discord bbox:', bbox)
    # Verify Discord is foreground if we can
    try:
        if utils and hasattr(utils, 'is_discord_foreground'):
            fg = utils.is_discord_foreground()
            print('Discord foreground?', fg)
            if not fg:
                # give user a chance to focus, but also try an activation attempt
                try:
                    if hasattr(utils, 'run_discord'):
                        utils.run_discord()
                except Exception:
                    pass
                print('Please ensure Discord is visible and focused. Waiting 2 seconds for manual focus...')
                time.sleep(2.0)
                try:
                    fg2 = utils.is_discord_foreground()
                    print('Discord foreground after attempt?', fg2)
                    if fg2:
                        bbox = utils.find_and_focus_discord() or bbox
                except Exception:
                    pass
    except Exception:
        pass
    if not bbox:
        # If Discord wasn't found or focused, prompt and return (don't auto-fallback to full-screen)
        print('Discord window not found or not focused despite attempts. Please ensure Discord is running and focused, then re-run this helper.')
        return None
    else:
        left, top, width, height = bbox

    # default to a slightly wider crop of the server column to reduce merging into a single region
    col_w = max(48, int(width * 0.08))
    col_box = (max(0, left - 2), top, min(left + col_w + 4, left + width), top + height)
    # Debug save dir
    debug_dir = None
    if debug_save:
        try:
            debug_dir = os.path.join('data', 'debug')
            os.makedirs(debug_dir, exist_ok=True)
        except Exception:
            debug_dir = None

    # local UI blacklist and DM keywords (keep in sync with utils)
    UI_BLACKLIST = {'friends', 'nitro', 'direct messages', 'direct message', 'home', 'add a server', 'create', 'download', 'explore public servers', 'threads', 'stage', 'settings', 'friends list'}
    DM_KEYWORDS = ('direct messages', 'direct message', 'home', 'friends', 'messages', 'rectmessage', 'irectmessage')

    # safety margins (avoid titlebar/toolbar)
    top_skip_px = 48
    bottom_skip_px = 64

    def _hover_and_read_local(cx, cy, hover_delay=hover_delay):
        txt = ''
        if not pytesseract or not pyautogui:
            return txt
        try:
            pyautogui.moveTo(cx, cy, duration=0.14)
            time.sleep(max(hover_delay, 0.35))
        except Exception:
            pass
        offsets = [(0, 0), (-8, 0), (8, 0), (-12, 0), (12, 0), (0, 8), (0, -8)]
        # Wider tooltip capture boxes to catch Discord tooltips at different positions
        candidate_boxes = [
            (cx + 50, cy - 20, cx + 280, cy + 20),   # right of icon, centered
            (cx + 40, cy - 35, cx + 320, cy + 15),   # right, slightly above
            (cx + 15, cy - 30, cx + 250, cy + 10),   # original right
            (cx + 8, cy - 20, cx + 200, cy + 20),    # close right
            (cx + 15, cy - 60, cx + 350, cy + 15),   # wide right capture
            (cx - 180, cy - 30, cx - 10, cy + 30),   # left tooltip
            (cx - 250, cy - 50, cx + 100, cy + 40),  # wide left capture
        ]
        for dx, dy in offsets:
            try:
                if dx or dy:
                    pyautogui.moveRel(dx, dy, duration=0.08)
                    time.sleep(0.08)
            except Exception:
                pass
            for tb in candidate_boxes:
                try:
                    tbimg = _safe_grab(tb)
                except Exception:
                    tbimg = None
                if tbimg is None:
                    continue
                try:
                    # Enhance contrast before OCR
                    if ImageOps:
                        tbimg = ImageOps.autocontrast(tbimg.convert('RGB'))
                    t = pytesseract.image_to_string(tbimg, config='--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 ')
                except Exception:
                    t = ''
                if t and t.strip() and len(t.strip()) > 1:
                    return t.strip()
        return txt

    def _clamp_hover_y(y):
        # keep y inside column and avoid titlebar and bottom UI
        miny = top + top_skip_px + 6
        maxy = top + height - bottom_skip_px - 6
        return int(max(miny, min(y, maxy)))

    # Move cursor to a safe position inside the server column before scrolling
    try:
        if pyautogui:
            cx = left + (col_w // 2)
            safe_y = top + (height // 2)
            # avoid header and bottom UI
            safe_y = max(safe_y, top + 64)
            safe_y = min(safe_y, top + height - 64)
            print('Moving mouse to safe scrolling position:', cx, safe_y)
            try:
                pyautogui.moveTo(cx, safe_y, duration=0.12)
            except Exception:
                pyautogui.moveTo(cx, safe_y)
            time.sleep(0.12)
            # DO NOT CLICK (user requested): only move cursor to safe scroll area
    except Exception:
        pass

    # Optionally try to scroll to top first via utils's helper (more strongly if requested)
    if utils and hasattr(utils, '_seek_extreme'):
        try:
            print('Seeking to top via utils._seek_extreme...')
            max_iters = 10 if start_from_top else 8
            ok = utils._seek_extreme('up', max_iters=max_iters, repeat_goal=3, step_clicks=18)
            print('seek_to_top result:', ok)
        except Exception as e:
            print('seek_to_top error:', e)
            pass
    # Re-check centers after seek; if too few centers detected, try a stronger seek
    col_img = _safe_grab(col_box)
    if col_img is not None:
        try:
            gray2 = col_img.convert('L')
            w2, h2 = gray2.size
            centers2 = _vertical_projection_centers(gray2, w2, h2)
            if len(centers2) < 2 and utils and hasattr(utils, '_seek_extreme'):
                print('Too few centers after initial seek; retrying a stronger seek-to-top')
                try:
                    utils._seek_extreme('up', max_iters=12, repeat_goal=4, step_clicks=28)
                except Exception:
                    pass
                time.sleep(0.25)
                col_img = _safe_grab(col_box)
        except Exception:
            pass

    # Take a snapshot and compute centers using variance-based detection
    col_img = _safe_grab(col_box)
    if col_img is None:
        print('Error: could not capture server column')
        return None
    gray = col_img.convert('L')
    w, h = gray.size
    centers = _vertical_projection_centers(gray, w, h)
    print('Detected centers count:', len(centers), 'first few centers:', centers[:8])
    
    # Translate to absolute screen coordinates
    centers_abs = [top + c for c in centers]
    
    # Debug: show diffs and spacing info
    diffs = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)] if len(centers) > 1 else []
    print('Centers diffs:', diffs[:10])
    median = None
    if diffs:
        diffs_sorted = sorted([d for d in diffs if d > 10])
        median = diffs_sorted[len(diffs_sorted) // 2] if diffs_sorted else None
        large_gap = max(10, int(median * 1.4)) if median else None
        print('Median step:', median, 'large_gap threshold:', large_gap)
    
    # If only a single center detected, try wider column capture
    if len(centers) < 2:
        try:
            wider_w = min(width, int(width * 0.12))
            col_box2 = (max(0, left - 20), top, min(left + wider_w + 20, left + width), top + height)
            col_img2 = _safe_grab(col_box2)
            if col_img2 is not None:
                gray2 = col_img2.convert('L')
                w2, h2 = gray2.size
                centers2 = _vertical_projection_centers(gray2, w2, h2)
                if len(centers2) > len(centers):
                    centers = centers2
                    centers_abs = [top + c for c in centers]
                    col_img = col_img2
                    gray = gray2
                    w, h = w2, h2
                    print('Wider column capture increased centers count:', len(centers))
        except Exception:
            pass

    # If we don't have enough centers detected, try a stronger seek-to-top and recalc centers
    seek_attempts = 0
    while len(centers) < 2 and seek_attempts < 8 and utils and hasattr(utils, '_seek_extreme'):
        try:
            print('Not enough centers detected (', len(centers), '), trying stronger seek-to-top')
            utils._seek_extreme('up', max_iters=8, repeat_goal=3, step_clicks=28)
        except Exception:
            pass
        time.sleep(0.2)
        col_img = _safe_grab(col_box)
        if col_img is None:
            break
        gray = col_img.convert('L')
        w, h = gray.size
        centers = _vertical_projection_centers(gray, w, h)
        centers_abs = [top + c for c in centers]
        seek_attempts += 1

    # If possible, try actively seeking until the DM/home item is visible in the viewport
    dm_search_attempts = 0
    dm_idx = None
    while dm_search_attempts < 6 and (dm_idx is None) and utils and hasattr(utils, '_seek_extreme'):
        try:
            dm_idx = _find_dm_index(centers_abs)
        except Exception:
            dm_idx = None
        if dm_idx is None:
            try:
                utils._seek_extreme('up', max_iters=4, repeat_goal=2, step_clicks=18)
            except Exception:
                try:
                    pyautogui.scroll(600)
                except Exception:
                    pass
            time.sleep(0.14)
            col_img = _safe_grab(col_box)
            if col_img is None:
                break
            gray = col_img.convert('L')
            w, h = gray.size
            centers = _vertical_projection_centers(gray, w, h)
            centers_abs = [top + c for c in centers]
            dm_search_attempts += 1
    if dm_idx is not None:
        print('Found DM index after active search:', dm_idx)
        # Attempt to hover servers immediately after DM to read tooltips and select the first legitimate server
        try:
            for cand_i in range(dm_idx + 1, min(len(centers_abs), dm_idx + 6)):
                y_hover = _clamp_hover_y(centers_abs[cand_i])
                try:
                    txtcand = _hover_and_read_local(cx, y_hover, hover_delay=hover_delay) or ''
                except Exception:
                    txtcand = ''
                lname = (txtcand or '').lower()
                print('  Hover after DM idx', cand_i, 'txt:', repr(txtcand))
                if txtcand and not any(k in lname for k in DM_KEYWORDS) and not any(k in lname for k in UI_BLACKLIST):
                    print('Detected first server after DM at index', cand_i, 'name:', txtcand)
                    return (cx, int(y_hover))
        except Exception:
            pass

    # Determine spacing-based first index and start scanning from there
    start_idx = 0
    # prefer DM detection first so we start after DM/home (most robust)
    try:
        dm_idx = _find_dm_index(centers_abs)
        print('dm_idx:', dm_idx)
        if dm_idx is not None:
            start_idx = dm_idx + 1
        else:
            start_idx = _detect_first_server_index_local(centers_abs, top_y=top)
    except Exception:
        try:
            start_idx = _detect_first_server_index_local(centers_abs, top_y=top)
        except Exception:
            start_idx = 0
    # If the gap at the top between center 0 and 1 indicates DM separation (double spacing), prefer index 1
    try:
        if len(centers) > 1 and median:
            if (centers[1] - centers[0]) > max(12, int(median * 1.4)):
                print('Top gap detected: centers[1]-centers[0]=', centers[1] - centers[0], ' > 1.4*median(', median, '), using index 1')
                start_idx = 1
    except Exception:
        pass
    if start_idx:
        print('Spacing-based start_idx (raw):', start_idx)
    # Apply user offset (positive moves downwards to later indices)
    try:
        orig_start_idx = start_idx
        start_idx = max(0, min(len(centers_abs)-1, start_idx + (start_index_offset or 0)))
        if start_idx != orig_start_idx:
            print('Adjusted start_idx by offset', start_index_offset, '->', start_idx)
    except Exception:
        pass
    # Pre-scan earlier centers before start_idx to ensure any server tooltip is not present earlier
    pre_scan_count = min(start_idx, 6)
    if pre_scan_count > 0:
        print('Pre-scanning first', pre_scan_count, 'centers for server names (to guard against mis-detection)')
        for j in range(pre_scan_count):
            cy = centers_abs[j]
            if pyautogui:
                y_for_hover = _clamp_hover_y(cy)
                try:
                    pyautogui.moveTo(cx, int(y_for_hover), duration=0.08)
                    time.sleep(max(hover_delay, 0.22))
                except Exception:
                    pass
            txt = ''
            try:
                txt = _hover_and_read_local(cx, y_for_hover, hover_delay=hover_delay)
            except Exception:
                txt = ''
            print('  pre-scan index', j, 'y', cy, 'txt=', repr(txt))
            lname = (txt or '').lower()
            if txt and not any(k in lname for k in ('direct messages', 'direct message', 'home')) and not any(k in lname for k in UI_BLACKLIST):
                print('  Pre-scan found server at index', j, 'name:', txt)
                return (cx, int(y_for_hover))
        centers_iter = centers_abs[start_idx:(start_idx + max_centers) if (max_centers and max_centers > 0) else None] if centers_abs else []
        print('Starting scan from index', start_idx, 'center y', centers_abs[start_idx] if centers_abs and start_idx < len(centers_abs) else None)
    if debug_dir:
        try:
            fn = os.path.join(debug_dir, f'col_{int(time.time()*1000)}.png')
            col_img.save(fn)
            print('Saved column screenshot to', fn)
        except Exception:
            pass
    # translate to absolute centers
    centers_abs = [top + c for c in centers]

    # Local spacing-based heuristic (moved above detection helper so nested helper can use it)
    def _detect_first_server_index_local(centers_list, top_y=top, header_skip=48):
        """Detect the first server index based on spacing patterns.
        
        Discord server list structure (scrolled to top):
        - Icon 0: DM/Home button at top
        - (LARGE GAP ~1.5-2x normal): Separator between DM and servers  
        - Icon 1+: Actual server icons with consistent spacing
        - (possible folder separators have ~1.3x normal spacing)
        - (LARGE GAP at bottom): "Add Server" button is separated
        
        The key insight is that the DM-to-first-server gap is significantly
        larger than server-to-server spacing.
        """
        if not centers_list:
            return 0
        if len(centers_list) < 2:
            # Single center - check if it's past the header
            for i, c in enumerate(centers_list):
                if c - top_y > header_skip + 6:
                    return i
            return 0
        
        # Calculate spacing between consecutive icons
        diffs = [centers_list[i + 1] - centers_list[i] for i in range(len(centers_list) - 1)]
        diffs_filtered = [d for d in diffs if d > 10]  # Filter tiny/noise diffs
        
        if not diffs_filtered:
            return 0
        
        # Find the typical (median) server-to-server spacing
        diffs_sorted = sorted(diffs_filtered)
        median_idx = len(diffs_sorted) // 2
        median = diffs_sorted[median_idx]
        
        print(f'  Spacing analysis: {len(diffs)} diffs, median={median}px, diffs={diffs[:8]}')
        
        # Large gap threshold: 1.4x median indicates DM separator or folder gap
        large_gap_thresh = int(median * 1.4)
        
        # Very large gap threshold: 1.8x median likely DM-to-server separator
        very_large_gap_thresh = int(median * 1.8)
        
        # Check if first gap (centers[0] to centers[1]) is large
        # This is the most common case - DM at index 0, first server at index 1
        if diffs and diffs[0] >= large_gap_thresh:
            print(f'  First gap {diffs[0]}px >= {large_gap_thresh}px threshold -> first server at index 1')
            return 1
        
        # Look for first large gap anywhere (in case scroll position is off)
        for i, d in enumerate(diffs):
            if d >= very_large_gap_thresh:
                # Verify next few spacings are normal (consistent server spacing)
                next_diffs = diffs[i+1:i+4]
                if next_diffs:
                    avg_next = sum(next_diffs) / len(next_diffs)
                    if abs(avg_next - median) < median * 0.3:  # Next spacings are consistent
                        print(f'  Very large gap at index {i} ({d}px) -> first server at index {i+1}')
                        return i + 1
        
        # Look for first large gap with consistent following spacing
        for i, d in enumerate(diffs):
            if d >= large_gap_thresh:
                # Check if subsequent spacing is normal
                next_diffs = diffs[i+1:i+4]
                consistent_count = sum(1 for nd in next_diffs if abs(nd - median) < median * 0.25)
                if consistent_count >= min(2, len(next_diffs)):
                    print(f'  Large gap at index {i} ({d}px) with consistent following -> first server at index {i+1}')
                    return i + 1
        
        # Fallback: find first run of 3+ consistent spacings
        for i in range(len(diffs) - 2):
            if (abs(diffs[i] - median) < median * 0.25 and 
                abs(diffs[i + 1] - median) < median * 0.25 and
                abs(diffs[i + 2] - median) < median * 0.25):
                print(f'  Found consistent run starting at index {i}')
                return i
        
        # Last resort: first icon past header
        for i, c in enumerate(centers_list):
            if c - top_y > header_skip + 6:
                return i
        
        return 0

    # Detection helper: run one pass of hover-based detection, returning a candidate or None
    cx = left + (col_w // 2)
    def _run_detection_once():
        nonlocal centers_abs
        candidate = None
        candidate_source = None
        candidate_idx = None
        hover_found = None
        centers_iter_local = centers_abs[:max_centers] if (max_centers and max_centers > 0) else centers_abs
        
        # First, use spacing to determine first server index - this is the PRIMARY method
        # Don't rely on OCR to skip/validate positions since OCR is unreliable
        spacing_idx = _detect_first_server_index_local(centers_iter_local, top_y=top)
        print(f'Spacing detection says first server at index {spacing_idx}')
        
        # If spacing found a valid index, trust it and start from there
        if spacing_idx > 0 and spacing_idx < len(centers_iter_local):
            cy = centers_iter_local[spacing_idx]
            y_for_hover = _clamp_hover_y(cy)
            candidate = (cx, int(y_for_hover))
            candidate_idx = spacing_idx
            candidate_source = 'spacing-primary'
            print(f'Using spacing-based first server at index {spacing_idx}, y={cy}')
            return candidate, candidate_source, candidate_idx
        
        # Fallback: scan all icons if spacing detection failed
        FIRST_SERVER_SCAN_MAX = 3
        early_nonempty_found = False
        for i, cy in enumerate(centers_iter_local):
            # skip near header
            if cy - top <= 48:
                continue
            # Check if icon area has content (variance check) but DON'T skip on failure
            # Dark icons may have low variance but are still valid servers
            has_icon = True
            try:
                has_icon = _is_icon_by_variance(gray, cx - left, cy - top, size=40, var_threshold=6.0)
            except Exception:
                has_icon = True  # Assume icon present if check fails
            # hover
            y_for_hover = _clamp_hover_y(cy)
            if pyautogui:
                try:
                    pyautogui.moveTo(cx, int(y_for_hover), duration=0.1)
                except Exception:
                    # try a safe move without duration
                    try:
                        pyautogui.moveTo(cx, int(y_for_hover))
                    except Exception:
                        pass
                time.sleep(max(hover_delay, 0.45))  # Give tooltip time to appear
            print(f"Hover candidate #{i} at ({cx},{int(cy)})")
            # try to read tooltip text
            txt = ''
            if pytesseract is not None:
                try:
                    candidate_boxes = [
                        (cx + 15, cy - 30, cx + 200, cy + 10),
                        (cx + 8, cy - 15, cx + 130, cy + 15),
                        (cx + 15, cy - 60, cx + 300, cy + 10),
                        (cx - 160, cy - 28, cx - 10, cy + 26),
                        (cx - 200, cy - 60, cx + 80, cy + 32),
                    ]
                    # Use the robust hover helper first
                    try:
                        txtcand = _hover_and_read_local(cx, y_for_hover, hover_delay=hover_delay)
                        # If empty, retry small jitter moves for tooltip up to max_icon_retries
                        retries = 0
                        while (not txtcand or not txtcand.strip()) and retries < max_icon_retries:
                            try:
                                pyautogui.moveRel(4 if retries % 2 == 0 else -4, 0, duration=0.06)
                                time.sleep(0.08)
                                txtcand = _hover_and_read_local(cx, y_for_hover, hover_delay=hover_delay)
                            except Exception:
                                txtcand = ''
                            retries += 1
                    except Exception:
                        txtcand = ''
                    if not txtcand:
                        # fallback to cropping from column image
                        for tb in candidate_boxes:
                            tb_local = (tb[0] - left, tb[1] - top, tb[2] - left, tb[3] - top)
                            try:
                                tip_img = gray.crop(tb_local)
                            except Exception:
                                tip_img = None
                            if tip_img is not None:
                                try:
                                    txtcand = pytesseract.image_to_string(tip_img, config='--psm 7')
                                except Exception:
                                    txtcand = ''
                            if txtcand and txtcand.strip():
                                txt = txtcand.strip()
                                break
                except Exception:
                    txt = ''
            print('    OCR text:', repr(txt), 'at index', i)
            # Save hover candidate crop for debugging
            if debug_dir:
                try:
                    hf = os.path.join(debug_dir, f'hover_{int(time.time()*1000)}_{i}.png')
                    rr = (cx - 100, int(cy) - 60, cx + 300, int(cy) + 40)
                    img = _safe_grab(rr)
                    if img is not None:
                        try:
                            img.save(hf)
                            print('Saved hover debug to', hf)
                        except Exception:
                            pass
                except Exception:
                    pass
            if txt:
                hover_found = i
                if i <= FIRST_SERVER_SCAN_MAX:
                    early_nonempty_found = True
            # If test_target provided, check for match
            if test_target and txt:
                try:
                    if test_target.lower() in txt.lower():
                        print('    Test target matched:', test_target)
                        candidate = (cx, int(y_for_hover))
                        candidate_idx = i
                        candidate_source = 'test-target'
                        break
                except Exception:
                    pass
            lname = (txt or '').lower()
            if any(k in lname for k in ('direct messages', 'direct message', 'home')):
                continue
            # require non-empty OCR for hover detection to avoid mistaking empty spots for servers
            if not txt:
                continue
            # If we reached here and we have a non-empty OCR that's not DM, treat as first server
            try:
                if pyautogui:
                    pyautogui.moveTo(cx, int(y_for_hover), duration=0.12)
            except Exception:
                pass
            try:
                cur = None
                if pyautogui:
                    cur = pyautogui.position()
                print('Found first-server candidate; cursor now at:', cur)
            except Exception:
                pass
            print('    Hover-based first index used:', i)
            candidate = (cx, int(y_for_hover))
            candidate_idx = i
            candidate_source = 'hover'
            break
        # Trust spacing-based detection - don't reject based on OCR timing
        # The spacing math is reliable; OCR is not

        # spacing fallback if no hover candidate
        if not candidate and centers_abs:
            centers_for_detection = centers_abs[:max_centers] if (max_centers and max_centers > 0) else centers_abs
            idx = _detect_first_server_index_local(centers_for_detection, top_y=top)
            print('Spacing-based first idx:', idx)
            if idx < len(centers_abs):
                cy = centers_abs[idx]
                y_for_hover = _clamp_hover_y(cy)
                candidate = (cx, int(y_for_hover))
                candidate_source = 'spacing'
                candidate_idx = idx
            # If spacing chose a later index but the top two icons show no tooltip at all, prefer index 1
            try:
                if idx > 1 and len(centers_abs) > 1:
                    top_two_empty = True
                    for check_i in range(0, min(2, len(centers_abs))):
                        cy_check = centers_abs[check_i]
                        y_check = _clamp_hover_y(cy_check)
                        txt_check = _hover_and_read_local(cx, y_check, hover_delay=hover_delay) or ''
                        retries_local = 0
                        while (not txt_check or not txt_check.strip()) and retries_local < max_icon_retries * 2:
                            try:
                                if pyautogui:
                                    pyautogui.moveRel(6 if retries_local % 2 == 0 else -6, 0, duration=0.06)
                                    time.sleep(0.06)
                                txt_check = _hover_and_read_local(cx, y_check, hover_delay=hover_delay) or ''
                            except Exception:
                                txt_check = ''
                            retries_local += 1
                        if txt_check and not any(k in txt_check.lower() for k in DM_KEYWORDS) and not any(k in txt_check.lower() for k in UI_BLACKLIST):
                            top_two_empty = False
                            break
                    if top_two_empty:
                        # prefer index 1 when the top two icons produce no tooltip text, avoid picking a later cluster
                        alt_idx = 1 if len(centers_abs) > 1 else 0
                        cy_alt = centers_abs[alt_idx]
                        y_alt = _clamp_hover_y(cy_alt)
                        candidate = (cx, int(y_alt))
                        candidate_source = 'spacing-top2-fallback'
                        candidate_idx = alt_idx
                        print('Top two produced no tooltips; preferring index', alt_idx)
                        # verify fallback candidate isn't DM/UI; if it is, move to the next non-DM icon
                        try:
                            cand_txt = _hover_and_read_local(candidate[0], candidate[1], hover_delay=hover_delay) or ''
                        except Exception:
                            cand_txt = ''
                        if cand_txt and (any(k in cand_txt.lower() for k in DM_KEYWORDS) or any(k in cand_txt.lower() for k in UI_BLACKLIST)):
                            print('Fallback candidate matched DM/UI; probing next indices for first server')
                            for nn in range(candidate_idx + 1, min(len(centers_abs), candidate_idx + 6)):
                                try:
                                    y_nn = _clamp_hover_y(centers_abs[nn])
                                    tnn = _hover_and_read_local(cx, y_nn, hover_delay=hover_delay) or ''
                                except Exception:
                                    tnn = ''
                                if tnn and not any(k in tnn.lower() for k in DM_KEYWORDS) and not any(k in tnn.lower() for k in UI_BLACKLIST):
                                    candidate = (cx, int(y_nn))
                                    candidate_idx = nn
                                    candidate_source = 'spacing-top2-next'
                                    print('Found next valid server at index', nn, 'via fallback probe')
                                    break
            except Exception:
                pass
        # Trust spacing-based detection - don't override with earlier probes that may misidentify DM/UI elements
        return candidate, candidate_source, candidate_idx
    # End helper _run_detection_once

    def _find_dm_index(centers_list):
        """Return index of DM/home icon or None.
        
        Uses color-based detection first (Discord's blue DM icon), then falls back to OCR.
        """
        if not centers_list:
            return None
        # Capture fresh RGB image for color detection
        col_img_rgb = _safe_grab(col_box)
        # First pass: try color-based detection for DM icon (faster and more reliable)
        for i, cy in enumerate(centers_list):
            if cy - top <= 48:
                # skip header region
                continue
            try:
                # Check if icon has Discord's characteristic blue color
                local_cy = cy - top  # convert to local coordinates
                if col_img_rgb and _is_dm_icon_by_color(col_img_rgb, cx - left, local_cy, size=40):
                    print('DM icon found via color detection at index', i, 'y', cy)
                    return i
            except Exception as e:
                print(f'  Color detection error at index {i}: {e}')
                pass
        
        # Second pass: fallback to OCR-based detection
        for i, cy in enumerate(centers_list):
            if cy - top <= 48:
                # skip header region
                continue
            # ensure there is an icon
            try:
                if not _is_icon_by_variance(gray, cx - left, cy - top, size=36, var_threshold=9.0):
                    continue
            except Exception:
                pass
            # hover and OCR
            try:
                if pyautogui:
                    y_for_hover = _clamp_hover_y(cy)
                    pyautogui.moveTo(cx, int(y_for_hover), duration=0.08)
                time.sleep(max(hover_delay, 0.24))
            except Exception:
                pass
            try:
                txt = _hover_and_read_local(cx, y_for_hover, hover_delay=hover_delay) or ''
            except Exception:
                txt = ''
            lname = (txt or '').lower()
            print('DM candidate OCR at index', i, 'y', cy, '->', repr(txt))
            if any(k in lname for k in DM_KEYWORDS):
                print('DM/home icon found at index', i, 'text:', txt)
                return i
        return None
    
    # Try detection with retry logic
    for detect_attempt in range(3):
        candidate, candidate_source, candidate_idx = _run_detection_once()
        if not candidate:
            print('No candidate found in detection pass', detect_attempt)
            # try seeking to top then retry
            if utils and hasattr(utils, '_seek_extreme'):
                try:
                    print('Attempting seek-to-top after empty detection (attempt', detect_attempt, ')')
                    utils._seek_extreme('up', max_iters=6, repeat_goal=3, step_clicks=20)
                except Exception:
                    pass
            time.sleep(0.2)
            # refresh column snapshot
            col_img = _safe_grab(col_box)
            if col_img is None:
                print('Error: could not recapture server column')
                break
            gray = col_img.convert('L')
            w, h = gray.size
            centers = _vertical_projection_centers(gray, w, h)
            centers_abs = [top + c for c in centers]
            continue
        # validate candidate position
        cand_x, cand_y = candidate
        too_far_px = max(160, int(height * 0.35))
        if cand_y - top > too_far_px and detect_attempt < 2 and utils and hasattr(utils, '_seek_extreme'):
            print('Candidate at', cand_y - top, 'px below top; trying stronger seek-to-top and retry')
            try:
                utils._seek_extreme('up', max_iters=10, repeat_goal=3, step_clicks=24)
            except Exception:
                pass
            time.sleep(0.3)
            # update centers after seeking
            col_img = _safe_grab(col_box)
            if col_img is None:
                print('Error: could not capture server column after seeking')
                break
            gray = col_img.convert('L')
            w, h = gray.size
            centers = _vertical_projection_centers(gray, w, h)
            centers_abs = [top + c for c in centers]
            continue
        # Validate candidate via hover and OCR; if invalid and spacing-based, try next index
        if candidate:
            cand_x, cand_y = candidate
            cand_y = _clamp_hover_y(cand_y)
            cand_name = ''
            try:
                cand_name = _hover_and_read_local(cand_x, cand_y, hover_delay=hover_delay) or ''
            except Exception:
                cand_name = ''
            lname = (cand_name or '').lower()
            if lname and any(k in lname for k in UI_BLACKLIST):
                print('Candidate OCR matched UI blacklist:', cand_name)
            if lname and any(k in lname for k in DM_KEYWORDS):
                print('Candidate OCR matched DM keywords:', cand_name)
            # If spacing candidate seems invalid (UI or DM or empty), try next downstream
            if (not lname or any(k in lname for k in UI_BLACKLIST) or any(k in lname for k in DM_KEYWORDS)) and candidate_source == 'spacing' and candidate_idx is not None:
                next_idx = candidate_idx + 1
                if next_idx < len(centers_abs):
                    cand_y = centers_abs[next_idx]
                    candidate = (cx, int(cand_y))
                    candidate_idx = next_idx
                    candidate_source = 'spacing-next'
                    print('Spacing candidate was invalid, trying next index', next_idx)
                else:
                    print('No further candidates to try')
            # ensure we don't hover into the titlebar region
            if candidate:
                cy_clamped = _clamp_hover_y(candidate[1])
                candidate = (candidate[0], int(cy_clamped))
                # keep our local cand_y consistent
                cand_x, cand_y = candidate
            # Special-case: if candidate was hover on index 0 and had no OCR, check center 1 which may be server after DM
            final_txt = ''
            try:
                final_txt = _hover_and_read_local(cand_x, cand_y, hover_delay=hover_delay) or ''
            except Exception:
                final_txt = ''
            if (candidate_idx == 0 or candidate_source == 'hover') and (not final_txt) and len(centers_abs) > 1:
                try:
                    alt_idx = 1
                    alt_y = centers_abs[alt_idx]
                    print('Attempting alt index 1 because index 0 had empty OCR; alt_y', alt_y)
                    if pyautogui:
                        alt_y_clamped = max(top + top_skip_px + 6, min(alt_y, top + height - bottom_skip_px - 6))
                        pyautogui.moveTo(cand_x, int(alt_y_clamped), duration=0.12)
                        time.sleep(max(hover_delay, 0.26))
                    alt_txt = _hover_and_read_local(cand_x, alt_y_clamped, hover_delay=hover_delay) or ''
                except Exception:
                    alt_txt = ''
                print('Alt OCR text at index 1:', repr(alt_txt))
                if alt_txt and not any(k in alt_txt.lower() for k in DM_KEYWORDS) and not any(k in alt_txt.lower() for k in UI_BLACKLIST):
                    print('Selecting alt index 1 as first server')
                    candidate = (cand_x, int(alt_y))
                    candidate_idx = alt_idx
                    candidate_source = 'hover-alt'
        if pyautogui and candidate:
            try:
                cand_y_clamped = _clamp_hover_y(cand_y)
                pyautogui.moveTo(cand_x, cand_y_clamped, duration=0.12)
            except Exception:
                pass
        print('Selected first-server candidate:', candidate, 'source:', candidate_source)
        # Enforce starting at DM+1 if we detected DM earlier
        try:
            if dm_idx is not None and candidate_idx is not None and (dm_idx + 1) < len(centers_abs) and candidate_idx != (dm_idx + 1):
                enforced_idx = dm_idx + 1
                print('Enforcing DM+1 start: adjusting candidate from', candidate_idx, 'to', enforced_idx)
                candidate_idx = enforced_idx
                y_enf = _clamp_hover_y(centers_abs[enforced_idx])
                candidate = (cx, int(y_enf))
                candidate_source = 'dm-enforce'
        except Exception:
            pass
        # Final OCR check to confirm the hovered name (helps assert we've found the first server)
        try:
            final_txt = _hover_and_read_local(candidate[0], candidate[1], hover_delay=hover_delay) or ''
        except Exception:
            final_txt = ''
        print('Final OCR text at candidate:', repr(final_txt))
        # If start_index_offset provided and step median computed, adjust candidate position physically by offset servers
        try:
            step = median if (median and median > 4) else 56
        except Exception:
            step = 56
        try:
            if start_index_offset and start_index_offset != 0:
                pixel_offset = int(step * start_index_offset)
                print(f'Applying start_index_offset {start_index_offset} => pixel offset {pixel_offset} (step {step})')
                new_y = int(candidate[1] + pixel_offset)
                # clamp within column
                new_y = _clamp_hover_y(new_y)
                print('Moving to adjusted candidate position', (candidate[0], new_y))
                try:
                    if pyautogui:
                        pyautogui.moveTo(candidate[0], new_y, duration=0.12)
                        time.sleep(max(hover_delay, 0.25))
                except Exception:
                    pass
                try:
                    final_txt2 = _hover_and_read_local(candidate[0], new_y, hover_delay=hover_delay) or ''
                except Exception:
                    final_txt2 = ''
                print('Final OCR text at adjusted candidate:', repr(final_txt2))
                if test_target and final_txt2 and test_target.lower() in final_txt2.lower():
                    print('SUCCESS: test_target matched at adjusted first server')
                    return (candidate[0], new_y)
                final_txt = final_txt2 or final_txt
        except Exception:
            pass
        if test_target:
            if final_txt and test_target.lower() in final_txt.lower():
                print('SUCCESS: test_target matched at first server')
                return candidate
            else:
                # continue detection: reset candidate and try another pass
                print('test_target not matched at selected candidate; continuing detection attempts')
                candidate = None
                candidate_source = None
                candidate_idx = None
                # fall through to outer detect_attempt loop and retry
        else:
            return candidate
        

    # If no tooltip-based match found, fallback to spacing-based detection
    # Local spacing-based heuristic
    def _detect_first_server_index_local(centers_list, top_y=top, header_skip=48):
        if not centers_list:
            return 0
        if len(centers_list) < 3:
            for i, c in enumerate(centers_list):
                if c - top_y > header_skip + 6:
                    return i
            return 0
        diffs = [centers_list[i + 1] - centers_list[i] for i in range(len(centers_list) - 1)]
        diffs_filtered = [d for d in diffs if d > 4]
        if not diffs_filtered:
            return 0
        # compute typical step using a mode-like histogram to avoid large outliers
        diffs_sorted = sorted(diffs_filtered)
        def _typical_step(dlist):
            if not dlist:
                return 56
            # bucket diffs by 2px bins and find most frequent
            from collections import Counter
            buckets = [int(round(d / 2.0) * 2) for d in dlist]
            c = Counter(buckets)
            most_common = c.most_common(1)[0][0]
            return int(most_common)
        median = _typical_step(diffs_sorted)
        tol = max(3, int(median * 0.20))
        large_gap = max(10, int(median * 1.4))
        # If the topmost gap (between centers[0] and centers[1]) is large, treat the first server as index 1
        if diffs and diffs[0] > large_gap:
            return 1
        for i in range(len(diffs)):
            if diffs[i] > large_gap:
                ok_follow = True
                for j in range(i + 1, min(i + 3, len(diffs))):
                    if abs(diffs[j] - median) > tol:
                        ok_follow = False
                        break
                if ok_follow:
                    return i + 1
        for i in range(len(diffs) - 2):
            if abs(diffs[i] - median) <= tol and abs(diffs[i + 1] - median) <= tol and abs(diffs[i + 2] - median) <= tol:
                return i
        for i, c in enumerate(centers_list):
            if c - top_y > header_skip + 6:
                return i
        return 0

    idx = 0
    if centers_abs:
        idx = _detect_first_server_index_local(centers_abs, top_y=top)
        print('Spacing-based first idx:', idx)
    if idx >= len(centers_abs):
        return None
    cy = centers_abs[idx]
    if pyautogui:
        final_y = _clamp_hover_y(cy)
        pyautogui.moveTo(cx, int(final_y), duration=0.12)
    print('No hover match; falling back to spacing-based index', idx, 'pos', (cx, int(final_y)))
    return (cx, int(final_y))


def iterate_all_servers(hover_delay: float = 0.4, debug_save: bool = False, max_servers: int = 50):
    """Iterate through ALL servers and return their names and positions.
    
    Returns list of dicts: [{'index': int, 'y': int, 'name': str or None}, ...]
    
    This function:
    1. Scrolls to top first
    2. Detects servers using spacing-based detection
    3. Iterates through visible servers, collecting names via OCR
    4. Scrolls down using the last server as anchor for overlap
    5. Continues until "Add a Server" is detected or no new servers found
    """
    import time
    
    # Get Discord window
    bbox = None
    if utils:
        try:
            bbox = utils.find_and_focus_discord()
        except Exception:
            bbox = None
    
    if not bbox:
        print('Discord window not found')
        return []
    
    left, top, width, height = bbox
    col_w = max(48, int(width * 0.08))
    col_box = (max(0, left - 2), top, min(left + col_w + 4, left + width), top + height)
    cx = left + (col_w // 2)
    
    # Debug save dir
    debug_dir = None
    if debug_save:
        try:
            debug_dir = os.path.join('data', 'debug')
            os.makedirs(debug_dir, exist_ok=True)
        except Exception:
            debug_dir = None
    
    # Safety margins
    top_skip_px = 48
    bottom_skip_px = 64
    
    def _clamp_y(y):
        miny = top + top_skip_px + 6
        maxy = top + height - bottom_skip_px - 6
        return int(max(miny, min(y, maxy)))
    
    # Keywords to detect end/DM
    DM_KEYWORDS = ('direct messages', 'direct message', 'home', 'friends', 'messages')
    END_KEYWORDS = ('add a server', 'add server', 'create', 'explore')
    
    # Scroll to ABSOLUTE TOP - use direct scrolling since _seek_extreme may have reversed sign
    print('Scrolling to top...')
    if pyautogui:
        # Move mouse to server column first
        try:
            safe_y = top + height // 2
            pyautogui.moveTo(cx, safe_y, duration=0.1)
            time.sleep(0.1)
        except Exception:
            pass
        
        # Aggressive scroll up - positive = scroll up on macOS
        for i in range(40):
            try:
                pyautogui.scroll(15)
                time.sleep(0.03)
            except Exception:
                pass
            # Brief pause every 10 scrolls
            if (i + 1) % 10 == 0:
                time.sleep(0.15)
        time.sleep(0.4)
    
    # Helper to read tooltip at position
    def _read_tooltip(y_pos):
        name = None
        if not pytesseract:
            return None
        # Move and wait for tooltip
        if pyautogui:
            try:
                pyautogui.moveTo(cx, y_pos, duration=0.08)
                time.sleep(hover_delay)
            except Exception:
                pass
        # Try multiple tooltip positions
        tooltip_boxes = [
            (cx + 50, y_pos - 20, cx + 280, y_pos + 20),
            (cx + 40, y_pos - 35, cx + 320, y_pos + 15),
            (cx + 15, y_pos - 30, cx + 250, y_pos + 10),
        ]
        for tb in tooltip_boxes:
            try:
                tbimg = _safe_grab(tb)
                if tbimg:
                    if ImageOps:
                        tbimg = ImageOps.autocontrast(tbimg.convert('RGB'))
                    txt = pytesseract.image_to_string(tbimg, config='--psm 7').strip()
                    if txt and len(txt) > 1:
                        return txt
            except Exception:
                pass
        return None
    
    # Helper to detect and analyze current viewport
    def _analyze_viewport(is_first_page=True):
        col_img = _safe_grab(col_box)
        if col_img is None:
            return [], None, None
        
        gray = col_img.convert('L')
        w, h = gray.size
        centers = _vertical_projection_centers(gray, w, h)
        centers_abs = [top + c for c in centers]
        
        if len(centers) < 2:
            return centers_abs, None, 48
        
        # Calculate spacing
        diffs = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
        diffs_sorted = sorted([d for d in diffs if d > 10])
        median = diffs_sorted[len(diffs_sorted) // 2] if diffs_sorted else 48
        
        # Only look for DM gap on first page
        dm_idx = None
        if is_first_page:
            large_gap_thresh = int(median * 1.6)  # Require bigger gap for DM detection
            very_large_thresh = int(median * 1.8)
            
            for i, d in enumerate(diffs):
                if d >= very_large_thresh:
                    dm_idx = i  # DM is at index i, first server at i+1
                    break
                elif d >= large_gap_thresh and i < 4:
                    # Only consider large gap as DM if it's near the top
                    dm_idx = i
                    break
        
        return centers_abs, dm_idx, median
    
    # Main iteration
    all_servers = []
    page = 0
    max_pages = 20  # Safety limit
    reached_end = False
    total_servers_counted = 0  # Track total position in the list
    
    while page < max_pages and not reached_end:
        print(f'\n=== Page {page} ===')
        
        # Analyze current viewport
        centers_abs, dm_idx, median = _analyze_viewport(is_first_page=(page == 0))
        print(f'Detected {len(centers_abs)} icons, dm_idx={dm_idx}, median={median}')
        
        if not centers_abs:
            print('No icons detected, stopping')
            break
        
        # Determine start index for this page
        if page == 0:
            # FIRST PAGE: Must find DM anchor via OCR to ensure we're at the top
            dm_found_idx = None
            print('Scanning for DM anchor via OCR...')
            
            for i, cy in enumerate(centers_abs[:6]):  # Check first 6 icons max
                y_hover = _clamp_y(cy)
                txt = _read_tooltip(y_hover)
                ltxt = (txt or '').lower()
                print(f'  Icon {i} at y={cy}: "{txt}"')
                
                if any(k in ltxt for k in DM_KEYWORDS):
                    dm_found_idx = i
                    print(f'  -> DM found at index {i}')
                    break
            
            if dm_found_idx is None:
                print('WARNING: Could not confirm DM anchor!')
                if dm_idx is not None:
                    start_idx = dm_idx + 1
                else:
                    start_idx = 0
            else:
                start_idx = dm_found_idx + 1
                print(f'DM confirmed at index {dm_found_idx}, starting at index {start_idx}')
        else:
            # SUBSEQUENT PAGES: We scrolled so that 3 icons overlap
            # Skip those 3 overlap icons and start fresh
            start_idx = 3
            print(f'Page {page}: skipping first 3 icons (overlap from previous page)')
        
        # Calculate safe iteration range
        # Skip last 2 icons to avoid "NEW" notification occlusion at bottom
        iter_start = start_idx
        iter_end = len(centers_abs) - 2
        iter_end = max(iter_start + 1, iter_end)
        
        print(f'Iterating from index {iter_start} to {iter_end - 1}')
        
        new_servers_this_page = 0
        
        for i in range(iter_start, iter_end):
            cy = centers_abs[i]
            y_hover = _clamp_y(cy)
            
            # Read tooltip - but DON'T skip position if OCR fails
            name = _read_tooltip(y_hover)
            lname = (name or '').lower()
            
            # Check for end marker
            if name and any(k in lname for k in END_KEYWORDS):
                print(f'  Detected end marker "{name}" at index {i}')
                reached_end = True
                break
            
            # Skip DM (shouldn't happen after page 0, but safety check)
            if name and any(k in lname for k in DM_KEYWORDS):
                print(f'  Skipping DM at index {i}: {name}')
                continue
            
            # Valid server position - ALWAYS add (trust spacing, OCR may fail)
            new_servers_this_page += 1
            total_servers_counted += 1
            
            server_info = {
                'index': len(all_servers),
                'raw_index': i,
                'page': page,
                'y': y_hover,
                'name': name  # May be None - that's OK
            }
            all_servers.append(server_info)
            print(f'  Server {server_info["index"]}: y={y_hover}, name={repr(name)}')
            
            # Save debug image
            if debug_dir:
                try:
                    hf = os.path.join(debug_dir, f'server_{len(all_servers)}_{int(time.time()*1000)}.png')
                    rr = (cx - 50, int(cy) - 40, cx + 300, int(cy) + 40)
                    img = _safe_grab(rr)
                    if img:
                        img.save(hf)
                except Exception:
                    pass
            
            # Safety limit
            if len(all_servers) >= max_servers:
                print(f'Reached max_servers limit ({max_servers})')
                reached_end = True
                break
        
        if reached_end:
            break
        
        # Check if we found any new servers
        if new_servers_this_page == 0:
            print('No new servers found on this page, stopping')
            break
        
        # Scroll down for next page
        # We want to keep 3 icons as overlap, so scroll by (processed - 3) icons
        overlap_icons = 3
        icons_to_scroll = max(1, new_servers_this_page - overlap_icons)
        
        # On macOS: ~4 scroll units per server icon (empirically determined)
        scroll_per_icon = 4
        total_scroll = icons_to_scroll * scroll_per_icon
        
        print(f'Scrolling down by {icons_to_scroll} icons ({total_scroll} scroll units)...')
        
        if pyautogui:
            try:
                for _ in range(total_scroll):
                    pyautogui.scroll(-1)  # Small increments, negative = down
                    time.sleep(0.03)
            except Exception:
                pass
        
        time.sleep(0.35)
        page += 1
    
    print(f'\n=== Finished: found {len(all_servers)} servers across {page + 1} pages ===')
    return all_servers


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Find and hover the first Discord server')
    parser.add_argument('--test-target', help='Optional server name substring to use for testing', default=None)
    parser.add_argument('--wait-top', action='store_true', help='Attempt a stronger seek-to-top before scanning')
    parser.add_argument('--force-run', action='store_true', help='Force running/starting Discord')
    parser.add_argument('--debug-save', action='store_true', help='Save debug screenshots (column and hover images)')
    parser.add_argument('--max-centers', type=int, default=0, help='Limit number of centers to consider (quick tests)')
    parser.add_argument('--hover-delay', type=float, default=0.6, help='Hover delay used for reading tooltip')
    parser.add_argument('--max-icon-retries', type=int, default=3, help='Max jitter retries per icon for tooltip OCR')
    parser.add_argument('--start-index-offset', type=int, default=0, help='Adjust start index by N servers (negative means earlier)')
    parser.add_argument('--list-all', action='store_true', help='List all servers instead of just finding the first')
    args = parser.parse_args()
    
    if args.list_all:
        servers = iterate_all_servers(
            hover_delay=args.hover_delay,
            debug_save=args.debug_save,
            max_servers=args.max_centers if args.max_centers > 0 else 50
        )
        print(f'\\nFound {len(servers)} servers:')
        for s in servers:
            print(f'  [{s["index"]}] {s["name"] or "(no name detected)"}')
    else:
        res = find_and_hover_first_server(
            start_from_top=args.wait_top,
            hover_delay=args.hover_delay,
            test_target=args.test_target,
            force_run=args.force_run,
            debug_save=args.debug_save,
            max_centers=(args.max_centers if args.max_centers > 0 else None),
            max_icon_retries=args.max_icon_retries,
            start_index_offset=args.start_index_offset,
        )
        print('Hovered at:', res)
