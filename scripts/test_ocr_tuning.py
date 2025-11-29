#!/usr/bin/env python3
"""
OCR Tuning Test Script

Quick iteration script to test different OCR preprocessing and configuration
approaches on saved tooltip images. Run this to see what works best for
Discord tooltip text extraction.

Usage:
    python scripts/test_ocr_tuning.py
    python scripts/test_ocr_tuning.py --image data/debug/server_1_*.png
    python scripts/test_ocr_tuning.py --capture  # Capture a new tooltip live
"""
import os
import sys
import glob
import time
import argparse
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from PIL import Image, ImageOps, ImageEnhance, ImageFilter, ImageStat
    import pytesseract
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Run: pip install pillow pytesseract")
    sys.exit(1)


def get_debug_images(pattern: str = None) -> list[Path]:
    """Get list of debug images to test on."""
    debug_dir = Path(__file__).parent.parent / "data" / "debug"
    if not debug_dir.exists():
        return []
    
    if pattern:
        # Allow glob pattern
        if '*' in pattern:
            return sorted(debug_dir.glob(pattern))
        else:
            # Exact file
            p = debug_dir / pattern
            return [p] if p.exists() else []
    
    # Default: all server_*.png files
    return sorted(debug_dir.glob("server_*.png"))


def analyze_image(img: Image.Image) -> dict:
    """Analyze image characteristics."""
    gray = img.convert('L')
    stat = ImageStat.Stat(gray)
    
    # Get pixel data
    px = list(gray.getdata())
    
    result = {
        'width': img.width,
        'height': img.height,
        'mean_brightness': stat.mean[0],
        'stddev': stat.stddev[0] if stat.stddev else 0,
        'min': min(px),
        'max': max(px),
        'is_dark_theme': stat.mean[0] < 100,  # Dark if mean < 100
    }
    
    # Count very dark and very bright pixels
    dark_count = sum(1 for p in px if p < 30)
    bright_count = sum(1 for p in px if p > 220)
    result['dark_ratio'] = dark_count / len(px)
    result['bright_ratio'] = bright_count / len(px)
    
    return result


def preprocess_v1_simple(img: Image.Image) -> Image.Image:
    """Simple preprocessing: scale 3x + autocontrast."""
    w, h = img.size
    scaled = img.resize((w * 3, h * 3), Image.LANCZOS)
    gray = scaled.convert('L')
    return ImageOps.autocontrast(gray, cutoff=2)


def preprocess_v2_invert(img: Image.Image) -> Image.Image:
    """Invert for dark theme: scale 3x + invert + autocontrast."""
    w, h = img.size
    scaled = img.resize((w * 3, h * 3), Image.LANCZOS)
    gray = scaled.convert('L')
    inverted = ImageOps.invert(gray)
    return ImageOps.autocontrast(inverted, cutoff=2)


def preprocess_v3_threshold(img: Image.Image, threshold: int = 128) -> Image.Image:
    """Binary threshold for clean edges."""
    w, h = img.size
    scaled = img.resize((w * 3, h * 3), Image.LANCZOS)
    gray = scaled.convert('L')
    # Invert if dark theme
    stat = ImageStat.Stat(gray)
    if stat.mean[0] < 100:
        gray = ImageOps.invert(gray)
    # Apply threshold
    return gray.point(lambda x: 255 if x > threshold else 0)


def preprocess_v4_adaptive(img: Image.Image) -> Image.Image:
    """Adaptive approach: detect dark theme, scale, enhance contrast."""
    w, h = img.size
    # Scale 4x for better detail
    scaled = img.resize((w * 4, h * 4), Image.LANCZOS)
    gray = scaled.convert('L')
    
    stat = ImageStat.Stat(gray)
    is_dark = stat.mean[0] < 100
    
    if is_dark:
        # Invert first
        gray = ImageOps.invert(gray)
    
    # Apply autocontrast
    gray = ImageOps.autocontrast(gray, cutoff=1)
    
    # Sharpen edges
    gray = gray.filter(ImageFilter.SHARPEN)
    
    # Add white padding for better edge detection
    padded = ImageOps.expand(gray, border=15, fill=255)
    
    return padded


def preprocess_v5_discord_optimized(img: Image.Image) -> Image.Image:
    """
    Discord-specific preprocessing:
    - Discord tooltips are typically white text on dark gray (#2C2F33 or similar)
    - Font is typically Whitney Medium, ~13-14px
    - Scale to get ~300 DPI equivalent
    """
    w, h = img.size
    
    # Scale 4x with high-quality resampling
    scaled = img.resize((w * 4, h * 4), Image.LANCZOS)
    
    # Convert to grayscale
    gray = scaled.convert('L')
    
    # Check if dark theme (Discord tooltip background is ~47,49,54 = mean ~50)
    stat = ImageStat.Stat(gray)
    mean_val = stat.mean[0]
    
    if mean_val < 120:  # Dark theme
        # Invert: white text becomes black on white
        gray = ImageOps.invert(gray)
        
        # Now we have dark text on light background
        # Apply aggressive autocontrast to maximize text/bg separation
        gray = ImageOps.autocontrast(gray, cutoff=0)
        
        # Slight threshold to clean up anti-aliasing
        # Keep mid-grays as they help Tesseract with letter shapes
        gray = gray.point(lambda x: 0 if x < 80 else (255 if x > 200 else x))
    else:
        # Light theme: just autocontrast
        gray = ImageOps.autocontrast(gray, cutoff=2)
    
    # Add generous white padding (Tesseract needs margin around text)
    padded = ImageOps.expand(gray, border=20, fill=255)
    
    return padded


def preprocess_v6_high_contrast(img: Image.Image) -> Image.Image:
    """High contrast binary threshold after inversion."""
    w, h = img.size
    scaled = img.resize((w * 4, h * 4), Image.LANCZOS)
    gray = scaled.convert('L')
    
    stat = ImageStat.Stat(gray)
    if stat.mean[0] < 120:
        gray = ImageOps.invert(gray)
    
    # Hard binary threshold
    binary = gray.point(lambda x: 255 if x > 140 else 0)
    
    # Dilate slightly to connect broken characters (simulate with blur + threshold)
    # binary = binary.filter(ImageFilter.MinFilter(3))  # This would erode
    
    padded = ImageOps.expand(binary, border=20, fill=255)
    return padded


def preprocess_v7_preserve_gray(img: Image.Image) -> Image.Image:
    """
    Preserve grayscale anti-aliasing - Tesseract 4+ handles this well.
    Key insight: Over-processing (thresholding) can hurt more than help.
    """
    w, h = img.size
    
    # Scale 3x - enough for ~300 DPI without over-interpolation
    scaled = img.resize((w * 3, h * 3), Image.LANCZOS)
    gray = scaled.convert('L')
    
    stat = ImageStat.Stat(gray)
    if stat.mean[0] < 120:
        gray = ImageOps.invert(gray)
    
    # Just autocontrast - let Tesseract handle the anti-aliased edges
    gray = ImageOps.autocontrast(gray, cutoff=1)
    
    # White padding
    padded = ImageOps.expand(gray, border=15, fill=255)
    
    return padded


def preprocess_v8_crop_tooltip(img: Image.Image) -> Image.Image:
    """
    Crop to just the tooltip area (white text region) before processing.
    Discord tooltips have a specific dark background - crop to content.
    """
    w, h = img.size
    
    # First, find the tooltip region by looking for the text area
    gray = img.convert('L')
    px = list(gray.getdata())
    
    # Find rows and cols with bright pixels (text)
    bright_thresh = 150
    
    # For each row, check if it has text
    row_has_text = []
    for y in range(h):
        row = [px[y * w + x] for x in range(w)]
        has_text = any(p > bright_thresh for p in row)
        row_has_text.append(has_text)
    
    # Find text bounding box (vertical)
    first_row = next((i for i, has in enumerate(row_has_text) if has), 0)
    last_row = next((h - 1 - i for i, has in enumerate(reversed(row_has_text)) if has), h - 1)
    
    # Similarly for columns
    col_has_text = []
    for x in range(w):
        col = [px[y * w + x] for y in range(h)]
        has_text = any(p > bright_thresh for p in col)
        col_has_text.append(has_text)
    
    first_col = next((i for i, has in enumerate(col_has_text) if has), 0)
    last_col = next((w - 1 - i for i, has in enumerate(reversed(col_has_text)) if has), w - 1)
    
    # Add small margin and crop
    margin = 5
    crop_box = (
        max(0, first_col - margin),
        max(0, first_row - margin),
        min(w, last_col + margin + 1),
        min(h, last_row + margin + 1)
    )
    
    cropped = img.crop(crop_box)
    
    # Now apply standard preprocessing
    return preprocess_v7_preserve_gray(cropped)


# OCR configurations to test
OCR_CONFIGS = {
    'psm7_default': '--psm 7 --oem 3',
    'psm7_whitelist': '--psm 7 --oem 3 -c tessedit_char_whitelist="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 -_#@&.:\'"',
    'psm6_block': '--psm 6 --oem 3',
    'psm8_word': '--psm 8 --oem 3',
    'psm13_raw': '--psm 13 --oem 3',
    'psm7_lstm': '--psm 7 --oem 1',  # LSTM only
    'psm7_legacy': '--psm 7 --oem 0',  # Legacy only (if available)
}

# Preprocessing functions to test
PREPROCESSORS = {
    'v1_simple': preprocess_v1_simple,
    'v2_invert': preprocess_v2_invert,
    'v3_threshold': preprocess_v3_threshold,
    'v4_adaptive': preprocess_v4_adaptive,
    'v5_discord': preprocess_v5_discord_optimized,
    'v6_high_contrast': preprocess_v6_high_contrast,
    'v7_preserve_gray': preprocess_v7_preserve_gray,
    'v8_crop': preprocess_v8_crop_tooltip,
}


def ocr_with_config(img: Image.Image, config: str) -> tuple[str, float]:
    """Run OCR and return (text, confidence)."""
    try:
        # Get data with confidence
        data = pytesseract.image_to_data(img, config=config, output_type=pytesseract.Output.DICT)
        
        texts = data.get('text', [])
        confs = data.get('conf', [])
        
        # Combine text and compute average confidence
        result_parts = []
        conf_vals = []
        
        for t, c in zip(texts, confs):
            if t and t.strip():
                result_parts.append(t.strip())
                try:
                    c_val = float(c)
                    if c_val >= 0:  # -1 means no confidence
                        conf_vals.append(c_val)
                except:
                    pass
        
        text = ' '.join(result_parts)
        avg_conf = sum(conf_vals) / len(conf_vals) if conf_vals else 0.0
        
        return text, avg_conf
    except Exception as e:
        return f"ERROR: {e}", 0.0


def test_image(img_path: Path, save_debug: bool = False) -> dict:
    """Test all preprocessing + config combinations on an image."""
    img = Image.open(img_path)
    analysis = analyze_image(img)
    
    results = {
        'path': str(img_path.name),
        'analysis': analysis,
        'results': []
    }
    
    best_score = 0
    best_result = None
    
    for prep_name, prep_func in PREPROCESSORS.items():
        try:
            processed = prep_func(img)
        except Exception as e:
            results['results'].append({
                'preprocessor': prep_name,
                'config': 'N/A',
                'error': str(e)
            })
            continue
        
        # Save processed image for debugging
        if save_debug:
            debug_path = img_path.parent / f"{img_path.stem}_{prep_name}.png"
            processed.save(debug_path)
        
        for cfg_name, cfg in OCR_CONFIGS.items():
            text, conf = ocr_with_config(processed, cfg)
            
            result = {
                'preprocessor': prep_name,
                'config': cfg_name,
                'text': text,
                'confidence': conf,
            }
            results['results'].append(result)
            
            # Track best result (highest confidence with actual text)
            score = conf if text and not text.startswith('ERROR') and len(text) > 2 else 0
            if score > best_score:
                best_score = score
                best_result = result
    
    results['best'] = best_result
    return results


def format_results(results: dict) -> str:
    """Format results for display."""
    lines = []
    lines.append(f"\n{'='*60}")
    lines.append(f"Image: {results['path']}")
    lines.append(f"Size: {results['analysis']['width']}x{results['analysis']['height']}")
    lines.append(f"Theme: {'Dark' if results['analysis']['is_dark_theme'] else 'Light'} (mean={results['analysis']['mean_brightness']:.1f})")
    lines.append(f"{'='*60}")
    
    # Group by preprocessor for cleaner output
    by_prep = {}
    for r in results['results']:
        prep = r['preprocessor']
        if prep not in by_prep:
            by_prep[prep] = []
        by_prep[prep].append(r)
    
    for prep, prep_results in by_prep.items():
        # Find best config for this preprocessor
        best = max(prep_results, key=lambda x: x.get('confidence', 0))
        if 'error' in best:
            lines.append(f"  {prep}: ERROR - {best['error']}")
        else:
            text = best['text'][:50] + '...' if len(best.get('text', '')) > 50 else best.get('text', '')
            lines.append(f"  {prep} [{best['config']}]: \"{text}\" (conf={best['confidence']:.1f})")
    
    if results.get('best'):
        lines.append(f"\n{'*'*60}")
        lines.append(f"BEST: {results['best']['preprocessor']} + {results['best']['config']}")
        lines.append(f"  Text: \"{results['best']['text']}\"")
        lines.append(f"  Confidence: {results['best']['confidence']:.1f}")
        lines.append(f"{'*'*60}")
    
    return '\n'.join(lines)


def capture_tooltip_live() -> Image.Image | None:
    """Capture a live tooltip from Discord."""
    try:
        from PIL import ImageGrab
        import pyautogui
    except ImportError:
        print("Need pyautogui for live capture: pip install pyautogui")
        return None
    
    print("Move your mouse to a Discord server icon and wait 3 seconds...")
    time.sleep(3)
    
    # Get mouse position
    x, y = pyautogui.position()
    
    # Capture region around tooltip (right of icon)
    # Discord tooltips appear to the right, about 40-280px wide
    box = (x + 45, y - 25, x + 320, y + 25)
    
    try:
        img = ImageGrab.grab(bbox=box)
        return img
    except Exception as e:
        print(f"Capture failed: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description='Test OCR tuning on Discord tooltip images')
    parser.add_argument('--image', '-i', help='Specific image or glob pattern to test')
    parser.add_argument('--capture', '-c', action='store_true', help='Capture a live tooltip')
    parser.add_argument('--save-debug', '-s', action='store_true', help='Save preprocessed images')
    parser.add_argument('--verbose', '-v', action='store_true', help='Show all results, not just best')
    parser.add_argument('--compare', action='store_true', help='Compare with src.utils.ocr_image_to_text')
    args = parser.parse_args()
    
    if args.capture:
        img = capture_tooltip_live()
        if img:
            # Save and test
            save_path = Path(__file__).parent.parent / "data" / "debug" / f"capture_{int(time.time())}.png"
            save_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(save_path)
            print(f"Saved to: {save_path}")
            
            results = test_image(save_path, save_debug=args.save_debug)
            print(format_results(results))
        return
    
    images = get_debug_images(args.image)
    
    if not images:
        print("No images found. Use --capture to capture a tooltip, or check data/debug/ directory.")
        return
    
    print(f"Testing {len(images)} images...")
    
    # Track overall best strategies
    strategy_scores = {}
    
    # If --compare, also test with src.utils.ocr_image_to_text
    utils_ocr = None
    if args.compare:
        try:
            from src import utils as src_utils
            utils_ocr = src_utils.ocr_image_to_text
            print("Comparing with src.utils.ocr_image_to_text\n")
        except ImportError as e:
            print(f"Could not import src.utils: {e}")
    
    for img_path in images:
        results = test_image(img_path, save_debug=args.save_debug)
        print(format_results(results))
        
        # Test with utils.ocr_image_to_text if comparing
        if utils_ocr:
            try:
                img = Image.open(img_path)
                utils_result = utils_ocr(img)
                print(f"  --> src.utils.ocr_image_to_text: \"{utils_result}\"")
            except Exception as e:
                print(f"  --> src.utils.ocr_image_to_text ERROR: {e}")
        
        # Aggregate scores per strategy
        for r in results['results']:
            if 'error' not in r:
                key = (r['preprocessor'], r['config'])
                if key not in strategy_scores:
                    strategy_scores[key] = []
                # Score: confidence * text_length_bonus
                text_len = len(r.get('text', ''))
                score = r['confidence'] * (1 + min(text_len / 20, 1))  # Bonus for longer text
                strategy_scores[key].append(score)
    
    # Summary
    if len(images) > 1:
        print(f"\n{'='*60}")
        print("OVERALL BEST STRATEGIES (by average score)")
        print(f"{'='*60}")
        
        avg_scores = {k: sum(v)/len(v) for k, v in strategy_scores.items() if v}
        sorted_strategies = sorted(avg_scores.items(), key=lambda x: -x[1])
        
        for (prep, cfg), score in sorted_strategies[:10]:
            print(f"  {prep} + {cfg}: avg_score={score:.1f}")


if __name__ == '__main__':
    main()
