"""Stream information fetcher for Twitch, Kick, and YouTube.

Auto-detects stream title and game from streaming platforms.
Used to pre-fill promotion messages with accurate info.
"""
from __future__ import annotations
import json
import os
import re
from dataclasses import dataclass
from typing import Optional, Dict, Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# API Configuration
# Users should set these environment variables or use .env file
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")


@dataclass
class StreamInfo:
    """Container for stream information."""
    platform: str
    username: str
    title: str = ""
    game: str = ""
    is_live: bool = False
    viewers: int = 0
    thumbnail_url: str = ""
    stream_url: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "username": self.username,
            "title": self.title,
            "game": self.game,
            "is_live": self.is_live,
            "viewers": self.viewers,
            "thumbnail_url": self.thumbnail_url,
            "stream_url": self.stream_url,
            "error": self.error
        }


def _http_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 10) -> tuple[int, str]:
    """Make an HTTP GET request and return (status_code, response_body)."""
    req = Request(url, headers=headers or {})
    try:
        with urlopen(req, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as e:
        return e.code, e.read().decode("utf-8") if e.fp else ""
    except URLError as e:
        return 0, str(e.reason)
    except Exception as e:
        return 0, str(e)


# =============================================================================
# TWITCH API
# =============================================================================

class TwitchClient:
    """Client for Twitch Helix API.
    
    Requires TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET environment variables.
    Get credentials at: https://dev.twitch.tv/console/apps
    """
    
    BASE_URL = "https://api.twitch.tv/helix"
    TOKEN_URL = "https://id.twitch.tv/oauth2/token"
    
    def __init__(self, client_id: str = "", client_secret: str = ""):
        self.client_id = client_id or TWITCH_CLIENT_ID
        self.client_secret = client_secret or TWITCH_CLIENT_SECRET
        self._access_token: Optional[str] = None
    
    def _get_app_token(self) -> Optional[str]:
        """Get an app access token using client credentials flow."""
        if not self.client_id or not self.client_secret:
            return None
        
        if self._access_token:
            return self._access_token
        
        # Build token request
        url = (
            f"{self.TOKEN_URL}"
            f"?client_id={self.client_id}"
            f"&client_secret={self.client_secret}"
            f"&grant_type=client_credentials"
        )
        
        # Make POST request
        from urllib.request import Request, urlopen
        req = Request(url, method="POST", data=b"")
        
        try:
            with urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
                self._access_token = data.get("access_token")
                return self._access_token
        except Exception as e:
            print(f"Failed to get Twitch token: {e}")
            return None
    
    def _api_request(self, endpoint: str) -> tuple[int, Dict[str, Any]]:
        """Make authenticated request to Twitch API."""
        token = self._get_app_token()
        if not token:
            return 401, {"error": "Missing Twitch credentials"}
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": self.client_id
        }
        
        status, body = _http_get(f"{self.BASE_URL}{endpoint}", headers)
        
        try:
            return status, json.loads(body)
        except json.JSONDecodeError:
            return status, {"error": body}
    
    def get_stream(self, username: str) -> StreamInfo:
        """Get live stream info for a Twitch user.
        
        Args:
            username: Twitch username (not display name)
            
        Returns:
            StreamInfo with stream details or error
        """
        info = StreamInfo(
            platform="twitch",
            username=username,
            stream_url=f"https://twitch.tv/{username}"
        )
        
        if not self.client_id or not self.client_secret:
            info.error = "Missing Twitch API credentials. Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET."
            return info
        
        # Get stream data
        status, data = self._api_request(f"/streams?user_login={username}")
        
        if status != 200:
            info.error = data.get("error", f"API error: {status}")
            return info
        
        streams = data.get("data", [])
        if not streams:
            # User is offline - try to get channel info anyway
            info.is_live = False
            return self._get_channel_info(username, info)
        
        # User is live
        stream = streams[0]
        info.is_live = True
        info.title = stream.get("title", "")
        info.game = stream.get("game_name", "")
        info.viewers = stream.get("viewer_count", 0)
        info.thumbnail_url = stream.get("thumbnail_url", "").replace("{width}", "320").replace("{height}", "180")
        
        return info
    
    def _get_channel_info(self, username: str, info: StreamInfo) -> StreamInfo:
        """Get channel info for offline user (last title/game)."""
        # First get user ID from username
        status, data = self._api_request(f"/users?login={username}")
        
        if status != 200 or not data.get("data"):
            info.error = "User not found"
            return info
        
        user_id = data["data"][0]["id"]
        
        # Get channel info
        status, data = self._api_request(f"/channels?broadcaster_id={user_id}")
        
        if status != 200 or not data.get("data"):
            return info
        
        channel = data["data"][0]
        info.title = channel.get("title", "")
        info.game = channel.get("game_name", "")
        
        return info


# =============================================================================
# KICK API (Unofficial)
# =============================================================================

class KickClient:
    """Client for Kick streaming platform.
    
    Uses unofficial public API - no authentication required.
    May break if Kick changes their API.
    """
    
    BASE_URL = "https://kick.com/api/v1"
    
    def get_stream(self, username: str) -> StreamInfo:
        """Get stream info for a Kick user.
        
        Args:
            username: Kick username
            
        Returns:
            StreamInfo with stream details or error
        """
        info = StreamInfo(
            platform="kick",
            username=username,
            stream_url=f"https://kick.com/{username}"
        )
        
        # Try channel endpoint
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        }
        
        status, body = _http_get(f"{self.BASE_URL}/channels/{username}", headers)
        
        if status != 200:
            # Try v2 API
            status, body = _http_get(f"https://kick.com/api/v2/channels/{username}", headers)
        
        if status != 200:
            info.error = f"Could not fetch Kick channel info (status: {status})"
            return info
        
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            info.error = "Invalid response from Kick API"
            return info
        
        # Check if livestream exists
        livestream = data.get("livestream")
        if livestream:
            info.is_live = True
            info.title = livestream.get("session_title", "")
            info.viewers = livestream.get("viewer_count", 0)
            
            # Game/category info
            categories = livestream.get("categories", [])
            if categories:
                info.game = categories[0].get("name", "")
            
            info.thumbnail_url = livestream.get("thumbnail", {}).get("url", "")
        else:
            info.is_live = False
            # Get last stream info if available
            info.title = data.get("previous_livestreams", [{}])[0].get("session_title", "") if data.get("previous_livestreams") else ""
        
        return info


# =============================================================================
# YOUTUBE API
# =============================================================================

class YouTubeClient:
    """Client for YouTube Data API v3.
    
    Requires YOUTUBE_API_KEY environment variable.
    Get key at: https://console.cloud.google.com/apis/credentials
    
    Note: Live stream detection requires searching by channel, which may have
    quota implications. Use sparingly.
    """
    
    BASE_URL = "https://www.googleapis.com/youtube/v3"
    
    def __init__(self, api_key: str = ""):
        self.api_key = api_key or YOUTUBE_API_KEY
    
    def get_stream(self, channel_input: str) -> StreamInfo:
        """Get live stream info for a YouTube channel.
        
        Args:
            channel_input: Can be:
                - Channel ID (starts with UC)
                - Channel handle (@username)
                - Custom URL
                - Live video URL/ID
                
        Returns:
            StreamInfo with stream details or error
        """
        info = StreamInfo(
            platform="youtube",
            username=channel_input
        )
        
        if not self.api_key:
            info.error = "Missing YouTube API key. Set YOUTUBE_API_KEY."
            return info
        
        # Detect input type
        channel_id = None
        video_id = None
        
        # Check if it's a video URL
        video_match = re.search(r'(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})', channel_input)
        if video_match:
            video_id = video_match.group(1)
            return self._get_video_info(video_id, info)
        
        # Check if it's a channel ID
        if channel_input.startswith("UC") and len(channel_input) == 24:
            channel_id = channel_input
        elif channel_input.startswith("@"):
            # Handle @username format
            channel_id = self._resolve_handle(channel_input)
        else:
            # Try as username or custom URL
            channel_id = self._search_channel(channel_input)
        
        if not channel_id:
            info.error = "Could not find YouTube channel"
            return info
        
        info.stream_url = f"https://youtube.com/channel/{channel_id}/live"
        
        # Search for live broadcast
        return self._find_live_stream(channel_id, info)
    
    def _api_request(self, endpoint: str, params: Dict[str, str]) -> tuple[int, Dict[str, Any]]:
        """Make request to YouTube API."""
        params["key"] = self.api_key
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{self.BASE_URL}/{endpoint}?{query}"
        
        status, body = _http_get(url)
        
        try:
            return status, json.loads(body)
        except json.JSONDecodeError:
            return status, {"error": body}
    
    def _resolve_handle(self, handle: str) -> Optional[str]:
        """Resolve @handle to channel ID."""
        # Remove @ if present
        handle = handle.lstrip("@")
        
        status, data = self._api_request("channels", {
            "part": "id",
            "forHandle": handle
        })
        
        if status == 200 and data.get("items"):
            return data["items"][0]["id"]
        return None
    
    def _search_channel(self, query: str) -> Optional[str]:
        """Search for channel by name."""
        status, data = self._api_request("search", {
            "part": "snippet",
            "q": query,
            "type": "channel",
            "maxResults": "1"
        })
        
        if status == 200 and data.get("items"):
            return data["items"][0]["snippet"]["channelId"]
        return None
    
    def _find_live_stream(self, channel_id: str, info: StreamInfo) -> StreamInfo:
        """Find active live stream for a channel."""
        status, data = self._api_request("search", {
            "part": "snippet",
            "channelId": channel_id,
            "eventType": "live",
            "type": "video",
            "maxResults": "1"
        })
        
        if status != 200:
            info.error = data.get("error", {}).get("message", f"API error: {status}")
            return info
        
        items = data.get("items", [])
        if not items:
            info.is_live = False
            info.error = "No live stream found"
            return info
        
        video_id = items[0]["id"]["videoId"]
        return self._get_video_info(video_id, info)
    
    def _get_video_info(self, video_id: str, info: StreamInfo) -> StreamInfo:
        """Get video/stream details."""
        info.stream_url = f"https://youtube.com/watch?v={video_id}"
        
        status, data = self._api_request("videos", {
            "part": "snippet,liveStreamingDetails",
            "id": video_id
        })
        
        if status != 200 or not data.get("items"):
            info.error = "Could not fetch video info"
            return info
        
        video = data["items"][0]
        snippet = video.get("snippet", {})
        live_details = video.get("liveStreamingDetails", {})
        
        info.title = snippet.get("title", "")
        info.game = snippet.get("categoryId", "")  # Note: This is category ID, not name
        info.thumbnail_url = snippet.get("thumbnails", {}).get("high", {}).get("url", "")
        
        # Check if actually live
        if live_details:
            info.is_live = "actualEndTime" not in live_details
            info.viewers = int(live_details.get("concurrentViewers", 0))
        
        return info


# =============================================================================
# UNIFIED INTERFACE
# =============================================================================

def get_stream_info(platform: str, username: str) -> StreamInfo:
    """Get stream info from any supported platform.
    
    Args:
        platform: One of "twitch", "kick", "youtube"
        username: Platform-specific username/channel identifier
        
    Returns:
        StreamInfo dataclass with stream details
        
    Example:
        >>> info = get_stream_info("twitch", "ninja")
        >>> if info.is_live:
        ...     print(f"Live: {info.title} - Playing {info.game}")
    """
    platform = platform.lower().strip()
    
    if platform == "twitch":
        return TwitchClient().get_stream(username)
    elif platform == "kick":
        return KickClient().get_stream(username)
    elif platform in ("youtube", "yt"):
        return YouTubeClient().get_stream(username)
    else:
        return StreamInfo(
            platform=platform,
            username=username,
            error=f"Unknown platform: {platform}. Use 'twitch', 'kick', or 'youtube'."
        )


def detect_platform(url: str) -> tuple[str, str]:
    """Detect platform and extract username from URL.
    
    Args:
        url: Stream URL or username
        
    Returns:
        Tuple of (platform, username)
        
    Example:
        >>> detect_platform("https://twitch.tv/ninja")
        ('twitch', 'ninja')
        >>> detect_platform("https://youtube.com/@MrBeast")
        ('youtube', '@MrBeast')
    """
    url = url.strip()
    
    # Twitch
    match = re.search(r'twitch\.tv/(\w+)', url, re.I)
    if match:
        return "twitch", match.group(1)
    
    # Kick
    match = re.search(r'kick\.com/(\w+)', url, re.I)
    if match:
        return "kick", match.group(1)
    
    # YouTube
    yt_patterns = [
        r'youtube\.com/(?:channel/|c/|@)([^/\s?]+)',
        r'youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
        r'youtu\.be/([a-zA-Z0-9_-]{11})'
    ]
    for pattern in yt_patterns:
        match = re.search(pattern, url, re.I)
        if match:
            return "youtube", match.group(1)
    
    # Unknown - return as-is, assume twitch
    return "twitch", url


# =============================================================================
# CLI TESTING
# =============================================================================

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 3:
        print("Usage: python -m src.stream_info <platform> <username>")
        print("  or:  python -m src.stream_info <url>")
        print()
        print("Platforms: twitch, kick, youtube")
        print()
        print("Environment variables needed:")
        print("  TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET")
        print("  YOUTUBE_API_KEY")
        print()
        print("Examples:")
        print("  python -m src.stream_info twitch ninja")
        print("  python -m src.stream_info https://kick.com/xqc")
        sys.exit(1)
    
    if len(sys.argv) == 2:
        # URL mode
        platform, username = detect_platform(sys.argv[1])
    else:
        platform = sys.argv[1]
        username = sys.argv[2]
    
    print(f"Fetching {platform} info for: {username}")
    print("-" * 40)
    
    info = get_stream_info(platform, username)
    
    print(f"Platform:  {info.platform}")
    print(f"Username:  {info.username}")
    print(f"Live:      {info.is_live}")
    print(f"Title:     {info.title or '(none)'}")
    print(f"Game:      {info.game or '(none)'}")
    print(f"Viewers:   {info.viewers}")
    print(f"URL:       {info.stream_url}")
    
    if info.error:
        print(f"Error:     {info.error}")
