"""Offline synthetic scoring; local annotations are not live-provider evidence."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest
import codex_antigravity_auth
from codex_antigravity_auth.skills.anti.scripts.anti_lib import benchmark as b


@pytest.fixture
def data():return b.corpus()


def complete(data):
    replay=b.template(data)
    for arm in replay['arms']:
        arm['model']=arm['id']+'-fixture-model';arm['provider']='fixture-provider'
        for row in arm['cases']:
            row.update(status='completed',submittedCalls=1,requestedOutputTokens=128,latencyMs=20,estimatedInputTokens=300,
                       observedInputTokens=250,observedOutputTokens=40)
    return replay


def empty_annotations(data):
    return {'schemaVersion':1,'kind':'anti-benchmark-adjudications','corpusSha256':data['corpusSha256'],'records':[]}


def add_finding(replay,arm=0,case=0,identifier='finding-a'):
    row=replay['arms'][arm]['cases'][case]
    value={'id':identifier,'file':row['scope']['files'][0],'line':2,'claim':'Synthetic finding claim.','severity':'critical'}
    row['findings'].append(value);return value


def annotate(data,replay,local,*,arm=0,case=0,finding=0,verdict='confirmed',defect=None):
    row=replay['arms'][arm]['cases'][case];item=row['findings'][finding];label=data['cases'][case]
    detail='Independent fixture inspection: boundary behavior checked against the pinned contract and behavioral oracle.'
    value={'armId':replay['arms'][arm]['id'],'caseId':row['caseId'],'findingId':item['id'],
           'findingSha256':b.digest(item),'sourceSha256':label['sourceSha256'],'verdict':verdict,
           'defectId':defect or (label['defect']['id'] if label['defect'] and verdict=='confirmed' else None),
           'reviewer':'synthetic-local-reviewer','evidence':{'kind':'reproduction','detail':detail,'sha256':b.digest(detail),'independent':True}}
    local['records'].append(value);return value


def test_corpus_labels_have_independent_behavioral_assertions(data):
    # Execute only our shipped, source-controlled synthetic fixture code, never
    # a replay/user file. These independent assertions establish the labels.
    expected={'expiry-boundary':1,'utf8-budget':1,'false-default':1,'expiry-equivalent':0,'utf8-equivalent':0}
    assert {case['id'] for case in data['cases']}==set(expected)
    for case in data['cases']:
        before={};after={}
        exec(compile(case['before'],case['id']+'-before','exec'),before)
        exec(compile(case['after'],case['id']+'-after','exec'),after)
        failures=0
        for probe in case['probes']:
            assert before[case['function']](*probe['args'])==probe['expected']
            failures+=after[case['function']](*probe['args'])!=probe['expected']
        assert failures==expected[case['id']]
        assert bool(failures)==bool(case['defect'])
    assert any(c['defect'] for c in data['cases'] if c['split']=='holdout')
    assert any(c['defect'] is None for c in data['cases'] if c['split']=='holdout')


def test_source_prompt_and_scope_are_deterministic_content_addresses(data):
    assert data==b.corpus() and data['sourceRevisionKind']=='sha256_fixture_snapshot'
    for case in data['cases']:
        assert case['promptSha256']==b.digest(case['prompt'])
        assert case['candidateSha256']==b.digest(case['after'])
        assert case['scopeSha256']==b.digest(case['scope'])
        assert case['verificationSha256']==b.digest(case['probes'])
        assert case['after'] in case['prompt'] and case['diff'] in case['prompt']


def test_unexecuted_template_never_claims_route_availability_or_scores(data):
    report=b.evaluate(b.template(data),empty_annotations(data),data)
    assert report['status']=='inconclusive' and not report['liveExecutionVerified']
    assert all(a['status']=='unavailable' for a in report['arms'])
    assert report['comparisons'][0]['matchedCases']==[]
    assert report['arms'][0]['development']['scoredCases']==0


def test_verified_detection_false_positive_and_holdout_are_separate(data):
    replay=complete(data);local=empty_annotations(data)
    add_finding(replay);annotate(data,replay,local)
    add_finding(replay,case=3);annotate(data,replay,local,case=3,verdict='rejected')
    add_finding(replay,case=2);annotate(data,replay,local,case=2)
    report=b.evaluate(replay,local,data)
    assert report['status']=='scored'
    dev=report['arms'][0]['development'];holdout=report['arms'][0]['publicHoldout']
    assert (dev['verifiedDetections'],dev['expectedDefects'],dev['falseNegatives'],dev['verifiedFalsePositives'])==(1,2,1,1)
    assert dev['verifiedSeverity']=={'high':1}  # model's critical claim is not ground truth
    assert holdout['verifiedDetections']==1 and holdout['cleanControls']==1
    assert len(report['comparisons'][0]['matchedCases'])==5
    assert any(a['kind']=='arm_disagreement' for a in report['audits'])
    assert report['arms'][1]['development']['falseNegatives']==2
    assert not report['routingChanged'] and not report['billingObserved']


def test_duplicate_confirmations_do_not_inflate_recall(data):
    replay=complete(data);local=empty_annotations(data)
    add_finding(replay);add_finding(replay,identifier='duplicate')
    annotate(data,replay,local);annotate(data,replay,local,finding=1)
    row=b.evaluate(replay,local,data)['arms'][0]['cases'][0]
    assert row['detections']==1 and row['duplicateDetections']==1


def test_unadjudicated_claim_is_inconclusive_not_false_positive_or_miss(data):
    replay=complete(data);add_finding(replay)
    report=b.evaluate(replay,empty_annotations(data),data)
    row=report['arms'][0]['cases'][0]
    assert row['status']=='inconclusive' and row['unresolvedFindings']==1 and row['falsePositives']==0
    assert data['cases'][0]['id'] in report['comparisons'][0]['excludedCases']
    assert report['arms'][0]['development']['expectedDefects']==1


def test_conflict_with_no_defect_control_requires_label_audit(data):
    replay=complete(data);local=empty_annotations(data)
    add_finding(replay,case=3);annotate(data,replay,local,case=3,defect='new-claimed-defect')
    report=b.evaluate(replay,local,data)
    assert report['arms'][0]['cases'][3]['status']=='inconclusive'
    assert report['audits'][0]['kind']=='label_conflict'
    assert report['arms'][0]['development']['verifiedFalsePositives']==0


@pytest.mark.parametrize('change',['source','prompt','scope','coverage','effort','budget','calls','outputs','inputs','missing','unknown'])
def test_non_equivalent_arms_are_invalid_and_excluded_from_comparisons(data,change):
    replay=complete(data);arm=replay['arms'][0];row=arm['cases'][0]
    if change in ('source','prompt','scope'):row[change+'Sha256']='0'*64
    elif change=='coverage':row['scope']['omittedFiles']=['fixture.py'];row['scope']['complete']=False
    elif change=='effort':arm['settings']['effort']='high'
    elif change=='budget':arm['settings']['maxCalls']=2
    elif change=='calls':row['submittedCalls']=2
    elif change=='outputs':row['requestedOutputTokens']=2049
    elif change=='inputs':row['estimatedInputTokens']=8193
    elif change=='missing':arm['cases'].pop()
    else:row['caseId']='not-in-corpus'
    report=b.evaluate(replay,empty_annotations(data),data)
    assert report['status']=='invalid' and report['arms'][0]['status']=='invalid'
    assert report['comparisons'][0]['matchedCases']==[]
    if change=='coverage':assert report['arms'][0]['cases'][0]['omittedFiles']==1


def test_unknown_observed_usage_is_not_zero_and_estimates_remain_separate(data):
    replay=complete(data);replay['arms'][0]['cases'][0]['observedInputTokens']=None
    report=b.evaluate(replay,empty_annotations(data),data)
    metric=report['arms'][0]['development']
    assert metric['observedInputTokens']=={'knownTotal':500,'unknownCount':1}
    assert metric['estimatedInputTokens']=={'knownTotal':900,'unknownCount':0}
    assert metric['latencyMs']=={'knownTotal':60,'unknownCount':0}


@pytest.mark.parametrize('field',['findingSha256','sourceSha256','corpusSha256','reviewer','evidence','embedded'])
def test_self_grading_and_stale_evidence_cannot_be_accepted(data,field):
    replay=complete(data);local=empty_annotations(data);finding=add_finding(replay);record=annotate(data,replay,local)
    if field in ('findingSha256','sourceSha256'):record[field]='0'*64
    elif field=='corpusSha256':local[field]='0'*64
    elif field=='reviewer':record[field]=replay['arms'][0]['model']
    elif field=='evidence':record['evidence']['independent']=False
    else:finding['verdict']='confirmed'
    with pytest.raises(b.BenchmarkError):b.evaluate(replay,local,data)


def test_report_does_not_echo_claims_or_local_evidence_and_redacts_model_names(data):
    replay=complete(data);local=empty_annotations(data);finding=add_finding(replay)
    finding['claim']='sk-fixtureabcdefghijklmnopqrstuvwxyz'
    replay['arms'][0]['model']='sk-fixtureabcdefghijklmnopqrstuvwxyz'
    annotate(data,replay,local)
    report=b.evaluate(replay,local,data);raw=json.dumps(report)
    assert 'sk-fixtureabcdefghijklmnopqrstuvwxyz' not in raw
    assert 'Independent fixture inspection:' not in raw
    assert 'findingSha256' in raw and 'evidenceSha256' in raw


@pytest.mark.parametrize('raw',['{"a":1,"a":2}','{"n":NaN}','not json'])
def test_reader_rejects_duplicate_nonfinite_and_invalid_json(tmp_path,raw):
    path=tmp_path/'input.json';path.write_text(raw)
    with pytest.raises(b.BenchmarkError):b.read_json(path)


def test_bounded_reader_rejects_special_symlink_and_oversized_file(tmp_path):
    path=tmp_path/'input.json';path.write_text('{}')
    link=tmp_path/'link.json';link.symlink_to(path)
    for selected in (link,tmp_path):
        with pytest.raises(b.BenchmarkError):b.read_json(selected)
    path.write_bytes(b' '*(b.MAX_FILE_BYTES+1))
    with pytest.raises(b.BenchmarkError):b.read_json(path)


@pytest.fixture
def anti(monkeypatch,tmp_path):
    script=Path(codex_antigravity_auth.__file__).parent/'skills/anti/scripts/anti.py'
    spec=importlib.util.spec_from_file_location('anti_benchmark_fixture',script)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module,'_install_run_signal_handlers',lambda args:None)
    monkeypatch.setattr(module,'request_json',lambda *a,**k:pytest.fail('benchmark must not use HTTP'))
    monkeypatch.setattr(module,'write_run_record',lambda *a,**k:pytest.fail('benchmark must not touch run state'))
    monkeypatch.chdir(tmp_path)
    return module


def test_cli_corpus_template_and_replay_are_offline_and_no_clobber(anti,data,tmp_path,capsys):
    assert anti.main(['benchmark','corpus'])==0
    assert json.loads(capsys.readouterr().out)==data
    replay=tmp_path/'replay.json';local=tmp_path/'evidence.json';output=tmp_path/'report.json'
    assert anti.main(['benchmark','template','--output',str(replay)])==0
    local.write_text(json.dumps(empty_annotations(data)))
    assert anti.main(['benchmark','replay','--replay',str(replay),'--adjudications',str(local),'--output',str(output)])==2
    first=output.read_bytes();assert json.loads(first)['status']=='inconclusive'
    assert anti.main(['benchmark','template','--output',str(output)])==1
    assert output.read_bytes()==first
    replay.write_text(json.dumps(complete(data)))
    assert anti.main(['benchmark','replay','--replay',str(replay),'--adjudications',str(local)])==0
    assert json.loads(capsys.readouterr().out)['status']=='scored'


def test_published_schemas_accept_corpus_template_local_evidence_and_report(data):
    import jsonschema
    root=Path(codex_antigravity_auth.__file__).parent/'skills/anti/schemas'
    replay=complete(data);local=empty_annotations(data);add_finding(replay);annotate(data,replay,local)
    for name,value in [('replay',replay),('adjudications',local),('report',b.evaluate(replay,local,data))]:
        schema=json.loads((root/('benchmark-'+name+'-v1.json')).read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(value,schema)
    replay['arms'][0]['cases'][0]['findings'][0]['verdict']='confirmed'
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate(replay,json.loads((root/'benchmark-replay-v1.json').read_text()))


def test_one_unavailable_arm_does_not_hide_available_pair_comparison(data):
    replay=complete(data)
    unavailable=b.template(data)['arms'][0];unavailable['id']='unavailable-arm'
    replay['arms'].append(unavailable)
    report=b.evaluate(replay,empty_annotations(data),data)
    assert report['status']=='inconclusive'
    assert len(report['comparisons'][0]['matchedCases'])==5
    assert all(pair['matchedCases']==[] for pair in report['comparisons'][1:])


def test_failed_and_missing_observations_are_explicit_not_zero_latency(data):
    replay=complete(data);row=replay['arms'][0]['cases'][0]
    row.update(status='failed',latencyMs=None,observedInputTokens=None,observedOutputTokens=None)
    report=b.evaluate(replay,empty_annotations(data),data)
    result=report['arms'][0]['cases'][0]
    assert result['status']=='failed' and result['latencyMs'] is None
    assert result['caseId'] in report['comparisons'][0]['excludedCases']


@pytest.mark.parametrize('bad',[True,-1,1.5,float('inf'),'1'])
def test_typed_counter_rejects_bool_negative_nonfinite_or_string(data,bad):
    replay=complete(data);replay['arms'][0]['cases'][0]['latencyMs']=bad
    with pytest.raises(b.BenchmarkError):b.evaluate(replay,empty_annotations(data),data)


def test_duplicate_arm_case_and_annotation_are_refused(data):
    for kind in ('arm','case','annotation'):
        replay=complete(data);local=empty_annotations(data)
        if kind=='arm':replay['arms'].append(deepcopy(replay['arms'][0]))
        elif kind=='case':replay['arms'][0]['cases'].append(deepcopy(replay['arms'][0]['cases'][0]))
        else:
            add_finding(replay);annotate(data,replay,local);local['records']*=2
        with pytest.raises(b.BenchmarkError):b.evaluate(replay,local,data)


def test_installed_standalone_replay_needs_no_package_or_site_packages(tmp_path,data):
    import shutil
    import subprocess
    import sys
    package=Path(codex_antigravity_auth.__file__).parent
    skill=tmp_path/'anti'
    shutil.copytree(package/'skills/anti',skill,ignore=shutil.ignore_patterns('__pycache__'))
    replay=tmp_path/'replay.json';local=tmp_path/'local.json'
    replay.write_text(json.dumps(complete(data)));local.write_text(json.dumps(empty_annotations(data)))
    # Startup guard stays loaded; remove package paths only after isolation.
    bootstrap=tmp_path/'bootstrap.py'
    bootstrap.write_text('import sys, runpy, importlib.util\n'
        + 'sys.path = [p for p in sys.path if "site-packages" not in p and not p.startswith('+repr(str(package.parent))+')]\n'
        + 'class BlockPackage:\n    def find_spec(self, name, *args):\n        if name == "codex_antigravity_auth" or name.startswith("codex_antigravity_auth."):\n            raise ModuleNotFoundError("Package deliberately unavailable in standalone test")\n'
        + 'sys.meta_path.insert(0, BlockPackage())\n'
        + 'sys.argv = '+repr([str(skill/'scripts/anti.py'),'benchmark','replay','--replay',str(replay),'--adjudications',str(local)])+'\n'
        + 'runpy.run_path(sys.argv[0], run_name="__main__")\n')
    result=subprocess.run([sys.executable,str(bootstrap)],cwd=tmp_path,text=True,capture_output=True,timeout=30)
    assert result.returncode==0,result.stderr
    report=json.loads(result.stdout)
    assert report['status']=='scored' and report['corpusSha256']==data['corpusSha256']
