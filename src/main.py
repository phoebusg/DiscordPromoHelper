#!/usr/bin/env python3
"""Discord Promo Helper - Main Entry Point.

Usage:
    python -m src.main          # Launch GUI (default)
    python -m src.main --ui     # Launch GUI explicitly
    python -m src.main --cli    # Run in CLI mode (legacy)
    python -m src.main --scan   # Run server scan only
    python -m src.main --help   # Show help
"""
from __future__ import annotations
import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        description="Discord Promo Helper - Respectful self-promotion automation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.main              Launch the GUI
  python -m src.main --cli        Run in legacy CLI mode
  python -m src.main --scan       Scan Discord servers only
  python -m src.main --post "Check out my stream!" "https://twitch.tv/example"
        """
    )
    
    parser.add_argument("--ui", action="store_true", help="Launch GUI (default)")
    parser.add_argument("--cli", action="store_true", help="Run in CLI/legacy mode")
    parser.add_argument("--scan", action="store_true", help="Scan Discord servers only")
    parser.add_argument("--post", nargs=2, metavar=("TITLE", "LINK"), 
                       help="Post promotion with given title and link")
    parser.add_argument("--game", type=str, help="Filter servers by game tag")
    parser.add_argument("--dry-run", action="store_true", 
                       help="Show what would be done without actually posting")
    
    args = parser.parse_args()
    
    # Determine mode
    if args.scan:
        run_scan()
    elif args.cli or args.post:
        run_cli(args)
    else:
        # Default: launch GUI
        run_ui()


def run_ui():
    """Launch the Tkinter GUI."""
    print("Launching Discord Promo Helper UI...")
    try:
        from .ui import run_ui as start_ui
        start_ui()
    except ImportError:
        from ui import run_ui as start_ui
        start_ui()


def run_scan():
    """Run server scanning."""
    print("Starting Discord server scan...")
    print("Make sure Discord is open and visible.\n")
    
    try:
        from .discord_nav import iterate_all_servers
    except ImportError:
        from discord_nav import iterate_all_servers
    
    try:
        servers = iterate_all_servers()
        print(f"\n✓ Scan complete! Found {len(servers)} servers.")
        print("Results saved to servers.json")
    except KeyboardInterrupt:
        print("\n\nScan interrupted by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Scan failed: {e}")
        sys.exit(1)


def run_cli(args):
    """Run in CLI mode (legacy behavior or with --post)."""
    try:
        from .server_config import load_config, get_servers_by_game, get_enabled_servers
        from .promotion_helper import post_update, queue_updates
    except ImportError:
        from server_config import load_config, get_servers_by_game, get_enabled_servers
        from promotion_helper import post_update, queue_updates
    
    config = load_config()
    
    if args.post:
        title, link = args.post
        
        # Get target servers
        if args.game:
            servers = get_servers_by_game(args.game, config)
            print(f"Posting to servers tagged with '{args.game}'...")
        else:
            servers = get_enabled_servers(config)
            print(f"Posting to all enabled servers...")
        
        if not servers:
            print("No servers found matching criteria.")
            sys.exit(1)
        
        print(f"Target servers: {len(servers)}")
        
        if args.dry_run:
            print("\n[DRY RUN] Would post to:")
            for s in servers:
                name = s.get("friendly_name") or s.get("ocr_name", "Unknown")
                channels = s.get("promo_channels", [])
                print(f"  • {name}: {channels or '(no channels configured)'}")
            print(f"\nMessage: {title}")
            print(f"Link: {link}")
        else:
            # Note: Full posting logic would go here
            # For now, use legacy queue_updates
            print("\nStarting promotion queue...")
            queue_updates(title, link)
    else:
        # Legacy mode without arguments
        print("CLI mode - no action specified.")
        print("Use --post TITLE LINK to post, or --help for options.")


if __name__ == "__main__":
    main()


