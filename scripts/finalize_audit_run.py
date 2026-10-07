#!/usr/bin/env python3
"""Repair recorded evidence paths, rebuild reports and check actual pilot outputs.

The existing runner records temporary paths before atomically moving project folders.
This post-processing preserves affected originals and repairs paths only, without
changing any observations, labels, measurements, rule hashes or collection dates.
"""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / 'src'))
from dpi_audit.codebook import load_codebook
from dpi_audit.pipeline import code_hash
from dpi_audit.report import build

def csv_rows(path):
    with path.open(newline='') as handle:
        return list(csv.DictReader(handle))

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def repair_paths(results, projects):
    repairs = []
    archive = results / 'evidence_path_originals'
    for pid in projects:
        folder = results / 'projects' / pid
        prefix = re.compile(re.escape(str((results / 'projects').resolve()))
                            + r'/\.tmp-' + re.escape(pid) + r'-[^/\\\s\"]+')
        for path in sorted(folder.glob('*')):
            if path.suffix not in ('.json', '.jsonl'):
                continue
            original = path.read_text()
            matches = sorted(set(prefix.findall(original)))
            if not matches:
                continue
            updated = original
            for old in matches:
                updated = updated.replace(old, str(folder.resolve()))
            backup = archive / pid / path.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            if backup.exists():
                raise RuntimeError(f'Preserve existing backup before repairing again: {backup}')
            shutil.copy2(path, backup)
            before = digest(path)
            path.write_text(updated)
            repairs.append({'project_id': pid, 'file': str(path.relative_to(results)),
                            'original_sha256': before, 'repaired_sha256': digest(path),
                            'old_prefixes': matches, 'new_prefix': str(folder.resolve()),
                            'original_copy': str(backup.relative_to(results))})
    manifest = results / 'evidence_path_repair.json'
    if repairs or not manifest.exists():
        manifest.write_text(json.dumps({'performed_utc': datetime.now(timezone.utc).isoformat(),
            'operation': 'Evidence path prefixes only; original data preserved; no measurement changes',
            'repairs': repairs}, indent=2) + '\n')
    return repairs

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--results', required=True, type=Path)
    results = parser.parse_args().results.resolve()
    manifest = json.loads((results / 'run_manifest.json').read_text())
    assert manifest.get('finished'), 'Collection must finish before finalization'
    projects = manifest['projects']
    assert len(projects) == len(set(projects))
    repairs = repair_paths(results, projects)
    report = build(results)
    codebook = load_codebook()
    feature_count = len(codebook.features)
    assert codebook.content_hash == manifest['codebook']['content_sha256']
    assert code_hash() == manifest['code_hash'], 'Detector code changed during collection'
    features = csv_rows(results / 'project_features.csv')
    primary = [r for r in features if r['scope'] == 'project']
    summaries = csv_rows(results / 'feature_summary.csv')
    inventory = csv_rows(results / 'project_inventory.csv')
    assert len(primary) == len(projects) * feature_count
    assert len({(r['project_id'], r['feature_id']) for r in primary}) == len(primary)
    for pid in projects:
        assert {r['feature_id'] for r in primary if r['project_id'] == pid} == set(codebook.ids())
    assert {r['project_id'] for r in inventory} == set(projects)
    assert len(summaries) == feature_count
    for row in summaries:
        matching = [r for r in primary if r['feature_id'] == row['feature_id']]
        av = Counter(r['availability'] for r in matching)
        ve = Counter(r['verification'] for r in matching)
        for category in ('present', 'not_found_in_scope', 'unknown', 'not_applicable'):
            assert int(row[f'n_{category}']) == av[category]
        for category in ('pass', 'partial', 'fail', 'not_tested'):
            assert int(row[f'n_{category}']) == ve[category]
        for denominator, percent, numerator, total in (
            ('availability_denominator', 'pct_present_of_assessable', av['present'], av['present']+av['not_found_in_scope']),
            ('verification_denominator', 'pct_pass_of_verified', ve['pass'], ve['pass']+ve['partial']+ve['fail'])):
            assert int(row[denominator]) == total
            assert float(row[percent]) == round(100*numerator/total, 1) if total else row[percent] == ''
    obs_ids = set()
    methods = Counter()
    source_review_flags = []
    with (results / 'observations.jsonl').open() as handle:
        for line in handle:
            row = json.loads(line)
            assert row['obs_id'] not in obs_ids
            obs_ids.add(row['obs_id'])
            methods[row['method']] += 1
            if row['method'] == 'static':
                for evidence in row.get('evidence', []):
                    path = evidence.get('path', '')
                    if re.search(r'(^|/)(tests?|fixtures?|__tests__|examples?|samples?)(/|$)', path, re.I):
                        source_review_flags.append({'project_id': row['project_id'], 'obs_id': row['obs_id'],
                            'feature_id': row['feature_id'], 'path': path,
                            'line_start': evidence.get('line_start', ''), 'line_end': evidence.get('line_end', ''),
                            'reason': 'Test/example/fixture path: review before using as production feature evidence'})
    assert len(obs_ids) == report['observations']
    provenance = json.loads((results / 'inputs/provenance.json').read_text())
    commits = {r['project_id']: r['commit'] for r in provenance['projects']}
    assert digest(WORKSPACE / 'list.txt') == provenance['original_list_sha256']
    assert digest(WORKSPACE / 'metadata.yaml') == manifest['metadata']['sha256']
    evidence_files_checked = 0
    coverage = []
    for pid in projects:
        folder = results / 'projects' / pid
        inv = json.loads((folder / 'inventory.json').read_text())
        sources = [i for i in inv['interfaces'] if i['kind'] == 'source']
        assert all(i.get('commit') == commits[pid] and not i.get('dirty') for i in sources)
        for row in json.loads((folder / 'pages.json').read_text()):
            for key, value in row.items():
                if isinstance(value, str) and value.startswith(str(folder / 'evidence')):
                    assert Path(value).is_file(), (pid, key, value)
                    evidence_files_checked += 1
        coverage.extend({'project_id': pid, 'status': i['status'], 'reason': i.get('reason'),
                         'coverage': i.get('coverage'), 'errors': i.get('errors'),
                         'states_skipped': i.get('states_skipped')}
                        for i in inv['interfaces'] if i['kind'] == 'web')
    review = csv_rows(results / 'manual_review.csv')
    with (results / 'static_test_example_evidence.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=['project_id', 'obs_id', 'feature_id', 'path', 'line_start', 'line_end', 'reason'])
        writer.writeheader()
        writer.writerows(source_review_flags)
    human_fields = [k for k in (review[0] if review else []) if k.startswith(('reviewer_', 'adjudicat'))]
    reviewed = sum(any(r.get(k) for k in human_fields) for r in review)
    interruption_path = results / 'diagnostics/peso_watchdog_result.json'
    interruption = json.loads(interruption_path.read_text()) if interruption_path.exists() else None
    interruptions = [interruption] if interruption and interruption['action'].startswith('owned stalled browser closed') else []
    summary = {'run_label': 'pilot', 'projects': len(projects),
        'project_statuses': dict(Counter(r['audit_status'] for r in inventory)),
        'source_files_inspected': sum(int(r['static_files_inspected']) for r in inventory),
        'pages_rendered': sum(int(r['pages_rendered']) for r in inventory),
        'observations': len(obs_ids), 'observation_methods': dict(methods),
        'project_feature_rows': len(primary), 'interface_feature_rows': len(features)-len(primary),
        'feature_summary_rows': len(summaries), 'review_tasks': len(review), 'human_reviewed_tasks': reviewed,
        'static_test_example_evidence_references': len(source_review_flags),
        'evidence_paths_checked': evidence_files_checked,
        'runtime_statuses': dict(Counter(r['status'] for r in coverage)),
        'collection_interruptions': interruptions,
        'detector_semantics_validation': 'Pending; see detector_review_flags.md, including NAV-12 error-as-pass and NAV-14 missing specifications',
        'checks': 'Unique IDs, commits, original list hash, feature counts, denominators, percentages, observation IDs and page evidence paths passed',
        'limitations': 'Draft instrument; no human validation or adoption measurement; source commits are upstream-only; review aggregation and coverage before publication',
        'web_interfaces': coverage}
    (results / 'execution_summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    table = '\n'.join(f"| {r['project_id']} | {r['website_status']} | {r['pages_rendered']} | {r['static_files_inspected']} |"
                      for r in inventory)
    interruption_note = (
        'PESO collection stalled in a Playwright wait. Its owned browser was closed after the '
        '1200-second interface budget plus startup grace, preserving partial observations. '
        'This is an instrument/collection interruption, not proof that the service is unavailable. '
        'See `diagnostics/peso_watchdog_result.json` and the stack dump; manual diagnosis and '
        'a documented rerun are pending.\n\n' if interruptions else '')
    (results / 'README.md').write_text(
        '# India interface audit — provisional pilot\n\n'
        f"The existing source-plus-browser pipeline processed {len(projects)} projects, inspected "
        f"{summary['source_files_inspected']:,} source files, rendered {summary['pages_rendered']} pages "
        f"and recorded {summary['observations']:,} observations. The page cap was 15 per interface.\n\n"
        f"The codebook remains draft and unfrozen. There are {len(review)} queued review tasks; "
        f"{reviewed} have human judgments. These are provisional machine results, not validated "
        'WCAG conformance or measured adoption. Failed/blocked collection is retained explicitly.\n\n'
        + interruption_note + '| Project | Runtime status | Pages rendered | Source files inspected |\n'
        '|---|---|---:|---:|\n' + table + '\n\n'
        'Open `summary.html` for the report, `feature_summary.csv` for feature-level denominators, '
        '`project_features.csv` for source/runtime distinctions, `pages.csv` for collection coverage, '
        'and `manual_review.csv` for the reviewer queue. Project folders contain raw findings and evidence.\n\n'
        '`static_test_example_evidence.csv` flags source evidence from test/example/fixture paths. '
        'Such paths need review before they support production-feature claims; automated labels are preserved.\n\n'
        '**Read `detector_review_flags.md` before interpreting percentages.** In particular, the current '
        'NAV-12 rule can count request errors as passes, and NAV-14 marks missing journey specifications '
        'as not-applicable. These provisional outputs need detector calibration and human review.\n\n'
        'The runner recorded temporary evidence paths before moving result folders. '
        '`evidence_path_repair.json` documents prefix-only repairs and hashes; affected originals '
        'are preserved in `evidence_path_originals/`. Feature labels and measurements were unchanged. '
        'The missing report-support methodology document was restored before reporting; '
        'no detector rules or codebook were changed.\n\n'
        'The manuscript and its ZIP predate this pilot and have not been rewritten to claim validated findings. '
        'Author review and manuscript integration remain pending.\n\n'
        'Resume collection from the workspace with:\n\n```sh\n'
        '.venv/bin/dpi-audit audit --input results/india-pilot/inputs/list.local.txt '
        '--output results/india-pilot --mode hybrid --config config/india_local_pilot.yaml '
        '--metadata metadata.yaml --resume\n```\n\n'
        'After any new collection, run `.venv/bin/python scripts/finalize_audit_run.py '
        '--results results/india-pilot`.\n')
    print(json.dumps({k:v for k,v in summary.items() if k != 'web_interfaces'}, indent=2))

if __name__ == '__main__':
    main()
