"""Infrastructure regression tests added after the India pilot; no live websites."""
import csv
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dpi_audit.batch import _bounded_futures, _execute_worker
from dpi_audit.config import load_config
from dpi_audit.report import _write_csv, measurement_rows
from dpi_audit.util import iter_jsonl


def test_csv_stream_keeps_late_columns_and_cells(tmp_path):
    def rows():
        yield {'a':True, 'nullable':None}
        yield {'a':False, 'late':{'x':1}}
    path=tmp_path/'rows.csv'
    assert _write_csv(path,rows())==2
    with path.open(newline='') as fh:
        reader=csv.DictReader(fh)
        actual=list(reader)
    assert reader.fieldnames==['a','nullable','late']
    assert actual==[{'a':'true','nullable':'','late':''},
                    {'a':'false','nullable':'','late':'{"x": 1}'}]


def test_failed_csv_stream_preserves_existing_output(tmp_path):
    path=tmp_path/'rows.csv';path.write_text('original\n')
    def broken():
        yield {'a':1}
        raise RuntimeError('input interrupted')
    with pytest.raises(RuntimeError):
        _write_csv(path,broken())
    assert path.read_text()=='original\n'
    assert not path.with_suffix('.csv.tmp').exists()


def test_jsonl_reads_incrementally(tmp_path):
    path=tmp_path/'rows.jsonl'
    path.write_text('{"a": 1}\nmalformed tail\n')
    rows=iter_jsonl(path)
    assert next(rows)=={'a':1}
    with pytest.raises(ValueError):
        next(rows)


def test_measurements_are_lazy_and_skip_boolean(tmp_path):
    path=tmp_path/'observations.jsonl'
    path.write_text('{"project_id":"x","interface_id":"x::web","feature_id":"RES-01",'
                    '"check_id":"fixture","method":"runtime","obs_id":"o",'
                    '"measurements":{"kb":12,"flag":true},"unit":{}}\n')
    rows=measurement_rows([{'dir':tmp_path}], 'fixture')
    assert iter(rows) is rows
    actual=list(rows)
    assert len(actual)==1 and actual[0]['metric']=='kb' and actual[0]['value']==12


def test_pending_futures_are_bounded():
    class Counted:
        def __init__(self): self.submitted=0
        def submit(self, fn, item):
            self.submitted+=1
            return pool.submit(fn,item)
    with ThreadPoolExecutor(max_workers=3) as pool:
        counted=Counted()
        results=_bounded_futures(counted,lambda x:x*x,range(100),3)
        first,_=next(results)
        assert counted.submitted==3
        actual=[first.result()]+[future.result() for future,_ in results]
    assert sorted(actual)==[x*x for x in range(100)]


@pytest.mark.parametrize('overrides',[
    {'run':{'workers':0}}, {'run':{'project_timeout_s':0}}, {'run':{'project_timeout_s':0.5}}])
def test_invalid_resource_limits_rejected(overrides):
    with pytest.raises(ValueError):load_config(None,overrides)


@pytest.mark.skipif(os.name!='posix',reason='POSIX process-group cleanup test')
def test_timeout_stops_child_that_ignores_termination(tmp_path):
    pidfile=tmp_path/'child.pid'
    child="import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)"
    parent=("import subprocess,sys,time,pathlib;"
            f"p=subprocess.Popen([sys.executable,'-c',{child!r}]);"
            f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid));time.sleep(120)")
    with (tmp_path/'worker.log').open('w') as log:
        with pytest.raises(subprocess.TimeoutExpired):
            _execute_worker([sys.executable,'-c',parent],stdout=log,timeout=1,
                            cwd=str(tmp_path),env=dict(os.environ))
    childpid=int(pidfile.read_text())
    for _ in range(50):
        stat=Path(f'/proc/{childpid}/stat')
        if not stat.exists() or stat.read_text().split()[2]=='Z':
            break
        time.sleep(0.02)
    else:
        os.kill(childpid,signal.SIGKILL)
        pytest.fail('timed-out worker left a running child')
