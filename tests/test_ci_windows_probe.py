import json
import os
from pathlib import Path
import tempfile

import pytest


def test_ci_windows_probe(request):
    with tempfile.TemporaryDirectory(prefix="ci-probe-") as tmp:
        path = Path(tmp) / "accounts.json"
        path.write_text("{\"probe\": true}")
        path.chmod(0o600)
        report = {}
        combos = {
            "readonly": ["O_RDONLY"],
            "binary": ["O_RDONLY", "O_BINARY"],
            "nonblock": ["O_RDONLY", "O_NONBLOCK"],
            "nofollow": ["O_RDONLY", "O_NOFOLLOW"],
            "full": ["O_RDONLY", "O_NOFOLLOW", "O_NONBLOCK"],
            "binfull": ["O_RDONLY", "O_BINARY", "O_NOFOLLOW", "O_NONBLOCK"],
        }
        for name, parts in combos.items():
            code = 0
            for part in parts:
                code |= getattr(os, part, 0)
            try:
                descriptor = os.open(path, code)
                os.close(descriptor)
                report[name] = "OK"
            except OSError as exc:
                report[name] = type(exc).__name__ + ": errno=" + str(exc.errno)
        try:
            path.read_bytes()
            report["pathlib_read"] = "OK"
        except OSError as exc:
            report["pathlib_read"] = type(exc).__name__ + ": errno=" + str(exc.errno)
        report["platform"] = os.name
        pytest.fail("CI_PROBE: " + json.dumps(report, sort_keys=True))
