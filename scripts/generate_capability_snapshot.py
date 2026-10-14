"""Regenerate Anti's standalone snapshot from pure built-in gateway definitions."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from codex_antigravity_auth.capability_catalog import standalone_snapshot

path = ROOT / "codex_antigravity_auth/skills/anti/scripts/anti_lib/capabilities.json"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
expected = json.dumps(standalone_snapshot(), indent=2, sort_keys=True) + "\n"
if args.check:
    if path.read_text() != expected:
        raise SystemExit("Anti capability snapshot drift; run scripts/generate_capability_snapshot.py")
else:
    path.write_text(expected)
