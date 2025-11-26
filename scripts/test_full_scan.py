#!/usr/bin/env python3
"""Test harness to run a full scan using iterate_all_servers and verify servers.json."""
import argparse
import json
from pathlib import Path
import sys

# Add repo path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.utils import check_tesseract_path
from src.discord_nav import iterate_all_servers
from PIL import ImageGrab


def summarize_servers(meta_path: Path):
    if not meta_path.exists():
        return {}
    with open(meta_path, 'r', encoding='utf-8') as fh:
        data = json.load(fh)
    # Handle both list format and dict with 'servers' key
    if isinstance(data, dict):
        servers = data.get('servers', [])
    else:
        servers = data
    total = len(servers)
    with_name = len([s for s in servers if s.get('name')])
    unnamed = total - with_name
    unique_names = len(set(s.get('name') for s in servers if s.get('name')))
    unique_icons = len(set(s.get('icon') for s in servers if s.get('icon')))
    return {
        'total': total,
        'with_name': with_name,
        'unnamed': unnamed,
        'unique_names': unique_names,
        'unique_icons': unique_icons,
    }


def main():
    parser = argparse.ArgumentParser(description='Run a full scan and summarize servers (OCR-driven)')
    parser.add_argument('--save-dir', '-d', default='data/test_scan', help='Directory to write images and servers.json')
    parser.add_argument('--max-scrolls', type=int, default=30, help='Maximum scroll passes to perform')
    parser.add_argument('--require-min', type=int, default=0, help='Fail if fewer than this many servers found')
    parser.add_argument('--start-from-top', action='store_true', help='Start the scan from the top of the server list')
    parser.add_argument('--max-icon-retries', type=int, default=120, help='Maximum hover/OCR attempts per icon before skipping')
    parser.add_argument('--debug-save-hover', action='store_true', help='Save hover tooltip boxes for debugging')
    args = parser.parse_args()

    save_dir = Path(args.save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    print(f"Running full scan; saving to {save_dir}")
    # Preflight checks
    try:
        check_tesseract_path()
    except Exception as e:
        print(f"Tesseract check failed: {e}")
        print("Please ensure tesseract is installed and available on PATH.")
        sys.exit(3)
    # Quick screen recording permission check (macOS users need to grant permission)
    try:
        ImageGrab.grab(bbox=(0, 0, 10, 10))
    except Exception as e:
        print("Screen capture failed — ensure Terminal/Python has macOS Screen Recording permission and Discord is visible.")
        print(f"Error: {e}")
        sys.exit(4)

    # Run the iterator and save results to servers.json in save_dir
    try:
        server_list = iterate_all_servers(
            hover_delay=0.4,
            debug_save=args.debug_save_hover,
            max_servers=args.max_scrolls * 10 if args.max_scrolls else 200
        )
    except Exception as e:
        print('Error running iterate_all_servers:', e)
        server_list = []

    meta_path = save_dir / 'servers.json'
    with open(meta_path, 'w', encoding='utf-8') as fh:
        json.dump({'total_servers': len(server_list), 'servers': server_list}, fh, ensure_ascii=False, indent=2)

    summary = summarize_servers(meta_path)
    print('Scan finished; summary:')
    for k, v in summary.items():
        print(f'  {k}: {v}')

    if args.require_min and summary.get('total', 0) < args.require_min:
        print(f"FAIL: Found {summary.get('total', 0)} servers, expected at least {args.require_min}")
        sys.exit(2)

    print('OK')


if __name__ == '__main__':
    main()
