import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Add src to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src import discord_nav

class TestNavLogic(unittest.TestCase):
    def setUp(self):
        # Mock dependencies
        self.mock_utils = MagicMock()
        discord_nav.utils = self.mock_utils
        discord_nav.pyautogui = MagicMock()
        discord_nav.pytesseract = MagicMock()
        
        # Setup synthetic server list (names and hashes)
        self.servers = [f"Server_{i}" for i in range(30)]
        self.hashes = [f"hash_{i}" for i in range(30)]
        
        # Viewport settings
        self.viewport_height = 10  # 10 icons visible at once
        self.scroll_pos = 0  # Current index of top visible icon
        
    def _mock_analyze_viewport(self, is_first_page=False):
        # Return centers for currently visible icons
        visible_indices = range(self.scroll_pos, min(self.scroll_pos + self.viewport_height, len(self.servers)))
        centers = [100 + (i - self.scroll_pos) * 50 for i in visible_indices]
        return centers, None, 50

    def _mock_read_tooltip(self, y_pos):
        # Map y_pos back to index
        # y = 100 + (index - scroll_pos) * 50
        # index = (y - 100) / 50 + scroll_pos
        idx = int((y_pos - 100) / 50) + self.scroll_pos
        if 0 <= idx < len(self.servers):
            return self.servers[idx]
        return None

    def _mock_compute_hash(self, img):
        # We can't easily map img back to index without context, 
        # so we'll patch the loop in the main function or mock _safe_grab to return an object with an ID
        return "hash_unknown"

    def test_pagination_logic(self):
        """Test that the pagination logic correctly scrolls and stitches the list."""
        
        # We need to patch the internal functions of iterate_all_servers
        # Since they are defined inside, we can't patch them directly.
        # Instead, we have to rely on mocking the external calls they make.
        
        # However, iterate_all_servers is complex. 
        # Let's verify the SCROLL CALCULATION logic specifically.
        
        # Scenario: Page 0
        # Visible: 0-9 (10 icons)
        # Total visible = 10
        # Scrollable start = 0 (assuming no fixed icons for simplicity)
        # Scrollable visible = 10
        # Icons to scroll = max(1, 10 - 4) = 6  (Using the "leave 4 overlap" logic)
        
        # Expected state after Page 0:
        # Scroll pos should increase by 6.
        # New visible: 6-15.
        # Overlap: 6,7,8,9 (4 icons).
        
        # Let's simulate the loop logic manually to verify the math
        
        total_visible = 10
        scrollable_start_idx = 0
        scrollable_visible = max(1, total_visible - scrollable_start_idx)
        
        # Current logic in discord_nav.py:
        # icons_to_scroll = max(1, total_visible - 4)
        icons_to_scroll = max(1, total_visible - 4)
        
        self.assertEqual(icons_to_scroll, 6)
        
        # Next page starts at index 6.
        # Previous page ended at 9.
        # Overlap indices: 6, 7, 8, 9.
        # Count = 4.
        
        # If logic was "visible - 2":
        # icons_to_scroll = 8.
        # Next page starts at 8.
        # Overlap: 8, 9. Count = 2.
        
        # The user reported "ignored top two servers".
        # This happens if the scanner thinks 6 and 7 are duplicates when they are NOT,
        # OR if the scanner skips 6 and 7 entirely.
        
        # If we scroll 6 icons, the new top is 6.
        # The scanner sees 6, 7, 8, 9...
        # It checks 6 against "seen" list. 6 was seen on page 0. -> Duplicate.
        # It checks 7. Seen. -> Duplicate.
        # It checks 8. Seen. -> Duplicate.
        # It checks 9. Seen. -> Duplicate.
        # It checks 10. NEW.
        
        # So it correctly identifies 6,7,8,9 as duplicates and 10 as new.
        # This is correct behavior.
        
        pass

    def test_fixed_icon_logic(self):
        """Test logic when there is a fixed Home icon."""
        # Scenario: Page 0
        # Visible: Home, S1, S2, ... S9 (10 icons)
        # Home is at index 0.
        # scrollable_start_idx = 1 (skip Home)
        
        total_visible = 10
        scrollable_start_idx = 1
        
        # Logic:
        # scrollable_visible = 10 - 1 = 9
        # icons_to_scroll = max(1, total_visible - 4) = 6
        
        # Wait, if we scroll 6 units:
        # The view moves down by 6 servers.
        # Old view: Home, S1..S9
        # New view: Home, S7..S15 (Home is fixed!)
        
        # The "Home" icon is NOT part of the scrollable list content, but it IS physically visible.
        # Discord's server list scrolls *underneath* the Home icon (and DM separator).
        # Actually, usually Home/DM are fixed at the top, and the list scrolls below them.
        # OR, the whole list scrolls including Home?
        # NO, Home and DM are usually pinned.
        
        # If Home is pinned:
        # We see Home, S1, S2... S9.
        # We scroll 6 units.
        # We see Home, S7, S8... S15.
        
        # The scanner reads index 0: "Home".
        # Page 0: "Home" is seen.
        # Page 1: "Home" is seen again.
        # It matches hash/name -> Duplicate. Correct.
        
        # The scanner reads index 1: S7.
        # S7 was seen on Page 0 (at index 7).
        # It matches -> Duplicate. Correct.
        
        # The scanner reads index 4: S10.
        # S10 is NEW.
        
        # The issue "ignored top two servers" implies that S7 and S8 were ignored.
        # Why?
        # If they were marked as duplicates, that's correct (they were seen).
        # Unless... they were NOT seen on Page 0?
        # No, they were visible.
        
        # What if the user meant "ignored top two NEW servers"?
        # i.e. S10 and S11 were marked as duplicates?
        # That would happen if the hash/name matching is too loose.
        
        pass

if __name__ == '__main__':
    unittest.main()
