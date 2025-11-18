#!/usr/bin/env python3
from pathlib import Path
p = Path('data/servers')
p.mkdir(parents=True, exist_ok=True)
stop = p / '.STOP'
stop.write_text('stop')
print(f'Created stop file: {stop.resolve()}')
