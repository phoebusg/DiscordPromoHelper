# Setup Guide

## macOS (Primary Platform)

### 1. Install Dependencies

```bash
# Homebrew (if not installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# System libraries for Pillow
brew install pkg-config jpeg zlib libpng libtiff freetype

# Tesseract OCR (required)
brew install tesseract

# Tkinter support (for GUI)
brew install python-tk@3.11
```

### 2. Clone & Setup

```bash
git clone https://github.com/phoebusg/DiscordPromoHelper.git
cd DiscordPromoHelper
./scripts/setup.sh
source .venv/bin/activate
```

### 3. Grant Permissions

**System Settings → Privacy & Security:**

| Permission | Why |
|------------|-----|
| Screen Recording | Screenshots for OCR |
| Accessibility | Mouse/keyboard control |

Grant access to **Terminal** (or your IDE like VS Code).

### 4. Verify Setup

```bash
# Check Tesseract
tesseract --version

# Check Python imports
python -c "import tkinter; import pytesseract; print('OK')"

# Launch GUI
python -m src.main
```

## Windows

### 1. Install Python 3.11+

Download from [python.org](https://www.python.org/downloads/)

### 2. Install Tesseract

Download installer from [UB-Mannheim](https://github.com/UB-Mannheim/tesseract/wiki)  
Default path: `C:\Program Files\Tesseract-OCR\tesseract.exe`

### 3. Clone & Setup

```cmd
git clone https://github.com/phoebusg/DiscordPromoHelper.git
cd DiscordPromoHelper
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Launch

```cmd
python -m src.main
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `ModuleNotFoundError: _tkinter` | `brew install python-tk@3.11` |
| Tesseract not found | Check path in `src/utils.py` or run `brew install tesseract` |
| Screenshots fail | Grant Screen Recording permission |
| Mouse clicks don't work | Grant Accessibility permission |
| OCR returns empty | Ensure Discord is visible and not minimized |

## First Run

1. **Open Discord** - Make it visible on screen
2. **Run scan**: `python -m src.main --scan`
3. **Launch GUI**: `python -m src.main`
4. **Configure**: Set friendly names and promo channels per server
