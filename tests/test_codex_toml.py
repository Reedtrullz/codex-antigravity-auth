"""All configuration, symlink and concurrency fixtures are temporary/synthetic."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import threading

import pytest
import tomlkit

from codex_antigravity_auth import cli, codex_config


def parsed(text):
    return tomlkit.parse(text).unwrap()


@pytest.mark.parametrize("quotes", ['"""', "'''"])
def test_multiline_fake_headers_and_keys_remain_literal_text(quotes):
    content = (f"notes = {quotes}\n[model_providers.antigravity]\nbase_url = 'fake'\n"
               f"model = 'not the active model'\n{quotes}\nmodel = 'gpt-5' # keep selection\n")
    result = cli.merge_codex_config(content)
    assert parsed(result)["notes"] == parsed(content)["notes"]
    assert f"notes = {quotes}\n[model_providers.antigravity]" in result
    assert "model = 'gpt-5' # keep selection" in result
    assert parsed(result)["model_providers"]["antigravity"]["base_url"] == cli.DEFAULT_CODEX_BASE_URL
    assert "model_provider" not in parsed(result)
    assert cli.parse_codex_config(content)["provider_tables"] == {}


@pytest.mark.parametrize("header", [
    '[model_providers."antigravity"] # original header',
    "[ 'model_providers' . 'antigravity' ] # spaced literal keys",
    '["model_providers"."antigravity"]',
])
def test_quoted_headers_and_inline_comments_preserve_unrelated_values(header):
    source = f'''# fixture comment
model = "gpt-5"
{header}
name = "old" # name comment
base_url = "http://localhost:1234/v1" # URL comment
extra = {{ retries = 3, text = "# literal hash" }}
[profiles.work]
approval_policy = "never" # unrelated
'''
    result = cli.merge_codex_config(source)
    data = parsed(result)
    assert header in result
    assert "# name comment" in result and "# URL comment" in result
    assert data["model_providers"]["antigravity"]["extra"] == parsed(source)["model_providers"]["antigravity"]["extra"]
    assert data["profiles"] == parsed(source)["profiles"]
    assert result.count("# unrelated") == 1
    assert cli.merge_codex_config(result) == result


@pytest.mark.parametrize("source", [
    'model_providers = { antigravity = { name = "old", base_url = "http://localhost:1234/v1", extra = 7 }, other = { name = "kept" } }\n',
    'model_providers = { other = { name = "kept" } }\n',
    'model_providers.antigravity.name = "old"\nmodel_providers.other.name = "kept"\n',
    '[model_providers.antigravity]\nname="old"\n[profiles.work]\nnotes="kept"\n[model_providers.antigravity.headers]\nfixture="retained"\n',
])
def test_inline_dotted_and_out_of_order_tables_are_updated_semantically(source):
    result = cli.merge_codex_config(source)
    expected = parsed(source)
    expected.setdefault("model_providers", {}).setdefault("antigravity", {}).update(
        name=cli.DEFAULT_CODEX_PROVIDER_NAME, base_url=cli.DEFAULT_CODEX_BASE_URL, wire_api="responses")
    assert parsed(result) == expected
    assert cli.merge_codex_config(result) == result


def test_activation_changes_only_the_explicit_root_keys():
    source = '''# root comment
"model" = 'gpt-5' # preserve comment
"model_provider" = 'existing'
wire_api = 'old'
[profiles.work]
model = "profile-model"
["model_providers"."literal.dot"]
name = "untouched"
'''
    result = cli.merge_codex_config(source, model="claude-sonnet-4-6", activate=True)
    data = parsed(result)
    assert data["model"] == "claude-sonnet-4-6"
    assert data["model_provider"] == "antigravity"
    assert data["wire_api"] == "responses"
    assert data["profiles"] == parsed(source)["profiles"]
    assert data["model_providers"]["literal.dot"] == {"name": "untouched"}
    assert "# root comment" in result and "# preserve comment" in result


def test_native_toml_types_and_nan_survive_untouched():
    source = '''float = nan
negative = -0.0
infinite = inf
flag = true
count = 1
date = 2026-09-30
moment = 2026-09-30T12:00:00Z
array = [1, 2, 3]
'''
    result = cli.merge_codex_config(source)
    assert result.startswith(source)
    assert codex_config._same_value(parsed(source), {key: parsed(result)[key] for key in parsed(source)})


@pytest.mark.parametrize("source", [
    'model="one"\nmodel="two"\n', '[model_providers.antigravity\n',
    'notes="""unfinished\n', 'model_providers = 3\n',
    'model_providers.antigravity = "not a table"\n',
    '[[model_providers.antigravity]]\nname="array table"\n',
])
def test_invalid_or_ambiguous_config_has_no_writes(tmp_path, source):
    path = tmp_path / "config.toml"
    path.write_text(source)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    mode = path.stat().st_mode
    with pytest.raises(ValueError):
        cli.write_codex_config(path)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert path.stat().st_mode == mode


def test_post_edit_semantic_guard_prevents_unrelated_change(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    original = 'unrelated = 3\n'
    path.write_text(original)
    dumps = codex_config.tomlkit.dumps
    monkeypatch.setattr(codex_config.tomlkit, "dumps", lambda value: dumps(value).replace("unrelated = 3", "unrelated = 4"))
    with pytest.raises(ValueError, match="unrelated"):
        cli.write_codex_config(path)
    assert path.read_text() == original
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]


def test_serialized_output_is_reparsed_before_backup_or_write(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('model="gpt-5"\n')
    monkeypatch.setattr(codex_config.tomlkit, "dumps", lambda value: 'invalid = [')
    with pytest.raises(ValueError, match="valid TOML"):
        cli.write_codex_config(path)
    assert path.read_text() == 'model="gpt-5"\n'
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]


def test_concurrent_writers_reread_after_acquiring_canonical_lock(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    source = '# kept\nmodel="gpt-5"\nnotes="shared unrelated setting"\n'
    target.write_text(source)
    barrier = threading.Barrier(4)
    local = threading.local()
    merge = cli.merge_codex_config
    def synchronized_preflight(*args, **kwargs):
        if not getattr(local, "preflight_done", False):
            local.preflight_done = True
            barrier.wait(timeout=5)
        return merge(*args, **kwargs)
    monkeypatch.setattr(cli, "merge_codex_config", synchronized_preflight)
    def update(index):
        return cli.write_codex_config(target, provider_id=f"fixture-{index}", provider_name=f"Fixture {index}")
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(update, range(4)))
    final = parsed(target.read_text())
    assert set(final["model_providers"]) == {f"fixture-{i}" for i in range(4)}
    assert final["model"] == "gpt-5" and final["notes"] == "shared unrelated setting"
    assert target.read_text().startswith("# kept\n")
    backups = [backup for changed, backup in results if changed]
    assert len(set(backups)) == 4
    assert sorted(len(parsed(path.read_text()).get("model_providers", {})) for path in backups) == [0, 1, 2, 3]


def test_changed_symlink_target_is_rejected_before_backup(tmp_path, monkeypatch):
    first, second, link = (tmp_path / name for name in ("first.toml", "second.toml", "config.toml"))
    first.write_text('model="first"\n')
    second.write_text('model="second"\n')
    try:
        link.symlink_to(first)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    lock = cli.file_lock
    @contextmanager
    def redirected(path):
        with lock(path):
            link.unlink()
            link.symlink_to(second)
            yield
    monkeypatch.setattr(cli, "file_lock", redirected)
    with pytest.raises(ValueError, match="target changed"):
        cli.write_codex_config(link)
    assert first.read_text() == 'model="first"\n'
    assert second.read_text() == 'model="second"\n'
    assert not list(tmp_path.glob("*.bak-*"))


def test_inspection_fails_cleanly_on_invalid_toml():
    for inspect in (cli.inspect_codex_gateway_config, cli.inspect_codex_provider_block_config):
        ready, reason = inspect('model="one"\nmodel="two"', provider_id="antigravity", expected_base_url=cli.DEFAULT_CODEX_BASE_URL)
        assert ready is False and "valid TOML" in reason


def test_inspection_uses_tables_not_multiline_notes():
    source = '''model_provider = "antigravity"
notes = """
[model_providers.antigravity]
base_url = "http://localhost:51122/v1"
wire_api = "responses"
"""
'''
    ready, reason = cli.inspect_codex_gateway_config(source, provider_id="antigravity", expected_base_url=cli.DEFAULT_CODEX_BASE_URL)
    assert not ready and "missing" in reason


def test_invalid_utf8_parameter_fails_without_creating_state(tmp_path):
    path = tmp_path / "missing" / "config.toml"
    with pytest.raises(ValueError):
        cli.write_codex_config(path, provider_name="invalid-\ud800")
    assert not path.parent.exists()


def test_literal_dotted_root_table_is_not_the_managed_provider():
    source = '["model_providers.antigravity"]\nname = "unrelated literal table"\n'
    result = cli.merge_codex_config(source)
    assert parsed(result)["model_providers.antigravity"] == parsed(source)["model_providers.antigravity"]
    assert parsed(result)["model_providers"]["antigravity"]["name"] == cli.DEFAULT_CODEX_PROVIDER_NAME


def test_crlf_backup_is_byte_exact_and_repeated_write_is_a_noop(tmp_path):
    path = tmp_path / "config.toml"
    original = b'# fixture comment\r\nmodel = "gpt-5"\r\n'
    path.write_bytes(original)
    changed, backup = cli.write_codex_config(path)
    assert changed and backup.read_bytes() == original
    updated = path.read_bytes()
    assert updated.startswith(original)
    assert b"\r\r\n" not in updated
    assert cli.write_codex_config(path) == (False, None)
    assert path.read_bytes() == updated


def test_invalid_utf8_config_is_preserved_without_locks_or_backups(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xffsynthetic invalid encoding")
    with pytest.raises(ValueError, match="UTF-8"):
        cli.write_codex_config(path)
    assert path.read_bytes() == b"\xffsynthetic invalid encoding"
    assert list(tmp_path.iterdir()) == [path]


def test_symlinked_lock_is_refused_without_changing_its_target(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model="gpt-5"\n')
    outside = tmp_path / "keep.txt"
    outside.write_bytes(b"synthetic unrelated bytes")
    mode = outside.stat().st_mode
    try:
        (tmp_path / ".config.toml.lock").symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    with pytest.raises(ValueError, match="symlinked"):
        cli.write_codex_config(config)
    assert outside.read_bytes() == b"synthetic unrelated bytes"
    assert outside.stat().st_mode == mode
    assert config.read_text() == 'model="gpt-5"\n'


def test_invalid_replacement_after_preflight_does_not_write_config_or_backup(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('model="gpt-5"\n')
    lock = cli.file_lock
    changed = 'model="one"\nmodel="two"\n'
    @contextmanager
    def external_update(path):
        with lock(path):
            config.write_text(changed)
            yield
    monkeypatch.setattr(cli, "file_lock", external_update)
    with pytest.raises(ValueError, match="valid TOML"):
        cli.write_codex_config(config)
    assert config.read_text() == changed
    assert not list(tmp_path.glob("*.bak-*"))


def test_process_writers_do_not_lose_updates_after_shared_preflight(tmp_path):
    import subprocess
    import sys
    import time
    config = tmp_path / "config.toml"
    config.write_text('model="gpt-5"\nnotes="retained"\n')
    gate = tmp_path / "release"
    code = r'''
import sys, time
from pathlib import Path
from codex_antigravity_auth import cli
config, gate, ready, provider = sys.argv[1:]
original = cli.merge_codex_config
first = True
def merge(*args, **kwargs):
    global first
    if first:
        first = False
        Path(ready).write_text("ready")
        deadline = time.monotonic() + 10
        while not Path(gate).exists():
            if time.monotonic() > deadline:
                raise RuntimeError("synthetic gate timed out")
            time.sleep(0.01)
    return original(*args, **kwargs)
cli.merge_codex_config = merge
cli.write_codex_config(Path(config), provider_id=provider)
'''
    ready = [tmp_path / f"ready-{i}" for i in range(2)]
    processes = [subprocess.Popen([sys.executable, "-c", code, str(config), str(gate), str(ready[i]), f"fixture-{i}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(2)]
    try:
        deadline = time.monotonic() + 10
        while not all(path.exists() for path in ready):
            assert time.monotonic() < deadline, "synthetic workers did not reach preflight"
            time.sleep(0.01)
        gate.write_text("go")
        for process in processes:
            _, error = process.communicate(timeout=10)
            assert process.returncode == 0, error
    finally:
        gate.touch()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    result = parsed(config.read_text())
    assert set(result["model_providers"]) == {"fixture-0", "fixture-1"}
    assert result["notes"] == "retained" and result["model"] == "gpt-5"
    assert len(list(tmp_path.glob("config.toml.bak-*"))) == 2
