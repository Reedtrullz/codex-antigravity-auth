"""Portable qualification manifest paths; synthetic trees only, no provider access."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
QUALIFICATION = ROOT / "scripts" / "qualification"
sys.path.insert(0, str(QUALIFICATION))

from manifest_paths import (  # noqa: E402
    ManifestPathError,
    evidence_root,
    portable_root,
    resolve_fixture_path,
)

MANIFEST = ROOT / "docs" / "qualification" / "fixture-manifest.json"


def test_committed_manifest_is_portable_and_matches_its_pin():
    payload = MANIFEST.read_bytes()
    pinned = (MANIFEST.parent / "fixture-manifest.json.sha256").read_text().split()[0]
    assert hashlib.sha256(payload).hexdigest() == pinned
    assert pinned in (MANIFEST.parent / "arming-runbook.md").read_text()
    text = payload.decode()
    assert "/Users/" not in text and "/home/" not in text
    manifest = json.loads(text)
    assert manifest["schemaVersion"] == 2
    assert manifest["evidenceRoot"].startswith("~/")
    assert len(manifest["fixtures"]) == 43
    for fixture in manifest["fixtures"]:
        for field in ("clipPath", "captureReceiptPath"):
            path = resolve_fixture_path(manifest, MANIFEST, fixture[field], override=Path("/evidence"))
            assert path == Path("/evidence").resolve() / fixture[field]


def test_root_override_relative_root_and_home_rendering(tmp_path):
    manifest = {"schemaVersion": 2, "evidenceRoot": "evidence"}
    assert evidence_root(manifest, tmp_path / "m.json") == (tmp_path / "evidence").resolve()
    assert evidence_root(manifest, tmp_path / "m.json", tmp_path / "other") == (tmp_path / "other").resolve()
    assert evidence_root({"evidenceRoot": "~/tree"}, tmp_path / "m.json") == (Path.home() / "tree").resolve()
    with pytest.raises(ManifestPathError):
        evidence_root({"schemaVersion": 2}, tmp_path / "m.json")
    assert portable_root(Path("/home/someone/x/y"), home=Path("/home/someone")) == "~/x/y"
    assert portable_root(Path("/srv/evidence"), home=Path("/home/someone")) == "/srv/evidence"


@pytest.mark.parametrize("value", ["/abs/clip.wav", "~/clip.wav", "../clip.wav", "media/../../clip.wav", "media\\clip.wav", ""])
def test_schema2_rejects_paths_that_are_not_contained_relatives(tmp_path, value):
    with pytest.raises(ManifestPathError):
        resolve_fixture_path({"schemaVersion": 2, "evidenceRoot": str(tmp_path)}, tmp_path / "m.json", value)


def test_schema1_absolute_paths_still_resolve_and_unknown_schema_fails(tmp_path):
    clip = tmp_path / "clip.wav"
    assert resolve_fixture_path({"schemaVersion": 1}, tmp_path / "m.json", str(clip)) == clip
    with pytest.raises(ManifestPathError):
        resolve_fixture_path({"schemaVersion": 1}, tmp_path / "m.json", "relative.wav")
    with pytest.raises(ManifestPathError):
        resolve_fixture_path({"schemaVersion": 3, "evidenceRoot": str(tmp_path)}, tmp_path / "m.json", "a.wav")


def test_builder_records_paths_relative_to_the_evidence_root(tmp_path):
    root = tmp_path / "fresh"
    media = root / "capture-retry" / "media"
    receipts = root / "capture-retry" / "capture-receipts"
    media.mkdir(parents=True)
    receipts.mkdir(parents=True)
    clip = media / "clip-a.wav"
    clip.write_bytes(b"synthetic clip bytes")
    digest = hashlib.sha256(clip.read_bytes()).hexdigest()
    (receipts / "clip-a.json").write_text(json.dumps({"audio": {"sha256": digest}}))
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"id": "clip-a", "split": "test", "audioSha256": "basis", "fit": {"events": [
        {"id": "e1", "midi": 60, "fittedStartSeconds": 0.5, "amplitude": 0.9},
        {"id": "e2", "midi": 60, "fittedStartSeconds": 1.0, "amplitude": 0.1},
    ]}}]))
    out = tmp_path / "out" / "manifest.json"
    subprocess.run([sys.executable, str(QUALIFICATION / "build_qualification_manifest.py"), "--rows", str(rows),
                    "--receipts-dir", str(receipts), "--media-dir", str(media), "--out", str(out),
                    "--evidence-root", str(root)], check=True, capture_output=True, text=True)
    manifest = json.loads(out.read_text())
    assert manifest["schemaVersion"] == 2
    assert manifest["evidenceRoot"] == portable_root(root.resolve())
    assert {f["clipPath"] for f in manifest["fixtures"]} == {"capture-retry/media/clip-a.wav"}
    assert {f["captureReceiptPath"] for f in manifest["fixtures"]} == {"capture-retry/capture-receipts/clip-a.json"}
    # Only evidenceRoot may name the machine-specific location.
    assert str(tmp_path.name) not in json.dumps(dict(manifest, evidenceRoot=None))
    assert resolve_fixture_path(manifest, out, "capture-retry/media/clip-a.wav") == clip.resolve()
    assert (out.parent / "manifest.json.sha256").read_text().split()[0] == hashlib.sha256(out.read_bytes()).hexdigest()


def test_builder_refuses_inputs_outside_the_evidence_root(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "rows.json").write_text("[]")
    result = subprocess.run([sys.executable, str(QUALIFICATION / "build_qualification_manifest.py"),
                             "--rows", str(tmp_path / "rows.json"), "--receipts-dir", str(tmp_path / "r"),
                             "--media-dir", str(tmp_path / "m"), "--out", str(tmp_path / "o.json"),
                             "--evidence-root", str(tmp_path / "root")], capture_output=True, text=True)
    assert result.returncode == 2 and "outside the evidence root" in result.stderr
