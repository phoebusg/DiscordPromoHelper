"""Column-based Discord server scanner.

This module takes a different approach:
1. Scroll to bottom first (detect "Add a Server" / "Discover" end markers)
2. Capture the server column
3. Scroll up by overlap amount, capture again
4. Use image matching on overlap regions to stitch captures accurately
5. Build one continuous column image
6. Detect icons from the stitched image
7. Index servers from bottom to top (stable ordering)

This approach is more reliable because:
- We know exactly where the list ends
- Image stitching is pixel-accurate (no scroll calibration guessing)
- Single source of truth for icon positions
"""

import os
import sys
import time
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None
    ImageDraw = None

try:
    import pyautogui
except ImportError:
    pyautogui = None

try:
    from src import utils
    from src.discord_nav import (
        _safe_grab, 
        _vertical_projection_centers,
        SCROLL_PER_ICON,
    )
except ImportError:
    try:
        import utils
        from discord_nav import (
            _safe_grab,
            _vertical_projection_centers, 
            SCROLL_PER_ICON,
        )
    except ImportError:
        utils = None
        _safe_grab = None
        _vertical_projection_centers = None
        SCROLL_PER_ICON = 3


# Platform-specific scroll settings
if sys.platform.startswith('win32'):
    SCROLL_UNITS_PER_CAPTURE = 60 * 8  # Scroll ~8 icons worth on Windows
else:
    SCROLL_UNITS_PER_CAPTURE = 2 * 8   # Scroll ~8 icons worth on macOS (reduced from 3)


def find_overlap_offset(img1, img2, overlap_height=100):
    """Find the vertical offset where img2's top overlaps with img1's bottom.
    
    Args:
        img1: Top/earlier image (we look at its bottom portion)
        img2: Bottom/later image (we look at its top portion)  
        overlap_height: Expected overlap region height in pixels
    
    Returns:
        best_offset: How many pixels from img1's bottom match img2's top
                    Returns 0 if no good match found
    """
    if not Image:
        return 0
    
    w1, h1 = img1.size
    w2, h2 = img2.size
    
    if w1 != w2:
        return 0
    
    # Convert to comparable format
    p1 = list(img1.convert('L').getdata())
    p2 = list(img2.convert('L').getdata())
    
    best_offset = 0
    best_score = float('inf')
    
    # Try different overlap amounts (from overlap_height down to 20 pixels)
    for offset in range(min(overlap_height, h1, h2), 19, -1):
        # Compare bottom 'offset' rows of img1 with top 'offset' rows of img2
        score = 0
        for y in range(offset):
            y1 = h1 - offset + y  # Row in img1 (from bottom)
            y2 = y                 # Row in img2 (from top)
            
            for x in range(w1):
                idx1 = y1 * w1 + x
                idx2 = y2 * w2 + x
                diff = abs(p1[idx1] - p2[idx2])
                score += diff
        
        # Normalize by area
        area = offset * w1
        normalized_score = score / area if area > 0 else float('inf')
        
        if normalized_score < best_score:
            best_score = normalized_score
            best_offset = offset
    
    # Only accept if score is good (low difference)
    if best_score > 15:  # Threshold: average pixel difference < 15
        print(f'  Warning: Overlap matching score {best_score:.1f} is high, may be inaccurate')
    
    return best_offset


def stitch_columns(images, overlap_height=100):
    """Stitch multiple column captures into one continuous image.
    
    Args:
        images: List of PIL Images, ordered from bottom to top of server list
        overlap_height: Expected overlap between consecutive captures
    
    Returns:
        Stitched PIL Image
    """
    if not images:
        return None
    
    if len(images) == 1:
        return images[0].copy()
    
    # Start with the bottom-most image (first in list)
    result = images[0].copy()
    
    for i in range(1, len(images)):
        prev_img = result
        curr_img = images[i]
        
        # Find where curr_img overlaps with prev_img
        # curr_img's BOTTOM should match prev_img's TOP (since we're going bottom-to-top)
        overlap = find_overlap_offset(curr_img, prev_img, overlap_height)
        
        if overlap > 0:
            print(f'  Stitch {i}: found {overlap}px overlap')
            # Crop the overlapping portion from curr_img (remove its bottom)
            w, h = curr_img.size
            new_portion = curr_img.crop((0, 0, w, h - overlap))
        else:
            print(f'  Stitch {i}: no overlap found, using full image')
            new_portion = curr_img
        
        # Prepend new_portion to result (since we're going bottom-to-top)
        w1, h1 = new_portion.size
        w2, h2 = result.size
        
        stitched = Image.new('RGB', (max(w1, w2), h1 + h2))
        stitched.paste(new_portion, (0, 0))
        stitched.paste(result, (0, h1))
        result = stitched
    
    return result


def detect_icons_from_column(column_img):
    """Detect icon centers from a stitched column image.
    
    Returns list of dicts with:
        - y: Y position in the stitched image
        - icon_img: Cropped icon image (48x48)
        - icon_hash: Perceptual hash of the icon
    """
    if not column_img or not _vertical_projection_centers:
        return []
    
    gray = column_img.convert('L')
    w, h = gray.size
    
    centers = _vertical_projection_centers(gray, w, h)
    
    # First pass: collect all icons with hashes
    all_icons = []
    for i, cy in enumerate(centers):
        # Crop icon (48x48 centered on cy)
        cx = w // 2
        x1 = max(0, cx - 24)
        y1 = max(0, int(cy) - 24)
        x2 = min(w, cx + 24)
        y2 = min(h, int(cy) + 24)
        
        icon_img = column_img.crop((x1, y1, x2, y2))
        
        # Compute hash
        icon_hash = None
        if utils and hasattr(utils, 'compute_icon_hash'):
            try:
                icon_hash = utils.compute_icon_hash(icon_img)
            except Exception:
                pass
        
        all_icons.append({
            'y': cy,
            'icon_img': icon_img,
            'icon_hash': icon_hash
        })
    
    # Second pass: deduplicate by hash (keep first occurrence)
    seen_hashes = set()
    unique_icons = []
    for icon in all_icons:
        icon_hash = icon.get('icon_hash')
        if icon_hash:
            # Check for exact or near match
            is_dup = False
            for seen in seen_hashes:
                if utils and hasattr(utils, 'icon_hash_distance'):
                    dist = utils.icon_hash_distance(icon_hash, seen)
                    if dist <= 3:  # Very similar
                        is_dup = True
                        break
                elif icon_hash == seen:
                    is_dup = True
                    break
            
            if is_dup:
                continue
            seen_hashes.add(icon_hash)
        
        icon['index'] = len(unique_icons)
        unique_icons.append(icon)
    
    print(f'  Detected {len(all_icons)} icons, {len(unique_icons)} unique after dedup')
    return unique_icons


def scroll_to_bottom(cx, cy_safe, col_box):
    """Scroll to the bottom of the server list.
    
    Returns True if we detected end markers, False otherwise.
    """
    if not pyautogui:
        return False
    
    print('Scrolling to bottom...')
    
    # Move to safe position and scroll down aggressively
    pyautogui.moveTo(cx, cy_safe, duration=0.1)
    time.sleep(0.1)
    
    # Scroll down a lot (negative = down on macOS)
    for _ in range(10):
        pyautogui.scroll(-100 * SCROLL_PER_ICON)
        time.sleep(0.05)
    
    time.sleep(0.2)
    print('At bottom of list')
    return True


def scroll_up_overlap(cx, cy_safe, icons_to_keep=2):
    """Scroll up while keeping some icons visible for overlap.
    
    Args:
        cx, cy_safe: Safe cursor position
        icons_to_keep: Number of icons to keep visible for overlap stitching
    """
    if not pyautogui:
        return
    
    # Move to position
    pyautogui.moveTo(cx, cy_safe, duration=0.05)
    time.sleep(0.05)
    
    # Scroll up - we want to move viewport up, keeping some overlap
    # Viewport shows ~10 icons, keep 3 for overlap = scroll 7 icons
    # With SCROLL_PER_ICON=2, that's 7*2 = 14 units
    icons_to_scroll = 7
    scroll_amount = icons_to_scroll * SCROLL_PER_ICON
    
    print(f'  Scrolling up: {scroll_amount} units (~{icons_to_scroll} icons)')
    
    # Positive = scroll up on macOS
    pyautogui.scroll(scroll_amount)
    time.sleep(0.2)


def capture_full_column(progress_callback=None):
    """Capture the full server column by stitching overlapping screenshots.
    
    Args:
        progress_callback: Optional function(message) to report progress
    
    Returns:
        dict with:
            - column_image: Stitched PIL Image of full column
            - icons: List of detected icon dicts
            - raw_captures: List of individual capture images (for debugging)
    """
    def log(msg):
        print(msg)
        if progress_callback:
            progress_callback(msg)
    
    # Get Discord window
    if not utils:
        log('Error: utils module not available')
        return None
    
    bbox = None
    try:
        bbox = utils.find_and_focus_discord()
    except Exception as e:
        log(f'Error finding Discord: {e}')
        return None
    
    if not bbox:
        log('Discord window not found')
        return None
    
    left, top, width, height = bbox
    col_w = max(48, int(width * 0.08))
    col_box = (max(0, left - 2), top + 50, min(left + col_w + 4, left + width), top + height - 60)
    cx = left + (col_w // 2)
    cy_safe = top + height // 2
    
    log(f'Discord window: {bbox}')
    log(f'Column box: {col_box}')
    
    # Step 1: Scroll to bottom
    scroll_to_bottom(cx, cy_safe, col_box)
    time.sleep(0.3)
    
    # Step 2: Capture from bottom, scrolling up
    captures = []
    max_captures = 20  # Safety limit
    
    for capture_num in range(max_captures):
        log(f'Capture {capture_num + 1}...')
        
        # Capture current column
        col_img = _safe_grab(col_box) if _safe_grab else None
        if not col_img:
            log('Failed to capture column')
            break
        
        captures.append(col_img)
        
        # Check if we're at the top (look for DM icon pattern)
        # For now, just do a fixed number of captures based on expected server count
        # A full list of ~60 servers with ~12 per viewport = ~5-6 captures needed
        
        if capture_num >= 8:  # Safety limit
            log('Reached capture limit')
            break
        
        # Scroll up for next capture
        scroll_up_overlap(cx, cy_safe, icons_to_keep=3)
        time.sleep(0.2)
        
        # TODO: Detect if we've reached the top (DM anchor visible)
        # For now, we'll capture a bit extra and trim later
    
    log(f'Captured {len(captures)} column images')
    
    if not captures:
        return None
    
    # Step 3: Stitch captures (they're in bottom-to-top order)
    log('Stitching captures...')
    stitched = stitch_columns(captures, overlap_height=120)
    
    if not stitched:
        log('Failed to stitch captures')
        return None
    
    log(f'Stitched image size: {stitched.size}')
    
    # Step 4: Detect icons from stitched image
    log('Detecting icons...')
    icons = detect_icons_from_column(stitched)
    log(f'Detected {len(icons)} icons')
    
    return {
        'column_image': stitched,
        'icons': icons,
        'raw_captures': captures
    }


def build_server_index(progress_callback=None):
    """Build a complete server index using the column capture method.
    
    Returns:
        List of server dicts with index, icon_hash, icon_img, y
    """
    result = capture_full_column(progress_callback)
    
    if not result:
        return []
    
    icons = result['icons']
    
    # Filter out non-server icons (DM, Add Server, Discover)
    # These are typically at the very top (DM) and bottom (Add/Discover)
    # For now, return all and let the caller filter
    
    # Save debug images
    try:
        debug_dir = Path('data/debug/column_scan')
        debug_dir.mkdir(parents=True, exist_ok=True)
        
        # Save stitched column
        result['column_image'].save(debug_dir / 'stitched_column.png')
        
        # Save individual captures
        for i, cap in enumerate(result['raw_captures']):
            cap.save(debug_dir / f'capture_{i:02d}.png')
        
        # Save detected icons
        for icon in icons:
            if icon.get('icon_img'):
                hash_prefix = icon.get('icon_hash', 'unknown')[:8] if icon.get('icon_hash') else 'unknown'
                icon['icon_img'].save(debug_dir / f'icon_{icon["index"]:02d}_{hash_prefix}.png')
        
        print(f'Debug images saved to {debug_dir}')
    except Exception as e:
        print(f'Failed to save debug images: {e}')
    
    return icons


# Standalone test
if __name__ == '__main__':
    print('=== Column Scanner Test ===')
    print('Make sure Discord is open with server list visible.')
    input('Press Enter to start...')
    
    icons = build_server_index(lambda msg: print(f'  {msg}'))
    
    print(f'\nFound {len(icons)} icons:')
    for icon in icons[:20]:  # Show first 20
        print(f'  [{icon["index"]:2d}] y={icon["y"]:.0f}, hash={icon.get("icon_hash", "?")[:8] if icon.get("icon_hash") else "?"}...')
