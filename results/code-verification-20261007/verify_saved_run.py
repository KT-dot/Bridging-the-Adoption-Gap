"""Offline provenance and reproducibility checks; never writes to the historical run."""
from pathlib import Path
from dataclasses import fields
import csv
import hashlib
import json
import shutil
import tempfile
from collections import Counter

from dpi_audit.aggregate import Aggregator
from dpi_audit.codebook import load_codebook
from dpi_audit.config import load_config
from dpi_audit.inputs import parse_list, project_ui_applicability
from dpi_audit.observations import Observation
from dpi_audit.pipeline import code_hash
from dpi_audit.report import build

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
RUN = ROOT / 'results/india-pilot'

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def snapshot():
    return {str(p.relative_to(RUN)): digest(p) for p in RUN.rglob('*') if p.is_file()}

def csv_rows(p):
    return list(csv.DictReader(p.open(newline='')))

def main():
    before = snapshot()
    manifest = json.loads((RUN / 'run_manifest.json').read_text())
    cb = load_codebook()
    assert cb.content_hash == manifest['codebook']['content_sha256']
    assert code_hash() == manifest['code_hash']
    parsed = parse_list(RUN / 'inputs/list.local.txt')
    assert parsed.ok, parsed.errors
    cfg = json.loads((RUN / 'config.resolved.json').read_text())
    context_fields = ('codebook_version', 'rule_version', 'config_hash', 'code_hash', 'audit_timestamp')
    observation_fields = {f.name for f in fields(Observation)}
    all_observations = []
    comparisons = []
    for project in parsed.projects:
        folder = RUN / 'projects' / project.project_id
        inv = json.loads((folder / 'inventory.json').read_text())
        raw = [json.loads(line) for line in (folder / 'observations.jsonl').read_text().splitlines()]
        all_observations.extend(raw)
        obs = [Observation(**{k:v for k,v in o.items() if k in observation_fields}) for o in raw]
        agg = Aggregator(cb, cfg, {k: inv[k] for k in context_fields})
        regenerated = []
        for info in inv['interfaces']:
            regenerated += agg.interface_rows(project, info, [o for o in obs if o.interface_id == info['interface_id']], inv['mode'])
        regenerated += agg.project_rows(project, project_ui_applicability(project), inv['interfaces'], obs, inv['mode'])
        saved = json.loads((folder / 'features.json').read_text())
        differences = []
        assert len(regenerated) == len(saved)
        for a,b in zip(regenerated, saved):
            if a != b:
                differences.append({'scope':b['scope'], 'feature':b['feature_id'],
                                    'fields':[k for k in set(a)|set(b) if a.get(k)!=b.get(k)]})
        comparisons.append({'project_id':project.project_id,'rows':len(saved),'differences':differences})
    # A fresh report build uses copies of saved inputs, not live collection.
    with tempfile.TemporaryDirectory(prefix='dpi-report-verification-') as tmp:
        target = Path(tmp)
        shutil.copy2(RUN / 'run_manifest.json', target / 'run_manifest.json')
        for pid in manifest['projects']:
            dest = target / 'projects' / pid
            dest.mkdir(parents=True)
            for name in ('inventory.json','features.json','observations.jsonl','pages.json'):
                shutil.copy2(RUN / 'projects' / pid / name, dest / name)
        rebuilt = build(target)
        csv_comparisons = []
        for path in sorted(target.glob('*.csv')):
            original = RUN / path.name
            csv_comparisons.append({'file':path.name,'matches_saved':original.exists() and csv_rows(path)==csv_rows(original),
                                    'rows':len(csv_rows(path))})
        # HTML timestamps differ; compare the actual rendered feature table instead.
        from bs4 import BeautifulSoup
        def table(p):
            return str(BeautifulSoup(p.read_text(),'html.parser').select_one('table#feat'))
        html_matches = table(target/'summary.html') == table(RUN/'summary.html')
    combined = [json.loads(line) for line in (RUN/'observations.jsonl').read_text().splitlines()]
    assert [{k:v for k,v in o.items() if k!='run_label'} for o in combined] == all_observations
    after = snapshot()
    result = {'scope':'Offline reconstruction only; no government websites revisited',
              'code_hash_matches_historical_run':True,'codebook_hash_matches_historical_run':True,
              'projects':len(parsed.projects),'raw_observations':len(combined),
              'raw_methods':dict(Counter(o['method'] for o in combined)),
              'feature_reaggregation':comparisons,'report_csv_rebuild':csv_comparisons,
              'html_feature_table_matches':html_matches,'rebuilt_report_counts':rebuilt,
              'historical_files_unchanged':before==after,'historical_files_hashed':len(before)}
    (OUT/'saved_run_verification.json').write_text(json.dumps(result,indent=2)+'\n')
    assert before==after
    assert html_matches, 'Rebuilt HTML feature table differs from saved report'
    assert all(not x['differences'] for x in comparisons), 'Saved feature rows differ from aggregation reconstruction'
    assert all(x['matches_saved'] for x in csv_comparisons), 'Rebuilt report differs from saved CSVs'
    print(json.dumps(result,indent=2))

if __name__ == '__main__':
    main()
