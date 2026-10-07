"""Check known behaviors in local fixture outputs; no website requests."""
from pathlib import Path
import json

out=Path(__file__).resolve().parent
root=out/'fixture_run/projects'
def project(name):
    rows=json.loads((root/f'fixture-{name}'/'features.json').read_text())
    return {r['feature_id']:r for r in rows if r['scope']=='project'}

cases=[('working','LANG-02','verification','pass'),
       ('working','LANG-05','verification','pass'),
       ('partial','LANG-01','availability','present'),
       ('partial','LANG-02','verification','fail'),
       ('contrast','VIS-01','verification','fail'),
       ('contrast','VIS-04','verification','fail'),
       ('forms','FORM-05','verification','fail'),
       ('robots','VIS-01','availability','unknown'),
       ('robots','VIS-01','verification','not_tested')]
checks=[]
for name,fid,key,expected in cases:
    actual=project(name)[fid][key]
    checks.append({'fixture':name,'feature_id':fid,'field':key,'expected':expected,
                   'actual':actual,'passed':actual==expected})
robot=json.loads((root/'fixture-robots/inventory.json').read_text())
web=next(i for i in robot['interfaces'] if i['kind']=='web')
checks.append({'fixture':'robots','field':'runtime_status','expected':'blocked',
               'actual':web['status'],'passed':web['status']=='blocked'})
(out/'fixture_assertions.json').write_text(json.dumps(checks,indent=2)+'\n')
assert all(c['passed'] for c in checks)
print(f'{len(checks)} known-behavior checks passed across five local fixtures.')
