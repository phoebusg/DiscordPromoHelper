import subprocess
import sys
import os
import base64
import ctypes
import shutil
try:
    import pytesseract
    _HAS_PYTESSERACT = True
except Exception:
    pytesseract = None
    _HAS_PYTESSERACT = False
try:
    from PIL import Image, ImageGrab
    _HAS_PIL = True
except Exception:
    Image = None
    ImageGrab = None
    _HAS_PIL = False

try:
    import pyautogui
    _HAS_PYAUTOGUI = True
except Exception:
    pyautogui = None
    _HAS_PYAUTOGUI = False

try:
    import psutil
    _HAS_PSUTIL = True
except Exception:
    psutil = None
    _HAS_PSUTIL = False
import time
import tempfile
import urllib.request
import re
import unicodedata
import json
from pathlib import Path
from PIL import ImageStat, ImageFilter


def run_command_as_admin(command):
    """
    Attempts to run the given command with administrative privileges.
    """
    if sys.platform.startswith('win32'):
        subprocess.run(["powershell", "Start-Process", command[0], "-ArgumentList", ' '.join(command[1:]), "-Verb", "RunAs"], check=True)
    else:
        subprocess.run(["sudo"] + command, check=True)


def is_homebrew_installed() -> bool:
    """Return True if Homebrew (`brew`) appears to be installed on this system."""
    try:
        if shutil.which("brew"):
            return True
    except Exception:
        pass
    # Check common Homebrew binary locations (Intel / Apple Silicon)
    for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        try:
            if os.path.isfile(p):
                return True
        except Exception:
            pass
    return False


def get_homebrew_prefix() -> str | None:
    """Return Homebrew prefix directory if available, else None.

    Attempts to call `brew --prefix`, falling back to common prefixes.
    """
    try:
        if shutil.which("brew"):
            proc = subprocess.run(["brew", "--prefix"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            prefix = proc.stdout.strip()
            if prefix:
                return prefix
    except Exception:
        pass
    for candidate in ("/opt/homebrew", "/usr/local"):
        try:
            if os.path.isdir(candidate):
                return candidate
        except Exception:
            pass
    return None

def install_tesseract_on_windows():
    """
    Download and install Tesseract on Windows without changing PowerShell execution policy.
    """
    # Prefer Chocolatey when available because it usually installs the latest packaged binary
    def is_choco_installed():
        try:
            subprocess.run(["choco", "--version"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return True
        except Exception:
            return False

    if is_choco_installed():
        print("Chocolatey detected. Installing Tesseract via choco...")
        subprocess.run(["choco", "install", "tesseract", "-y"], check=True)
        print("Tesseract installation completed via Chocolatey.")
        return

    # Fallback: download the latest UB Mannheim installer (known reliable Windows builds)
    def get_latest_mannheim_installer_url():
        base_url = "https://digi.bib.uni-mannheim.de/tesseract/"
        try:
            with urllib.request.urlopen(base_url, timeout=15) as resp:
                html = resp.read().decode(errors='ignore')
        except Exception:
            return None

        # Find all .exe links matching the Windows installer naming
        matches = re.findall(r'href=["\']([^"\']*tesseract-ocr-w64-setup[^"\']*\.exe)["\']', html, flags=re.IGNORECASE)
        if not matches:
            # Try simpler pattern
            matches = re.findall(r'>(tesseract-ocr-w64-setup[^<]*\.exe)<', html, flags=re.IGNORECASE)
            matches = [m for m in matches]

        if not matches:
            return None

        # Normalize to full URLs and pick the lexicographically largest (newest)
        norm = []
        for m in matches:
            if m.startswith('http'):
                norm.append(m)
            else:
                norm.append(urllib.request.urljoin(base_url, m))

        norm.sort()
        return norm[-1]

    tesseract_installer_url = get_latest_mannheim_installer_url()
    if not tesseract_installer_url:
        # Fallback to a known installer if scraping failed
        tesseract_installer_url = "https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-5.3.3.20231005.exe"

    temp_dir = tempfile.gettempdir()
    tesseract_installer_path = os.path.join(temp_dir, "tesseract-installer.exe")

    print("Downloading Tesseract installer...", tesseract_installer_url)
    download_command = ["powershell", "-Command", f"Invoke-WebRequest -Uri '{tesseract_installer_url}' -OutFile '{tesseract_installer_path}'"]
    subprocess.run(download_command, check=True)

    print("Installing Tesseract, please accept any UAC prompts...")
    install_command = ["powershell", "-Command", f"Start-Process -FilePath '{tesseract_installer_path}' -ArgumentList '/S' -Wait -Verb RunAs"]
    subprocess.run(install_command, check=True)

    # Cleanup the installer
    try:
        os.remove(tesseract_installer_path)
    except Exception:
        pass
    print("Tesseract installation completed.")

    # Note: some flows may attempt to use Homebrew on macOS; helper checks below
    # will be no-ops on Windows if Homebrew is not installed.
    if not is_homebrew_installed():
        print("Homebrew not found. Skipping Homebrew-based steps on Windows.")
    else:
        print("Homebrew is installed (unexpected on Windows). Skipping further Homebrew steps.")

    # Use Homebrew prefix if possible (handles /opt/homebrew on Apple Silicon)
    brew_prefix = get_homebrew_prefix()
    brew_cmd = "brew"
    if brew_prefix:
        possible = os.path.join(brew_prefix, "bin", "brew")
        if os.path.isfile(possible):
            brew_cmd = possible

    print("Installing Tesseract using Homebrew...")
    subprocess.run([brew_cmd, "install", "tesseract"], check=True)
    print("Tesseract installation completed.")

def ensure_system_dependencies():
    """Ensure system (non-python) dependencies are present.

    Currently ensures Tesseract is installed on the host platform. Will
    attempt to install using platform-native package managers (choco/brew/apt).
    """
    try:
        if not is_tesseract_installed():
            print("Tesseract not found: attempting automatic install...")
            install_tesseract()
        else:
            print("Tesseract already installed.")
    except Exception as e:
        print("Automatic dependency installation failed:", e)
        print("Please install Tesseract manually and re-run the script.")

def install_homebrew():
    """
    Install Homebrew on macOS using the official installation script.
    """
    homebrew_url = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
    homebrew_script_path = os.path.join(tempfile.gettempdir(), "homebrew-install.sh")

    print("Downloading Homebrew installer...")
    download_command = ["curl", "-fsSL", "-o", homebrew_script_path, homebrew_url]
    subprocess.run(download_command, check=True)

    print("Installing Homebrew, please accept any prompts...")
    install_command = ["/bin/bash", homebrew_script_path]
    subprocess.run(install_command, check=True)

    # Cleanup the installer
    try:
        os.remove(homebrew_script_path)
    except Exception:
        pass
    print("Homebrew installation completed.")

def install_tesseract_on_macos():
    """
    Install Tesseract on macOS using Homebrew, if homebrew is not installed, it will be installed.
    """
    if not is_homebrew_installed():
        print("Homebrew not found. Installing...")
        install_homebrew()
        print("Homebrew installation completed.")
    else:
        print("Homebrew is already installed.")

    print("Installing Tesseract using Homebrew...")
    subprocess.run(["brew", "install", "tesseract"], check=True)
    print("Tesseract installation completed.")

def install_tesseract_on_linux():
    """
    Install Tesseract on Linux using the system's package manager.
    """
    print("Installing Tesseract using the system's package manager...")
    if os.path.isfile("/etc/debian_version"):
        subprocess.run(["sudo", "apt", "install", "tesseract-ocr"], check=True)
    elif os.path.isfile("/etc/redhat-release"):
        subprocess.run(["sudo", "yum", "install", "tesseract"], check=True)
    else:
        print("Unsupported Linux distribution. Please install Tesseract manually.")
        return
    print("Tesseract installation completed.")

def install_tesseract():
    """
    Checks if Tesseract is installed. If not, installs it.
    """
    if not is_tesseract_installed():
        print("Tesseract not found. Installing...")
        if sys.platform.startswith('win32'):
            install_tesseract_on_windows()
        elif sys.platform.startswith('darwin'):
            install_tesseract_on_macos()
        elif sys.platform.startswith('linux'):
            install_tesseract_on_linux()
        else:
            print("Unsupported OS.")
            return
        print("Tesseract installation completed.")
    else:
        print("Tesseract is already installed.")

def update_tesseract_cmd():
    """
    Sets the Tesseract command path based on the operating system and updates the system PATH temporarily for the script's execution context.
    """
    tesseract_cmd_path = ""
    if sys.platform.startswith('win32'):
        tesseract_cmd_path = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    elif sys.platform.startswith('darwin') or sys.platform.startswith('linux'):
        tesseract_cmd_path = 'tesseract'  # Assuming Tesseract is in the PATH for Unix-like systems

    # Update pytesseract command path
    pytesseract.pytesseract.tesseract_cmd = tesseract_cmd_path
    print(f"Tesseract command updated: {pytesseract.pytesseract.tesseract_cmd}")

    if sys.platform.startswith('win32'):
        # Temporarily add Tesseract to PATH for the script's execution context
        os.environ["PATH"] += os.pathsep + os.path.dirname(tesseract_cmd_path)
        print(f"Temporarily added Tesseract to PATH for this script's context: {os.path.dirname(tesseract_cmd_path)}")

def run_powershell_command_as_admin(command):
    """
    Runs a PowerShell command with administrative privileges.
    """
    # Encode PowerShell script to Base64
    # This avoids issues with special characters in the command
    command_bytes = command.encode('utf-16le')
    command_base64 = base64.b64encode(command_bytes).decode()

    # Use PowerShell to decode and run the original command
    ps_command = f"powershell -EncodedCommand {command_base64}"
    
    # Prompt for UAC elevation and execute the PowerShell command
    ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell", ps_command, None, 1)

def add_tesseract_to_path_windows(tesseract_path):
    """
    Adds Tesseract to the system PATH on Windows and attempts to escalate privileges.
    """
    # Check if Tesseract is already in the PATH
    current_path = os.environ.get('Path', '')
    if tesseract_path in current_path.split(';'):
        print("Tesseract is already in the PATH.")
        return
    # Add the provided tesseract_path to the process PATH for this session.
    try:
        dirname = os.path.dirname(tesseract_path)
        if dirname and dirname not in current_path:
            os.environ['Path'] = current_path + ';' + dirname if current_path else dirname
            print(f"Added '{dirname}' to PATH for this process.")
    except Exception:
        print("Failed to add Tesseract to PATH for this process.")


def is_tesseract_installed() -> bool:
    """Return True if a Tesseract binary can be found on the system."""
    try:
        if shutil.which('tesseract'):
            return True
    except Exception:
        pass
    # common Windows install location
    if sys.platform.startswith('win32'):
        possible = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        return os.path.isfile(possible)
    return False


def check_tesseract_path():
    """Ensure pytesseract is pointed at an available tesseract binary if possible."""
    if not _HAS_PYTESSERACT:
        return
    try:
        if sys.platform.startswith('win32'):
            default = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
            if os.path.isfile(default):
                pytesseract.pytesseract.tesseract_cmd = default
                return
        # otherwise rely on PATH
        if shutil.which('tesseract'):
            pytesseract.pytesseract.tesseract_cmd = 'tesseract'
    except Exception:
        pass


def run_discord():
    """Attempt to launch Discord (best-effort for macOS/Linux/Windows)."""
    try:
        if sys.platform.startswith('darwin'):
            subprocess.Popen(['open', '-a', 'Discord'])
        elif sys.platform.startswith('linux'):
            # many linux distros provide `discord` command after install
            subprocess.Popen(['discord'])
        elif sys.platform.startswith('win32'):
            # let shell handle the registered protocol/app
            subprocess.Popen(['start', 'Discord'], shell=True)
    except Exception:
        pass


def find_and_focus_discord():
    """Try to locate a visible Discord window and return its bbox (left, top, width, height).

    Attempts to activate the window when possible. Returns None if not found.
    """
    try:
        import pygetwindow as gw
    except Exception:
        gw = None

    # Begin detection logic
    # On macOS, try to activate Discord via AppleScript first (more reliable than open)
    if sys.platform.startswith('darwin'):
        try:
            subprocess.run(["osascript", "-e", 'tell application "Discord" to activate'], check=False)
            time.sleep(0.4)
        except Exception:
            pass

    # If the foreground window is Discord, return its bbox immediately
    try:
        app_name, title, bbox = get_foreground_window_info()
        # Check for "- Discord" suffix to identify actual Discord window (not VS Code with Discord files)
        is_discord = (app_name and 'discord' in (app_name or '').lower()) or \
                     (title and title.lower().endswith('- discord'))
        if is_discord:
            # Return bbox if we have it, else attempt to find via pygetwindow
            if bbox:
                try:
                    l, t, r, b = bbox
                    return (l, t, r - l, b - t)
                except Exception:
                    pass
    except Exception:
        pass

    # Try pygetwindow - works cross-platform but API differs
    if gw is not None:
        try:
            # Find Discord windows - look for title ending with "- Discord"
            titles = gw.getAllTitles()
            for t in titles:
                if not t:
                    continue
                # Match windows with "- Discord" at the end (actual Discord app)
                if t.lower().endswith('- discord') or t.lower() == 'discord':
                    try:
                        # On Windows, use getWindowsWithTitle which returns window objects
                        wins = gw.getWindowsWithTitle(t)
                        for w in wins:
                            if hasattr(w, 'left') and hasattr(w, 'width'):
                                # Windows pygetwindow uses object properties directly
                                left, top, width, height = w.left, w.top, w.width, w.height
                                # Try to activate/focus the window
                                try:
                                    if hasattr(w, 'activate'):
                                        w.activate()
                                except Exception:
                                    pass
                                return (int(left), int(top), int(width), int(height))
                    except Exception:
                        # Fallback: try getWindowGeometry for macOS compatibility  
                        try:
                            geom = gw.getWindowGeometry(t)
                            if geom:
                                left, top, width, height = (int(geom[0]), int(geom[1]), int(geom[2]), int(geom[3]))
                                return (left, top, width, height)
                        except Exception:
                            pass
                        continue
        except Exception:
            pass

    # Fallback: try to find a Discord process and bring it to front using platform-specific commands
    try:
        for proc in psutil.process_iter(['name', 'exe', 'cmdline']):
            name = (proc.info.get('name') or '').lower()
            if 'discord' in name:
                # best-effort activation: on macOS use `open -a Discord`, on Windows rely on run_discord
                try:
                    if sys.platform.startswith('darwin'):
                        subprocess.Popen(['open', '-a', 'Discord'])
                        # try to obtain window bounds via AppleScript (System Events) for macOS
                        try:
                            script = 'tell application "System Events"\n    if exists (process "Discord") then\n        tell process "Discord"\n            if (count of windows) > 0 then\n                set b to bounds of window 1\n                return b\n            end if\n        end tell\n    end if\nend tell\n'
                            proc_as = subprocess.run(["osascript", "-e", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                            out = proc_as.stdout.strip()
                            if out:
                                parts = [int(x.strip()) for x in out.split(',') if x.strip().isdigit()]
                                if len(parts) == 4:
                                    return (parts[0], parts[1], parts[2]-parts[0], parts[3]-parts[1])
                        except Exception:
                            pass
                    elif sys.platform.startswith('win32'):
                        subprocess.Popen(['start', 'Discord'], shell=True)
                    else:
                        subprocess.Popen(['discord'])
                except Exception:
                    pass
                time.sleep(0.6)
                # retry pygetwindow activation after attempting to start/activate
                try:
                    if gw is not None:
                        titles = gw.getAllTitles()
                        for t in titles:
                            # Match actual Discord window
                            if t and (t.lower().endswith('- discord') or t.lower() == 'discord'):
                                try:
                                    # Windows: use getWindowsWithTitle
                                    wins = gw.getWindowsWithTitle(t)
                                    for w in wins:
                                        if hasattr(w, 'left') and hasattr(w, 'width'):
                                            try:
                                                if hasattr(w, 'activate'):
                                                    w.activate()
                                            except Exception:
                                                pass
                                            return (int(w.left), int(w.top), int(w.width), int(w.height))
                                except Exception:
                                    # macOS fallback
                                    try:
                                        geom = gw.getWindowGeometry(t)
                                        if geom:
                                            left, top, width, height = (int(geom[0]), int(geom[1]), int(geom[2]), int(geom[3]))
                                            return (left, top, width, height)
                                    except Exception:
                                        pass
                                    continue
                except Exception:
                    pass
                break
    except Exception:
        pass

    return None


def _get_foreground_app_name_on_macos():
    """Return the frontmost application name using AppleScript, or None."""
    try:
        out = subprocess.run(["osascript", "-e", 'tell application "System Events" to name of first application process whose frontmost is true'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        name = out.stdout.strip()
        return name or None
    except Exception:
        return None


def _get_foreground_window_info_windows():
    """Return (title, (left,top,right,bottom)) for the foreground window on Windows, or (None, None).

    Uses Win32 GetForegroundWindow and GetWindowTextW.
    """
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None, None
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd) + 1
        buf = ctypes.create_unicode_buffer(length)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length)
        title = buf.value
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        rect = RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return title, (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        return None, None


def _get_foreground_window_info_linux():
    """Return (title, (left, top, right, bottom)) using xdotool or wmctrl, or (None, None)."""
    try:
        out = subprocess.run(["xdotool", "getactivewindow", "getwindowname"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        title = out.stdout.strip()
        geom = subprocess.run(["xdotool", "getactivewindow", "getwindowgeometry", "--shell"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if 'X:' in geom.stdout:
            # Parse relevant geometry values
            left = 0
            top = 0
            for line in geom.stdout.splitlines():
                if line.startswith('X='):
                    left = int(line.split('=')[1])
                if line.startswith('Y='):
                    top = int(line.split('=')[1])
                if line.startswith('WIDTH='):
                    w = int(line.split('=')[1])
                if line.startswith('HEIGHT='):
                    h = int(line.split('=')[1])
            return title, (left, top, left + w, top + h)
        return title, None
    except Exception:
        return None, None


def get_foreground_window_info():
    """Return (app_name, title, bbox) for the foreground window in a cross-platform way.

    On macOS this is (app_name, title, bbox). For Windows and Linux returns title and bbox if available; app_name is deduced from title.
    """
    try:
        if sys.platform.startswith('darwin'):
            app_name = _get_foreground_app_name_on_macos()
            # Try to get bounds via osascript as well
            try:
                script = 'tell application "System Events"\n    if exists (process "' + (app_name or '') + '") then\n        tell process "' + (app_name or '') + '"\n            if (count of windows) > 0 then\n                set b to bounds of window 1\n                return b\n            end if\n        end tell\n    end if\nend tell\n'
                proc_as = subprocess.run(["osascript", "-e", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                out = proc_as.stdout.strip()
                parts = [int(x.strip()) for x in out.split(',') if x.strip().lstrip('-').isdigit()]
                if len(parts) == 4:
                    bbox = (parts[0], parts[1], parts[2]-parts[0], parts[3]-parts[1])
                else:
                    bbox = None
            except Exception:
                bbox = None
            # If AppleScript didn't supply bbox, try pygetwindow to read geometry instead
            if not bbox:
                try:
                    import pygetwindow as gw
                    for t in gw.getAllTitles():
                        if t and 'discord' in t.lower():
                            geom = gw.getWindowGeometry(t)
                            if geom:
                                left = int(geom[0]); top = int(geom[1]); width = int(geom[2]); height = int(geom[3])
                                bbox = (left, top, width, height)
                                break
                except Exception:
                    pass
            return app_name, None, bbox
        if sys.platform.startswith('win32'):
            title, rect = _get_foreground_window_info_windows()
            return None, title, rect
        # linux
        title, rect = _get_foreground_window_info_linux()
        return None, title, rect
    except Exception:
        return None, None, None


def is_discord_foreground() -> bool:
    """Return True if the foreground window or frontmost application is Discord."""
    try:
        app, title, bbox = get_foreground_window_info()
        if app and 'discord' in (app or '').lower():
            return True
        # Check for "- Discord" suffix to avoid matching VS Code with Discord files open
        if title and title.lower().endswith('- discord'):
            return True
    except Exception:
        pass
    return False


def find_discord():
    """
    Main function to find or run Discord.
    Attempts to bring Discord to the foreground if it's already running,
    or runs it if it's not. Then confirms the window is successfully brought to the foreground.
    """
    # Try to detect an existing Discord window and focus it, but do not auto-launch.
    # This avoids repeatedly stealing focus when the user switches back to the editor.
    window_position = find_and_focus_discord()
    if window_position:
        print("Discord window found and focused.")
        return True

    # Try a couple of quick retries (short wait) in case the user is switching apps.
    for attempt in range(3):
        time.sleep(0.6)
        window_position = find_and_focus_discord()
        if window_position:
            print("Discord window found and focused.")
            return True

    # If still not found, avoid launching or aggressive retries; instruct the caller/user.
    print("Discord window not found. Please make sure Discord is running and focused, then re-run the capture.")
    return False

def read_screen_with_tesseract():
    # Capture the entire screen
    screenshot = ImageGrab.grab()
    screenshot.save("screenshot.png")  # Optionally save the screenshot for debugging

    # Use Tesseract to read text from the screenshot
    text = pytesseract.image_to_string(screenshot)
    print(text)


def find_channel_position(keywords):
    """Attempt to locate a channel in the left sidebar using OCR.

    Returns center (x, y) tuple if found, else None.
    """
    if not _HAS_PIL:
        return None

    def normalize_text(s: str) -> str:
        """Normalize OCR text to a canonical channel-like name.

        - lowercases
        - removes control chars and diacritics
        - removes punctuation except dashes/underscores
        - collapses whitespace
        """
        if not s:
            return ""
        # Normalize unicode and remove diacritics
        s = unicodedata.normalize('NFKD', s)
        s = ''.join(ch for ch in s if not unicodedata.category(ch).startswith('M'))
        # Lowercase
        s = s.lower()
        # Replace punctuation (keep - and _)
        s = re.sub(r"[^a-z0-9\-\_\s]", ' ', s)
        # Collapse whitespace
        s = re.sub(r"\s+", ' ', s).strip()
        return s
    # Prefer capturing the Discord window area if we can find it; this avoids
    # problems when Discord is not full-screen or is positioned away from the
    # left edge of the primary display.
    window_bbox = None
    try:
        window_bbox = find_and_focus_discord()
    except Exception:
        window_bbox = None

    try:
        if window_bbox:
            left, top, width, height = window_bbox
            # capture only the Discord window region
            screenshot = ImageGrab.grab(bbox=(left, top, left + width, top + height))
            offset_x, offset_y = left, top
            w, h = width, height
        else:
            screenshot = ImageGrab.grab()
            offset_x, offset_y = 0, 0
            w, h = screenshot.size
    except Exception:
        return None

    # crop left sidebar (approx 25% width of the captured region)
    sidebar = screenshot.crop((0, 0, int(w * 0.25), h))

    if _HAS_PYTESSERACT and pytesseract:
        try:
            # Use pytesseract to get positional data
            try:
                Output = getattr(pytesseract, 'Output')
            except Exception:
                Output = None
            if Output is not None:
                data = pytesseract.image_to_data(sidebar, output_type=Output.DICT)
            else:
                data = {}
        except Exception:
            # Fallback to simple string search
            text = pytesseract.image_to_string(sidebar)
            norm = normalize_text(text)
            for k in keywords:
                if k.lower() in (norm or ""):
                    # return a rough click point in the sidebar and the matched normalized name
                    return ((offset_x + int(w * 0.05), offset_y + int(h * 0.1)), normalize_text(k), 0.6)
        else:
            texts = data.get('text', [])
            # Examine OCR words and compute confidence where possible
            best = None
            best_conf = -1.0
            for i, t in enumerate(texts):
                txt = (t or '').strip()
                ntxt = normalize_text(txt)
                for k in keywords:
                    if k.lower() in ntxt:
                        lx = data.get('left', [0])[i]
                        ty = data.get('top', [0])[i]
                        wd = data.get('width', [0])[i]
                        ht = data.get('height', [0])[i]
                        # get confidence if available
                        confs = data.get('conf', [])
                        try:
                            conf = float(confs[i]) if i < len(confs) and confs[i].isdigit() else 50.0
                        except Exception:
                            conf = 50.0
                        if conf > best_conf:
                            best_conf = conf
                            # convert coordinates relative to the sidebar image to absolute screen coords
                            abs_x = offset_x + lx + (wd // 2)
                            abs_y = offset_y + ty + (ht // 2)
                            best = ((abs_x, abs_y), ntxt, conf / 100.0)
            if best:
                return best

    return None


def normalize_ocr_name(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    s = unicodedata.normalize('NFKD', s)
    s = re.sub(r"\s+", ' ', s)
    # Remove common decorative symbols/emojis not part of server names
    s = re.sub(r"[\u2600-\u26FF\u2700-\u27BF\U0001F300-\U0001F6FF\U0001F900-\U0001F9FF]", '', s)
    # Remove surrounding punctuation introduced by OCR
    s = re.sub(r"^[^A-Za-z0-9#@]+|[^A-Za-z0-9]+$", '', s)
    return s


def reverse_polarity_if_needed(img, dark_thresh=110, bright_pixel_ratio=0.005):
    """Detect if an image is dark-theme (light text over dark bg) and invert it.

    Returns (image, inverted) where `inverted` is True if the image was inverted.
    """
    if img is None or not _HAS_PIL:
        return img, False
    try:
        gray = img.convert('L')
        stat = ImageStat.Stat(gray)
        mean = stat.mean[0] if stat.mean else 0
    except Exception:
        return img, False

    if mean >= dark_thresh:
        return img, False

    # quick downsample scan for bright pixels
    try:
        w, h = gray.size
        scan = gray.resize((max(16, w // 16), max(8, h // 16)))
        px = list(scan.getdata())
        # threshold relative to mean to catch anti-aliased or faint white text
        threshold = mean + 30
        bright = sum(1 for p in px if p >= threshold)
        ratio = bright / max(1, len(px))
        if ratio < bright_pixel_ratio:
            return img, False
    except Exception:
        # if scanning fails, optimistically invert
        pass

    try:
        from PIL import ImageOps
        out = ImageOps.invert(img.convert('RGB'))
        return out, True
    except Exception:
        return img, False


def _pil_clahe(img):
    """Apply CLAHE-like local contrast via OpenCV if available, else return original image.

    Returns a PIL Image.
    """
    try:
        import cv2
        import numpy as np
    except Exception:
        return img
    try:
        # convert PIL -> gray numpy
        gray = img.convert('L')
        arr = np.array(gray)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        out = clahe.apply(arr)
        return Image.fromarray(out)
    except Exception:
        return img


def _add_padding(img, pad=10, fill=255):
    try:
        from PIL import ImageOps
        return ImageOps.expand(img, border=pad, fill=fill)
    except Exception:
        return img


def _average_confidence_from_data(data):
    """Return the average confidence from pytesseract image_to_data result dict.

    If per-word confidences are present, compute the average of valid confidences.
    """
    if not data:
        return 0.0
    try:
        confs = data.get('conf', [])
        vals = []
        for c in confs:
            try:
                # Some versions return strings like '-1'
                v = float(c)
            except Exception:
                continue
            # Skip invalid/confidence placeholders
            if v >= 0:
                vals.append(v)
        return (sum(vals) / len(vals)) if vals else 0.0
    except Exception:
        return 0.0


def ocr_image_to_text(img, debug: bool = False):
    """Return the best-effort OCR text from an image using multiple pre-processing steps.

    Quick-fixes implemented:
    - Add padding to avoid clipped leading characters
    - Upscale small crops (x2-x3) with Lanczos for better DPI
    - Try CLAHE (OpenCV) if available
    - Try both original and inverted polarity
    - Run multiple preprocess pipelines and select result with highest confidence from pytesseract
    - Optional `debug=True` writes candidate images and scores to `data/debug/ocr_*`
    """
    if img is None or not _HAS_PYTESSERACT or not pytesseract:
        return ""
    try:
        # Try direct OCR first with small padding and conservative whitelist
        probe = _add_padding(img, pad=8)
        cfg = '--psm 7 --oem 3 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#-_@/:. &'
        txt = pytesseract.image_to_string(probe, config=cfg)
        if txt and txt.strip():
            return txt.strip()
    except Exception:
        txt = ""
    try:
        # Convert to grayscale, resize, and increase contrast
        tmp = img.convert('L')
        # Detect dark-theme screenshots (white text on dark background)
        # and invert polarity to improve OCR accuracy (e.g. Discord dark theme)
        try:
            alt_img, inverted = reverse_polarity_if_needed(img)
        except Exception:
            alt_img, inverted = img, False
        # Build a set of preprocessing variants to evaluate
        variants = []

        # baseline scaled (3x) with Lanczos
        w, h = tmp.size
        scaled = tmp.resize((max(64, w * 3), max(32, h * 3)), resample=Image.LANCZOS)
        variants.append(('scaled', scaled))

        # padded + scaled
        padded = _add_padding(scaled, pad=10, fill=255)
        variants.append(('padded', padded))

        # CLAHE variant (if available)
        try:
            clahe_img = _pil_clahe(scaled)
            variants.append(('clahe', clahe_img))
        except Exception:
            pass

        # Autocontrast variant
        try:
            from PIL import ImageOps
            ac = ImageOps.autocontrast(scaled, cutoff=2)
            variants.append(('autocontrast', ac))
        except Exception:
            pass

        # Inverted variants (if polarity suggested inversion or not)
        try:
            from PIL import ImageOps
            # always include inverted forms to be robust
            inv_scaled = ImageOps.invert(scaled.convert('RGB')).convert('L')
            variants.append(('inv_scaled', inv_scaled))
            inv_padded = ImageOps.invert(padded.convert('RGB')).convert('L')
            variants.append(('inv_padded', inv_padded))
        except Exception:
            pass
        # apply a light contrast boost on each variant
        try:
            from PIL import ImageEnhance
            enriched = []
            for name, v in variants:
                try:
                    enhancer = ImageEnhance.Contrast(v)
                    v2 = enhancer.enhance(1.6)
                except Exception:
                    v2 = v
                enriched.append((name, v2))
            variants = enriched
        except Exception:
            pass

        # we've already applied an inversion if it was helpful; continue
        # try different psm settings for robustness
        # Try several psm modes and select best word using image_to_data (highest confidence)
        # Evaluate each variant using image_to_data and pick the highest average confidence
        best_score = -1.0
        best_text = ''
        o_output = getattr(pytesseract, 'Output', None)
        for name, v in variants:
            for cfg in ('--psm 7 --oem 3', '--psm 6 --oem 3'):
                try:
                    out_type = o_output.DICT if o_output else None
                    data = pytesseract.image_to_data(v, output_type=out_type, config=cfg)
                except Exception:
                    # fallback to plain string and a low pseudo-confidence
                    try:
                        txt = pytesseract.image_to_string(v, config='--psm 7') or ''
                        score = len(txt) * 5.0
                        if score > best_score:
                            best_score = score
                            best_text = txt.strip()
                    except Exception:
                        continue
                    continue

                # Calculate average confidence for this call
                score = _average_confidence_from_data(data)
                # Favour higher numeric score and non-empty text
                txts = data.get('text', []) if isinstance(data, dict) else []
                txt_combined = ' '.join([t for t in txts if t and t.strip()])
                if txt_combined and score >= best_score:
                    best_score = score
                    best_text = txt_combined.strip()

                # Optional debug save per-variant
                if debug:
                    try:
                        import json, time
                        debug_dir = os.path.join('data', 'debug')
                        os.makedirs(debug_dir, exist_ok=True)
                        ts = int(time.time() * 1000)
                        fname = f'ocr_{ts}_{name}_{int(score)}.png'
                        v.convert('RGB').save(os.path.join(debug_dir, fname))
                        meta = {'variant': name, 'score': score, 'cfg': cfg}
                        with open(os.path.join(debug_dir, f'ocr_{ts}_{name}_{int(score)}.json'), 'w') as f:
                            json.dump(meta, f)
                    except Exception:
                        pass

        if best_text and best_text.strip():
            return best_text.strip()
        # If still nothing, try an inverted variant
        try:
            inv = ImageOps.invert(ac)
            for cfg in ('--psm 7', '--psm 6'):
                try:
                    txt3 = pytesseract.image_to_string(inv, config=cfg)
                    if txt3 and txt3.strip():
                        return txt3.strip()
                except Exception:
                    pass
        except Exception:
            pass
    except Exception:
        pass
    return ""




# NOTE: capture_discord_servers has been moved to src/discord_nav.py as iterate_all_servers()
# Use: from src.discord_nav import iterate_all_servers


if __name__ == "__main__":
    check_tesseract_path()
    discord_window = find_discord()
    if discord_window:
        print("Discord window found and focused.")
    else:
        print("Discord window not found. Please ensure Discord is running and not minimized.")
    read_screen_with_tesseract()

