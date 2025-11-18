## Discord Sidebar First-Server Detection - Spec & Next Steps

Summary:
- The helper now focuses Discord, captures the server column, and determines discrete centers for server icons using grayscale vertical projection and a second peak-based detector.
- Hover-based OCR is required to confirm a server; empty OCR is ignored and candidate selection prefers the first valid, non-DM OCR result.
- DM detection is enforced if DM/home is detected in the viewport; the helper attempts to hover icons right after DM to select the first server deterministically.

Known Current Behavior:
- Starts at a spacing-detected index; spacing can pick a later server cluster if the top icons produce no tooltip text.
- Peak-based detection helps split merged icon regions; still sometimes returns a single center if column crop or theme merges icons.
- Cursor is clamped to avoid titlebar interference; fallback heuristics adjust hover position when initial OCR is empty.

Outstanding Issues & What To Fix Next:
1. Improve stability of center detection when single center is detected:
   - Refine `_vertical_projection_centers()` thresholding and merging rules.
   - Add color/alpha detection (RGB channel or alpha) in addition to grayscale to help with custom themes.
   - Add a column-left/right expansion test to ensure full icon area is captured.

2. Precisely find DM (home) icon and the server right after it in all layouts:
   - Detect DM via icon pixel pattern/color rather than relying solely on OCR text (Direct Messages), which helps with localization and different tooltip styles.
   - If DM is detected, ensure `start_idx == dm_idx + 1` unless `start-index-offset` is explicitly set.

3. Reduce skipped icons during iteration:
   - Use a calibrated pixel step (median) derived from `vertical_projection` but also cross-check with measured distances between hover successes.
   - Add a single-pass fine-grained scan between the chosen indices to find adjacent icons.

4. Improve OCR success for tooltips:
   - Try different OCR psm configs for tooltips, and try enhancing contrast before passing to Tesseract.
   - Implement more jitter patterns for hidden tooltips (vertical + horizontal micro-movements).

5. Add unit and regression tests using saved column and hover debug images (data/debug):
   - Tests for `vertical_projection_centers()` with known images; evaluate step size detection and top-gap recognition.
   - Tests for `peak` fallback scenarios where icons merge.

6. Add CLI flags for tuning and runtime testing (done partially):
   - `--start-index-offset`, `--max-centers`, `--hover-delay`, `--debug-save` should be kept and expanded with `--force-top` and `--force-peaks` for testing.

7. Tuning and heuristics: fine tune `FIRST_SERVER_SCAN_MAX`, `var_threshold`, `merge_gap` and `top_skip_px` for different Discord themes and UI configurations.

Quick Tests To Run Locally:
```
source .venv/bin/activate
python src/discord_nav.py --debug-save --wait-top --hover-delay 0.6
```

Developer Notes:
- The default `start-index-offset` has been set to `0` (no offset) to avoid unintended shifts.
- The code attempts broader column captures when only one center is found; the peak detector tries to split merged regions.
- The fallback logic is conservative: prefer earliest OCR-confirmed server; if DM is detected, prefer DM+1.

Next Iteration (High Priority):
- Add color matching to detect DM icon and server icons.
- Refine center detection thresholds and add tests for merged-case scenarios.
- Add an option to log the centers and OCR results to a test file for further offline analysis.

If you want, I can add color-based detection and unit tests next (recommended).

End of SPEC
