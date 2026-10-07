#!/usr/bin/env python3
"""Clone the repositories parsed by the existing auditor; never run project code."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import csv
import os
import subprocess
import sys

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / 'src'))
from dpi_audit.inputs import parse_list

DESTINATION = WORKSPACE / 'repositories'
FIELDS = ['project_id', 'repository_url', 'local_path', 'status', 'commit',
          'branch', 'shallow', 'submodules_verified', 'observed_utc', 'log_path', 'error']

def git(*args, cwd=None):
    return subprocess.run(['git', *args], cwd=cwd, text=True,
                          capture_output=True, check=True).stdout.strip()

def clone(project):
    source = project.sources[0].location
    url = source.url
    folder = DESTINATION / project.project_id
    log = DESTINATION / 'clone_logs' / f'{project.project_id}.log'
    record = dict.fromkeys(FIELDS, '')
    record.update(project_id=project.project_id, repository_url=url,
                  local_path=str(folder), log_path=str(log))
    print(f'Starting {project.project_id}: {url}', flush=True)
    try:
        with log.open('a') as stream:
            if folder.exists():
                origin = git('remote', 'get-url', 'origin', cwd=folder)
                if origin.rstrip('/').removesuffix('.git') != url.rstrip('/').removesuffix('.git'):
                    raise ValueError('Existing repository has a different origin; preserved unchanged')
                git('rev-parse', 'HEAD', cwd=folder)
                command = ['git', '-C', str(folder), 'submodule', 'update', '--init',
                           '--recursive', '--depth', '1']
            else:
                command = ['git', 'clone', '--depth', '1', '--recurse-submodules',
                           '--shallow-submodules', url, str(folder)]
            environment = dict(os.environ, GIT_TERMINAL_PROMPT='0')
            subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
                           env=environment, check=True, timeout=1800)
        record['commit'] = git('rev-parse', 'HEAD', cwd=folder)
        record['branch'] = git('branch', '--show-current', cwd=folder)
        record['shallow'] = git('rev-parse', '--is-shallow-repository', cwd=folder)
        submodules = git('submodule', 'status', '--recursive', cwd=folder)
        if any(line.startswith(('-', '+', 'U')) for line in submodules.splitlines()):
            raise ValueError('Submodule checkout is incomplete or differs from its recorded commit')
        record['submodules_verified'] = 'yes' if submodules else 'none'
        record['status'] = 'cloned_and_verified'
    except Exception as error:
        record['status'] = 'failed'
        record['error'] = str(error)
    record['observed_utc'] = datetime.now(timezone.utc).isoformat()
    print(f"{record['status']}: {project.project_id} {record['commit']}", flush=True)
    return record

def save(records):
    temporary = DESTINATION / 'clone_manifest.csv.tmp'
    with temporary.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(sorted(records, key=lambda row: row['project_id']))
    temporary.replace(DESTINATION / 'clone_manifest.csv')

def main():
    parsed = parse_list(WORKSPACE / 'list.txt')
    if not parsed.ok:
        raise ValueError(parsed.errors)
    if any(len(project.sources) != 1 or not project.sources[0].location.url
           for project in parsed.projects):
        raise ValueError('Expected exactly one remote repository per project')
    DESTINATION.mkdir(exist_ok=True)
    (DESTINATION / 'clone_logs').mkdir(exist_ok=True)
    (DESTINATION / 'README.md').write_text(
        '# Repositories from list.txt\n\n'
        'One checkout per project ID, parsed with the existing auditor. '
        'Clones include the default branch tip and recursively initialized submodules. '
        'History is shallow (`--depth 1`); no project dependencies were installed or applications run.\n\n'
        '`clone_manifest.csv` records origins, exact commits, branches and verification status. '
        '`clone_logs/` preserves transfer logs. Upstream commits do not establish deployed versions.\n\n'
        'To obtain full history for a checkout, run `git -C <project-folder> fetch --unshallow`.\n')
    records = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(clone, project) for project in parsed.projects]
        for future in as_completed(futures):
            records.append(future.result())
            save(records)
    failed = sum(row['status'] == 'failed' for row in records)
    print(f'Finished: {len(records) - failed}/{len(records)} verified in {DESTINATION}', flush=True)
    return bool(failed)

if __name__ == '__main__':
    sys.exit(main())
