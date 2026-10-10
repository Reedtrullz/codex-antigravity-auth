"""Installation failure injection uses only synthetic temporary skill trees."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os

import pytest

from codex_antigravity_auth import cli


@pytest.fixture
def trees(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    manifest = {
        "SKILL.md": "bundled fixture\n",
        "agents/openai.yaml": "display_name: fixture\n",
        "scripts/anti.py": "raise RuntimeError('staged skill must never execute')\n",
        "scripts/anti_lib/helper.py": "fixture = True\n",
        "tests/test_anti.py": "raise RuntimeError('staged tests must never execute')\n",
    }
    for name, text in manifest.items():
        path = bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, newline="\n")
    skills = tmp_path / "skills"
    destination = skills / "anti"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text("customized previous skill\n")
    (destination / "notes.txt").write_text("user customization\n")
    foreign = skills / ".anti.stage-foreign"
    foreign.mkdir()
    (foreign / "untouched").write_text("unrelated staging path\n")
    monkeypatch.setattr(cli, "bundled_skill_root", lambda: bundle)
    return bundle, skills, destination, cli._path_tree_manifest(destination)


def assert_restored(trees):
    _, skills, destination, old = trees
    assert cli._path_tree_manifest(destination) == old
    assert (skills / ".anti.stage-foreign/untouched").read_text() == "unrelated staging path\n"
    assert sorted(path.name for path in skills.glob(".anti.stage-*")) == [".anti.stage-foreign"]


@pytest.mark.parametrize("failure", ["copy", "stage-validation", "backup-rename", "publish-rename", "final-validation"])
def test_failed_upgrade_preserves_previous_installation(monkeypatch, trees, failure):
    _, skills, destination, _ = trees
    copy = cli._copy_resource_tree
    rename = Path.rename
    manifest = cli._path_tree_manifest

    def copy_tree(source, target):
        copy(source, target)
        if failure == "copy":
            raise OSError("synthetic copy failure")
        if failure == "stage-validation":
            (target / "SKILL.md").write_text("corrupt staged copy")

    def rename_path(source, target):
        if failure == "backup-rename" and source == destination:
            raise OSError("synthetic backup rename failure")
        if failure == "publish-rename" and source.name == "payload":
            raise OSError("synthetic publish rename failure")
        return rename(source, target)

    def read_manifest(path):
        result = manifest(path)
        if failure == "final-validation" and path == destination and result.get("SKILL.md") == b"bundled fixture\n":
            raise OSError("synthetic final validation failure")
        return result

    monkeypatch.setattr(cli, "_copy_resource_tree", copy_tree)
    monkeypatch.setattr(Path, "rename", rename_path)
    monkeypatch.setattr(cli, "_path_tree_manifest", read_manifest)
    with pytest.raises((RuntimeError, OSError)):
        cli.install_codex_skill(skills, force=True)
    assert_restored(trees)


def test_success_validates_all_files_and_keeps_user_backup(trees):
    bundle, skills, destination, old = trees
    action, installed, backup = cli.install_codex_skill(skills, force=True)
    assert action == "replaced" and installed == destination
    assert cli._path_tree_manifest(destination) == cli._path_tree_manifest(bundle)
    assert cli._path_tree_manifest(backup) == old
    if os.name != "nt":
        assert destination.stat().st_mode & 0o777 == 0o700
        assert (destination / "scripts/anti.py").stat().st_mode & 0o777 == 0o755
        assert (destination / "SKILL.md").stat().st_mode & 0o777 == 0o644
    assert sorted(path.name for path in skills.glob(".anti.stage-*")) == [".anti.stage-foreign"]
    assert cli.install_codex_skill(skills)[0] == "unchanged"


def test_failed_restore_reports_exact_recovery_paths(monkeypatch, trees):
    _, skills, destination, old = trees
    rename = Path.rename
    def fail_rename(source, target):
        if source.name == "payload" or ".backup-" in source.name:
            raise OSError("synthetic publication/restore failure")
        return rename(source, target)
    monkeypatch.setattr(Path, "rename", fail_rename)
    with pytest.raises(RuntimeError, match="automatic restore failed") as exc:
        cli.install_codex_skill(skills, force=True)
    backups = list(cli._skill_backup_root(skills).iterdir())
    assert len(backups) == 1
    assert cli._path_tree_manifest(backups[0]) == old
    assert str(backups[0]) in str(exc.value)
    assert str(destination) in str(exc.value)
    assert not destination.exists()


def test_cleanup_failure_cannot_hide_failed_restore_recovery_paths(monkeypatch, trees):
    _, skills, destination, old = trees
    rename = Path.rename
    cleanup = cli.tempfile.TemporaryDirectory.cleanup
    def fail_rename(source, target):
        if source.name == "payload" or ".backup-" in source.name:
            raise OSError("synthetic publication/restore failure")
        return rename(source, target)
    def fail_cleanup(self):
        cleanup(self)
        raise OSError("synthetic cleanup failure")
    monkeypatch.setattr(Path, "rename", fail_rename)
    monkeypatch.setattr(cli.tempfile.TemporaryDirectory, "cleanup", fail_cleanup)
    with pytest.raises(RuntimeError, match="automatic restore failed") as exc:
        cli.install_codex_skill(skills, force=True)
    backup = next(cli._skill_backup_root(skills).iterdir())
    assert cli._path_tree_manifest(backup) == old
    assert "cleanup also failed" in str(exc.value)
    assert str(backup) in str(exc.value) and str(destination) in str(exc.value)
    assert not destination.exists()


def test_cleanup_failure_reports_successful_install_and_retained_backup(monkeypatch, trees):
    bundle, skills, destination, old = trees
    original = cli.tempfile.TemporaryDirectory.cleanup
    def fail_cleanup(self):
        original(self)
        raise OSError("synthetic final cleanup failure")
    monkeypatch.setattr(cli.tempfile.TemporaryDirectory, "cleanup", fail_cleanup)
    with pytest.raises(RuntimeError, match="Skill is installed") as exc:
        cli.install_codex_skill(skills, force=True)
    assert cli._path_tree_manifest(destination) == cli._path_tree_manifest(bundle)
    backup = next(cli._skill_backup_root(skills).iterdir())
    assert cli._path_tree_manifest(backup) == old
    assert str(destination) in str(exc.value) and str(backup) in str(exc.value)


def test_dry_run_creates_no_staging_lock_or_backup(trees):
    _, skills, destination, _ = trees
    before = cli._path_tree_manifest(skills.parent)
    action, installed, backup = cli.install_codex_skill(skills, force=True, dry_run=True)
    assert action == "replaced" and installed == destination and backup is not None
    assert cli._path_tree_manifest(skills.parent) == before
    assert not cli._skill_backup_root(skills).exists()


def test_concurrent_installers_preserve_one_backup_without_partial_tree(trees):
    bundle, skills, destination, old = trees
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: cli.install_codex_skill(skills, force=True), range(2)))
    assert sorted(result[0] for result in results) == ["replaced", "unchanged"]
    assert cli._path_tree_manifest(destination) == cli._path_tree_manifest(bundle)
    backups = list(cli._skill_backup_root(skills).iterdir())
    assert len(backups) == 1 and cli._path_tree_manifest(backups[0]) == old


def test_partial_bundle_is_rejected_before_old_install_is_moved(trees):
    bundle, skills, _, _ = trees
    (bundle / "scripts/anti.py").unlink()
    with pytest.raises(RuntimeError, match="incomplete"):
        cli.install_codex_skill(skills, force=True)
    assert_restored(trees)


def test_fresh_install_copy_failure_publishes_no_partial_skill(monkeypatch, trees):
    _, skills, destination, _ = trees
    empty = skills.parent / "fresh-skills"
    monkeypatch.setattr(cli, "_copy_resource_tree", lambda *args: (_ for _ in ()).throw(OSError("synthetic copy failure")))
    with pytest.raises(OSError):
        cli.install_codex_skill(empty)
    assert not (empty / "anti").exists()
    assert list(empty.glob(".anti.stage-*")) == []
    assert destination.exists()
