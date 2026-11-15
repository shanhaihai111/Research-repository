import json, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
subprocess.run([sys.executable,'src/run_weekly.py','--date','2026-11-09','--sample','--no-email'],cwd=ROOT,check=True)
p=ROOT/'reports'/'2026-11-09'
assert (p/'Weekly_Research_Report_20261109.html').exists()
assert (p/'Weekly_Research_Report_20261109.pdf').exists()
a=json.loads((p/'audit.json').read_text(encoding='utf-8'))
assert a['included'] == 1
