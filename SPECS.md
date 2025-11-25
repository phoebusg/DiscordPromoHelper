# Technical Specification

> Developer reference for Discord Promo Helper internals.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Discord Promo Helper                        │
├─────────────────────────────────────────────────────────────────────┤
│  UI Layer                                                           │
│  └── src/ui.py           Tkinter GUI                               │
├─────────────────────────────────────────────────────────────────────┤
│  Business Logic                                                      │
│  ├── src/main.py          Entry point                              │
│  ├── src/server_config.py Server settings                          │
│  ├── src/promotion_helper.py  Posting queue                        │
│  ├── src/stream_info.py   Twitch/Kick/YouTube APIs                 │
│  └── src/storage.py       Timestamp persistence                    │
├─────────────────────────────────────────────────────────────────────┤
│  Discord Automation                                                  │
│  ├── src/discord_nav.py   Server scanning                          │
│  └── src/utils.py         OCR & window helpers                     │
├─────────────────────────────────────────────────────────────────────┤
│  Data Files                                                          │
│  ├── servers.json         Scanned servers                          │
│  ├── server_config.json   User settings                            │
│  └── data/*.json          Runtime data                             │
└─────────────────────────────────────────────────────────────────────┘
```

## Core Modules

| Module | Purpose |
|--------|---------|
| `main.py` | CLI entry point (`--ui`, `--scan`, `--post`) |
| `ui.py` | Tkinter GUI for server configuration |
| `discord_nav.py` | Server scanning via OCR + mouse automation |
| `server_config.py` | Load/save user settings (friendly names, tags) |
| `stream_info.py` | Fetch stream info from Twitch/Kick/YouTube |
| `promotion_helper.py` | Posting logic with rate limiting |
| `storage.py` | Per-channel timestamp persistence |
| `utils.py` | Tesseract setup, Discord window management |
| `config.py` | Global constants (keywords, rate limits) |

## Stream Info Module

### Overview

`src/stream_info.py` provides a unified interface to fetch live stream information:

```python
from src.stream_info import get_stream_info, detect_platform

# Auto-detect platform from URL
platform, username = detect_platform("https://twitch.tv/ninja")

# Fetch stream info
info = get_stream_info(platform, username)
print(info.title)    # "Playing Fortnite with Viewers!"
print(info.game)     # "Fortnite"
print(info.is_live)  # True
print(info.viewers)  # 50000
```

### Supported Platforms

| Platform | API Type | Auth Required | Notes |
|----------|----------|---------------|-------|
| Twitch | Official Helix API | Yes (Client ID + Secret) | Most reliable |
| Kick | Unofficial Public API | No | May break if API changes |
| YouTube | Data API v3 | Yes (API Key) | Quota limits apply |

### Environment Variables

```bash
# Twitch (required for Twitch support)
export TWITCH_CLIENT_ID="your_client_id"
export TWITCH_CLIENT_SECRET="your_client_secret"

# YouTube (required for YouTube support)
export YOUTUBE_API_KEY="your_api_key"
```

### StreamInfo Dataclass

```python
@dataclass
class StreamInfo:
    platform: str      # "twitch", "kick", "youtube"
    username: str      # Platform username/channel
    title: str         # Stream title
    game: str          # Game/category name
    is_live: bool      # Currently streaming?
    viewers: int       # Concurrent viewers
    thumbnail_url: str # Stream thumbnail
    stream_url: str    # Direct link to stream
    error: str         # Error message (if any)
```

## Data Files

| File | Purpose |
|------|---------|
| `servers.json` | Scanned server list (OCR names, positions) |
| `server_config.json` | User config (friendly names, channels, tags) |
| `data/last_post_timestamps.json` | Rate limit tracking |

### server_config.json Format

```json
{
  "servers": {
    "normalized key": {
      "ocr_name": "T900fficial Discord",
      "friendly_name": "T90 Official",
      "promo_channels": ["self-promo"],
      "game_tags": ["aoe2"],
      "enabled": true
    }
  },
  "settings": {
    "rate_limit_hours": 3
  }
}
```

## Rate Limiting

- **Default**: 3 hours per channel
- **Storage**: `data/last_post_timestamps.json`
- **Key format**: `{server}:{channel}`
- **Logic**: Check timestamp before posting, update after success

## Server Scanning

### How It Works

1. Focus Discord window
2. Hover each server icon in left sidebar
3. OCR the tooltip that appears
4. Scroll down, repeat until end detected

### End Detection

1. **End markers**: "Add a Server" or "Discover" tooltip
2. **Stale fallback**: 3 consecutive pages with no new servers
3. **Safety limit**: Max 50 pages

### OCR Challenges

- Tooltips appear briefly → multiple retry attempts
- Dark icons may not trigger tooltip → synthetic hover
- OCR produces garbled text → fuzzy deduplication
- Dedup: substring match, 60% word overlap, or 85% char similarity

## Game Filtering

Tag servers by game to filter promotions:

```bash
# Only post to Fortnite servers
python -m src.main --post "Title" "link" --game fortnite
```

**Predefined tags** (in `config.py`):
- FPS: fortnite, valorant, apex, warzone, cs2
- Strategy: aoe2, aoe4
- MMO: wow, ffxiv
- General: variety, just chatting

## Channel Detection

**Keywords** (partial match):
- promo, self-promo, promotion
- advertise, ads
- plug, share, showcase

---

# Future Implementation Plans

> Detailed specifications for future agents to implement.

## 1. One-Button Auto-Promo (HIGH PRIORITY)

### Goal
Single button click to promote to all enabled servers with auto-fetched stream info.

### Implementation Steps

#### Step 1: Add "Auto Promo" button to UI
```python
# In src/ui.py, add to _create_widgets():
promo_frame = ttk.LabelFrame(right_frame, text="Promotion", padding="10")
promo_frame.pack(fill=tk.X, pady=5)

ttk.Button(promo_frame, text="🚀 Auto Promo to All",
           command=self._auto_promo_all).pack(fill=tk.X, pady=2)
ttk.Button(promo_frame, text="📝 Custom Message...",
           command=self._custom_promo).pack(fill=tk.X, pady=2)
```

#### Step 2: Create auto-promo workflow
```python
# Add to src/ui.py:
def _auto_promo_all(self):
    """Auto-promote to all enabled servers."""
    # 1. Check if stream info is loaded
    if not hasattr(self, '_current_stream_info') or not self._current_stream_info:
        result = messagebox.askyesno(
            "No Stream Info",
            "No stream info loaded. Fetch now?"
        )
        if result:
            self._fetch_stream_info()
            if not hasattr(self, '_current_stream_info'):
                return
        else:
            return
    
    info = self._current_stream_info
    
    # 2. Build message from template
    message = f"🔴 LIVE NOW: {info.title}\n"
    message += f"🎮 Playing: {info.game}\n" if info.game else ""
    message += f"👀 {info.viewers} watching\n" if info.viewers else ""
    message += f"🔗 {info.stream_url}"
    
    # 3. Get enabled servers (optionally filtered by game)
    servers = get_enabled_servers(self.config)
    if info.game:
        # Filter to servers tagged with this game
        game_servers = get_servers_by_game(info.game, self.config)
        if game_servers:
            servers = game_servers
    
    # 4. Show confirmation
    result = messagebox.askyesno(
        "Confirm Auto-Promo",
        f"Post to {len(servers)} servers?\n\n"
        f"Message preview:\n{message[:200]}..."
    )
    
    if result:
        # 5. Trigger posting (see Step 3)
        self._execute_promo(servers, message)
```

#### Step 3: Create promotion executor
```python
# In src/promotion_helper.py, add:
import time
from .storage import get_last_post_time, set_last_post_time
from .server_config import get_rate_limit_hours

def execute_promo_queue(servers: List[dict], message: str, 
                         config: dict, callback=None) -> dict:
    """
    Execute promotion across multiple servers.
    
    Args:
        servers: List of server configs to post to
        message: Message to post
        config: App config
        callback: Optional callback(server_name, status, error)
        
    Returns:
        {"success": int, "skipped": int, "failed": int, "errors": []}
    """
    results = {"success": 0, "skipped": 0, "failed": 0, "errors": []}
    rate_limit = get_rate_limit_hours(config)
    
    for server in servers:
        server_name = server.get("friendly_name") or server.get("ocr_name")
        channels = server.get("promo_channels", [])
        
        if not channels:
            results["skipped"] += 1
            continue
        
        for channel in channels:
            key = f"{server_name}:{channel}"
            
            # Check rate limit
            last_post = get_last_post_time(key)
            if last_post and (time.time() - last_post) < (rate_limit * 3600):
                results["skipped"] += 1
                if callback:
                    callback(server_name, "skipped", "Rate limited")
                continue
            
            try:
                # TODO: Integrate with discord_nav to actually post
                # For now, just log
                print(f"Would post to {server_name} #{channel}")
                
                set_last_post_time(key)
                results["success"] += 1
                
                if callback:
                    callback(server_name, "success", None)
                    
            except Exception as e:
                results["failed"] += 1
                results["errors"].append(f"{server_name}: {e}")
                if callback:
                    callback(server_name, "failed", str(e))
        
        # Small delay between servers
        time.sleep(0.5)
    
    return results
```

#### Step 4: Add progress dialog
```python
# In src/ui.py, add new dialog:
class PromoProgressDialog(tk.Toplevel):
    """Shows progress during auto-promotion."""
    
    def __init__(self, parent, servers, message):
        super().__init__(parent)
        self.title("Promoting...")
        self.geometry("400x300")
        
        self.servers = servers
        self.message = message
        self.cancelled = False
        
        self._create_widgets()
        self._start_promo()
    
    def _create_widgets(self):
        main = ttk.Frame(self, padding="15")
        main.pack(fill=tk.BOTH, expand=True)
        
        ttk.Label(main, text="Auto-Promoting...", 
                 font=("", 12, "bold")).pack()
        
        self.progress_var = tk.DoubleVar()
        self.progress = ttk.Progressbar(main, variable=self.progress_var,
                                         maximum=len(self.servers))
        self.progress.pack(fill=tk.X, pady=10)
        
        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(main, textvariable=self.status_var).pack()
        
        # Log area
        self.log = tk.Text(main, height=10, width=50)
        self.log.pack(fill=tk.BOTH, expand=True, pady=10)
        
        ttk.Button(main, text="Cancel", 
                   command=self._cancel).pack(side=tk.RIGHT)
    
    def _update_progress(self, server, status, error):
        """Callback for promotion progress."""
        self.progress_var.set(self.progress_var.get() + 1)
        
        icon = {"success": "✓", "skipped": "⏭", "failed": "✗"}.get(status, "?")
        self.log.insert(tk.END, f"{icon} {server}: {status}\n")
        self.log.see(tk.END)
        self.status_var.set(f"Processing: {server}")
    
    def _start_promo(self):
        """Start promotion in background thread."""
        def promo_thread():
            results = execute_promo_queue(
                self.servers, self.message, 
                load_config(), self._update_progress
            )
            self.after(0, lambda: self._show_results(results))
        
        thread = threading.Thread(target=promo_thread, daemon=True)
        thread.start()
    
    def _show_results(self, results):
        self.status_var.set("Complete!")
        messagebox.showinfo(
            "Promo Complete",
            f"✓ Success: {results['success']}\n"
            f"⏭ Skipped: {results['skipped']}\n"
            f"✗ Failed: {results['failed']}"
        )
        self.destroy()
    
    def _cancel(self):
        self.cancelled = True
        self.destroy()
```

### Testing
```bash
# Test stream info fetching
python -m src.stream_info twitch yourname

# Test UI
python -m src.main --ui
# Click "Fetch Stream Info", enter your channel
# Click "Auto Promo to All"
```

---

## 2. Message Templates (MEDIUM PRIORITY)

### Goal
Allow customizable message templates with variable substitution.

### Template Variables
- `{title}` - Stream title
- `{game}` - Current game
- `{viewers}` - Viewer count
- `{url}` - Stream URL
- `{platform}` - Platform name (Twitch/Kick/YouTube)

### Implementation

#### Step 1: Add template storage
```python
# In src/server_config.py, add:
DEFAULT_TEMPLATE = """🔴 LIVE NOW: {title}
{game_line}
{viewers_line}
🔗 {url}"""

def get_message_templates(config: dict) -> dict:
    """Get all message templates."""
    return config.get("templates", {
        "default": DEFAULT_TEMPLATE,
        "minimal": "🔴 {title} - {url}",
        "detailed": "🔴 **LIVE** 🔴\n{title}\n🎮 {game}\n👀 {viewers} watching\n{url}"
    })

def set_message_template(name: str, template: str, config: dict) -> dict:
    """Save a message template."""
    if "templates" not in config:
        config["templates"] = {}
    config["templates"][name] = template
    save_config(config)
    return config
```

#### Step 2: Add template rendering
```python
# In src/stream_info.py or new src/templates.py:
def render_template(template: str, info: StreamInfo) -> str:
    """
    Render a message template with stream info.
    
    Args:
        template: Template string with {variables}
        info: StreamInfo dataclass
        
    Returns:
        Rendered message string
    """
    # Build substitution dict
    subs = {
        "title": info.title,
        "game": info.game or "Streaming",
        "viewers": str(info.viewers) if info.viewers else "",
        "url": info.stream_url,
        "platform": info.platform.title(),
        "username": info.username,
    }
    
    # Conditional lines (only include if value exists)
    subs["game_line"] = f"🎮 Playing: {info.game}" if info.game else ""
    subs["viewers_line"] = f"👀 {info.viewers} watching" if info.viewers else ""
    
    # Substitute
    result = template
    for key, value in subs.items():
        result = result.replace(f"{{{key}}}", value)
    
    # Clean up empty lines
    result = "\n".join(line for line in result.split("\n") if line.strip())
    
    return result
```

#### Step 3: Add template editor dialog
```python
# In src/ui.py, add:
class TemplateEditorDialog(tk.Toplevel):
    """Dialog for editing message templates."""
    
    def __init__(self, parent, config):
        super().__init__(parent)
        self.title("Message Templates")
        self.geometry("600x500")
        self.config = config
        self._create_widgets()
        
    def _create_widgets(self):
        main = ttk.Frame(self, padding="10")
        main.pack(fill=tk.BOTH, expand=True)
        
        # Template selector
        ttk.Label(main, text="Template:").pack(anchor=tk.W)
        templates = get_message_templates(self.config)
        
        self.template_var = tk.StringVar(value=list(templates.keys())[0])
        combo = ttk.Combobox(main, textvariable=self.template_var,
                             values=list(templates.keys()))
        combo.pack(fill=tk.X)
        combo.bind("<<ComboboxSelected>>", self._load_template)
        
        # Template editor
        ttk.Label(main, text="Template Content:").pack(anchor=tk.W, pady=(10, 0))
        self.editor = tk.Text(main, height=10, width=60)
        self.editor.pack(fill=tk.BOTH, expand=True)
        
        # Variables reference
        ttk.Label(main, text="Variables: {title}, {game}, {viewers}, {url}, "
                           "{platform}, {game_line}, {viewers_line}",
                 foreground="gray", wraplength=550).pack(anchor=tk.W, pady=5)
        
        # Preview
        ttk.Label(main, text="Preview:").pack(anchor=tk.W)
        self.preview = tk.Text(main, height=6, width=60, state="disabled")
        self.preview.pack(fill=tk.X)
        
        # Buttons
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=10)
        ttk.Button(btn_frame, text="Preview", command=self._preview).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="Save", command=self._save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Close", command=self.destroy).pack(side=tk.RIGHT)
        
        self._load_template()
    
    def _load_template(self, event=None):
        templates = get_message_templates(self.config)
        name = self.template_var.get()
        self.editor.delete("1.0", tk.END)
        self.editor.insert("1.0", templates.get(name, ""))
    
    def _preview(self):
        # Mock stream info for preview
        mock_info = StreamInfo(
            platform="twitch", username="yourname",
            title="Epic Gaming Session!", game="Fortnite",
            is_live=True, viewers=1234,
            stream_url="https://twitch.tv/yourname"
        )
        
        template = self.editor.get("1.0", tk.END).strip()
        rendered = render_template(template, mock_info)
        
        self.preview.configure(state="normal")
        self.preview.delete("1.0", tk.END)
        self.preview.insert("1.0", rendered)
        self.preview.configure(state="disabled")
    
    def _save(self):
        name = self.template_var.get()
        template = self.editor.get("1.0", tk.END).strip()
        set_message_template(name, template, self.config)
        messagebox.showinfo("Saved", f"Template '{name}' saved!")
```

---

## 3. Mid-Stream Game Detection (LOW PRIORITY)

### Goal
Automatically detect when streamer switches games and re-promote to relevant servers.

### Implementation

#### Step 1: Background polling
```python
# Create new file: src/stream_monitor.py
import time
import threading
from typing import Callable, Optional
from .stream_info import get_stream_info, StreamInfo

class StreamMonitor:
    """
    Monitors a stream for changes (game switches, going live/offline).
    """
    
    def __init__(self, platform: str, username: str, 
                 poll_interval: int = 60):
        self.platform = platform
        self.username = username
        self.poll_interval = poll_interval
        
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_info: Optional[StreamInfo] = None
        
        # Callbacks
        self.on_game_change: Optional[Callable[[str, str], None]] = None
        self.on_live_change: Optional[Callable[[bool], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None
    
    def start(self):
        """Start monitoring in background thread."""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        """Stop monitoring."""
        self._running = False
    
    def _poll_loop(self):
        """Main polling loop."""
        while self._running:
            try:
                info = get_stream_info(self.platform, self.username)
                
                if self._last_info:
                    # Check for game change
                    if info.game != self._last_info.game and info.is_live:
                        if self.on_game_change:
                            self.on_game_change(self._last_info.game, info.game)
                    
                    # Check for live status change
                    if info.is_live != self._last_info.is_live:
                        if self.on_live_change:
                            self.on_live_change(info.is_live)
                
                self._last_info = info
                
            except Exception as e:
                if self.on_error:
                    self.on_error(str(e))
            
            time.sleep(self.poll_interval)


# Usage example:
def handle_game_change(old_game: str, new_game: str):
    print(f"Game changed from {old_game} to {new_game}")
    # Trigger re-promotion to servers tagged for new_game

monitor = StreamMonitor("twitch", "yourname", poll_interval=60)
monitor.on_game_change = handle_game_change
monitor.start()
```

#### Step 2: Add UI controls
```python
# In src/ui.py, add to stream_frame section:
self.monitor_var = tk.BooleanVar(value=False)
ttk.Checkbutton(stream_frame, text="Monitor for game changes",
               variable=self.monitor_var,
               command=self._toggle_monitor).pack(anchor=tk.W)

def _toggle_monitor(self):
    if self.monitor_var.get():
        # Start monitoring
        if hasattr(self, '_current_stream_info') and self._current_stream_info:
            info = self._current_stream_info
            self._stream_monitor = StreamMonitor(info.platform, info.username)
            self._stream_monitor.on_game_change = self._on_game_change
            self._stream_monitor.start()
    else:
        # Stop monitoring
        if hasattr(self, '_stream_monitor'):
            self._stream_monitor.stop()

def _on_game_change(self, old_game: str, new_game: str):
    # Notify user and optionally auto-promote
    self.after(0, lambda: messagebox.askyesno(
        "Game Changed!",
        f"Switched from {old_game} to {new_game}.\n\n"
        "Re-promote to relevant servers?"
    ))
```

---

## 4. OAuth Setup Helper (MEDIUM PRIORITY)

### Goal
Guide users through setting up API credentials for each platform.

### Implementation

#### Step 1: Create setup wizard dialog
```python
# In src/ui.py or new src/oauth_setup.py:
class OAuthSetupDialog(tk.Toplevel):
    """Guides user through platform API setup."""
    
    PLATFORMS = {
        "twitch": {
            "name": "Twitch",
            "url": "https://dev.twitch.tv/console/apps",
            "env_vars": ["TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"],
            "instructions": [
                "1. Go to https://dev.twitch.tv/console/apps",
                "2. Click 'Register Your Application'",
                "3. Name: 'Discord Promo Helper' (or anything)",
                "4. OAuth Redirect: http://localhost",
                "5. Category: Application Integration",
                "6. Click 'Create'",
                "7. Copy Client ID and generate Client Secret"
            ]
        },
        "youtube": {
            "name": "YouTube",
            "url": "https://console.cloud.google.com/apis/credentials",
            "env_vars": ["YOUTUBE_API_KEY"],
            "instructions": [
                "1. Go to https://console.cloud.google.com/",
                "2. Create a new project or select existing",
                "3. Enable 'YouTube Data API v3'",
                "4. Go to Credentials → Create Credentials → API Key",
                "5. Copy the API key",
                "Note: Free tier allows 10,000 units/day"
            ]
        },
        "kick": {
            "name": "Kick",
            "url": None,
            "env_vars": [],
            "instructions": [
                "Kick uses a public API - no setup required!",
                "Note: This is an unofficial API and may change."
            ]
        }
    }
    
    def __init__(self, parent):
        super().__init__(parent)
        self.title("API Setup")
        self.geometry("600x450")
        self._create_widgets()
    
    def _create_widgets(self):
        main = ttk.Frame(self, padding="15")
        main.pack(fill=tk.BOTH, expand=True)
        
        ttk.Label(main, text="Platform API Setup", 
                 font=("", 14, "bold")).pack(anchor=tk.W)
        ttk.Label(main, text="Configure API keys to enable stream info fetching.",
                 wraplength=550).pack(anchor=tk.W, pady=(0, 15))
        
        # Platform tabs
        notebook = ttk.Notebook(main)
        notebook.pack(fill=tk.BOTH, expand=True)
        
        for key, platform in self.PLATFORMS.items():
            frame = ttk.Frame(notebook, padding="10")
            notebook.add(frame, text=platform["name"])
            
            # Instructions
            ttk.Label(frame, text="Setup Instructions:",
                     font=("", 10, "bold")).pack(anchor=tk.W)
            
            for instruction in platform["instructions"]:
                ttk.Label(frame, text=instruction, 
                         wraplength=500).pack(anchor=tk.W, padx=10)
            
            if platform["url"]:
                btn = ttk.Button(frame, text=f"Open {platform['name']} Console",
                                command=lambda u=platform["url"]: self._open_url(u))
                btn.pack(anchor=tk.W, pady=10)
            
            # Environment variables
            if platform["env_vars"]:
                ttk.Label(frame, text="\nEnvironment Variables:",
                         font=("", 10, "bold")).pack(anchor=tk.W)
                
                for var in platform["env_vars"]:
                    var_frame = ttk.Frame(frame)
                    var_frame.pack(fill=tk.X, pady=2)
                    
                    ttk.Label(var_frame, text=f"{var}:").pack(side=tk.LEFT)
                    
                    current = os.environ.get(var, "")
                    display = current[:10] + "..." if current else "(not set)"
                    status = "✓" if current else "✗"
                    ttk.Label(var_frame, text=f"{status} {display}",
                             foreground="green" if current else "red").pack(side=tk.LEFT, padx=5)
        
        # Close button
        ttk.Button(main, text="Close", command=self.destroy).pack(side=tk.RIGHT, pady=10)
    
    def _open_url(self, url):
        import webbrowser
        webbrowser.open(url)
```

#### Step 2: Add .env file support
```python
# In src/stream_info.py, at the top, add:
from pathlib import Path

def _load_env():
    """Load environment variables from .env file if it exists."""
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    # Don't override existing env vars
                    if key not in os.environ:
                        os.environ[key] = value.strip('"').strip("'")

_load_env()
```

#### Step 3: Create .env.example
```bash
# .env.example
# Copy to .env and fill in your values

# Twitch API (https://dev.twitch.tv/console/apps)
TWITCH_CLIENT_ID=your_client_id_here
TWITCH_CLIENT_SECRET=your_client_secret_here

# YouTube Data API (https://console.cloud.google.com/apis/credentials)
YOUTUBE_API_KEY=your_api_key_here
```

---

## Development

### Requirements

- Python 3.11+
- Tesseract OCR: `brew install tesseract`
- Tkinter: `brew install python-tk@3.11`

### Permissions (macOS)

- Screen Recording (for screenshots)
- Accessibility (for mouse/keyboard)

### Running Tests

```bash
source .venv/bin/activate
python scripts/test_full_scan.py
python -m pytest tests/

# Test stream info module
python -m src.stream_info twitch yourname
python -m src.stream_info https://kick.com/xqc
```

### Code Style

- Type hints where practical
- Docstrings for public functions
- JSON for persistence (no database)


