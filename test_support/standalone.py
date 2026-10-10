"""Keep test startup isolation while removing installed-package import paths."""


def without_installed_packages(source):
    return '''
import os, sys
import _test_isolation
assert _test_isolation._installed
sys.path[:] = [os.getcwd()] + [p for p in sys.path if p and 'site-packages' not in p and 'dist-packages' not in p]
sys.meta_path[:] = [finder for finder in sys.meta_path if not getattr(finder, '__module__', '').startswith('__editable__')]
from importlib.util import find_spec
assert find_spec('codex_antigravity_auth') is None
''' + source
