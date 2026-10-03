"""Owned modules must be usable without importing either orchestration entrypoint."""
import subprocess
import sys
from pathlib import Path

import codex_antigravity_auth


def test_owned_contracts_import_without_cli_or_server_back_imports(tmp_path):
    scripts=Path(codex_antigravity_auth.__file__).parent/'skills/anti/scripts'
    probe='''
import builtins, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in {'anti', 'codex_antigravity_auth.server', 'codex_antigravity_auth.cli'}:
        raise AssertionError('entrypoint back-import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from anti_lib.context import ordered_prompt, coverage_summary, build_review_prompt
from anti_lib.run_records import publish_unlocked, check_record_retention
from codex_antigravity_auth.route_lifecycle import write_route_lifecycle
assert ordered_prompt([' first ', None, 'second']) == 'first\\n\\nsecond'
assert callable(coverage_summary) and callable(build_review_prompt)
assert callable(publish_unlocked) and callable(check_record_retention)
assert callable(write_route_lifecycle)
assert not {'anti', 'codex_antigravity_auth.server', 'codex_antigravity_auth.cli'} & sys.modules.keys()
'''
    result=subprocess.run([sys.executable,'-c',probe,str(scripts)],cwd=tmp_path,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr



def test_compatibility_byte_cap_applies_to_direct_decode_and_file_read(tmp_path):
    import importlib.util
    script=Path(codex_antigravity_auth.__file__).parent/'skills/anti/scripts/anti.py'
    spec=importlib.util.spec_from_file_location('anti_cap_compatibility',script)
    anti=importlib.util.module_from_spec(spec);spec.loader.exec_module(anti)
    anti.MAX_FILE_BYTES=7
    raw=('😀'*10).encode('utf-8')
    (tmp_path/'fixture.txt').write_bytes(raw)
    direct=anti.decode_source_bytes('fixture.txt',raw)
    assert direct==anti.read_text_file(tmp_path,'fixture.txt')
    assert direct[0]=='😀' and 'truncated to 7 bytes' in direct[1]
    assert anti.decode_source_bytes('fixture.txt',raw,truncate=False)==('😀'*10,None)
