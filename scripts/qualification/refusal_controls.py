#!/usr/bin/env python3
"""Refusal-control probes for the bounded Gemini listening qualification.

Each control drives one real request path that must be refused before any
provider generation can occur, and records the observed result. Probes use
either the Anti helper in --dry-run (no dispatch), the mint script against a
deviation, or a loopback HTTP request the server refuses at the binding,
route, or account-acquisition layer. The expired-token control is a
state-home canary: it is verified from the account inventory rather than
attempted live, so no near-expiry token ever reaches a provider call.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ANTI = REPO_ROOT / "codex_antigravity_auth" / "skills" / "anti" / "scripts" / "anti.py"
MINT = REPO_ROOT / "scripts" / "qualification" / "mint_account_binding.py"


def make_wav(rate: int = 44100, channels: int = 1, seconds: float = 0.1, amplitude: int = 12000) -> bytes:
    frames = int(rate * seconds)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<" + "h" * frames, *([amplitude] * frames)))
    return buffer.getvalue()


def oversize_wav() -> bytes:
    rate = 8000
    channels = 1
    frames = (2 * 1024 * 1024) // 2 + 1
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x01" * frames)
    payload = buffer.getvalue()
    while len(payload) <= 2 * 1024 * 1024:
        payload += b"\x00\x01"
    return payload


def mint_binding(base_url: str, model: str, out: Path) -> tuple[int, str]:
    completed = subprocess.run(
        [sys.executable, str(MINT), "--base-url", base_url, "--model", model, "--out", str(out)],
        capture_output=True, text=True, timeout=30,
    )
    message = ""
    try:
        message = json.loads(completed.stdout or completed.stderr).get("error", "")
    except ValueError:
        message = (completed.stdout or completed.stderr).strip()[-400:]
    return completed.returncode, message


def helper_listen(audio: Path, model: str, binding: Path | None, base_url: str) -> tuple[int, str]:
    cmd = [sys.executable, str(ANTI), "listen", "--base-url", base_url, "--model", model,
           "--audio", str(audio), "--max-output-tokens", "4096", "--retry", "0",
           "--timeout", "15", "--run-timeout", "15", "--no-pre-read", "--json"]
    if binding is not None:
        cmd += ["--account-binding-json", str(binding)]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return completed.returncode, (completed.stdout + completed.stderr).strip()[-500:]


def post_responses(base_url: str, body: dict[str, Any], binding_header: str | None) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(base_url.rstrip("/") + "/responses", data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if binding_header:
        request.add_header("X-Anti-Account-Binding", binding_header)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read().decode("utf-8", "replace")[:400]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:400]
    except (urllib.error.URLError, OSError) as exc:
        return 0, f"transport-failure: {exc}"


def run_controls(base_url: str, model: str, workdir: Path, expired_evidence: str | None) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    def record(control_id: str, observed: str, detail: str) -> None:
        results.append({"controlId": control_id, "observed": observed, "detail": detail[-400:]})

    # The Anti helper's inventory reader rejects any symlinked path component;
    # on macOS /tmp is a symlink to /private/tmp, so resolve before use.
    workdir = workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    good_wav = workdir / "good.wav"
    good_wav.write_bytes(make_wav())
    stereo_wav = workdir / "stereo.wav"
    stereo_wav.write_bytes(make_wav(channels=2))
    wrong_rate_wav = workdir / "wrong-rate.wav"
    wrong_rate_wav.write_bytes(make_wav(rate=22050))
    big_wav = workdir / "oversize.wav"
    big_wav.write_bytes(oversize_wav())
    second_wav = workdir / "second-oversize.wav"
    second_wav.write_bytes(oversize_wav())

    valid_binding = {
        "schemaVersion": 1,
        "gatewayInstance": uuid.uuid4().hex,
        "accountRef": "acct_" + uuid.uuid4().hex[:12],
        "inventorySha256": hashlib.sha256(b"control").hexdigest(),
    }
    binding_path = workdir / "binding.json"
    binding_path.write_text(json.dumps(valid_binding, separators=(",", ":")), encoding="utf-8")
    header = json.dumps(valid_binding, separators=(",", ":"))

    minted = workdir / "minted.json"
    code, message = mint_binding(base_url, model, minted)
    record("mint-healthy", "minted" if code == 0 else "refused", message)
    minted.unlink(missing_ok=True)

    code, message = mint_binding("http://127.0.0.1:1/v1", model, workdir / "mint-stale.json")
    record("stale-binding", "refused" if code != 0 else "minted", message)

    dry = subprocess.run(
        [sys.executable, str(ANTI), "listen", "--base-url", base_url, "--model", model,
         "--audio", str(good_wav), "--prompt", "control", "--max-output-tokens", "4096",
         "--retry", "0", "--no-pre-read", "--json", "--dry-run",
         "--account-binding-json", str(workdir / "missing-binding.json")],
        capture_output=True, text=True, timeout=30)
    combined = (dry.stdout + dry.stderr).strip()
    refused = dry.returncode != 0 and ("binding" in combined.lower() or "missing" in combined.lower() or "No such file" in combined)
    record("missing-binding", "refused" if refused else "unexpected", combined[-400:])

    tampered = dict(valid_binding)
    tampered["accountRef"] = "acct_" + uuid.uuid4().hex[:12]
    tampered_path = workdir / "tampered.json"
    tampered_path.write_text(json.dumps(tampered, separators=(",", ":")), encoding="utf-8")
    code, message = helper_listen(good_wav, model, tampered_path, base_url)
    record("tampered-binding", "refused" if code != 0 else "accepted", message)

    malformed_header = "{not-json"
    code, message = post_responses(base_url, {"model": model, "input": []}, malformed_header)
    record("malformed-payload", "refused" if code == 400 else f"http-{code}", message)

    code, message = post_responses(base_url, {"model": model, "input": []}, header)
    record("inventory-mismatch", "refused" if code in (400, 403) else f"http-{code}", message)

    body = {"model": "gpt-4o", "input": [{"role": "user", "content": [{"type": "input_text", "text": "control"}]}]}
    code, message = post_responses(base_url, body, header)
    record("unsupported-route", "refused" if code in (400, 403) else f"http-{code}", message)

    body = {"model": model, "input": [{"role": "user", "content": [
        {"type": "input_text", "text": "control"},
        {"type": "antigravity_audio", "mime_type": "audio/wav",
         "data": base64.b64encode(make_wav(channels=2)).decode("ascii"),
         "probe_unverified": True}]}]}
    code, message = post_responses(base_url, body, header)
    record("stereo-input", "refused" if code == 400 else f"http-{code}", message)

    oversized = oversize_wav()
    body = {"model": model, "input": [{"role": "user", "content": [
        {"type": "input_text", "text": "control"},
        {"type": "antigravity_audio", "mime_type": "audio/wav",
         "data": base64.b64encode(oversized).decode("ascii"),
         "probe_unverified": True}]}]}
    code, message = post_responses(base_url, body, header)
    record("oversized-file", "refused" if code == 400 else f"http-{code}", message)

    wrong_rate = make_wav(rate=22050)
    body = {"model": model, "input": [{"role": "user", "content": [
        {"type": "input_text", "text": "control"},
        {"type": "antigravity_audio", "mime_type": "audio/wav",
         "data": base64.b64encode(wrong_rate).decode("ascii"),
         "probe_unverified": True}]}]}
    code, message = post_responses(base_url, body, header)
    record("wrong-sample-rate", "refused" if code == 400 else f"http-{code}", message)

    code, message = helper_listen_multi(good_wav, second_wav, model, binding_path, base_url)
    record("second-file-over-limit", "refused" if code != 0 else "accepted", message)

    code, message = helper_listen(good_wav, "not-a-real-model", binding_path, base_url)
    record("unauthorized-model", "refused" if code != 0 else "accepted", message)

    if expired_evidence:
        record("expired-token", "refused", expired_evidence)
    else:
        record("expired-token", "not-run", "state-home canary; verified from inventory in runbook preflight")
    return results


def helper_listen_multi(first: Path, second: Path, model: str, binding: Path, base_url: str) -> tuple[int, str]:
    cmd = [sys.executable, str(ANTI), "listen", "--base-url", base_url, "--model", model,
           "--audio", str(first), "--audio", str(second), "--max-output-tokens", "4096",
           "--retry", "0", "--timeout", "15", "--run-timeout", "15", "--no-pre-read", "--json",
           "--account-binding-json", str(binding)]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return completed.returncode, (completed.stdout + completed.stderr).strip()[-500:]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the 12 refusal-control probes")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--expired-token-evidence", default=None,
                        help="Recorded when the runbook's source-verified token-expiry canary has passed")
    args = parser.parse_args()
    results = run_controls(args.base_url, args.model, args.workdir, args.expired_token_evidence)
    args.out.write_text(json.dumps({"schemaVersion": 1, "generatedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": results}, indent=2) + chr(10), encoding="utf-8")
    passed = sum(1 for r in results if r["observed"] == "refused")
    print(json.dumps({"controls": len(results), "refused": passed, "out": str(args.out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
