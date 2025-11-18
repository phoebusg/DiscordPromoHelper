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
        if (app_name and 'discord' in (app_name or '').lower()) or (title and 'discord' in (title or '').lower()):
            # Return bbox if we have it, else attempt to find via pygetwindow
            if bbox:
                try:
                    l, t, r, b = bbox
                    return (l, t, r - l, b - t)
                except Exception:
                    pass
    except Exception:
        pass

    # Try pygetwindow first
    if gw is not None:
        try:
            titles = gw.getAllTitles()
            for t in titles:
                if not t:
                    continue
                if 'discord' in t.lower():
                    try:
                        geom = gw.getWindowGeometry(t)
                        # Try to activate the window (pygetwindow provides `activate` convenience)
                        try:
                            w = gw.Window(t)
                            w.activate()
                        except Exception:
                            pass
                        # Return left, top, width, height
                        if geom:
                            left, top, width, height = (int(geom[0]), int(geom[1]), int(geom[2]), int(geom[3]))
                            return (left, top, width, height)
                    except Exception:
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
                            if t and 'discord' in t.lower():
                                try:
                                    geom = gw.getWindowGeometry(t)
                                    if geom:
                                        left, top, width, height = (int(geom[0]), int(geom[1]), int(geom[2]), int(geom[3]))
                                        try:
                                            w = gw.Window(t)
                                            w.activate()
                                        except Exception:
                                            pass
                                        return (left, top, width, height)
                                except Exception:
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
        if title and 'discord' in (title or '').lower():
            return True
    except Exception:
        pass
    return False

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
        if (app_name and 'discord' in (app_name or '').lower()) or (title and 'discord' in (title or '').lower()):
            # Return bbox if we have it, else attempt to find via pygetwindow
            if bbox:
                try:
                    l, t, r, b = bbox
                    return (l, t, r - l, b - t)
                except Exception:
                    pass
    except Exception:
        pass

    # Try pygetwindow first
    if gw is not None:
        try:
            titles = gw.getAllTitles()
            for t in titles:
                if not t:
                    continue
                if 'discord' in t.lower():
                    try:
                        geom = gw.getWindowGeometry(t)
                        # Try to activate the window (pygetwindow provides `activate` convenience)
                        try:
                            w = gw.Window(t)
                            w.activate()
                        except Exception:
                            pass
                        # Return left, top, width, height
                        if geom:
                            left, top, width, height = (int(geom[0]), int(geom[1]), int(geom[2]), int(geom[3]))
                            return (left, top, width, height)
                    except Exception:
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
                        wins = gw.getWindowsWithTitle('Discord') or gw.getWindowsWithTitle('discord')
                        for w in wins:
                            try:
                                if not w.isMinimized:
                                    try:
                                        w.activate()
                                    except Exception:
                                        pass
                                    return (w.left, w.top, w.width, w.height)
                            except Exception:
                                continue
                except Exception:
                    pass
                break
    except Exception:
        pass

    return None

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


def ocr_image_to_text(img):
    """Return the best-effort OCR text from an image using multiple pre-processing steps."""
    if img is None or not _HAS_PYTESSERACT or not pytesseract:
        return ""
    try:
        # Try direct OCR first with conservative whitelist
        cfg = '--psm 7 --oem 3 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#-_@/:. &'
        txt = pytesseract.image_to_string(img, config=cfg)
        if txt and txt.strip():
            return txt.strip()
    except Exception:
        txt = ""
    try:
        # Convert to grayscale, resize, and increase contrast
        tmp = img.convert('L')
        w, h = tmp.size
        tmp = tmp.resize((max(32, w * 3), max(32, h * 3)), resample=Image.BILINEAR)
        try:
            from PIL import ImageEnhance, ImageOps
            enhancer = ImageEnhance.Contrast(tmp)
            tmp = enhancer.enhance(1.8)
            # Try auto-contrast as well
            ac = ImageOps.autocontrast(tmp)
        except Exception:
            ac = tmp
        # try different psm settings for robustness
        # Try several psm modes and select best word using image_to_data (highest confidence)
        for cfg in ('--psm 7', '--psm 6'):
            try:
                data = pytesseract.image_to_data(ac, output_type=getattr(pytesseract, 'Output', {}).DICT if hasattr(pytesseract, 'Output') else None, config=cfg)
                # data may be a dict-like output; prefer the element with highest confidence
                try:
                    texts = data.get('text', [])
                    confs = data.get('conf', [])
                    best_idx = None
                    best_conf = -999
                    for i, t in enumerate(texts):
                        if not t or not t.strip():
                            continue
                        try:
                            c = float(confs[i]) if i < len(confs) else 0
                        except Exception:
                            c = 0
                        if c > best_conf:
                            best_conf = c
                            best_idx = i
                    if best_idx is not None and texts[best_idx].strip():
                        return texts[best_idx].strip()
                except Exception:
                    pass
            except Exception:
                pass
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


def capture_discord_servers(save_dir: str = "data/servers", hover_delay: float = 0.6, merge_gap: int = 8,
                            max_scrolls: int = 12, scroll_amount: int = 300, duplicate_thresh: int = 4000,
                            max_icon_retries: int = 120,
                            wait_for_focus: bool = False,
                            wait_timeout_seconds: int = 30,
                            allow_fullscreen_fallback: bool = False,
                            start_from_top: bool = False,
                            top_skip_px: int = 48,
                            bottom_skip_px: int = 64,
                            debug_save_hover: bool = False):
    """Scan Discord's server list, capture each server icon and hover to read its name.

    Saves PNGs into `save_dir/images` and metadata into `save_dir/servers.json`.

    Returns a list of dicts: [{"name": ..., "icon": "path/to/png", "pos": (x,y)}...]
    """
    if not _HAS_PIL:
        raise RuntimeError("Pillow not available for screenshots")
    p = Path(save_dir)
    imgs = p / "images"
    imgs.mkdir(parents=True, exist_ok=True)
    hover_debug_dir = p / "hover_debug"
    if debug_save_hover:
        hover_debug_dir.mkdir(parents=True, exist_ok=True)

    # populate seen_thumbs from existing saved icons so we don't re-add previous runs
    # store pairs of (thumb_image, icon_path) for matching and updating existing entries
    seen_thumbs = []  # list of (thumb_img, icon_path)
    try:
        for existing_path in sorted(imgs.glob('server_*.png')):
            try:
                ex = Image.open(existing_path).convert('L').resize((32, 32), resample=Image.BILINEAR)
                seen_thumbs.append((ex, str(existing_path)))
            except Exception:
                continue
    except Exception:
        seen_thumbs = []

    # simplified mode: debug screenshots disabled

    # Add a stop sentinel file and optional keyboard listener (if available)
    stop_file = p / ".STOP"
    stop_requested = False
    try:
        import importlib
        import threading
        # Prefer pynput for global hotkey support (cross-platform). Use importlib so Pylance doesn't statically flag missing module.
        _pynput_spec = importlib.util.find_spec('pynput')
        if _pynput_spec is not None:
            try:
                _pynput_keyboard = importlib.import_module('pynput.keyboard')
            except Exception:
                _pynput_keyboard = None
        else:
            _pynput_keyboard = None
        keyboard_available = bool(_pynput_keyboard)
        _keyboard_listener = None
    except Exception:
        _pynput_keyboard = None
        _keyboard_listener = None
        keyboard_available = False

    def _set_stop():
        nonlocal stop_requested
        stop_requested = True
        print("Stop requested (keyboard shortcut or stop file). Will exit cleanly soon.")

    # Ensure pynput is installed (try to auto-install into the active venv)
    if not keyboard_available:
        try:
            print('pynput not installed; attempting to install to enable ESC hotkey...')
            subprocess.run([sys.executable, '-m', 'pip', 'install', 'pynput'], check=True)
            _pynput_keyboard = importlib.import_module('pynput.keyboard')
            keyboard_available = True
        except Exception:
            keyboard_available = False
            _pynput_keyboard = None
            _keyboard_listener = None

    # Register global hotkey if `pynput` is available
    if keyboard_available and _pynput_keyboard is not None:
        try:
            def _on_press(key):
                try:
                    if key == _pynput_keyboard.Key.esc:
                        _set_stop()
                        return False
                except Exception:
                    pass
            _keyboard_listener = _pynput_keyboard.Listener(on_press=_on_press)
            _keyboard_listener.daemon = True
            _keyboard_listener.start()
        except Exception:
            _keyboard_listener = None
    if keyboard_available and _keyboard_listener is None:
        print('ESC hotkey enabled via pynput')
    elif not keyboard_available:
        print('ESC hotkey not available; use sentinel file or Ctrl-C')

    def should_stop_scan():
        nonlocal stop_requested
        if stop_requested:
            return True
        try:
            if stop_file.exists():
                return True
        except Exception:
            pass
        return False

    # Ensure Discord is active and get its bbox. Retry a few times if necessary.
    bbox = None
    # If requested, pause and let the user focus Discord manually.
    if wait_for_focus:
        # Wait for the user to focus Discord (polling) up to wait_timeout_seconds.
        waited = 0.0
        poll = 0.6
        print(f"Waiting up to {wait_timeout_seconds}s for Discord to be focused...")
        while waited < wait_timeout_seconds and not bbox:
            try:
                bbox = find_and_focus_discord()
            except Exception:
                bbox = None
            if bbox:
                break
            time.sleep(poll)
            waited += poll

    # final quick attempts if not found yet
    attempts = 0
    while attempts < 3 and not bbox:
        try:
            bbox = find_and_focus_discord()
        except Exception:
            bbox = None
        if not bbox:
            time.sleep(0.6)
        attempts += 1

    if not bbox:
        if allow_fullscreen_fallback:
            try:
                full = ImageGrab.grab()
                sw, sh = full.size
                left, top = 0, 0
                width, height = sw, sh
                print("Warning: Discord window bbox not found — using full screen fallback.")
            except Exception:
                raise RuntimeError("Discord window not found or could not be focused")
        else:
            raise RuntimeError("Discord window not found or could not be focused. Ensure Discord is visible and the left server sidebar is open; retry with wait_for_focus=True if needed.")
    else:
        left, top, width, height = bbox

    # Quick sanity check: verify the active-server indicator (thin white line)
    # exists in the server column; if not, give Discord one more chance to focus.
    col_w = max(40, int(width * 0.06))
    try:
        col_img_check = ImageGrab.grab(bbox=(left, top, left + col_w, top + height))
        gray_check = col_img_check.convert('L')
        wc, hc = gray_check.size
        pixels = list(gray_check.getdata())
        found_indicator = False
        # Robust sanity check: compute brightness variance in the column
        # Discord's sidebar contains icons and indicators; low variance implies nothing useful.
        mean = sum(pixels) / len(pixels) if pixels else 0
        var = sum((pv - mean) ** 2 for pv in pixels) / len(pixels) if pixels else 0
        stddev = var ** 0.5
        if stddev > 6.0:
            found_indicator = True
        else:
            # fallback: check for near-white pixels in the leftmost few columns (older heuristic)
            for x in range(min(6, wc)):
                col_count = 0
                for y in range(hc):
                    if pixels[y * wc + x] >= 240:
                        col_count += 1
                if col_count > max(2, int(hc * 0.02)):
                    found_indicator = True
                    break
        if not found_indicator:
            # give the user one quick chance to re-focus and retry
            try:
                time.sleep(0.6)
                bbox = find_and_focus_discord() or bbox
            except Exception:
                pass
    except Exception:
        pass

    # server column approximated as left-most ~6% of the window width
    col_w = max(40, int(width * 0.06))
    col_box = (left, top, left + col_w, top + height)
    # Compute a safe scroll Y inside the server column (avoid hovering top UI)
    def _get_safe_scroll_y():
        y = top + (height // 2)
        y = max(y, top + top_skip_px + 12)
        y = min(y, top + height - bottom_skip_px - 12)
        return int(round(y))

    # Move the cursor to a safe position inside the server column to ensure scroll events target the column
    try:
        if _HAS_PYAUTOGUI and pyautogui:
            cx = left + (col_w // 2)
            safe_y = _get_safe_scroll_y()
            pyautogui.moveTo(cx, safe_y, duration=0.12)
            time.sleep(0.08)
    except Exception:
        pass

    # Helper to hover and read a tooltip at an absolute center position
    def _hover_and_read(cx, cy):
        if not (_HAS_PYAUTOGUI and pyautogui and _HAS_PYTESSERACT and pytesseract):
            return ""
        try:
            pyautogui.moveTo(cx, cy, duration=0.12)
            time.sleep(max(0.32, hover_delay))
        except Exception:
            pass
        # Try multiple small offsets to force tooltip to appear (some themes
        # require slight horizontal movement)
        offsets = [(0, 0), (-6, 0), (6, 0), (-10, 0), (10, 0)]
        candidate_boxes = [
            (cx + 15, cy - 30, cx + 200, cy + 10),
            (cx + 8, cy - 15, cx + 130, cy + 15),
            (cx + 15, cy - 60, cx + 300, cy + 10),
            (cx - 60, cy - 45, cx + 90, cy - 5),
            (cx + 10, cy - 40, cx + 220, cy + 10),
        ]
        for dx, dy in offsets:
            try:
                if dx or dy:
                    pyautogui.moveRel(dx, dy, duration=0.08)
                    time.sleep(0.08)
            except Exception:
                pass
            for tb in candidate_boxes:
                try:
                    tip_img = _safe_grab(tb)
                except Exception:
                    tip_img = None
                if tip_img is not None:
                    txt = ocr_image_to_text(tip_img)
                    if txt and txt.strip():
                        if debug_save_hover:
                            try:
                                tfn = hover_debug_dir / f"hover_{int(time.time() * 1000)}_{cx}_{cy}.png"
                                tip_img.save(tfn)
                            except Exception:
                                pass
                        return txt.strip()
        return ""

    # safe wrapper for ImageGrab.grab with timeout (prevents hangs on macOS with missing permissions)
    def _safe_grab(bbox=None, timeout_sec: float = 1.2):
        try:
            import platform
            if platform.system() == 'Darwin':
                # Use macOS screencapture CLI with region (-R "x,y,w,h") to avoid PIL hanging
                x = bbox[0] if bbox else 0
                y = bbox[1] if bbox else 0
                w = bbox[2] - bbox[0] if bbox else 1
                h = bbox[3] - bbox[1] if bbox else 1
                import tempfile, subprocess, time, os
                tmp = Path(tempfile.gettempdir()) / f"pycap_{os.getpid()}_{int(time.time() * 1000)}.png"
                cmd = ['screencapture', '-x', '-R', f"{x},{y},{w},{h}", str(tmp)]
                try:
                    subprocess.run(cmd, timeout=timeout_sec, check=True)
                    from PIL import Image as PILImage
                    im = PILImage.open(str(tmp))
                    return im
                except Exception:
                    try:
                        if tmp.exists():
                            tmp.unlink()
                    except Exception:
                        pass
                    return None
            else:
                try:
                    return ImageGrab.grab(bbox=bbox)
                except Exception:
                    return None
        except Exception:
            try:
                return ImageGrab.grab(bbox=bbox)
            except Exception:
                return None

    def _is_icon_at(cx, cy, size=28, var_threshold=8.0):
        """Return True if the square area centered at (cx,cy) looks like a server icon.

        This performs a quick variance / contrast check to avoid hovering over empty areas.
        """
        if not _HAS_PIL:
            return True
        try:
            bbox = (cx - size // 2, cy - size // 2, cx + size // 2, cy + size // 2)
            im = _safe_grab(bbox)
            if im is None:
                return True
            im = im.convert('L')
            px = list(im.getdata())
            if not px:
                return False
            mean = sum(px) / len(px)
            var = sum((p - mean) ** 2 for p in px) / len(px)
            stddev = var ** 0.5
            if stddev < var_threshold:
                return False
        except Exception:
            return True
        return True

    # Print stop instructions
    try:
        if keyboard_available:
            print('Scanning started — press ESC to stop, or run scripts/stop_scan.py to create sentinel file to stop')
        else:
            print('Scanning started — run scripts/stop_scan.py to create sentinel file to stop')
    except Exception:
        pass
    # Remove any leftover stop sentinel so a stray file doesn't cancel the run
    try:
        if stop_file.exists():
            stop_file.unlink()
            print('Removed leftover stop sentinel to start a clean scan')
    except Exception:
        pass

    # Load existing metadata into server_list to avoid re-adding duplicates and to preserve entries
    server_list = []
    meta_path = p / "servers.json"
    try:
        if meta_path.exists():
            with open(meta_path, 'r', encoding='utf-8') as fh:
                server_list = json.load(fh)
    except Exception:
        server_list = []
    known_names = set(s['name'] for s in server_list if s.get('name'))
    # Do not prepopulate server_list from existing images; only use servers.json if present.
    processed_centers = set()

    def _save_meta():
        try:
            with open(p / "servers.json", 'w', encoding='utf-8') as fh:
                json.dump(server_list, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _ensure_record_for_icon(icon_path: str, center_x: int, center_y: int, name: str = ""):
        """Ensure server_list contains an entry for `icon_path`. Update pos/name if found, else append a new record."""
        nonlocal server_list, known_names
        try:
            for s in server_list:
                if s.get('icon') == icon_path:
                    if s.get('pos') is None and center_x is not None:
                        s['pos'] = (center_x, center_y)
                    if not s.get('name') and name:
                        s['name'] = name
                    return
            # not found; append
            rec = {"name": name or "", "icon": str(icon_path), "pos": (center_x, center_y) if center_x is not None else None}
            server_list.append(rec)
            if name:
                known_names.add(name)
            _save_meta()
        except Exception:
            pass

    def _save_viewport_history(history):
        try:
            with open(p / "viewport_history.json", 'w', encoding='utf-8') as fh:
                json.dump(history, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass
    try:
        from PIL import ImageChops
    except Exception:
        ImageChops = None

    # nested helper to scan the current viewport and process icon regions
    def _scan_viewport():
        nonlocal server_list, seen_thumbs, known_names
        added = False
        if should_stop_scan():
            return False, []
        col_img_local = _safe_grab(col_box)
        if col_img_local is None:
            return False
        gray = col_img_local.convert('L')
        w, h = gray.size
        arr = list(gray.getdata())

        # compute vertical projection: sum of (255 - pixel) per row
        proj = [0] * h
        for y in range(h):
            row_sum = 0
            offset = y * w
            for x in range(w):
                row_sum += 255 - arr[offset + x]
            proj[y] = row_sum

        maxp = max(proj) if proj else 0
        thresh = max(8, int(maxp * 0.12))

        regions = []
        in_region = False
        start = 0
        for y, v in enumerate(proj):
            if v >= thresh and not in_region:
                in_region = True
                start = y
            elif v < thresh and in_region:
                end = y
                regions.append((start, end))
                in_region = False
        if in_region:
            regions.append((start, h))

        if not regions:
            # sliding window fallback
            win_h = 48
            step = max(24, int(win_h * 0.6))
            regions = [(y0, y0 + win_h) for y0 in range(0, max(1, h - win_h + 1), step)]

        # merge regions
        merged_local = []
        for r in regions:
            if not merged_local:
                merged_local.append(r)
                continue
            prev = merged_local[-1]
            if r[0] - prev[1] <= merge_gap:
                merged_local[-1] = (prev[0], r[1])
            else:
                merged_local.append(r)

        centers = []
        icon_h = 56
        for (s, e) in merged_local:
            pad_v = min(28, int((e - s) * 0.35) + 8)
            crop_top = max(0, s - pad_v)
            crop_bot = min(h, e + pad_v)
            crop = col_img_local.crop((0, crop_top, w, crop_bot))

            try:
                thumb = crop.convert('L').resize((32, 32), resample=Image.BILINEAR)
            except Exception:
                thumb = None

            # dedup by thumb
            is_dup = False
            matched_icon_path = None
            if thumb is not None and ImageChops is not None:
                for existing_thumb, existing_icon_path in seen_thumbs:
                    try:
                        d = ImageChops.difference(thumb, existing_thumb)
                        if hasattr(d, 'histogram'):
                            hist = d.histogram()
                            diff_metric = sum(i * v for i, v in enumerate(hist))
                        else:
                            diff_metric = sum(d.getdata())
                        if diff_metric < duplicate_thresh:
                            is_dup = True
                            matched_icon_path = existing_icon_path
                            break
                    except Exception:
                        continue

            center_x = left + (col_w // 2)
            center_y = top + crop_top + ((crop_bot - crop_top) // 2)
            if int(round(center_y)) in processed_centers:
                continue
            centers.append(center_y)
            # compute step/icon height dynamically as centers accumulate
            step = _compute_icon_step(centers)
            icon_h = max(48, min(80, int(round(step))))
            if center_y - top <= top_skip_px:
                continue
            if (top + height - center_y) <= bottom_skip_px:
                continue

            if is_dup:
                try:
                    if matched_icon_path:
                        _ensure_record_for_icon(matched_icon_path, center_x, center_y, name="")
                except Exception:
                    pass
                continue

            # Quick check: ensure there's an icon present at this center to avoid dead zones
            try:
                if not _is_icon_at(center_x, center_y):
                    # mark as processed (dead area) and skip
                    try:
                        processed_centers.add(int(round(center_y)))
                    except Exception:
                        pass
                    continue
            except Exception:
                pass

            # Abort if stop requested
            if should_stop_scan():
                return added, centers
            # Hover and OCR
            name = ""
            try:
                if _HAS_PYAUTOGUI and pyautogui:
                    pyautogui.moveTo(center_x, center_y, duration=0.28)
                    time.sleep(max(hover_delay, 0.9))
                    if should_stop_scan():
                        return added, centers
                    candidate_boxes = [
                        (center_x + 15, center_y - 30, center_x + 200, center_y + 10),
                        (center_x + 8, center_y - 15, center_x + 130, center_y + 15),
                        (center_x + 15, center_y - 60, center_x + 300, center_y + 10),
                        (center_x - 60, center_y - 45, center_x + 90, center_y - 5),
                        (center_x + 10, center_y - 40, center_x + 220, center_y + 10),
                    ]
                    tip_text = ""
                    for tb in candidate_boxes:
                        try:
                                    tip_img = _safe_grab(tb)
                        except Exception:
                            tip_img = None
                        if tip_img is not None and _HAS_PYTESSERACT and pytesseract:
                            try:
                                    cand = ocr_image_to_text(tip_img)
                            except Exception:
                                cand = ""
                            if cand and len(cand.strip()) > 1:
                                tip_text = cand
                                break
                        try:
                            pyautogui.moveRel(0, 4, duration=0.12)
                            time.sleep(0.12)
                            pyautogui.moveRel(0, -4, duration=0.12)
                            time.sleep(0.12)
                        except Exception:
                            pass
                    name = normalize_ocr_name(tip_text)
            except Exception:
                name = ""

            is_name_dup = False
            if name and name in known_names:
                is_name_dup = True
                matched_icon_path = None
                for s in server_list:
                    if s.get('name') == name:
                        matched_icon_path = s.get('icon')
                        break

            idx = len(list(imgs.glob('server_*.png')))
            icon_path = imgs / f"server_{idx}.png"
            try:
                crop.save(icon_path)
            except Exception:
                continue
            if thumb is not None:
                seen_thumbs.append((thumb, str(icon_path)))
            if is_name_dup or is_dup:
                try:
                    if matched_icon_path:
                        for s in server_list:
                            if s.get('icon') == matched_icon_path:
                                if s.get('pos') is None:
                                    s['pos'] = (center_x, center_y)
                                break
                except Exception:
                    pass
                continue
            server_list.append({"name": name, "icon": str(icon_path), "pos": (center_x, center_y)})
            try:
                print(f"Captured server: '{name}' at {center_x},{center_y}")
            except Exception:
                pass
            # simplified mode: no per-capture debug screenshot
            _save_meta()
            if name:
                known_names.add(name)
            added = True
        # return both whether any items were added and the absolute center Y positions
        return added, centers

    def _scan_by_step(icon_step: int = 56):
        """Deterministically step down the visible server column and hover each potential icon center."""
        nonlocal server_list, seen_thumbs, known_names
        added_any = False
        # Determine current centers for page and hover each
        centers = _get_viewport_centers()
        step = _compute_icon_step(centers)
        icon_h = max(48, min(80, int(round(step))))
        added_any = False
        for cy in centers:
            if should_stop_scan():
                return added_any, centers
            if int(round(cy)) in processed_centers:
                continue
            # Skip dead zones by checking for icon presence
            try:
                if not _is_icon_at(left + (col_w // 2), cy):
                    try:
                        processed_centers.add(int(round(cy)))
                    except Exception:
                        pass
                    continue
            except Exception:
                pass
            # hover at center
            cx = left + (col_w // 2)
            try:
                if _HAS_PYAUTOGUI and pyautogui:
                    pyautogui.moveTo(cx, cy, duration=0.18)
                    time.sleep(max(hover_delay, 0.8))
                    if should_stop_scan():
                        return added_any, centers
            except Exception:
                pass
            # Hover at (cx, cy)
            try:
                if _HAS_PYAUTOGUI and pyautogui:
                    pyautogui.moveTo(cx, cy, duration=0.18)
                    time.sleep(max(hover_delay, 0.8))
            except Exception:
                pass
            # capture icon area locally
            try:
                bbox = _get_safe_crop_bbox(cx, cy, icon_h)
                crop = _safe_grab(bbox)
            except Exception:
                # skip this center if we couldn't capture it
                continue
            try:
                thumb = crop.convert('L').resize((32, 32), resample=Image.BILINEAR)
            except Exception:
                thumb = None
            is_dup = False
            matched_icon_path = None
            if thumb is not None and ImageChops is not None:
                for existing_thumb, existing_icon_path in seen_thumbs:
                    try:
                        d = ImageChops.difference(thumb, existing_thumb)
                        if hasattr(d, 'histogram'):
                            hist = d.histogram()
                            diff_metric = sum(i * v for i, v in enumerate(hist))
                        else:
                            diff_metric = sum(d.getdata())
                        if diff_metric < duplicate_thresh:
                            is_dup = True
                            matched_icon_path = existing_icon_path
                            break
                    except Exception:
                        continue
            # compute center and skip UI
            center_x = cx
            center_y = cy
            if center_y - top <= top_skip_px:
                # skip header area
                continue
            if (top + height - center_y) <= bottom_skip_px:
                # skip bottom area
                continue
            # OCR Tooltip
            name = ""
            try:
                candidate_boxes = [
                    (center_x + 15, center_y - 30, center_x + 200, center_y + 10),
                    (center_x + 8, center_y - 15, center_x + 130, center_y + 15),
                    (center_x + 15, center_y - 60, center_x + 300, center_y + 10),
                    (center_x - 60, center_y - 45, center_x + 90, center_y - 5),
                    (center_x + 10, center_y - 40, center_x + 220, center_y + 10),
                ]
                for tb in candidate_boxes:
                    try:
                        tip_img = _safe_grab(tb)
                    except Exception:
                        tip_img = None
                    if tip_img is not None:
                        txt = ocr_image_to_text(tip_img)
                        if txt and txt.strip():
                            name = normalize_ocr_name(txt)
                            break
            except Exception:
                name = ""
            if name and name in known_names:
                is_dup = True
                matched_icon_path = None
                for s in server_list:
                    if s.get('name') == name:
                        matched_icon_path = s.get('icon')
                        break
            if is_dup:
                # update existing record
                if matched_icon_path:
                    _ensure_record_for_icon(matched_icon_path, center_x, center_y, name=name)
            else:
                # Save icon
                idx = len(list(imgs.glob('server_*.png')))
                icon_path = imgs / f"server_{idx}.png"
                try:
                    crop.save(icon_path)
                except Exception:
                    # couldn't save crop
                    continue
                if thumb is not None:
                    seen_thumbs.append((thumb, str(icon_path)))
                server_list.append({"name": name, "icon": str(icon_path), "pos": (center_x, center_y)})
                try:
                    print(f"Captured server: '{name}' at {center_x},{center_y}")
                except Exception:
                    pass
                _save_meta()
                if name:
                    known_names.add(name)
                added_any = True
        # return added_any and the processed centers
        return added_any, centers

    def process_viewport_centers(direction: str = 'down', icon_step: int = 56):
        """Process centers in the current viewport in the specified direction.

        Returns (added_any, centers)
        """
        nonlocal server_list, seen_thumbs, known_names, processed_centers
        # simplified mode: no per-viewport screenshots
        added_any_local = False
        centers_local = _get_viewport_centers()
        if direction == 'up':
            centers_local = list(reversed(centers_local))
        seen_this_pass = set()
        for cy in centers_local:
            if should_stop_scan():
                return added_any_local, centers_local
            if int(round(cy)) in seen_this_pass:
                continue
            try:
                if not _is_icon_at(left + (col_w // 2), cy):
                    processed_centers.add(int(round(cy)))
                    seen_this_pass.add(int(round(cy)))
                    continue
            except Exception:
                pass
            cx = left + (col_w // 2)
            try:
                if _HAS_PYAUTOGUI and pyautogui:
                    pyautogui.moveTo(cx, cy, duration=0.18)
                    # give more time for tooltip to appear and stabilize
                    time.sleep(max(hover_delay, 0.9))
                    try:
                        pyautogui.moveRel(4, 0, duration=0.06)
                        time.sleep(0.06)
                        pyautogui.moveRel(-4, 0, duration=0.06)
                        time.sleep(0.06)
                    except Exception:
                        pass
            except Exception:
                pass
            # capture
            try:
                step = _compute_icon_step(centers_local)
                icon_h = max(48, min(80, int(round(step))))
                bbox = _get_safe_crop_bbox(cx, cy, icon_h)
                crop = _safe_grab(bbox)
            except Exception:
                processed_centers.add(int(round(cy)))
                seen_this_pass.add(int(round(cy)))
                continue
            try:
                thumb = crop.convert('L').resize((32, 32), resample=Image.BILINEAR)
            except Exception:
                thumb = None
            is_dup = False
            matched_icon_path = None
            if thumb is not None and ImageChops is not None:
                for existing_thumb, existing_icon_path in seen_thumbs:
                    try:
                        d = ImageChops.difference(thumb, existing_thumb)
                        if hasattr(d, 'histogram'):
                            hist = d.histogram()
                            diff_metric = sum(i * v for i, v in enumerate(hist))
                        else:
                            diff_metric = sum(d.getdata())
                        if diff_metric < duplicate_thresh:
                            is_dup = True
                            matched_icon_path = existing_icon_path
                            break
                    except Exception:
                        continue

            # compute center and skip UI
            center_x = cx
            center_y = cy
            if center_y - top <= top_skip_px:
                processed_centers.add(int(round(center_y)))
                seen_this_pass.add(int(round(center_y)))
                continue
            if (top + height - center_y) <= bottom_skip_px:
                processed_centers.add(int(round(center_y)))
                seen_this_pass.add(int(round(center_y)))
                continue

            # OCR Tooltip
            name = ""
            try:
                cand_txt = _hover_and_read(center_x, center_y)
                name = normalize_ocr_name(cand_txt) if cand_txt else ""
            except Exception:
                name = ""

            if name and name in known_names:
                is_dup = True
                matched_icon_path = None
                for s in server_list:
                    if s.get('name') == name:
                        matched_icon_path = s.get('icon')
                        break

            if is_dup:
                try:
                    if matched_icon_path:
                        for s in server_list:
                            if s.get('icon') == matched_icon_path:
                                if s.get('pos') is None:
                                    s['pos'] = (center_x, center_y)
                                if not s.get('name') and name:
                                    s['name'] = name
                                break
                except Exception:
                    pass
                processed_centers.add(int(round(center_y)))
                seen_this_pass.add(int(round(center_y)))
                continue

            # Save icon
            idx = len(list(imgs.glob('server_*.png')))
            icon_path = imgs / f"server_{idx}.png"
            try:
                crop.save(icon_path)
            except Exception:
                processed_centers.add(int(round(center_y)))
                seen_this_pass.add(int(round(center_y)))
                continue
            if thumb is not None:
                seen_thumbs.append((thumb, str(icon_path)))
            server_list.append({"name": name, "icon": str(icon_path), "pos": (center_x, center_y)})
            try:
                print(f"Captured server: '{name}' at {center_x},{center_y}")
            except Exception:
                pass
            _save_meta()
            if name:
                known_names.add(name)
            processed_centers.add(int(round(center_y)))
            seen_this_pass.add(int(round(center_y)))
            added_any_local = True
        # simplified mode: no per-viewport screenshots
        return added_any_local, centers_local

    def _get_viewport_centers():
        """Return absolute Y positions of icon centers in the current viewport without saving or OCR."""
        col_img_local = _safe_grab(col_box)
        if col_img_local is None:
            return []
        gray = col_img_local.convert('L')
        w, h = gray.size
        arr = list(gray.getdata())
        proj = [0] * h
        for y in range(h):
            row_sum = 0
            offset = y * w
            for x in range(w):
                row_sum += 255 - arr[offset + x]
            proj[y] = row_sum
        maxp = max(proj) if proj else 0
        thresh = max(8, int(maxp * 0.12))
        regions = []
        in_region = False
        start = 0
        for y, v in enumerate(proj):
            if v >= thresh and not in_region:
                in_region = True
                start = y
            elif v < thresh and in_region:
                end = y
                regions.append((start, end))
                in_region = False
        if in_region:
            regions.append((start, h))
        if not regions:
            win_h = 48
            step = max(24, int(win_h * 0.6))
            regions = [(y0, y0 + win_h) for y0 in range(0, max(1, h - win_h + 1), step)]
        merged_local = []
        for r in regions:
            if not merged_local:
                merged_local.append(r)
                continue
            prev = merged_local[-1]
            if r[0] - prev[1] <= merge_gap:
                merged_local[-1] = (prev[0], r[1])
            else:
                merged_local.append(r)
        centers = []
        for (s, e) in merged_local:
            pad_v = min(28, int((e - s) * 0.35) + 8)
            crop_top = max(0, s - pad_v)
            crop_bot = min(h, e + pad_v)
            center_y_local = crop_top + ((crop_bot - crop_top) // 2)
            centers.append(top + center_y_local)
        # compute typical spacing (icon step) and filter centers that cannot safely fit a centered crop
        def _compute_icon_step(centers_list):
            if not centers_list or len(centers_list) < 2:
                return 56
            diffs = [centers_list[i + 1] - centers_list[i] for i in range(len(centers_list) - 1)]
            diffs = [d for d in diffs if d > 4]
            if not diffs:
                return 56
            diffs.sort()
            mid = len(diffs) // 2
            return int(round(diffs[mid]))

        step = _compute_icon_step(centers)
        icon_h = max(48, min(80, int(round(step))))
        safe_centers = []
        min_center_y = top + top_skip_px + (icon_h // 2)
        max_center_y = top + height - bottom_skip_px - (icon_h // 2)
        for cy in centers:
            if cy < min_center_y or cy > max_center_y:
                continue
            safe_centers.append(int(round(cy)))
        return safe_centers

    def _detect_first_server_index(centers_list):
        """Return index of the first server icon in `centers_list`.

        Strategy:
        - Compute median spacing between centers.
        - Look for the first run of 3 consecutive spacings close to the median.
        - If found, return the lower index of that run (likely the first regular server row).
        - Fallback: return the first center that's safely below `top_skip_px` and has enough room for `icon_h`.
        """
        try:
            if not centers_list:
                return 0
            if len(centers_list) < 3:
                # small lists: prefer the first center that's below our header area
                for i, c in enumerate(centers_list):
                    if c - top > top_skip_px + 6:
                        return i
                return 0
            diffs = [centers_list[i + 1] - centers_list[i] for i in range(len(centers_list) - 1)]
            diffs_filtered = [d for d in diffs if d > 4]
            if not diffs_filtered:
                return 0
            diffs_sorted = sorted(diffs_filtered)
            median = diffs_sorted[len(diffs_sorted) // 2]
            # tolerance: 20% of median or 3 pixels
            tol = max(3, int(median * 0.20))
            # large-gap threshold: a gap larger than median + 40% or 10px indicates header/DMed separation
            large_gap = max(10, int(median * 1.4))
            # Find the earliest large gap (from top) that is followed by regular spacing
            for i in range(len(diffs)):
                if diffs[i] > large_gap:
                    # ensure next couple diffs look regular (if present)
                    ok_follow = True
                    for j in range(i + 1, min(i + 3, len(diffs))):
                        if abs(diffs[j] - median) > tol:
                            ok_follow = False
                            break
                    if ok_follow:
                        return i + 1
            # If none found, find first run of 3 regular diffs
            for i in range(len(diffs) - 2):
                if abs(diffs[i] - median) <= tol and abs(diffs[i + 1] - median) <= tol and abs(diffs[i + 2] - median) <= tol:
                    return i
            # fallback: find first center below header area
            for i, c in enumerate(centers_list):
                if c - top > top_skip_px + 6:
                    return i
            return 0
        except Exception:
            return 0

    def _find_first_server_index_hover(centers_list):
        """Find the first server index using hover+tooltip and spacing heuristics.

        Returns index in centers_list or 0.
        """
        try:
            if not centers_list:
                return 0
            step = _compute_icon_step(centers_list)
            median = step
            tol = max(3, int(median * 0.25))
            cx = left + (col_w // 2)
            for i, cy in enumerate(centers_list):
                if cy - top <= top_skip_px:
                    continue
                # ensure likely icon by contrast check with slightly larger area
                try:
                    if not _is_icon_at(cx, cy, size=36, var_threshold=9.0):
                        continue
                except Exception:
                    pass
                # verify spacing - prefer centers followed by another at roughly median spacing
                next_ok = False
                if i + 1 < len(centers_list):
                    d = centers_list[i + 1] - centers_list[i]
                    if abs(d - median) <= tol:
                        next_ok = True
                if not next_ok and len(centers_list) > 1:
                    # second fallback: if the next is within a reasonable range
                    if i + 1 < len(centers_list) and (centers_list[i + 1] - centers_list[i]) > 8:
                        next_ok = True
                # hover to read tooltip if present
                try:
                    txt = _hover_and_read(cx, cy)
                    name = normalize_ocr_name(txt) if txt else ""
                except Exception:
                    name = ""
                lname = (name or "").lower()
                if name and any(k in lname for k in DM_KEYWORDS):
                    # this is DM/home, skip
                    continue
                if name and any(bl in lname for bl in UI_BLACKLIST):
                    # UI element, skip
                    continue
                # if there is a hover name that looks like a server name, pick this
                if name:
                    return i
                # if no hover text but the center looks like an icon and spacing is ok, pick it
                if not name and next_ok:
                    return i
            return 0
        except Exception:
            return 0

    def _get_safe_crop_bbox(cx, cy, icon_h):
        half = int(icon_h // 2)
        y0 = int(cy) - half
        y1 = int(cy) + half
        min_y0 = top + top_skip_px
        max_y1 = top + height - bottom_skip_px
        if y0 < min_y0:
            y0 = min_y0
            y1 = y0 + icon_h
        if y1 > max_y1:
            y1 = max_y1
            y0 = y1 - icon_h
        # Clamp within global bounds
        y0 = max(min_y0, y0)
        y1 = min(max_y1, y1)
        x0 = left
        x1 = left + col_w
        return (int(x0), int(y0), int(x1), int(y1))

    def _compute_icon_step(centers_list):
        if not centers_list or len(centers_list) < 2:
            return 56
        diffs = [centers_list[i + 1] - centers_list[i] for i in range(len(centers_list) - 1)]
        diffs = [d for d in diffs if d > 4]
        if not diffs:
            return 56
        diffs.sort()
        mid = len(diffs) // 2
        return int(round(diffs[mid]))

    def _get_viewport_name_set(include_processed: bool = False):
        """Return a set of OCR'd server names currently visible in the viewport.

        This uses the projection centers, hovers each center to capture a tooltip if it's present,
        OCRs it using pytesseract, and returns a set of normalized names found.
        It does not save icons. This is used for overlap detection and end detection.
        """
        names = set()
        try:
            centers = _get_viewport_centers()
        except Exception:
            return names
        for cy in centers:
            if should_stop_scan():
                return names
            if not include_processed and int(round(cy)) in processed_centers:
                continue
            # Quick icon check to avoid hovering empty rows
            try:
                if not _is_icon_at(left + (col_w // 2), cy):
                    continue
            except Exception:
                pass
            try:
                txt = _hover_and_read(left + (col_w // 2), cy)
                if txt and txt.strip():
                    name = normalize_ocr_name(txt)
                    if name:
                        names.add(name)
            except Exception:
                continue
        return names

    def _scroll_safe(delta_pixels: int, batches: int = 4):
        if not (_HAS_PYAUTOGUI and pyautogui) or delta_pixels == 0:
            return
        cx = left + (col_w // 2)
        safe_y = _get_safe_scroll_y()
        try:
            pyautogui.moveTo(cx, safe_y, duration=0.12)
        except Exception:
            pass
        if batches <= 0:
            batches = 1
        step = int(delta_pixels / batches)
        if step == 0:
            step = -1 if delta_pixels < 0 else 1
        for _ in range(batches):
            try:
                pyautogui.scroll(step)
            except Exception:
                pass
            time.sleep(0.06)
        time.sleep(0.2)

    def scan_direction(direction: str, scroll_delta: int, max_iterations: int):
        """Traverse the server list in the given direction until the viewport stabilizes."""
        nonlocal server_list, known_names, processed_centers
        # allow re-processing when switching directions
        processed_centers.clear()
        iteration = 0
        consecutive_no_new_local = 0
        seen_viewport_counts_local = {}
        last_fingerprint = frozenset()
        stable_batches = 0
        viewport_history = []
        while iteration < max_iterations:
            prev_server_count = len(server_list)
            prev_known_count = len(known_names)
            # simplified mode: no per-iteration screenshots
            try:
                added_any, centers = process_viewport_centers(direction=direction, icon_step=56)
            except Exception:
                added_any = False
                centers = []
            new_server_count = len(server_list)
            new_known_count = len(known_names)
            new_entries = new_server_count > prev_server_count or new_known_count > prev_known_count
            if new_entries:
                consecutive_no_new_local = 0
            else:
                consecutive_no_new_local += 1
            try:
                viewport_names = _get_viewport_name_set(include_processed=True)
            except Exception:
                viewport_names = set()
            fingerprint = frozenset(viewport_names)
            try:
                viewport_history.append(sorted(list(fingerprint)))
                _save_viewport_history(viewport_history)
            except Exception:
                pass
            seen_viewport_counts_local[fingerprint] = seen_viewport_counts_local.get(fingerprint, 0) + 1
            if fingerprint == last_fingerprint:
                stable_batches += 1
            else:
                stable_batches = 1
                last_fingerprint = fingerprint
            for cy in centers:
                try:
                    processed_centers.add(int(round(cy)))
                except Exception:
                    pass
            if viewport_names and viewport_names.issubset(known_names) and consecutive_no_new_local >= 3 and stable_batches >= 3:
                break
            if seen_viewport_counts_local.get(fingerprint, 0) >= 4:
                break
            if should_stop_scan() or not (_HAS_PYAUTOGUI and pyautogui):
                break
            _scroll_safe(scroll_delta)
            iteration += 1

    def _ensure_top_position(attempts: int = 3):
        for _ in range(attempts):
            if not (_HAS_PYAUTOGUI and pyautogui):
                break
            _scroll_safe(scroll_amount)

    def _seek_extreme(direction: str = 'up', max_iters: int = 40, repeat_goal: int = 3, step_clicks: int = 20):
        """Try to move viewport to the extreme (top/up or bottom/down).

        Scrolls in larger batches while checking viewport fingerprints; stops
        when the fingerprint becomes stable for `repeat_goal` iterations or
        when `max_iters` is reached.
        Returns True if stability was achieved.
        """
        if not (_HAS_PYAUTOGUI and pyautogui):
            return False
        stable = 0
        last_fp = None
        sign = 1 if direction == 'down' else -1
        for _ in range(max_iters):
            try:
                fp = frozenset(_get_viewport_name_set(include_processed=True))
            except Exception:
                fp = frozenset()
            if fp == last_fp and fp:
                stable += 1
            else:
                stable = 1
                last_fp = fp
            if stable >= repeat_goal:
                return True
            # perform a larger scroll chunk
            try:
                _scroll_safe(sign * step_clicks, batches=4)
            except Exception:
                try:
                    pyautogui.scroll(sign * (step_clicks * 3))
                except Exception:
                    pass
            time.sleep(0.18)
        return False

    # New strict progressive scan:
    # - Seek to top (attempt several small upward scrolls until viewport stabilizes)
    # - Figure out which center is the first server by hovering centers and detecting the DM/home label
    # - For each viewport: process centers top->bottom starting at the detected first server index
    #   - MUST hover and OCR each icon until a non-empty normalized name is returned (or stop requested)
    # - Only scroll after the last visible center has been successfully OCR'd
    # - Repeat until viewport fingerprint stabilizes at bottom
    icon_step = 56
    overlap = 1
    scroll_step = max(8, icon_step - overlap)
    up_delta = max(32, abs(scroll_amount) // 3)

    # UI labels to ignore (non-server entries)
    UI_BLACKLIST = {
        'friends', 'nitro', 'direct messages', 'direct message', 'home', 'add a server', 'create', 'download',
        'explore public servers', 'threads', 'stage', 'settings', 'friends list'
    }

    # small helper to detect DM/home tooltip text
    DM_KEYWORDS = ('direct messages', 'direct message', 'home', 'friends')

    # seek to top conservatively (small scrolls), then try a stronger seek
    try:
        last_fp = None
        stable = 0
        for _ in range(max_scrolls * 3):
            try:
                fp = frozenset(_get_viewport_name_set(include_processed=True))
            except Exception:
                fp = frozenset()
            if fp == last_fp and fp:
                stable += 1
            else:
                stable = 1
                last_fp = fp
            if stable >= 3:
                break
            _scroll_safe(up_delta)
            time.sleep(0.12)
    except Exception:
        pass
    # Try a stronger seek to top using fingerprint-driven larger scrolls
    try:
        _seek_extreme('up', max_iters=max_scrolls * 6, repeat_goal=3, step_clicks=20)
    except Exception:
        pass
    # If user requested to start from top, enforce a stronger seek again (longer if requested)
    if start_from_top:
        try:
            _seek_extreme('up', max_iters=max_scrolls * 8, repeat_goal=4, step_clicks=24)
        except Exception:
            pass

    prev_fp = None
    stable_bottom = 0
    iterations = 0
    max_iters = max_scrolls * 6
    desired_top_y = top + top_skip_px + 4

    # detect first-server index by inspecting center spacing and header area
    detected_first_idx = 0
    try:
        centers = _get_viewport_centers()
        # First try hover-based detection; if it returns 0 and centers[0] is likely not server, then fallback
        hover_idx = _find_first_server_index_hover(centers)
        spacing_idx = _detect_first_server_index(centers)
        # prefer hover detection when available
        detected_first_idx = hover_idx if hover_idx is not None else spacing_idx
        # cast to 0 if invalid
        if detected_first_idx is None:
            detected_first_idx = 0
        cx = left + (col_w // 2)
        try:
            # sanity check: if detected index is not an icon, fall back to spacing_idx
            if not _is_icon_at(cx, centers[detected_first_idx]):
                detected_first_idx = spacing_idx
        except Exception:
            pass
        print(f"Detected first-server index: hover={hover_idx}, spacing={spacing_idx} => using {detected_first_idx}")
        # Emit centers and diffs for debugging to help diagnose mis-detection
        try:
            if len(centers) > 0:
                diffs = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
                print(f"centers: {centers}")
                print(f"diffs: {diffs}")
        except Exception:
            pass
    except Exception:
        detected_first_idx = 0

    initial_pass = True
    while iterations < max_iters and not should_stop_scan():
        iterations += 1
        centers = _get_viewport_centers()
        if not centers:
            _scroll_safe(-scroll_step)
            time.sleep(0.12)
            continue

        if initial_pass:
            start_idx = detected_first_idx if detected_first_idx is not None else 0
        else:
            start_idx = 0
        cx = left + (col_w // 2)
        # ensure start_idx is within bounds
        start_idx = min(max(0, start_idx), max(0, len(centers) - 1))

        # process every visible icon top->bottom, blocking until a name is read
        step = _compute_icon_step(centers)
        icon_h = max(48, min(80, int(round(step))))
        for i in range(start_idx, len(centers)):
            cy = centers[i]
            if should_stop_scan():
                break
            if int(round(cy)) in processed_centers:
                continue
            try:
                if not _is_icon_at(cx, cy):
                    processed_centers.add(int(round(cy)))
                    continue
            except Exception:
                pass

            # Save crop immediately
            try:
                bbox = _get_safe_crop_bbox(cx, cy, icon_h)
                crop = _safe_grab(bbox)
                if crop is None:
                    print(f"Warning: safe_grab returned None for center at y={cy}; skipping")
                    processed_centers.add(int(round(cy)))
                    continue
            except Exception:
                processed_centers.add(int(round(cy)))
                continue
            try:
                thumb = crop.convert('L').resize((32, 32), resample=Image.BILINEAR)
            except Exception:
                thumb = None

            # Block until we have a name (or stop requested), but bound attempts
            name = ""
            attempts = 0
            while not name and not should_stop_scan() and attempts < max_icon_retries:
                try:
                    txt = _hover_and_read(cx, cy)
                    name = normalize_ocr_name(txt) if txt else ""
                except Exception:
                    name = ""
                if not name:
                    attempts += 1
                    time.sleep(0.12)
            print(f"Icon at y={cy}: OCR result='{name}' after {attempts} attempts")

            # save icon file
            idx = len(list(imgs.glob('server_*.png')))
            icon_path = imgs / f"server_{idx}.png"
            try:
                crop.save(icon_path)
                print(f"Saved icon crop to {icon_path}")
            except Exception:
                processed_centers.add(int(round(cy)))
                continue
            if thumb is not None:
                try:
                    seen_thumbs.append((thumb, str(icon_path)))
                except Exception:
                    pass

            # record or update
            try:
                if name:
                    server_list.append({"name": name, "icon": str(icon_path), "pos": (cx, cy)})
                    known_names.add(name)
                else:
                    server_list.append({"name": "", "icon": str(icon_path), "pos": (cx, cy)})
                _save_meta()
            except Exception:
                pass

            processed_centers.add(int(round(cy)))

        if should_stop_scan():
            break

        # After scanning the last visible icon, scroll so the last becomes the new top
        last_center = centers[-1]
        delta_pixels = int(desired_top_y - last_center)
        try:
            _scroll_safe(delta_pixels)
        except Exception:
            try:
                if _HAS_PYAUTOGUI and pyautogui:
                    pyautogui.scroll(delta_pixels)
            except Exception:
                pass
        time.sleep(0.18)

        try:
            fp = frozenset(_get_viewport_name_set(include_processed=True))
        except Exception:
            fp = frozenset()
        if fp == prev_fp and fp:
            stable_bottom += 1
        else:
            stable_bottom = 1
            prev_fp = fp
        if stable_bottom >= 4:
            break
        # after the first pass, ensure subsequent passes do not assume DM skip
        initial_pass = False

    # (no debug screenshots in simplified mode)

    # save metadata
    meta_path = p / "servers.json"
    try:
        with open(meta_path, 'w', encoding='utf-8') as fh:
            json.dump(server_list, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass

    # Attempt to repair blank names by re-hovering recorded positions
    try:
        blanks = [s for s in server_list if not s.get('name') and s.get('pos')]
        if blanks and (_HAS_PYAUTOGUI and pyautogui) and _HAS_PYTESSERACT and pytesseract:
            for s in blanks:
                try:
                    x, y = s['pos']
                    pyautogui.moveTo(x, y, duration=0.18)
                    time.sleep(max(hover_delay, 0.9))
                    # capture tooltip area and OCR
                    candidate_boxes = [
                        (x + 24, y - 40, x + 260, y + 40),
                        (x - 120, y - 60, x + 120, y - 10),
                        (x + 10, y - 40, x + 220, y + 10),
                    ]
                    for tb in candidate_boxes:
                        try:
                            tip_img = _safe_grab(tb)
                        except Exception:
                            tip_img = None
                        if tip_img is not None:
                            txt = ocr_image_to_text(tip_img)
                            if txt and txt.strip():
                                s['name'] = normalize_ocr_name(txt)
                                break
                except Exception:
                    continue
            try:
                with open(meta_path, 'w', encoding='utf-8') as fh:
                    json.dump(server_list, fh, ensure_ascii=False, indent=2)
            except Exception:
                pass
    except Exception:
        pass

    # Attempt to locate icon images with null positions by scanning and matching thumbnails
    try:
        placeholder_entries = [s for s in server_list if not s.get('pos') and s.get('icon')]
        if placeholder_entries and _HAS_PYAUTOGUI and pyautogui:
            # Build mapping of icon_path -> thumb
            icon_thumb_map = {}
            for s in placeholder_entries:
                try:
                    p = Path(s['icon'])
                    if p.exists():
                        img = Image.open(p).convert('L').resize((32, 32), resample=Image.BILINEAR)
                        icon_thumb_map[str(p)] = img
                except Exception:
                    continue

            # Scan the viewport step-wise and try to match icons
            icon_step = 56
            y0 = top + top_skip_px + int(icon_step / 2)
            y = y0
            while y < top + height - bottom_skip_px:
                cx = left + (col_w // 2)
                cy = int(y)
                try:
                    if _HAS_PYAUTOGUI and pyautogui:
                        pyautogui.moveTo(cx, cy, duration=0.12)
                        time.sleep(max(0.6, hover_delay))
                except Exception:
                    pass
                try:
                    step = _compute_icon_step(_get_viewport_centers())
                    icon_h = max(48, min(80, int(round(step))))
                    bbox = _get_safe_crop_bbox(cx, cy, icon_h)
                    crop = _safe_grab(bbox)
                except Exception:
                    y += icon_step
                    continue
                try:
                    thumb = crop.convert('L').resize((32, 32), resample=Image.BILINEAR)
                except Exception:
                    thumb = None
                if thumb is None:
                    y += icon_step
                    continue
                if should_stop_scan():
                    print('Stop requested during placeholder scanning; exiting')
                    break
                # compare against placeholders
                for icon_path, saved_thumb in list(icon_thumb_map.items()):
                    try:
                        d = ImageChops.difference(thumb, saved_thumb)
                        if hasattr(d, 'histogram'):
                            hist = d.histogram()
                            diff_metric = sum(i * v for i, v in enumerate(hist))
                        else:
                            diff_metric = sum(d.getdata())
                        if diff_metric < duplicate_thresh:
                            # assign pos and OCR name
                            for s in server_list:
                                if s.get('icon') == icon_path and not s.get('pos'):
                                    s['pos'] = (cx, cy)
                                    # re-ocr tooltip
                                    candidate_boxes = [
                                        (cx + 15, cy - 30, cx + 200, cy + 10),
                                        (cx + 8, cy - 15, cx + 130, cy + 15),
                                    ]
                                    for tb in candidate_boxes:
                                        try:
                                            tip_img = _safe_grab(tb)
                                            txt = ocr_image_to_text(tip_img)
                                            if txt and txt.strip():
                                                s['name'] = normalize_ocr_name(txt)
                                                break
                                        except Exception:
                                            continue
                                    # remove from mapping so we don't find it again
                                    try:
                                        del icon_thumb_map[icon_path]
                                    except Exception:
                                        pass
                                    break
                    except Exception:
                        continue
                y += icon_step

            # Save updated metadata
            try:
                with open(meta_path, 'w', encoding='utf-8') as fh:
                    json.dump(server_list, fh, ensure_ascii=False, indent=2)
            except Exception:
                pass
    except Exception:
        pass

    # simplified mode: no screenshot archiving

    return server_list


if __name__ == "__main__":
    check_tesseract_path()
    discord_window = find_discord()
    if discord_window:
        print("Discord window found and focused.")
    else:
        print("Discord window not found. Please ensure Discord is running and not minimized.")
    read_screen_with_tesseract()

