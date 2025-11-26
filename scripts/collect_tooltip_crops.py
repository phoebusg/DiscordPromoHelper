#!/usr/bin/env python3
"""Collect real Discord tooltip crops for inspection.

This script runs a best-effort scan (using iterate_all_servers) with
debug image saving enabled, then copies the captured hover/server images
from `data/debug/` into `data/tooltip_samples/` for manual review.

Run locally when Discord is visible and you have screen-capture permission.
"""
import argparse
import shutil
import os
from pathlib import Path
import glob


def main():
    parser = argparse.ArgumentParser(description='Collect tooltip crop images from a Discord scan')
    parser.add_argument('--outdir', '-o', default='data/tooltip_samples', help='Directory to store collected crops')
    parser.add_argument('--count', '-n', type=int, default=10, help='Maximum number of crops to collect')
    parser.add_argument('--max-servers', type=int, default=60, help='Limit how many servers to iterate (controls runtime)')
    parser.add_argument('--hover-delay', type=float, default=0.4, help='Hover delay used during scanning')
    parser.add_argument('--debug-save', action='store_true', help='Save debug crops during scan (recommended)')
    args = parser.parse_args()

    # Local import to avoid side effects during test collection
    try:
        from src.discord_nav import iterate_all_servers
    except Exception as e:
        print('error: unable to import iterate_all_servers:', e)
        return 2

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)

    dbg = Path('data') / 'debug'
    dbg.mkdir(parents=True, exist_ok=True)

    print('Starting scan (this will move your mouse / hover over Discord server icons).')
    print('Make sure Discord is visible and focused; allow screen recording for Terminal if required.')

    try:
        servers = iterate_all_servers(hover_delay=args.hover_delay, debug_save=args.debug_save, max_servers=args.max_servers)
    except Exception as e:
        print('iterate_all_servers failed:', e)
        # Fall through to harvest whatever debug images exist already

    # Collect matching debug images
    img_patterns = ['hover_*.png', 'server_*.png', 'col_*.png']
    collected = 0
    files = []
    for pat in img_patterns:
        files.extend(sorted(glob.glob(str(dbg / pat))))

    if not files:
        print('No debug crop images found in', dbg)
        print('If you ran this script with --debug-save, ensure Discord was visible and the scan completed.')
        return 0

    # Copy up to args.count files into outdir with stable names
    for f in files[: args.count]:
        try:
            src = Path(f)
            dst = out / f'{collected:03d}_{src.name}'
            shutil.copy2(src, dst)
            collected += 1
        except Exception:
            continue

    print(f'Collected {collected} tooltip crops into {out.resolve()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
