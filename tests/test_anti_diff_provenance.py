"""Immutable patch coordinates from synthetic local Git repositories only."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import codex_antigravity_auth

SCRIPT = Path(codex_antigravity_auth.__file__).resolve().parent / 'skills/anti/scripts/anti.py'


@pytest.fixture
def repo(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True).stdout.decode().strip()
    git('init', '-q')
    git('config', 'user.name', 'Synthetic fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    source = ''.join(f'base line {index}\n' for index in range(1, 31))
    (tmp_path / 'source.py').write_text(source)
    git('add', '.')
    git('commit', '-qm', 'baseline')
    spec = importlib.util.spec_from_file_location('anti_diff_fixture', SCRIPT)
    anti = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(anti)
    monkeypatch.chdir(tmp_path)
    return anti, tmp_path, git


def collect(anti, *options):
    args = anti.build_parser().parse_args(['review', '--no-progress', *options])
    context = anti.collect_review_context(args)
    _, _, _, metadata = anti.assemble_review_prompt_from_context(context, max_prompt_chars=0)
    metadata.update(_review_context=context, scope_status='complete')
    return context, metadata


def enrich(anti, metadata, file, line, side=None):
    finding = {'file':file, 'line':line, 'claim':'Synthetic advisory claim', 'verify':'Inspect fixture',
               'excerptSha256':'0'*64, 'sourceExcerpt':'untrusted excerpt', 'verificationStatus':'verified'}
    if side is not None: finding['diffSide'] = side
    normalized = anti.normalize_finding_item(finding, 1)
    return anti.enrich_finding_provenance({'findings':[normalized]}, metadata)['findings'][0]


def blob(text):
    raw = text.encode()
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def test_added_deleted_context_lines_and_blob_identity_survive_mutation(repo):
    anti, root, _ = repo
    old = (root / 'source.py').read_text()
    new = old.replace('base line 2\n', 'changed line 2\nextra line\n').replace('base line 24\n', '')
    (root / 'source.py').write_text(new)
    context, metadata = collect(anti)
    assert context['diff_snapshot']['status'] == 'complete'
    assert len(context['diff_snapshot']['files'][0]['hunks']) == 2
    (root / 'source.py').write_text('mutation after capture\n')
    for side, line, text, kind in [('old',2,'base line 2','deleted'), ('new',2,'changed line 2','added'),
                                   ('new',3,'extra line','added'), ('old',24,'base line 24','deleted'),
                                   ('new',1,'base line 1','context')]:
        finding = enrich(anti, metadata, 'source.py', line, side)
        assert finding['line'] == line and finding['diffSide'] == side
        assert finding['sourceExcerpt'] == text and finding['locationStatus'] == 'mapped'
        assert finding['excerptSha256'] == hashlib.sha256(text.encode()).hexdigest()
        assert finding['diffProvenance']['blob'] == blob(old if side == 'old' else new)
        assert finding['diffProvenance']['kind'] == kind
        assert finding['verificationStatus'] == 'unverified'
    unknown = enrich(anti, metadata, 'source.py', 15, 'new')
    assert unknown['line'] is None and unknown['excerptSha256'] is None
    assert unknown['locationReason'] == 'outside_captured_hunk'


def test_staged_and_working_tree_capture_distinct_index_and_disk_bytes(repo):
    anti, root, git = repo
    source = (root / 'source.py').read_text()
    (root / 'source.py').write_text(source.replace('base line 2', 'staged line 2'))
    git('add', 'source.py')
    _, staged = collect(anti, '--scope', 'staged')
    (root / 'source.py').write_text(source.replace('base line 2', 'unstaged line 2'))
    _, working = collect(anti)
    (root / 'source.py').unlink()
    assert enrich(anti, staged, 'source.py', 2)['sourceExcerpt'] == 'staged line 2'
    assert enrich(anti, working, 'source.py', 2)['sourceExcerpt'] == 'unstaged line 2'
    assert staged['diffSnapshot']['diffSha256'] != working['diffSnapshot']['diffSha256']


def test_revision_diff_normalizes_display_configuration_and_persists_identity(repo, monkeypatch):
    anti, root, git = repo
    git('config', 'diff.noprefix', 'true')
    git('config', 'color.ui', 'always')
    (root / 'source.py').write_text('committed fixture\n')
    git('add', 'source.py'); git('commit', '-qm', 'second fixture')
    value, metadata = collect(anti, '--scope', 'diff', '--changed-files', 'HEAD~1..HEAD')
    finding = enrich(anti, metadata, 'source.py', 1)
    assert finding['sourceExcerpt'] == 'committed fixture'
    assert '\x1b' not in value['diff'] and '--- a/source.py' in value['diff']
    assert '_rows' not in json.dumps(metadata['diffSnapshot'])
    monkeypatch.setattr(anti, 'RUNS_DIR', root / 'saved-fixture')
    monkeypatch.setattr(anti, 'helper_identity', lambda: {})
    public = {key:value for key,value in metadata.items() if not key.startswith('_')}
    public['findings'] = {'findings':[finding]}
    path = anti.write_run_record(SimpleNamespace(save_output='full', run_id='diff-fixture', command='panel', progress=False),
        mode='panel', status='success', output_text='Synthetic finding', metadata=public)
    record = anti.load_run_record(path)
    saved = json.loads(Path(record['resultPath']).read_text())
    retained = saved['findings'][0]
    assert retained['diffProvenance'] == finding['diffProvenance']
    assert retained['sourceExcerpt'] == 'committed fixture' and retained['verificationStatus'] == 'unverified'


def test_renames_preserve_old_and_new_paths_without_side_guessing(repo):
    anti, root, git = repo
    git('mv', 'source.py', 'renamed.py')
    (root / 'renamed.py').write_text((root / 'renamed.py').read_text().replace('base line 2', 'renamed line 2'))
    _, metadata = collect(anti)
    old = enrich(anti, metadata, 'source.py', 2, 'old')
    new = enrich(anti, metadata, 'renamed.py', 2)
    assert old['diffProvenance']['oldPath'] == 'source.py'
    assert old['diffProvenance']['newPath'] == new['diffProvenance']['newPath'] == 'renamed.py'
    assert old['sourceExcerpt'] == 'base line 2' and new['sourceExcerpt'] == 'renamed line 2'
    assert enrich(anti, metadata, 'renamed.py', 2, 'old')['line'] is None


@pytest.mark.parametrize('name', ['space name.py', 'tab\tname.py', 'quote"name.py', 'unicode-æ.py'])
def test_git_quoted_paths_map_to_exact_selected_names(repo, name):
    anti, root, git = repo
    (root / name).write_text('before\n')
    git('add', name); git('commit', '-qm', 'named fixture')
    (root / name).write_text('after\n')
    _, metadata = collect(anti)
    finding = enrich(anti, metadata, name, 1)
    assert finding['sourceExcerpt'] == 'after' and finding['diffProvenance']['path'] == name


def test_added_deleted_files_and_empty_line_locations(repo):
    anti, root, git = repo
    (root / 'source.py').unlink()
    (root / 'added.py').write_text('\nadded\n')
    git('add', '-A')
    _, metadata = collect(anti, '--scope', 'staged')
    assert enrich(anti, metadata, 'source.py', 1, 'old')['sourceExcerpt'] == 'base line 1'
    assert enrich(anti, metadata, 'source.py', 1)['line'] is None
    empty = enrich(anti, metadata, 'added.py', 1)
    assert empty['line'] == 1 and empty['sourceExcerpt'] == ''
    assert empty['excerptSha256'] == hashlib.sha256(b'').hexdigest()


@pytest.mark.parametrize('scope', ['staged', 'working-tree', 'diff'])
@pytest.mark.parametrize('change', ['delete', 'rename'])
def test_explicit_old_path_selection_uses_diff_coverage_without_disk_reads(repo, monkeypatch, scope, change):
    anti, root, git = repo
    if change == 'delete':
        (root / 'source.py').unlink()
    else:
        git('mv', 'source.py', 'renamed.py')
        (root / 'renamed.py').write_text((root / 'renamed.py').read_text().replace('base line 2', 'renamed line 2'))
    git('add', '-u')
    if scope == 'diff': git('commit', '-qm', 'changed fixture')
    original = Path.read_bytes
    def read(path):
        assert path != root / 'source.py', 'deleted diff source must not be read from disk'
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', read)
    value, metadata = collect(anti, '--scope', scope, '--file', 'source.py', '--required-file', 'source.py',
                              *(['--changed-files', 'HEAD~1..HEAD'] if scope == 'diff' else []))
    assert not value['file_texts']
    assert metadata['status'] == 'complete' and not metadata['omitted_files']
    record = next(row for row in metadata['coverage'] if row['path'] == 'source.py')
    assert record['sourceKind'] == 'diff' and record['contentStatus'] == 'complete'
    finding = enrich(anti, metadata, 'source.py', 2, 'old')
    assert finding['line'] == 2 and finding['sourceExcerpt'] == 'base line 2'
    assert finding['locationStatus'] == 'mapped' and finding['verificationStatus'] == 'unverified'
    chunks, chunk_metadata = anti.build_review_chunk_prompts(value, max_prompt_chars=8000, max_chunks=0,
                                                            required_paths=['source.py'])
    assert chunks and chunk_metadata['status'] == 'complete'


def test_missing_explicit_file_without_a_diff_remains_omitted(repo):
    anti, _, _ = repo
    value, metadata = collect(anti, '--scope', 'staged', '--file', 'missing.py')
    assert value['diff'] == '' and metadata['status'] == 'incomplete'
    assert metadata['coverage'][0]['sourceKind'] == 'file'
    assert enrich(anti, metadata, 'missing.py', 1, 'old')['line'] is None


def test_selected_diff_paths_do_not_expand_git_wildcards(repo):
    anti, root, git = repo
    for name in ('source[1].py', 'source1.py'):
        (root / name).write_text('before\n')
    git('add', '.'); git('commit', '-qm', 'literal path fixtures')
    (root / 'source[1].py').write_text('selected change\n')
    (root / 'source1.py').write_text('unselected change\n')
    git('add', '-u')
    value, metadata = collect(anti, '--scope', 'staged', '--file', 'source[1].py')
    assert value['paths'] == ['source[1].py']
    assert 'selected change' in value['diff'] and 'unselected change' not in value['diff']
    assert enrich(anti, metadata, 'source[1].py', 1)['sourceExcerpt'] == 'selected change'


def test_no_final_newline_and_unicode_line_separator_are_preserved(repo):
    anti, root, _ = repo
    (root / 'source.py').write_text('captured\u2028separator')
    _, metadata = collect(anti)
    finding = enrich(anti, metadata, 'source.py', 1)
    assert finding['sourceExcerpt'] == 'captured\u2028separator'
    assert finding['diffProvenance']['newline'] is False


def test_excerpts_are_redacted_without_falsifying_original_hash(repo):
    anti, root, _ = repo
    text = 'api_key = "fixture-secret-123456789abcdef"'
    (root / 'source.py').write_text(text + '\n')
    _, metadata = collect(anti)
    finding = enrich(anti, metadata, 'source.py', 1)
    assert 'fixture-secret' not in finding['sourceExcerpt']
    assert finding['excerptRedacted'] is True
    assert finding['excerptSha256'] == hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize('line,side', [(True,None), (1.5,None), (10**400,None), (1,{}), (1,'left')])
def test_invalid_model_coordinates_remain_unknown(repo, line, side):
    anti, root, _ = repo
    (root / 'source.py').write_text('new\n')
    _, metadata = collect(anti)
    finding = enrich(anti, metadata, 'source.py', line, side)
    assert finding['line'] is None and finding['locationStatus'] == 'unknown'


def test_binary_and_submodule_content_never_get_fabricated_lines(repo):
    anti, root, git = repo
    (root / 'source.py').write_bytes(b'new\0binary')
    _, metadata = collect(anti)
    assert enrich(anti, metadata, 'source.py', 1)['line'] is None
    module = anti.diff_snapshot
    patch = 'diff --git a/sub b/sub\nindex ' + '1'*40 + '..' + '2'*40 + ' 160000\n--- a/sub\n+++ b/sub\n@@ -1 +1 @@\n-Subproject commit old\n+Subproject commit new\n'
    snapshot = module.capture(patch, ['sub'])
    assert snapshot['files'][0]['reason'] == 'nonregular_diff' and not snapshot['_lookup']


@pytest.mark.parametrize('suffix', ['@@ -1,2 +1 @@\n-old\n+new\n', '@@@ -1 -1 +1 @@@\n--old\n++new\n',
    '@@ -1 +1 @@\n-old\n+new\n+extra\n', '@@ -1 +1 @@\n\\ No newline at end of file\n-old\n+new\n'])
def test_malformed_hunks_never_publish_partial_coordinate_maps(repo, suffix):
    anti, _, _ = repo
    patch = 'diff --git a/f b/f\nindex ' + '1'*40 + '..' + '2'*40 + ' 100644\n--- a/f\n+++ b/f\n' + suffix
    snapshot = anti.diff_snapshot.capture(patch, ['f'])
    assert snapshot['status'] == 'partial' and not snapshot['_lookup']


def test_non_utf8_patch_is_refused_before_replacement_evidence(repo):
    anti, root, _ = repo
    (root / 'source.py').write_bytes(b'non-utf8 \xff\n')
    with pytest.raises(anti.AntiError, match='non-UTF-8'):
        collect(anti)


def test_combined_diff_and_oversized_input_remain_explicit_unknowns(repo, monkeypatch):
    anti, _, _ = repo
    snapshot = anti.diff_snapshot.capture('diff --cc source.py\n@@@ -1 -1 +1 @@@\n++fixture\n', ['source.py'])
    assert snapshot['files'][0]['reason'] == 'combined_diff' and not snapshot['_lookup']
    monkeypatch.setattr(anti.diff_snapshot, 'MAX_CHARS', 10)
    snapshot = anti.diff_snapshot.capture('x' * 11, [])
    assert snapshot['status'] == 'unavailable' and snapshot['diffSha256'] is None


def test_coordinate_limits_fail_mapping_without_upgrading_coverage(repo, monkeypatch):
    anti, root, _ = repo
    (root / 'source.py').write_text('new\n')
    monkeypatch.setattr(anti.diff_snapshot, 'MAX_ROWS', 1)
    _, metadata = collect(anti)
    finding = enrich(anti, metadata, 'source.py', 1)
    assert finding['locationStatus'] == 'unknown'
    assert finding['scopeStatus'] == 'complete' and finding['verificationStatus'] == 'unverified'


def test_only_full_submitted_rows_receive_locations(repo):
    anti, root, _ = repo
    (root / 'source.py').write_text('new\n')
    value, metadata = collect(anti)
    snapshot = value['diff_snapshot']
    file, row = snapshot['_lookup'][('source.py','new',1)][0]
    metadata['diff_ranges'] = [{'start':row['patchStart'], 'end':row['patchEnd']-1}]
    assert enrich(anti, metadata, 'source.py', 1)['locationReason'] == 'line_not_submitted'
    metadata['diff_ranges'] = [{'start':row['patchStart'], 'end':row['patchEnd'], 'chunkId':'fixture-chunk'}]
    finding = enrich(anti, metadata, 'source.py', 1)
    assert finding['chunkId'] == 'fixture-chunk' and finding['locationStatus'] == 'mapped'
    plan, planned = anti.build_review_chunk_prompts(value, max_prompt_chars=5000, max_chunks=0)
    assert plan and planned['diff_ranges'] == []


def test_chunk_execution_marks_only_submitted_rows_for_mapping(repo, monkeypatch):
    anti, root, _ = repo
    (root / 'source.py').write_text(''.join(f'changed long source {i} ' + 'x'*60 + '\n' for i in range(100)))
    value, metadata = collect(anti)
    args = anti.build_parser().parse_args(['review', '--allow-partial', '--max-review-chunks', '1', '--no-progress'])
    monkeypatch.setattr(anti, 'generate_with_fallback', lambda args, **kw: ('Synthetic review summary.',kw['model'],{}))
    _, _, executed = anti.run_chunked_review(args=args, context=value, model='fixture', base_metadata=metadata, max_prompt_chars=3000)
    executed['_review_context'] = value
    executed['scope_status'] = 'partial'
    assert executed['diff_ranges'] and executed['diff_ranges'][0]['chunkId']
    snapshot = value['diff_snapshot']
    sent = [line for path,side,line in snapshot['_lookup'] if side == 'new'
            and anti.diff_snapshot.locate(snapshot,path,line,side,executed['diff_ranges'])[0]]
    assert sent and max(sent) < 100
    assert enrich(anti, executed, 'source.py', sent[0])['locationStatus'] == 'mapped'
    assert enrich(anti, executed, 'source.py', 100)['locationReason'] == 'line_not_submitted'
