"""New targeted verification tests, not evidence of validation before the pilot."""
from types import SimpleNamespace
from unittest.mock import patch
import time
import pytest
from dpi_audit.aggregate import combine_availability, combine_verification
from dpi_audit.config import load_config
from dpi_audit.observations import Observation
from dpi_audit.runtime.runner import InterfaceRunner

def ob(availability='present', verification='not_tested'):
    return Observation('fixture','fixture::web','service_ui','KBD-01','fixture-check','runtime',
                       'direct_observation',availability,verification,'fixture',{'kind':'page'})

def test_untested_does_not_become_failure():
    status, counts = combine_verification([ob(verification='pass'),ob(availability='unknown')], 'mixed_is_partial')
    assert status=='pass' and counts['units_tested']==1 and counts['units_not_tested']==1

def test_mixed_page_outcomes_are_partial():
    assert combine_verification([ob(verification='pass'),ob(verification='fail')],'mixed_is_partial')[0]=='partial'

def test_critical_rule_retains_failure():
    assert combine_verification([ob(verification='pass'),ob(verification='fail')],'any_fail_is_fail')[0]=='fail'

def test_unknown_not_converted_to_absence():
    assert combine_availability([ob(availability='unknown')])[0]=='unknown'

def test_presence_retains_mixed_availability():
    assert combine_availability([ob(),ob(availability='not_found_in_scope')])[0]=='present'

def runner():
    r=InterfaceRunner.__new__(InterfaceRunner)
    r.pid='fixture';r.iface=SimpleNamespace(interface_id='fixture::web');r.itype='service_ui'
    r.url='http://127.0.0.1/';r.cfg=load_config();r.rt=r.cfg['runtime'];r.meta={};r.obs=[]
    r.states_run={};r.states_skipped={};r.t0=time.time();r.budget=1200
    r.sess=SimpleNamespace(policy=SimpleNamespace(excluded=lambda u:False))
    return r

@pytest.mark.xfail(strict=True,raises=AssertionError,reason='Known NAV-12 defect: a request error is counted as a passing checked link')
def test_request_error_must_not_be_link_pass():
    r=runner();target='http://127.0.0.1/missing'
    snaps={r.url:{'links':[{'abs':target,'internal':True,'download':False}]}}
    with patch('dpi_audit.runtime.runner.C.link_check',return_value={target:{'class':'error','status':None}}):
        r.link_checks(None,snaps)
    assert next(o for o in r.obs if o.feature_id=='NAV-12').verification!='pass'

@pytest.mark.xfail(strict=True,raises=AssertionError,reason='Known NAV-14 missingness issue: missing specification is classified not_applicable')
def test_unspecified_journey_is_unknown():
    r=runner();r.journeys(None,r.url)
    assert r.obs[0].availability=='unknown'
