"""Generate local, source-bound test/artifact evidence without live credentials."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


def source_identity(root):
    def git(*args):
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        return subprocess.check_output(['git', '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false', *args],
                                       cwd=root, env=env, text=True).strip()
    return {'commit': git('rev-parse', 'HEAD'),
            'dirty': bool(git('status', '--porcelain', '--untracked-files=normal'))}


def junit_counts(path):
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == 'testsuite' else list(root.findall('testsuite'))
    if not suites:
        raise ValueError('JUnit report has no test suites')
    counts = {name: sum(int(suite.attrib[name]) for suite in suites)
              for name in ('tests', 'failures', 'errors', 'skipped')}
    if any(value < 0 for value in counts.values()):
        raise ValueError('JUnit counts must be nonnegative')
    counts['countBasis'] = 'JUnit test cases; pytest subtests may be separate cases'
    return counts


def artifact_hashes(dist):
    paths = sorted([*dist.glob('*.whl'), *dist.glob('*.tar.gz')])
    if len([p for p in paths if p.suffix == '.whl']) != 1 or len([p for p in paths if p.name.endswith('.tar.gz')]) != 1:
        raise ValueError('expected exactly one wheel and one sdist')
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def run_step(name, args, root, output):
    with (output / (name + '.log')).open('w', encoding='utf-8') as log:
        result = subprocess.run(args, cwd=root, stdout=log, stderr=subprocess.STDOUT)
    return {'exitCode': result.returncode, 'log': name + '.log'}


def generate(root, output, dist=None):
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError('evidence output directory must be empty')
    version = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    before = source_identity(root)
    report = {'schemaVersion': 1, 'startedAt': datetime.now(timezone.utc).isoformat(),
              'packageVersion': version, 'sourceBefore': before,
              'runtime': {'python': platform.python_version(), 'implementation': platform.python_implementation(),
                          'system': platform.system(), 'release': platform.release(), 'machine': platform.machine()},
              'tests': None, 'artifacts': {'status': 'not_checked'},
              'artifactBuildProvenance': 'not_attested',
              'nonClaims': ['No live provider verification', 'No native service-manager verification',
                            'No hosted CI result', 'No publication or deployed-artifact verification',
                            'Supplied artifact hashes and installed tests do not attest build provenance'],
              'errors': []}
    try:
        tests = run_step('tests', [sys.executable, str(root / 'scripts/run_tests.py'), '-q',
                                  '--junitxml', str(output / 'tests.xml')], root, output)
        report['tests'] = tests
        try:
            tests['junit'] = junit_counts(output / 'tests.xml')
        except (OSError, ValueError, KeyError, ET.ParseError) as error:
            # Fixed error type only: no arbitrary test/provider content in report.
            report['errors'].append('test_report_' + type(error).__name__)
        if dist is not None:
            hashes = artifact_hashes(dist)
            artifacts = {'status': 'checking', 'before': hashes}
            report['artifacts'] = artifacts
            artifacts['contents'] = run_step('artifact-contents', [sys.executable, str(root / 'scripts/check_artifacts.py'),
                                                                  '--dist', str(dist)], root, output)
            artifacts['installed'] = run_step('artifact-installed', [sys.executable, str(root / 'scripts/check_installed.py'),
                                                                     '--dist', str(dist)], root, output)
            artifacts['after'] = artifact_hashes(dist)
            artifacts['unchanged'] = artifacts['before'] == artifacts['after']
            artifacts['status'] = ('passed' if artifacts['unchanged'] and artifacts['contents']['exitCode'] == 0
                                   and artifacts['installed']['exitCode'] == 0 else 'failed')
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report['errors'].append('verification_' + type(error).__name__)
    finally:
        try:
            report['sourceAfter'] = source_identity(root)
        except (OSError, subprocess.SubprocessError):
            report['sourceAfter'] = None
            report['errors'].append('source_identity_unavailable')
        report['finishedAt'] = datetime.now(timezone.utc).isoformat()
        report['sourceUnchanged'] = before == report['sourceAfter']
        report['exactCleanRevision'] = report['sourceUnchanged'] and not before['dirty']
        tests = report['tests'] or {}
        counts = tests.get('junit', {})
        report['checksPassed'] = (not report['errors'] and tests.get('exitCode') == 0 and counts.get('tests', 0) > 0
                                  and counts.get('failures') == counts.get('errors') == 0
                                  and report['artifacts']['status'] in {'passed', 'not_checked'})
        report['revisionVerified'] = report['checksPassed'] and report['exactCleanRevision']
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='empty evidence directory, preferably outside checkout')
    parser.add_argument('--dist', type=Path, help='optional directory containing the built wheel and sdist')
    args = parser.parse_args()
    try:
        report = generate(ROOT, args.output.resolve(), args.dist.resolve() if args.dist is not None else None)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(2, f'Cannot generate evidence: {type(error).__name__}\n')
    print('Report:', args.output / 'report.json')
    return 0 if report['revisionVerified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
