#!/usr/bin/env python3
"""Build the bounded Gemini listening qualification fixture manifest.

Deterministic: identical inputs produce a byte-identical manifest file.
Standard library only. No network access, no provider calls, and no writes
outside the requested output path. Evidence trees are opened read-only.

Ground-truth sources, all verified at build time:

  rows.json           fitted events (fit basis is the pre-compressor audio;
                      rows.audioSha256 records that basis, not the clip bytes)
  capture-receipts/   canonical per-clip audio hashes for the accepted capture
  capture-retry/media the audible post-compressor clip WAVs

A fixture is included only when its clip exists, its receipt hash matches the
clip bytes, and its row carries at least one fitted event. Every exclusion is
recorded in the manifest with its reason.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

MANIFEST_SCHEMA_VERSION = 1
CLASSES = ("core", "quiet", "repeat")
REFUSAL_CONTROLS = [
    {"controlId": "malformed-payload", "expected": "refused"},
    {"controlId": "stale-binding", "expected": "refused"},
    {"controlId": "expired-token", "expected": "refused"},
    {"controlId": "inventory-mismatch", "expected": "refused"},
    {"controlId": "oversized-file", "expected": "refused"},
    {"controlId": "wrong-sample-rate", "expected": "refused"},
    {"controlId": "stereo-input", "expected": "refused"},
    {"controlId": "unsupported-route", "expected": "refused"},
    {"controlId": "missing-binding", "expected": "refused"},
    {"controlId": "tampered-binding", "expected": "refused"},
    {"controlId": "second-file-over-limit", "expected": "refused"},
    {"controlId": "unauthorized-model", "expected": "refused"},
]


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def summarize_row(row):
    events = row.get("fit", {}).get("events") or []
    if not events:
        return None
    starts = sorted({round(float(e["fittedStartSeconds"]), 3) for e in events})
    pitches = Counter(int(e["midi"]) for e in events)
    amplitudes = [float(e["amplitude"]) for e in events]
    repeat_midi, repeat_count = pitches.most_common(1)[0]
    return {
        "events": [
            {
                "id": e["id"],
                "midi": int(e["midi"]),
                "startSeconds": round(float(e["fittedStartSeconds"]), 3),
                "amplitude": float(e["amplitude"]),
            }
            for e in sorted(events, key=lambda e: (float(e["fittedStartSeconds"]), int(e["midi"])))
        ],
        "temporalAttackCount": len(starts),
        "attackTimesSeconds": starts,
        "repeatPitch": {"midi": repeat_midi, "count": repeat_count} if repeat_count >= 2 else None,
        "quietRatio": round(min(amplitudes) / max(amplitudes), 6),
    }


def main():
    parser = argparse.ArgumentParser(description="Build qualification manifest")
    parser.add_argument("--rows", required=True, type=Path)
    parser.add_argument("--receipts-dir", required=True, type=Path)
    parser.add_argument("--media-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--quiet-threshold", type=float, default=0.15)
    parser.add_argument("--clip-ids", default="")
    args = parser.parse_args()

    rows = json.loads(args.rows.read_text())
    selected = {c.strip() for c in args.clip_ids.split(",") if c.strip()}

    fixtures = []
    exclusions = []
    for row in sorted(rows, key=lambda r: r["id"]):
        clip_id = row["id"]
        if selected and clip_id not in selected:
            continue
        summary = summarize_row(row)
        if summary is None:
            exclusions.append({"clipId": clip_id, "reason": "no fitted events"})
            continue
        receipt_path = args.receipts_dir / (clip_id + ".json")
        clip_path = args.media_dir / (clip_id + ".wav")
        if not receipt_path.is_file() or not clip_path.is_file():
            exclusions.append({"clipId": clip_id, "reason": "missing receipt or clip for accepted capture"})
            continue
        receipt = json.loads(receipt_path.read_text())
        receipt_hash = receipt.get("audio", {}).get("sha256")
        clip_hash = sha256_file(clip_path)
        if receipt_hash != clip_hash:
            exclusions.append({"clipId": clip_id, "reason": "clip bytes do not match capture receipt"})
            continue
        base = {
            "clipId": clip_id,
            "split": row.get("split"),
            "clipPath": str(clip_path),
            "clipSha256": clip_hash,
            "fitBasisSha256": row.get("audioSha256"),
            "captureReceiptPath": str(receipt_path),
            "quietThreshold": args.quiet_threshold,
        }
        base.update(summary)
        if 1 <= summary["temporalAttackCount"] <= 8:
            fixtures.append(dict(base, clipClass="core", question="How many distinct piano note attacks do you hear in this clip?", responseField="attackCount"))
        if summary["repeatPitch"] is not None:
            fixtures.append(dict(base, clipClass="repeat", question="How many times is the pitch repeated in this clip?", responseField="repeatPitchCount"))
        fixtures.append(dict(base, clipClass="quiet", question="Is any very quiet note present in this clip?", responseField="quietNotePresent", expectedQuietNotePresent=(summary["quietRatio"] <= args.quiet_threshold)))

    class_order = {"core": 0, "quiet": 1, "repeat": 2}
    fixtures.sort(key=lambda f: (class_order[f["clipClass"]], f["clipId"]))
    manifest = {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "quietThreshold": args.quiet_threshold,
        "fixtures": fixtures,
        "refusalControls": REFUSAL_CONTROLS,
        "exclusions": exclusions,
        "notes": [
            "Amplitudes are fitted on the pre-compressor audio (fitBasisSha256); post-compressor quiet ratios differ.",
            "The manifest is immutable once a run ID is minted. Regeneration requires a new run ID.",
        ],
    }
    payload = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(payload)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    args.out.with_suffix(args.out.suffix + ".sha256").write_text(digest + "  " + args.out.name + "\n")
    counts = Counter(f["clipClass"] for f in fixtures)
    print(json.dumps({"manifest": str(args.out), "sha256": digest, "fixtures": dict(counts), "exclusions": len(exclusions)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
