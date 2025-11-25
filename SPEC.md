## Discord Sidebar First-Server Detection - Spec & Next Steps

Summary:
- The helper now focuses Discord, captures the server column, and determines discrete centers for server icons using grayscale vertical projection and a second peak-based detector.
- Hover-based OCR is required to confirm a server; empty OCR is ignored and candidate selection prefers the first valid, non-DM OCR result.
- DM detection uses both color-based detection (Discord's blurple #5865F2) and OCR fallback.
- Peak detection merges nearby peaks (within 40px) to avoid spurious centers.

Recent Improvements (Nov 2025):
- Fixed peak detection: increased merge gap from 8px to 40px, raised threshold to 15% of max projection
- Improved OCR capture: wider tooltip boxes, contrast enhancement via ImageOps.autocontrast
- Added DM color detection: checks for Discord's blurple color before falling back to OCR
- Increased hover delays to allow tooltips to appear

Known Current Behavior:
- Detects 3-5 centers in typical Discord sidebar (down from 33 spurious peaks)
- Starts at spacing-detected index 0 or 1 depending on DM detection
- Final OCR captures partial server names - tooltip box positioning needs tuning
- Cursor is clamped to avoid titlebar interference

Outstanding Issues & What To Fix Next:
1. Tooltip capture box positioning:
   - Final OCR captures partial text ("eat" instead of "Brave Alice Games")
   - Need to adjust capture box coordinates to be more centered on tooltip

2. OCR during hover iteration returns empty:
   - Tooltip appears after hover but OCR capture timing may be off
   - Consider adding explicit wait-for-tooltip logic

3. Consistent center detection:
   - Peak detection gives 3-5 centers depending on scroll position
   - May need to normalize projection values before peak detection

4. DM color detection:
   - Color-based detection added but not triggering in tests
   - May need to capture RGB image before grayscale conversion

Quick Tests To Run Locally:
```
source .venv/bin/activate
python src/discord_nav.py --debug-save --hover-delay 0.7
```

Developer Notes:
- Peak merge gap set to 40px (typical icon spacing is 48-56px)
- Projection threshold raised to 15% to filter noise
- Tooltip boxes widened to 280-350px width
- Added ImageOps.autocontrast for better OCR

Next Iteration (High Priority):
- Fix tooltip capture box positioning to get full server names
- Add wait-for-tooltip logic before OCR capture
- Debug why DM color detection isn't triggering
- Add unit tests for projection and peak detection

End of SPEC
