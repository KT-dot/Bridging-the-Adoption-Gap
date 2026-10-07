"""Run updated CLI on local fixtures, then compare known behaviors; no government requests."""
from pathlib import Path
import json
import subprocess
import sys
from dpi_audit.fixtures import serve

OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[1]
old=ROOT/'results/code-verification-20261007'
servers=serve(port_base=19700)
try:
    command=[str(ROOT/'.venv/bin/dpi-audit'),'audit','--input',str(old/'fixture_input.txt'),
             '--output',str(OUT/'hybrid_smoke'),'--mode','hybrid','--config',str(old/'fixture_config.yaml'),
             '--metadata',str(old/'fixture_metadata.yaml')]
    with (OUT/'hybrid_smoke.log').open('w') as log:
        result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,timeout=300)
    assert result.returncode==0
    cases=json.loads((old/'fixture_assertions.json').read_text())
    for case in cases:
        folder=OUT/'hybrid_smoke/projects'/f"fixture-{case['fixture']}"
        if case['field']=='runtime_status':
            inv=json.loads((folder/'inventory.json').read_text())
            actual=next(i['status'] for i in inv['interfaces'] if i['kind']=='web')
        else:
            features=json.loads((folder/'features.json').read_text())
            row=next(r for r in features if r['scope']=='project' and r['feature_id']==case['feature_id'])
            actual=row[case['field']]
        case['actual']=actual;case['passed']=actual==case['expected']
    (OUT/'hybrid_smoke_assertions.json').write_text(json.dumps(cases,indent=2)+'\n')
    assert all(c['passed'] for c in cases)
    print(f'{len(cases)} local hybrid behavior checks passed.')
finally:
    for _,_,server in servers:
        server.shutdown();server.server_close()
