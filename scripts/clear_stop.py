#!/usr/bin/env python3
from pathlib import Path
p = Path('data/servers')
stop = p / '.STOP'
if stop.exists():
    stop.unlink()
    print('Removed stop file')
else:
    print('Stop file not present')
