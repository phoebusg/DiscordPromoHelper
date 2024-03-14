import subprocess
import sys
import os
import pytesseract
from PIL import Image
import pyautogui

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
    Automatically updates the Tesseract command path based on the operating system.
    """
    if sys.platform.startswith('win32'):
        pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    elif sys.platform.startswith('darwin') or sys.platform.startswith('linux'):
        pytesseract.pytesseract.tesseract_cmd = 'tesseract'
    print(f"Tesseract command updated: {pytesseract.pytesseract.tesseract_cmd}")

def is_tesseract_installed():
    """
    Checks if Tesseract is installed by trying to run 'tesseract -v'
    and also by checking common installation directories on Windows.
    """
    try:
        subprocess.run(["tesseract", "-v"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception:
        if sys.platform.startswith('win32'):
            common_paths = [r"C:\Program Files\Tesseract-OCR", r"C:\Program Files (x86)\Tesseract-OCR"]
            for path in common_paths:
                if os.path.isfile(os.path.join(path, "tesseract.exe")):
                    print(f"Tesseract found in {path}. Please add it to your PATH.")
                    pytesseract.pytesseract.tesseract_cmd = os.path.join(path, "tesseract.exe")
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

if __name__ == "__main__":
    check_tesseract_path()
