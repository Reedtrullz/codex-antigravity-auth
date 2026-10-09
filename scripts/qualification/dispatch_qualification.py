#!/usr/bin/env python3
"""Sequential dispatcher for the bounded Gemini listening qualification.

Runs the 43 manifest fixtures one at a time through the bundled Anti helper,
appending every response, latency, and confidence record to the run record
immediately after each attempt. One backend attempt per fixture; no retries,
no fallback, no parallelism. Arming (run-ID minting) remains an owner gate:
the run record is created only when the owner-supplied run ID is provided.

The run record is scorer-compatible: each entry carries a structured
"response" object parsed from the helper's JSON output, and the record
carries "controls" (from --controls-file, produced by refusal_controls.py)
and "gateway" RSS metrics (from --gateway-metrics). A fixture whose output
is not valid JSON is recorded with response null and is scored incorrect;
it is never guessed or repaired.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import wave
from typing import Any, NoReturn
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ANTI = REPO_ROOT / "codex_antigravity_auth" / "skills" / "anti" / "scripts" / "anti.py"


def fail(message: str) -> NoReturn:
    print(json.dumps({"error": message}))
    raise SystemExit(2)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_clip_profile(path: Path) -> None:
    if path.stat().st_size > 2 * 1024 * 1024:
        fail(f"clip exceeds 2 MiB: {path}")
    with path.open("rb") as handle:
        header = handle.read(12)
    if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        fail(f"clip is not RIFF/WAVE: {path}")
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        rate = source.getframerate()
        width = source.getsampwidth()
        frames = source.getnframes()
    if width != 2:
        fail(f"clip is not 16-bit PCM: {path}")
    if channels != 1:
        fail(f"clip is not mono: {path}")
    if rate != 44100:
        fail(f"clip sample rate is not 44100 Hz: {path}")
    if frames > 44100 * 30:
        fail(f"clip exceeds 30 seconds: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Dispatch qualification fixtures sequentially")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", required=True, help="Isolated gateway base URL ending in /v1")
    parser.add_argument("--run-id", required=True, help="Owner-minted immutable run ID")
    parser.add_argument("--run-record", required=True, type=Path)
    parser.add_argument("--anti", type=Path, default=ANTI, help="Explicit helper path override")
    parser.add_argument("--authorized-model", required=True, help="The single model authorized for this run")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--controls-file", type=Path, default=None,
                        help="Refusal-control results JSON from refusal_controls.py")
    parser.add_argument("--gateway-metrics", type=Path, default=None,
                        help="JSON file with gateway rssPeakBytes")
    parser.add_argument("--stop-after", type=int, default=None, help="Dev-only bounded self-check; not for live runs")
    parser.add_argument("--dry-run", action="store_true", help="Assemble and print commands without contacting the gateway")
    args = parser.parse_args()

    if not args.manifest.is_absolute() or not args.manifest.is_file():
        fail("manifest path must be an absolute existing file")
    if not args.binding.is_absolute() or not args.binding.is_file():
        fail("binding path must be an absolute existing file")
    if args.run_record.exists():
        fail("run record already exists; a minted run cannot be restarted")
    if not args.run_record.is_absolute():
        fail("run record path must be absolute")
    if args.model != args.authorized_model:
        fail(f"model {args.model} is not the authorized model {args.authorized_model}")
    if args.controls_file is not None and not (args.controls_file.is_absolute() and args.controls_file.is_file()):
        fail("controls file must be an absolute existing file")
    if args.gateway_metrics is not None and not (args.gateway_metrics.is_absolute() and args.gateway_metrics.is_file()):
        fail("gateway metrics file must be an absolute existing file")

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list) or len(fixtures) != 43:
        fail("manifest must carry exactly 43 fixtures")

    binding = json.loads(args.binding.read_text(encoding="utf-8"))
    if binding.get("schemaVersion") != 1 or not isinstance(binding.get("gatewayInstance"), str):
        fail("binding file is not a four-field schema-1 binding")
    expected_controls = manifest.get("refusalControls")
    if not isinstance(expected_controls, list) or not expected_controls:
        fail("manifest carries no refusalControls expectations")

    helper = args.anti.resolve()
    if not helper.is_file():
        fail("anti helper not found: " + str(helper))

    responses: list[dict[str, Any]] = []
    record: dict[str, Any] = {
        "schemaVersion": 1,
        "runId": args.run_id,
        "manifestSha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        "bindingSha256": hashlib.sha256(args.binding.read_bytes()).hexdigest(),
        "model": args.model,
        "gatewayBaseUrl": args.base_url,
        "startedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "responses": responses,
        "controls": [],
        "gateway": {},
    }
    if args.controls_file is not None:
        controls_doc = json.loads(args.controls_file.read_text(encoding="utf-8"))
        observed_rows = controls_doc.get("results")
        if not isinstance(observed_rows, list) or not observed_rows:
            fail("controls file has no results list")
        observed_ids: set[str] = set()
        observed_by_id: dict[str, str] = {}
        for row in observed_rows:
            if not isinstance(row, dict) or not isinstance(row.get("controlId"), str) or not isinstance(row.get("observed"), str):
                fail("controls file rows need controlId and observed strings")
            observed_ids.add(row["controlId"])
            observed_by_id[row["controlId"]] = row["observed"]
        expected_ids = {c["controlId"] for c in expected_controls}
        missing = sorted(expected_ids - observed_ids)
        unexpected = sorted(observed_ids - expected_ids)
        if missing:
            fail("controls file missing expected control(s): " + ", ".join(missing))
        allowed_extra = {"mint-healthy", "expired-token"}
        rejected = sorted(set(unexpected) - allowed_extra)
        if rejected:
            fail("controls file carries unexpected control(s): " + ", ".join(rejected))
        not_refused = sorted(c["controlId"] for c in expected_controls if observed_by_id.get(c["controlId"]) != "refused")
        if not_refused:
            fail("refusal controls not all refused; dispatch refused: " + ", ".join(not_refused))
        record["controls"] = observed_rows
    if args.gateway_metrics is not None:
        metrics = json.loads(args.gateway_metrics.read_text(encoding="utf-8"))
        rss = metrics.get("rssPeakBytes")
        if not isinstance(rss, (int, float)) or isinstance(rss, bool) or rss < 0:
            fail("gateway metrics need a non-negative numeric rssPeakBytes")
        record["gateway"] = {"rssPeakBytes": rss}
    commands = []
    for fixture in fixtures:
        clip = Path(fixture["clipPath"])
        if not clip.is_file():
            fail("clip missing on disk: " + fixture["clipId"])
        actual = sha256_file(clip)
        if actual != fixture["clipSha256"]:
            fail("clip hash mismatch for " + fixture["clipId"])
        validate_clip_profile(clip)
        prompt = fixture["question"]
        cmd = [
            sys.executable, str(helper), "listen",
            "--base-url", args.base_url,
            "--model", args.model,
            "--audio", str(clip),
            "--prompt", prompt,
            "--account-binding-json", str(args.binding),
            "--max-output-tokens", "4096",
            "--run-timeout", str(args.timeout),
            "--timeout", str(args.timeout),
            "--retry", "0",
            "--no-pre-read",
            "--probe-unverified-audio",
            "--json",
            "--run-id", args.run_id,
        ]
        commands.append((fixture, cmd))

    if args.dry_run:
        for fixture, cmd in commands:
            print(json.dumps({"clipId": fixture["clipId"], "clipClass": fixture["clipClass"], "command": cmd}))
        return 0

    newline = chr(10)
    args.run_record.write_text(json.dumps(record, indent=2) + newline, encoding="utf-8")
    os.chmod(args.run_record, 0o600)

    for index, (fixture, cmd) in enumerate(commands):
        if args.stop_after is not None and index >= args.stop_after:
            break
        started = time.monotonic()
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout + 30)
        latency = time.monotonic() - started
        entry = {
            "clipId": fixture["clipId"],
            "clipClass": fixture["clipClass"],
            "attempt": 1,
            "latencySeconds": round(latency, 3),
            "exitCode": completed.returncode,
        }
        try:
            payload = json.loads(completed.stdout)
            entry["outputText"] = payload.get("output_text")
            meta = payload.get("metadata") or {}
            entry["confidence"] = meta.get("confidence")
            entry["runStatus"] = payload.get("runStatus")
            entry["modelUsed"] = payload.get("model")
            try:
                entry["response"] = json.loads(payload.get("output_text")) if isinstance(payload.get("output_text"), str) else None
            except ValueError:
                entry["response"] = None
        except ValueError:
            entry["outputText"] = None
            entry["response"] = None
            entry["parseError"] = completed.stdout[-2000:]
        responses.append(entry)
        args.run_record.write_text(json.dumps(record, indent=2) + newline, encoding="utf-8")

    record["finishedAtUtc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    args.run_record.write_text(json.dumps(record, indent=2) + newline, encoding="utf-8")
    print(json.dumps({"dispatched": len(responses), "runRecord": str(args.run_record)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
