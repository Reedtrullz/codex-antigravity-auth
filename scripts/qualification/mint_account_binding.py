#!/usr/bin/env python3
"""Mint one instance-scoped account binding from live gateway inventory.

Read-only against the gateway: GET /v1/account-bindings, then compose and
self-validate the four-field binding through the repository's own parser.
Never contacts a provider and never prints binding contents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import urllib.parse
import urllib.request
from typing import NoReturn
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from codex_antigravity_auth.account_binding import parse_account_binding_header


def fail(message: str) -> NoReturn:
    print(json.dumps({"error": message}))
    raise SystemExit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint a bound account binding file")
    parser.add_argument("--base-url", default="http://127.0.0.1:51131/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    url = f"{args.base_url.rstrip('/')}/account-bindings?model={urllib.parse.quote(args.model, safe='')}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            inventory = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"inventory read failed: {exc}")

    gateway_instance = inventory.get("gatewayInstance")
    inventory_sha = inventory.get("inventorySha256")
    rows = inventory.get("accounts")
    if not (isinstance(gateway_instance, str) and len(gateway_instance) == 32):
        fail("inventory gatewayInstance missing or malformed")
    if not (isinstance(inventory_sha, str) and len(inventory_sha) == 64):
        fail("inventory inventorySha256 missing or malformed")
    if not isinstance(rows, list) or not rows:
        fail("inventory has no accounts")

    eligible = [r for r in rows if isinstance(r, dict) and r.get("eligible") is True and r.get("inFlight") == 0]
    stale = [r for r in rows if isinstance(r, dict) and (r.get("eligible") is not True or r.get("inFlight") != 0)]
    if stale:
        fail(f"{len(stale)} stale or in-flight account(s) present; no-stale-accounts precondition fails")
    if not eligible:
        fail("no eligible account with inFlight 0")
    chosen = sorted(eligible, key=lambda r: str(r.get("accountRef")))[0]
    account_ref = chosen.get("accountRef")

    binding = {
        "schemaVersion": 1,
        "gatewayInstance": gateway_instance,
        "accountRef": account_ref,
        "inventorySha256": inventory_sha,
    }
    header = json.dumps(binding, separators=(",", ":"), sort_keys=True)
    try:
        parse_account_binding_header(header)
    except ValueError as exc:
        fail(f"composed binding rejected by repository parser: {exc}")

    out = Path(args.out)
    if out.exists() or out.is_symlink():
        fail("binding file already exists; refusing to overwrite a minted binding")
    if not out.is_absolute():
        fail("binding output path must be absolute")
    if not args.check_only:
        out.write_text(header + "\n", encoding="utf-8")
        os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)

    print(json.dumps({
        "minted": not args.check_only,
        "model": args.model,
        "inventorySha256": inventory_sha,
        "eligibleAccounts": len(eligible),
        "bindingSha256": hashlib.sha256(header.encode("utf-8")).hexdigest(),
        "bindingPath": str(out),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
