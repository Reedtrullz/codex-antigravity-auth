"""Evidence provenance and failure accounting with synthetic files/results."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_evidence', ROOT / 'scripts/release_evidence.py')
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


@pytest.mark.parametrize('dirty,moving,test_exit,artifact_exit,change_artifact,expected', [
    (False, False, 0, 0, False, True), (True, False, 0, 0, False, False),
    (False, True, 0, 0, False, False), (False, False, 1, 0, False, False),
    (False, False, 0, 1, False, False), (False, False, 0, 0, True, False),
])
def test_exact_evidence_requires_stable_clean_source_and_passing_unchanged_artifacts(tmp_path, monkeypatch, dirty, moving, test_exit, artifact_exit, change_artifact, expected):
    root = tmp_path / 'source'; root.mkdir()
    (root / 'pyproject.toml').write_text('[project]\nversion="7.8.9"\n')
    dist = tmp_path / 'dist'; dist.mkdir()
    (dist / 'fixture.whl').write_bytes(b'fixture-wheel')
    (dist / 'fixture.tar.gz').write_bytes(b'fixture-sdist')
    identities = iter([{'commit':'a'*40,'dirty':dirty}, {'commit':('b' if moving else 'a')*40,'dirty':dirty}])
    monkeypatch.setattr(evidence, 'source_identity', lambda root: next(identities))
    commands = []
    def run(name, args, root, output):
        commands.append(args)
        if name == 'tests':
            (output / 'tests.xml').write_text('<testsuites><testsuite tests="12" failures="0" errors="0" skipped="2"/></testsuites>')
        if change_artifact and name == 'artifact-installed':
            (dist / 'fixture.whl').write_bytes(b'changed-wheel')
        return {'exitCode': test_exit if name == 'tests' else artifact_exit, 'log':name+'.log'}
    monkeypatch.setattr(evidence, 'run_step', run)
    report = evidence.generate(root, tmp_path / 'out', dist)
    assert report['revisionVerified'] is expected
    assert report['packageVersion'] == '7.8.9'
    assert report['tests']['junit']['tests'] == 12
    assert report['tests']['junit']['skipped'] == 2
    assert 'run_tests.py' in commands[0][1]
    assert report['nonClaims'] and report['runtime']['python']
    assert json.loads((tmp_path/'out/report.json').read_text()) == report


def test_missing_test_report_and_unchecked_artifacts_are_not_success(tmp_path, monkeypatch):
    (tmp_path/'pyproject.toml').write_text('[project]\nversion="0.0.1"\n')
    monkeypatch.setattr(evidence,'source_identity',lambda root:{'commit':'a'*40,'dirty':False})
    monkeypatch.setattr(evidence,'run_step',lambda *args:{'exitCode':0})
    report=evidence.generate(tmp_path,tmp_path/'out')
    assert not report['checksPassed'] and not report['revisionVerified']
    assert report['artifacts']['status'] == 'not_checked'
    assert report['errors']


def test_nonempty_destination_is_preserved(tmp_path):
    (tmp_path/'report.json').write_text('existing')
    with pytest.raises(ValueError, match='empty'):
        evidence.generate(tmp_path,tmp_path)
    assert (tmp_path/'report.json').read_text() == 'existing'


@pytest.mark.parametrize('xml', [
    '<testsuites/>',
    '<testsuites><testsuite tests="-1" failures="0" errors="0" skipped="0"/></testsuites>',
    '<testsuites><testsuite tests="1" failures="unexpected" errors="0" skipped="0"/></testsuites>',
])
def test_invalid_counts_are_rejected(tmp_path, xml):
    path = tmp_path/'tests.xml'; path.write_text(xml)
    with pytest.raises(ValueError): evidence.junit_counts(path)


def test_source_disappearing_after_tests_still_leaves_failure_report(tmp_path, monkeypatch):
    (tmp_path/'pyproject.toml').write_text('[project]\nversion="0.0.1"\n')
    calls = []
    def identity(root):
        calls.append(1)
        if len(calls) == 2: raise OSError('synthetic unavailable repository')
        return {'commit':'a'*40,'dirty':False}
    monkeypatch.setattr(evidence,'source_identity',identity)
    def run(name,args,root,output):
        (output/'tests.xml').write_text('<testsuite tests="3" failures="0" errors="0" skipped="0"/>')
        return {'exitCode':0}
    monkeypatch.setattr(evidence,'run_step',run)
    report=evidence.generate(tmp_path,tmp_path/'out')
    assert report['sourceAfter'] is None and not report['revisionVerified']
    assert (tmp_path/'out/report.json').exists()


@pytest.mark.parametrize('total,skipped,expected', [(12,12,False), (0,0,False), (12,11,True), (12,0,True)])
def test_revision_verification_requires_an_executed_case(tmp_path, monkeypatch, total, skipped, expected):
    (tmp_path/'pyproject.toml').write_text('[project]\nversion="0.0.1"\n')
    monkeypatch.setattr(evidence,'source_identity',lambda root:{'commit':'a'*40,'dirty':False})
    def run(name,args,root,output):
        (output/'tests.xml').write_text(f'<testsuite tests="{total}" failures="0" errors="0" skipped="{skipped}"/>')
        return {'exitCode':0}
    monkeypatch.setattr(evidence,'run_step',run)
    report=evidence.generate(tmp_path,tmp_path/'out')
    assert report['checksPassed'] is expected
    assert report['revisionVerified'] is expected
    assert report['tests']['junit']['tests'] == total
    assert report['tests']['junit']['skipped'] == skipped
