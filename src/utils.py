import subprocess
import sys
import os
import base64
import ctypes
import pytesseract
from PIL import Image, ImageGrab
import pyautogui
import psutil
import time


def run_command_as_admin(command):
    """
    Attempts to run the given command with administrative privileges.
    """
    if sys.platform.startswith('win32'):
        subprocess.run(["powershell", "Start-Process", command[0], "-ArgumentList", ' '.join(command[1:]), "-Verb", "RunAs"], check=True)
    else:
        subprocess.run(["sudo"] + command, check=True)

def install_tesseract_on_windows():
    """
    Download and install Tesseract on Windows without changing PowerShell execution policy.
    """
    tesseract_installer_url = "https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-5.3.3.20231005.exe"
    tesseract_installer_path = os.path.join(os.environ["TEMP"], "tesseract-installer.exe")
    
    print("Downloading Tesseract installer...")
    download_command = ["powershell", "-Command", f"Invoke-WebRequest -Uri '{tesseract_installer_url}' -OutFile '{tesseract_installer_path}'"]
    subprocess.run(download_command, check=True)

    print("Installing Tesseract, please accept any UAC prompts...")
    install_command = ["powershell", "-Command", f"Start-Process -FilePath '{tesseract_installer_path}' -ArgumentList '/S' -Wait -Verb RunAs"]
    subprocess.run(install_command, check=True)

    # Cleanup the installer
    os.remove(tesseract_installer_path)
    print("Tesseract installation completed.")

def is_homebrew_installed():
    """
    Checks if Homebrew is installed on macOS.
    """
    try:
        subprocess.run(["brew", "--version"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception:
        return False

def install_homebrew():
    """
    Install Homebrew on macOS using the official installation script.
    """
    homebrew_url = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
    homebrew_script_path = os.path.join(os.environ["TEMP"], "homebrew-install.sh")
    
    print("Downloading Homebrew installer...")
    download_command = ["curl", "-fsSL", "-o", homebrew_script_path, homebrew_url]
    subprocess.run(download_command, check=True)

    print("Installing Homebrew, please accept any prompts...")
    install_command = ["bash", homebrew_script_path]
    subprocess.run(install_command, check=True)

    # Cleanup the installer
    os.remove(homebrew_script_path)
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

    # PowerShell command to add Tesseract to the system PATH
    command = f'$newPath = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";{tesseract_path}"; ' \
              f'[Environment]::SetEnvironmentVariable("Path", $newPath, "Machine")'

    try:
        run_powershell_command_as_admin(command)
        print(f"Added Tesseract to PATH: {tesseract_path}")

        # Attempt to reflect the change in the current session
        os.environ['Path'] += f";{tesseract_path}"
        print("Tesseract path added to the PATH in the current session.")
    except Exception as e:
        print("Failed to add Tesseract to PATH.")
        print(e)

def add_tesseract_to_path_unix(tesseract_path):
    """
    Adds Tesseract to the PATH on Unix-based systems (macOS and Linux).
    """
    shell_profile = "~/.bashrc"  # Default to bash
    if os.environ.get("SHELL") and "zsh" in os.environ["SHELL"]:
        shell_profile = "~/.zshrc"
    
    try:
        with open(os.path.expanduser(shell_profile), "a") as profile:
            profile.write(f"\nexport PATH=$PATH:{tesseract_path}")
        print(f"Added Tesseract to PATH in {shell_profile}")
    except Exception as e:
        print("Failed to add Tesseract to PATH. Please update your shell profile manually.")
        print(e)


def is_tesseract_installed():
    """
    Checks if Tesseract is installed and attempts to add it to the system's PATH if necessary.
    """
    try:
        subprocess.run(["tesseract", "-v"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception:
        tesseract_path = None
        if sys.platform.startswith('win32'):
            common_paths = [r"C:\Program Files\Tesseract-OCR", r"C:\Program Files (x86)\Tesseract-OCR"]
            for path in common_paths:
                if os.path.isfile(os.path.join(path, "tesseract.exe")):
                    tesseract_path = path
                    break
        if tesseract_path:
            print(f"Tesseract found in {tesseract_path}. Attempting to add it to your PATH...")
            if sys.platform.startswith('win32'):
                add_tesseract_to_path_windows(tesseract_path)
            else:
                add_tesseract_to_path_unix(tesseract_path)
            return True
        return False


def check_tesseract_path():
    """
    Verifies that Tesseract is correctly installed and in the system's PATH.
    """
    if is_tesseract_installed():
        print("Tesseract is correctly installed and accessible.")
    else:
        print("Tesseract is not accessible. Attempting installation...")
        install_tesseract()

def is_discord_running():
    """
    Check if Discord is running by looking for its process.
    """
    for process in psutil.process_iter(['name']):
        if "discord" in process.info['name'].lower():
            return True
    return False

def run_discord():
    """
    Attempts to run Discord based on the operating system.
    """
    if sys.platform.startswith('win32'):
        discord_update_path = os.path.join(os.environ["LOCALAPPDATA"], "Discord", "Update.exe")
        try:
            # Use the correct PowerShell command format
            command = f"Start-Process -FilePath '{discord_update_path}' -ArgumentList '--processStart Discord.exe'"
            subprocess.run(["powershell", "-Command", command], check=True)
            print("Discord should now be running.")
        except FileNotFoundError:
            print("Discord executable not found. Please ensure Discord is installed.")
    elif sys.platform.startswith('darwin'):
        try:
            subprocess.Popen(["open", "/Applications/Discord.app"])
            print("Discord should now be running.")
        except FileNotFoundError:
            print("Discord app not found. Please ensure Discord is installed on your Mac.")
    elif sys.platform.startswith('linux'):
        try:
            subprocess.Popen(["discord"])
            print("Discord should now be running.")
        except FileNotFoundError:
            print("Discord executable not found. Please ensure Discord is installed on your Linux system.")
    else:
        print("Unsupported OS.")

def find_and_focus_discord():
    """
    Finds the Discord window and brings it to the foreground if possible.
    """
    for window in pyautogui.getAllWindows():
        if "discord" in window.title.lower():
            window.activate()
            # Correctly return the window's position and size
            return (window.left, window.top, window.width, window.height)
        return None

def find_discord():
    """
    Main function to find or run Discord.
    Attempts to bring Discord to the foreground if it's already running,
    or runs it if it's not. Then confirms the window is successfully brought to the foreground.
    """
    run_discord()  # Attempt to run Discord, which should bring it to the foreground if already running

    # Wait a brief moment for Discord to possibly come to the foreground
    time.sleep(5)  # Adjust based on the expected delay for Discord to respond

    # Confirm Discord window is in the foreground
    window_position = find_and_focus_discord()  # This function needs to check if Discord is actually focused
    if window_position:
        print("Discord window found and focused.")
        return True

    # If the window wasn't focused initially, try a few more times
    max_retries = 5  # Adjust based on testing, fewer retries may be needed given the initial delay
    retries = 0
    while retries < max_retries:
        window_position = find_and_focus_discord()
        if window_position:
            print("Discord window successfully brought to the foreground.")
            return True
        else:
            print(f"Retrying to bring Discord to the foreground... Attempt {retries + 1}/{max_retries}")
            time.sleep(5)  # Wait for 5 seconds before trying again
            retries += 1

    print("Unable to confirm Discord window in the foreground. Please check manually.")
    return False

def read_screen_with_tesseract():
    # Capture the entire screen
    screenshot = ImageGrab.grab()
    screenshot.save("screenshot.png")  # Optionally save the screenshot for debugging

    # Use Tesseract to read text from the screenshot
    text = pytesseract.image_to_string(screenshot)
    print(text)


if __name__ == "__main__":
    check_tesseract_path()
    discord_window = find_discord()
    if discord_window:
        print("Discord window found and focused.")
    else:
        print("Discord window not found. Please ensure Discord is running and not minimized.")
    read_screen_with_tesseract()

