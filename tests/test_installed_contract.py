"""Also copied outside the checkout by the installed-artifact release gate."""
from pathlib import Path
import subprocess
import sys

from codex_antigravity_auth import cli, google_transport


def test_installed_origin_and_complete_skill(tmp_path):
    # In the external harness there is no package next to this test. Require the
    # imported module to live in that interpreter's venv, not an editable tree.
    here = Path(__file__).resolve().parent
    if not (here.parent / "pyproject.toml").exists():
        assert Path(cli.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    names = cli.bundled_skill_asset_names()
    assert "scripts/anti_lib/reflections.py" in names
    assert "scripts/anti_lib/verifier.py" in names
    assert "scripts/anti_lib/capabilities.json" in names
    assert "scripts/anti_lib/capabilities.py" in names
    assert any(name.startswith("tests/fixtures/") for name in names)
    action, destination, _ = cli.install_codex_skill(tmp_path / "skills")
    assert action == "installed"
    assert cli.codex_skill_matches_bundled(destination)
    # Verify and execute the installed standalone copy in an isolated child.
    assert cli.verify_codex_skill(destination)
    for args in (["--help"], ["doctor", "--help"], ["provider", "presets"]):
        result = subprocess.run([sys.executable, "-m", "codex_antigravity_auth.cli", *args], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_actual_os_metadata_is_distinct_from_legacy_backend_platform(monkeypatch):
    # The IDE user-agent declares the real OS. get_platform's legacy upstream
    # field is deliberately not changed without evidence for that wire contract.
    monkeypatch.setattr(google_transport, "_IDE_USER_AGENT_CACHE", None)
    import platform
    assert "os_type=" + platform.system().lower() in google_transport.ide_user_agent()
