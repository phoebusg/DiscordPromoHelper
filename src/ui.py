"""Minimal Tkinter UI for Discord Promo Helper.

Features:
- Server list with OCR names and editable friendly names
- Per-server promo channel configuration
- Game tag filtering
- Global settings (rate limit, etc.)
"""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from typing import Optional, List, Dict, Any
import json
from pathlib import Path

# Import our modules
try:
    from .server_config import (
        load_config, save_config, get_server_config, set_server_config,
        get_display_name, import_from_servers_json, get_rate_limit_hours,
        set_rate_limit_hours, get_enabled_servers, get_servers_by_game
    )
except ImportError:
    from server_config import (
        load_config, save_config, get_server_config, set_server_config,
        get_display_name, import_from_servers_json, get_rate_limit_hours,
        set_rate_limit_hours, get_enabled_servers, get_servers_by_game
    )


class ServerSettingsDialog(tk.Toplevel):
    """Dialog for editing a single server's settings."""
    
    def __init__(self, parent, ocr_name: str, config: Dict[str, Any]):
        super().__init__(parent)
        self.title(f"Edit Server: {ocr_name[:30]}...")
        self.geometry("500x450")
        self.resizable(True, True)
        
        self.ocr_name = ocr_name
        self.config = config
        self.result = None
        
        # Get current server config
        self.server_cfg = get_server_config(ocr_name, config)
        
        self._create_widgets()
        
        # Modal behavior
        self.transient(parent)
        self.grab_set()
        
    def _create_widgets(self):
        # Main frame with padding
        main = ttk.Frame(self, padding="10")
        main.pack(fill=tk.BOTH, expand=True)
        
        # OCR Name (read-only)
        ttk.Label(main, text="OCR Name (detected):").pack(anchor=tk.W)
        ocr_entry = ttk.Entry(main, width=60)
        ocr_entry.insert(0, self.server_cfg.get("ocr_name", self.ocr_name))
        ocr_entry.configure(state="readonly")
        ocr_entry.pack(fill=tk.X, pady=(0, 10))
        
        # Friendly Name
        ttk.Label(main, text="Friendly Name (your label):").pack(anchor=tk.W)
        self.friendly_var = tk.StringVar(value=self.server_cfg.get("friendly_name", ""))
        ttk.Entry(main, textvariable=self.friendly_var, width=60).pack(fill=tk.X, pady=(0, 10))
        
        # Enabled checkbox
        self.enabled_var = tk.BooleanVar(value=self.server_cfg.get("enabled", True))
        ttk.Checkbutton(main, text="Enabled (include in promotions)", 
                       variable=self.enabled_var).pack(anchor=tk.W, pady=(0, 10))
        
        # Promo Channels
        ttk.Label(main, text="Promo Channels (one per line):").pack(anchor=tk.W)
        self.channels_text = tk.Text(main, height=4, width=60)
        channels = self.server_cfg.get("promo_channels", [])
        self.channels_text.insert("1.0", "\n".join(channels))
        self.channels_text.pack(fill=tk.X, pady=(0, 10))
        
        # Game Tags
        ttk.Label(main, text="Game Tags (comma-separated):").pack(anchor=tk.W)
        tags = self.server_cfg.get("game_tags", [])
        self.tags_var = tk.StringVar(value=", ".join(tags))
        ttk.Entry(main, textvariable=self.tags_var, width=60).pack(fill=tk.X, pady=(0, 10))
        
        # Notes
        ttk.Label(main, text="Notes:").pack(anchor=tk.W)
        self.notes_text = tk.Text(main, height=3, width=60)
        self.notes_text.insert("1.0", self.server_cfg.get("notes", ""))
        self.notes_text.pack(fill=tk.X, pady=(0, 10))
        
        # Buttons
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=(10, 0))
        
        ttk.Button(btn_frame, text="Save", command=self._save).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
    
    def _save(self):
        # Parse channels (one per line, filter empty)
        channels_raw = self.channels_text.get("1.0", tk.END).strip()
        channels = [c.strip() for c in channels_raw.split("\n") if c.strip()]
        
        # Parse tags (comma-separated)
        tags_raw = self.tags_var.get().strip()
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        
        # Update config
        self.result = set_server_config(
            ocr_name=self.ocr_name,
            friendly_name=self.friendly_var.get().strip(),
            promo_channels=channels,
            game_tags=tags,
            enabled=self.enabled_var.get(),
            notes=self.notes_text.get("1.0", tk.END).strip(),
            config=self.config,
            save=True
        )
        
        self.destroy()


class GameFilterDialog(tk.Toplevel):
    """Dialog for editing game-based filters."""
    
    def __init__(self, parent, config: Dict[str, Any]):
        super().__init__(parent)
        self.title("Game Filter Settings")
        self.geometry("500x400")
        
        self.config = config
        self._create_widgets()
        
        self.transient(parent)
        self.grab_set()
    
    def _create_widgets(self):
        main = ttk.Frame(self, padding="10")
        main.pack(fill=tk.BOTH, expand=True)
        
        ttk.Label(main, text="Game Filters", font=("", 12, "bold")).pack(anchor=tk.W)
        ttk.Label(main, text="Define which servers to use for specific games.\n"
                           "When streaming a game, only servers with matching tags will be used.",
                 wraplength=450).pack(anchor=tk.W, pady=(0, 10))
        
        # Game filters display
        filters_frame = ttk.LabelFrame(main, text="Current Filters", padding="5")
        filters_frame.pack(fill=tk.BOTH, expand=True)
        
        # Treeview for filters
        columns = ("game", "server_count")
        self.tree = ttk.Treeview(filters_frame, columns=columns, show="headings", height=8)
        self.tree.heading("game", text="Game Tag")
        self.tree.heading("server_count", text="# Servers")
        self.tree.column("game", width=200)
        self.tree.column("server_count", width=100)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        scroll = ttk.Scrollbar(filters_frame, orient=tk.VERTICAL, command=self.tree.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=scroll.set)
        
        # Populate tree
        self._refresh_tree()
        
        # Info label
        ttk.Label(main, text="Game tags are set per-server in the server settings.\n"
                           "Servers with matching game tags will be shown here.",
                 wraplength=450, foreground="gray").pack(pady=10)
        
        ttk.Button(main, text="Close", command=self.destroy).pack(side=tk.RIGHT)
    
    def _refresh_tree(self):
        # Clear existing
        for item in self.tree.get_children():
            self.tree.delete(item)
        
        # Collect all game tags and count servers
        tag_counts: Dict[str, int] = {}
        for server in self.config.get("servers", {}).values():
            for tag in server.get("game_tags", []):
                tag_lower = tag.lower()
                tag_counts[tag_lower] = tag_counts.get(tag_lower, 0) + 1
        
        # Insert into tree
        for tag, count in sorted(tag_counts.items()):
            self.tree.insert("", tk.END, values=(tag, count))


class DiscordPromoApp(tk.Tk):
    """Main application window."""
    
    def __init__(self):
        super().__init__()
        self.title("Discord Promo Helper")
        self.geometry("800x600")
        self.minsize(600, 400)
        
        # Load config
        self.config = load_config()
        
        # Load servers from servers.json if it exists
        self._import_servers()
        
        self._create_menu()
        self._create_widgets()
        self._refresh_server_list()
    
    def _import_servers(self):
        """Import servers from scan results if available."""
        servers_path = Path("servers.json")
        if servers_path.exists():
            imported = import_from_servers_json()
            if imported > 0:
                self.config = load_config()  # Reload after import
    
    def _create_menu(self):
        menubar = tk.Menu(self)
        
        # File menu
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Rescan Servers", command=self._rescan_servers)
        file_menu.add_command(label="Import from servers.json", command=self._manual_import)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.quit)
        menubar.add_cascade(label="File", menu=file_menu)
        
        # Settings menu
        settings_menu = tk.Menu(menubar, tearoff=0)
        settings_menu.add_command(label="Rate Limit...", command=self._edit_rate_limit)
        settings_menu.add_command(label="Game Filters...", command=self._edit_game_filters)
        menubar.add_cascade(label="Settings", menu=settings_menu)
        
        # Help menu
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        
        self.configure(menu=menubar)
    
    def _create_widgets(self):
        # Main paned window
        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Left panel - Server list
        left_frame = ttk.Frame(paned)
        paned.add(left_frame, weight=1)
        
        # Server list header
        header = ttk.Frame(left_frame)
        header.pack(fill=tk.X)
        ttk.Label(header, text="Servers", font=("", 12, "bold")).pack(side=tk.LEFT)
        self.server_count_label = ttk.Label(header, text="(0)")
        self.server_count_label.pack(side=tk.LEFT, padx=5)
        
        # Filter frame
        filter_frame = ttk.Frame(left_frame)
        filter_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(filter_frame, text="Filter:").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar()
        self.filter_var.trace("w", lambda *args: self._refresh_server_list())
        filter_entry = ttk.Entry(filter_frame, textvariable=self.filter_var, width=20)
        filter_entry.pack(side=tk.LEFT, padx=5)
        
        self.show_enabled_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(filter_frame, text="Enabled only", 
                       variable=self.show_enabled_only,
                       command=self._refresh_server_list).pack(side=tk.LEFT)
        
        # Server treeview
        tree_frame = ttk.Frame(left_frame)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        
        columns = ("display_name", "channels", "tags", "enabled")
        self.server_tree = ttk.Treeview(tree_frame, columns=columns, show="headings")
        self.server_tree.heading("display_name", text="Server Name")
        self.server_tree.heading("channels", text="Promo Channels")
        self.server_tree.heading("tags", text="Game Tags")
        self.server_tree.heading("enabled", text="✓")
        
        self.server_tree.column("display_name", width=200)
        self.server_tree.column("channels", width=120)
        self.server_tree.column("tags", width=100)
        self.server_tree.column("enabled", width=30)
        
        self.server_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.server_tree.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.server_tree.configure(yscrollcommand=scroll.set)
        
        # Double-click to edit
        self.server_tree.bind("<Double-1>", self._on_server_double_click)
        
        # Right panel - Actions and info
        right_frame = ttk.Frame(paned)
        paned.add(right_frame, weight=0)
        
        # Action buttons
        actions = ttk.LabelFrame(right_frame, text="Actions", padding="10")
        actions.pack(fill=tk.X, pady=5)
        
        ttk.Button(actions, text="Edit Selected", command=self._edit_selected).pack(fill=tk.X, pady=2)
        ttk.Button(actions, text="Quick Enable/Disable", command=self._toggle_enabled).pack(fill=tk.X, pady=2)
        ttk.Button(actions, text="Set Friendly Name...", command=self._quick_set_name).pack(fill=tk.X, pady=2)
        
        # Status/info panel
        info_frame = ttk.LabelFrame(right_frame, text="Quick Info", padding="10")
        info_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        self.info_text = tk.Text(info_frame, width=30, height=15, wrap=tk.WORD)
        self.info_text.pack(fill=tk.BOTH, expand=True)
        self.info_text.configure(state="disabled")
        
        self.server_tree.bind("<<TreeviewSelect>>", self._on_server_select)
        
        # Bottom status bar
        status_frame = ttk.Frame(self)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM)
        
        rate_limit = get_rate_limit_hours(self.config)
        self.status_var = tk.StringVar(value=f"Rate limit: {rate_limit}h per channel")
        ttk.Label(status_frame, textvariable=self.status_var).pack(side=tk.LEFT, padx=5)
    
    def _refresh_server_list(self):
        """Refresh the server treeview from config."""
        # Clear existing
        for item in self.server_tree.get_children():
            self.server_tree.delete(item)
        
        filter_text = self.filter_var.get().lower()
        enabled_only = self.show_enabled_only.get()
        
        servers = self.config.get("servers", {})
        count = 0
        
        for key, server in sorted(servers.items()):
            ocr_name = server.get("ocr_name", key)
            friendly_name = server.get("friendly_name", "")
            display = friendly_name if friendly_name else ocr_name
            enabled = server.get("enabled", True)
            channels = server.get("promo_channels", [])
            tags = server.get("game_tags", [])
            
            # Apply filters
            if filter_text:
                searchable = f"{ocr_name} {friendly_name}".lower()
                if filter_text not in searchable:
                    continue
            
            if enabled_only and not enabled:
                continue
            
            # Insert row
            self.server_tree.insert("", tk.END, iid=key, values=(
                display,
                len(channels),
                ", ".join(tags[:2]) + ("..." if len(tags) > 2 else ""),
                "✓" if enabled else ""
            ))
            count += 1
        
        self.server_count_label.configure(text=f"({count})")
    
    def _get_selected_key(self) -> Optional[str]:
        """Get the key of the currently selected server."""
        selection = self.server_tree.selection()
        if not selection:
            return None
        return selection[0]
    
    def _on_server_select(self, event):
        """Update info panel when selection changes."""
        key = self._get_selected_key()
        if not key:
            return
        
        server = self.config.get("servers", {}).get(key, {})
        
        info = []
        info.append(f"OCR Name:\n{server.get('ocr_name', 'N/A')}\n")
        info.append(f"Friendly Name:\n{server.get('friendly_name', '(not set)')}\n")
        info.append(f"Enabled: {'Yes' if server.get('enabled', True) else 'No'}\n")
        
        channels = server.get("promo_channels", [])
        info.append(f"Promo Channels ({len(channels)}):")
        for ch in channels:
            info.append(f"  • {ch}")
        info.append("")
        
        tags = server.get("game_tags", [])
        info.append(f"Game Tags: {', '.join(tags) if tags else '(none)'}\n")
        
        notes = server.get("notes", "")
        if notes:
            info.append(f"Notes:\n{notes}")
        
        self.info_text.configure(state="normal")
        self.info_text.delete("1.0", tk.END)
        self.info_text.insert("1.0", "\n".join(info))
        self.info_text.configure(state="disabled")
    
    def _on_server_double_click(self, event):
        """Open edit dialog on double-click."""
        self._edit_selected()
    
    def _edit_selected(self):
        """Open edit dialog for selected server."""
        key = self._get_selected_key()
        if not key:
            messagebox.showinfo("Select Server", "Please select a server first.")
            return
        
        server = self.config.get("servers", {}).get(key, {})
        ocr_name = server.get("ocr_name", key)
        
        dialog = ServerSettingsDialog(self, ocr_name, self.config)
        self.wait_window(dialog)
        
        if dialog.result:
            self.config = load_config()  # Reload
            self._refresh_server_list()
            self._on_server_select(None)  # Update info panel
    
    def _toggle_enabled(self):
        """Toggle enabled status for selected server."""
        key = self._get_selected_key()
        if not key:
            messagebox.showinfo("Select Server", "Please select a server first.")
            return
        
        server = self.config.get("servers", {}).get(key, {})
        current = server.get("enabled", True)
        
        set_server_config(
            ocr_name=server.get("ocr_name", key),
            enabled=not current,
            config=self.config
        )
        self.config = load_config()
        self._refresh_server_list()
    
    def _quick_set_name(self):
        """Quick dialog to set friendly name."""
        key = self._get_selected_key()
        if not key:
            messagebox.showinfo("Select Server", "Please select a server first.")
            return
        
        server = self.config.get("servers", {}).get(key, {})
        ocr_name = server.get("ocr_name", key)
        current = server.get("friendly_name", "")
        
        name = simpledialog.askstring(
            "Set Friendly Name",
            f"Enter a friendly name for:\n{ocr_name}",
            initialvalue=current
        )
        
        if name is not None:  # User didn't cancel
            set_server_config(
                ocr_name=ocr_name,
                friendly_name=name,
                config=self.config
            )
            self.config = load_config()
            self._refresh_server_list()
    
    def _edit_rate_limit(self):
        """Edit global rate limit setting."""
        current = get_rate_limit_hours(self.config)
        
        hours = simpledialog.askinteger(
            "Rate Limit",
            "Hours between posts to the same channel:\n(Recommended: 3 hours)",
            initialvalue=current,
            minvalue=1,
            maxvalue=24
        )
        
        if hours:
            set_rate_limit_hours(hours, self.config)
            self.config = load_config()
            self.status_var.set(f"Rate limit: {hours}h per channel")
    
    def _edit_game_filters(self):
        """Show game filters dialog."""
        GameFilterDialog(self, self.config)
    
    def _rescan_servers(self):
        """Trigger a server rescan (will run discord_nav)."""
        result = messagebox.askyesno(
            "Rescan Servers",
            "This will scan Discord for all servers.\n\n"
            "Make sure Discord is open and visible.\n\n"
            "Continue?"
        )
        
        if result:
            messagebox.showinfo(
                "Rescan",
                "Server scanning is not yet integrated.\n\n"
                "Run the scan manually:\n"
                "python -m src.discord_nav"
            )
    
    def _manual_import(self):
        """Manually import from servers.json."""
        imported = import_from_servers_json()
        self.config = load_config()
        self._refresh_server_list()
        
        messagebox.showinfo("Import Complete", f"Imported {imported} new servers.")
    
    def _show_about(self):
        """Show about dialog."""
        messagebox.showinfo(
            "About Discord Promo Helper",
            "Discord Promo Helper\n\n"
            "A tool for respectful self-promotion across Discord servers.\n\n"
            "Features:\n"
            "• Server management with friendly names\n"
            "• Per-server promo channel configuration\n"
            "• Game-based filtering\n"
            "• Rate limiting (default: 3h per channel)\n\n"
            "Always follow server rules!"
        )


def run_ui():
    """Entry point to run the UI."""
    app = DiscordPromoApp()
    app.mainloop()


if __name__ == "__main__":
    run_ui()
