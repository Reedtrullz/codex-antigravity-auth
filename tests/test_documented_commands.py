"""Parse documented commands without dispatch, user state, providers or services."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shlex
from unittest.mock import patch

import pytest

from codex_antigravity_auth import cli

ROOT = Path(__file__).resolve().parents[1]
DOCS = ('README.md', 'USAGE.md', 'STATUS.md', 'VERIFICATION.md')


def documented_commands():
    for name in DOCS:
        text = (ROOT / name).read_text(encoding='utf-8')
        for block in re.findall(r'^```(?:bash|sh|shell)\n(.*?)^```', text, re.M | re.S):
            for line in block.replace('\\\n', ' ').splitlines():
                words = shlex.split(line, comments=True)
                if words and words[0] == 'codex-antigravity':
                    yield name, 'gateway', words[1:]
                elif len(words) >= 2 and words[0] in {'python', 'python3'} and words[1].endswith('/anti.py'):
                    yield name, 'anti', words[2:]


class ParsedOnly(Exception):
    pass


@pytest.mark.parametrize('document,kind,args', list(documented_commands()))
def test_current_documented_commands_parse_without_dispatch(document, kind, args):
    if kind == 'anti':
        spec = importlib.util.spec_from_file_location('documented_anti', ROOT / 'codex_antigravity_auth/skills/anti/scripts/anti.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        parser = module.build_parser()
        try:
            parser.parse_args(args)
        except SystemExit as error:
            assert error.code == 0, (document, args)
    else:
        original = argparse.ArgumentParser.parse_args
        def parse_only(parser, *unused, **kwargs):
            original(parser, args)
            raise ParsedOnly
        with patch.object(argparse.ArgumentParser, 'parse_args', parse_only):
            try:
                cli.main()
            except SystemExit as error:
                assert error.code == 0, (document, args)
            except ParsedOnly:
                pass
            else:
                pytest.fail('CLI dispatch was reached')


def test_historical_snapshots_retain_exact_bytes():
    manifest = json.loads((ROOT / 'docs/history/2026-10-01/manifest.json').read_text())
    assert manifest['files']
    for entry in manifest['files']:
        assert hashlib.sha256((ROOT / entry['archivePath']).read_bytes()).hexdigest() == entry['sha256']
        assert len(entry['sourceCommit']) == 40


def test_current_docs_link_one_status_owner_and_retire_auth_claims():
    for name in ('README.md', 'USAGE.md', 'VERIFICATION.md', 'AGENTS.md'):
        assert '(STATUS.md)' in (ROOT / name).read_text()
    assert 'Account, provider, xAI OAuth' not in (ROOT / 'docs/refactor-migration.md').read_text()
    assert len(list(documented_commands())) > 60
