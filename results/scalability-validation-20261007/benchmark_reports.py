"""100 synthetic project reports, baseline vs updated renderer; no network requests."""
from pathlib import Path
import argparse
import csv
import hashlib
import json
import os
import resource
import subprocess
import sys
import time

OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[1]

def prepare():
    run=ROOT/'results/india-pilot'
    manifest=json.loads((run/'run_manifest.json').read_text())
    original=manifest['projects']
    ids=[f'synthetic-{i:03d}' for i in range(100)]
    for variant in ('baseline_report','updated_report'):
        target=OUT/variant;target.mkdir(exist_ok=True)
        copied=json.loads(json.dumps(manifest))
        copied['projects']=ids
        copied['config']['overrides']={'run':{'label':'fixture'}}
        copied['synthetic_workload']=True
        (target/'run_manifest.json').write_text(json.dumps(copied))
        for i,pid in enumerate(ids):
            old=original[i%len(original)]
            src=run/'projects'/old;dest=target/'projects'/pid;dest.mkdir(parents=True,exist_ok=True)
            for name in ('inventory.json','features.json','observations.jsonl','pages.json'):
                # Prefix observation IDs and project/interface references consistently.
                text=(src/name).read_text().replace(old,pid).replace('obs-',f'obs-syn{i:03d}-')
                (dest/name).write_text(text)
        (target/'SYNTHETIC_ONLY.md').write_text('Synthetic capacity workload: ten saved profiles repeated under 100 artificial IDs. No new government projects or observations were collected. Never add these to research results.\n')

def render(target):
    from dpi_audit.report import build
    started=time.perf_counter(); result=build(target)
    print(json.dumps({'seconds':round(time.perf_counter()-started,3),
                      'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      'counts':result}))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--render');args=parser.parse_args()
    if args.render:
        render(Path(args.render));return
    prepare();stats={}
    for variant,src in [('baseline_report',OUT/'baseline/src'),('updated_report',ROOT/'src')]:
        env=dict(os.environ,PYTHONPATH=str(src),DPI_AUDIT_HOME=str(ROOT))
        proc=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--render',str(OUT/variant)],
                            env=env,capture_output=True,text=True,timeout=180)
        (OUT/f'{variant}.log').write_text(proc.stdout+proc.stderr)
        assert proc.returncode==0,proc.stderr
        stats[variant]=json.loads(proc.stdout)
    matches={}
    for p in sorted((OUT/'updated_report').glob('*.csv')):
        matches[p.name]=p.read_bytes()==(OUT/'baseline_report'/p.name).read_bytes()
    assert all(matches.values()),matches
    stats['csv_byte_matches']=matches
    stats['scope']='100 synthetic report projects; no 100-site browser benchmark or research sample expansion'
    (OUT/'report_benchmark.json').write_text(json.dumps(stats,indent=2)+'\n')
    print(json.dumps(stats,indent=2))

if __name__=='__main__':main()
