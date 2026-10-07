#!/usr/bin/env python3
"""Close only a named owned audit browser at a documented collection deadline."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTICS = ROOT / 'results/india-pilot/diagnostics'
plan = json.loads((DIAGNOSTICS / 'peso_watchdog_plan.json').read_text())
pid = plan['browser_pid']
worker_pid = plan['worker_pid']
status = 'browser finished before watchdog deadline'
elapsed = None
while Path(f'/proc/{pid}/cmdline').exists():
    browser_cmd = Path(f'/proc/{pid}/cmdline').read_bytes().decode(errors='replace')
    worker_file = Path(f'/proc/{worker_pid}/cmdline')
    worker_cmd = worker_file.read_bytes().decode(errors='replace') if worker_file.exists() else ''
    if plan['browser_identity'] not in browser_cmd or plan['project_id'] not in worker_cmd:
        status = 'identity changed or worker finished; no signal sent'
        break
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'etimes='], capture_output=True, text=True)
    if not result.stdout.strip():
        break
    elapsed = int(result.stdout.strip())
    if elapsed >= plan['browser_elapsed_limit_s']:
        os.kill(pid, signal.SIGTERM)
        status = 'owned stalled browser closed at documented collection deadline; partial results require review'
        break
    time.sleep(min(10, plan['browser_elapsed_limit_s'] - elapsed))
record = dict(plan, completed_utc=datetime.now(timezone.utc).isoformat(),
              browser_elapsed_s=elapsed, action=status)
(DIAGNOSTICS / 'peso_watchdog_result.json').write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps(record, indent=2), flush=True)
