#!/usr/bin/env bash
# Setup script for DiscordPromoHelper
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

# Prefer python3.11 if available
PYTHON_CMD="$(command -v python3.11 || command -v python3 || true)"
if [[ -z "$PYTHON_CMD" ]]; then
  echo "Python not found. Please install Python 3.11+ and re-run this script." >&2
  exit 1
fi

echo "Using Python: $($PYTHON_CMD --version)"

echo "Creating virtual environment in .venv"
$PYTHON_CMD -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel

echo "Installing Homebrew dependencies for macOS (if brew available)"
if command -v brew >/dev/null 2>&1; then
  brew install pkg-config jpeg zlib libpng libtiff freetype || true
fi

REQ="clean_requirements.txt"
if [[ ! -f "$REQ" ]]; then
  echo "Generating $REQ from requirements.txt"
  python - <<'PY'
from pathlib import Path
raw=Path('requirements.txt').read_bytes()
txt=raw.decode('utf-8','ignore').replace('\x00','')
lines=[l.strip() for l in txt.splitlines() if l.strip() and not l.strip().startswith('```')]
Path('clean_requirements.txt').write_text('\n'.join(lines))
print('wrote clean_requirements.txt')
PY
fi

echo "Installing Python packages from $REQ"
pip install -r "$REQ"

echo "Setup complete. Activate with: source .venv/bin/activate"