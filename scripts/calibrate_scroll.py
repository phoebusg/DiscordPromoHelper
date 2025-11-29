#!/usr/bin/env python3
"""
Simple script to calibrate scroll amount on macOS.
Run with Discord open and server list visible.
We'll scroll by a known amount and count how many icons moved.
"""
import sys
import time
sys.path.insert(0, '.')

from src.discord_nav import _safe_grab, _vertical_projection_centers
import pyautogui

# Discord server column box (adjust if needed)
# These should match what discord_nav uses
LEFT = 10
TOP = 80
WIDTH = 72
BOTTOM_OFFSET = 100

def get_icon_centers():
    """Get list of icon Y centers in current viewport."""
    import pyautogui
    screen_w, screen_h = pyautogui.size()
    col_box = (LEFT, TOP, LEFT + WIDTH, screen_h - BOTTOM_OFFSET)
    
    col_img = _safe_grab(col_box)
    if not col_img:
        return []
    
    gray = col_img.convert('L')
    w, h = gray.size
    centers = _vertical_projection_centers(gray, w, h)
    # Convert to absolute screen Y
    return [TOP + c for c in centers]


def main():
    print("=== Scroll Calibration Tool ===")
    print("Make sure Discord is open and server list is visible.")
    print("Move mouse over Discord server column, then we'll test scrolling.")
    print()
    
    input("Press Enter when ready...")
    
    # Get initial icon positions
    before = get_icon_centers()
    print(f"Before scroll: {len(before)} icons detected")
    print(f"  First 5 Y positions: {before[:5]}")
    
    # Move mouse to server column area
    cx = LEFT + WIDTH // 2
    cy = before[len(before)//2] if before else TOP + 200
    pyautogui.moveTo(cx, cy, duration=0.2)
    time.sleep(0.3)
    
    # Scroll down by a known amount
    SCROLL_AMOUNT = -30  # Negative = scroll down
    print(f"\nScrolling by {SCROLL_AMOUNT} units...")
    pyautogui.scroll(SCROLL_AMOUNT)
    time.sleep(0.5)
    
    # Get new icon positions
    after = get_icon_centers()
    print(f"After scroll: {len(after)} icons detected")
    print(f"  First 5 Y positions: {after[:5]}")
    
    # Calculate how much the icons moved
    if before and after:
        # Find a reference icon that appears in both
        # The icons should have moved UP in screen coordinates (lower Y)
        # If first icon was at Y=100 and now at Y=50, it moved up by 50 pixels
        
        # Simple: compare first icon position
        move_pixels = before[0] - after[0] if len(before) > 0 and len(after) > 0 else 0
        
        # Icon spacing is ~48-52 pixels
        ICON_SPACING = 48
        icons_moved = move_pixels / ICON_SPACING
        
        print(f"\nAnalysis:")
        print(f"  First icon moved: {move_pixels} pixels UP")
        print(f"  Assuming {ICON_SPACING}px per icon = {icons_moved:.1f} icons")
        print(f"  Scroll amount: {abs(SCROLL_AMOUNT)} units")
        print(f"  => Units per icon: {abs(SCROLL_AMOUNT) / icons_moved:.1f}" if icons_moved > 0 else "  => Could not calculate")
        
        if icons_moved > 0:
            recommended = abs(SCROLL_AMOUNT) / icons_moved
            print(f"\n  RECOMMENDED SCROLL_PER_ICON = {recommended:.0f}")


if __name__ == '__main__':
    main()
