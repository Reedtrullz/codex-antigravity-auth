"""Synthetic static report inputs; never access user runs or launch model calls."""
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import codex_antigravity_auth


@pytest.fixture
def ui(monkeypatch,tmp_path):
    script=Path(codex_antigravity_auth.__file__).parent/'skills/anti/scripts/anti.py'
    spec=importlib.util.spec_from_file_location('anti_html_fixture',script)
    anti=importlib.util.module_from_spec(spec);spec.loader.exec_module(anti)
    from anti_lib import artifacts,html_report,reflections,reports
    monkeypatch.setattr(anti,'RUNS_DIR',tmp_path/'runs')
    monkeypatch.setattr(anti,'helper_identity',lambda:{'version':'synthetic'})
    monkeypatch.setattr(anti,'_install_run_signal_handlers',lambda args:None)
    monkeypatch.setattr(anti,'request_json',lambda *a,**k:pytest.fail('no HTML export network'))
    monkeypatch.setattr(reflections,'REFLECTIONS_DIR',tmp_path/'reflections')
    return anti,artifacts,html_report,reflections,reports,tmp_path


def publication(ui,run_id='left',mode='full',**changes):
    anti,*_=ui
    metadata={'scope_status':'partial','sourceCommit':'a'*40,'actual_models':['fixture-fallback'],
        'actual_providers':['fixture-provider'],'requested_models':['fixture-requested'],
        'panel_status':'partial_multi_model','omitted_files':['unreviewed.py'],
        'findings':{'findings':[{'id':'F1','claim':'Synthetic off-by-one concern','severity':'medium','file':'fixture.py','line':2,
            'evidence':'The endpoint allows one extra item.','checks':[]}],
            'disagreements':[{'claim':'Lane opinions disagree.'}],'unverifiable':['Runtime behavior not exercised.']},
        'panel_results':[{'requested_model':'fixture-requested','actual_model':'fixture-fallback','provider':'fixture-provider',
                         'status':'success','fallback_used':True,'output_preview':'Synthetic lane notes.'}],
        'verification':{'status':'not_run','performedBy':None,'evidence':[]}}
    metadata.update(changes.pop('metadata',{}))
    args=SimpleNamespace(save_output=mode,run_id=run_id,command='review',progress=False)
    return anti.write_run_record(args,mode='review',status=changes.pop('status','partial'),metadata=metadata,
        output_text='Synthetic final notes.',execution_ledger=[{'stage':'review','output':'Synthetic raw lane.'}],**changes)


class Document(HTMLParser):
    def __init__(self,text):
        super().__init__();self.elements=[];self.ids=[];self.text=[];self.feed(text)
    def handle_starttag(self,tag,attrs):
        attributes=dict(attrs);self.elements.append((tag,attributes))
        if 'id' in attributes:self.ids.append(attributes['id'])
    def handle_data(self,value):self.text.append(value)


def test_validated_publication_is_read_only_and_keeps_independent_states(ui):
    anti,artifacts,renderer,_,_,_=ui
    path=publication(ui);before={str(p):p.read_bytes() for p in anti.RUNS_DIR.rglob('*.json')}
    bundle=artifacts.read_publication(path)
    assert bundle['record']['publicationStatus']=='validated' and bundle['filesRead']==3
    text=renderer.render([renderer.from_publication(bundle)])
    assert 'Lifecycle: partial' in text and 'Scope: partial' in text
    assert 'Verification: not_run' in text and 'Panel: partial_multi_model' in text
    assert 'unreviewed.py' in text and 'Lane opinions disagree.' in text and 'fixture-fallback' in text
    assert 'Local verdict: unresolved' in text
    assert all(Path(name).read_bytes()==raw for name,raw in before.items())


def test_corrupt_publication_never_creates_html(ui):
    anti,_,_,_,_,tmp=ui;path=publication(ui)
    index=json.loads(path.read_text());Path(index['resultPath']).write_text('{}')
    output=tmp/'report.html'
    assert anti.main(['runs','report','left','--output',str(output)])==1
    assert not output.exists() and Path(index['resultPath']).read_text()=='{}'


def test_result_snapshot_does_not_reread_after_validation(ui,monkeypatch):
    _,artifacts,renderer,_,_,_=ui;path=publication(ui)
    result_path=Path(json.loads(path.read_text())['resultPath']);original=artifacts.validate_record
    def validate(*args,**kwargs):
        value=original(*args,**kwargs)
        result_path.write_text('{"injected":"after validation"}')
        return value
    monkeypatch.setattr(artifacts,'validate_record',validate)
    bundle=artifacts.read_publication(path)
    assert bundle['result']['output_text']=='Synthetic final notes.'
    assert 'after validation' not in renderer.render([renderer.from_publication(bundle)])


@pytest.mark.parametrize('mode',['never','summary','full'])
def test_retention_is_visible_without_inventing_coverage_or_content(ui,mode):
    _,artifacts,renderer,_,_,_=ui
    view=renderer.from_publication(artifacts.read_publication(publication(ui,mode=mode)))
    text=renderer.render([view])
    assert 'Retention: '+mode in text
    if mode=='never':
        assert 'Scope: unknown' in text and 'lifecycle_only' in text
        assert 'Synthetic final notes.' not in text and view['declaredFindings'] is None
    elif mode=='summary':
        assert view['contentComplete'] is False and 'Content retained completely' in text
    else:assert view['contentComplete'] is True


def test_legacy_metadata_only_record_stays_unverified(ui):
    anti,artifacts,renderer,_,_,_=ui
    anti.RUNS_DIR.mkdir();path=anti.RUNS_DIR/'legacy.json';path.write_text('{"id":"legacy","status":"success"}')
    view=renderer.from_publication(artifacts.read_publication(path))
    assert view['publication']=='legacy_unverified' and view['scope']=='unknown' and view['verification']=='unknown'
    assert 'legacy_unverified' in renderer.render([view])


def test_every_model_html_url_and_command_is_inert_escaped_text(ui):
    _,artifacts,renderer,_,_,_=ui
    view=renderer.from_publication(artifacts.read_publication(publication(ui)))
    attack='</style><script>window.fixtureAttack=1</script><img src="https://invalid.example/pixel" onerror="alert(1)"><a href="javascript:alert(1)">bad</a>'
    view.update(runId=attack,output=attack,errors=attack,disagreements=[attack],sourceIdentity={'sourceCommit':attack})
    view['findings'][0]['advisory'].update(claim=attack,evidence=attack,checks=[{'command':['curl','https://invalid.example']}])
    text=renderer.render([view],title=attack);doc=Document(text)
    assert not any(tag in {'script','img','iframe','object','embed','form','input','button','link','base'} for tag,attrs in doc.elements)
    assert all(not any(key.lower().startswith('on') for key in attrs) for tag,attrs in doc.elements)
    assert all(attrs.get('href','').startswith('#') for tag,attrs in doc.elements if tag=='a')
    assert attack in ''.join(doc.text)
    assert 'script-src &#x27;none&#x27;' not in text  # CSP is a fixed literal, not data
    assert any(tag=='meta' and attrs.get('http-equiv')=='Content-Security-Policy' and "default-src 'none'" in attrs['content'] for tag,attrs in doc.elements)


def test_credentials_redacted_before_html_and_absolute_paths_omitted(ui):
    _,artifacts,renderer,_,_,_=ui
    view=renderer.from_publication(artifacts.read_publication(publication(ui)))
    view['output']='api_key=fixture-very-private-token-value'
    view['findings'][0]['advisory'].update(file='/private/sensitive/location.py',evidence='password="fixture secret words"')
    text=renderer.render([view]);assert 'fixture-very-private-token-value' not in text and 'fixture secret words' not in text
    assert '/private/sensitive/location.py' not in text and '&lt;absolute path omitted&gt;' in text


def test_side_by_side_comparison_has_generated_ids_and_no_resolution_inference(ui,capsys):
    anti,*_=ui;publication(ui,'left');publication(ui,'right',metadata={'sourceCommit':'b'*40,'panel_status':'degraded_single_model'})
    assert anti.main(['runs','report','left','--compare','right'])==0
    text=capsys.readouterr().out;doc=Document(text)
    assert 'Side-by-side comparison' in text and 'Absence of a finding does not mean it was resolved' in text
    assert 'degraded_single_model' in text and 'a'*40 in text and 'b'*40 in text
    assert len(doc.ids)==len(set(doc.ids))
    assert all(attrs['href'][1:] in doc.ids for tag,attrs in doc.elements if tag=='a')
    assert any(tag=='main' and attrs.get('tabindex')=='-1' for tag,attrs in doc.elements)
    assert any(tag=='th' and attrs.get('scope')=='col' for tag,attrs in doc.elements)


def test_output_never_overwrites_existing_file_or_symlink(ui,tmp_path):
    anti,*_=ui;publication(ui);existing=tmp_path/'existing.html';existing.write_text('preserved')
    assert anti.main(['runs','report','left','--output',str(existing)])==1
    assert existing.read_text()=='preserved'
    linked=tmp_path/'linked.html';linked.symlink_to(existing)
    assert anti.main(['runs','report','left','--output',str(linked)])==1
    assert existing.read_text()=='preserved'


@pytest.mark.parametrize('bad',['duplicate','huge','symlink'])
def test_bounded_publication_reader_refuses_unsafe_inputs(ui,bad):
    anti,artifacts,*_=ui;path=publication(ui)
    if bad=='duplicate':path.write_text('{"id":"left","id":"left"}')
    elif bad=='huge':path.write_bytes(b' '*(8*1024*1024+1))
    else:
        target=path.with_name('target.json');path.rename(target);path.symlink_to(target)
    with pytest.raises(artifacts.ArtifactError):artifacts.read_publication(path)


def test_display_limits_are_explicit_and_do_not_upgrade_original_counts(ui):
    _,artifacts,renderer,_,_,_=ui
    view=renderer.from_publication(artifacts.read_publication(publication(ui)))
    view['findings']*=201;view['declaredFindings']=300;view['output']='x'*9000
    text=renderer.render([view])
    assert 'Showing 200 of 201 retained entries.' in text and 'Additional retained entries are not displayed' in text
    assert 'Display shortened at 8,000 characters' in text and '>300<' in text


def reflections_fixture(ui):
    _,_,_,reflections,_,tmp=ui
    repo=tmp/'repo';repo.mkdir();(repo/'fixture.py').write_text('fixture = 1\n')
    for run in ('alpha','beta'):
        record=reflections.record_review(repo_path=repo,run_id=run,mode='review',save_output='full',models=['requested'],
            panel_status='partial_multi_model',context={'scopeStatus':'partial','runStatus':'partial','sourceCommit':('a' if run=='alpha' else 'b')*40,
                'omitted_files':['omitted.py'],'actualModels':['actual'],'actualProviders':['fixture']},
            findings=[{'id':'F1','claim':'Synthetic reflection claim','evidence':'Model evidence','file':'fixture.py','line':1}])
        reflections.update_finding_verdict(repo,run,record['findings'][0]['findingKey'],status='confirmed' if run=='alpha' else 'rejected',
            author='fixture-reviewer',evidence='Local independent evidence',source_file='fixture.py')
    return repo


def test_reflection_html_retains_explicit_confirmed_rejected_and_evidence(ui,capsys):
    anti,_,_,_,_,_=ui;repo=reflections_fixture(ui)
    assert anti.main(['runs','export','--repo',str(repo),'--format','html','--run-id','alpha','--compare-run-id','beta'])==0
    text=capsys.readouterr().out
    assert 'Local verdict: confirmed' in text and 'Local verdict: rejected' in text
    assert 'Model claim: unverified' in text and 'Local independent evidence' in text
    assert 'Scope: partial' in text and 'Lifecycle: unknown' in text  # reflection format does not retain lifecycle
    assert str(repo) not in text


def test_comparison_requires_two_unambiguous_reflection_runs(ui,capsys):
    anti,*_=ui;repo=reflections_fixture(ui)
    assert anti.main(['runs','export','--repo',str(repo),'--format','html','--compare-run-id','beta'])==1
    assert '--run-id' in capsys.readouterr().err
    assert anti.main(['runs','export','--repo',str(repo),'--format','html','--run-id','alpha','--compare-run-id','missing'])==1
    assert 'exactly one' in capsys.readouterr().err


def test_save_browser_fixture(ui):
    anti,artifacts,renderer,_,_,tmp=ui
    left=renderer.from_publication(artifacts.read_publication(publication(ui,'desktop-left')))
    right=renderer.from_publication(artifacts.read_publication(publication(ui,'desktop-right',mode='summary',metadata={'panel_status':'degraded_single_model'})))
    right['runId']='A long synthetic identifier with no whitespace '+('very-long-'*30)
    text=renderer.render([left,right]);destination=tmp/'synthetic-report.html';destination.write_text(text)
    print('VIEWER_FIXTURE='+str(destination))
    assert destination.stat().st_size>1000


def test_saved_preview_does_not_invent_declared_findings_from_retained_rows(ui):
    _,artifacts,renderer,_,_,_=ui
    view=renderer.from_publication(artifacts.read_publication(publication(ui,mode='summary')))
    assert len(view['findings'])==1 and view['declaredFindings'] is None
    assert view['parserLoss']=='unknown' and view['parserTotal'] is None
    path=publication(ui,'with-counts',metadata={'findings_count':45,'findings':{'findings':[], 'findings_total':50,'findings_dropped':5}})
    view=renderer.from_publication(artifacts.read_publication(path))
    assert view['declaredFindings']==45 and view['parserTotal']==50 and view['parserDropped']==5 and view['parserLoss']=='loss'


def test_standalone_html_export_without_installed_package(ui,tmp_path):
    import shutil
    import subprocess
    import sys
    from standalone import without_installed_packages
    anti,*_=ui
    publication(ui)
    skill=tmp_path/'standalone'
    shutil.copytree(Path(codex_antigravity_auth.__file__).parent/'skills/anti',skill,ignore=shutil.ignore_patterns('__pycache__'))
    source=without_installed_packages('import importlib.util\nfrom pathlib import Path\n'
        + 'spec=importlib.util.spec_from_file_location("anti_html_child",'+repr(str(skill/'scripts/anti.py'))+')\n'
        + 'module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n'
        + 'module.RUNS_DIR=Path('+repr(str(anti.RUNS_DIR))+')\n'
        + 'raise SystemExit(module.main(["runs","report","left"]))\n')
    result=subprocess.run([sys.executable,'-c',source],cwd=tmp_path,text=True,capture_output=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert '<!doctype html>' in result.stdout and 'Scope: partial' in result.stdout


def test_publication_lane_count_limit_refuses_large_view_without_modifying_store(ui):
    anti,artifacts,_,_,_,_=ui
    args=SimpleNamespace(save_output='full',run_id='many-lanes',command='review',progress=False)
    path=anti.write_run_record(args,mode='review',status='partial',metadata={'scope_status':'partial'},
        output_text='fixture',execution_ledger=[{'stage':'review','output':'fixture'} for _ in range(257)])
    before=path.read_bytes()
    with pytest.raises(artifacts.ArtifactError,match='at most256'):
        artifacts.read_publication(path)
    assert path.read_bytes()==before


def test_publication_total_byte_limit_applies_across_references(ui,monkeypatch,tmp_path):
    _,artifacts,_,_,_,_=ui
    index=tmp_path/'bounded.json';index.write_text('{"id":"bounded"}')
    files=[]
    for number in range(3):
        path=tmp_path/f'part-{number}.json';path.write_text(json.dumps({'fixture':'x'*(6*1024*1024)}));files.append(path)
    def consume(record,path,*,read_bytes):
        for selected in files:read_bytes(selected)
        return record
    monkeypatch.setattr(artifacts,'validate_record',consume)
    with pytest.raises(artifacts.ArtifactError,match='16MiB total'):
        artifacts.read_publication(index)
