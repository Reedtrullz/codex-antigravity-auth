"""Immutable checkpoints use synthetic runs and owned temporary files only."""
import argparse
from copy import deepcopy
import json
from pathlib import Path

import pytest

from codex_antigravity_auth.skills.anti.scripts.anti_lib import checkpoints as cp
from codex_antigravity_auth.skills.anti.scripts.anti_lib.run_records import publish_unlocked

RECIPE='a'*64
CHUNKS=['b'*64,'c'*64,'d'*64]
ROUTE='e'*64


def write_record(root, run_id, *, status='running', checkpoint=None, mode='full', writer='1'*32):
    args=argparse.Namespace(run_id=run_id,command='review',_anti_writer_id=writer)
    metadata={'scope_status':'partial','run_control':{'scope':'process_local','attempts_started':3,'permits_acquired':3,'permits_released':3}}
    if checkpoint:metadata['checkpoint']=checkpoint.reference
    publish_unlocked(args,runs_dir=root,output_mode=mode,timestamp='2026-10-01T00:00:00Z',helper=None,
        output_preview_chars=100,verification_required_checks=[],mode='review',status=status,metadata=metadata)
    return args


def create(root, run_id='source', **kwargs):
    return cp.Checkpoint(root,run_id,'1'*32,recipe=kwargs.pop('recipe',RECIPE),chunks=CHUNKS,routes=[ROUTE],**kwargs)


def complete(checkpoint,index):
    checkpoint.record(index,status='success',output=f'Completed fixture chunk {index}.',model='fixture:model',
        generation={'actual_model':'fixture:model','usage':{'input_tokens':5,'output_tokens':10}},route=ROUTE)


def source_run(root):
    write_record(root,'source')
    source=create(root)
    complete(source,1);complete(source,2)
    source.record(3,status='failed',generation={'submitted':True})
    write_record(root,'source',status='error',checkpoint=source)
    return source


def test_resume_preserves_source_and_reuses_only_completed_chunks(tmp_path):
    source_run(tmp_path)
    before=(tmp_path/'source.json').read_bytes()
    originals={str(p):p.read_bytes() for p in (tmp_path/'source').rglob('*.json')}
    write_record(tmp_path,'target')
    target=create(tmp_path,'target',resume='source',rerun=[3])
    assert target.take(1)['output']=='Completed fixture chunk 1.'
    assert target.take(2)['status']=='success' and target.take(3) is None
    complete(target,3)
    assert target.reference['sourceRun']=='source'
    assert target.prior_runs['source']['run_control']['attempts_started']==3
    assert (tmp_path/'source.json').read_bytes()==before
    assert all(Path(path).read_bytes()==raw for path,raw in originals.items())
    assert len(target.events)==4 and len(target.refs)==4


def test_failed_chunk_requires_explicit_rerun_selection(tmp_path):
    source_run(tmp_path);write_record(tmp_path,'target')
    with pytest.raises(cp.CheckpointError,match='--rerun-chunk 3'):
        create(tmp_path,'target',resume='source')
    assert not (tmp_path/'target/checkpoints').exists()


def test_changed_recipe_never_reuses_prior_outputs(tmp_path):
    source_run(tmp_path);write_record(tmp_path,'target')
    with pytest.raises(cp.CheckpointError,match='changed'):
        create(tmp_path,'target',resume='source',rerun=[3],recipe='f'*64)


@pytest.mark.parametrize('mode',['never','summary'])
def test_retention_without_full_output_cannot_checkpoint(tmp_path,mode):
    write_record(tmp_path,'source',mode=mode)
    with pytest.raises(cp.CheckpointError,match='full-retention'):
        create(tmp_path)


def test_source_must_be_terminal_and_target_must_have_its_own_identity(tmp_path):
    write_record(tmp_path,'source');create(tmp_path)
    write_record(tmp_path,'target')
    with pytest.raises(cp.CheckpointError,match='terminal'):
        create(tmp_path,'target',resume='source')
    with pytest.raises(cp.CheckpointError,match='new run id'):
        create(tmp_path,'source',resume='source')


def test_modified_or_missing_completed_blob_refuses_all_reuse(tmp_path):
    source=source_run(tmp_path);write_record(tmp_path,'target')
    path=tmp_path/'source'/source.refs[0]['path']
    path.write_text('{"changed":true}')
    with pytest.raises(cp.CheckpointError,match='bytes changed'):
        create(tmp_path,'target',resume='source',rerun=[3])
    path.unlink()
    with pytest.raises(cp.CheckpointError,match='missing or invalid'):
        create(tmp_path,'target',resume='source',rerun=[3])


def test_redacted_output_and_unknown_actual_route_are_not_reusable(tmp_path):
    write_record(tmp_path,'source');source=create(tmp_path)
    source.record(1,status='success',output='password=fixture-sensitive-value',model='fixture:model',route=ROUTE)
    source.record(2,status='success',output='Safe fixture output.',model='fixture:model')
    assert not source.latest[1]['reusable'] and not source.latest[2]['reusable']
    assert 'fixture-sensitive-value' not in (tmp_path/'source'/source.refs[0]['path']).read_text()
    write_record(tmp_path,'source',status='partial',checkpoint=source);write_record(tmp_path,'target')
    with pytest.raises(cp.CheckpointError,match='--rerun-chunk 1'):
        create(tmp_path,'target',resume='source')


def test_writer_loss_stops_checkpoint_publication(tmp_path):
    write_record(tmp_path,'source');source=create(tmp_path)
    path=tmp_path/'source.json';record=json.loads(path.read_text());record['writerId']='2'*32;path.write_text(json.dumps(record))
    before=path.read_bytes()
    with pytest.raises(cp.CheckpointError,match='writer'):
        complete(source,1)
    assert path.read_bytes()==before


def test_invalid_source_id_is_rejected_before_lock_creation(tmp_path):
    write_record(tmp_path,'target')
    with pytest.raises(cp.CheckpointError,match='Invalid checkpoint run id'):
        create(tmp_path,'target',resume='../outside')
    assert not (tmp_path.parent/'.outside.json.lock').exists()


def test_checkpoints_store_prompt_hashes_without_raw_prompt(tmp_path):
    write_record(tmp_path,'source');source=create(tmp_path);complete(source,1)
    manifest=cp.read_ref(tmp_path/'source',source.reference['manifest'])
    assert manifest['chunks']==CHUNKS
    assert 'prompt' not in source.events[0] and source.events[0]['promptSha256']==CHUNKS[0]


def test_failed_atomic_index_update_leaves_the_old_checkpoint_readable(tmp_path,monkeypatch):
    write_record(tmp_path,'source');source=create(tmp_path)
    before=(tmp_path/'source.json').read_bytes();old=deepcopy(source.reference)
    def fail(*args):raise OSError('synthetic publication failure')
    monkeypatch.setattr(cp,'atomic_write_json',fail)
    with pytest.raises(OSError,match='synthetic'):
        complete(source,1)
    assert (tmp_path/'source.json').read_bytes()==before and source.reference==old
    assert cp.read_ref(tmp_path/'source',old['manifest'])['events']==[]


def test_cleanup_tombstone_prevents_new_checkpoint_writes(tmp_path):
    from codex_antigravity_auth.skills.anti.scripts.anti_lib.persistence import PersistenceError
    write_record(tmp_path,'source');source=create(tmp_path)
    before=(tmp_path/'source.json').read_bytes()
    (tmp_path/'.deleted').mkdir()
    (tmp_path/'.deleted/source.json').write_text('{}')
    with pytest.raises(PersistenceError,match='cleanup'):
        complete(source,1)
    assert (tmp_path/'source.json').read_bytes()==before


def test_symlinked_checkpoint_blob_never_reads_outside_artifacts(tmp_path):
    source=source_run(tmp_path);write_record(tmp_path,'target')
    path=tmp_path/'source'/source.refs[0]['path']
    path.unlink()
    outside=tmp_path/'outside.json';outside.write_text('{"private":"fixture"}')
    try:path.symlink_to(outside)
    except OSError:pytest.skip('symlinks unavailable')
    with pytest.raises(cp.CheckpointError,match='symlink'):
        create(tmp_path,'target',resume='source',rerun=[3])
    assert outside.read_text()=='{"private":"fixture"}'
