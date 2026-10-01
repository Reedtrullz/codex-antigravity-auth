"""Repository inventories use temporary Git repositories and synthetic source only."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest
import codex_antigravity_auth

SCRIPT = Path(codex_antigravity_auth.__file__).resolve().parent / 'skills/anti/scripts/anti.py'


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    root.mkdir()
    def git(*args):
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True).stdout
    git('init', '-q')
    git('config', 'user.name', 'Synthetic fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    for name, content in {'.gitignore':'ignored/\n', 'tracked.py':'before\n',
                          'pkg/package.json':'{}', 'pkg/source.py':'source\n',
                          'pkg-extra/other.py':'other\n', 'deleted.py':'deleted\n',
                          'old.py':'rename fixture\n'}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git('add', '.')
    git('commit', '-qm', 'fixture')
    (root / 'tracked.py').write_text('after\n')
    (root / 'deleted.py').unlink()
    git('mv', 'old.py', 'renamed.py')
    (root / 'untracked.py').write_text('new source\n')
    (root / 'pkg/new.py').write_text('new package source\n')
    (root / '.env').write_text('synthetic private fixture')
    (root / 'ignored').mkdir()
    (root / 'ignored/private.py').write_text('ignored fixture')
    spec = importlib.util.spec_from_file_location('anti_inventory_test', SCRIPT)
    anti = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(anti)
    monkeypatch.chdir(root)
    return anti, root, git


def context(anti, *options):
    args = anti.build_parser().parse_args(['review', '--no-progress', *options])
    return anti.collect_review_context(args)


def reasons(value):
    return {row['path']:row['reason'] for row in value['inventory']['excluded']}


def test_default_working_tree_reports_untracked_and_opt_in_captures_exact_bytes(repo):
    anti, root, git = repo
    default = context(anti)
    assert {'deleted.py', 'old.py', 'renamed.py', 'tracked.py'} <= set(default['paths'])
    assert reasons(default)['untracked.py'] == 'untracked_not_requested'
    assert 'untracked.py' not in default['paths']
    included = context(anti, '--include-untracked')
    assert dict(included['file_texts'])['untracked.py'] == 'new source\n'
    assert reasons(included)['.env'] == 'sensitive_cache_or_binary'
    _, _, _, metadata = anti.assemble_review_prompt_from_context(included, max_prompt_chars=0)
    assert metadata['inventory'] == included['inventory']
    assert 'deleted.py' in metadata['declared_files']


@pytest.mark.parametrize('scope', ['staged', 'files', 'diff'])
def test_untracked_opt_in_never_broadens_other_scopes(repo, scope):
    anti, _, _ = repo
    with pytest.raises(anti.AntiError, match='requires working-tree or repository'):
        context(anti, '--scope', scope, '--include-untracked')


def test_repository_roots_exclusions_and_nested_package_identity_are_exact(repo):
    anti, _, _ = repo
    result = context(anti, '--scope', 'repository', '--review-root', 'pkg', '--include-untracked', '--exclude-path', 'pkg/source.py')
    assert result['paths'] == ['pkg/new.py', 'pkg/package.json']
    assert result['inventory']['package_roots'] == ['pkg']
    assert reasons(result)['pkg/source.py'] == 'user_excluded'
    assert not any('pkg-extra' in path for path in result['paths'])
    assert result['diff'] == ''
    assert dict(result['file_texts'])['pkg/new.py'] == 'new package source\n'


def test_repository_tracks_ignored_deleted_and_not_requested_paths(repo):
    anti, _, _ = repo
    result = context(anti, '--scope', 'repository')
    assert reasons(result)['ignored/'] == 'git_ignored'
    assert reasons(result)['.env'] == 'sensitive_cache_or_binary'
    assert reasons(result)['untracked.py'] == 'untracked_not_requested'
    assert next(row for row in result['file_records'] if row['path'] == 'deleted.py')['contentStatus'] == 'omitted'
    assert 'old.py' not in result['paths'] and 'renamed.py' in result['paths']


def test_symlinks_inside_and_outside_root_are_excluded_without_reading(repo, tmp_path):
    anti, root, _ = repo
    outside = tmp_path / 'outside.py'
    outside.write_text('outside fixture must not be read')
    try:
        (root / 'link.py').symlink_to(outside)
        (root / 'inner.py').symlink_to(root / 'tracked.py')
        (root / 'alias').symlink_to(root / 'pkg', target_is_directory=True)
    except OSError:
        pytest.skip('symlink creation unavailable')
    result = context(anti, '--scope', 'repository', '--include-untracked')
    assert reasons(result)['link.py'] == reasons(result)['inner.py'] == reasons(result)['alias'] == 'symlink'
    assert 'outside fixture' not in repr(result['file_texts'])
    with pytest.raises(anti.AntiError, match='without symlinks'):
        context(anti, '--scope', 'repository', '--review-root', 'alias')


@pytest.mark.parametrize('options', [
    ['--review-root', '..'], ['--review-root', 'tracked.py'], ['--review-root', 'absent'],
    ['--exclude-path', '../outside'], ['--file', 'absent.py'],
])
def test_invalid_inventory_selection_fails_closed(repo, options):
    anti, _, _ = repo
    with pytest.raises(anti.AntiError):
        context(anti, '--scope', 'repository', *options)


@pytest.mark.parametrize('required', ['deleted.py', 'untracked.py', '.env'])
def test_required_missing_untracked_and_excluded_files_fail_before_generation(repo, required):
    anti, _, _ = repo
    with pytest.raises(anti.AntiError):
        context(anti, '--scope', 'repository', '--required-file', required)


def test_inventory_and_source_caps_never_claim_complete_coverage(repo, monkeypatch):
    anti, _, _ = repo
    monkeypatch.setattr(anti.review_inventory, 'MAX_FILE_BYTES', 3)
    result = context(anti, '--scope', 'repository', '--review-root', 'pkg', '--include-untracked')
    _, _, _, metadata = anti.assemble_review_prompt_from_context(result, max_prompt_chars=0)
    assert metadata['status'] == 'incomplete'
    omitted = [row for row in metadata['coverage'] if row['contentStatus'] == 'omitted']
    assert {row['path'] for row in omitted} == {'pkg/source.py', 'pkg/new.py'}
    with pytest.raises(anti.AntiError, match='captured completely'):
        context(anti, '--scope', 'repository', '--review-root', 'pkg', '--required-file', 'pkg/source.py')
    monkeypatch.setattr(anti.review_inventory, 'MAX_PATHS', 1)
    with pytest.raises(anti.AntiError, match='path limit'):
        context(anti, '--scope', 'repository')


def test_git_byte_cap_refuses_truncated_inventory(repo, monkeypatch):
    anti, _, _ = repo
    monkeypatch.setattr(anti.review_inventory, 'MAX_PATH_BYTES', 5)
    with pytest.raises(anti.AntiError, match='byte limit'):
        context(anti, '--scope', 'repository')


def test_total_source_byte_limit_is_explicit_and_required_file_cannot_bypass_it(repo, monkeypatch):
    anti, _, _ = repo
    monkeypatch.setattr(anti.review_inventory, 'MAX_SOURCE_BYTES', 2)
    result = context(anti, '--scope', 'repository', '--review-root', 'pkg')
    assert next(row for row in result['file_records'] if row['path'] == 'pkg/source.py')['reason'] == 'total_byte_limit'
    with pytest.raises(anti.AntiError, match='captured completely'):
        context(anti, '--scope', 'repository', '--review-root', 'pkg', '--required-file', 'pkg/source.py')


def test_inventory_paths_are_literal_even_when_they_contain_git_pathspec_characters(repo):
    anti, root, git = repo
    (root / 'pkg[1]').mkdir()
    (root / 'pkg[1]/source.py').write_text('literal fixture')
    result = context(anti, '--scope', 'repository', '--review-root', 'pkg[1]', '--include-untracked')
    assert result['paths'] == ['pkg[1]/source.py']


def test_git_inventory_failure_is_not_an_empty_repository(repo, monkeypatch):
    anti, root, git = repo
    monkeypatch.setattr(anti, 'find_repo_root', lambda _: root / 'pkg')
    # This remains a repository, but an invalid Git index forces a command error.
    (root / '.git/index').write_bytes(b'synthetic invalid index')
    with pytest.raises(anti.AntiError, match='inventory failed'):
        context(anti, '--scope', 'repository')


def test_chunk_caps_keep_omissions_and_required_files_fail_closed(repo):
    anti, root, git = repo
    for name in ('pkg/a.py', 'pkg/b.py'):
        (root / name).write_text('synthetic source line\n' * 500)
    result = context(anti, '--scope', 'repository', '--review-root', 'pkg', '--include-untracked')
    chunks, metadata = anti.build_review_chunk_prompts(result, max_prompt_chars=5000, max_chunks=1)
    assert chunks and metadata['status'] == 'incomplete' and metadata['omitted_items']
    assert metadata['inventory'] == result['inventory']
    with pytest.raises(anti.AntiError, match='required file'):
        anti.build_review_chunk_prompts(result, max_prompt_chars=5000, max_chunks=1, required_paths=['pkg/b.py'])


def test_review_workflows_forward_selection_and_plan_refuses_it(repo):
    anti, _, _ = repo
    args = anti.build_parser().parse_args(['workflow', 'review-ready', '--scope', 'repository',
        '--review-root', 'pkg', '--exclude-path', 'pkg/source.py', '--include-untracked'])
    expanded = anti.build_parser().parse_args(anti.workflow_expansion(args))
    assert expanded.scope == 'repository' and expanded.include_untracked
    assert expanded.review_root == ['pkg'] and expanded.exclude_path == ['pkg/source.py']
    args.name = 'plan-deep'
    with pytest.raises(anti.AntiError, match='review workflow'):
        anti.workflow_expansion(args)


def test_empty_working_tree_reports_untracked_reason(repo, capsys):
    anti, root, git = repo
    git('add', '-u')
    git('commit', '-qm', 'clean tracked fixture')
    assert anti.main(['review', '--print-prompt', '--no-progress']) == 1
    assert 'untracked_not_requested' in capsys.readouterr().err


def test_repository_print_prompt_exposes_inventory_without_any_http(repo, capsys):
    anti, _, _ = repo
    assert anti.main(['review', '--scope', 'repository', '--review-root', 'pkg', '--include-untracked',
                      '--print-prompt', '--json', '--no-progress']) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['metadata']['inventory']['roots'] == ['pkg']
    assert 'new package source' in printed['prompt']


@pytest.mark.parametrize('reason', ['file_byte_limit', 'total_byte_limit', 'missing', 'unreadable'])
def test_read_omissions_reach_source_and_synthesis_manifests(repo, monkeypatch, reason):
    anti, root, _ = repo
    original = anti.review_inventory.read_file
    def read(root, rel, budget):
        return (None, 123, reason) if rel == 'pkg/source.py' else original(root, rel, budget)
    monkeypatch.setattr(anti.review_inventory, 'read_file', read)
    value = context(anti, '--scope', 'repository', '--review-root', 'pkg')
    prompt, _, _, metadata = anti.assemble_review_prompt_from_context(value, max_prompt_chars=0)
    assert '- status: incomplete' in prompt
    assert '- omitted_files: pkg/source.py' in prompt
    assert metadata['omission_reasons']['pkg/source.py'] == reason
    chunks, chunk_metadata = anti.build_review_chunk_prompts(value, max_prompt_chars=5000, max_chunks=0)
    assert chunks and chunk_metadata['omitted_items'] == ['pkg/source.py']
    assert chunk_metadata['omission_reasons']['pkg/source.py'] == reason
    assert all('- status: incomplete' in chunk['prompt'] and reason in chunk['prompt'] for chunk in chunks)
    synthesis, _, _ = anti.build_chunk_synthesis_prompt(context=value, chunks=chunks,
        chunk_outputs=['Synthetic finding summary.' for _ in chunks], chunk_metadata=chunk_metadata, max_chars=20000)
    assert 'pkg/source.py' in synthesis and reason in synthesis and 'incomplete' in synthesis


def test_partial_generation_receives_omissions_and_keeps_incomplete_outcome(repo, monkeypatch):
    anti, root, _ = repo
    monkeypatch.setattr(anti.review_inventory, 'MAX_FILE_BYTES', 3)
    value = context(anti, '--scope', 'repository', '--review-root', 'pkg')
    _, _, _, source_metadata = anti.assemble_review_prompt_from_context(value, max_prompt_chars=0)
    args = anti.build_parser().parse_args(['review', '--scope', 'repository', '--allow-partial', '--no-progress'])
    seen = []
    def generate(args, **kwargs):
        seen.append(kwargs['prompt'])
        return 'Synthetic review: no defects in the supplied content.', kwargs['model'], {}
    monkeypatch.setattr(anti, 'generate_with_fallback', generate)
    _, _, metadata = anti.run_chunked_review(args=args, context=value, model='fixture',
        base_metadata=source_metadata, max_prompt_chars=5000)
    assert len(seen) == 2  # One source chunk and one synthesis, entirely mocked.
    assert all('pkg/source.py' in prompt and 'file_byte_limit' in prompt for prompt in seen)
    assert metadata['status'] == 'incomplete' and 'pkg/source.py' in metadata['omitted_files']


@pytest.mark.parametrize('selected', [False, True])
def test_untracked_capture_reuses_inventory_without_per_file_git_or_diff_paths(repo, monkeypatch, selected):
    anti, root, _ = repo
    for index in range(40): (root / f'new-{index}.py').write_text('fixture\n')
    calls = []
    original = subprocess.Popen
    def popen(argv, **kwargs):
        calls.append(list(argv))
        return original(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', popen)
    value = context(anti, '--include-untracked', *(['--file', 'untracked.py'] if selected else []))
    assert 'untracked.py' in dict(value['file_texts'])
    assert len([argv for argv in calls if 'ls-files' in argv]) == 1
    diffs = [argv for argv in calls if 'diff' in argv]
    assert all('untracked.py' not in argv and not any(str(arg).startswith('new-') for arg in argv) for argv in diffs)
    chunks, _ = anti.build_review_chunk_prompts(value, max_prompt_chars=8000, max_chunks=0)
    for chunk in chunks:
        if chunk['kind'] == 'diff':
            assert 'untracked.py' not in chunk['metadata']['included_files']


@pytest.mark.parametrize('required', [False, True])
def test_chunk_packing_preserves_captured_empty_files(repo, required):
    anti, root, git = repo
    (root / 'pkg/empty.py').write_bytes(b'')
    git('add', 'pkg/empty.py')
    value = context(anti, '--scope', 'repository', '--review-root', 'pkg',
                    *(['--required-file', 'pkg/empty.py'] if required else []))
    chunks, metadata = anti.build_review_chunk_prompts(value, max_prompt_chars=5000, max_chunks=0,
                                                       required_paths=['pkg/empty.py'] if required else None)
    assert metadata['status'] == 'complete' and not metadata['omitted_items']
    assert 'pkg/empty.py' in metadata['included_files']
    empty = next(row for row in metadata['coverage'] if row['path'] == 'pkg/empty.py')
    assert empty['contentStatus'] == 'complete' and empty['bytesSent'] == empty['bytesDeclared'] == 0
    assert empty['chunksExpected'] == empty['chunksSent'] == 1
    assert any('### pkg/empty.py\n```text\n\n```' in chunk['prompt'] for chunk in chunks)
