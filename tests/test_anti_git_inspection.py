"""Default repository inspection must use raw Git data and surface failures."""

import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "codex_antigravity_auth/skills/anti/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import anti


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout
    git("init", "-q")
    git("config", "user.name", "Synthetic fixture")
    git("config", "user.email", "fixture@example.invalid")
    (tmp_path / ".gitattributes").write_text("*.txt diff=fixture\n*.bin diff=fixture\n")
    (tmp_path / "sample.txt").write_text("before\n")
    (tmp_path / "old.txt").write_text("unchanged rename payload\n")
    (tmp_path / "data.bin").write_bytes(b"before\x00data")
    git("add", "-A")
    git("commit", "-qm", "base")
    converter = tmp_path / "converter.py"
    converter.write_text("from pathlib import Path\nPath('converter-ran').write_text('called')\nprint('CONVERTED CONTENT')\n")
    command = shlex.join([sys.executable, str(converter)])
    git("config", "diff.fixture.textconv", command)
    git("config", "diff.external", command)
    (tmp_path / "sample.txt").write_text("after\n")
    (tmp_path / "data.bin").write_bytes(b"after\x00data")
    git("mv", "old.txt", "new.txt")
    return tmp_path, git


@pytest.mark.parametrize("scope", ["staged", "working-tree", "diff"])
def test_inspection_never_invokes_configured_converter_or_external_diff(repo, scope, monkeypatch):
    root, git = repo
    if scope != "working-tree":
        git("add", "sample.txt", "data.bin")
    if scope == "diff":
        git("commit", "-qm", "change")
    # Positive control: this configured converter really executes without our guard.
    args = ["--cached"] if scope == "staged" else ["HEAD~1..HEAD"] if scope == "diff" else ["HEAD"]
    converted = git("diff", *args, "--no-ext-diff", "--", "sample.txt")
    assert (root / "converter-ran").exists()
    (root / "converter-ran").unlink()
    monkeypatch.chdir(root)
    args = anti.build_parser().parse_args(["review", "--scope", scope, *(["--changed-files", "HEAD~1..HEAD"] if scope == "diff" else [])])
    context = anti.collect_review_context(args)
    assert not (root / "converter-ran").exists()
    assert "-before" in context["diff"] and "+after" in context["diff"]
    assert "Binary files" in context["diff"]
    assert "CONVERTED CONTENT" not in context["diff"]
    assert {"old.txt", "new.txt", "sample.txt", "data.bin"} <= set(context["paths"])
    assert {"old.txt", "new.txt", "sample.txt", "data.bin"} <= {row["path"] for row in context["file_records"]}


@pytest.mark.parametrize("selected", [False, True])
def test_git_diff_error_cannot_become_empty_successful_scope(repo, monkeypatch, selected):
    root, git = repo
    monkeypatch.chdir(root)
    original = subprocess.run

    def run(args, **kwargs):
        if args[0] == "git" and "diff" in args:
            return subprocess.CompletedProcess(args, 128, b"" if not kwargs.get("text") else "", b"synthetic git failure" if not kwargs.get("text") else "synthetic git failure")
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    args = anti.build_parser().parse_args(["review", "--scope", "staged", *(["--file", "sample.txt"] if selected else [])])
    with pytest.raises(anti.AntiError, match="synthetic git failure"):
        anti.collect_review_context(args)


def test_failed_tracking_lookup_is_not_treated_as_untracked(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", MagicMock(return_value=subprocess.CompletedProcess([], 128, "", "synthetic index failure")))
    with pytest.raises(anti.AntiError, match="synthetic index failure"):
        anti.file_is_tracked(tmp_path, "sample.txt")
