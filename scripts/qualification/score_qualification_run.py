#!/usr/bin/env python3
"""Independent deterministic scorer for the bounded listening qualification.

Not an LLM. No network. Standard library only. Reads the immutable fixture
manifest and one run record, then writes the verdict report exactly once.

Self-test: python3 score_qualification_run.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

GATES = {
    "coreMaxErrors": 2,
    "quietMinCorrect": 14,
    "repeatMinCorrect": 10,
    "refusalRequired": 12,
    "maxAcceptedWrong": 0,
    "latencyP95SecondsMax": 20.0,
    "rssPeakBytesMax": 2 * 1024**3,
}


def score_response(fixture, response):
    field = fixture["responseField"]
    if not isinstance(response, dict) or field not in response:
        return False, "malformed or missing response field"
    value = response[field]
    if fixture["clipClass"] == "core":
        ok = isinstance(value, int) and not isinstance(value, bool) and value == fixture["temporalAttackCount"]
        return ok, "integer attack count compared to ground truth"
    if fixture["clipClass"] == "repeat":
        ok = isinstance(value, int) and not isinstance(value, bool) and value == fixture["repeatPitch"]["count"]
        return ok, "integer repeat count compared to ground truth"
    expected = fixture["expectedQuietNotePresent"]
    ok = isinstance(value, bool) and value == expected
    return ok, "boolean quiet-note presence compared to ground truth"


def p95(values):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(0.95 * (len(ordered) - 1))))
    return ordered[index]


def score(manifest, run):
    by_key = {(f["clipId"], f["clipClass"]): f for f in manifest["fixtures"]}
    per_class = {c: {"total": 0, "correct": 0, "wrong": 0, "missing": 0} for c in ("core", "quiet", "repeat")}
    verdicts = []
    latencies = []
    accepted_wrong = 0
    for key in sorted(by_key):
        fixture = by_key[key]
        bucket = per_class[fixture["clipClass"]]
        bucket["total"] += 1
        entry = next((r for r in run.get("responses", []) if (r.get("clipId"), r.get("clipClass")) == key), None)
        if entry is None:
            bucket["missing"] += 1
            verdicts.append({"clipId": key[0], "clipClass": key[1], "verdict": "incorrect", "reason": "missing response"})
            continue
        if isinstance(entry.get("latencySeconds"), (int, float)):
            latencies.append(float(entry["latencySeconds"]))
        ok, reason = score_response(fixture, entry.get("response"))
        confident = isinstance(entry.get("confidence"), (int, float)) and entry["confidence"] >= 0.7
        if ok:
            bucket["correct"] += 1
            verdicts.append({"clipId": key[0], "clipClass": key[1], "verdict": "correct"})
        else:
            bucket["wrong"] += 1
            if confident:
                accepted_wrong += 1
            verdicts.append({"clipId": key[0], "clipClass": key[1], "verdict": "incorrect", "reason": reason})
    controls = {c.get("controlId"): c.get("observed") for c in run.get("controls", [])}
    controls_expected = {c["controlId"]: c["expected"] for c in manifest["refusalControls"]}
    controls_passed = sum(1 for cid, expected in controls_expected.items() if controls.get(cid) == expected)
    latency_p95 = p95(latencies)
    rss = run.get("gateway", {}).get("rssPeakBytes")
    gates = {
        "core": per_class["core"]["wrong"] <= GATES["coreMaxErrors"],
        "quiet": per_class["quiet"]["correct"] >= min(GATES["quietMinCorrect"], per_class["quiet"]["total"]),
        "repeat": per_class["repeat"]["correct"] >= min(GATES["repeatMinCorrect"], per_class["repeat"]["total"]),
        "refusals": controls_passed == GATES["refusalRequired"],
        "acceptedWrong": accepted_wrong <= GATES["maxAcceptedWrong"],
        "latencyP95": latency_p95 is not None and latency_p95 <= GATES["latencyP95SecondsMax"],
        "rssPeak": isinstance(rss, (int, float)) and rss <= GATES["rssPeakBytesMax"],
    }
    return {
        "runId": run.get("runId"),
        "perClass": per_class,
        "acceptedWrong": accepted_wrong,
        "refusalControlsPassed": controls_passed,
        "latencyP95Seconds": latency_p95,
        "rssPeakBytes": rss,
        "gates": gates,
        "admitted": all(gates.values()),
        "verdicts": verdicts,
        "gatesDefinition": GATES,
    }


def self_test():
    manifest = {
        "fixtures": [
            {"clipId": "a", "clipClass": "core", "responseField": "attackCount", "temporalAttackCount": 2},
            {"clipId": "b", "clipClass": "quiet", "responseField": "quietNotePresent", "expectedQuietNotePresent": True},
            {"clipId": "c", "clipClass": "repeat", "responseField": "repeatPitchCount", "repeatPitch": {"midi": 60, "count": 3}},
        ],
        "refusalControls": [{"controlId": "stale-binding", "expected": "refused"}],
    }
    run = {
        "runId": "selftest",
        "responses": [
            {"clipId": "a", "clipClass": "core", "response": {"attackCount": 2}, "latencySeconds": 2.0},
            {"clipId": "b", "clipClass": "quiet", "response": {"quietNotePresent": True}, "latencySeconds": 25.0},
            {"clipId": "c", "clipClass": "repeat", "response": {"repeatPitchCount": 2}, "latencySeconds": 3.0, "confidence": 0.9},
        ],
        "controls": [{"controlId": "stale-binding", "observed": "refused"}],
        "gateway": {"rssPeakBytes": 1024**3},
    }
    report = score(manifest, run)
    checks = [
        report["perClass"]["core"] == {"total": 1, "correct": 1, "wrong": 0, "missing": 0},
        report["perClass"]["repeat"] == {"total": 1, "correct": 0, "wrong": 1, "missing": 0},
        report["acceptedWrong"] == 1,
        report["latencyP95Seconds"] == 25.0,
        report["refusalControlsPassed"] == 1,
        report["gates"]["core"] is True,
        report["gates"]["repeat"] is False,
        report["gates"]["latencyP95"] is False,
        report["gates"]["acceptedWrong"] is False,
        report["admitted"] is False,
    ]
    ok = all(checks)
    print(json.dumps({"selfTest": "pass" if ok else "fail", "checks": checks}, sort_keys=True))
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(description="Independent qualification scorer")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--run", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not (args.manifest and args.run and args.out):
        parser.error("--manifest, --run and --out are required unless --self-test")
    manifest = json.loads(args.manifest.read_text())
    run = json.loads(args.run.read_text())
    report = score(manifest, run)
    payload = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    args.out.write_text(payload)
    print(json.dumps({"report": str(args.out), "sha256": hashlib.sha256(payload.encode()).hexdigest(), "admitted": report["admitted"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
