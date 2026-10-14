"""Inherited by test subprocesses through their guarded PYTHONPATH."""
import os
if os.environ.get("ANTIGRAVITY_TEST_ROOT"):
    try:
        from _test_isolation import install
        install()
    except BaseException:
        # Python normally prints sitecustomize errors and continues. Fail closed.
        os._exit(97)
