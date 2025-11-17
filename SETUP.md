# macOS Setup Notes

Recommended quick setup for macOS:

1. Install Homebrew if you don't have it:

   ```sh
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```

2. Install system libs needed for Pillow:

   ```sh
   brew install pkg-config jpeg zlib libpng libtiff freetype
   ```

3. Prefer Python 3.11 (prebuilt Pillow wheels are available for 3.11):

   - If you have multiple Python versions, `scripts/setup.sh` will prefer `python3.11`.

4. Create venv and install Python packages:

   ```sh
   ./scripts/setup.sh
   source .venv/bin/activate
   ```

5. Permissions:

   - Give Terminal/Python permission for Screen Recording (System Settings → Privacy & Security → Screen Recording) so screenshots and PyAutoGUI work.

6. Tesseract:

   - The utility attempts to detect Tesseract. Install via Homebrew if missing:

     ```sh
     brew install tesseract
     ```

7. Run the helper in a controlled way and test in a private server first to ensure the posting behaviour meets server rules.
